"""Smoke checks: run both commands as a user would, on a throwaway SQLite file.

Never touches T3's real state: every run gets --db or T3_STATE_DB pointing at a
temporary fixture, and the fixture's bytes are checked to be unchanged.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
PARENT = 'parent-thread'
REV = 'x-autoreview-add-rev'
META = 'x-autoreview-add-meta'


def build(path):
    c = sqlite3.connect(path)
    c.execute('CREATE TABLE orchestration_v2_projection_threads (thread_id, title, default_provider, created_at, payload_json)')
    c.execute('CREATE TABLE orchestration_v2_projection_messages (thread_id, message_id, run_id, role, streaming, created_at, updated_at, payload_json)')
    c.execute('CREATE TABLE orchestration_v2_projection_runs (thread_id, run_id, status, requested_at, ordinal, payload_json)')
    c.execute('CREATE TABLE orchestration_v2_projection_provider_turns (thread_id, provider_turn_id, node_id, provider_thread_id, payload_json)')
    c.execute('CREATE TABLE orchestration_v2_projection_nodes (node_id, run_id)')
    c.execute('CREATE TABLE orchestration_v2_projection_provider_threads (provider_thread_id, provider)')
    c.execute('INSERT INTO orchestration_v2_projection_threads VALUES (?,?,?,?,?)', (PARENT, 'Builder', 'codex', '0', json.dumps(
        dict(autoreviewers=[dict(threadId=REV, brief='Check claims.'), dict(threadId=META, brief='Judge findings.')]))))
    for n, (tid, title, provider) in enumerate(((REV, 'Facts: claims (Opus high)', 'claudeAgent'),
                                                (META, 'Meta-review A', 'codex')), 1):
        c.execute('INSERT INTO orchestration_v2_projection_threads VALUES (?,?,?,?,?)', (tid, title, provider, str(n), json.dumps(
            dict(lineage=dict(parentThreadId=PARENT),
                 modelSelection=dict(model='model-' + tid[-3:], options=[dict(id='effort', value='high')])))))
        c.execute('INSERT INTO orchestration_v2_projection_provider_threads VALUES (?,?)', ('p-' + tid, provider))
        batch = 'message:autoreview-batch:' + tid
        c.execute('INSERT INTO orchestration_v2_projection_messages VALUES (?,?,?,?,?,?,?,?)',
                  (tid, batch, 'r-' + tid, 'user', 0, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z',
                   json.dumps(dict(text='You are an autoreviewer for the T3 thread "Builder".\n\nReview brief: Check it.'))))
        answer = ('1. a claim is wrong\n2. another' if tid == REV
                  else '| Facts | item | HOLDS | ok |\n| Facts | item 2 | NIT | minor |')
        c.execute('INSERT INTO orchestration_v2_projection_messages VALUES (?,?,?,?,?,?,?,?)',
                  (tid, 'a-' + tid, 'r-' + tid, 'assistant', 0, '2026-01-01T00:00:05Z', '2026-01-01T00:00:30Z',
                   json.dumps(dict(text=answer))))
        c.execute('INSERT INTO orchestration_v2_projection_runs VALUES (?,?,?,?,?,?)',
                  (tid, 'r-' + tid, 'completed', '2026-01-01T00:00:00Z', 1, json.dumps(dict(userMessageId=batch))))
        c.execute('INSERT INTO orchestration_v2_projection_nodes VALUES (?,?)', ('n-' + tid, 'r-' + tid))
        c.execute('INSERT INTO orchestration_v2_projection_provider_turns VALUES (?,?,?,?,?)',
                  (tid, 't-' + tid, 'n-' + tid, 'p-' + tid,
                   json.dumps(dict(turnTokenUsage=dict(usageStatus='complete', inputTokens=1000, outputTokens=50)))))
    c.execute('INSERT INTO orchestration_v2_projection_messages VALUES (?,?,?,?,?,?,?,?)',
              (PARENT, 'post', None, 'user', 0, '1', '1', json.dumps(dict(
                  text='Autoreview from "Facts: claims (Opus high)" (thread x)\n1. SECRET FINDING\n2. two', senderThreadId=REV))))
    c.commit()
    c.close()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(name, *args, env=None):
    return subprocess.run([sys.executable, str(HERE / name), *args], capture_output=True, text=True,
                          env={**os.environ, 'HOME': '/nonexistent', **(env or {})}, timeout=30)


class Smoke(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / 'state.sqlite'
        build(self.db)
        self.before = digest(self.db)

    def tearDown(self):
        self.assertEqual(digest(self.db), self.before, 'fixture database changed')

    def test_bench_with_db_flag(self):
        r = run('autoreview-bench', '--thread', PARENT, '--db', str(self.db))
        self.assertEqual(r.returncode, 0, r.stderr)
        row = next(line for line in r.stdout.splitlines() if line.startswith('| Facts'))
        self.assertIn('| 2 / 1 |', row)          # two numbered findings in one post
        self.assertIn('| 30.00 |', row)          # batch created 00:00:00, answer updated 00:00:30
        self.assertIn('1/0/0/0/0/1', row)        # HOLDS and NIT from the meta-reviewer
        self.assertNotIn('SECRET', r.stdout)

    def test_bench_env_var_and_meta_prefix_off(self):
        r = run('autoreview-bench', '--thread', PARENT, '--meta-prefix', '', env={'T3_STATE_DB': str(self.db)})
        self.assertEqual(r.returncode, 0, r.stderr)
        row = next(line for line in r.stdout.splitlines() if line.startswith('| Facts'))
        self.assertIn('0/0/0/0/0/0', row)

    def test_bench_missing_db_fails_quietly(self):
        r = run('autoreview-bench', '--thread', PARENT, '--db', str(self.dir / 'none.sqlite'))
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.dir / 'none.sqlite').exists())
        self.assertIn('unavailable', r.stderr)

    def test_briefs_writes_only_the_out_file(self):
        out = self.dir / 'briefs.md'
        r = run('autoreview-briefs', '--thread', PARENT, '--db', str(self.db), '--out', str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        text = out.read_text()
        self.assertIn('# Autoreviewer briefs for Builder', text)
        self.assertIn('Check claims.', text)
        self.assertIn('## Meta-review A (attached)', text)
        self.assertNotIn('SECRET', text)

    def test_briefs_requires_thread(self):
        r = run('autoreview-briefs', '--db', str(self.db), '--out', str(self.dir / 'x.md'))
        self.assertEqual(r.returncode, 2)


if __name__ == '__main__':
    unittest.main()
