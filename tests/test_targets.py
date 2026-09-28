"""验证动态请求、预算与冻结起点，而非只校验字段存在。"""
import copy
import errno
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codesign_lab.config import ROOT
from codesign_lab.search.targets import TargetPool, inject, validate_target
from codesign_lab.evaluation.cache import ResultCache


class TargetsChecks(unittest.TestCase):
    def setUp(self):
        self.records = [{'id': 'base', 'config': {'hardware': {'cache_mib': 0}, 'programs': {}}}]
        self.target = {'schema_version': 1, 'target_id': 'attention', 'lane': 'p1_attention',
            'hypothesis': '比较查询分块', 'base_record': 'base', 'source_epoch': 'epoch-a',
            'cases': ['M1_P1'], 'variables': {'programs.M1_P1.config.attention_query_tile': [8, 16]},
            'sampler': 'enumerate', 'max_trials': 2, 'priority': .8, 'evidence_ids': ['base']}

    def test_unsupported_variable_or_value_never_enters_pool(self):
        for variables in [{'programs.M1_P1.config.w2_preload_k': [128]},
                          {'programs.M2_D1.config.vector_tile': [32]},
                          {'hardware.cache_mib': [1]}]:
            target = copy.deepcopy(self.target);target['variables'] = variables
            with self.assertRaises(ValueError):
                validate_target(target, 'epoch-a', self.records)

    def test_cleaned_or_composite_base_cannot_use_current_generator(self):
        record=copy.deepcopy(self.records[0])
        record['source_available']=False
        with self.assertRaisesRegex(ValueError,'历史源码已清理'):
            validate_target(self.target,'epoch-a',[record])
        record.pop('source_available')
        record['reproduction']='verified_composite'
        with self.assertRaisesRegex(ValueError,'跨源码组合'):
            validate_target(self.target,'epoch-a',[record])

    def test_injected_request_survives_restart_and_duplicate_is_idempotent(self):
        root = ROOT / 'workspace/pipeline';root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory, patch('codesign_lab.search.targets.read', return_value=self.records):
            path = Path(directory)
            inject(path, {'op': 'add', 'target': self.target}, 'request-1')
            pool = TargetPool(path, 'epoch-a', max_proposals=4)
            self.assertEqual(pool.consume()[0]['status'], 'accepted')
            inject(path, {'op': 'add', 'target': self.target}, 'request-1')
            restored = TargetPool(path, 'epoch-a', max_proposals=4)
            self.assertEqual(restored.consume()[0]['status'], 'accepted')
            self.assertEqual(len(restored.state['targets']), 1)
            self.records[0]['config']['hardware']['cache_mib'] = 1
            self.assertEqual(restored.state['targets']['attention']['base_config']['hardware']['cache_mib'], 0)

    def test_budget_and_epoch_cannot_be_bypassed(self):
        with tempfile.TemporaryDirectory() as directory, patch('codesign_lab.search.targets.read', return_value=self.records):
            pool = TargetPool(directory, 'epoch-a', max_proposals=2)
            self.assertEqual(pool.apply({'request_id': '1', 'command': {'op': 'add', 'target': self.target}})['status'], 'accepted')
            other = copy.deepcopy(self.target);other['target_id'] = 'other'
            self.assertEqual(pool.apply({'request_id': '2', 'command': {'op': 'add', 'target': other}})['status'], 'rejected')
            with self.assertRaises(ValueError):
                TargetPool(directory, 'epoch-b')

    def test_lock_contention_retries_but_unrelated_error_propagates(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = ResultCache(directory, {'engine': 'test'}, lock_timeout=.01)
            with patch('codesign_lab.evaluation.cache.fcntl.flock', side_effect=[BlockingIOError(errno.EAGAIN, 'busy'), None]), patch('codesign_lab.evaluation.cache.time.sleep'):
                self.assertEqual(cache.call('timing', {}, lambda: {'cycles': 10})[0], {'cycles': 10})
            with patch('codesign_lab.evaluation.cache.fcntl.flock', side_effect=OSError(errno.EIO, 'io')):
                with self.assertRaises(OSError):
                    cache.call('timing', {}, lambda: self.fail('锁失败不能冷计算'))

    def test_lock_wait_has_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = ResultCache(directory, {}, lock_timeout=.02)
            with patch('codesign_lab.evaluation.cache.fcntl.flock', side_effect=BlockingIOError(errno.EAGAIN, 'busy')):
                with self.assertRaises(TimeoutError):
                    cache.call('timing', {}, lambda: self.fail('锁超时不能冷计算'))


if __name__ == '__main__':
    unittest.main()
