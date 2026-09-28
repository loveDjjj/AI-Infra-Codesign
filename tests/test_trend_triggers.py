"""改善和停滞必须建立在可比较的合格观测上。"""
import unittest
from codesign_lab.search.triggers import Triggers


class TrendTriggerChecks(unittest.TestCase):
    def controller(self):
        state={};trigger=Triggers(state,batch_size=100,low_watermark=0,stagnation_trials=3,cooldown=0)
        targets={'t':{'definition':{'lane':'lane'},'status':'ACTIVE'}}
        return state,trigger,targets

    def observe(self, trigger, identifier, cycles, **fields):
        trigger.observe('lane',dict(id=identifier,cycles=cycles,functional_passed=True,
            peak_power_w=19,hardware_hash='hw',case='M1_P1',**fields))

    def test_new_eligible_best_triggers_improvement(self):
        _,trigger,targets=self.controller()
        self.observe(trigger,'a',1000);self.observe(trigger,'b',990)
        self.assertEqual(trigger.poll(targets,10,now=100)[0]['reasons'],['improvement'])

    def test_other_hardware_and_case_are_not_comparable(self):
        _,trigger,targets=self.controller()
        for identifier,hw,case,cycles in [('a','h1','M1_P1',1000),('b','h2','M1_P1',900),('c','h1','M2_D1',100)]:
            trigger.observe('lane',{'id':identifier,'cycles':cycles,'functional_passed':True,
                                  'peak_power_w':19,'hardware_hash':hw,'case':case})
        self.assertEqual(trigger.poll(targets,10,now=100),[])

    def test_flat_window_stagnates_and_ack_prevents_repeat(self):
        state,trigger,targets=self.controller()
        for i,cycles in enumerate([1000,1001,1002]):self.observe(trigger,str(i),cycles)
        request=trigger.poll(targets,10,now=100)[0]
        self.assertEqual(request['reasons'],['stagnation'])
        trigger.acknowledge('lane',request['decision_id'],now=100)
        restored=Triggers(state,batch_size=100,low_watermark=0,stagnation_trials=3,cooldown=0)
        self.assertEqual(restored.poll(targets,10,now=101),[])

    def test_significant_gain_in_window_prevents_stagnation(self):
        _,trigger,targets=self.controller()
        for i,cycles in enumerate([1000,990,991]):self.observe(trigger,str(i),cycles)
        reasons=trigger.poll(targets,10,now=100)[0]['reasons']
        self.assertIn('improvement',reasons);self.assertNotIn('stagnation',reasons)

    def test_power_failure_cannot_create_improvement(self):
        _,trigger,targets=self.controller()
        self.observe(trigger,'a',1000)
        trigger.observe('lane',{'id':'power','cycles':900,'functional_passed':True,
                              'peak_power_w':21,'hardware_hash':'hw','case':'M1_P1'})
        self.assertEqual(trigger.poll(targets,10,now=100),[])

    def test_cached_gain_cannot_create_improvement(self):
        _,trigger,targets=self.controller()
        self.observe(trigger,'a',1000);self.observe(trigger,'cached',900,cache_reused=True)
        self.assertEqual(trigger.poll(targets,10,now=100),[])

    def test_invalid_threshold_rejected(self):
        with self.assertRaises(ValueError):Triggers({},failure_window=2,failure_threshold=3)
