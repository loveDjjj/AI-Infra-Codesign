#!/usr/bin/env python3
"""启动当前流水线；可指定本源码批次的初始目标，默认只检查计划。"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from codesign_lab.config import load, digest, verify_official
from codesign_lab.search.targets import TargetPool, epoch, validate_target
from codesign_lab.search.scheduler import atomic_json
from codesign_lab.search.settings import arguments


def busy(path):
    """用真实控制器文件锁判定存活，不依据可能复用的 PID。"""
    if not path.exists():
        return False
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--config', type=Path, default=ROOT / 'configs/pipeline.yaml')
    parser.add_argument('--target', type=Path, action='append', default=[])
    args = parser.parse_args()
    config = args.config.resolve()
    targets = [path.resolve() for path in args.target]
    settings = load(config)
    out = ROOT / settings['out']
    source = epoch()
    official = verify_official()
    pinned = {str(p): digest(p) for p in [config, *targets]}
    definitions = [validate_target(load(p), source, max_trials=settings['budget']['max_proposals'])[0] for p in targets]
    argv = arguments(config, execute=True)
    plan = {'model': settings['ai'].get('model'), 'reasoning_effort': settings['ai'].get('reasoning_effort'),
            'out': str(out), 'targets': [t['target_id'] for t in definitions],
            'initial_nominal_candidates': sum(t['max_trials'] for t in definitions),
            'source_epoch': source, 'input_hashes': pinned, 'budget': settings['budget'],
            'scheduler': settings['scheduler'], 'website_submission': False}
    if not args.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return
    out.mkdir(parents=True, exist_ok=True)
    with (out / 'launcher.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        atomic_json(out / 'launch-plan.json', plan)
        status = out / 'launch-state.json'
        gates = [ROOT / 'workspace/search/.pipeline.lock']
        queued_wall = time.time()
        try:
            while any(busy(gate) for gate in gates):
                if epoch() != source or any(digest(Path(name)) != sha for name, sha in pinned.items()):
                    raise ValueError('等待期间源码或启动输入改变，拒绝启动旧计划')
                atomic_json(status, {'status': 'WAITING_FOR_BENCHMARK_OR_PIPELINE',
                    'queued_wall': queued_wall, 'updated_wall': time.time(),
                    'reason': '保留正在运行的算法对照，不抢占其主控锁'})
                time.sleep(10)
            if epoch() != source or verify_official() != official:
                raise ValueError('启动时源码或官方身份改变')
            if any(digest(Path(name)) != sha for name, sha in pinned.items()):
                raise ValueError('启动配置或模型包装器身份改变')
            pool = TargetPool(out, source, max_proposals=settings['budget']['max_proposals'])
            for target in definitions:
                result = pool.apply({'request_id': 'initial-' + target['target_id'],
                    'command': {'op': 'add', 'target': target}})
                if result['status'] not in {'accepted', 'reused'}:
                    raise ValueError(result)
            env = os.environ.copy()
            env['PYTHONPATH'] = str(ROOT / 'src')
            env['OPENBLAS_NUM_THREADS'] = env['OMP_NUM_THREADS'] = '1'
            python = load(ROOT / 'configs/toolchain.yaml')['python']
            command = [python, '-m', 'codesign_lab.search.pipeline', *argv]
            if (out / 'identity.json').exists():
                command.append('--resume')
            atomic_json(status, {'status': 'STARTING', 'updated_wall': time.time(), 'command': command})
            with (out / 'controller.log').open('a') as log:
                result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=log)
            atomic_json(status, {'status': 'FINISHED' if result.returncode == 0 else 'FAILED',
                'exit_code': result.returncode, 'updated_wall': time.time()})
            if result.returncode:
                raise SystemExit(result.returncode)
        except Exception as exc:
            atomic_json(status, {'status': 'FAILED', 'error': type(exc).__name__ + ': ' + str(exc),
                'updated_wall': time.time()})
            raise


if __name__ == '__main__':
    main()
