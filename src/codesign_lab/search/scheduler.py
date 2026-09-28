"""调度独立子进程，按 CPU、内存和时间预算准入并动态补位。"""
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time

from ..evaluation.monitor import sample_tree


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def resources():
    cpus = len(os.sched_getaffinity(0))
    try:
        quota, period = Path('/sys/fs/cgroup/cpu.max').read_text().split()
        if quota != 'max':
            cpus = min(cpus, max(1, math.floor(int(quota) / int(period))))
    except (OSError, ValueError):
        pass
    available = int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                         if line.startswith('MemAvailable:'))) * 1024
    try:
        limit = Path('/sys/fs/cgroup/memory.max').read_text().strip()
        if limit != 'max':
            used = int(Path('/sys/fs/cgroup/memory.current').read_text())
            available = min(available, max(0, int(limit) - used))
    except (OSError, ValueError):
        pass
    return {'cpus': cpus, 'available_memory_bytes': available}


def proc_pid(pid):
    """处理容器 PID 与 /proc 所见主机 PID 不一致的环境。"""
    try:
        thread = os.readlink('/proc/thread-self').split('/')[-1]
        children = Path('/proc/self/task', thread, 'children').read_text().split()
        for child in children:
            text = Path('/proc', child, 'status').read_text()
            if text.split('NSpid:')[1].splitlines()[0].split()[-1] == str(pid):
                return int(child)
    except (OSError, IndexError):
        pass
    return pid


def terminate(child):
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
    except ProcessLookupError:
        child.wait()


def run(jobs, directory, *, workers=8, memory_fraction=.8, timeout=3600, budget=7200, env=None, on_complete=None):
    if workers < 1 or not 0 < memory_fraction <= 1 or timeout <= 0 or budget <= 0:
        raise ValueError('并发数、内存比例和时间预算必须有效')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    capacity = resources()
    workers = min(workers, capacity['cpus'])
    memory_budget = int(capacity['available_memory_bytes'] * memory_fraction)
    pending = list(jobs)
    if len({job['key'] for job in pending}) != len(pending):
        raise ValueError('调度任务必须先按实际输入去重')
    active, done = {}, []
    started = time.monotonic()
    last_status = 0
    try:
        while pending or active:
            elapsed = time.monotonic() - started
            if elapsed >= budget:
                for job in pending:
                    done.append({'key': job['key'], 'status': 'budget_exhausted', 'wall_seconds': 0})
                pending.clear()
            reserved = sum(item['job']['memory_bytes'] for item in active.values())
            available_now = int(resources()['available_memory_bytes'] * memory_fraction)
            for job in list(pending):
                if len(active) >= workers or elapsed >= budget:
                    break
                if job['memory_bytes'] > memory_budget:
                    done.append({'key': job['key'], 'status': 'resource_rejected', 'wall_seconds': 0})
                    pending.remove(job)
                    continue
                if reserved + job['memory_bytes'] > memory_budget or job['memory_bytes'] > available_now:
                    continue
                stdout = (directory / (job['key'] + '.stdout.log')).open('w')
                stderr = (directory / (job['key'] + '.stderr.log')).open('w')
                try:
                    child = subprocess.Popen(job['command'], cwd=job.get('cwd'), env=env,
                                             stdout=stdout, stderr=stderr, start_new_session=True)
                except Exception:
                    stdout.close(); stderr.close()
                    raise
                item = {'child': child, 'proc_pid': proc_pid(child.pid), 'job': job,
                        'start': time.monotonic(), 'stdout': stdout, 'stderr': stderr, 'peak_rss_bytes': 0}
                active[job['key']] = item
                reserved += job['memory_bytes']; available_now -= job['memory_bytes']
                pending.remove(job)
            now = time.monotonic()
            for key, item in list(active.items()):
                duration = now - item['start']
                try:
                    sample = sample_tree(item['proc_pid'])
                    item['peak_rss_bytes'] = max(item['peak_rss_bytes'], sample['tree_rss_bytes'])
                    item['sample'] = sample
                except (OSError, ValueError, IndexError):
                    pass
                status = None
                if duration >= timeout or now - started >= budget:
                    status = 'timeout' if duration >= timeout else 'budget_exhausted'
                    terminate(item['child'])
                code = item['child'].poll()
                if code is not None:
                    result = {'key': key, 'status': status or ('completed' if code == 0 else 'failed'),
                              'exit_code': code, 'wall_seconds': duration,
                              'peak_rss_bytes': item['peak_rss_bytes'],
                              'host_last_sample': item.get('sample')}
                    atomic_json(directory / (key + '.result.json'), result)
                    done.append(result)
                    item['stdout'].close(); item['stderr'].close(); del active[key]
                    if on_complete:
                        on_complete(result)
            if now - last_status >= 2 or not active:
                atomic_json(directory / 'status.json', {'elapsed_seconds': now - started,
                    'workers_limit': workers, 'memory_budget_bytes': memory_budget,
                    'pending': len(pending), 'completed': len(done),
                    'active': [{'key': key, 'pid': item['proc_pid'], 'wall_seconds': now - item['start'],
                                'rss_bytes': item.get('sample', {}).get('tree_rss_bytes')}
                               for key, item in active.items()]})
                last_status = now
            if pending or active:
                time.sleep(.2)
    finally:
        for item in active.values():
            terminate(item['child']); item['stdout'].close(); item['stderr'].close()
    return {'jobs': done, 'wall_seconds': time.monotonic() - started, 'workers_limit': workers,
            'memory_budget_bytes': memory_budget}
