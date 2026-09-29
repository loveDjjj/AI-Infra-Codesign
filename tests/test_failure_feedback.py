"""失败结果、静态成本和诊断恢复的确定性回放。"""
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from codesign_lab.search import implementation, pipeline, profiles


class FailureFeedbackChecks(unittest.TestCase):
    def test_first_joint_point_over_power_still_releases_bounded_family_pilots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / 'workspace/pipeline/current'
            campaign.mkdir(parents=True)
            (campaign / 'state.json').write_text(json.dumps({'analysis': {'lanes': {
                'global': {'session_id': str(uuid.uuid4())}}}}))
            source = root / 'workspace/implementation-loop/current/joint/source'
            inputs = source / 'workspace/implementation-input'
            inputs.mkdir(parents=True)
            base = {'hardware': {'sm_count': 16}, 'programs': {
                'M1_P1': {'config': {}}, 'M2_D1': {'config': {}}}}
            inputs.joinpath('config.json').write_text(json.dumps({
                'hardware': {'sm_count': 24}, 'programs': base['programs']}))
            release = source / 'data/releases/base/local-grade.json'
            release.parent.mkdir(parents=True)
            release.write_text(json.dumps({'cases': {
                'M1_P1': {'timing': {'cycles': 391510}},
                'M2_D1': {'timing': {'cycles': 41737}}}}))
            for folder in ('baseline', 'default-off'):
                for name in implementation.ARTIFACTS:
                    path = source / 'workspace/implementation-builds' / folder / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('baseline')
            item = {'id': 'joint', 'case': 'both', 'decision_id': 'decision'}
            state_path = source.parent / 'state.json'
            state_path.write_text(json.dumps({'status': 'CODED', 'snapshot': str(source),
                'baseline_id': 'base', 'proposal': item}))
            baseline = {'id': 'base', 'candidate': 'data/releases/base',
                'reproduction': 'verified', 'config': base}
            def run(_source, _argv, label, **_kwargs):
                report = source / 'workspace/implementation-reports' / (label + '.json')
                report.parent.mkdir(parents=True, exist_ok=True)
                if label == 'functional':
                    report.write_text(json.dumps({'cases': {case: {'functional_passed': True}
                        for case in ('M1_P1', 'M2_D1')}}))
                if label == 'estimate':
                    report.write_text(json.dumps({'cases': {
                        'M1_P1': {'timing': {'cycles': 397048, 'peak_window_power_w': 20.344268}},
                        'M2_D1': {'timing': {'cycles': 41854, 'peak_window_power_w': 18.970615}}}}))
            def artifacts(path):
                return {'hardware.json': 'new' if path.name == 'candidate' else 'old',
                        'programs/M1_P1.asm': 'p1', 'programs/M2_D1.asm': 'd1'}
            outcome = {'comparison': {'product_ratio_to_parent':
                (397048 * 41854) / (391510 * 41737)},
                'constraints': {'power_ok_by_case': {'M1_P1': False, 'M2_D1': True}}}
            with patch.object(implementation, 'ROOT', root), \
                 patch.object(implementation, 'campaign_generator_unchanged', return_value=True), \
                 patch.object(implementation, 'verify_official'), \
                 patch.object(implementation, 'verify_snapshot_official'), \
                 patch.object(implementation, 'validate_diff', return_value=['codegen.py']), \
                 patch('codesign_lab.search.prune.reject', return_value=None), \
                 patch.object(implementation, 'read', return_value=[baseline]), \
                 patch.object(implementation, 'artifacts', side_effect=artifacts), \
                 patch.object(implementation, 'interpreter', return_value='/test/python'), \
                 patch.object(implementation, 'run', side_effect=run), \
                 patch.object(implementation, 'run_regression'), \
                 patch.object(implementation, 'measured_outcome', return_value=outcome), \
                 patch.object(implementation, 'prepare_family_pilot', return_value=True), \
                 patch.object(implementation, 'record_research_candidate',
                              return_value='research-joint') as record, \
                 patch.object(implementation.ImplementationLoop, 'finish_research',
                              side_effect=lambda _item, state, _save: state) as seed:
                result = implementation.ImplementationLoop(campaign).process(item, phase='validate')
            self.assertEqual(result['status'], 'RESEARCH_READY', result.get('error'))
            self.assertLess(result['case_gain'], 0)
            self.assertNotEqual(record.call_args.args[4], 0)
            self.assertFalse(record.call_args.kwargs['admission'])
            self.assertTrue(record.call_args.kwargs['family_pilot_approved'])
            self.assertEqual(set(result['timing']['cases']), {'M1_P1', 'M2_D1'})
            seed.assert_called_once()

    def test_vec16_power_failure_keeps_true_product_and_constraints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / 'data/releases/parent/local-grade.json'
            release.parent.mkdir(parents=True)
            release.write_text(json.dumps({'cases': {
                'M1_P1': {'timing': {'cycles': 391510}},
                'M2_D1': {'timing': {'cycles': 41737}}}}))
            report = root / 'estimate.json'
            report.write_text(json.dumps({'area_mm2': 23.9, 'cases': {
                'M1_P1': {'functional_passed': True, 'timing': {
                    'cycles': 397048, 'peak_window_power_w': 20.344268,
                    'instruction_count': 110384, 'hbm_read_bytes': 32332288}},
                'M2_D1': {'functional_passed': True, 'timing': {
                    'cycles': 41854, 'peak_window_power_w': 18.970615}}}}))
            champion = {'id': 'champion', 'scope': 'full', 'audited': True,
                'eligible': True, 'score': 49697.96, 'cases': {
                'M1_P1': {'timing': {'cycles': 391510}},
                'M2_D1': {'timing': {'cycles': 41737}}}}
            with patch.object(implementation, 'read', return_value=[champion]):
                result = implementation.measured_outcome(root,
                    {'case': 'both', 'parent_record': 'parent'}, report, 'parent')
            self.assertGreater(result['comparison']['product_ratio_to_champion'], 1)
            self.assertLess(result['comparison']['score_ratio_ignoring_gates'], 1)
            self.assertFalse(result['constraints']['power_ok_by_case']['M1_P1'])
            self.assertEqual(result['case_metrics']['M1_P1']['instruction_count'], 110384)
            self.assertEqual(result['case_metrics']['M1_P1']['hbm_read_bytes'], 32332288)

    def test_32sm_cost_screen_rejects_uncompensated_tc_rf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'workspace/implementation-input/cost-screen.json'
            path.parent.mkdir(parents=True)
            row = {'name': 'P1.W1', 'tc_ratio': 1027/259, 'rf_ratio': 1600/416,
                   'parallel_ratio': 2, 'traffic_ratio': 1,
                   'evidence': '官方静态服务公式：小阵列与低端口的同一条 W1 MMA'}
            path.write_text(json.dumps({'schema_version': 1, 'hotspots': [row]}))
            with self.assertRaisesRegex(ValueError, '超过并行补偿'):
                implementation.check_joint_cost_screen(root)
            row['rf_ratio'] = 1.1
            path.write_text(json.dumps({'schema_version': 1, 'hotspots': [row]}))
            self.assertEqual(implementation.check_joint_cost_screen(root)['hotspots'][0]['name'], 'P1.W1')

    def test_matching_d1_trace_wakes_same_proposal_without_new_simulation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / 'workspace/pipeline/current'
            campaign.mkdir(parents=True)
            state_path = root / 'workspace/implementation-loop/current/d1/state.json'
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({'status': 'NEEDS_EVIDENCE',
                'snapshot': str(state_path.parent / 'source'),
                'evidence_request': {'record_id': 'parent', 'case': 'M2_D1',
                    'question': '后七步 attention 到 WO 的匹配事件与读写依赖',
                    'required_artifacts': ['matching_trace', 'operator_map']}}))
            trace = root / 'matching-trace.json'
            trace.write_text('{}')
            instance = pipeline.Pipeline.__new__(pipeline.Pipeline)
            instance.out = campaign
            instance.pool = SimpleNamespace(source_epoch='epoch', state={
                'profiles': {}, 'applied_decisions': {}})
            instance.triggers = Mock()
            instance.enqueue = Mock()
            instance.family_sources = {}
            with patch.object(pipeline, 'ROOT', root), \
                 patch.object(pipeline, 'read', return_value=[{'id': 'parent'}]), \
                 patch.object(profiles, 'inputs', return_value=(root, trace)), \
                 patch.object(profiles, 'matching_trace', return_value=trace), \
                 patch.object(implementation, 'provision_parent_evidence',
                              return_value={'record_id': 'parent', 'trace': 'local-trace.json'}):
                instance.process_profile_requests()
            self.assertEqual(json.loads(state_path.read_text())['status'], 'EVIDENCE_READY')
            self.assertEqual(len(instance.pool.state['profiles']), 1)
            instance.enqueue.assert_not_called()
            instance.process_profile_requests()
            self.assertEqual(len(instance.pool.state['profiles']), 1)

    def test_current_campaign_snapshot_includes_structural_power_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'workspace/implementation-loop/current/vec/state.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'status': 'REJECTED', 'research_record': 'research-vec',
                'proposal': {'transformation_id': 'vec16'},
                'outcome': {'constraints': {'power_ok_by_case': {'M1_P1': False}}}}))
            (root / 'data').mkdir()
            (root / 'data/state.json').write_text('{}')
            record = {'id': 'research-vec', 'campaign': 'implementation-vec',
                'scope': 'both', 'config': {'hardware': {}, 'programs': {
                    'M1_P1': {'config': {}}}}, 'cases': {'M1_P1': {
                    'functional_passed': True, 'timing': {'cycles': 397048,
                    'peak_window_power_w': 20.344268, 'instruction_count': 110384,
                    'hbm_read_bytes': 32332288}}},
                'outcome': {'comparison': {'product_ratio_to_champion': 1.02},
                            'constraints': {'power_ok_by_case': {'M1_P1': False}}}}
            instance = pipeline.Pipeline.__new__(pipeline.Pipeline)
            instance.out = root / 'workspace/pipeline/current'
            instance.pool = SimpleNamespace(state={'targets': {}})
            with patch.object(pipeline, 'ROOT', root), \
                 patch.object(pipeline, 'read', return_value=[record]):
                snapshot = instance.global_snapshot()
            self.assertEqual(snapshot['case_facts'][0]['record_id'], 'research-vec')
            self.assertEqual(snapshot['case_facts'][0]['hbm_read_bytes'], 32332288)
            self.assertFalse(snapshot['implementation_states'][0]['outcome']
                             ['constraints']['power_ok_by_case']['M1_P1'])


if __name__ == '__main__':
    unittest.main()
