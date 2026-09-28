"""结构研究记录的资源诊断必须用原冻结源码逐字节再生。"""
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from codesign_lab.config import ROOT
from codesign_lab.search.pipeline import Pipeline, key


class ProfileSourceChecks(unittest.TestCase):
    def test_structure_profile_build_uses_pinned_generator(self):
        (ROOT / 'workspace/families').mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / 'workspace/families') as directory:
            source = Path(directory)
            code = source / 'src/code.py'
            code.parent.mkdir()
            code.write_text('value = 1\n')
            (source / 'vendor').mkdir()
            (source / 'vendor/official').symlink_to(
                ROOT / 'vendor/official', target_is_directory=True)
            identity = {'src/code.py': hashlib.sha256(code.read_bytes()).hexdigest()}
            record = {'id': 'power-failed', 'source_root': str(source),
                      'source_sha256': key(identity), 'config': {'hardware': {}, 'programs': {}}}
            with tempfile.TemporaryDirectory(dir=ROOT / 'workspace') as output:
                root = Path(output)
                controller = Pipeline.__new__(Pipeline)
                controller.out = root / 'campaign'
                controller.out.mkdir()
                controller.python = '/test/python'
                controller.family_sources = {}
                controller.pool = SimpleNamespace(source_epoch='epoch', state={
                    'applied_decisions': {'decision': {'lane': 'global', 'decision': {
                        'profile_requests': [{'record_id': record['id'], 'case': 'M1_P1',
                                              'reason': '定位功耗窗口'}]}}}})
                controller.enqueue = Mock()
                original, report = root / 'original', root / 'estimate.json'
                with patch('codesign_lab.search.pipeline.read', return_value=[record]), \
                     patch('codesign_lab.search.profiles.inputs', return_value=(original, report)):
                    controller.process_profile_requests()
                jobs = [call.args[0] for call in controller.enqueue.call_args_list]
                self.assertEqual(len(jobs), 1)
                self.assertEqual(jobs[0]['stage'], 'profile_build')
                self.assertIn('codesign_lab.search.family_build', jobs[0]['command'])
                self.assertEqual(jobs[0]['command'][-2:], ['--verify', str(original)])
                self.assertEqual(controller.family_sources[str(source)], identity)
