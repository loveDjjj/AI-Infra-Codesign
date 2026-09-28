"""验证主控退出不丢结果、监督超时会终止真实任务。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


class WorkerChecks(unittest.TestCase):
    def test_worker_survives_creator_exit_and_records_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);spec = root / 'job.json';result = root / 'result.json'
            spec.write_text(json.dumps({'job': {'key': 'survivor', 'stage': 'case',
                'command': [sys.executable, '-c', 'import time;time.sleep(.6)']},
                'cwd': str(root), 'result': str(result), 'timeout': 5}))
            code = 'import subprocess,sys;subprocess.Popen([sys.executable,"-m","codesign_lab.evaluation.worker",sys.argv[1]],start_new_session=True)'
            env = os.environ.copy()
            env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
            creator = subprocess.run([sys.executable, '-c', code, str(spec)], env=env, timeout=5)
            self.assertEqual(creator.returncode, 0)
            deadline = time.monotonic() + 8
            while not result.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(result.exists())
            self.assertEqual(json.loads(result.read_text())['status'], 'completed')
            lease = json.loads(spec.with_suffix('.lease.json').read_text())
            self.assertGreater(lease['start_ticks'], 0)

    def test_resume_attaches_same_live_supervisor_without_relaunch(self):
        from codesign_lab.search.pipeline import Pipeline
        from codesign_lab.search.scheduler import atomic_json
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);jobs = root / 'jobs';jobs.mkdir()
            spec = jobs / 'live.spec.json';result = jobs / 'live.attempt.result.json'
            job = {'key': 'live', 'stage': 'case', 'memory_bytes': 1, 'candidate': str(root),
                   'command': [sys.executable, '-c', 'import time;time.sleep(1.5)']}
            atomic_json(spec, {'job': job, 'cwd': str(root), 'result': str(result), 'timeout': 5})
            child = subprocess.Popen([sys.executable, '-m', 'codesign_lab.evaluation.worker', str(spec)])
            try:
                lease = spec.with_suffix('.lease.json');deadline = time.monotonic()+5
                while not lease.exists() and time.monotonic()<deadline:
                    time.sleep(.02)
                self.assertTrue(lease.exists())
                instance = Pipeline.__new__(Pipeline)
                instance.out=root;instance.seen=set();instance.active={};instance.pending=[]
                instance.pool=SimpleNamespace(save=lambda: None, state={'jobs': {'live': {'job': job, 'status': 'RUNNING',
                    'spec': str(spec), 'attempt_result': str(result)}}})
                observed=[];instance.completed=lambda j,r:observed.append(r)
                instance.restore_jobs()
                self.assertEqual(list(instance.active), ['live'])
                self.assertEqual(instance.active['live']['pid'], json.loads(lease.read_text())['pid'])
                self.assertEqual(instance.pending, [])
                self.assertIsInstance(instance.pool.state['jobs']['live']['job']['candidate'], str)
                json.dumps(instance.pool.state)
                child.wait(timeout=5)
                instance.active={}
                instance.restore_jobs()
                self.assertEqual(observed[0]['status'], 'completed')
                self.assertTrue((jobs/'live.result.json').exists())
            finally:
                if child.poll() is None:
                    child.terminate();child.wait(timeout=5)

    def test_timeout_stops_work_before_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);spec = root / 'job.json';result = root / 'result.json';marker = root / 'should-not-exist'
            spec.write_text(json.dumps({'job': {'key': 'slow', 'stage': 'case',
                'command': [sys.executable, '-c', 'import time,pathlib,sys;time.sleep(2);pathlib.Path(sys.argv[1]).touch()', str(marker)]},
                'cwd': str(root), 'result': str(result), 'timeout': .2}))
            subprocess.run([sys.executable, '-m', 'codesign_lab.evaluation.worker', str(spec)], check=True, timeout=5)
            self.assertEqual(json.loads(result.read_text())['status'], 'timeout')
            time.sleep(2)
            self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
