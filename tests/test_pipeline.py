"""验证官方验收优先、预留容量与资源准入。"""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('rolling_pipeline', Path(__file__).resolve().parents[1] / 'scripts/pipeline.py')
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


def job(name, stage='case', memory=1):
    return {'key': name, 'stage': stage, 'memory_bytes': memory}


class PipelineChecks(unittest.TestCase):
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
