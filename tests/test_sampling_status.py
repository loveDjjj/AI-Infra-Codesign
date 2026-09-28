"""看板采样统计不混淆缓存反馈、提案来源和独立观测。"""
import unittest
from codesign_lab.search.status import target_summary


class SamplingStatusChecks(unittest.TestCase):
    def test_distinguishes_pending_feedback_and_actual_observations(self):
        entry={'status':'ACTIVE','definition':{'lane':'d1','hypothesis':'h','sampler':'tpe','max_trials':4},
            'trials_launched':3,'trials_completed':2,'adaptive':{'pending_jobs':['sample'], 'trials':{
                'a':{'number':0,'origin':'tpe','told':True,'outcome':'no_performance'},
                'b':{'number':1,'origin':'tpe','told':True,'outcome':'power_failed'},
                'c':{'number':2,'origin':'enumeration_fallback'}},
                'unconsumed_proposals':[{'proposal':{'number':2}}]}}
        summary=target_summary('t',entry)
        self.assertEqual(summary['feedback_pending'],1)
        self.assertEqual(summary['independent_observations'],1)
        self.assertEqual(summary['origins'],{'tpe':2,'enumeration_fallback':1})
        self.assertEqual(summary['sampling_jobs_pending'],1)

    def test_legacy_target_does_not_require_adaptive_state(self):
        entry={'status':'DONE','definition':{'lane':'p1','hypothesis':'h'},
               'sampler_state':{'observations':{'id':{}}}}
        summary=target_summary('old',entry)
        self.assertEqual(summary['sampler'],'enumerate')
        self.assertEqual(summary['feedback_pending'],0)
        self.assertEqual(summary['independent_observations'],1)

if __name__=='__main__':unittest.main()
