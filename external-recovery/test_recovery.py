import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest

# Never pick up the real user's settings file.
os.environ['T3_RECOVERY_CONFIG'] = '/nonexistent/t3-external-recovery-test.json'
spec = importlib.util.spec_from_file_location('recovery', Path(__file__).with_name('recovery.py'))
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class RecoveryTests(unittest.TestCase):
    def test_outage_debounce_and_durable_no_replay_after_window_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            monitor = recovery.Monitor(state)
            self.assertIsNotNone(monitor.update({'status': 'up', 'identity': 'boot:1:10'}, False))
            self.assertIsNone(monitor.update({'status': 'down'}, False))
            self.assertIn('stopped', monitor.update({'status': 'down'}, False))
            resumed = recovery.Monitor(state)
            self.assertIsNone(resumed.update({'status': 'down'}, False))
            self.assertIsNone(resumed.update({'status': 'down'}, False))
            self.assertIsNotNone(resumed.update({'status': 'up', 'identity': 'boot:2:20'}, False))

    def test_explicit_pause_and_planned_reload_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            state = home / 'recovery'
            state.mkdir()
            marker = home / 'maintenance/pause-until'
            files = [str(home / 'absent'), str(marker)]
            self.assertFalse(recovery.paused(state, files, now=1))
            (state / 'pause').touch()
            self.assertTrue(recovery.paused(state, files, now=1))
            (state / 'pause').unlink()
            marker.parent.mkdir(parents=True)
            marker.write_text('1970-01-01T00:00:10Z')
            self.assertTrue(recovery.paused(state, files, now=1))
            self.assertFalse(recovery.paused(state, files, now=11))
            self.assertFalse(recovery.paused(state, [], now=1))
            for text in ('invalid', '', '1970-01-01T00:00:10'):  # malformed, bare, no time zone
                marker.write_text(text)
                self.assertTrue(recovery.paused(state, files, now=11))

    def test_start_rechecks_pause_and_health_without_launching(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            state = home / 'state'
            state.mkdir()
            calls = []
            run = lambda *args, **kwargs: calls.append(args)
            for status in ('up', 'unknown'):
                result = recovery.start_t3(state, lambda: {'status': status}, run)
                self.assertEqual(result['status'], 'held')
            (state / 'pause').touch()
            result = recovery.start_t3(state, lambda: {'status': 'down'}, run)
            self.assertEqual(result['status'], 'held')
            self.assertEqual(calls, [])

    def test_recovery_launch_is_fixed_and_separate_from_kitty_cgroup(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            calls = []
            def run(command, **kwargs):
                calls.append(command)
                return types.SimpleNamespace(returncode=0)
            config = {**recovery.DEFAULTS, 'launcher': str(home / 'bin/t3code')}
            result = recovery.start_t3(home / 'state', lambda: {'status': 'down'}, run, config)
            self.assertEqual(result['status'], 'requested')
            self.assertFalse(result['healthy'])
            self.assertIn('--unit=t3code-recovered', calls[0])
            self.assertIn('--setenv=T3CODE_BACKGROUND_GIT=0', calls[0])
            self.assertEqual(calls[0][-4:], ['/usr/bin/env', '-u', 'ELECTRON_RUN_AS_NODE',
                                            str(home / 'bin/t3code')])
            config = {**config, 'recovered_unit': 'other', 'launcher_env': {}}
            recovery.start_t3(home / 'state', lambda: {'status': 'down'}, run, config)
            self.assertIn('--unit=other', calls[1])
            self.assertFalse(any('T3CODE_BACKGROUND_GIT' in arg for arg in calls[1]))
            self.assertNotIn('kill', calls[0])

    def test_exact_session_and_model_persist_without_fallback(self):
        for initialized in (False, True):
            command = recovery.claude_argv({'id': 'fixed-session', 'initialized': initialized}, 'hi')
            self.assertEqual(command[-3:], ['--resume' if initialized else '--session-id', 'fixed-session', 'hi'])
            self.assertNotIn('--print', command)
            self.assertIn('--append-system-prompt', command)
            self.assertEqual(command[command.index('--model') + 1], 'claude-opus-5-5')
            self.assertEqual(command[command.index('--effort') + 1], 'low')
            self.assertNotIn('--fallback-model', command)

    def test_draft_detection_ignores_grey_hints_and_finds_typed_text(self):
        sep = '\x1b[38:2:136:136:136m' + '─' * 20
        status = '\x1b[m  bypass'
        typed = '\n'.join(['old', sep + ' T3 Recovery ─', '\x1b[m❯\xa0Did you stop', sep, status])
        empty = '\n'.join(['old', sep, '\x1b[m❯\xa0\x1b[38:2:153:153:153mTry "fix"\x1b[m', sep, status])
        self.assertEqual(recovery.draft(typed), 'Did you stop')
        self.assertEqual(recovery.draft(empty), '')
        self.assertIsNone(recovery.draft('no box here'))

    def test_sgr_reset_combined_and_256_grey(self):
        self.assertEqual(recovery.visible_typed('\x1b[38;2;9;9;9mhint\x1b[0;39mtyped'), 'typed')
        self.assertEqual(recovery.visible_typed('\x1b[2mhint\x1b[22;39mtyped'), 'typed')
        self.assertEqual(recovery.visible_typed('\x1b[38;5;244mhint'), '')
        self.assertEqual(recovery.visible_typed('\x1b[38;5;196mred'), 'red')
        self.assertEqual(recovery.visible_typed('\x1b[90mhint\x1b[mok'), 'ok')

    def courier(self, boxes):
        sent = []
        original = recovery.draft
        recovery.draft = lambda s: s
        self.addCleanup(setattr, recovery, 'draft', original)
        c = recovery.Courier(lambda: boxes.pop(0), lambda t: sent.append(('paste', t)),
                             lambda: sent.append(('cr',)), settle=0)
        return c, sent

    def test_courier_clean_send_verifies_box_before_cr(self):
        c, sent = self.courier(['', 'N'])
        c.offer('N')
        self.assertEqual(c.attempt(), 'sent')
        self.assertEqual(sent, [('paste', 'N'), ('cr',)])

    def test_courier_never_submits_text_typed_during_paste(self):
        c, sent = self.courier(['', 'hiN'])
        c.offer('N')
        self.assertEqual(c.attempt(), 'pasted-unsubmitted')
        self.assertEqual(sent, [('paste', 'N')])
        self.assertIsNone(c.pending)

    def test_courier_resubmits_landed_paste_without_duplicate(self):
        c, sent = self.courier(['N'])
        c.offer('N')
        self.assertEqual(c.attempt(), 'sent')
        self.assertEqual(sent, [('cr',)])

    def test_courier_never_types_into_a_draft_however_long_it_sits(self):
        c, sent = self.courier(['A', 'A', None, '', 'N'])
        c.offer('old'); c.offer('N')
        self.assertEqual(c.attempt(), 'deferred')
        self.assertEqual(c.attempt(), 'deferred')
        self.assertEqual(c.attempt(), 'unknown-screen')
        self.assertEqual(sent, [])
        self.assertEqual(c.attempt(), 'sent')  # box emptied: newest notice delivered
        self.assertEqual(sent, [('paste', 'N'), ('cr',)])

    def test_courier_failure_keeps_newest_pending(self):
        c, sent = self.courier(['', '', 'N'])
        c.paste_fn = lambda t: (_ for _ in ()).throw(OSError())
        c.offer('N')
        self.assertTrue(c.attempt().startswith('failed'))
        self.assertEqual(c.pending, 'N')
        c.paste_fn = lambda t: sent.append(('paste', t))
        self.assertEqual(c.attempt(), 'sent')

    def test_courier_usage_event_and_t3_observation_do_not_overwrite(self):
        c, sent = self.courier(['', 'T', '', 'U'])
        c.offer('T'); c.offer('U', key='usage:codex:limit-hit:1')
        self.assertEqual(c.attempt(), 'sent')
        self.assertEqual(c.attempt(), 'sent')
        self.assertEqual(sent, [('paste', 'T'), ('cr',), ('paste', 'U'), ('cr',)])
        self.assertIsNone(c.pending)

    def write_events(self, path, *events):
        import json
        with path.open('a') as f:
            for e in events:
                f.write(json.dumps(e) + '\n')

    def test_usage_inbox_delivers_each_category_transition_once(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); events = root / 'events.jsonl'
            hit = dict(id='codex:limit-hit:100', category='codex', label='Codex', transition='limit-hit',
                       first_seen=100, confirmed_at=1000, earliest_return=1794405718)
            back = dict(id='codex:available-again:200', category='codex', label='Codex',
                        transition='available-again', first_seen=200, confirmed_at=1100)
            self.write_events(events, hit)
            inbox = recovery.UsageInbox(root, events, now=lambda: 1200)
            notes = inbox.poll()
            self.assertEqual([k for k, _ in notes], ['codex:limit-hit:100'])
            self.assertIn('Codex category LIMIT HIT', notes[0][1])
            self.assertIn('grants no new authority', notes[0][1])
            self.assertEqual(inbox.poll(), [])
            self.write_events(events, hit, back)  # a duplicate id (crash replay) is skipped
            with events.open('a') as f:
                f.write('{"id": "partial')  # half-written line waits
            notes = recovery.UsageInbox(root, events, now=lambda: 1200).poll()
            self.assertEqual([k for k, _ in notes], ['codex:available-again:200'])
            self.assertIn('AVAILABLE AGAIN', notes[0][1])

    def test_usage_inbox_first_start_skips_stale_backlog(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); events = root / 'events.jsonl'
            self.write_events(events,
                dict(id='fable:limit-hit:1', category='fable', transition='limit-hit', confirmed_at=1),
                dict(id='fable:available-again:2', category='fable', transition='available-again', confirmed_at=9500))
            notes = recovery.UsageInbox(root, events, now=lambda: 10000).poll()
            self.assertEqual([k for k, _ in notes], ['fable:available-again:2'])
            self.assertEqual(recovery.UsageInbox(root, Path(d) / 'missing', now=lambda: 0).poll(), [])

    def test_usage_inbox_replaced_file_of_equal_or_larger_size_is_read(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); events = root / 'events.jsonl'
            a = dict(id='codex:limit-hit:1', category='codex', transition='limit-hit', confirmed_at=990)
            b = dict(id='codex:available-again:2', category='codex', transition='available-again', confirmed_at=995)
            self.write_events(events, a)
            inbox = recovery.UsageInbox(root, events, now=lambda: 1000)
            self.assertEqual(len(inbox.poll()), 1)
            tmp = root / 'new.jsonl'
            self.write_events(tmp, b, a)  # replacement at least as long as the old offset
            os.replace(tmp, events)
            notes = recovery.UsageInbox(root, events, now=lambda: 1000).poll()
            self.assertEqual([k for k, _ in notes], ['codex:available-again:2'])

    def test_window_supervision_and_emergency_stop_integration(self):
        base = Path(__file__).parent
        unit = (base / 't3-external-recovery.service').read_text()
        self.assertIn('Restart=always', unit)
        self.assertIn('KillMode=control-group', unit)
        self.assertNotIn('/home/', unit)

    def test_config_defaults_overrides_and_unknown_keys(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'config.json'
            self.assertEqual(recovery.load_config(path), recovery.DEFAULTS)
            path.write_text(json.dumps({'port': 4000, 'model': 'claude-sonnet-5-5'}))
            config = recovery.load_config(path)
            self.assertEqual((config['port'], config['model'], config['effort']), (4000, 'claude-sonnet-5-5', 'low'))
            path.write_text(json.dumps({'prot': 4000}))
            with self.assertRaises(SystemExit):
                recovery.load_config(path)

    def test_prompt_file_and_model_settings_reach_claude(self):
        with tempfile.TemporaryDirectory() as d:
            prompt = Path(d) / 'prompt.md'
            prompt.write_text('Custom role. Start with {recovery} start-t3 on port {port}.')
            config = {**recovery.DEFAULTS, 'prompt_file': str(prompt), 'model': 'm', 'effort': 'high',
                      'claude': '/opt/claude', 'port': 4000}
            command = recovery.claude_argv({'id': 's', 'initialized': False}, 'hi', config)
            self.assertEqual(command[0], '/opt/claude')
            self.assertEqual(command[command.index('--model') + 1], 'm')
            text = command[command.index('--append-system-prompt') + 1]
            self.assertTrue(text.startswith('Custom role.'))
            self.assertIn('recovery.py start-t3 on port 4000', text)
        default = recovery.prompt_text(recovery.DEFAULTS)
        self.assertIn('scan_stopped.py --eligible', default)
        self.assertNotIn('{', default)

    def test_environment_strips_parent_session_and_proxy_is_opt_in(self):
        parent = {'PATH': '/bin', 'CLAUDECODE': '1', 'CLAUDE_CODE_ENTRYPOINT': 'x', 'T3_THREAD': 't',
                  'ELECTRON_RUN_AS_NODE': '1', 'CLAUDE_CODE_OAUTH_TOKEN': 'oauth'}
        env = recovery.claude_environment(recovery.DEFAULTS, parent)
        self.assertEqual(env, {'PATH': '/bin', 'CLAUDE_CODE_OAUTH_TOKEN': 'oauth'})
        # The agent's own recovery.py / scan_stopped.py calls must see the host's settings.
        own = {'T3_RECOVERY_CONFIG': '/etc/x.json', 'T3_STATE_DB': '/srv/state.sqlite'}
        env = recovery.claude_environment(recovery.DEFAULTS, {**parent, **own})
        self.assertEqual(env, {'PATH': '/bin', 'CLAUDE_CODE_OAUTH_TOKEN': 'oauth', **own})
        with tempfile.TemporaryDirectory() as d:
            settings = Path(d) / 'settings.json'
            proxy = {**recovery.DEFAULTS, 'anthropic_base_url': 'http://127.0.0.1:9999'}
            with self.assertRaises(RuntimeError):
                recovery.claude_environment(proxy, parent, settings)
            settings.write_text(json.dumps({'env': {'ANTHROPIC_AUTH_TOKEN': 'fixture-token'}}))
            env = recovery.claude_environment(proxy, parent, settings)
            self.assertEqual(env['ANTHROPIC_BASE_URL'], 'http://127.0.0.1:9999')
            self.assertEqual(env['ANTHROPIC_AUTH_TOKEN'], 'fixture-token')
            self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', env)
            env = recovery.claude_environment(proxy, {**parent, 'T3_RECOVERY_CONFIG': '/etc/x.json'}, settings)
            self.assertEqual(env['T3_RECOVERY_CONFIG'], '/etc/x.json')

    def test_transcript_follows_claude_code_project_folder(self):
        self.assertEqual(recovery.project_slug('/home/u/code/my.project'), '-home-u-code-my-project')
        path = recovery.transcript({'id': 'abc'}, '/tmp')
        self.assertEqual(path.parent.name, recovery.project_slug(Path('/tmp').resolve()))
        self.assertEqual(path.name, 'abc.jsonl')

    def test_cli_config_pause_status_use_only_the_configured_state_dir(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / 'state'
            config = Path(d) / 'config.json'
            config.write_text(json.dumps({'state_dir': str(state)}))
            env = {**os.environ, 'T3_RECOVERY_CONFIG': str(config)}
            script = str(Path(__file__).with_name('recovery.py'))
            run = lambda *a: subprocess.run([sys.executable, script, *a], env=env, capture_output=True,
                                            text=True, timeout=30, check=True).stdout
            self.assertEqual(json.loads(run('config'))['state_dir'], str(state))
            self.assertTrue(json.loads(run('pause'))['paused'])
            self.assertTrue((state / 'pause').exists())
            self.assertFalse(json.loads(run('unpause'))['paused'])
            self.assertEqual(sorted(p.name for p in state.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
