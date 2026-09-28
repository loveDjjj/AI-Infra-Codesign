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

    def test_failed_build_never_releases_evaluation(self):
        instance = pipeline.Pipeline.__new__(pipeline.Pipeline)
        instance.done = []
        instance.evaluate_candidate = lambda unused: self.fail('失败构建不能进入评估')
        instance.completed(job('failed', 'build'), {'status': 'failed'})
        self.assertEqual(len(instance.done), 1)


if __name__ == '__main__':
    unittest.main()
