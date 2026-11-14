import io
import json
import os
import shlex
import tempfile
import tarfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import subprocess
import threading
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import boat_t3_build as cloud


class CloudTests(unittest.TestCase):
    def test_cache_retrieval_failure_preserves_job_failure_and_owned_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            args = SimpleNamespace(repo=root, revision='head', output=root / 'output', mode='test',
                                   test_file=['a.test.ts'], test_name=None, test_case=[], package=[],
                                   no_isolate=False, no_dependency_cache=False,
                                   dependency_cache=root / 'cache', name_prefix='job-',
                                   machine_type='large', ttl=600, min_included_hours=4)
            boat = cloud.Boat(args.output)
            created = []
            limits = dict(canStart=True, nextStartDollars=0, subscriptionRemainingSeconds=20000,
                          starts={w: dict(remaining=3) for w in ('minute', 'hour', 'day')},
                          machineTypes=[dict(type='large', available=True)])

            def call(*command, **kwargs):
                if command[0] == 'limits':
                    return json.dumps(limits)
                if command[0] == 'new':
                    boat.box = 'bx_owned'
                    created.append(command)
                if command[0] == 'ssh':
                    raise RuntimeError('original check failed')
                if command[0] == 'scp' and command[1].endswith('pnpm-cache-status.json'):
                    raise OSError('cache download failed')
                return ''

            def archive_command(command, **kwargs):
                path = Path(command[command.index('-o') + 1])
                with tarfile.open(path, 'w:gz') as archive:
                    item = tarfile.TarInfo('package.json')
                    item.size = 2
                    archive.addfile(item, io.BytesIO(b'{}'))

            with patch.object(cloud, 'Boat', return_value=boat), \
                    patch.object(boat, 'call', side_effect=call), \
                    patch.object(boat, 'cleanup') as cleanup, \
                    patch.object(cloud.subprocess, 'check_output', side_effect=[
                        'fullsha\n', b'{"engines":{"node":"26.12.1"},"packageManager":"pnpm@12.14.0"}', b'lock']), \
                    patch.object(cloud.subprocess, 'run', side_effect=archive_command):
                with self.assertRaisesRegex(RuntimeError, 'original check failed'):
                    cloud.build(args)
                cleanup.assert_called_once()
            self.assertIn('cache download failed', (args.output / 'dependency-cache.json').read_text())
            new = created[0]
            self.assertEqual(new[new.index('--name') + 1], 'job-fullsha')
            self.assertEqual(new[new.index('--ttl') + 1], '600')
            self.assertEqual(new[new.index('--type') + 1], 'large')

    def dependency_archive(self, path, name='store/package', content=b'public package'):
        with tarfile.open(path, 'w:gz') as archive:
            item = tarfile.TarInfo(name)
            item.size = len(content)
            archive.addfile(item, io.BytesIO(content))

    def test_dependency_key_invalidates_lock_and_toolchain_changes(self):
        key = cloud.dependency_cache_key(b'lock', '26.12.1', '12.14.0')
        self.assertIn(cloud.hashlib.sha256(b'lock').hexdigest(), key)
        for lock, node, pnpm in [(b'new lock', '26.12.1', '12.14.0'),
                                 (b'lock', '26.12.2', '12.14.0'),
                                 (b'lock', '26.12.1', '12.14.1')]:
            self.assertNotEqual(key, cloud.dependency_cache_key(lock, node, pnpm))

    def test_dependency_cache_corrupt_incomplete_and_atomic_concurrent_publish(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            archive = root / 'download.tar.gz'
            self.dependency_archive(archive)
            cache = cloud.DependencyCache(root / 'cache', 'key')
            self.assertIsNone(cache.lookup())
            entered, release = threading.Event(), threading.Event()
            copyfile = cloud.shutil.copyfile

            def paused_copy(*args):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test did not release cache writer')
                return copyfile(*args)

            with patch.object(cloud.shutil, 'copyfile', side_effect=paused_copy):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(cache.publish, archive)
                    self.assertTrue(entered.wait(5))
                    self.assertIsNone(cache.lookup())  # An incomplete staging directory is invisible.
                    second = pool.submit(cache.publish, archive)
                    release.set()
                    first.result(timeout=5)
                    second.result(timeout=5)
            self.assertEqual(cache.lookup()[1], cloud.file_sha256(archive))
            (cache.entry / 'store.tar.gz').write_bytes(b'corrupt')
            self.assertIsNone(cache.lookup())
            cache.publish(archive)
            self.assertIsNotNone(cache.lookup())
            (cache.entry / 'manifest.json').unlink()
            self.assertIsNone(cache.lookup())
            cache.publish(archive)
            self.assertIsNotNone(cache.lookup())
            previous = cache.lookup()[1]
            self.dependency_archive(archive, content=b'clean replacement')
            cache.publish(archive, rejected_sha256=previous)
            self.assertEqual(cache.lookup()[1], cloud.file_sha256(archive))
            self.dependency_archive(archive, '../escape')
            with self.assertRaises(RuntimeError):
                cache.publish(archive)

    def test_dependency_install_cold_warm_and_corrupt_restore_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            fake = bin_dir / 'pnpm'
            fake.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
store = pathlib.Path(args[args.index('--store-dir') + 1])
store.mkdir(parents=True, exist_ok=True)
with open(os.environ['CALLS'], 'a') as out:
    out.write(json.dumps({'args':args, 'restored':(store / 'package').exists()}) + '\\n')
if os.environ.get('FAIL_RESTORED') and (store / 'package').exists():
    sys.exit(1)
(store / 'package').write_text('public package')
''')
            fake.chmod(0o755)
            env = os.environ | {'PATH': str(bin_dir) + ':' + os.environ['PATH'],
                                'CALLS': str(root / 'calls')}

            def install(checksum):
                script = cloud.dependency_install_script(checksum)
                script = script.replace('/home/user/pnpm-cache', str(root / 'remote'))
                script = script.replace('/tmp/pnpm-', str(root / 'pnpm-'))
                return subprocess.run(['bash', '-euc', script], cwd=root, env=env,
                                      text=True, capture_output=True)

            self.assertEqual(install('').returncode, 0)
            status = json.loads((root / 'pnpm-cache-status.json').read_text())
            self.assertEqual(status, {'restored': False, 'exported': True})
            cache = cloud.DependencyCache(root / 'cache', 'key')
            cache.publish(root / 'pnpm-store-export.tar.gz')
            archive, checksum = cache.lookup()
            cloud.shutil.copyfile(archive, root / 'pnpm-store.tar.gz')
            cloud.shutil.rmtree(root / 'remote')
            self.assertEqual(install(checksum).returncode, 0)
            self.assertEqual(json.loads((root / 'pnpm-cache-status.json').read_text()),
                             {'restored': True, 'exported': False})
            calls = [json.loads(line) for line in (root / 'calls').read_text().splitlines()]
            self.assertEqual([call['restored'] for call in calls], [False, True])
            for call in calls:
                self.assertIn('--frozen-lockfile', call['args'])
                self.assertIn('--side-effects-cache=false', call['args'])
                self.assertIn('--verify-store-integrity=true', call['args'])
            (root / 'pnpm-store.tar.gz').write_bytes(b'truncated upload')
            cloud.shutil.rmtree(root / 'remote')
            self.assertEqual(install(checksum).returncode, 0)
            self.assertEqual(json.loads((root / 'pnpm-cache-status.json').read_text()),
                             {'restored': False, 'exported': True})
            cloud.shutil.copyfile(archive, root / 'pnpm-store.tar.gz')
            cloud.shutil.rmtree(root / 'remote')
            env['FAIL_RESTORED'] = '1'
            self.assertEqual(install(checksum).returncode, 0)
            calls = [json.loads(line) for line in (root / 'calls').read_text().splitlines()]
            self.assertEqual([call['restored'] for call in calls[-2:]], [True, False])
            self.assertEqual(json.loads((root / 'pnpm-cache-status.json').read_text()),
                             {'restored': False, 'exported': True})

    def test_reject_chargeable_start_or_depleted_quota(self):
        good = dict(canStart=True, nextStartDollars=0, subscriptionRemainingSeconds=20000,
                    starts={w: dict(remaining=3) for w in ('minute', 'hour', 'day')},
                    machineTypes=[dict(type='large', available=True)])
        cloud.check_limits(good)
        for change in [dict(nextStartDollars=.03), dict(subscriptionRemainingSeconds=1), dict(canStart=False)]:
            with self.assertRaises(RuntimeError):
                cloud.check_limits(good | change)
        cloud.check_limits(good, hours=5)
        with self.assertRaises(RuntimeError):
            cloud.check_limits(good, hours=6)
        with self.assertRaisesRegex(RuntimeError, 'small machine unavailable'):
            cloud.check_limits(good, machine_type='small')

    def test_cli_rejects_bad_name_prefix_before_any_work(self):
        script = Path(cloud.__file__)
        result = subprocess.run(['python3', str(script), '--repo', '/nonexistent', '--revision', 'x',
                                 '--output', '/nonexistent/out', '--name-prefix', 'Bad Prefix'],
                                capture_output=True, text=True, env={**os.environ, 'PATH': '/usr/bin:/bin'})
        self.assertEqual(result.returncode, 2)
        self.assertIn('name prefix', result.stderr)
        result = subprocess.run(['python3', str(script), '--repo', '/nonexistent', '--revision', 'x',
                                 '--output', '/nonexistent/out', '--ttl', '0'],
                                capture_output=True, text=True, env={**os.environ, 'PATH': '/usr/bin:/bin'})
        self.assertEqual(result.returncode, 2)
        self.assertIn('--ttl must be positive', result.stderr)

    def test_source_rejects_secrets_and_symlinks(self):
        with tempfile.TemporaryDirectory() as d:
            for name, link in [('src/ok.ts', False), ('.env', False), ('x/auth.json', False), ('src/link', True)]:
                path = Path(d) / 'source.tar'
                with tarfile.open(path, 'w') as archive:
                    item = tarfile.TarInfo(name)
                    if link:
                        item.type = tarfile.SYMTYPE
                        item.linkname = '/home/user/credentials/secret'
                        archive.addfile(item)
                    else:
                        item.size = 1
                        archive.addfile(item, io.BytesIO(b'x'))
                if name == 'src/ok.ts':
                    cloud.validate_archive(path)
                else:
                    with self.assertRaises(RuntimeError):
                        cloud.validate_archive(path)

    def test_remote_failure_despite_cli_success(self):
        with tempfile.TemporaryDirectory() as d:
            boat = cloud.Boat(Path(d))
            with patch.object(boat, 'call', return_value='{"exitCode":1,"success":false}\n'):
                with self.assertRaises(RuntimeError):
                    boat.execute('false', 1)

    def test_tracks_created_id_on_readiness_failure_timeout_and_interrupt(self):
        for failure in [None, subprocess.TimeoutExpired('boat', 1), KeyboardInterrupt()]:
            with tempfile.TemporaryDirectory() as d:
                boat = cloud.Boat(Path(d))
                def spawn(*args, **kwargs):
                    kwargs['stdout'].write('{"event":"created","id":"bx_owned"}\n')
                    kwargs['stdout'].flush()
                    process = Mock()
                    process.wait.side_effect = [failure, 0] if failure else [1]
                    return process
                with patch('subprocess.Popen', side_effect=spawn):
                    with self.assertRaises((subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyboardInterrupt)):
                        boat.call('new')
                self.assertEqual(boat.box, 'bx_owned')

    def test_scoped_commands_quote_handoff_values_and_continue_after_failure(self):
        script = cloud.remote_script('26.12.1', '12.14.0', 'test', ['path with space.test.ts'],
                                     ['t3', '@t3tools/web'], "keeps 'work' alive", [('menu.test.ts', '')], no_isolate=True)
        task = shlex.split(script[script.index('/usr/bin/time -v bash -c '):script.index('\ncd /home/user/artifacts')])[-1]
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / 'pnpm'
            fake.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\nsys.exit(1 if "path with space.test.ts" in sys.argv else 0)\n')
            fake.chmod(0o755)
            result = subprocess.run(['bash', '-c', task], env=os.environ | {'PATH': d + ':' + os.environ['PATH']}, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        commands = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('[')]
        self.assertEqual(commands[0], ['exec', 'vp', 'test', 'run', '--maxWorkers=1', '--no-isolate', 'path with space.test.ts', '-t', "keeps 'work' alive"])
        self.assertIn('menu.test.ts', commands[1])
        self.assertEqual(commands[2], ['--fail-if-no-match', '--filter', 't3', 'exec', 'effect-tsgo', 'tsrs', '--noEmit'])
        self.assertEqual(commands[3], ['--fail-if-no-match', '--filter', '@t3tools/web', 'exec', 'effect-tsgo', 'tsrs', '--noEmit'])
        self.assertIn('CHECK_STATUS typecheck:t3 0', result.stdout)
        self.assertIn('CHECK_STATUS typecheck:@t3tools/web 0', result.stdout)
        self.assertIn('CHECK_STATUS test-1 1', result.stdout)
        self.assertIn('CHECK_STATUS test-2 0', result.stdout)
        self.assertNotIn('dist:desktop', task)

    def test_unknown_package_and_filter_expressions_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'source.tar'
            with tarfile.open(path, 'w') as archive:
                item = tarfile.TarInfo('apps/server/package.json')
                content = b'{"name":"t3"}'
                item.size = len(content)
                archive.addfile(item, io.BytesIO(content))
            cloud.validate_packages(path, ['t3'])
            for package in ['typo', 't3...']:
                with self.assertRaises(RuntimeError):
                    cloud.validate_packages(path, [package])

    def test_web_browser_runtime_installed_before_checks_only_when_requested(self):
        install = 'node apps/web/node_modules/playwright/cli.js install --with-deps --only-shell chromium'
        for files, cases, needed in [
            (['apps/web/src/chat.browser.test.tsx'], [], True),
            ([], [('apps/web/src/guest.browser.test.tsx', 'GuestPage')], True),
            (['apps/web/src/chat.logic.test.ts'], [], False),
        ]:
            script = cloud.remote_script('26.12.1', '12.14.0', 'test', files, test_cases=cases)
            self.assertEqual(install in script, needed)
            if needed:
                self.assertLess(script.index('pnpm install --frozen-lockfile'), script.index(install))
                self.assertLess(script.index(install), script.index('/usr/bin/time -v'))
            subprocess.run(['bash', '-n'], input=script, text=True, check=True)

    def test_cleanup_only_owned_box_and_detects_failure(self):
        with tempfile.TemporaryDirectory() as d:
            boat = cloud.Boat(Path(d))
            boat.box = 'bx_owned'
            with patch.object(boat, 'call', side_effect=['{"operation":{}}', '{"sandboxes":[{"id":"bx_other"}]}']) as call:
                boat.cleanup()
                self.assertEqual(call.call_args_list[0].args, ('delete', 'bx_owned', '--yes'))
            with patch.object(boat, 'call', side_effect=['{"operation":{}}', '{"sandboxes":[{"id":"bx_owned"}]}']):
                with self.assertRaises(RuntimeError):
                    boat.cleanup()


if __name__ == '__main__':
    unittest.main()
