"""验证官方验收优先、预留容量与资源准入。"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from codesign_lab.evaluation.pipeline import compact_cases

spec = importlib.util.spec_from_file_location('rolling_pipeline', Path(__file__).resolve().parents[1] / 'scripts/pipeline.py')
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


def job(name, stage='case', memory=1):
    return {'key': name, 'stage': stage, 'memory_bytes': memory}


class PipelineChecks(unittest.TestCase):
    def test_promoted_release_is_available_for_case_combination(self):
        import codesign_lab.search.pipeline as controller_module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'data/releases/joint28').mkdir(parents=True)
            (root / 'data/releases/top').mkdir(parents=True)
            (root / 'data/state.json').write_text(json.dumps({'promoted_record': 'top-record'}))
            (root / 'data/experiments.jsonl').write_text(json.dumps({
                'id': 'top-record', 'candidate': 'data/releases/top',
                'eligible': True, 'audited': True}) + '\n')
            for name, cycles in [('joint28', 100), ('top', 80)]:
                report = {'cases': {'M1_P1': {'timing': {'cycles': cycles,
                    'peak_window_power_w': 19}}}}
                (root / 'data/releases' / name / 'local-grade.json').write_text(json.dumps(report))
            instance = pipeline.Pipeline.__new__(pipeline.Pipeline)
            instance.observe = Mock()
            with patch.object(controller_module, 'ROOT', root):
                instance.accept_release()
            self.assertEqual(instance.observe.call_count, 2)
            self.assertEqual(instance.observe.call_args.args[0], root / 'data/releases/top')

    def test_official_case_compaction_preserves_metrics_without_nested_resource_trace(self):
        original={'M1_P1':{'functional_passed':True,'timing':{
            'cycles':397212,'peak_window_power_w':19.1,
            'resource_stats':{'large_trace':[1,2,3]}}},
            'M2_D1':{'functional_passed':False,'timing':{'cycles':43523}}}
        compact=compact_cases(original)
        self.assertEqual(compact['M1_P1']['timing'],
            {'cycles':397212,'peak_window_power_w':19.1})
        self.assertFalse(compact['M2_D1']['functional_passed'])
        self.assertIn('resource_stats',original['M1_P1']['timing'])

    def test_full_grade_starts_before_waiting_exploration(self):
        selected = pipeline.select_jobs([job('explore'), job('grade', 'full')], [], 0, 4, 1, 10, 10)
        self.assertEqual(selected[0]['key'], 'grade')

    def test_exploration_preserves_full_grade_slot(self):
        active = [job('a'), job('b'), job('c')]
        self.assertEqual(pipeline.select_jobs([job('d')], active, 0, 4, 1, 10, 10), [])
        selected = pipeline.select_jobs([job('d'), job('grade', 'full')], active, 0, 4, 1, 10, 10)
        self.assertEqual([x['key'] for x in selected], ['grade'])

    def test_existing_batches_count_against_total_capacity(self):
        selected = pipeline.select_jobs([job('a'), job('b'), job('g', 'full')], [], 3, 4, 1, 10, 10)
        self.assertEqual([x['key'] for x in selected], ['g'])

    def test_memory_and_full_concurrency_are_hard_limits(self):
        self.assertEqual(pipeline.select_jobs([job('g', 'full', 4)], [], 0, 4, 1, 10, 3), [])
        self.assertEqual(pipeline.select_jobs([job('g', 'full')], [job('running', 'full')], 0, 4, 1, 10, 10), [])

    def test_structure_uses_same_capacity_and_preserves_one_official_slot(self):
        selected = pipeline.select_jobs([job('search'), job('structure', 'implementation'),
            job('grade', 'full')], [], 0, 4, 2, 10, 10)
        self.assertEqual([item['key'] for item in selected], ['grade', 'structure', 'search'])
        self.assertEqual(pipeline.select_jobs([job('second', 'implementation')],
            [job('first', 'implementation')], 0, 4, 2, 10, 10), [])
        self.assertEqual([item['key'] for item in pipeline.select_jobs([job('grade', 'full')],
            [job('first', 'implementation')], 0, 4, 2, 10, 10)], ['grade'])
        self.assertEqual(pipeline.select_jobs([job('structure', 'implementation')],
            [], 0, 4, 1, 10, 10), [])

    def test_structural_proposal_is_enqueued_once_after_planner_session_exists(self):
        import codesign_lab.search.pipeline as controller_module
        instance = pipeline.Pipeline.__new__(pipeline.Pipeline)
        instance.args = type('Args', (), {'implementation_enabled': True,
            'implementation_max_proposals': 2, 'implementation_model': 'gpt-6-astra',
            'implementation_effort': 'medium', 'implementation_min_case_gain': .002,
            'implementation_min_score_gain': 100})()
        instance.out = Path('/tmp/structure-campaign')
        instance.python = '/usr/bin/python3'
        instance.pool = type('Pool', (), {'state': {'analysis': {'lanes': {}}}})()
        instance.pending, instance.active, instance.seen = [], {}, set()
        def enqueue(item):
            instance.pending.append(item)
            instance.seen.add(item['key'])
        instance.enqueue = enqueue
        proposals = [('first', {'id': 'first'}), ('second', {'id': 'second'})]
        with tempfile.TemporaryDirectory() as directory, \
             patch('codesign_lab.search.implementation.proposals', return_value=proposals), \
             patch.object(controller_module, 'ROOT', Path(directory)):
            instance.process_implementations()
            self.assertEqual(instance.pending, [])
            instance.pool.state['analysis']['lanes']['global'] = {'session_id': 'planner'}
            instance.process_implementations()
            instance.process_implementations()
        self.assertEqual([item['proposal_id'] for item in instance.pending], ['first', 'second'])
        self.assertTrue(all(item['timeout'] == 21600 for item in instance.pending))

    def test_interrupted_structure_is_recorded_and_sent_to_global_analysis(self):
        import codesign_lab.search.pipeline as controller_module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'workspace/implementation-loop/campaign/first/state.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'status': 'CODING', 'proposal': {'case': 'M1_P1'}}))
            instance = pipeline.Pipeline.__new__(pipeline.Pipeline)
            instance.out = root / 'workspace/pipeline/campaign'
            instance.done = []
            instance.triggers = Mock()
            instance.pool = Mock()
            instance.budget = Mock()
            with patch.object(controller_module, 'ROOT', root):
                instance.completed({'stage': 'implementation', 'proposal_id': 'first',
                    'budget_key': 'structure-budget'},
                    {'status': 'infrastructure_failed', 'error': 'worker lost'})
            self.assertEqual(json.loads(path.read_text())['status'], 'FAILED')
            observation = instance.triggers.observe.call_args.args[1]
            self.assertEqual(observation['proposal_status'], 'FAILED')
            self.assertEqual(observation['case'], 'M1_P1')
            instance.budget.reused.assert_called_once_with('structure-budget')

    def test_structure_not_started_by_budget_limit_is_not_design_failure(self):
        import codesign_lab.search.pipeline as controller_module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instance = pipeline.Pipeline.__new__(pipeline.Pipeline)
            instance.out = root / 'workspace/pipeline/campaign'
            instance.done = []
            instance.triggers = Mock()
            instance.pool = Mock()
            with patch.object(controller_module, 'ROOT', root):
                instance.completed({'stage': 'implementation', 'proposal_id': 'first'},
                    {'status': 'budget_exhausted'})
            path = root / 'workspace/implementation-loop/campaign/first/state.json'
            self.assertEqual(json.loads(path.read_text())['status'], 'BUDGET_EXHAUSTED')
            instance.triggers.observe.assert_not_called()

    def test_structure_keeps_full_budget_after_official_attempt(self):
        import codesign_lab.search.pipeline as controller_module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'workspace/implementation-loop/campaign/first/state.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'status': 'FAILED', 'official_attempted': True,
                'proposal': {'case': 'M1_P1'}}))
            instance = pipeline.Pipeline.__new__(pipeline.Pipeline)
            instance.out = root / 'workspace/pipeline/campaign'
            instance.done = []
            instance.triggers, instance.pool, instance.budget = Mock(), Mock(), Mock()
            with patch.object(controller_module, 'ROOT', root):
                instance.completed({'stage': 'implementation', 'proposal_id': 'first',
                    'budget_key': 'structure-budget'}, {'status': 'completed'})
            instance.budget.reused.assert_not_called()

    def test_failed_build_never_releases_evaluation(self):
        instance = pipeline.Pipeline.__new__(pipeline.Pipeline)
        instance.done = []
        instance.evaluate_candidate = lambda unused: self.fail('失败构建不能进入评估')
        instance.completed(job('failed', 'build'), {'status': 'failed'})
        self.assertEqual(len(instance.done), 1)


if __name__ == '__main__':
    unittest.main()
