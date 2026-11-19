"""Synthetic SQLite only: never reads T3's real state."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sqlite3
import unittest
import urllib.parse

loader = importlib.machinery.SourceFileLoader('bench', str(Path(__file__).with_name('autoreview-bench')))
spec = importlib.util.spec_from_loader(loader.name, loader)
bench = importlib.util.module_from_spec(spec)
loader.exec_module(bench)

MAIN = 'main'
FACTS = 'x-autoreview-add-facts'
INTENT = 'x-autoreview-add-intent'


def make_db():
    c = sqlite3.connect(':memory:')
    c.execute('CREATE TABLE orchestration_v2_projection_threads (thread_id, title, default_provider, created_at, payload_json)')
    c.execute('CREATE TABLE orchestration_v2_projection_messages (thread_id, message_id, run_id, role, streaming, created_at, updated_at, payload_json)')
    c.execute('CREATE TABLE orchestration_v2_projection_runs (thread_id, run_id, status, requested_at, ordinal, payload_json)')
    c.execute('CREATE TABLE orchestration_v2_projection_provider_turns (thread_id, provider_turn_id, node_id, provider_thread_id, payload_json)')
    c.execute('CREATE TABLE orchestration_v2_projection_nodes (node_id, run_id)')
    c.execute('CREATE TABLE orchestration_v2_projection_provider_threads (provider_thread_id, provider)')
    c.execute('INSERT INTO orchestration_v2_projection_threads VALUES (?,?,?,?,?)', (MAIN, 'Main', 'claudeAgent', '0', json.dumps(
        dict(autoreviewers=[dict(threadId=FACTS), dict(threadId=INTENT)]))))
    for n, (tid, title) in enumerate(((FACTS, 'Facts Reviewer: claims (Opus high)'), (INTENT, "Intent Reviewer: the user's intent (Fable high)")), 1):
        c.execute('INSERT INTO orchestration_v2_projection_threads VALUES (?,?,?,?,?)', (tid, title, 'claudeAgent', str(n), json.dumps(
            dict(lineage=dict(parentThreadId=MAIN), modelSelection=dict(model='m', options=[])))))
    c.commit()
    return c


def post(c, mid, text, sender=None):
    c.execute('INSERT INTO orchestration_v2_projection_messages VALUES (?,?,?,?,?,?,?,?)',
              (MAIN, mid, None, 'user', 0, '1', '1', json.dumps(dict(text=text, senderThreadId=sender))))
    c.commit()


def counts(c):
    data = bench.read_benchmark(c, MAIN)
    return {r['id']: (r['posts'], r['findings']) for r in data['reviewers']}, data['unknown_posts']


class Tests(unittest.TestCase):
    def test_old_format_by_title_renamed_reviewer_via_sender(self):
        c = make_db()
        post(c, 'm1', 'Autoreview from "Review 1: claims and changes (Opus high)" (thread t)\n1. a\n2. b', sender=FACTS)
        by, unknown = counts(c)
        self.assertEqual(by[FACTS], (1, 2))
        self.assertEqual(unknown, 0)

    def test_combined_two_blocks_attributed_by_thread_id_not_title(self):
        c = make_db()
        text = ('Autoreview findings from reviewers that review your work as you go. Weigh each point.\n\n'
                f'--- Autoreview from "Old title of the facts reviewer" (thread {FACTS}) ---\n1. one\n2. two\n\n'
                f'--- Autoreview from "Intent Reviewer: the user\'s intent (Fable high)" (thread {INTENT}) ---\n- only finding\n')
        post(c, 'm1', text, sender=FACTS)
        by, unknown = counts(c)
        self.assertEqual(by[FACTS], (1, 2))
        self.assertEqual(by[INTENT], (1, 1))
        self.assertEqual(unknown, 0)

    def test_combined_falls_back_to_title_and_lgtm_blocks_skipped(self):
        c = make_db()
        text = ('Autoreview findings from reviewers that review your work as you go.\n\n'
                '--- Autoreview from "Facts Reviewer: claims (Opus high)" (thread gone) ---\n1. one\n\n'
                f'--- Autoreview from "x" (thread {INTENT}) ---\nLGTM\n')
        post(c, 'm1', text)
        by, unknown = counts(c)
        self.assertEqual(by[FACTS], (1, 1))
        self.assertEqual(by[INTENT], (0, 0))

    def test_delegated_task_findings_attributed_by_node_id(self):
        c = make_db()
        node = 'node:delegated-task:server:autoreview-findings:run:thread:' + FACTS + ':ordinal:3'
        enc = urllib.parse.quote(urllib.parse.quote(FACTS, safe=''), safe='')
        text = (f'Delegated task node:delegated-task:server%3Aautoreview-findings%3Arun%3Athread%3A{enc}%3Aordinal%3A3 '
                '(Renamed reviewer) [completed]:\n1. one\n2. two\n3. three')
        post(c, 'm1', text)
        post(c, 'm2', 'Delegated task node:delegated-task:command%3Aother (Some task) [completed]:\nnot findings')
        by, unknown = counts(c)
        self.assertEqual(by[FACTS], (1, 3))
        self.assertEqual(unknown, 0)

    def test_short_name_post_attributed_by_sender(self):
        c = make_db()
        post(c, 'm1', 'Facts Reviewer:\n1. one\n2. two', sender=FACTS)
        post(c, 'm2', 'Intent Reviewer:\nLGTM', sender=INTENT)
        post(c, 'm3', 'Someone else:\n1. not a reviewer post', sender='x-autoreview-add-gone')
        by, unknown = counts(c)
        self.assertEqual(by[FACTS], (1, 2))
        self.assertEqual(by[INTENT], (0, 0))
        self.assertEqual(unknown, 1)

    def test_split_posts_no_text_leak_in_render(self):
        c = make_db()
        post(c, 'm1', f'Autoreview findings from reviewers that review your work as you go.\n\n--- Autoreview from "a" (thread {FACTS}) ---\nPRIVATE FINDING TEXT', sender=FACTS)
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bench.render(bench.read_benchmark(c, MAIN))
        self.assertNotIn('PRIVATE', out.getvalue())


if __name__ == '__main__':
    unittest.main()
