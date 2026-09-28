"""目标扩展再次完成时触发一次，新结果不被旧确认吞掉。"""
import unittest
from codesign_lab.search.triggers import Triggers


class CompletionRevisionChecks(unittest.TestCase):
    def target(self):
        return {'t':{'definition':{'lane':'lane'},'status':'DONE','candidates':{
            'a':{'status':'OBSERVED','case_keys':['case-a']}}}}

    def test_expanded_completion_has_new_decision_once(self):
        state={};trigger=Triggers(state,cooldown=0);targets=self.target()
        first=trigger.poll(targets,0,now=100)[0]
        trigger.acknowledge('lane',first['decision_id'],now=100)
        targets['t']['candidates']['b']={'status':'OBSERVED','case_keys':['case-b']}
        second=trigger.poll(targets,0,now=101)[0]
        self.assertNotEqual(first['decision_id'],second['decision_id'])
        self.assertEqual(second['target_ids'],['t'])
        trigger.acknowledge('lane',second['decision_id'],now=101)
        self.assertEqual(Triggers(state,cooldown=0).poll(targets,0,now=102),[])

    def test_running_analysis_cannot_swallow_later_completion(self):
        state={};trigger=Triggers(state,cooldown=0);targets=self.target()
        first=trigger.poll(targets,0,now=100)[0]
        trigger.lane('lane')['running_decision']=first['decision_id']
        targets['t']['candidates']['b']={'status':'OBSERVED','case_keys':['case-b']}
        self.assertEqual(trigger.poll(targets,0,now=101),[])
        trigger.acknowledge('lane',first['decision_id'],now=102)
        trigger.lane('lane').pop('running_decision')
        self.assertEqual(trigger.poll(targets,0,now=103)[0]['target_ids'],['t'])

    def test_same_completion_does_not_repeat_after_restart(self):
        state={};trigger=Triggers(state,cooldown=0);targets=self.target()
        first=trigger.poll(targets,0,now=100)[0]
        trigger.acknowledge('lane',first['decision_id'],now=100)
        targets['t']['definition']['max_trials']=50
        self.assertEqual(Triggers(state,cooldown=0).poll(targets,0,now=101),[])

    def test_legacy_ack_is_migrated_without_fake_new_event(self):
        state={};trigger=Triggers(state,cooldown=0)
        trigger.lane('lane')['completed_targets']=['t']
        self.assertEqual(trigger.poll(self.target(),0,now=100),[])
