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
    def test_joint_hardware_validation_checks_both_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / 'workspace/pipeline/current'
            campaign.mkdir(parents=True)
            (campaign / 'state.json').write_text(json.dumps({'analysis': {'lanes': {
                'global': {'session_id': str(uuid.uuid4())}}}}))
            source = root / 'workspace/implementation-loop/current/joint/source'
            (source / 'workspace/implementation-input').mkdir(parents=True)
            base = {'hardware': {'sm_count': 16},
                    'programs': {'M1_P1': {'config': {}}, 'M2_D1': {'config': {}}}}
            changed = {'hardware': {'sm_count': 24}, 'programs': base['programs']}
            (source / 'workspace/implementation-input/config.json').write_text(json.dumps(changed))
            release = source / 'data/releases/base/local-grade.json'
            release.parent.mkdir(parents=True)
            release.write_text(json.dumps({'cases': {
                'M1_P1': {'timing': {'cycles': 1000}},
                'M2_D1': {'timing': {'cycles': 100}}}}))
            for folder in ('baseline', 'default-off'):
                for name in module.ARTIFACTS:
                    path = source / 'workspace/implementation-builds' / folder / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('baseline')
            state_path = source.parent / 'state.json'
            proposal = {'id': 'joint', 'case': 'both', 'decision_id': 'decision'}
            state_path.write_text(json.dumps({'status': 'CODED', 'snapshot': str(source),
                'baseline_id': 'base', 'proposal': proposal}))
            baseline = {'id': 'base', 'candidate': 'data/releases/base',
                'reproduction': 'verified', 'config': base}
            commands = {}
            def run(_source, argv, label, **_kwargs):
                commands[label] = argv
                if label == 'functional':
                    output = source / 'workspace/implementation-reports/functional.json'
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(json.dumps({'cases': {
                        case: {'functional_passed': True} for case in ('M1_P1', 'M2_D1')}}))
                if label == 'estimate':
                    output = source / 'workspace/implementation-reports/estimate.json'
                    output.write_text(json.dumps({'cases': {
                        'M1_P1': {'timing': {'cycles': 900, 'peak_window_power_w': 19}},
                        'M2_D1': {'timing': {'cycles': 95, 'peak_window_power_w': 18}}}}))
            def artifacts(path):
                return {'hardware.json': 'new' if path.name == 'candidate' else 'old',
                        'programs/M1_P1.asm': 'p1', 'programs/M2_D1.asm': 'd1'}
            with patch.object(module, 'ROOT', root), \
                 patch.object(module, 'campaign_generator_unchanged', return_value=True), \
                 patch.object(module, 'verify_official'), \
                 patch.object(module, 'verify_snapshot_official'), \
                 patch.object(module, 'validate_diff', return_value=['codegen.py']), \
                 patch('codesign_lab.search.prune.reject', return_value=None), \
                 patch.object(module, 'read', return_value=[baseline]), \
                 patch.object(module, 'artifacts', side_effect=artifacts), \
                 patch.object(module, 'interpreter', return_value='/test/python'), \
                 patch.object(module, 'run', side_effect=run), \
                 patch.object(module, 'run_regression'), \
                 patch.object(module, 'record_research_candidate', return_value='research-joint'), \
                 patch.object(module.ImplementationLoop, 'finish_research') as seed:
                seed.side_effect = lambda item, state, save: state
                result = module.ImplementationLoop(campaign).process(proposal, phase='validate')
            self.assertEqual(result['status'], 'RESEARCH_READY', result.get('error'))
            self.assertAlmostEqual(result['case_gain'], .145)
            self.assertNotIn('--case', commands['functional'])
            self.assertNotIn('--case', commands['estimate'])
            seed.assert_called_once()

    def test_best_record_includes_reproducible_epoch_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / 'data/releases/new-best'
            release.mkdir(parents=True)
            for name in ('build.json', 'generator-source.tar.gz', 'local-grade.json'):
                (release / name).write_text('{}')
            older = {'scope': 'full', 'eligible': True, 'audited': True,
                     'score': 48000, 'reproduction': 'verified',
                     'candidate': 'data/releases/new-best'}
            newer = dict(older, score=49000, reproduction='epoch_verified')
            with patch.object(module, 'ROOT', root), patch.object(module, 'read', return_value=[older, newer]):
                self.assertEqual(module.best_record(), newer)
                (release / 'generator-source.tar.gz').unlink()
                self.assertEqual(module.best_record(), older)

    def test_case_baseline_uses_fastest_compatible_audited_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('overall', 'faster-p1', 'other-hardware'):
                release = root / 'data/releases' / name
                release.mkdir(parents=True)
                (release / 'local-grade.json').write_text('{}')
            def row(name, score, hardware, p1, d1):
                return {'id': name, 'scope': 'full', 'eligible': True, 'audited': True,
                        'score': score, 'reproduction': 'verified',
                        'candidate': 'data/releases/' + name,
                        'config': {'hardware': hardware}, 'cases': {
                            case: {'functional_passed': True, 'timing': {
                                'cycles': cycles, 'peak_window_power_w': 19}}
                            for case, cycles in (('M1_P1', p1), ('M2_D1', d1))}}
            overall = row('overall', 49283, {'sm_count': 16}, 396729, 41883)
            p1 = row('faster-p1', 48695, {'sm_count': 16}, 394545, 43139)
            alien = row('other-hardware', 48000, {'sm_count': 32}, 300000, 30000)
            with patch.object(module, 'ROOT', root), \
                 patch.object(module, 'read', return_value=[overall, p1, alien]):
                self.assertEqual(module.best_record(), overall)
                self.assertEqual(module.best_record('M1_P1'), p1)
                self.assertEqual(module.best_record('M2_D1'), overall)

    def test_epoch_snapshot_restores_release_generator_before_git_baseline(self):
        release = ROOT / 'data/releases/official-1790581630771528243'
        if not release.is_dir():
            self.skipTest('本机没有受保护的最高分 release')
        with tempfile.TemporaryDirectory(prefix='implementation-snapshot-', dir=ROOT / 'workspace') as directory:
            destination = Path(directory) / 'source'
            baseline = {'candidate': str(release), 'reproduction': 'epoch_verified'}
            original = module.snapshot_project(destination, baseline)
            expected = json.loads((release / 'build.json').read_text())['source_sha256']
            actual = {name: module.digest(destination / name) for name in expected}
            self.assertEqual(actual, expected)
            self.assertEqual(json.loads((destination / 'workspace/source-lineage.json').read_text())
                             ['generator_hashes'], expected)
            self.assertEqual(original, {name: module.digest(ROOT / name) for name in original})

    def test_validate_worker_accepts_official_handoff_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / 'workspace/pipeline/current'
            campaign.mkdir(parents=True)
            (campaign / 'state.json').write_text('{}')
            item = {'id': 'candidate', 'case': 'M1_P1'}
            with patch.object(module, 'ROOT', root), \
                 patch.object(module, 'proposals', return_value=[('candidate', item)]), \
                 patch.object(module, 'process_locked', return_value={
                     'status': 'OFFICIAL_QUEUED'}):
                result = module.main(['--campaign', 'current', '--proposal-id', 'candidate',
                                      '--execute', '--worker', '--phase', 'validate'])
            self.assertEqual(result, 0)

    def test_large_single_case_regression_pauses_automatic_neighborhood(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / 'workspace/pipeline/current'
            campaign.mkdir(parents=True)
            (campaign / 'state.json').write_text(json.dumps({'analysis': {'lanes': {
                'global': {'session_id': str(uuid.uuid4())}}}}))
            proposal = {'id': 'regressed', 'case': 'M1_P1', 'decision_id': 'decision'}
            source = root / 'workspace/implementation-loop/current/regressed/source'
            (source / 'workspace/implementation-input').mkdir(parents=True)
            (source / 'workspace/implementation-input/config.json').write_text(json.dumps({
                'hardware': {}, 'programs': {'M1_P1': {'new_switch': True}, 'M2_D1': {}}}))
            release = source / 'data/releases/joint28/local-grade.json'
            release.parent.mkdir(parents=True)
            release.write_text(json.dumps({'cases': {'M1_P1': {'timing': {'cycles': 1000}}}}))
            for name in module.ARTIFACTS:
                path = source / 'workspace/implementation-builds/baseline' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('baseline')
            state_path = source.parent / 'state.json'
            state_path.write_text(json.dumps({'status': 'CODED', 'snapshot': str(source),
                'baseline_id': 'base', 'proposal': proposal}))
            baseline = {'id': 'base', 'candidate': 'data/releases/joint28', 'config': {
                'hardware': {}, 'programs': {'M1_P1': {}, 'M2_D1': {}}}}
            def artifacts(path):
                version = 'candidate' if path.name == 'candidate' else 'baseline'
                return {'hardware.json': 'same', 'programs/M1_P1.asm': version,
                        'programs/M2_D1.asm': 'same'}
            def run(_source, argv, label, **_kwargs):
                if label == 'functional':
                    output = source / 'workspace/implementation-reports/functional.json'
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(json.dumps({'cases': {'M1_P1': {'functional_passed': True}}}))
                if label == 'estimate':
                    output = source / 'workspace/implementation-reports/estimate.json'
                    output.write_text(json.dumps({'cases': {'M1_P1': {'timing': {
                        'cycles': 1100, 'peak_window_power_w': 19}}}}))
            with patch.object(module, 'ROOT', root), \
                 patch.object(module, 'campaign_generator_unchanged', return_value=True), \
                 patch.object(module, 'verify_official'), \
                 patch.object(module, 'verify_snapshot_official'), \
                 patch.object(module, 'validate_diff', return_value=['codegen.py']), \
                 patch.object(module, 'preserved_fields', return_value=True), \
                 patch.object(module, 'read', return_value=[baseline]), \
                 patch.object(module, 'artifacts', side_effect=artifacts), \
                 patch.object(module, 'interpreter', return_value='/test/python'), \
                 patch.object(module, 'run', side_effect=run), \
                 patch.object(module, 'run_regression'), \
                 patch.object(module, 'record_research_candidate', return_value='research-id') as record, \
                 patch.object(module.ImplementationLoop, 'finish_research') as seed:
                result = module.ImplementationLoop(campaign).process(proposal, phase='validate')
            self.assertEqual(result['status'], 'RESEARCH_PAUSED', result.get('error'))
            self.assertAlmostEqual(result['case_gain'], -0.1)
            self.assertEqual(result['research_record'], 'research-id')
            self.assertIs(record.call_args.kwargs['admission'], False)
            self.assertIn('RESEARCH_PAUSED', module.TERMINAL)
            seed.assert_not_called()

    def test_code_phase_stops_at_coded_without_running_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / 'workspace/pipeline/current'
            campaign.mkdir(parents=True)
            (campaign / 'state.json').write_text(json.dumps({'analysis': {'lanes': {
                'global': {'session_id': str(uuid.uuid4())}}}}))
            manifest = root / 'vendor/official/isolation-manifest.json'
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{}')
            baseline = {'id': 'base', 'candidate': 'data/releases/joint28',
                'config': {'hardware': {}, 'programs': {}}}
            item = {'id': 'first', 'case': 'M1_P1', 'lane': 'p1_w2',
                'source_epoch': 'epoch', 'decision_id': 'decision',
                'proposal': '新结构', 'evidence_ids': []}
            def snapshot(destination, unused):
                destination.mkdir(parents=True)
                (destination / 'data/releases/joint28').mkdir(parents=True)
                (destination / 'data/releases/joint28/local-grade.json').write_text('{}')
                (destination / 'data/experiments.jsonl').write_text('')
                return {}
            with patch.object(module, 'ROOT', root), \
                 patch.object(module, 'campaign_generator_unchanged', return_value=True), \
                 patch.object(module, 'verify_official'), \
                 patch.object(module, 'best_record', return_value=baseline), \
                 patch.object(module, 'snapshot_project', side_effect=snapshot), \
                 patch.object(module, 'interpreter', return_value='/usr/bin/python3'), \
                 patch.object(module, 'run'), \
                 patch.object(module, 'coding_turn', return_value={
                     'coder_session_id': str(uuid.uuid4())}), \
                 patch.object(module, 'run_regression') as regression:
                loop = module.ImplementationLoop(campaign)
                result = loop.process(item, phase='code')
            self.assertEqual(result['status'], 'CODED', result.get('error'))
            regression.assert_not_called()

    def test_static_refutation_is_recorded_without_candidate_or_simulator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / 'workspace/pipeline/current'
            campaign.mkdir(parents=True)
            (campaign / 'state.json').write_text(json.dumps({'analysis': {'lanes': {
                'global': {'session_id': str(uuid.uuid4())}}}}))
            official = root / 'vendor/official/isolation-manifest.json'
            official.parent.mkdir(parents=True)
            official.write_text('{}')
            baseline = {'id': 'base', 'candidate': 'data/releases/joint28',
                'config': {'hardware': {}, 'programs': {}}}
            item = {'id': 'first', 'case': 'M1_P1', 'lane': 'p1_ffn',
                'source_epoch': 'epoch', 'decision_id': 'decision',
                'transformation_id': 'ffn-static', 'proposal': '静态核算', 'evidence_ids': []}
            def snapshot(destination, unused):
                destination.mkdir(parents=True)
                (destination / 'data/releases/joint28').mkdir(parents=True)
                (destination / 'data/releases/joint28/local-grade.json').write_text('{}')
                (destination / 'data/experiments.jsonl').write_text('')
                return {}
            def coding(destination, *args):
                stop = destination / 'workspace/implementation-input/stop.json'
                stop.parent.mkdir(parents=True)
                stop.write_text(json.dumps({'schema_version': 1,
                    'mechanism_id': 'ffn-static',
                    'reason': '权重重读量超过可节省的中间激活流量，停止该微块方案',
                    'evidence': ['M8 每层需要额外读取约三十 MiB 权重']}))
                return {'coder_session_id': str(uuid.uuid4())}
            with patch.object(module, 'ROOT', root), \
                 patch.object(module, 'campaign_generator_unchanged', return_value=True), \
                 patch.object(module, 'verify_official'), \
                 patch.object(module, 'best_record', return_value=baseline), \
                 patch.object(module, 'snapshot_project', side_effect=snapshot), \
                 patch.object(module, 'interpreter', return_value='/usr/bin/python3'), \
                 patch.object(module, 'run'), \
                 patch.object(module, 'coding_turn', side_effect=coding), \
                 patch.object(module, 'run_regression') as regression:
                result = module.ImplementationLoop(campaign).process(item, phase='code')
            self.assertEqual(result['status'], 'REJECTED', result.get('error'))
            self.assertEqual(result['rejection_kind'], 'static_refutation')
            regression.assert_not_called()

    def test_same_proposal_cannot_run_from_worker_and_manual_cli_together(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            class Loop:
                directory = root
                def process(self, item):
                    raise AssertionError('重复执行')
            loop = Loop()
            path = root / 'same/proposal.lock'
            path.parent.mkdir()
            with path.open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    module.process_locked(loop, {'id': 'same'})

    def test_handoff_only_launches_verified_waiting_version_after_lock_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / 'workspace/pipeline/current'
            campaign.mkdir(parents=True)
            state_path = root / 'workspace/implementation-loop/current/proposal/state.json'
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({'status': 'WAITING_FOR_LAUNCH',
                'snapshot': str(root / 'snapshot'), 'next_settings': str(root / 'next.json')}))
            with patch.object(module, 'ROOT', root), \
                 patch.object(module, 'launch_next', return_value=None) as launch:
                self.assertTrue(module.pending_handoff(campaign))
                self.assertIsNone(module.handoff_waiting(campaign))
                self.assertEqual(json.loads(state_path.read_text())['status'], 'WAITING_FOR_LAUNCH')
                launch.return_value = {'pid': 123, 'log': 'next.log'}
                self.assertEqual(module.handoff_waiting(campaign)['pid'], 123)
                self.assertIsNone(module.handoff_waiting(campaign))
                self.assertFalse(module.pending_handoff(campaign))
            self.assertEqual(json.loads(state_path.read_text())['status'], 'LAUNCHED')
            self.assertEqual(launch.call_count, 2)

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
            self.assertTrue((destination / 'workspace/families').is_dir())
            self.assertTrue((destination / 'workspace/search-env/bin/python').is_file())
            self.assertEqual(module.digest(destination / 'data/releases/joint28/hardware.json'),
                             module.digest(ROOT / 'data/releases/joint28/hardware.json'))

    def test_official_phase_only_starts_after_single_case_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / 'workspace/pipeline/test-campaign'
            snapshot = root / 'snapshot'
            state_path = root / 'workspace/implementation-loop/test-campaign/proposal/state.json'
            state_path.parent.mkdir(parents=True)
            snapshot.mkdir()
            expected = {'hardware.json': 'hw', 'programs/M1_P1.asm': 'p1',
                        'programs/M2_D1.asm': 'd1'}
            state_path.write_text(json.dumps({'status': 'OFFICIAL_QUEUED',
                'snapshot': str(snapshot), 'artifact_sha256': expected}))
            item = {'id': 'proposal', 'case': 'M1_P1'}
            def grade(_snapshot, argv, _label, **_kwargs):
                report = Path(argv[-1]);report.parent.mkdir(parents=True)
                report.write_text(json.dumps({'experimental_score': 50000}))
            with patch.object(module, 'ROOT', root), \
                 patch.object(module, 'artifacts', return_value=expected), \
                 patch.object(module, 'verify_official'), \
                 patch.object(module, 'verify_snapshot_official'), \
                 patch.object(module, 'interpreter', return_value='/test/python'), \
                 patch.object(module, 'run', side_effect=grade) as run, \
                 patch.object(module.ImplementationLoop, 'finish_graded',
                              side_effect=lambda _item, state, _save: state):
                loop = module.ImplementationLoop(campaign)
                self.assertEqual(loop.process(item, phase='validate')['status'], 'OFFICIAL_QUEUED')
                run.assert_not_called()
                result = loop.process(item, phase='official')
            self.assertEqual(result['status'], 'GRADED')
            self.assertTrue(result['official_attempted'])
            self.assertEqual(result['official_score'], 50000)
            run.assert_called_once()

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

    def test_research_fact_is_recorded_in_child_source_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / 'source'
            origin = root / 'origin'
            (origin / 'data').mkdir(parents=True)
            (origin / 'data/experiments.jsonl').write_text('')
            candidate = snapshot / 'workspace/candidate'
            estimate = snapshot / 'workspace/estimate.json'
            (snapshot / 'data').mkdir(parents=True)
            (snapshot / 'data/experiments.jsonl').write_text('')
            (candidate / 'programs').mkdir(parents=True)
            (candidate / 'hardware.json').write_text('{}')
            (candidate / 'programs/M1_P1.asm').write_text('P1')
            (candidate / 'programs/M2_D1.asm').write_text('D1')
            (candidate / 'config.json').write_text(json.dumps({'hardware': {}, 'programs': {}}))
            estimate.write_text(json.dumps({'hardware': {}, 'program_sha256': {
                'M1_P1': hashlib.sha256(b'P1').hexdigest()}, 'cases': {'M1_P1': {
                'functional_passed': True, 'timing': {'cycles': 101, 'peak_window_power_w': 19}}}}))
            item = {'id': 'structure-1', 'source_epoch': 'old', 'decision_id': 'decision', 'case': 'M1_P1'}
            with patch.object(module, 'origin_root', return_value=origin):
                first = module.record_research_candidate(snapshot, item, candidate, estimate, -0.01)
                second = module.record_research_candidate(snapshot, item, candidate, estimate, -0.01)
            rows = [json.loads(line) for line in (snapshot / 'data/experiments.jsonl').read_text().splitlines()]
            exported = [json.loads(line) for line in (origin / 'data/experiments.jsonl').read_text().splitlines()]
            self.assertEqual(first, second)
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(exported), 1)
            self.assertEqual(rows[0]['report'], 'workspace/estimate.json')
            self.assertTrue(Path(exported[0]['report']).is_file())
            self.assertEqual(exported[0]['candidate'], str(candidate))
            self.assertIsNone(rows[0]['score'])

            power_report = snapshot / 'workspace/power-estimate.json'
            power_report.write_text(json.dumps({'hardware': {}, 'program_sha256': {
                'M1_P1': hashlib.sha256(b'P1').hexdigest()}, 'cases': {'M1_P1': {
                'functional_passed': True, 'timing': {'cycles': 99,
                    'peak_window_power_w': 20.1, 'resource_stats': {'large': 'raw'}}}}}))
            power_item = dict(item, id='structure-power')
            with patch.object(module, 'origin_root', return_value=origin):
                power_id = module.record_research_candidate(
                    snapshot, power_item, candidate, power_report, 0.01, admission=False)
            power_record = next(row for row in
                (json.loads(line) for line in (origin / 'data/experiments.jsonl').read_text().splitlines())
                if row['id'] == power_id)
            self.assertIs(power_record['eligible'], False)
            self.assertIs(power_record['research_admission'], False)
            self.assertNotIn('resource_stats', power_record['cases']['M1_P1']['timing'])
            self.assertTrue(Path(power_record['report']).is_file())

    def test_next_epoch_does_not_seed_unrelated_legacy_neighborhood(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory)
            settings = snapshot / 'configs/pipeline.yaml'
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_bytes((ROOT / 'configs/pipeline.yaml').read_bytes())
            source = snapshot / 'src/codesign_lab/codegen/one.py'
            source.parent.mkdir(parents=True)
            source.write_text('x=1\n')
            with patch.object(module, 'run'):
                output = module.seed_next_campaign(snapshot, {'id': 'structure-1', 'case': 'M1_P1'},
                                                   'research-structure-1', str(uuid.uuid4()))
            payload = json.loads((snapshot / 'workspace/next-epoch-targets.json').read_text())
            self.assertEqual(payload['targets'], [])
            self.assertEqual(json.loads(output.read_text())['budget']['max_proposals'], 8)

    def test_research_admission_waits_for_global_lock_then_launches(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory)
            manifest = snapshot / 'workspace/implementation-input/capabilities.json'
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{}')
            loop = module.ImplementationLoop.__new__(module.ImplementationLoop)
            state = {'status': 'RESEARCH_READY', 'snapshot': str(snapshot),
                     'research_record': 'research-one', 'session_id': str(uuid.uuid4())}
            def save(status, **fields):
                state.update(status=status, **fields)
            with patch.object(module, 'seed_next_campaign', return_value=snapshot / 'settings.json') as seed, \
                 patch.object(module, 'launch_next', side_effect=[None, {'pid': 123}]) as launch:
                loop.finish_research({'id': 'one'}, state, save)
                self.assertEqual(state['status'], 'WAITING_FOR_LAUNCH')
                self.assertEqual(state['research_record'], 'research-one')
                loop.finish_research({'id': 'one'}, state, save)
                self.assertEqual(state['status'], 'LAUNCHED')
                self.assertEqual(seed.call_count, 2)
                self.assertEqual(launch.call_count, 2)

    def test_audited_small_gain_enters_research_without_promotion(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory)
            report = snapshot / 'official.json'
            report.write_text(json.dumps({'eligible': True, 'experimental_score': 500}))
            loop = module.ImplementationLoop.__new__(module.ImplementationLoop)
            loop.min_score_gain = 100
            state = {'status': 'GRADED', 'snapshot': str(snapshot), 'report': str(report),
                     'session_id': str(uuid.uuid4())}
            def save(status, **fields):
                state.update(status=status, **fields)
            item = {'id': 'proposal', 'case': 'M1_P1'}
            record = {'id': 'audited-case', 'audited': True}
            capability = snapshot / 'workspace/implementation-input/capabilities.json'
            capability.parent.mkdir(parents=True)
            capability.write_text('{}')
            with patch.object(module, 'grade_record', return_value=record), \
                 patch.object(module, 'audited_best_score', return_value=1000), \
                 patch.object(module, 'export_epoch_record') as export, \
                 patch.object(module, 'seed_next_campaign', return_value=snapshot / 'settings.json'), \
                 patch.object(module, 'launch_next', return_value=None), \
                 patch.object(module, 'run') as run:
                loop.finish_graded(item, state, save)
            self.assertEqual(state['status'], 'WAITING_FOR_LAUNCH')
            self.assertEqual(state['research_record'], 'audited-case')
            self.assertEqual(state['audit_record'], 'audited-case')
            export.assert_called_once_with(snapshot, 'audited-case', item)
            self.assertEqual(run.call_args.args[2], 'retain-research-grade')

    def test_retained_below_best_grade_resumes_without_regrading(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory)
            report = snapshot / 'workspace/official.json'
            report.parent.mkdir()
            report.write_text(json.dumps({'eligible': True, 'experimental_score': 500}))
            release = snapshot / 'data/releases/audited-case'
            release.mkdir(parents=True)
            (release / 'local-grade.json').write_bytes(report.read_bytes())
            ledger = snapshot / 'data/experiments.jsonl'
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text(json.dumps({'id': 'audited-case', 'scope': 'full',
                'eligible': True, 'audited': True,
                'report': 'data/releases/audited-case/local-grade.json'}) + '\n')
            loop = module.ImplementationLoop.__new__(module.ImplementationLoop)
            loop.min_score_gain = 100
            state = {'status': 'GRADED', 'snapshot': str(snapshot), 'report': str(report),
                     'audit_record': 'audited-case', 'comparison_score': 1000,
                     'session_id': str(uuid.uuid4())}
            def save(status, **fields):
                state.update(status=status, **fields)
            item = {'id': 'proposal', 'case': 'M1_P1'}
            capability = snapshot / 'workspace/implementation-input/capabilities.json'
            capability.parent.mkdir(parents=True)
            capability.write_text('{}')
            with patch.object(module, 'grade_record') as lookup, \
                 patch.object(module, 'export_epoch_record') as export, \
                 patch.object(module, 'seed_next_campaign', return_value=snapshot / 'settings.json'), \
                 patch.object(module, 'launch_next', return_value=None), \
                 patch.object(module, 'run') as run:
                loop.finish_graded(item, state, save)
            self.assertEqual(state['status'], 'WAITING_FOR_LAUNCH')
            lookup.assert_not_called()
            run.assert_not_called()
            export.assert_called_once_with(snapshot, 'audited-case', item)

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
