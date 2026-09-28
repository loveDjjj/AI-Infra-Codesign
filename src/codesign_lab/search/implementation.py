"""结构提案的隔离实现与确定性验收；运行状态仅保存在 workspace。"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from ..config import ROOT, digest, load, verify_official
from ..records import read
from .scheduler import atomic_json, terminate
from .targets import epoch

CASES = {'p1': 'M1_P1', 'd1': 'M2_D1'}
ARTIFACTS = ('hardware.json', 'programs/M1_P1.asm', 'programs/M2_D1.asm')
TERMINAL = {'REJECTED', 'FAILED', 'LAUNCHED', 'AUDITED_NO_PROMOTION'}
CONTROLLER_FILES = ('src/codesign_lab/search/implementation.py', 'src/codesign_lab/ai_bridge.py')


def proposals(campaign: Path):
    """只接受此源码批次的已有决策；同一决策重复读取不会重复实现。"""
    state = load(campaign / 'state.json')
    source = state['source_epoch']
    ledger = ROOT / 'data/decisions.jsonl'
    for line in ledger.read_text().splitlines() if ledger.exists() else []:
        decision = json.loads(line)
        if decision.get('source_epoch') != source:
            continue
        for index, proposal in enumerate(decision['decision'].get('implementation_proposals', [])):
            lane = proposal.get('lane', '')
            if not (lane.startswith('p1') or lane.startswith('d1')):
                continue
            identifier = hashlib.sha256((decision['id'] + ':' + str(index)).encode()).hexdigest()[:20]
            yield identifier, {'id': identifier, 'decision_id': decision['id'],
                               'source_epoch': source, 'lane': lane,
                               'case': CASES['p1' if lane.startswith('p1') else 'd1'],
                               'proposal': proposal['proposal'],
                               'evidence_ids': proposal['evidence_ids']}


def campaign_generator_unchanged(campaign: Path):
    """基础设施升级可共存，结构实验必须从原批次同一生成器出发。"""
    pinned = load(campaign / 'identity.json')['source']
    paths = [name for name in pinned if name.startswith('src/codesign_lab/codegen/')]
    return bool(paths) and all((ROOT / name).is_file() and digest(ROOT / name) == pinned[name]
                               for name in paths)


def best_record():
    rows = [row for row in read() if row.get('scope') == 'full' and row.get('eligible') is True
            and row.get('audited') is True and isinstance(row.get('score'), (int, float))
            and row.get('reproduction') == 'verified']
    if not rows:
        raise ValueError('缺少已审计且可再生的整案基线')
    return max(rows, key=lambda row: row['score'])


def origin_root():
    return Path(os.environ.get('CODESIGN_ORIGIN_ROOT', str(ROOT))).resolve()


def audited_best_score():
    ledger = origin_root() / 'data/experiments.jsonl'
    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    scores = [row['score'] for row in rows if row.get('scope') == 'full' and
              row.get('eligible') is True and row.get('audited') is True and
              isinstance(row.get('score'), (int, float))]
    return max(scores) if scores else float('-inf')


def export_epoch_record(snapshot: Path, record_id: str, parent: dict):
    """把已晋升版本的原件写回主账本，不声称主源码可再生该版本。"""
    origin = origin_root()
    source = snapshot / 'data/releases' / record_id
    if not source.is_dir():
        raise ValueError('隔离晋升版本缺少 release 原件')
    target = origin / 'data/releases' / record_id
    if target.exists():
        if digest(target / 'local-grade.json') != digest(source / 'local-grade.json'):
            raise ValueError('主项目已有同 ID 不同内容的 release')
    else:
        shutil.copytree(source, target)
    rows = [json.loads(line) for line in (snapshot / 'data/experiments.jsonl').read_text().splitlines()
            if line.strip()]
    record = next(row for row in rows if row['id'] == record_id)
    if record.get('eligible') is not True or record.get('audited') is not True:
        raise ValueError('只能归档已审计合格的整案')
    exported = dict(record)
    source_files = {str(p.relative_to(snapshot)): digest(p)
                    for p in sorted((snapshot / 'src').rglob('*.py'))}
    exported.update(candidate=str(target.relative_to(origin)),
                    report=str((target / 'local-grade.json').relative_to(origin)),
                    audit_path=str((target / 'local-grade.audit.json').relative_to(origin)),
                    reproduction='epoch_verified',
                    source_epoch=hashlib.sha256(json.dumps(source_files, sort_keys=True).encode()).hexdigest(),
                    source_snapshot=str(snapshot), parent_source_epoch=parent['source_epoch'],
                    parent_decision=parent['decision_id'])
    ledger = origin / 'data/experiments.jsonl'
    with (origin / 'data/.experiments.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
        if not any(row['id'] == record_id for row in existing):
            with ledger.open('a') as stream:
                stream.write(json.dumps(exported, ensure_ascii=False) + '\n')
                stream.flush();os.fsync(stream.fileno())
    return exported


def project_env(snapshot: Path):
    env = os.environ.copy()
    env.update(PYTHONPATH=str(snapshot / 'src'), OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1',
               PYTHONDONTWRITEBYTECODE='1')
    env.setdefault('CODEX_SESSION_LOCK_ROOT', str(ROOT / 'workspace/ai-sessions'))
    env.setdefault('CODESIGN_PIPELINE_LOCK_PATH', str(ROOT / 'workspace/search/.pipeline.lock'))
    env.setdefault('CODESIGN_ORIGIN_ROOT', str(origin_root()))
    env.setdefault('CODESIGN_EVAL_CACHE_ROOT', str(origin_root() / 'workspace/search/cache'))
    env.pop('PYTHONHOME', None)
    safe = snapshot / '.git/safe-config'
    if safe.is_file():
        env['GIT_CONFIG_GLOBAL'] = str(safe)
    return env


def interpreter():
    path = Path(load(ROOT / 'configs/toolchain.yaml')['python'])
    if not path.is_file():
        raise ValueError('锁定解释器不存在')
    return str(path)


def run(snapshot: Path, argv: list[str], label: str, *, timeout: int):
    logs = snapshot / 'workspace/implementation-logs'
    logs.mkdir(parents=True, exist_ok=True)
    out = logs / (label + '.stdout.log')
    err = logs / (label + '.stderr.log')
    with out.open('w') as stdout, err.open('w') as stderr:
        child = subprocess.Popen(argv, cwd=snapshot, env=project_env(snapshot),
                                 stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            code = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            terminate(child)
            child.wait()
            raise TimeoutError(label + ' 超时') from None
    if code:
        raise RuntimeError(label + ' 失败；见 ' + str(err))
    return {'stdout': str(out), 'stderr': str(err)}


def run_regression(snapshot: Path):
    """隔离副本运行可迁移测试，排除绑定旧版本原件的历史锚点。"""
    code = '''import unittest,sys
from pathlib import Path
loader=unittest.TestLoader()
suite=loader.discover("tests",pattern="test_*.py")
historical=("test_contracts.",
 "test_d1_w2_groups.D1W2GroupChecks.test_supported_groups_change_only_d1_sync")
def missing_fixed_release(test):
    module=sys.modules.get(test.__class__.__module__)
    base=getattr(module,"BASE",None)
    return isinstance(base,Path) and not base.exists()
def filtered(group):
    result=unittest.TestSuite()
    for test in group:
        if isinstance(test,unittest.TestSuite):result.addTests(filtered(test))
        elif not test.id().startswith(historical) and not missing_fixed_release(test):result.addTest(test)
    return result
answer=unittest.TextTestRunner(verbosity=1).run(filtered(suite))
raise SystemExit(not answer.wasSuccessful())
'''
    return run(snapshot, [interpreter(), '-c', code], 'regression', timeout=900)


def inherit_controller_files(destination: Path):
    """仅继承主项目结构控制文件，绝不覆盖当前 epoch 的生成器。"""
    origin = origin_root()
    hashes = {}
    for name in CONTROLLER_FILES:
        source = origin / name
        if not source.is_file():
            raise ValueError('主项目缺少结构实验控制文件：' + name)
        if origin != ROOT:
            shutil.copy2(source, destination / name)
        hashes[name] = digest(destination / name)
    return hashes


def snapshot_project(destination: Path, baseline: dict):
    """快照复制当前工作树内容，不从可能过期的 Git HEAD 建树。"""
    if destination.exists():
        raise FileExistsError(destination)
    destination.mkdir(parents=True)
    for name in ('src', 'configs', 'tests', 'scripts', 'schemas', 'vendor/official'):
        shutil.copytree(ROOT / name, destination / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    # 跨 epoch 只刷新结构实验控制代码；算子生成器始终来自本批次固定源码。
    origin = origin_root()
    controller_hashes = inherit_controller_files(destination)
    for name in ('AGENTS.md', 'README.md', 'pyproject.toml', 'requirements.lock', 'lab', '.gitignore'):
        shutil.copy2(ROOT / name, destination / name)
    (destination / 'docs').mkdir()
    for path in (ROOT / 'docs').glob('*.md'):
        shutil.copy2(path, destination / 'docs' / path.name)
    (destination / 'data/releases').mkdir(parents=True)
    for name in ('experiments.jsonl', 'state.json'):
        shutil.copy2(ROOT / 'data' / name, destination / 'data' / name)
    release = Path(baseline['candidate'])
    release = release if release.is_absolute() else ROOT / release
    if not release.is_relative_to(ROOT / 'data/releases') or not release.is_dir():
        raise ValueError('基线必须有受保护的 release 目录')
    # 回归测试要求账本引用的受保护报告都可取回；副本是临时工作区。
    for source in sorted((ROOT / 'data/releases').iterdir()):
        if not source.is_dir():
            continue
        shutil.copytree(source, destination / 'data/releases' / source.name)
    for name in ('iteration-log.md', 'official-starter.zip'):
        if (ROOT / 'data' / name).exists():
            shutil.copy2(ROOT / 'data' / name, destination / 'data' / name)
    (destination / 'data/agent-trace/pipeline').mkdir(parents=True)
    trace_origin = origin_root() / 'data/agent-trace'
    for relative in ('pipeline/global', 'implementation'):
        target = trace_origin / relative
        target.mkdir(parents=True, exist_ok=True)
        link = destination / 'data/agent-trace' / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target, target_is_directory=True)
    (destination / 'workspace').mkdir()
    generator_hashes = {str(path.relative_to(ROOT)): digest(path)
                        for path in (ROOT / 'src/codesign_lab/codegen').rglob('*.py')}
    copied = {name: digest(destination / name) for name in generator_hashes}
    if generator_hashes != copied:
        raise ValueError('隔离生成器复制后哈希不一致')
    atomic_json(destination / 'workspace/source-lineage.json',
                {'generator_source': str(ROOT), 'generator_hashes': generator_hashes,
                 'controller_source': str(origin), 'controller_hashes': controller_hashes})
    run(destination, ['git', 'init', '-q'], 'git-init', timeout=30)
    (destination / '.git/safe-config').write_text('[safe]\n\tdirectory = ' + str(destination) + '\n')
    with (destination / '.git/info/exclude').open('a') as excluded:
        excluded.write('\n/data/\n/docs/dashboard.html\n')
    run(destination, ['git', 'add', 'src', 'configs', 'tests', 'scripts', 'schemas', 'vendor',
                      '.gitignore', 'AGENTS.md', 'README.md', 'pyproject.toml', 'requirements.lock', 'lab',
                      'docs'], 'git-add', timeout=60)
    run(destination, ['git', '-c', 'user.name=Implementation Bot',
                      '-c', 'user.email=implementation@localhost', 'commit', '-qm', '隔离源码基线'],
        'git-commit', timeout=60)
    return generator_hashes


def changed_files(snapshot: Path):
    result = subprocess.run(['git', 'status', '--porcelain', '-uall'], cwd=snapshot,
                            env=project_env(snapshot), text=True, capture_output=True, check=True)
    return [line[3:] for line in result.stdout.splitlines()]


def validate_diff(snapshot: Path):
    changed = changed_files(snapshot)
    if not changed:
        raise ValueError('AI 未修改任何源码')
    allowed = re.compile(r'(src/codesign_lab/codegen/.+\.py|tests/test_implementation_[A-Za-z0-9_]+\.py)\Z')
    invalid = [name for name in changed if not allowed.fullmatch(name)]
    if invalid:
        raise ValueError('AI 修改了隔离实验允许范围外的文件：' + ', '.join(invalid))
    if not any(name.startswith('src/codesign_lab/codegen/') for name in changed):
        raise ValueError('没有生成器结构修改')
    return changed


def artifacts(candidate: Path):
    return {name: digest(candidate / name) for name in ARTIFACTS}


def preserved_fields(baseline, candidate):
    """候选只能在目标配置中新增开关，不能暗改既有参数。"""
    if isinstance(baseline, dict):
        return isinstance(candidate, dict) and all(
            key in candidate and preserved_fields(value, candidate[key])
            for key, value in baseline.items())
    return baseline == candidate


def verify_snapshot_official(snapshot: Path):
    root = snapshot / 'vendor/official'
    manifest = load(root / 'isolation-manifest.json')
    for name, expected in manifest['files_sha256'].items():
        if digest(root / name) != expected:
            raise ValueError('隔离副本中的冻结官方文件被修改：' + name)


def coding_turn(snapshot: Path, campaign: Path, session_id: str, item: dict,
                baseline: dict, timeout: int, model: str, effort: str):
    """同一全局会话串行写入隔离副本；是否成功完全由后续关卡判定。"""
    import uuid
    uuid.UUID(session_id)
    lock_root = Path(os.environ.get('CODEX_SESSION_LOCK_ROOT',
        str(ROOT / 'workspace/ai-sessions')))
    lock_root.mkdir(parents=True, exist_ok=True)
    legacy = campaign / 'ai/global.session.lock'
    legacy.parent.mkdir(parents=True, exist_ok=True)
    with (lock_root / (session_id + '.session.lock')).open('a') as lock, legacy.open('a') as old_lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fcntl.flock(old_lock, fcntl.LOCK_EX)
        output = snapshot / 'workspace/implementation-logs/codex-final.txt'
        output.parent.mkdir(parents=True, exist_ok=True)
        by_id = {record['id']: record for record in read()}
        evidence = []
        for identifier in item['evidence_ids']:
            record = by_id.get(identifier)
            if record:
                evidence.append({'id': identifier, 'scope': record.get('scope'),
                    'score': record.get('score'), 'eligible': record.get('eligible'),
                    'cases': {case: {'functional_passed': info.get('functional_passed'),
                        'cycles': info.get('timing', {}).get('cycles'),
                        'peak_power_w': info.get('timing', {}).get('peak_window_power_w')}
                        for case, info in record.get('cases', {}).items()}})
        prompt = (
            '你正在实现独立源码 epoch 的一个结构实验。仅可修改本副本的 '
            'src/codesign_lab/codegen 下 Python 文件，必要时新增 tests/test_implementation_*.py。'
            '不得修改 vendor/official、主仓库、已有评分报告或上传网站；不得运行完整模拟器。'
            '只能参考本项目自己的源码、账本和报告，不得参考或复用其他参与者的作品或代理输出。'
            '如提案包含多个结构变换，本次只实现第一个可独立检验的变换，不把多个因素捆在一起。'
            '必须保留原算法作为默认行为，通过有默认值的配置开关启用新实现。'
            '请将完整候选设计 JSON 写到 workspace/implementation-input/config.json；'
            '该文件从基线配置复制，只打开你新增的开关。完成后说明改动。\n'
            '受影响案例：' + item['case'] + '\n结构提案：' + item['proposal'] + '\n'
            '本项目证据摘要：' + json.dumps(evidence, ensure_ascii=False) + '\n'
            '基线记录：' + baseline['id'] + '\n基线配置：' +
            json.dumps(baseline['config'], ensure_ascii=False) + '\n'
            '请先阅读本副本 AGENTS.md 和相关生成器源码。')
        executable = os.environ.get('CODEX_IMPLEMENTATION_CLI', 'codex')
        command = [executable, 'exec', '--sandbox', 'workspace-write', '-C', str(snapshot),
                   '-m', model, '-c', 'model_reasoning_effort="' + effort + '"',
                   'resume', session_id, '--json', '-o', str(output), '-']
        logs = origin_root() / 'data/agent-trace/implementation' / item['id']
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / 'codex.events.jsonl').open('w') as stdout, (logs / 'codex.stderr.log').open('w') as stderr:
            child = subprocess.Popen(command, cwd=snapshot, env=project_env(snapshot), stdin=subprocess.PIPE,
                                     stdout=stdout, stderr=stderr, text=True, start_new_session=True)
            try:
                child.communicate(prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                terminate(child)
                child.wait()
                raise TimeoutError('结构实现 Codex 回合超时') from None
        if child.returncode:
            raise RuntimeError('结构实现 Codex 回合失败，见受保护轨迹')
        events = [json.loads(line) for line in (logs / 'codex.events.jsonl').read_text().splitlines()
                  if line.startswith('{')]
        seen = {event.get('thread_id') for event in events if event.get('type') == 'thread.started'}
        if seen != {session_id} or not any(event.get('type') == 'turn.completed' for event in events):
            raise ValueError('Codex 恢复的会话身份或完成事件无效')
        return str(output)


def grade_record(snapshot: Path, report: Path):
    rows = [json.loads(line) for line in (snapshot / 'data/experiments.jsonl').read_text().splitlines() if line.strip()]
    matching = []
    for row in rows:
        if row.get('scope') != 'full' or row.get('eligible') is not True or not row.get('report'):
            continue
        path = Path(row['report'])
        path = path if path.is_absolute() else snapshot / path
        if path.resolve() == report.resolve():
            matching.append(row)
    if len(matching) != 1:
        raise ValueError('官方报告没有唯一合格账本记录')
    return matching[0]


def retry_infrastructure_failure(state_path: Path):
    """已完成编码的已知输出路径错误只重试确定性验证，不重放 AI。"""
    state = load(state_path)
    retryable = ('RuntimeError: default-off 失败', 'RuntimeError: regression 失败')
    if state.get('status') != 'FAILED' or not str(state.get('error', '')).startswith(retryable):
        raise ValueError('仅支持恢复已完成编码的复现或回归关卡失败')
    snapshot = Path(state['snapshot'])
    if not snapshot.is_dir() or not (snapshot / 'workspace/implementation-input/config.json').is_file():
        raise ValueError('缺少已经编码的隔离副本或候选配置')
    validate_diff(snapshot)
    state['recovered_error'] = state.pop('error')
    state.update(status='CODED', updated_wall=time.time())
    atomic_json(state_path, state)
    return state


def seed_next_campaign(snapshot: Path, item: dict, record_id: str, session_id: str):
    settings = load(snapshot / 'configs/pipeline-astra-global-v2.yaml')
    settings['out'] = 'workspace/pipeline/epoch-' + item['id']
    settings['watch'] = []
    settings['stay_open'] = False
    settings['stop_on_exhaustion'] = True
    settings['ai'] = {'enabled': True, 'mode': 'global', 'timeout_seconds': 900}
    settings['implementation'] = {'enabled': True, 'model': 'gpt-6-astra',
        'reasoning_effort': 'medium', 'max_proposals': 2,
        'min_case_gain': .002, 'min_score_gain': 100}
    settings_path = snapshot / 'configs/next-epoch.json'
    atomic_json(settings_path, settings)
    from .targets import DOMAINS
    source = hashlib.sha256(json.dumps({str(p.relative_to(snapshot)): digest(p)
        for p in sorted((snapshot / 'src').rglob('*.py'))}, sort_keys=True).encode()).hexdigest()
    targets = []
    for name in ('p1-preload.yaml', 'p1-attention.yaml', 'd1-w2-group.yaml', 'hw-neighborhood.yaml'):
        path = snapshot / 'configs/targets/astra-global-v2' / name
        if not path.exists():
            continue
        target = load(path)
        target.update(target_id='epoch-' + item['id'][:8] + '-' + target['lane'],
                      source_epoch=source, base_record=record_id, evidence_ids=[record_id])
        target['variables'] = {key: values for key, values in target['variables'].items() if key in DOMAINS}
        if target['variables']:
            targets.append(target)
    if not targets:
        raise ValueError('新源码批次没有可验证的初始目标')
    payload = snapshot / 'workspace/next-epoch-targets.json'
    atomic_json(payload, {'targets': targets, 'session_id': session_id, 'out': settings['out']})
    code = (
        'import json,sys;from pathlib import Path;'
        'from codesign_lab.config import ROOT;'
        'from codesign_lab.search.targets import TargetPool,epoch;'
        'from codesign_lab.search.triggers import Triggers;'
        'd=json.loads(Path(sys.argv[1]).read_text());p=TargetPool(ROOT/d["out"],epoch(),max_proposals=96);'
        '[(lambda r: (_ for _ in ()).throw(ValueError(r)) if r["status"] not in ("accepted","reused") else None)'
        '(p.apply({"request_id":"seed-"+t["target_id"],"command":{"op":"add","target":t}})) for t in d["targets"]];'
        'Triggers(p.state).lane("global")["session_id"]=d["session_id"];p.save()')
    run(snapshot, [interpreter(), '-c', code, str(payload)], 'seed-next', timeout=60)
    return settings_path


def launch_next(snapshot: Path, settings_path: Path):
    global_lock = Path(os.environ.get('CODESIGN_PIPELINE_LOCK_PATH',
        str(ROOT / 'workspace/search/.pipeline.lock')))
    global_lock.parent.mkdir(parents=True, exist_ok=True)
    with global_lock.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
    log = snapshot / 'workspace/next-epoch-controller.log'
    with log.open('a') as stream:
        child = subprocess.Popen([interpreter(), '-m', 'codesign_lab.cli', 'pipeline',
                                  str(settings_path), '--execute'], cwd=snapshot,
                                 env=project_env(snapshot), stdout=stream, stderr=stream,
                                 start_new_session=True)
    time.sleep(2)
    if child.poll() is not None and child.returncode:
        raise RuntimeError('新源码批次启动失败，见 ' + str(log))
    return {'pid': child.pid, 'log': str(log)}


class ImplementationLoop:
    def __init__(self, campaign: Path, *, model='gpt-6-astra', effort='medium',
                 min_case_gain=0.002, min_score_gain=100, code_timeout=1800):
        self.campaign = campaign
        self.directory = ROOT / 'workspace/implementation-loop' / campaign.name
        self.directory.mkdir(parents=True, exist_ok=True)
        self.model = model
        self.effort = effort
        self.min_case_gain = min_case_gain
        self.min_score_gain = min_score_gain
        self.code_timeout = code_timeout

    def finish_graded(self, item, state, save):
        """官方评分已完成后可重入；绝不因审计或交接失败重新跑整包。"""
        snapshot = Path(state['snapshot'])
        report = Path(state['report'])
        grade = load(report)
        if grade.get('eligible') is not True:
            save('REJECTED', reason='官方整案不合格', official_score=grade.get('experimental_score'))
            return state
        record = grade_record(snapshot, report)
        if not record.get('audited'):
            run(snapshot, [interpreter(), '-m', 'codesign_lab.cli', 'audit', record['id']],
                'audit', timeout=600)
        current_best = state.get('comparison_score')
        if current_best is None:
            current_best = audited_best_score()
            save('GRADED', comparison_score=current_best)
        score = grade['experimental_score']
        if score < current_best + self.min_score_gain:
            save('AUDITED_NO_PROMOTION', official_score=score, current_best=current_best,
                 audit_record=record['id'])
            return state
        isolated_state = load(snapshot / 'data/state.json')
        if isolated_state.get('promoted_record') != record['id']:
            run(snapshot, [interpreter(), '-m', 'codesign_lab.cli', 'promote', record['id']],
                'promote', timeout=600)
        export_epoch_record(snapshot, record['id'], item)
        settings = seed_next_campaign(snapshot, item, record['id'], state['session_id'])
        save('WAITING_FOR_LAUNCH', next_settings=str(settings), official_score=score,
             audit_record=record['id'])
        launch = launch_next(snapshot, settings)
        if launch is not None:
            save('LAUNCHED', launch=launch)
        return state

    def process(self, item: dict):
        state_path = self.directory / item['id'] / 'state.json'
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state = load(state_path) if state_path.exists() else {'status': 'QUEUED', 'proposal': item}
        if state['status'] in TERMINAL:
            return state
        if state['status'] not in {'QUEUED', 'CODED', 'GRADED', 'WAITING_FOR_LAUNCH'}:
            # 崩溃时不重放可能仍在运行的同一 AI 会话或昂贵官方调用。
            return state
        def save(status, **fields):
            state.update(status=status, updated_wall=time.time(), **fields)
            atomic_json(state_path, state)
        if state['status'] == 'WAITING_FOR_LAUNCH':
            launch = launch_next(Path(state['snapshot']), Path(state['next_settings']))
            if launch is not None:
                save('LAUNCHED', launch=launch)
            return state
        if state['status'] == 'GRADED':
            try:
                return self.finish_graded(item, state, save)
            except Exception as exc:
                save('FAILED', error=type(exc).__name__ + ': ' + str(exc))
                return state
        if not campaign_generator_unchanged(self.campaign):
            raise ValueError('提案所属生成器源码已改变')
        verify_official()
        lane = load(self.campaign / 'state.json')['analysis']['lanes'].get('global', {})
        session_id = lane.get('session_id')
        if not session_id:
            return state
        baseline = next((row for row in read() if row['id'] == state.get('baseline_id')),
                        None) if state['status'] == 'CODED' else best_record()
        if baseline is None:
            raise ValueError('已编码实验的基线记录不可取回')
        snapshot = state_path.parent / 'source'
        try:
            base_config = snapshot / 'workspace/implementation-baseline.json'
            source_release = Path(baseline['candidate']).name
            baseline_build = snapshot / 'workspace/implementation-builds/baseline'
            if state['status'] == 'QUEUED':
                save('SNAPSHOTTING', baseline_id=baseline['id'])
                original_hashes = snapshot_project(snapshot, baseline)
                if original_hashes != {str(p.relative_to(ROOT)): digest(p)
                                       for p in (ROOT / 'src/codesign_lab/codegen').rglob('*.py')}:
                    raise ValueError('快照期间生成器源码发生变化')
                atomic_json(base_config, baseline['config'])
                official_hash = digest(ROOT / 'vendor/official/isolation-manifest.json')
                ledger_hash = digest(snapshot / 'data/experiments.jsonl')
                release_report_hash = digest(snapshot / 'data/releases' / source_release / 'local-grade.json')
                run(snapshot, [interpreter(), '-m', 'codesign_lab.cli', 'build', str(base_config),
                    '--out', str(baseline_build), '--verify', str(snapshot / 'data/releases' / source_release)],
                    'baseline-build', timeout=300)
                save('CODING', snapshot=str(snapshot))
                coding_turn(snapshot, self.campaign, session_id, item, baseline,
                            self.code_timeout, self.model, self.effort)
                if original_hashes != {str(p.relative_to(ROOT)): digest(p)
                                       for p in (ROOT / 'src/codesign_lab/codegen').rglob('*.py')}:
                    raise ValueError('编码回合修改了原批次生成器源码')
                if digest(ROOT / 'vendor/official/isolation-manifest.json') != official_hash or \
                        digest(snapshot / 'data/experiments.jsonl') != ledger_hash or \
                        digest(snapshot / 'data/releases' / source_release / 'local-grade.json') != release_report_hash:
                    raise ValueError('编码回合修改了冻结依据或实验账本')
                save('CODED')
            elif not all((baseline_build / name).is_file() for name in ARTIFACTS):
                # 旧控制器把输出放在 workspace 根目录，恢复时重建到稳定的子目录。
                run(snapshot, [interpreter(), '-m', 'codesign_lab.cli', 'build', str(base_config),
                    '--out', str(baseline_build), '--verify',
                    str(snapshot / 'data/releases' / source_release)],
                    'baseline-build-recovery', timeout=300)
            verify_official();verify_snapshot_official(snapshot)
            changed = validate_diff(snapshot)
            config_path = snapshot / 'workspace/implementation-input/config.json'
            if not config_path.is_file():
                raise ValueError('AI 未生成候选 config.json')
            config = load(config_path)
            if config.get('hardware') != baseline['config'].get('hardware'):
                raise ValueError('本阶段结构实验不得改变硬件')
            unaffected = 'M2_D1' if item['case'] == 'M1_P1' else 'M1_P1'
            if config['programs'][unaffected] != baseline['config']['programs'][unaffected]:
                raise ValueError('候选同时改变了非目标案例')
            original_target = baseline['config']['programs'][item['case']]
            if not preserved_fields(original_target, config['programs'][item['case']]) or \
                    original_target == config['programs'][item['case']]:
                raise ValueError('目标配置必须仅新增结构开关，保留既有参数')
            save('VALIDATING', changed_files=changed)
            default_off = snapshot / 'workspace/implementation-builds/default-off'
            if all((default_off / name).is_file() for name in ARTIFACTS):
                if artifacts(default_off) != artifacts(baseline_build):
                    raise ValueError('已有默认配置产物与基线字节不一致')
            else:
                run(snapshot, [interpreter(), '-m', 'codesign_lab.cli', 'build', str(base_config),
                    '--out', str(default_off), '--verify', str(baseline_build)],
                    'default-off', timeout=300)
            run_regression(snapshot)
            candidate = snapshot / 'workspace/implementation-builds/candidate'
            run(snapshot, [interpreter(), '-m', 'codesign_lab.cli', 'build', str(config_path),
                '--out', str(candidate)], 'candidate-build', timeout=300)
            before, after = artifacts(baseline_build), artifacts(candidate)
            if after['hardware.json'] != before['hardware.json'] or \
                    after['programs/' + unaffected + '.asm'] != before['programs/' + unaffected + '.asm']:
                raise ValueError('候选改变硬件或非目标 ASM')
            if after['programs/' + item['case'] + '.asm'] == before['programs/' + item['case'] + '.asm']:
                raise ValueError('结构开关没有改变目标 ASM')
            save('FUNCTIONAL', artifact_sha256=after)
            functional = snapshot / 'workspace/implementation-reports/functional.json'
            run(snapshot, [interpreter(), '-m', 'codesign_lab.cli', 'run', str(candidate), '--level',
                'functional', '--case', item['case'], '--seed', '7', '--seed', '123', '--out', str(functional)],
                'functional', timeout=3600)
            info = load(functional)['cases'][item['case']]
            if info.get('functional_passed') is not True:
                raise ValueError('功能检查没有通过')
            save('ESTIMATING')
            estimate = snapshot / 'workspace/implementation-reports/estimate.json'
            cache = Path(os.environ.get('CODESIGN_EVAL_CACHE_ROOT',
                                        str(origin_root() / 'workspace/search/cache'))).resolve()
            run(snapshot, [interpreter(), '-m', 'codesign_lab.evaluation.runner', str(candidate),
                '--mode', 'estimate', '--case', item['case'], '--functional-report', str(functional),
                '--seed', '7', '--seed', '123', '--cache-dir', str(cache), '--out', str(estimate)],
                'estimate', timeout=3600)
            timing = load(estimate)['cases'][item['case']]['timing']
            baseline_timing = load(snapshot / 'data/releases' / source_release / 'local-grade.json')['cases'][item['case']]['timing']
            gain = (baseline_timing['cycles'] - timing['cycles']) / baseline_timing['cycles']
            if timing['peak_window_power_w'] > 20 or gain < self.min_case_gain:
                save('REJECTED', reason='单案收益或功耗未达门槛', case_gain=gain, timing=timing)
                return state
            save('OFFICIAL', case_gain=gain,
                 timing={'cycles': timing['cycles'], 'peak_window_power_w': timing['peak_window_power_w']})
            report = snapshot / 'workspace/implementation-reports/official.json'
            run(snapshot, [interpreter(), '-m', 'codesign_lab.cli', 'run', str(candidate),
                '--level', 'full', '--out', str(report)], 'official', timeout=7200)
            save('GRADED', report=str(report), official_score=load(report).get('experimental_score'),
                 session_id=session_id)
            return self.finish_graded(item, state, save)
        except Exception as exc:
            save('FAILED', error=type(exc).__name__ + ': ' + str(exc))
            return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign', required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--poll-seconds', type=int, default=30)
    parser.add_argument('--max-proposals', type=int, default=2)
    parser.add_argument('--proposal-id', help='仅运行指定结构提案，便于复核单个方向')
    parser.add_argument('--retry-failed', action='store_true',
                        help='恢复已完成编码但在默认配置复现阶段失败的提案')
    parser.add_argument('--min-case-gain', type=float, default=.002)
    parser.add_argument('--min-score-gain', type=float, default=100)
    parser.add_argument('--model', default='gpt-6-astra')
    parser.add_argument('--reasoning-effort', default='medium')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', args.campaign):
        parser.error('campaign 名称无效')
    if args.poll_seconds < 1 or args.max_proposals < 1 or not 0 <= args.min_case_gain < 1 or args.min_score_gain < 0:
        parser.error('预算或门槛无效')
    campaign = ROOT / 'workspace/pipeline' / args.campaign
    if not (campaign / 'state.json').is_file():
        parser.error('找不到批次状态')
    loop = ImplementationLoop(campaign, model=args.model, effort=args.reasoning_effort,
                              min_case_gain=args.min_case_gain, min_score_gain=args.min_score_gain)
    selected = [(identifier,item) for identifier,item in proposals(campaign)
                if args.proposal_id is None or identifier == args.proposal_id]
    if args.proposal_id and not selected:
        parser.error('指定的提案不属于该批次')
    if args.retry_failed and (not args.execute or not args.proposal_id):
        parser.error('--retry-failed 需要 --execute 和 --proposal-id')
    if not args.execute:
        print(json.dumps({'mode': '计划', 'campaign': args.campaign,
            'proposals': selected[:args.max_proposals]}, ensure_ascii=False, indent=2))
        return 0
    with (loop.directory / 'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.retry_failed:
            retry_infrastructure_failure(loop.directory / args.proposal_id / 'state.json')
        while True:
            count = 0
            current = [(identifier,item) for identifier,item in proposals(campaign)
                       if args.proposal_id is None or identifier == args.proposal_id]
            for _, item in current:
                if count >= args.max_proposals:
                    break
                state = loop.process(item)
                if state['status'] not in TERMINAL:
                    break
                count += 1
                print(json.dumps({'proposal_id': item['id'], 'status': state['status']}, ensure_ascii=False), flush=True)
                if state['status'] == 'LAUNCHED':
                    return 0
            if count >= args.max_proposals:
                return 0
            if not args.watch:
                break
            # 搜索主控已落盘总结、且已知提案均处理完后，监视进程不再空转。
            if (campaign / 'summary.json').is_file() and all(
                    (loop.directory / identifier / 'state.json').is_file() and
                    load(loop.directory / identifier / 'state.json').get('status') in TERMINAL
                    for identifier, _ in current):
                return 0
            time.sleep(args.poll_seconds)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
