"""恢复已保存的启动意图；同一 attempt 由监督进程锁防止重复执行。"""
import subprocess
import time
from pathlib import Path
from ..config import ROOT, load
from .monitor import sample


def recover_start(spec, python, env, *, timeout=10):
    spec = Path(spec)
    document = load(spec)
    result = Path(document['result'])
    lease_path = spec.with_suffix('.lease.json')
    if lease_path.exists():
        lease = load(lease_path)
        if lease.get('spec') != str(spec.resolve()):
            raise ValueError('已有监督身份与 attempt 不一致，禁止补启动')
        identity = sample(lease['pid'])
        if identity['start_ticks'] != lease['start_ticks'] or identity['state'] == 'Z':
            raise ValueError('已有监督身份失效，禁止补启动')
        return {'lease': lease, 'child': None}
    # 两个 supervisor 即使同时出现，也只有拿到 attempt 锁的一个能执行任务。
    log = spec.with_suffix('.recovery.log')
    with log.open('a') as stream:
        child = subprocess.Popen([python, '-m', 'codesign_lab.evaluation.worker', str(spec)],
                                 cwd=ROOT, env=env, stdout=stream, stderr=stream, start_new_session=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result.exists():
            child.wait(timeout=5)
            return {'result': load(result)}
        if lease_path.exists():
            lease = load(lease_path)
            if lease.get('spec') != str(spec.resolve()):
                raise ValueError('恢复租约与 attempt 路径不一致')
            try:
                identity = sample(lease['pid'])
            except OSError:
                time.sleep(.05)
                continue
            if identity['start_ticks'] != lease['start_ticks'] or identity['state'] == 'Z':
                time.sleep(.05)
                continue
            if child.pid != lease['pid']: child.wait(timeout=5)
            return {'lease': lease, 'child': child if child.pid == lease['pid'] else None}
        time.sleep(.05)
    # 观察超时不意味着任务死亡；保留 supervisor，不盲目重启。
    raise RuntimeError('启动窗口恢复仍无可核对结果/租约，保留当前进程：' + str(spec))


def reap_orphan(spec):
    """监督身份已失效时，核对并终止其独立任务进程组。"""
    import os
    import signal
    from ..search.scheduler import atomic_json
    spec = Path(spec).resolve()
    document = load(spec)
    lease = load(spec.with_suffix('.lease.json'))
    if lease.get('spec') != str(spec) or lease.get('child_protocol') != 1:
        raise RuntimeError('旧任务缺少执行前子进程协议，不能自动重跑')
    try:
        supervisor = sample(lease['pid'])
    except FileNotFoundError:
        supervisor = None
    if supervisor and supervisor['start_ticks'] == lease['start_ticks'] and supervisor['state'] != 'Z':
        raise RuntimeError('监督进程仍存活，禁止孤儿收尾')
    result_path = Path(document['result'])
    if result_path.exists(): return load(result_path)
    child_path = spec.with_suffix('.child.json')
    if not child_path.exists() and spec.with_suffix('.permit.json').exists():
        raise RuntimeError('放行证据存在但子进程身份丢失，禁止重跑')
    if child_path.exists():
        child = load(child_path)
        if child.get('spec') != str(spec) or child['process_group'] != child['kill_pid']:
            raise RuntimeError('子进程租约不匹配，拒绝发送信号')
        try:
            current = sample(child['pid'])
        except FileNotFoundError:
            current = None
        if current and current['start_ticks'] != child['start_ticks']:
            raise RuntimeError('子进程 PID 已复用，拒绝发送信号')
        if current:
            status = (Path('/proc') / str(child['pid']) / 'status').read_text()
            namespace_pid = int(status.split('NSpid:')[1].splitlines()[0].split()[-1])
            if namespace_pid != child['kill_pid'] or os.readlink('/proc/' + str(child['pid']) + '/ns/pid') != os.readlink('/proc/self/ns/pid'):
                raise RuntimeError('子进程命名空间映射不匹配')
        def members():
            found = []
            for path in Path('/proc').iterdir():
                if not path.name.isdigit(): continue
                try: item = sample(int(path.name))
                except FileNotFoundError: continue
                if item['process_group'] == child['pid'] and item['state'] != 'Z':
                    if item['session_id'] != child['pid'] or item['start_ticks'] < child['start_ticks']:
                        raise RuntimeError('进程组成员身份不明确，拒绝发送信号')
                    found.append(item)
            return found
        for signum in [signal.SIGTERM, signal.SIGKILL]:
            if not members(): break
            try: os.killpg(child['kill_pid'], signum)
            except ProcessLookupError: pass
            deadline = time.monotonic() + 3
            while members() and time.monotonic() < deadline: time.sleep(.05)
        if members(): raise RuntimeError('孤儿进程组仍存活，禁止重跑')
    # 没有 child lease 则没有放行：迟到的 wrapper 检查到监督身份失效后自行退出。
    result = {'key': document['job']['key'], 'stage': document['job']['stage'],
              'status': 'infrastructure_failed', 'error': '监督进程失效，已确认任务进程组收尾',
              'wall_seconds': max(0, time.time() - lease['started_wall']), 'peak_rss_bytes': None,
              'wall_seconds_estimated': True,
              'recovered_orphan': True}
    atomic_json(result_path, result)
    return result


def supervisor_lost(spec):
    """只用身份确认监督进程退出，采样权限错误不会被当作退出。"""
    lease = load(Path(spec).with_suffix('.lease.json'))
    try:
        current = sample(lease['pid'])
    except FileNotFoundError:
        return True
    return current['start_ticks'] != lease['start_ticks'] or current['state'] == 'Z'
