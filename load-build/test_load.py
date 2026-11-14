"""Fakes only: never stops or starts T3, never touches the real watchdog or state."""
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.machinery
import importlib.util
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader('tlb', str(Path(__file__).with_name('t3-load-build')))
spec = importlib.util.spec_from_loader(loader.name, loader)
tlb = importlib.util.module_from_spec(spec)
loader.exec_module(tlb)

NOW = datetime(2025, 1, 9, 23, 0, tzinfo=timezone.utc)


class Fake:
    def __init__(self, running=(True,), send=(True,)):
        self.running, self.sends, self.calls, self.texts = list(running), list(send), [], []

    def now(self):
        return NOW

    def sleep(self, s):
        self.calls.append(('sleep', s))

    def stop_t3(self, sleep):
        self.calls.append('stop')

    def start_t3(self):
        self.calls.append('start')

    def t3_running(self):
        return self.running.pop(0) if self.running else True

    def send(self, text):
        self.texts.append(text)
        return self.sends.pop(0) if self.sends else True


class Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        tlb.STATE = self.dir / 'state'
        tlb.LOG = tlb.STATE / 'load.log'
        tlb.GUARDS = (self.dir / 'quiet-until', tlb.STATE / 'no-restart-until')
        tlb.PAUSE_FILE = self.dir / 'pause-until'
        self.staged = self.dir / 'T3-Code-0.1.7-x-1234-x86_64.AppImage.staged'
        self.staged.write_bytes(b'build bytes')
        self.sha = hashlib.sha256(b'build bytes').hexdigest()
        self.wake = self.dir / 'wake.txt'
        self.wake.write_text('loaded the new build')

    def run_load(self, deps):
        return tlb.load(self.staged, self.sha, self.wake, deps=deps, settle=0)

    def test_success(self):
        deps = Fake()
        self.assertEqual(self.run_load(deps), 0)
        self.assertTrue((self.dir / 'T3-Code-0.1.7-x-1234-x86_64.AppImage').exists())
        self.assertFalse(self.staged.exists())
        self.assertEqual(deps.texts, ['loaded the new build'])
        self.assertEqual([c for c in deps.calls if isinstance(c, str)], ['stop', 'start'])
        self.assertIn('2025-01-09T23:08:00Z', tlb.PAUSE_FILE.read_text())

    def test_guard_files_refuse(self):
        for guard in (self.dir / 'quiet-until', tlb.STATE / 'no-restart-until'):
            guard.parent.mkdir(parents=True, exist_ok=True)
            guard.write_text(tlb.iso(NOW + timedelta(minutes=5)))
            deps = Fake()
            self.assertEqual(self.run_load(deps), 3)
            self.assertEqual(deps.calls, [])
            self.assertTrue(self.staged.exists())
            guard.write_text(tlb.iso(NOW - timedelta(minutes=5)))
            self.assertIsNone(tlb.active_guard(NOW))

    def test_sha_mismatch_touches_nothing(self):
        deps = Fake()
        self.sha = '0' * 64
        self.assertEqual(self.run_load(deps), 1)
        self.assertEqual(deps.calls, [])
        self.assertTrue(self.staged.exists())
        self.assertFalse(tlb.PAUSE_FILE.exists())

    def test_rollback_when_t3_not_running(self):
        deps = Fake(running=(False, True))
        self.assertEqual(self.run_load(deps), 2)
        live = self.dir / 'T3-Code-0.1.7-x-1234-x86_64.AppImage'
        self.assertTrue(Path(str(live) + '.off').exists())
        self.assertFalse(live.exists())
        self.assertIn('rolled back', deps.texts[-1])
        self.assertEqual([c for c in deps.calls if isinstance(c, str)], ['stop', 'start', 'stop', 'start'])

    def test_rollback_when_message_cannot_be_sent(self):
        deps = Fake(running=(True, True), send=(False, False, True))
        self.assertEqual(self.run_load(deps), 2)
        self.assertEqual(len(deps.texts), 3)

    def test_retry_once_then_success(self):
        deps = Fake(send=(False, True))
        self.assertEqual(self.run_load(deps), 0)
        self.assertEqual(len(deps.texts), 2)

    def test_live_name(self):
        n = lambda s: tlb.live_name(Path(s)).name
        self.assertEqual(n('T3-Code-0.1.7-autoreview2-f4e3a1096e-next.AppImage.staged'), 'T3-Code-0.1.7-autoreview2-f4e3a1096e-x86_64.AppImage')
        self.assertEqual(n('T3-Code-0.1.7-devnames-a6a3c54459-x86_64.AppImage.staged'), 'T3-Code-0.1.7-devnames-a6a3c54459-x86_64.AppImage')
        with self.assertRaises(ValueError):
            tlb.live_name(Path('x.AppImage'))

    def test_missing_wake_file_touches_nothing(self):
        deps = Fake()
        self.wake.unlink()
        self.assertEqual(self.run_load(deps), 1)
        self.assertEqual(deps.calls, [])

    def test_detach_passes_settings_to_systemd_run(self):
        # systemd-run uses the user manager's environment, so the settings must be passed on.
        env = {'T3_LOAD_GUARDS': '/q/quiet-until', 'T3_LOAD_PAUSE_FILE': '/q/pause', 'T3_LOAD_WAKE_THREAD': 'thread-1',
               'UNRELATED': 'x'}
        argv = ['t3-load-build', str(self.staged), self.sha, str(self.wake), '--detach']
        with patch.dict(os.environ, env), patch.object(sys, 'argv', argv), \
                patch.object(tlb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(tlb.main(), 0)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:2], ['systemd-run', '--user'])
        for k in ('T3_LOAD_GUARDS', 'T3_LOAD_PAUSE_FILE', 'T3_LOAD_WAKE_THREAD'):
            self.assertIn(f'--setenv={k}={env[k]}', cmd)
        self.assertNotIn('--setenv=UNRELATED=x', cmd)
        self.assertNotIn('--detach', cmd)


if __name__ == '__main__':
    unittest.main()
