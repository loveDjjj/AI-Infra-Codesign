"""采样恢复与反馈分类，不把复用/失败变成伪性能。"""
import copy
import unittest
from codesign_lab.search.samplers import FiniteSampler


class SamplerFeedbackChecks(unittest.TestCase):
    def setUp(self):
        self.base={'value':0};self.variables={'value':[1,2,3,4]}
        self.state={};self.sampler=FiniteSampler(self.base,self.variables,sampler='random',seed=42,state=self.state)

    def observation(self, **changes):
        return {'id':'obs','case':'M2_D1','hardware_hash':'hw','functional_passed':True,
                'cycles':100,'peak_power_w':15}|changes

    def test_ask_resume_never_repeats_pending_point(self):
        first=self.sampler.ask()
        restored=FiniteSampler(self.base,self.variables,sampler='random',seed=42,state=copy.deepcopy(self.state))
        points=[first]
        while (point:=restored.ask()) is not None:points.append(point)
        self.assertEqual(len({identifier for identifier,_ in points}),4)
        with self.assertRaises(ValueError):FiniteSampler(self.base,self.variables,sampler='random',seed=43,state=self.state)

    def test_infrastructure_cache_and_functional_failures_have_no_performance(self):
        identifier,_=self.sampler.ask()
        for i,(change,status) in enumerate([({'cache_reused':True},'reused'),
                ({'failure_kind':'infrastructure'},'infrastructure_failed'),
                ({'functional_passed':False},'functional_failed'),({'cycles':float('nan')},'no_performance')]):
            self.assertEqual(self.sampler.tell(identifier,self.observation(id=f'obs{i}',**change)),status)
        self.assertEqual(self.state['observations'],[])

    def test_power_failure_keeps_real_values_and_receipt_deduplicates(self):
        identifier,_=self.sampler.ask();obs=self.observation(peak_power_w=21)
        self.assertEqual(self.sampler.tell(identifier,obs),'power_failed')
        self.assertEqual(self.sampler.tell(identifier,obs),'power_failed')
        self.assertEqual(len(self.state['observations']),1)
        self.assertEqual(self.state['observations'][0]['cycles'],100)
        self.assertEqual(self.state['observations'][0]['peak_power_w'],21)
        with self.assertRaises(ValueError):self.sampler.tell('unknown',obs)

    def test_external_proposal_is_skipped_by_next_ask(self):
        identifier,config=self.sampler.ask()
        other=FiniteSampler(self.base,self.variables,sampler='random',seed=42)
        other.remember(identifier,config)
        self.assertNotEqual(other.ask()[0],identifier)
