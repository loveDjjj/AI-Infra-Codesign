"""停止订阅不能取消其他方向共享的任务。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from codesign_lab.search.pipeline import Pipeline


class TargetStopChecks(unittest.TestCase):
    def controller(self):
        controller = Pipeline.__new__(Pipeline)
        candidate = {'status': 'EVALUATING', 'case_keys': ['shared', 'exclusive']}
        controller.pool = SimpleNamespace(state={'targets': {
            'stopped': {'status': 'DRAINING', 'trials_launched': 1,
                        'candidates': {'a': candidate}},
            'live': {'status': 'ACTIVE', 'candidates': {
                'b': {'status': 'EVALUATING', 'case_keys': ['shared']}}}}})
        controller.pending = [{'key': key, 'target_owned': owned} for key, owned in
                              [('shared', True), ('exclusive', True), ('legacy', False)]]
        controller.seen = {'shared', 'exclusive', 'legacy'}
        controller.persist_job = Mock()
        return controller

    def test_shared_and_legacy_tasks_survive(self):
        controller = self.controller()
        controller.cancel_stopped_targets()
        self.assertEqual([job['key'] for job in controller.pending], ['shared', 'legacy'])
        controller.persist_job.assert_called_once_with(
            {'key': 'exclusive', 'target_owned': True}, 'CANCELLED')
        self.assertNotIn('exclusive', controller.seen)
        entry = controller.pool.state['targets']['stopped']
        self.assertEqual(entry['status'], 'STOPPED')
        self.assertEqual(entry['candidates']['a']['status'], 'CANCELLED')

    def test_shared_base_build_survives(self):
        controller = self.controller()
        controller.pool.state['targets']['live']['candidates'] = {
            'next': {'status': 'BUILDING', 'base_key': 'base'}}
        controller.pending = [{'key': key, 'target_owned': True}
                              for key in ['build-base', 'build-next', 'build-abandoned']]
        controller.cancel_stopped_targets()
        self.assertEqual([job['key'] for job in controller.pending], ['build-base', 'build-next'])

    def test_legacy_subscription_protects_existing_dynamic_task(self):
        controller = self.controller()
        controller.enqueue({'key': 'exclusive'})
        controller.cancel_stopped_targets()
        self.assertIn('exclusive', [job['key'] for job in controller.pending])

    def test_live_worker_is_not_terminated(self):
        controller = self.controller()
        controller.active = {'exclusive': {'child': Mock()}}
        controller.cancel_stopped_targets()
        controller.active['exclusive']['child'].terminate.assert_not_called()

    def test_resume_skips_cancelled_task_and_allows_new_subscription(self):
        controller = self.controller()
        controller.pool.state['jobs'] = {'cancelled': {
            'job': {'key': 'cancelled'}, 'status': 'CANCELLED'}}
        controller.seen = set()
        controller.restore_jobs()
        self.assertNotIn('cancelled', controller.seen)


    def test_split_shared_dependencies_survive_until_last_subscriber_stops(self):
        controller=self.controller()
        for entry in controller.pool.state['targets'].values():
            for candidate in entry['candidates'].values():
                candidate['case_keys']=['functional-shared','case-shared']
        controller.pending=[{'key':key,'target_owned':True}
                            for key in ['functional-shared','case-shared']]
        controller.seen={'functional-shared','case-shared'}
        controller.cancel_stopped_targets()
        self.assertEqual(len(controller.pending),2)
        controller.pool.state['targets']['live'].update(status='DRAINING',trials_launched=1)
        controller.cancel_stopped_targets()
        self.assertEqual(controller.pending,[])
        self.assertEqual(controller.seen,set())
