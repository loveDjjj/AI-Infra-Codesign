"""验证混合组合必须通过字节再生，才能进入正式验收。"""
import tempfile
import json
import shutil
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from codesign_lab.search.pipeline import Pipeline,select_jobs
from codesign_lab.search.pair_verify import verify_pair,generator_identity,source_identity
from codesign_lab.search.composite import snapshot,reproduce
from codesign_lab.release import audit
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
                              'generator_sha256': generator_identity(ROOT),
                              'source_sha256': source_identity(ROOT)}
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
                          'generator_sha256': generator_identity(ROOT),
                          'source_sha256': source_identity(ROOT)},
                'M2_D1': {'candidate': str(parent), 'source_root': str(second),
                          'generator_sha256': generator_identity(second),
                          'source_sha256': source_identity(second)}}
            (candidate / 'selection.json').write_text(json.dumps({'parents': parents}))
            def fake_build(source, config, output, expected):
                shutil.copytree(parent, output)
            with patch('codesign_lab.search.pair_verify.build_at', side_effect=fake_build) as builds:
                result = verify_pair(candidate, root / 'verified')
            self.assertEqual(result['mode'], 'mixed_sources')
            self.assertEqual(builds.call_count, 2)
            code.write_text('x=2\n')
            with self.assertRaisesRegex(ValueError, '生成器身份'):
                verify_pair(candidate, root / 'verified')

    def test_composite_snapshot_rebuilds_real_programs(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'workspace') as directory:
            root = Path(directory)
            candidate = root / 'finalist'
            parent = ROOT / 'data/releases/joint28'
            shutil.copytree(parent, candidate)
            other = root / 'other-source'
            shutil.copytree(ROOT / 'src', other / 'src')
            parents = {case: {'candidate': str(parent),
                              'source_root': str(ROOT if case == 'M1_P1' else other),
                              'generator_sha256': generator_identity(ROOT if case == 'M1_P1' else other),
                              'source_sha256': source_identity(ROOT if case == 'M1_P1' else other)}
                       for case in ('M1_P1', 'M2_D1')}
            (candidate / 'selection.json').write_text(json.dumps({'parents': parents}))
            snapshot(candidate, root / 'snapshot')
            result = reproduce(root / 'snapshot', candidate, root / 'scratch')
            self.assertTrue(result['verified'])
            archive = root / 'snapshot/M2_D1-source.tar.gz'
            archive.write_bytes(archive.read_bytes() + b'changed')
            with self.assertRaisesRegex(ValueError, '快照文件已改变'):
                reproduce(root / 'snapshot', candidate, root / 'another-scratch')

    def test_composite_official_audit_uses_both_source_snapshots(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'workspace') as directory:
            root = Path(directory)
            parent = ROOT / 'data/releases/joint28'
            candidate = root / 'finalist'
            shutil.copytree(parent, candidate)
            other = root / 'other-source'
            shutil.copytree(ROOT / 'src', other / 'src')
            (other / 'vendor').mkdir()
            (other / 'vendor/official').symlink_to(ROOT / 'vendor/official', target_is_directory=True)
            (other / 'configs').mkdir()
            shutil.copy2(ROOT / 'configs/toolchain.yaml', other / 'configs/toolchain.yaml')
            parents = {case: {'candidate': str(parent), 'source_root': str(source),
                              'generator_sha256': generator_identity(source),
                              'source_sha256': source_identity(source)}
                       for case, source in [('M1_P1', ROOT), ('M2_D1', other)]}
            (candidate / 'selection.json').write_text(json.dumps({'parents': parents}))
            report = root / 'grade.json'
            shutil.copy2(parent / 'local-grade.json', report)
            shutil.copy2(parent / 'local-grade.isolated-run.json', report.with_suffix('.isolated-run.json'))
            record = {'id': 'test-composite', 'scope': 'full', 'eligible': True,
                      'report': str(report), 'candidate': str(candidate),
                      'config': json.loads((parent / 'config.json').read_text())}
            with patch('codesign_lab.release.read', return_value=[record]), \
                 patch('codesign_lab.records.update', side_effect=lambda _id, fields: record | fields):
                result = audit('test-composite')
            self.assertEqual(result['reproduction'], 'verified_composite')
            self.assertTrue(json.loads(report.with_suffix('.audit.json').read_text())['composite'])

    def test_mixed_pair_releases_full_grade_after_byte_verification(self):
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
                                  'followup': {'stage': 'full', 'candidate': str(root / 'candidate'),
                                               'report': str(root / 'grade.json')}}, {'status': 'completed'})
            controller.enqueue.assert_called_once()
            self.assertEqual(controller.pool.state['mixed_pairs']['pair']['status'],
                             'VERIFIED_QUEUED_FOR_FULL_GRADE')

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
