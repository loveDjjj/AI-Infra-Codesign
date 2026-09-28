"""验证混合组合必须通过字节再生，才能进入正式验收。"""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock
from codesign_lab.search.pipeline import Pipeline,select_jobs
from codesign_lab.build import build
from codesign_lab.config import ROOT


class PairVerifyChecks(unittest.TestCase):
    def test_failed_verification_never_releases_full_grade(self):
        controller=Pipeline.__new__(Pipeline);controller.done=[];controller.enqueue=Mock()
        controller.completed({'stage':'verify','followup':{}},{'status':'failed'})
        controller.enqueue.assert_not_called()

    def test_successful_verification_releases_exact_followup(self):
        controller=Pipeline.__new__(Pipeline);controller.done=[];controller.enqueue=Mock()
        followup={'stage':'full','candidate':'/tmp/candidate','report':'/tmp/grade.json','key':'full-1'}
        controller.completed({'stage':'verify','followup':followup},{'status':'completed'})
        job=controller.enqueue.call_args[0][0]
        self.assertEqual(job['key'],'full-1')
        self.assertEqual(job['candidate'],Path('/tmp/candidate'))

    def test_verify_can_use_reserved_slot_when_exploration_is_full(self):
        active=[{'stage':'case','memory_bytes':1} for _ in range(3)]
        pending=[{'key':'verify','stage':'verify','memory_bytes':1}]
        self.assertEqual(select_jobs(pending,active,0,4,1,10,10),pending)

    def test_byte_mismatch_is_detected_by_real_generator(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            build(ROOT/'configs/best.yaml',root/'original')
            asm=root/'original/programs/M1_P1.asm';asm.write_text(asm.read_text()+'\n')
            with self.assertRaisesRegex(ValueError,'Reproduction mismatch'):
                build(ROOT/'configs/best.yaml',root/'rebuild',root/'original')
