"""本地假 CLI 的真实进程恢复验证，不发送摘要到模型服务。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch
from codesign_lab.config import ROOT
from codesign_lab.search.analysis_jobs import AnalysisCall,prepare


class AnalysisRecoveryChecks(unittest.TestCase):
    def setUp(self):
        # 假 CLI 的轨迹只属于本测试，不进入受保护的课程会话目录。
        self.trace_directory = tempfile.TemporaryDirectory()
        self.origin_patch = patch.dict(
            os.environ, {'CODESIGN_ORIGIN_ROOT': self.trace_directory.name})
        self.origin_patch.start()

    def tearDown(self):
        self.origin_patch.stop()
        self.trace_directory.cleanup()

    def fixture(self, root, fail=False):
        session=str(uuid.uuid4());calls=root/'calls';cli=root/'fake-codex'
        cli.write_text('#!'+sys.executable+'\n'+f'''
import sys,json,pathlib,time
if '--version' in sys.argv: print('fake-local-test');raise SystemExit(0)
if '--help' in sys.argv: print('--output-schema --json --output-last-message');raise SystemExit(0)
prompt=sys.stdin.read();snapshot=json.loads(prompt.split('\\n',1)[1])
pathlib.Path({str(calls)!r}).open('a').write('call\\n')
time.sleep(.4)
if {fail!r}:raise SystemExit(7)
session={session!r}
if 'resume' in sys.argv:session=sys.argv[sys.argv.index('resume')+1]
decision={{'schema_version':1,'decision_id':snapshot['request']['decision_id'],'lane':snapshot['request']['lane'],'summary':'本地恢复测试','conclusions':[],'new_targets':[],'stop_targets':[],'profile_requests':[],'implementation_proposals':[]}}
pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(decision))
print(json.dumps({{'type':'thread.started','thread_id':session,'local_test_fixture':True}}))
print(json.dumps({{'type':'turn.completed','local_test_fixture':True}}))
''')
        cli.chmod(0o700)
        lane='test-analysis-'+uuid.uuid4().hex[:10]
        snapshot={'request':{'decision_id':uuid.uuid4().hex,'lane':lane}}
        return cli,calls,snapshot,lane,session

    def await_answer(self, call):
        deadline=time.monotonic()+8
        while not call.done() and time.monotonic()<deadline:time.sleep(.03)
        self.assertTrue(call.done())
        return call.result()

    def test_independent_call_survives_launcher_exit_and_is_not_called_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cli,calls,snapshot,lane,session=self.fixture(root)
            metadata=prepare(root,snapshot,lane=lane,attempt=1,session_id=None,timeout=5,executable=str(cli))
            launcher=subprocess.run([sys.executable,'-c',
                'import json,sys,os;from codesign_lab.search.analysis_jobs import AnalysisCall;AnalysisCall(json.loads(sys.argv[1]),launch=True);os._exit(0)',json.dumps(metadata)],cwd=ROOT)
            self.assertEqual(launcher.returncode,0)
            answer=self.await_answer(AnalysisCall(metadata))
            self.assertEqual(answer['session_id'],session)
            self.assertEqual(calls.read_text().splitlines(),['call'])
            replay=AnalysisCall(metadata)
            self.assertTrue(replay.done());self.assertEqual(replay.result(),answer)
            self.assertEqual(calls.read_text().splitlines(),['call'])
            from codesign_lab.search.analyst import Analyst
            from codesign_lab.search.triggers import Triggers
            from codesign_lab.search.budget import Budget
            state={'targets':{}};triggers=Triggers(state);budget=Budget(state,wall_seconds=100)
            identifier=snapshot['request']['decision_id']
            budget.reserve('ai-'+identifier+'-1','ai')
            entry=triggers.lane(lane)
            entry.update(running_job=metadata,running_decision=identifier,
                         pending=snapshot['request'],attempts={identifier:1})
            accepted=[]
            pool=SimpleNamespace(state=state,save=lambda:None,
                apply=lambda request:accepted.append(request) or {'status':'accepted'})
            analyst=Analyst(pool,triggers,budget,root,enabled=False)
            try:
                analyst.poll()
                self.assertEqual(len(accepted),1)
                self.assertNotIn('running_job',entry)
                self.assertEqual(budget.used('ai'),1)
                self.assertEqual(calls.read_text().splitlines(),['call'])
            finally:analyst.close()

    def test_failed_call_cannot_return_a_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cli,_,snapshot,lane,_=self.fixture(root,fail=True)
            metadata=prepare(root,snapshot,lane=lane,attempt=1,session_id=None,timeout=5,executable=str(cli))
            call=AnalysisCall(metadata,launch=True)
            with self.assertRaises(RuntimeError):self.await_answer(call)

    def test_dotted_lane_keeps_unique_artifact_stem(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata=prepare(Path(directory),{'request':{'decision_id':'abc'}},lane='test.lane',attempt=2,session_id=None,timeout=5)
            self.assertIn('test.lane-abc-attempt2',metadata['spec'])
