"""真实监督进程验证启动意图恢复与重复启动互斥。"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from codesign_lab.config import ROOT, load
from codesign_lab.search.scheduler import atomic_json
from codesign_lab.evaluation.recovery import recover_start


class StartRecoveryChecks(unittest.TestCase):
    def spec(self, root):
        spec = root/'attempt.spec.json'
        marker = root/'calls.txt'
        atomic_json(spec, {'job': {'key':'recover','stage':'case','command':[
            sys.executable,'-c',
            'import pathlib,sys,time;p=pathlib.Path(sys.argv[1]);p.open("a").write("run\\n");time.sleep(.4)',str(marker)]},
            'cwd':str(ROOT),'result':str(root/'attempt.result.json'),'timeout':5})
        return spec, marker

    def finish(self, spec, marker):
        deadline=time.monotonic()+5
        result=Path(load(spec)['result'])
        while not result.exists() and time.monotonic()<deadline:time.sleep(.02)
        self.assertEqual(load(result)['status'],'completed')
        self.assertEqual(marker.read_text().splitlines(),['run'])

    def test_saved_intent_before_popen_is_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            spec,marker=self.spec(Path(directory))
            answer=recover_start(spec,sys.executable,os.environ.copy())
            if answer.get('child'):answer['child'].wait(timeout=5)
            self.finish(spec,marker)

    def test_original_and_recovery_race_execute_once(self):
        with tempfile.TemporaryDirectory() as directory:
            spec,marker=self.spec(Path(directory))
            first=subprocess.Popen([sys.executable,'-m','codesign_lab.evaluation.worker',str(spec)],
                                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                # 在 lease 出现前补启动；attempt flock 决定唯一执行者。
                if spec.with_suffix('.lease.json').exists():
                    self.skipTest('原进程已越过启动窗口')
                answer=recover_start(spec,sys.executable,os.environ.copy())
                if answer.get('child'):answer['child'].wait(timeout=5)
                first.wait(timeout=5)
                self.finish(spec,marker)
            finally:
                if first.poll() is None:first.terminate();first.wait(timeout=5)

    def test_existing_lease_cannot_blindly_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            spec,_=self.spec(Path(directory))
            atomic_json(spec.with_suffix('.lease.json'),{'pid':1})
            with self.assertRaises(ValueError):recover_start(spec,sys.executable,os.environ.copy())
