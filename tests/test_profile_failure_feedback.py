"""诊断失败要反馈原因，但不能伪造成性能点或阻塞其他任务。"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from codesign_lab.search.pipeline import Pipeline
from codesign_lab.search.triggers import Triggers
from codesign_lab.search.scheduler import atomic_json


class ProfileFailureFeedbackChecks(unittest.TestCase):
    def controller(self):
        controller=Pipeline.__new__(Pipeline);controller.done=[]
        state={'profiles':{'p':{'record_id':'record','case':'M2_D1','status':'QUEUED',
            'lane':'one','lanes':['one','two']}}}
        controller.pool=SimpleNamespace(state=state,save=Mock())
        controller.triggers=Triggers(state,cooldown=0)
        return controller

    def test_budget_failure_notifies_all_subscribers_without_cycles(self):
        controller=self.controller()
        controller.completed({'stage':'profile','profile_id':'p'},
                             {'status':'budget_exhausted','wall_seconds':0})
        requests=controller.triggers.poll({},0,now=100)
        self.assertEqual({request['lane'] for request in requests},{'one','two'})
        self.assertTrue(all(request['reasons']==['profile_failed'] for request in requests))
        for lane in ['one','two']:
            summary=controller.triggers.lane(lane)['profiles']['p']
            self.assertNotIn('cycles',summary)
            self.assertEqual(summary['stage'],'profile')

    def test_invalid_completed_trace_is_isolated_not_registered_as_success(self):
        controller=self.controller()
        with tempfile.TemporaryDirectory() as directory:
            report=Path(directory)/'trace.json';atomic_json(report,{'complete_timing_identical':False})
            controller.completed({'stage':'profile','profile_id':'p','report':report},
                                 {'status':'completed'})
        self.assertEqual(controller.pool.state['profiles']['p']['status'],'FAILED')
        self.assertEqual(controller.triggers.poll({},0,now=100)[0]['reasons'],['profile_failed'])

    def test_failed_diagnostic_ack_is_not_repeated_after_restart(self):
        controller=self.controller()
        controller.completed({'stage':'profile_build','profile_id':'p'},{'status':'failed'})
        requests=controller.triggers.poll({},0,now=100)
        for request in requests:controller.triggers.acknowledge(request['lane'],request['decision_id'],now=100)
        self.assertEqual(Triggers(controller.pool.state,cooldown=0).poll({},0,now=101),[])
