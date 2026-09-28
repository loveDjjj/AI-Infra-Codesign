"""验证长期启动器的主控锁检查与模型覆盖，不调用模型服务。"""
import fcntl
import importlib.util
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch
from codesign_lab.config import ROOT

spec = importlib.util.spec_from_file_location('long_launcher', ROOT / 'scripts/start-long-optimization.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LongLauncherChecks(unittest.TestCase):
    def test_real_controller_lock_blocks_waiter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'controller.lock'
            self.assertFalse(launcher.busy(path))
            with path.open('a') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(launcher.busy(path))
            self.assertFalse(launcher.busy(path))

    def test_resume_keeps_session_and_overrides_model(self):
        with patch('sys.argv', ['codex', 'exec', '--sandbox', 'read-only', 'resume', 'same-session', '--json']), patch('os.execv') as call:
            runpy.run_path(str(ROOT / 'scripts/codex-analyst/codex'))
        argv = call.call_args.args[1]
        self.assertEqual(argv[1:6], ['exec', '-m', 'gpt-6-sol', '-c', 'model_reasoning_effort="medium"'])
        self.assertEqual(argv[argv.index('resume') + 1], 'same-session')

    def test_capability_probe_does_not_set_model(self):
        with patch('sys.argv', ['codex', '--version']), patch('os.execv') as call:
            runpy.run_path(str(ROOT / 'scripts/codex-analyst/codex'))
        self.assertEqual(call.call_args.args[1][1:], ['--version'])
