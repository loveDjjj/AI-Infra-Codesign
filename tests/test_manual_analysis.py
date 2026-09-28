"""手动分析只投递控制请求，不能绕过累计预算或重复在途调用。"""
import tempfile
import threading
import unittest
from pathlib import Path
from codesign_lab.search.targets import TargetPool
from codesign_lab.search.triggers import Triggers
from codesign_lab.search.budget import Budget
from codesign_lab.search.analyst import Analyst
from codesign_lab.search.scheduler import atomic_json
from codesign_lab.config import ROOT
from codesign_lab.search.targets import epoch
import subprocess


class ManualAnalysisChecks(unittest.TestCase):
    def test_real_cli_execute_only_injects_without_calling_model(self):
        directory=ROOT/'workspace/pipeline';directory.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            pool=TargetPool(temporary,epoch())
            Budget(pool.state,wall_seconds=100)
            Triggers(pool.state).lane('lane')['pending']={'decision_id':'id','lane':'lane'}
            pool.save()
            process=subprocess.run([str(ROOT/'lab'),'pipeline-analyze','--campaign',Path(temporary).name,
                '--lane','lane','--execute'],cwd=ROOT,capture_output=True,text=True,timeout=5)
            self.assertEqual(process.returncode,0,process.stderr)
            self.assertEqual(len(list((Path(temporary)/'inbox').glob('*.json'))),1)
            self.assertEqual(pool.consume()[0]['status'],'accepted')
            self.assertEqual(pool.state['budget']['reservations'],{})
            self.assertNotIn('running_job',pool.state['analysis']['lanes']['lane'])

    def pool(self, directory):
        pool=TargetPool(directory,'epoch')
        budget=Budget(pool.state,wall_seconds=100,ai_calls=1)
        triggers=Triggers(pool.state)
        triggers.lane('lane')['pending']={'decision_id':'id','lane':'lane'}
        return pool,budget,triggers

    def request(self, pool, identifier='request', decision='id'):
        return pool.apply({'request_id':identifier,'command':{
            'op':'analyze','lane':'lane','decision_id':decision,'timeout':20}})

    def test_request_persists_and_duplicate_does_not_spend_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            pool,budget,_=self.pool(directory)
            self.assertEqual(self.request(pool)['status'],'accepted')
            self.assertEqual(self.request(pool,'duplicate')['status'],'reused')
            self.assertEqual(budget.used('ai'),0)
            restored=TargetPool(directory,'epoch')
            self.assertEqual(restored.state['analysis']['lanes']['lane']['manual_request']['timeout'],20)

    def test_budget_exhaustion_and_expiry_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            pool,budget,_=self.pool(directory)
            budget.reserve('spent','ai')
            self.assertEqual(self.request(pool)['status'],'rejected')
        with tempfile.TemporaryDirectory() as directory:
            pool,budget,_=self.pool(directory);budget.state['deadline']=0
            self.assertEqual(self.request(pool)['status'],'rejected')

    def test_stale_identity_or_recovery_block_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            pool,_,triggers=self.pool(directory)
            self.assertEqual(self.request(pool,decision='stale')['status'],'rejected')
            triggers.lane('lane')['recovery_blocked']=True
            self.assertEqual(self.request(pool,'blocked')['status'],'rejected')

    def test_manual_request_runs_when_auto_disabled_but_still_uses_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            pool,budget,triggers=self.pool(directory)
            root=Path(directory);atomic_json(root/'id.snapshot.json',{'request':{'decision_id':'id'}})
            self.request(pool)
            started=threading.Event();release=threading.Event()
            def callback(snapshot,**kwargs):
                self.assertEqual(kwargs['timeout'],20)
                started.set();release.wait(timeout=3)
                raise RuntimeError('本地模拟分析失败')
            analyst=Analyst(pool,triggers,budget,root,enabled=False,callback=callback)
            try:
                analyst.poll();self.assertTrue(started.wait(timeout=2))
                self.assertEqual(budget.used('ai'),1)
                self.assertEqual(len(analyst.active),1)
                analyst.poll();self.assertEqual(budget.used('ai'),1)
            finally:
                release.set()
                for future in analyst.active.values():
                    with self.assertRaises(RuntimeError):future.result(timeout=3)
                analyst.poll();analyst.close()

    def test_auto_disabled_without_manual_request_never_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            pool,budget,triggers=self.pool(directory)
            analyst=Analyst(pool,triggers,budget,Path(directory),enabled=False,callback=lambda **kwargs:None)
            analyst.poll();self.assertEqual(analyst.active,{})
            self.assertEqual(budget.used('ai'),0);analyst.close()
