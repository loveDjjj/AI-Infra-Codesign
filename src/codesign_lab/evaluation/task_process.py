"""执行前登记子进程身份，等待监督进程确认后才进入评估。"""
import os
from pathlib import Path
import sys
import time
from ..config import load
from ..search.scheduler import atomic_json
from .monitor import sample


def run(path):
    path = Path(path).resolve()
    spec = load(path)
    supervisor = load(path.with_suffix('.lease.json'))
    pid = int(Path('/proc/self').resolve().name)
    identity = sample(pid)
    atomic_json(path.with_suffix('.child.json'), {'pid': pid, 'kill_pid': os.getpid(),
        'start_ticks': identity['start_ticks'], 'spec': str(path),
        'process_group': os.getpgrp(), 'started_wall': time.time()})
    permit = path.with_suffix('.permit.json')
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            parent = sample(supervisor['pid'])
        except OSError:
            return 75
        if parent['start_ticks'] != supervisor['start_ticks'] or parent['state'] == 'Z':
            return 75
        if permit.exists():
            authorization = load(permit)
            if authorization != {'pid': pid, 'start_ticks': identity['start_ticks']}:
                raise ValueError('监督放行身份不一致')
            command = spec['job']['command']
            if spec['job']['stage'] == 'implementation':
                # 结构验证的子进程留在本任务进程组，监督器中断时一并收尾。
                os.environ['CODESIGN_STRUCTURE_WORKER'] = '1'
            os.execvpe(command[0], command, os.environ)
        time.sleep(.02)
    return 75


if __name__ == '__main__':
    raise SystemExit(run(sys.argv[1]))
