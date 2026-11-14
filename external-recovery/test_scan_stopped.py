"""Offline tests for scan_stopped.py: in-memory or temporary SQLite only, never T3's real state."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

os.environ['T3_RECOVERY_CONFIG'] = '/nonexistent/t3-external-recovery-test.json'
spec = importlib.util.spec_from_file_location('scan_stopped', Path(__file__).with_name('scan_stopped.py'))
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)
MODEL = {'instanceId': 'codex', 'model': 'gpt-6.1-sol'}
SCHEMA = '''
  CREATE TABLE orchestration_v2_projection_threads(thread_id TEXT, payload_json TEXT);
  CREATE TABLE orchestration_v2_projection_runs(thread_id TEXT,run_id TEXT,status TEXT,
    completed_at TEXT,payload_json TEXT,ordinal INTEGER);
  CREATE TABLE orchestration_v2_projection_runtime_requests(thread_id TEXT,status TEXT);
  CREATE TABLE orchestration_v2_projection_turn_items(thread_id TEXT,type TEXT,payload_json TEXT);
  CREATE TABLE orchestration_command_receipts(command_id TEXT);
  CREATE TABLE orchestration_v2_projection_messages(thread_id TEXT,message_id TEXT);
  CREATE TABLE orchestration_v2_effect_outbox(effect_id TEXT);
'''


class StoppedSources(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(':memory:')
        self.addCleanup(self.c.close)
        self.c.executescript(SCHEMA)
        self.c.execute('INSERT INTO orchestration_v2_projection_threads VALUES(?,?)',
                       ('thread', json.dumps({'modelSelection': MODEL})))
        self.run_row(1, 'failed')

    def run_row(self, ordinal, status, **fields):
        raw = dict(startedAt='2025-01-02T12:00:00Z', modelSelection=MODEL, **fields)
        self.c.execute('INSERT INTO orchestration_v2_projection_runs VALUES(?,?,?,?,?,?)',
                       ('thread', 'run:' + str(ordinal), status, '2025-01-02T12:01:00Z', json.dumps(raw), ordinal))

    def sources(self, **kwargs):
        return list(scan.candidates(self.c, retired=kwargs.pop('retired', set()), **kwargs))

    def test_all_stop_causes_have_same_canonical_identity(self):
        for status in ('failed', 'interrupted', 'cancelled'):
            self.c.execute('UPDATE orchestration_v2_projection_runs SET status=?', (status,))
            row, = self.sources()
            self.assertIsNone(row['skip'])
            self.assertEqual(row['command_id'], 'command:restart-continuation:run:1')

    def test_cancelled_queued_row_does_not_revive_running_older_turn(self):
        self.c.execute("UPDATE orchestration_v2_projection_runs SET status='running'")
        self.c.execute('INSERT INTO orchestration_v2_projection_runs VALUES(?,?,?,?,?,?)',
                       ('thread', 'run:2', 'cancelled', 'now', json.dumps({'startedAt': None}), 2))
        self.assertEqual(self.sources(), [])

    def test_any_active_run_holds_stopped_source_even_if_older(self):
        self.run_row(0, 'running')
        self.assertEqual(self.sources()[0]['skip'], 'existing active or queued run')

    def test_new_completed_turn_supersedes_old_source(self):
        self.run_row(2, 'completed')
        self.assertEqual(self.sources(), [])

    def test_never_started_cancelled_message_is_not_prior_work(self):
        self.c.execute('UPDATE orchestration_v2_projection_runs SET payload_json=?', (json.dumps({'startedAt': None}),))
        self.assertEqual(self.sources(), [])

    def test_retired_and_human_pending_are_never_offered(self):
        self.assertEqual(self.sources(retired={'thread'})[0]['skip'], 'retired role')
        self.c.execute("INSERT INTO orchestration_v2_projection_runtime_requests VALUES('thread','pending')")
        self.assertEqual(self.sources()[0]['skip'], 'pending human request')

    def test_pending_secret_is_not_answered(self):
        self.c.execute('INSERT INTO orchestration_v2_projection_turn_items VALUES(?,?,?)',
                       ('thread', 'secret_request', '{"secretStatus":"pending"}'))
        self.assertEqual(self.sources()[0]['skip'], 'pending secret request')

    def test_usage_holds_match_current_selection_not_every_category(self):
        self.assertEqual(scan.category({'model': 'claude-opus-5-5'}), 'non-fable')
        self.assertEqual(scan.category({'model': 'claude-fable-5-2'}), 'fable')
        self.assertIsNone(self.sources(holds={'fable'})[0]['skip'])
        self.assertEqual(self.sources(holds={'codex'})[0]['skip'], 'usage category held')
        self.c.execute('UPDATE orchestration_v2_projection_threads SET payload_json=?',
                       (json.dumps({'modelSelection': {'instanceId': 'claudeAgent', 'model': 'claude-opus-5-5'}}),))
        self.assertIn('selection changed', self.sources()[0]['skip'])

    def test_native_evidence_and_legacy_attempt_never_resend(self):
        self.assertIn('reconcile', self.sources(handled={'run:1'})[0]['skip'])
        self.c.execute('INSERT INTO orchestration_command_receipts VALUES(?)', ('command:restart-continuation:run:1',))
        self.assertIn('receipt', self.sources()[0]['skip'])
        self.c.execute('DELETE FROM orchestration_command_receipts')
        self.c.execute('INSERT INTO orchestration_v2_projection_messages VALUES(?,?)', ('thread', 'message:restart-continuation:run:1'))
        self.assertIn('message', self.sources()[0]['skip'])

    def test_unknown_guard_file_fails_closed_and_expired_latch_is_not_self_released(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            guard = home / 'hard-stops.json'
            with self.assertRaises(FileNotFoundError):
                scan.usage_holds(guard, home)
            guard.write_text('{"latches":{"pool":{"category":"codex","end":1}}}')
            self.assertEqual(scan.usage_holds(guard, home, now=100), {'codex'})

    def test_untracked_usage_does_not_check_model_category(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(scan.usage_holds('', Path(temp)))
            (Path(temp) / 'category-holds.json').write_text('{"codex": 50}')
            self.assertEqual(scan.usage_holds('', Path(temp), now=10), {'codex'})
            self.assertEqual(scan.usage_holds('', Path(temp), now=99), set())
        self.c.execute('UPDATE orchestration_v2_projection_threads SET payload_json=?',
                       (json.dumps({'modelSelection': {'model': 'other-model'}}),))
        self.c.execute('UPDATE orchestration_v2_projection_runs SET payload_json=?', (json.dumps(
            dict(startedAt='2025-01-02T12:00:00Z', modelSelection={'model': 'other-model'})),))
        self.assertIsNone(self.sources()[0]['skip'])
        self.assertEqual(self.sources(holds=set())[0]['skip'], 'unknown usage category')

    def test_retired_file_parsing(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'retired.txt'
            path.write_text('# old roles\nthread-a\n  thread-b  # note\n\n')
            self.assertEqual(scan.retired_ids(str(path)), {'thread-a', 'thread-b'})
        self.assertEqual(scan.retired_ids(''), set())


class Command(unittest.TestCase):
    def test_cli_on_fixture_file_is_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'state.sqlite'
            c = sqlite3.connect(db)
            c.executescript(SCHEMA)
            for tid, status in (('stopped', 'failed'), ('done', 'completed')):
                c.execute('INSERT INTO orchestration_v2_projection_threads VALUES(?,?)', (tid, json.dumps({'modelSelection': MODEL})))
                c.execute('INSERT INTO orchestration_v2_projection_runs VALUES(?,?,?,?,?,?)', (tid, 'run-' + tid, status, 'x',
                          json.dumps({'startedAt': 'x', 'modelSelection': MODEL}), 1))
            c.execute("INSERT INTO orchestration_v2_projection_runtime_requests VALUES('other','pending')")
            c.commit()
            c.close()
            before = hashlib.sha256(db.read_bytes()).hexdigest()
            env = {**os.environ, 'T3_RECOVERY_CONFIG': str(Path(temp) / 'none.json'), 'HOME': temp}
            out = subprocess.run([sys.executable, str(Path(__file__).with_name('scan_stopped.py')), '--db', str(db), '--eligible'],
                                 capture_output=True, text=True, env=env, timeout=30, check=True).stdout
            rows = [json.loads(line) for line in out.splitlines()]
            self.assertEqual([(r['thread_id'], r['run_id'], r['skip']) for r in rows], [('stopped', 'run-stopped', None)])
            self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), before)
            self.assertFalse(any(p.name.endswith(('-wal', '-journal')) for p in Path(temp).iterdir()))


if __name__ == '__main__':
    unittest.main()
