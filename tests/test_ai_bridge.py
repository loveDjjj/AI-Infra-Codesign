"""用模拟 CLI 验证身份、锁和输出，不将其称为真实模型测试。"""
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from codesign_lab.ai_bridge import analyze, write_trace


class FakeProcess:
    returncode=0
    def __init__(self,command,**kwargs):
        self.output=Path(command[command.index('-o')+1])
    def communicate(self,prompt,timeout):
        snapshot=json.loads(prompt.split('\n',1)[1])
        decision={'schema_version':1,'decision_id':snapshot['request']['decision_id'],'lane':'p1_attention',
            'summary':'空间已覆盖','conclusions':[],'new_targets':[],'stop_targets':[],
            'profile_requests':[],'implementation_proposals':[]}
        self.output.write_text(json.dumps(decision))
        return json.dumps({'type':'thread.started','thread_id':SESSION})+'\n'+json.dumps({'type':'turn.completed'}),' '


SESSION=str(uuid.uuid4())


class BridgeChecks(unittest.TestCase):
    def test_trace_uses_origin_when_running_in_source_epoch(self):
        import os
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'CODESIGN_ORIGIN_ROOT':directory}):
            write_trace('global','attempt-1','event\n','error\n')
            target=Path(directory)/'data/agent-trace/pipeline/global'
            self.assertEqual((target/'attempt-1.events.jsonl').read_text(),'event\n')
            self.assertEqual((target/'attempt-1.stderr.log').read_text(),'error\n')

    def test_resume_same_session_and_returns_decision(self):
        with tempfile.TemporaryDirectory() as directory,patch('codesign_lab.ai_bridge.capabilities',return_value={'version':'fake'}),patch('codesign_lab.ai_bridge.subprocess.Popen',side_effect=FakeProcess) as launched,patch('codesign_lab.ai_bridge.write_trace'):
            result=analyze({'request':{'decision_id':'a'*64}},lane='p1_attention',directory=directory,session_id=SESSION)
            self.assertEqual(result['session_id'],SESSION)
            self.assertEqual(result['decision']['new_targets'],[])
            command=launched.call_args.args[0]
            self.assertEqual(command[command.index('--model')+1],'gpt-6-astra')
            self.assertIn('model_reasoning_effort=medium',command)

    def test_wrong_resumed_session_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory,patch('codesign_lab.ai_bridge.capabilities',return_value={'version':'fake'}),patch('codesign_lab.ai_bridge.subprocess.Popen',side_effect=FakeProcess),patch('codesign_lab.ai_bridge.write_trace'):
            with self.assertRaises(ValueError):
                analyze({'request':{'decision_id':'a'*64}},lane='p1_attention',directory=directory,session_id=str(uuid.uuid4()))

    def test_session_lock_prevents_second_call(self):
        import fcntl
        with tempfile.TemporaryDirectory() as directory:
            with (Path(directory)/'p1_attention.session.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    analyze({'request':{'decision_id':'a'*64}},lane='p1_attention',directory=directory)

    def test_resumed_session_uses_shared_lock_across_directories(self):
        import fcntl
        import os
        with tempfile.TemporaryDirectory() as directory:
            lock=Path(directory)/(SESSION+'.session.lock')
            with lock.open('a') as stream,patch.dict(os.environ,{'CODEX_SESSION_LOCK_ROOT':directory}):
                fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    analyze({'request':{'decision_id':'a'*64}},lane='global',
                            directory=Path(directory)/'campaign-two',session_id=SESSION)

    def test_timeout_never_returns_a_decision(self):
        import subprocess
        from unittest.mock import Mock
        child=Mock();child.communicate.side_effect=[subprocess.TimeoutExpired('codex',.01), ('', '')]
        with tempfile.TemporaryDirectory() as directory,patch('codesign_lab.ai_bridge.capabilities',return_value={'version':'fake'}),patch('codesign_lab.ai_bridge.subprocess.Popen',return_value=child),patch('codesign_lab.ai_bridge.terminate') as stop,patch('codesign_lab.ai_bridge.write_trace'):
            with self.assertRaises(TimeoutError):
                analyze({'request':{'decision_id':'a'*64}},lane='p1_attention',directory=directory,timeout=.01)
            stop.assert_called_once_with(child)

    def test_missing_completed_event_rejects_even_with_exit_zero(self):
        class Incomplete(FakeProcess):
            def communicate(self,prompt,timeout):
                stdout,stderr=super().communicate(prompt,timeout)
                return stdout.splitlines()[0],stderr
        with tempfile.TemporaryDirectory() as directory,patch('codesign_lab.ai_bridge.capabilities',return_value={'version':'fake'}),patch('codesign_lab.ai_bridge.subprocess.Popen',side_effect=Incomplete),patch('codesign_lab.ai_bridge.write_trace'):
            with self.assertRaises(ValueError):
                analyze({'request':{'decision_id':'a'*64}},lane='p1_attention',directory=directory)
