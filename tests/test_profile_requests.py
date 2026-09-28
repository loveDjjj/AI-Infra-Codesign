"""请求阶段跟踪必须有真实性能证据，结果触发不重复训练样本。"""
import tempfile
import unittest
from pathlib import Path
from codesign_lab.config import ROOT
from codesign_lab.search.profiles import inputs
from codesign_lab.search.triggers import Triggers
from codesign_lab.search.budget import Budget
from codesign_lab.search.scheduler import atomic_json


class ProfileRequestChecks(unittest.TestCase):
    def test_actual_legacy_release_record_can_locate_verified_input(self):
        from codesign_lab.records import read
        record=next(record for record in read() if record['id']=='joint28_v072_w2_owner_powerfix')
        candidate,_=inputs(record,'M2_D1')
        self.assertEqual(candidate,ROOT/'data/releases/joint28')

    def test_real_released_input_matches_frozen_report(self):
        record={'candidate':'data/releases/joint28','report':'data/releases/joint28/local-grade.json','config':{'present':True}}
        candidate,report=inputs(record,'M2_D1')
        self.assertEqual(candidate,ROOT/'data/releases/joint28')
        self.assertTrue(report.is_file())

    def test_functional_failure_cannot_be_profiled(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);report=root/'report.json'
            atomic_json(report,{'cases':{'M1_P1':{'functional_passed':False,'timing':{'cycles':1}}}})
            with self.assertRaises(ValueError):inputs({'candidate':str(root),'report':str(report),'config':{'ok':True}},'M1_P1')

    def test_profile_completion_triggers_once_without_performance_observation(self):
        state={};trigger=Triggers(state,cooldown=0)
        trigger.profile_ready('lane','profile-1',{'record_id':'record','cycles':10})
        request=trigger.poll({},0,now=100)[0]
        self.assertEqual(request['reasons'],['profile_ready'])
        self.assertEqual(request['observation_ids'],[])
        self.assertEqual(request['profile_ids'],['profile-1'])
        trigger.acknowledge('lane',request['decision_id'],now=100)
        self.assertEqual(Triggers(state,cooldown=0).poll({},0,now=101),[])

    def test_profile_budget_is_separate_and_bounded(self):
        budget=Budget({},wall_seconds=100,profile_calls=1)
        self.assertTrue(budget.reserve('profile-1','profile'))
        self.assertFalse(budget.reserve('profile-2','profile'))
        self.assertTrue(budget.reserve('case-1','case'))
        self.assertEqual(budget.used('profile'),1)
