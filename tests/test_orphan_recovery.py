"""监督进程硬退出后只收尾身份匹配的任务，不重复运行孤儿。"""
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from codesign_lab.config import ROOT, load
from codesign_lab.search.scheduler import atomic_json
from codesign_lab.evaluation.recovery import reap_orphan


class OrphanRecoveryChecks(unittest.TestCase):
    def test_unreleased_child_does_not_execute_after_parent_identity_changes(self):
        from codesign_lab.evaluation.monitor import sample
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);spec=root/'blocked.spec.json';marker=root/'forbidden'
            pid=int(Path('/proc/self').resolve().name)
            atomic_json(spec,{'job':{'command':[sys.executable,'-c',
                'import pathlib,sys;pathlib.Path(sys.argv[1]).touch()',str(marker)]}})
            atomic_json(spec.with_suffix('.lease.json'),
                        {'pid':pid,'start_ticks':sample(pid)['start_ticks']+1})
            child=subprocess.run([sys.executable,'-m','codesign_lab.evaluation.task_process',str(spec)])
            self.assertEqual(child.returncode,75)
            self.assertTrue(spec.with_suffix('.child.json').exists())
            self.assertFalse(marker.exists())

    def launch(self, root):
        spec=root/'attempt.spec.json';ready=root/'ready';forbidden=root/'forbidden'
        atomic_json(spec, {'job':{'key':'orphan','stage':'case','command':[
            sys.executable,'-c','import pathlib,sys,time;pathlib.Path(sys.argv[1]).touch();time.sleep(5);pathlib.Path(sys.argv[2]).touch()',str(ready),str(forbidden)]},
            'cwd':str(ROOT),'result':str(root/'result.json'),'timeout':10})
        child=subprocess.Popen([sys.executable,'-m','codesign_lab.evaluation.worker',str(spec)])
        deadline=time.monotonic()+5
        while not ready.exists() and time.monotonic()<deadline:time.sleep(.02)
        self.assertTrue(ready.exists())
        child.kill();child.wait(timeout=5)
        return spec,forbidden

    def test_hard_supervisor_exit_reaps_task_before_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            spec,forbidden=self.launch(Path(directory))
            result=reap_orphan(spec)
            self.assertEqual(result['status'],'infrastructure_failed')
            self.assertTrue(result['recovered_orphan'])
            self.assertFalse(forbidden.exists())
            self.assertEqual(reap_orphan(spec),result)

    def test_wrong_start_ticks_never_signals_process(self):
        with tempfile.TemporaryDirectory() as directory:
            spec,_=self.launch(Path(directory))
            path=spec.with_suffix('.child.json');original=load(path)
            atomic_json(path,dict(original,start_ticks=original['start_ticks']+1))
            try:
                with self.assertRaisesRegex(RuntimeError,'PID 已复用'):reap_orphan(spec)
                os.kill(original['kill_pid'],0)
            finally:
                atomic_json(path,original);reap_orphan(spec)

    def test_legacy_protocol_is_not_blindly_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            spec=Path(directory)/'old.spec.json'
            atomic_json(spec,{'result':str(Path(directory)/'result.json')})
            atomic_json(spec.with_suffix('.lease.json'),{'spec':str(spec.resolve())})
            with self.assertRaisesRegex(RuntimeError,'旧任务'):reap_orphan(spec)
