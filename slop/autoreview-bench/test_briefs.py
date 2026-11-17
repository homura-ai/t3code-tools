"""Synthetic SQLite only: never reads T3's real state."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

loader = importlib.machinery.SourceFileLoader('briefs', str(Path(__file__).with_name('autoreview-briefs')))
spec = importlib.util.spec_from_loader(loader.name, loader)
briefs = importlib.util.module_from_spec(spec)
loader.exec_module(briefs)

ROOT = 'root'
STD = 'You are an autoreviewer for the T3 thread "X" (t). Activity...\n\n'


def make_db(path):
    c = sqlite3.connect(path)
    c.execute('CREATE TABLE orchestration_v2_projection_threads (thread_id, title, created_at, payload_json)')
    c.execute('CREATE TABLE orchestration_v2_projection_messages (thread_id, role, created_at, payload_json)')
    return c


def add_thread(c, tid, title, parent, created, model='m', effort='low', autoreviewers=None):
    payload = dict(lineage=dict(parentThreadId=parent), modelSelection=dict(
        model=model, options=[dict(id='effort', value=effort)]), autoreviewers=autoreviewers or [])
    c.execute('INSERT INTO orchestration_v2_projection_threads VALUES (?,?,?,?)', (tid, title, created, json.dumps(payload)))


def add_msg(c, tid, text):
    c.execute('INSERT INTO orchestration_v2_projection_messages VALUES (?,?,?,?)', (tid, 'user', '1', json.dumps(dict(text=text))))


class Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.c = make_db(self.dir / 's.sqlite')

    def build(self):
        c = self.c
        add_thread(c, ROOT, 'Main', None, '0', autoreviewers=[
            dict(threadId='x-autoreview-add-a', brief='live brief A', minBatchChars=2500, preamble='MY PREAMBLE')])
        add_thread(c, 'x-autoreview-add-a', 'Alpha: a', ROOT, '1', 'ma', 'high',
                   autoreviewers=[dict(threadId='x-autoreview-add-m', brief='meta brief')])
        add_msg(c, 'x-autoreview-add-a', STD + 'Review brief: old A' + briefs.ACTIVITY_MARK + ' SECRET HISTORY')
        add_thread(c, 'x-autoreview-add-m', 'Meta: m', 'x-autoreview-add-a', '2', 'mm', 'low')
        add_msg(c, 'x-autoreview-add-m', STD + 'Review brief: meta brief' + briefs.ACTIVITY_MARK + ' SECRET HISTORY')
        add_thread(c, 'x-autoreview-add-old', 'Old: removed', ROOT, '3', 'mo', 'low')
        add_msg(c, 'x-autoreview-add-old', STD + 'Review brief: removed brief' + briefs.ACTIVITY_MARK + ' SECRET HISTORY')
        add_thread(c, 'x-autoreview-add-old2', 'Old: removed', ROOT, '4', 'mo', 'low')
        add_msg(c, 'x-autoreview-add-old2', STD + 'Review brief: newer removed brief' + briefs.ACTIVITY_MARK)
        c.commit()

    def test_generate(self):
        self.build()
        out = self.dir / 'out.md'
        out.write_text('## Old: removed (removed)\n\nNote (Main, 1/9): keep me\n')
        entries = briefs.generate(self.c, ROOT, out, '2025-01-09 17:00 EST')
        text = out.read_text()
        self.assertEqual([(e['title'], e['attached']) for e in entries],
                         [('Alpha: a', True), ('Meta: m', True), ('Old: removed', False)])
        self.assertIn('## Alpha: a (attached)', text)
        self.assertIn('## Meta: m (attached to Alpha)', text)
        self.assertIn('minBatchChars: 2500', text)
        self.assertIn('MY PREAMBLE', text)
        self.assertIn('live brief A', text)
        self.assertIn('newer removed brief', text)
        self.assertNotIn('\nremoved brief', text)
        self.assertIn('Note (Main, 1/9): keep me', text)
        self.assertNotIn('SECRET', text)
        self.assertNotIn('Activity so far', text)
        self.assertIn('regenerated 2025-01-09 17:00 EST', text)

    def test_removed_when_parent_not_attached(self):
        self.build()
        c = self.c
        c.execute("UPDATE orchestration_v2_projection_threads SET payload_json=? WHERE thread_id=?",
                  (json.dumps(dict(autoreviewers=[])), ROOT))
        c.commit()
        entries = briefs.collect(c, ROOT)
        self.assertFalse(any(e['attached'] for e in entries))

    def test_bad_layout_fails_safely(self):
        c = sqlite3.connect(':memory:')
        c.execute('CREATE TABLE orchestration_v2_projection_threads (thread_id)')
        c.execute('CREATE TABLE orchestration_v2_projection_messages (thread_id)')
        with self.assertRaises(ValueError):
            briefs.generate(c, ROOT, self.dir / 'o.md', 'now')

    def test_missing_thread(self):
        with self.assertRaises(ValueError):
            briefs.generate(self.c, 'nope', self.dir / 'o.md', 'now')


if __name__ == '__main__':
    unittest.main()
