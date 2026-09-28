"""验证触发游标、事件合并与低水位不重复循环。"""
import unittest
from codesign_lab.search.triggers import Triggers


class TriggerChecks(unittest.TestCase):
    def test_structural_result_triggers_global_review_while_other_work_remains(self):
        state = {};triggers = Triggers(state, global_only=True, batch_size=8, low_watermark=2)
        targets = {'t': {'definition': {'lane': 'p1_w2'}, 'status': 'ACTIVE'}}
        triggers.observe('global', {'id': 'implementation-p1', 'observation_kind': 'implementation',
            'proposal_status': 'REJECTED', 'case': 'M1_P1'})
        request = triggers.poll(targets, 20, now=100)[0]
        self.assertIn('implementation_result', request['reasons'])
        self.assertEqual(request['observation_ids'], ['implementation-p1'])

    def test_completion_survives_resume_and_ack_stops_retrigger(self):
        state = {};targets = {'t': {'definition': {'lane': 'p1_attention'}, 'status': 'DONE'}}
        first = Triggers(state)
        requests = first.poll(targets, 0, now=100)
        self.assertEqual(len(requests), 1)
        restored = Triggers(state)
        self.assertEqual(restored.poll(targets, 0, now=101)[0]['decision_id'], requests[0]['decision_id'])
        restored.acknowledge('p1_attention', requests[0]['decision_id'], now=101)
        self.assertEqual(restored.poll(targets, 0, now=500), [])

    def test_reuse_and_infrastructure_failure_are_not_new_observations(self):
        state = {};triggers = Triggers(state, batch_size=1)
        targets = {'t': {'definition': {'lane': 'p1_attention'}, 'status': 'ACTIVE'}}
        for row in [{'id': 'cached', 'cache_reused': True}, {'id': 'infra', 'failure_kind': 'infrastructure'}]:
            triggers.observe('p1_attention', row)
        self.assertEqual(triggers.poll(targets, 0, now=100), [])
        triggers.observe('p1_attention', {'id': 'real', 'cycles': 10, 'functional_passed': True})
        triggers.observe('p1_attention', {'id': 'real', 'cycles': 10, 'functional_passed': True})
        request = triggers.poll(targets, 0, now=100)[0]
        self.assertEqual(request['observation_ids'], ['real'])
        self.assertEqual(set(request['reasons']), {'batch', 'low_watermark'})

    def test_wrong_ack_and_cooldown_cannot_lose_new_evidence(self):
        state = {};triggers = Triggers(state, batch_size=1, cooldown=120)
        targets = {'t': {'definition': {'lane': 'd1_decode'}, 'status': 'ACTIVE'}}
        triggers.observe('d1_decode', {'id': 'first'})
        request = triggers.poll(targets, 0, now=100)[0]
        with self.assertRaises(ValueError):
            triggers.acknowledge('d1_decode', 'wrong', now=100)
        triggers.acknowledge('d1_decode', request['decision_id'], now=100)
        triggers.observe('d1_decode', {'id': 'second'})
        self.assertEqual(triggers.poll(targets, 0, now=101), [])
        self.assertEqual(triggers.poll(targets, 0, now=221)[0]['observation_ids'], ['second'])
