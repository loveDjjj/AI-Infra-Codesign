"""调度等待原因和主机身份核对不冒充芯片 bound。"""
import unittest
from unittest.mock import patch
from codesign_lab.search.status import queue_details,process_identity,attempt_costs


class PipelineStatusChecks(unittest.TestCase):
    def queue(self,job,active=(),**kwargs):
        settings=dict(workers=4,full_slots=1,external=0,memory_budget=10,available=10,now=100)
        settings.update(kwargs)
        return queue_details([dict(key='task',stage='case',memory_bytes=2,queued_wall=90,**job)],active,**settings)[0]

    def test_exploration_preserves_official_capacity(self):
        result=self.queue({},[{'stage':'case','memory_bytes':1}]*3)
        self.assertIn('保留验收',result['reason']);self.assertEqual(result['queue_seconds'],10)

    def test_memory_and_backoff_and_expiry_are_distinct(self):
        self.assertEqual(self.queue({},available=1)['reason'],'内存准入限制')
        self.assertEqual(self.queue({'retry_after':101})['reason'],'重试退避')
        self.assertEqual(self.queue({},expired=True)['reason'],'累计截止时间已到')

    def test_pid_reuse_is_not_live_process(self):
        with patch('codesign_lab.search.status.sample',return_value={'start_ticks':2,'state':'R'}):
            self.assertEqual(process_identity(100,1),'PID 已复用')
            self.assertEqual(process_identity(100,2),'身份匹配，生成时存活')

    def test_attempt_costs_keep_estimates_and_retries_separate(self):
        result=attempt_costs({'jobs':{'one':{'attempts':[
            {'attempt':1,'result':{'wall_seconds':3}},
            {'attempt':2,'result':{'wall_seconds':5,'wall_seconds_estimated':True}}]}}})
        self.assertEqual(result,{'attempts':2,'attempt_host_seconds':8,'retry_host_seconds':5,'estimated_attempts':1})
