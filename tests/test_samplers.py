"""验证随机不放回、恢复前缀及旧枚举兼容。"""
import unittest
from codesign_lab.search.samplers import candidates, target_seed
from codesign_lab.search.space import candidates as old_candidates
from codesign_lab.search.targets import validate_target


class SamplerChecks(unittest.TestCase):
    def setUp(self):
        self.base = {'a': {'x': 0, 'y': 0}}
        self.variables = {'a.x': list(range(7)), 'a.y': list(range(5))}

    def test_random_unique_complete_and_prefix_stable(self):
        short = list(candidates(self.base, self.variables, 9, sampler='random', seed=42))
        full = list(candidates(self.base, self.variables, 100, sampler='random', seed=42))
        self.assertEqual(short, full[:9])
        self.assertEqual(len(full), 35)
        self.assertEqual(len({key for key, _ in full}), 35)
        self.assertNotEqual(short, list(candidates(self.base, self.variables, 9, sampler='random', seed=43)))
        self.assertEqual(self.base['a'], {'x': 0, 'y': 0})

    def test_enumeration_preserves_existing_hashes_and_order(self):
        self.assertEqual(list(candidates(self.base, self.variables, 35)),
                         list(old_candidates(self.base, self.variables, 35)))

    def test_random_ignores_variable_field_order(self):
        reordered = dict(reversed(list(self.variables.items())))
        self.assertEqual(list(candidates(self.base, self.variables, 20, sampler='random', seed=3)),
                         list(candidates(self.base, reordered, 20, sampler='random', seed=3)))

    def test_target_seed_and_validation(self):
        target = {'schema_version': 1, 'target_id': 'random-proof', 'lane': 'p1_attention',
            'hypothesis': '随机选择合法分块', 'base_record': 'base', 'source_epoch': 'epoch',
            'cases': ['M1_P1'], 'variables': {'programs.M1_P1.config.attention_query_tile': [8,16,32]},
            'sampler': 'random', 'max_trials': 3, 'priority': .5, 'evidence_ids': ['base'], 'seed': 42}
        records = [{'id': 'base', 'config': self.base}]
        self.assertEqual(validate_target(target, 'epoch', records)[0]['seed'], 42)
        self.assertEqual(target_seed(target), 42)
        target.pop('seed')
        self.assertEqual(target_seed(target), target_seed(dict(target)))
        for seed in [True, -1, 2**64, '42']:
            with self.assertRaises(ValueError):
                validate_target(target | {'seed': seed}, 'epoch', records)
