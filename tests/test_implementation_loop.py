"""验证结构实验的隔离边界、提案去重和候选门槛。"""
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codesign_lab.search import implementation as module


class ImplementationChecks(unittest.TestCase):
    def test_next_epoch_inherits_controller_but_preserves_generator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current, origin, candidate = (root / name for name in ('current', 'origin', 'candidate'))
            controller = module.CONTROLLER_FILES[0]
            generator = 'src/codesign_lab/codegen/d1/compiler.py'
            for base in (current, origin, candidate):
                for name in (controller, generator):
                    (base / name).parent.mkdir(parents=True, exist_ok=True)
            (current / controller).write_text('old controller')
            (origin / controller).write_text('fixed controller')
            (candidate / controller).write_text('old controller')
            (candidate / generator).write_text('new epoch generator')
            for name in module.CONTROLLER_FILES[1:]:
                (origin / name).parent.mkdir(parents=True, exist_ok=True)
                (candidate / name).parent.mkdir(parents=True, exist_ok=True)
                (origin / name).write_text('fixed bridge')
                (candidate / name).write_text('old bridge')
            with patch.object(module, 'ROOT', current), patch.object(module, 'origin_root', return_value=origin):
                hashes = module.inherit_controller_files(candidate)
            self.assertEqual((candidate / controller).read_text(), 'fixed controller')
            self.assertEqual((candidate / generator).read_text(), 'new epoch generator')
            self.assertEqual(hashes[controller], module.digest(origin / controller))

    def test_known_failed_validation_resumes_without_coding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / 'source'
            config = snapshot / 'workspace/implementation-input/config.json'
            config.parent.mkdir(parents=True)
            config.write_text('{}')
            state_path = root / 'state.json'
            state_path.write_text(json.dumps({'status': 'FAILED', 'snapshot': str(snapshot),
                'error': 'RuntimeError: default-off 失败；见日志'}))
            with patch.object(module, 'validate_diff', return_value=['src/codesign_lab/codegen/d1/compiler.py']):
                state = module.retry_infrastructure_failure(state_path)
            self.assertEqual(state['status'], 'CODED')
            self.assertIn('default-off', state['recovered_error'])

    def test_only_same_epoch_case_proposals_are_collected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / 'workspace/pipeline/current'
            campaign.mkdir(parents=True)
            (campaign / 'state.json').write_text(json.dumps({'source_epoch': 'epoch-a'}))
            (root / 'data').mkdir()
            decision = {'id': 'd' * 64, 'source_epoch': 'epoch-a', 'decision': {
                'implementation_proposals': [
                    {'lane': 'p1_w2', 'proposal': '同步结构', 'evidence_ids': ['r1']},
                    {'lane': 'global', 'proposal': '不确定案例', 'evidence_ids': []}]}}
            other = {'id': 'e' * 64, 'source_epoch': 'epoch-b', 'decision': {
                'implementation_proposals': [
                    {'lane': 'd1_decode', 'proposal': '旧版本', 'evidence_ids': []}]}}
            (root / 'data/decisions.jsonl').write_text(
                json.dumps(decision) + '\n' + json.dumps(other) + '\n')
            with patch.object(module, 'ROOT', root):
                rows = list(module.proposals(campaign))
                repeated = list(module.proposals(campaign))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1]['case'], 'M1_P1')
            self.assertEqual(rows, repeated)

    def test_existing_config_values_cannot_be_silently_changed(self):
        old = {'config': {'tile': 32}, 'schedule': 'operator'}
        self.assertTrue(module.preserved_fields(old, {'config': {'tile': 32, 'new_switch': True},
                                                       'schedule': 'operator'}))
        self.assertFalse(module.preserved_fields(old, {'config': {'tile': 64, 'new_switch': True},
                                                        'schedule': 'operator'}))

    def test_busy_old_campaign_prevents_new_epoch_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / 'workspace/search/.pipeline.lock'
            lock_path.parent.mkdir(parents=True)
            with lock_path.open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                with patch.object(module, 'ROOT', root), \
                     patch.dict('os.environ', {'CODESIGN_PIPELINE_LOCK_PATH': str(lock_path)}):
                    self.assertIsNone(module.launch_next(root, root / 'settings.json'))

    def test_generator_hash_is_checked_against_campaign_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'src/codesign_lab/codegen/kernel.py'
            source.parent.mkdir(parents=True)
            source.write_text('x = 1\n')
            campaign = root / 'workspace/pipeline/current'
            campaign.mkdir(parents=True)
            (campaign / 'identity.json').write_text(json.dumps({'source': {
                str(source.relative_to(root)): module.digest(source)}}))
            with patch.object(module, 'ROOT', root):
                self.assertTrue(module.campaign_generator_unchanged(campaign))
                source.write_text('x = 2\n')
                self.assertFalse(module.campaign_generator_unchanged(campaign))

    def test_audited_epoch_is_exported_once_with_its_own_source_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin, snapshot = root / 'origin', root / 'snapshot'
            (origin / 'data/releases').mkdir(parents=True)
            (origin / 'data/experiments.jsonl').write_text('')
            release = snapshot / 'data/releases/new-score'
            release.mkdir(parents=True)
            (release / 'local-grade.json').write_text('{"experimental_score":50000}')
            (release / 'local-grade.audit.json').write_text('{"audit_passed":true}')
            (snapshot / 'src').mkdir()
            (snapshot / 'src/kernel.py').write_text('new_kernel = True\n')
            (snapshot / 'data/experiments.jsonl').write_text(json.dumps({
                'id': 'new-score', 'scope': 'full', 'score': 50000,
                'eligible': True, 'audited': True}) + '\n')
            parent = {'source_epoch': 'old', 'decision_id': 'decision'}
            with patch.object(module, 'origin_root', return_value=origin):
                first = module.export_epoch_record(snapshot, 'new-score', parent)
                second = module.export_epoch_record(snapshot, 'new-score', parent)
            self.assertEqual(first, second)
            self.assertEqual(first['reproduction'], 'epoch_verified')
            self.assertTrue((origin / first['report']).is_file())
            self.assertEqual(len((origin / 'data/experiments.jsonl').read_text().splitlines()), 1)

    def test_official_record_lookup_ignores_historical_null_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'data').mkdir()
            report = root / 'workspace/reports/official.json'
            report.parent.mkdir(parents=True)
            report.write_text('{}')
            rows = [{'id': 'old', 'scope': 'full', 'eligible': True, 'report': None},
                    {'id': 'new', 'scope': 'full', 'eligible': True,
                     'report': 'workspace/reports/official.json'}]
            (root / 'data/experiments.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
            self.assertEqual(module.grade_record(root, report)['id'], 'new')


if __name__ == '__main__':
    unittest.main()
