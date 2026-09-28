"""验证结构任务被监督器中断时不会留下仍在计算的子进程。"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from codesign_lab.config import load
from codesign_lab.search.scheduler import atomic_json


class StructureWorkerChecks(unittest.TestCase):
    def test_timeout_ends_nested_process_in_same_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = root / 'structure.spec.json'
            result = root / 'structure.result.json'
            nested_pid = root / 'nested.pid'
            inner = ('import pathlib,subprocess,sys;'
                     'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"]);'
                     f'pathlib.Path({str(nested_pid)!r}).write_text(str(child.pid));'
                     'child.wait()')
            code = ('from pathlib import Path;import sys;'
                    'from codesign_lab.search.implementation import run;'
                    f'run(Path({str(root)!r}),[sys.executable,"-c",{inner!r}],"stage",timeout=30)')
            atomic_json(spec, {'job': {'key': 'structure-test', 'stage': 'implementation_code',
                'command': [sys.executable, '-c', code]}, 'cwd': str(root),
                'result': str(result), 'timeout': 1})
            worker = subprocess.run([sys.executable, '-m', 'codesign_lab.evaluation.worker',
                str(spec)], capture_output=True, text=True, timeout=15)
            self.assertEqual(worker.returncode, 0, worker.stderr)
            self.assertEqual(load(result)['status'], 'timeout')
            self.assertTrue(nested_pid.is_file())
            pid = int(nested_pid.read_text())
            for _ in range(30):
                path = Path('/proc') / str(pid) / 'status'
                if not path.exists() or path.read_text().split('State:')[1].lstrip().startswith('Z'):
                    break
                time.sleep(.1)
            else:
                os.kill(pid, 9)
                self.fail('结构任务超时后仍留下运行中的子进程')
