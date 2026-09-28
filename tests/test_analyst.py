"""验证外部分析不会阻塞主控、并发有界且失败有冷却。"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from codesign_lab.search.analyst import Analyst
from codesign_lab.search.budget import Budget
from codesign_lab.search.triggers import Triggers
from codesign_lab.search.scheduler import atomic_json


class AnalystChecks(unittest.TestCase):
    def test_two_analysis_calls_do_not_block_main_poll_and_third_waits(self):
        state={'targets':{}};triggers=Triggers(state);budget=Budget(state,wall_seconds=100)
        release=threading.Event();started=threading.Barrier(3)
        for index in range(3):
            entry=triggers.lane('lane'+str(index));entry['pending']={'decision_id':str(index),'lane':'lane'+str(index)}
        pool=SimpleNamespace(state=state,save=lambda:None)
        def callback(snapshot,**kwargs):
            started.wait(timeout=3);release.wait(timeout=3)
            raise RuntimeError('模拟服务错误')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for index in range(3):atomic_json(root/(str(index)+'.snapshot.json'),{'request':{}})
            analyst=Analyst(pool,triggers,budget,root,enabled=True,callback=callback)
            try:
                before=time.monotonic();analyst.poll()
                self.assertLess(time.monotonic()-before,1)
                started.wait(timeout=3)
                self.assertEqual(len(analyst.active),2)
                self.assertEqual(budget.used('ai'),2)
                release.set()
                for future in analyst.active.values():
                    with self.assertRaises(RuntimeError):future.result(timeout=3)
                analyst.enabled=False;analyst.poll()
                self.assertEqual(analyst.active,{})
                self.assertGreater(triggers.lane('lane0')['retry_after_wall'],time.time())
            finally:
                release.set();analyst.close()

    def test_running_analysis_snapshot_is_frozen_until_completion(self):
        state={};triggers=Triggers(state,batch_size=1)
        entry=triggers.lane('lane');entry['running_decision']='original'
        entry['pending']={'decision_id':'original','observation_ids':[]}
        triggers.observe('lane',{'id':'new'})
        self.assertEqual(triggers.poll({'t':{'definition':{'lane':'lane'},'status':'DONE'}},0),[])
        self.assertEqual(entry['pending']['observation_ids'],[])
