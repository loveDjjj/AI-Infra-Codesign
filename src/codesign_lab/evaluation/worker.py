"""独立监督评估子进程；主控退出后仍保存最终结果与可核对的进程身份。"""
import argparse
import fcntl
import os
import signal
from pathlib import Path
import subprocess
import sys
import time

from ..config import ROOT, load
from ..search.scheduler import atomic_json, proc_pid, terminate
from .monitor import sample, sample_tree


def run(spec_path):
    spec_path = Path(spec_path)
    spec = load(spec_path)
    result_path = Path(spec['result'])
    lease = spec_path.with_suffix('.lease.json')
    lock_path = spec_path.with_suffix('.lock')
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if result_path.exists():
            return
        pid = int(Path('/proc/self').resolve().name)
        identity = sample(pid)
        atomic_json(lease, {'pid': pid, 'start_ticks': identity['start_ticks'],
            'spec': str(spec_path.resolve()), 'started_wall': time.time(), 'child_protocol': 1})
        started = time.monotonic()
        child = None
        peak = 0
        status = None
        def interrupted(signum, frame):
            raise InterruptedError('监督 worker 收到停止信号')
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        try:
            child_env = os.environ.copy()
            child_env['PYTHONPATH'] = str(ROOT / 'src') + os.pathsep + child_env.get('PYTHONPATH', '')
            child = subprocess.Popen([sys.executable, '-m', 'codesign_lab.evaluation.task_process',
                                      str(spec_path.resolve())], cwd=spec['cwd'], env=child_env, start_new_session=True)
            target = proc_pid(child.pid)
            child_lease = spec_path.with_suffix('.child.json')
            handshake_deadline = time.monotonic() + min(10, spec['timeout'])
            while not child_lease.exists():
                if time.monotonic() >= handshake_deadline:
                    raise TimeoutError('评估子进程登记超时')
                if child.poll() is not None:
                    raise RuntimeError('评估子进程未完成身份登记')
                time.sleep(.02)
            registered = load(child_lease)
            if registered['pid'] != target or registered['spec'] != str(spec_path.resolve()):
                raise ValueError('评估子进程身份不一致')
            atomic_json(spec_path.with_suffix('.permit.json'),
                        {'pid': target, 'start_ticks': registered['start_ticks']})
            while child.poll() is None:
                try:
                    peak = max(peak, sample_tree(target)['tree_rss_bytes'])
                except (OSError, ValueError, IndexError):
                    pass
                if time.monotonic() - started >= spec['timeout']:
                    status = 'timeout'
                    terminate(child)
                    break
                time.sleep(.2)
            code = child.wait()
            result = {'key': spec['job']['key'], 'stage': spec['job']['stage'],
                'status': status or ('completed' if code == 0 else 'failed'), 'exit_code': code,
                'wall_seconds': time.monotonic() - started, 'peak_rss_bytes': peak}
        except BaseException as exc:
            if child is not None:
                terminate(child)
            result = {'key': spec['job']['key'], 'stage': spec['job']['stage'],
                'status': 'timeout' if isinstance(exc, TimeoutError) else 'infrastructure_failed',
                'error': type(exc).__name__ + ': ' + str(exc),
                'wall_seconds': time.monotonic() - started, 'peak_rss_bytes': peak}
        atomic_json(result_path, result)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('spec', type=Path)
    run(parser.parse_args().spec)
