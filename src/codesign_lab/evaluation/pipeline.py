"""准备冻结环境的逐案评估任务，主控统一登记结果。"""
import os
from pathlib import Path
import subprocess

from ..config import ROOT, load, digest, reference


def runtime():
    python = Path(load(ROOT / 'configs/toolchain.yaml')['python'])
    if not python.is_file():
        raise ValueError('锁定的评估解释器不存在')
    env = os.environ.copy()
    env.pop('PYTHONHOME', None)
    env.update(PYTHONPATH=str(ROOT / 'src'), OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1',
               PYTHONDONTWRITEBYTECODE='1')
    checked = subprocess.run([str(python), '-c',
        'import sys,numpy; assert sys.version_info[:2]==(3,12); assert numpy.__version__=="2.5.3"'],
        env=env, capture_output=True, text=True)
    if checked.returncode:
        raise ValueError('评估 Python/NumPy 版本不符：' + checked.stderr)
    return str(python), env


def cache_root():
    """源码 epoch 可共享精确评估缓存；路径必须属于主工程工作区。"""
    configured = os.environ.get('CODESIGN_EVAL_CACHE_ROOT')
    if not configured:
        return ROOT / 'workspace/search/cache'
    path = Path(configured)
    origin = Path(os.environ.get('CODESIGN_ORIGIN_ROOT', ROOT)).resolve()
    if not path.is_absolute() or not path.resolve().is_relative_to(origin / 'workspace'):
        raise ValueError('共享评估缓存必须是主工程 workspace 内的绝对路径')
    return path.resolve()


def task(python, candidate, case, seeds, report, key, memory_bytes, cache=True, *, mode="both", functional_report=None):
    command = [python, '-m', 'codesign_lab.evaluation.runner', str(candidate),
               '--mode', mode, '--case', case, '--out', str(report)]
    if mode not in {'functional', 'estimate', 'both'}:
        raise ValueError('未知评估阶段')
    if functional_report is not None:
        if mode != 'estimate':
            raise ValueError('功能依赖仅供时序阶段使用')
        command += ['--functional-report', str(functional_report)]
    if cache:
        command += ['--cache-dir', str(cache if isinstance(cache, Path) else cache_root())]
    for seed in seeds:
        command += ['--seed', str(seed)]
    return {'key': key, 'command': command, 'cwd': str(ROOT), 'memory_bytes': memory_bytes}


def compact_cases(cases):
    """账本保留可比较指标；大型资源统计继续只存于原始报告。"""
    compact = {}
    for case, info in cases.items():
        entry = {key: value for key, value in info.items() if key != 'resource_stats'}
        if isinstance(info.get('timing'), dict):
            entry['timing'] = {key: value for key, value in info['timing'].items()
                               if key != 'resource_stats'}
        compact[case] = entry
    return compact


def record_result(identifier, campaign, candidate, case, report, execution):
    from ..records import append, read
    from .profile import summarize
    if identifier in {record['id'] for record in read()}:
        return
    data = load(report) if report.exists() else {}
    info = data.get('cases', {}).get(case, {})
    timing = info.get('timing', {})
    if data:
        expected = digest(candidate / 'programs' / (case + '.asm'))
        if data.get('program_sha256', {}).get(case) != expected:
            raise ValueError('报告的程序哈希不属于本次候选')
        if data.get('hardware') != load(candidate / 'hardware.json'):
            raise ValueError('报告的硬件不属于本次候选')
    profile = summarize(timing) if 'resource_stats' in timing else {}
    append({'id': identifier, 'campaign': campaign, 'scope': 'both',
        'cases': compact_cases({case: info}),
        'profile': {case: {key: value for key, value in profile.items() if key != 'timeline'}},
        'config': load(candidate / 'config.json'), 'report': reference(report) if report.exists() else None,
        'candidate': reference(candidate), 'provenance': data.get('provenance'),
        'eligible': None, 'score': None, 'audited': False, 'reproduction': 'record_only',
        'host_seconds': execution['wall_seconds'], 'execution': execution,
        'failure_kind': 'design' if info.get('functional_passed') is False
                        else 'infrastructure' if info.get('error') or execution['status'] != 'completed' else None})
