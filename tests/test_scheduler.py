"""验证进程重叠、超时收尾和缓存同键只计算一次。"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from codesign_lab.search.scheduler import run


class SchedulerChecks(unittest.TestCase):
    def test_two_processes_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code = ('import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); '
                    'p.touch(); other=pathlib.Path(sys.argv[2]); end=time.monotonic()+3\n'
                    'while not other.exists() and time.monotonic()<end: time.sleep(.02)\n'
                    'assert other.exists()\n')
            jobs = [{'key': str(index), 'command': [sys.executable, '-c', code,
                    str(root / str(index)), str(root / str(1 - index))], 'memory_bytes': 1024}
                    for index in (0, 1)]
            result = run(jobs, root / 'jobs', workers=2, timeout=5, budget=10)
            self.assertTrue(all(job['status'] == 'completed' for job in result['jobs']))

    def test_timeout_reaps_process(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run([{'key': 'slow', 'command': [sys.executable, '-c',
                'import time;time.sleep(30)'], 'memory_bytes': 1024}], directory,
                workers=1, timeout=.3, budget=5)
            self.assertEqual(result['jobs'][0]['status'], 'timeout')
            self.assertIsNotNone(result['jobs'][0]['exit_code'])

    def test_cache_single_flight_across_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code = '''import sys,time
from pathlib import Path
from codesign_lab.evaluation.cache import ResultCache
root=Path(sys.argv[1])
def compute():
    with (root/'calls').open('a') as stream:stream.write('called\\n')
    time.sleep(.3)
    return {'answer':42}
assert ResultCache(root/'cache', {'version':1}).call('timing', {'input':1}, compute)[0]=={'answer':42}
'''
            env = os.environ.copy()
            env.update(OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1')
            children = [subprocess.Popen([sys.executable, '-c', code, str(root)], env=env) for _ in range(2)]
            self.assertEqual([child.wait(timeout=10) for child in children], [0, 0])
            self.assertEqual((root / 'calls').read_text().splitlines(), ['called'])


if __name__ == '__main__':
    unittest.main()
