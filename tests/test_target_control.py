"""目标优先级与剩余预算作用于真实队列，不撤销已启动成本。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch
import tempfile
from pathlib import Path
from codesign_lab.search.target_control import priorities,started_candidates
from codesign_lab.search.pipeline import Pipeline


class TargetControlChecks(unittest.TestCase):
    def test_expansion_preserves_started_outside_enumeration_prefix(self):
        entry=self.entry();entry.update(expand=True,trials_launched=3,trials_completed=2,
            base_config={'value':0})
        entry['definition'].update(variables={'value':[1,2,3]},cases=['M1_P1'])
        for identity in ['a','b']:
            entry['candidates'][identity].update(status='CANCELLED',cancel_reason='目标剩余预算缩减')
        with tempfile.TemporaryDirectory() as directory:
            controller=Pipeline.__new__(Pipeline);controller.out=Path(directory);controller.python='python'
            controller.pending=[];controller.enqueue=Mock();controller.persist_job=Mock()
            controller.pool=SimpleNamespace(state={'targets':{'t':entry},'jobs':{'build-c':{'status':'RUNNING'}}},
                consume=Mock(),save=Mock(),max_proposals=5)
            controller.triggers=SimpleNamespace(poll=Mock(return_value=[]))
            with patch('codesign_lab.search.samplers.candidates',return_value=[('a',{'value':1}),('b',{'value':2}),('c',{'value':3})]), \
                 patch('codesign_lab.search.pipeline.reject',return_value=None):
                controller.process_targets()
            self.assertEqual(entry['candidates']['a']['status'],'BUILDING')
            self.assertEqual(entry['candidates']['b']['status'],'CANCELLED')
            self.assertEqual(entry['candidates']['c']['status'],'BUILDING')
            self.assertEqual(entry['trials_launched'],3)

    def entry(self,priority=.5):
        return {'status':'ACTIVE','definition':{'priority':priority,'max_trials':2},'candidates':{
            'a':{'status':'BUILDING','base_key':'base'},
            'b':{'status':'BUILDING','base_key':'base'},
            'c':{'status':'BUILDING','base_key':'base'}}}

    def test_shared_priority_uses_highest_live_subscription(self):
        a=self.entry(.2);b=self.entry(.9)
        self.assertEqual(priorities({'a':a,'b':b})['build-base'],.9)
        b['status']='STOPPED'
        self.assertEqual(priorities({'a':a,'b':b})['build-base'],.2)

    def test_budget_preserves_started_candidate_outside_prefix(self):
        entry=self.entry();entry['budget_reconcile']=True
        controller=Pipeline.__new__(Pipeline)
        controller.pool=SimpleNamespace(state={'targets':{'t':entry},'jobs':{
            'build-c':{'status':'RUNNING'}}})
        controller.pending=[];controller.persist_job=Mock()
        controller.reconcile_target_controls()
        self.assertEqual(started_candidates(entry,controller.pool.state['jobs']),{'c'})
        self.assertEqual(entry['candidates']['a']['status'],'BUILDING')
        self.assertEqual(entry['candidates']['b']['status'],'CANCELLED')
        self.assertEqual(entry['candidates']['c']['status'],'BUILDING')

    def test_pending_priority_changes_in_both_directions(self):
        entry=self.entry(.9);controller=Pipeline.__new__(Pipeline)
        controller.pool=SimpleNamespace(state={'targets':{'t':entry}})
        controller.pending=[{'key':'build-a','priority':.2,'target_owned':True}]
        controller.persist_job=Mock();controller.reconcile_target_controls()
        self.assertEqual(controller.pending[0]['priority'],.9)
        entry['definition']['priority']=.1
        controller.reconcile_target_controls()
        self.assertEqual(controller.pending[0]['priority'],.1)
