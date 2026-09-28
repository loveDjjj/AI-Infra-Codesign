"""验证结构实验的隔离边界、提案去重和候选门槛。"""
import fcntl
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import uuid

from codesign_lab.search import implementation as module
from codesign_lab.config import ROOT


class ImplementationChecks(unittest.TestCase):
    def test_release_generator_restore_requires_exact_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / 'family'
            release = root / 'release'
            release.mkdir()
            target = 'src/codesign_lab/codegen/p1/compiler.py'
            content = b'candidate = True\n'
            (release / 'build.json').write_text(json.dumps({
                'source_sha256': {target: hashlib.sha256(content).hexdigest()}}))
            with tarfile.open(release / 'generator-source.tar.gz', 'w:gz') as archive:
                entry = tarfile.TarInfo(target)
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
            self.assertEqual(module.restore_release_generator(destination, release)[target],
                             hashlib.sha256(content).hexdigest())
            self.assertEqual((destination / target).read_bytes(), content)
            (release / 'build.json').write_text(json.dumps({'source_sha256': {target: '0' * 64}}))
            with self.assertRaisesRegex(ValueError, '不一致'):
                module.restore_release_generator(destination, release)
            self.assertEqual((destination / target).read_bytes(), content)

    def test_snapshot_copies_only_required_releases_and_creates_test_workspace(self):
        with tempfile.TemporaryDirectory(prefix='implementation-snapshot-', dir=ROOT / 'workspace') as directory:
            destination = Path(directory) / 'source'
            module.snapshot_project(destination, {'candidate': 'data/releases/joint28'})
            self.assertEqual([path.name for path in (destination / 'data/releases').iterdir()], ['joint28'])
            self.assertTrue((destination / 'workspace/pipeline').is_dir())
            self.assertEqual(module.digest(destination / 'data/releases/joint28/hardware.json'),
                             module.digest(ROOT / 'data/releases/joint28/hardware.json'))

    def test_coder_uses_new_session_without_locking_global_planner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / 'source'
            campaign = root / 'campaign'
            campaign.mkdir()
            planner_id = str(uuid.uuid4())
            coder_id = str(uuid.uuid4())
            item = {'id': 'proposal-one', 'case': 'M1_P1', 'proposal': '独立结构',
                    'evidence_ids': []}
            baseline = {'id': 'base', 'config': {'hardware': {}, 'programs': {}}}

            class Child:
                returncode = 0
                def communicate(self, prompt, timeout):
                    self.stdout.write(json.dumps({'type': 'thread.started', 'thread_id': coder_id}) + '\n')
                    self.stdout.write(json.dumps({'type': 'turn.completed'}) + '\n')
                    return '', ''

            def launch(command, **kwargs):
                self.assertNotIn('resume', command)
                self.assertNotIn(planner_id, command)
                child = Child()
                child.stdout = kwargs['stdout']
                return child

            with patch.object(module, 'ROOT', root), \
                 patch.object(module, 'origin_root', return_value=root), \
                 patch.object(module, 'read', return_value=[]), \
                 patch.object(module.subprocess, 'Popen', side_effect=launch), \
                 patch.dict('os.environ', {'CODEX_SESSION_LOCK_ROOT': str(root / 'locks')}, clear=True):
                result = module.coding_turn(snapshot, campaign, planner_id, item, baseline,
                                            60, 'gpt-6-sol', 'medium')
            self.assertEqual(result['coder_session_id'], coder_id)
            self.assertTrue((root / 'locks/coder-proposal-one.session.lock').exists())
            self.assertFalse((root / 'locks' / (planner_id + '.session.lock')).exists())

    def test_completed_proposals_do_not_block_later_proposals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / 'workspace/pipeline/current'
            campaign.mkdir(parents=True)
            (campaign / 'state.json').write_text('{}')
            controller = root / 'workspace/implementation-loop/current'
            for identifier in ('one', 'two'):
                path = controller / identifier / 'state.json'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({'status': 'FAILED'}))
            items = [(identifier, {'id': identifier}) for identifier in ('one', 'two', 'three')]
            processed = []
            class Loop:
                directory = controller
                def __init__(self, *args, **kwargs):
                    pass
                def process(self, item):
                    processed.append(item['id'])
                    return {'status': 'REJECTED'}
            with patch.object(module, 'ROOT', root), \
                 patch.object(module, 'ImplementationLoop', Loop), \
                 patch.object(module, 'proposals', return_value=items):
                self.assertEqual(module.main(['--campaign', 'current', '--execute',
                                               '--max-proposals', '2']), 0)
            self.assertEqual(processed, ['three'])

    def test_new_epoch_receives_origin_evaluation_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            origin=root/'origin'
            snapshot=root/'epoch/source'
            with patch.object(module,'ROOT',snapshot), \
                 patch.object(module,'origin_root',return_value=origin), \
                 patch.dict('os.environ',{},clear=True):
                env=module.project_env(snapshot)
            self.assertEqual(env['CODESIGN_EVAL_CACHE_ROOT'],
                             str(origin/'workspace/search/cache'))
            self.assertEqual(env['CODESIGN_ORIGIN_ROOT'],str(origin))

    def test_next_epoch_inherits_controller_but_preserves_generator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current, origin, candidate = (root / name for name in ('current', 'origin', 'candidate'))
            controller = module.CONTROLLER_FILES[0]
            generator = 'src/codesign_lab/codegen/d1/compiler.py'
            trigger = 'src/codesign_lab/search/triggers.py'
            schema = 'schemas/ai-decision.schema.json'
            for base in (current, origin, candidate):
                for name in (controller, generator, trigger, schema):
                    (base / name).parent.mkdir(parents=True, exist_ok=True)
            (current / controller).write_text('old controller')
            (origin / controller).write_text('fixed controller')
            (candidate / controller).write_text('old controller')
            (candidate / generator).write_text('new epoch generator')
            (origin / trigger).write_text('fixed trigger')
            (candidate / trigger).write_text('old trigger')
            (origin / schema).write_text('{"fixed": true}')
            (candidate / schema).write_text('{"fixed": false}')
            for name in module.CONTROLLER_FILES[1:]:
                (origin / name).parent.mkdir(parents=True, exist_ok=True)
                (candidate / name).parent.mkdir(parents=True, exist_ok=True)
                (origin / name).write_text('fixed bridge')
                (candidate / name).write_text('old bridge')
            with patch.object(module, 'ROOT', current), patch.object(module, 'origin_root', return_value=origin):
                hashes = module.inherit_controller_files(candidate)
            self.assertEqual((candidate / controller).read_text(), 'fixed controller')
            self.assertEqual((candidate / generator).read_text(), 'new epoch generator')
            self.assertEqual((candidate / trigger).read_text(), 'fixed trigger')
            self.assertEqual((candidate / schema).read_text(), '{"fixed": true}')
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

    def test_transformation_identity_deduplicates_across_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / 'workspace/pipeline/current'
            campaign.mkdir(parents=True)
            (campaign / 'state.json').write_text(json.dumps({'source_epoch': 'epoch-a'}))
            (root / 'data').mkdir()
            decisions = [
                {'id': 'a' * 64, 'source_epoch': 'epoch-a', 'decision': {
                    'implementation_proposals': [{'lane': 'p1_w2', 'transformation_id': 'w2-shared-input',
                        'proposal': '复用 W2 输入', 'evidence_ids': []}]}},
                {'id': 'b' * 64, 'source_epoch': 'epoch-a', 'decision': {
                    'implementation_proposals': [
                        {'lane': 'p1_w2', 'transformation_id': 'w2-shared-input',
                         'proposal': '换一种说法描述输入复用', 'evidence_ids': []},
                        {'lane': 'd1_decode', 'transformation_id': 'w2-shared-input',
                         'proposal': 'D1 独立机制', 'evidence_ids': []}]}},
            ]
            (root / 'data/decisions.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in decisions))
            with patch.object(module, 'ROOT', root):
                rows = list(module.proposals(campaign))
            self.assertEqual([item['case'] for _, item in rows], ['M1_P1', 'M2_D1'])

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
