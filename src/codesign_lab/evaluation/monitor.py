'只读采集 Linux 进程树；主机 CPU 和 I/O 不代表模拟芯片资源。'
import argparse
import json
import os
from pathlib import Path
import time

def sample(pid):
    root = Path('/proc') / str(pid)
    raw = (root / 'stat').read_text()
    fields = raw[raw.rfind(')') + 2:].split()
    result = {'timestamp': time.time(), 'pid': pid, 'state': fields[0],
              'cpu_seconds': (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK'),
              'start_ticks': int(fields[19]), 'rss_bytes': int(fields[21]) * os.sysconf('SC_PAGE_SIZE')}
    for name in ('io', 'wchan'):
        try:
            content = (root / name).read_text().strip()
            result[name] = {k: int(v) for k, v in (line.split(':', 1) for line in content.splitlines())} if name == 'io' else content
        except (OSError, ValueError):
            result[name] = None
    return result

def sample_tree(pid):
    primary=sample(pid);pending=[pid];members=[]
    while pending:
        target=pending.pop()
        try:
            item=sample(target);members.append(item)
            pending.extend(int(value) for value in (Path('/proc')/str(target)/'task'/str(target)/'children').read_text().split())
        except (FileNotFoundError,ProcessLookupError):pass
    primary['tree_cpu_seconds']=sum(item['cpu_seconds'] for item in members)
    primary['tree_rss_bytes']=sum(item['rss_bytes'] for item in members)
    primary['tree_pids']=[item['pid'] for item in members]
    return primary

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid', type=int, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--interval', type=float, default=5)
    p.add_argument('--seconds', type=float, default=0, help='0: until target exits')
    a = p.parse_args()
    if a.interval <= 0 or a.seconds < 0:
        p.error('Invalid monitoring interval/duration')
    a.out.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    previous = None
    identity = None
    with a.out.open('x') as log:
        while True:
            try:
                current = sample_tree(a.pid)
            except FileNotFoundError:
                log.write(json.dumps({'event': 'process_gone', 'pid': a.pid, 'timestamp': time.time()}) + '\n')
                break
            if identity is None:
                identity = current['start_ticks']
            if identity != current['start_ticks']:
                log.write(json.dumps({'event': 'pid_reused', 'pid': a.pid}) + '\n')
                break
            if previous:
                dt = current['timestamp'] - previous['timestamp']
                current['cpu_percent_one_core'] = 100 * (current['tree_cpu_seconds'] - previous['tree_cpu_seconds']) / dt if dt > 0 else None
                if current['io'] is not None and previous['io'] is not None:
                    current['io_delta'] = {k: current['io'][k] - previous['io'].get(k, 0) for k in current['io']}
            current['elapsed_monitor_seconds'] = time.monotonic() - started
            log.write(json.dumps(current) + '\n'); log.flush()
            latest = a.out.with_suffix('.latest.json')
            temporary = latest.with_suffix('.tmp')
            temporary.write_text(json.dumps(current, indent=2) + '\n'); temporary.replace(latest)
            previous = current
            if current['state'] == 'Z' or a.seconds and time.monotonic() - started >= a.seconds:
                break
            time.sleep(a.interval)

if __name__ == '__main__':
    main()
