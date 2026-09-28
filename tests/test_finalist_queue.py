"""候选验收队列有界，优先更好的同硬件组合且不抢占运行任务。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from codesign_lab.search.pipeline import Pipeline


class FinalistQueueChecks(unittest.TestCase):
    def controller(self, jobs, active=()):
        controller=Pipeline.__new__(Pipeline)
        controller.args=SimpleNamespace(max_pending_full=2,min_predicted_gain=0)
        controller.initial_score=100;controller.pending=list(jobs)
        controller.active={str(i):{'job':job} for i,job in enumerate(active)}
        controller.persist_job=Mock()
        return controller

    def job(self, name, score, hw, stage='verify'):
        return {'key':name,'priority':score,'hardware_hash':hw,'stage':stage}

    def test_same_hardware_keeps_best_waiting_pair(self):
        controller=self.controller([self.job('old',110,'a','full'),self.job('new',120,'a')])
        controller.trim_finalists()
        self.assertEqual([job['key'] for job in controller.pending],['new'])
        self.assertEqual(controller.persist_job.call_args[0][1],'CANCELLED')

    def test_total_waiting_cap_spans_hardware_groups(self):
        controller=self.controller([self.job('a',110,'a'),self.job('b',120,'b'),self.job('c',130,'c')])
        controller.trim_finalists()
        self.assertEqual({job['key'] for job in controller.pending},{'b','c'})

    def test_running_weaker_pair_not_cancelled(self):
        running=self.job('running',110,'a','full')
        controller=self.controller([self.job('new',120,'a')],[running])
        controller.trim_finalists()
        self.assertEqual(len(controller.pending),1)
        self.assertEqual(controller.active['0']['job'],running)

    def test_running_better_pair_removes_waiting_weaker(self):
        controller=self.controller([self.job('old',110,'a')],[self.job('running',120,'a','full')])
        controller.trim_finalists()
        self.assertEqual(controller.pending,[])

    def test_audited_threshold_removes_stale_pairs_but_keeps_audit(self):
        controller=self.controller([self.job('old',110,'a'),self.job('audit',110,'a','audit')])
        controller.initial_score=115
        controller.trim_finalists()
        self.assertEqual([job['stage'] for job in controller.pending],['audit'])
