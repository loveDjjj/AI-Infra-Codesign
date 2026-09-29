"""实现族有限种子与无结果供给请求的核心回归。"""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from codesign_lab.search.families import configurations, validate_manifest, expansion_signal
from codesign_lab.search.supply import review
from codesign_lab.search.triggers import Triggers


class FamilySupplyChecks(unittest.TestCase):
    def setUp(self):
        self.base = {'hardware': {'sm_count': 16}, 'programs': {
            'M1_P1': {'config': {'w2_n_shards': 8, 'w2_k_shards': 4}},
            'M2_D1': {'config': {'cold_group': 4}}}}
        self.manifest = {'schema_version': 1, 'family_id': 'w2-nk-v1',
            'mechanism_id': 'w2_nk_rolling', 'case': 'M1_P1', 'operator': 'prompt.W2',
            'base_record_id': 'research-w2', 'registered_source_sha256': 'source-hash',
            'variables': {'programs.M1_P1.config.w2_n_shards': [8, 16],
                          'programs.M1_P1.config.w2_k_shards': [2, 4]},
            'seed_variants': [
                {'programs.M1_P1.config.w2_n_shards': 16,
                 'programs.M1_P1.config.w2_k_shards': 2},
                {'programs.M1_P1.config.w2_n_shards': 16,
                 'programs.M1_P1.config.w2_k_shards': 4}],
            'critical_checks': ['rf_liveness', 'partial_dependencies'],
            'stop_if': '单案关键路径没有改善'}

    def test_family_uses_only_declared_tuples(self):
        validate_manifest(self.manifest, base_config=self.base, source_sha256='source-hash',
            case='M1_P1', base_record_id='research-w2')
        configs = [config for _, config in configurations(self.base, self.manifest['seed_variants'])]
        self.assertEqual(len(configs), 2)
        self.assertEqual([(c['programs']['M1_P1']['config']['w2_n_shards'],
                           c['programs']['M1_P1']['config']['w2_k_shards']) for c in configs],
                         [(16, 2), (16, 4)])
        self.assertEqual(self.base['programs']['M1_P1']['config'],
                         {'w2_n_shards': 8, 'w2_k_shards': 4})

    def test_family_rejects_other_case_and_forged_source(self):
        invalid = copy.deepcopy(self.manifest)
        invalid['variables'] = {'programs.M2_D1.config.cold_group': [4, 8]}
        invalid['seed_variants'] = [{'programs.M2_D1.config.cold_group': 8}]
        with self.assertRaisesRegex(ValueError, '受影响案例'):
            validate_manifest(invalid, base_config=self.base, source_sha256='source-hash',
                case='M1_P1', base_record_id='research-w2')
        with self.assertRaisesRegex(ValueError, '冻结源码'):
            validate_manifest(self.manifest, base_config=self.base, source_sha256='other',
                case='M1_P1', base_record_id='research-w2')

    def test_seed_budget_and_joint_hardware_domain(self):
        manifest = copy.deepcopy(self.manifest)
        manifest['candidate_budget'] = 3
        manifest['sampler'] = 'random'
        validate_manifest(manifest, base_config=self.base, source_sha256='source-hash',
            case='M1_P1', base_record_id='research-w2')
        manifest['candidate_budget'] = 4
        with self.assertRaisesRegex(ValueError, '候选预算'):
            validate_manifest(manifest, base_config=self.base, source_sha256='source-hash',
                case='M1_P1', base_record_id='research-w2')
        joint = copy.deepcopy(self.manifest)
        joint['case'] = 'both'
        joint['variables'] = {'hardware.sm_count': [16, 24],
                              'programs.M2_D1.config.cold_group': [4, 8]}
        joint['seed_variants'] = [{'hardware.sm_count': 24,
                                  'programs.M2_D1.config.cold_group': 4}]
        validate_manifest(joint, base_config=self.base, source_sha256='source-hash',
            case='both', base_record_id='research-w2')

    def test_pilot_expands_only_with_useful_cycles(self):
        self.assertTrue(expansion_signal(base_cycles=400000, global_cycles=390000,
            pilot_cycles=[388000], threshold=.002))
        self.assertFalse(expansion_signal(base_cycles=400000, global_cycles=390000,
            pilot_cycles=[399000], threshold=.002))

    def test_supply_without_new_observation_is_idempotent_and_backed_off(self):
        state = {'targets': {}, 'applied_decisions': {}, 'analysis': {'lanes': {}}}
        budget = Mock()
        budget.snapshot.return_value = {'remaining_wall_seconds': 3600,
            'used': {'case': 0, 'full': 0, 'ai': 0, 'profile': 0},
            'limits': {'case': 10, 'full': 2, 'ai': 4, 'profile': 1}}
        kwargs = dict(pending=[], active={}, completed=[], budget=budget, workers=8,
                      source_epoch='fixed-source')
        first = review(state, now=1000, **kwargs)
        self.assertEqual(first['ready_tasks'], 0)
        self.assertIsNone(review(state, now=1001, **kwargs))
        trigger = Triggers(state)
        trigger.global_only = True
        requests = trigger.poll({}, 0, now=1001, supply=first)
        self.assertEqual(len(requests), 1)
        self.assertIn('supply_low', requests[0]['reasons'])
        state['supply']['last_request_id'] = requests[0]['decision_id']
        state['applied_decisions'][requests[0]['decision_id']] = {'decision': {
            'new_targets': [], 'implementation_proposals': [], 'profile_requests': []}}
        trigger.acknowledge('global', requests[0]['decision_id'], now=1002)
        self.assertIsNone(review(state, now=1002, **kwargs))
        second = review(state, now=1603, **kwargs)
        self.assertIsNotNone(second)
        state['applied_decisions'][second['decision_id']] = {'decision': {
            'new_targets': [], 'implementation_proposals': [], 'profile_requests': []}}
        self.assertIsNone(review(state, now=2800, **kwargs))
        self.assertIn('连续两次', state['supply']['blocked_reason'])


if __name__ == '__main__':
    unittest.main()
