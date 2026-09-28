"""失败尝试不会伪造观测，重试次数与冷预算均有界。"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from codesign_lab.search.pipeline import Pipeline
from codesign_lab.search.budget import Budget
from codesign_lab.search.scheduler import atomic_json


class RetryChecks(unittest.TestCase):
    def controller(self, root):
        controller=Pipeline.__new__(Pipeline)
        controller.out=root;controller.pending=[]
        controller.pool=SimpleNamespace(state={'jobs':{},'targets':{}},save=Mock())
        controller.args=SimpleNamespace(max_retries=1)
        controller.budget=Budget(controller.pool.state,wall_seconds=100,case_calls=2)
        controller.completed=Mock()
        return controller

    def test_timeout_retries_once_then_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            controller=self.controller(Path(directory))
            job={'key':'case-1','stage':'case'}
            result={'status':'timeout','wall_seconds':1}
            controller.finish_attempt(job,result)
            self.assertEqual(controller.pending[0]['attempt'],2)
            controller.completed.assert_not_called()
            self.assertFalse((controller.out/'jobs/case-1.result.json').exists())
            controller.finish_attempt(controller.pending.pop(),result)
            controller.completed.assert_called_once()
            self.assertEqual(len(controller.pool.state['jobs']['case-1']['attempts']),2)

    def test_design_failure_does_not_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            controller=self.controller(Path(directory))
            report=Path(directory)/'report.json'
            atomic_json(report,{'cases':{'M1_P1':{'functional_passed':False}}})
            controller.finish_attempt({'key':'case-1','stage':'case','case':'M1_P1','report':report},
                                      {'status':'timeout','wall_seconds':1})
            self.assertEqual(controller.pending,[])
            controller.completed.assert_called_once()

    def test_positive_exit_error_not_assumed_transient(self):
        with tempfile.TemporaryDirectory() as directory:
            controller=self.controller(Path(directory))
            controller.finish_attempt({'key':'verify-1','stage':'verify'},
                                      {'status':'failed','exit_code':1,'wall_seconds':1})
            self.assertEqual(controller.pending,[])

    def test_retry_cost_counts_separately(self):
        budget=Budget({},wall_seconds=100,case_calls=2)
        self.assertTrue(budget.reserve('case-1','case'))
        self.assertTrue(budget.reserve('case-1-attempt2','case'))
        self.assertFalse(budget.reserve('case-1-attempt3','case'))
        self.assertEqual(budget.used('case'),2)

    def test_stopped_target_does_not_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            controller=self.controller(Path(directory))
            controller.finish_attempt({'key':'build-1','stage':'build','target_owned':True},
                                      {'status':'infrastructure_failed','wall_seconds':1})
            self.assertEqual(controller.pending,[])
