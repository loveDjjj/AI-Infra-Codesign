"""验证混合组合必须通过字节再生，才能进入正式验收。"""
import tempfile
import json
import shutil
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from codesign_lab.search.pipeline import Pipeline,select_jobs
from codesign_lab.search.pair_verify import verify_pair,generator_identity
from codesign_lab.build import build
from codesign_lab.config import ROOT


class PairVerifyChecks(unittest.TestCase):
    def test_single_source_pair_rebuilds_real_joint28_bytes(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'workspace') as directory:
            root = Path(directory)
            candidate = root / 'finalist'
            source = ROOT / 'data/releases/joint28'
            shutil.copytree(source, candidate)
            parents = {case: {'candidate': str(source), 'source_root': str(ROOT),
                              'generator_sha256': generator_identity(ROOT)}
                       for case in ('M1_P1', 'M2_D1')}
            (candidate / 'selection.json').write_text(json.dumps({'parents': parents}))
            result = verify_pair(candidate, root / 'verified')
            self.assertTrue(result['verified'])
            self.assertEqual(result['mode'], 'single_source')

    def test_mixed_pair_checks_each_source_and_rejects_changed_generator(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'workspace') as directory:
            root = Path(directory)
            candidate = root / 'finalist'
            parent = ROOT / 'data/releases/joint28'
            shutil.copytree(parent, candidate)
            second = root / 'second-source'
            code = second / 'src/codesign_lab/codegen/other.py'
            code.parent.mkdir(parents=True)
            code.write_text('x=1\n')
            manifest = second / 'vendor/official/isolation-manifest.json'
            manifest.parent.mkdir(parents=True)
            shutil.copy2(ROOT / 'vendor/official/isolation-manifest.json', manifest)
            parents = {
                'M1_P1': {'candidate': str(parent), 'source_root': str(ROOT),
                          'generator_sha256': generator_identity(ROOT)},
                'M2_D1': {'candidate': str(parent), 'source_root': str(second),
                          'generator_sha256': generator_identity(second)}}
            (candidate / 'selection.json').write_text(json.dumps({'parents': parents}))
            def fake_build(source, config, output, expected):
                shutil.copytree(expected, output)
            with patch('codesign_lab.search.pair_verify.build_at', side_effect=fake_build) as builds:
                result = verify_pair(candidate, root / 'verified')
            self.assertEqual(result['mode'], 'mixed_sources')
            self.assertEqual(builds.call_count, 2)
            code.write_text('x=2\n')
            with self.assertRaisesRegex(ValueError, '生成器身份'):
                verify_pair(candidate, root / 'verified')

    def test_mixed_pair_is_held_until_composite_audit_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / 'verified/pair/pair-verify.json'
            evidence.parent.mkdir(parents=True)
            evidence.write_text(json.dumps({'mode': 'mixed_sources'}))
            controller = Pipeline.__new__(Pipeline)
            controller.out = root
            controller.done = []
            controller.enqueue = Mock()
            controller.pool = SimpleNamespace(state={}, save=Mock())
            controller.completed({'stage': 'verify', 'pair_id': 'pair',
                                  'candidate': root / 'candidate',
                                  'followup': {'stage': 'full'}}, {'status': 'completed'})
            controller.enqueue.assert_not_called()
            self.assertEqual(controller.pool.state['mixed_pairs']['pair']['status'],
                             'VERIFIED_PENDING_COMPOSITE_AUDIT')

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
