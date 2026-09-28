"""结构提案的隔离实现与确定性验收；运行状态仅保存在 workspace。"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tarfile
import time

from ..config import ROOT, digest, load, verify_official
from ..records import read
from .scheduler import atomic_json, terminate
from .targets import epoch

CASES = {'p1': 'M1_P1', 'd1': 'M2_D1'}
ARTIFACTS = ('hardware.json', 'programs/M1_P1.asm', 'programs/M2_D1.asm')
TERMINAL = {'REJECTED', 'FAILED', 'LAUNCHED', 'AUDITED_NO_PROMOTION', 'BUDGET_EXHAUSTED',
            'TARGET_QUEUED', 'TARGET_INJECTED', 'RESEARCH_PAUSED'}
# 只控制自动追加三个昂贵邻域点；结果和来源仍入账，AI 可以按机制重新提案。
RESEARCH_GAIN_FLOOR = -0.02
CONTROLLER_FILES = ('src/codesign_lab/search/implementation.py', 'src/codesign_lab/ai_bridge.py')


def proposals(campaign: Path):
    """只接受此源码批次的已有决策；相同结构机制只编码一次。"""
    state = load(campaign / 'state.json')
    source = state['source_epoch']
    ledger = ROOT / 'data/decisions.jsonl'
    seen = set()
    for line in ledger.read_text().splitlines() if ledger.exists() else []:
        decision = json.loads(line)
        if decision.get('source_epoch') != source:
            continue
        for index, proposal in enumerate(decision['decision'].get('implementation_proposals', [])):
            lane = proposal.get('lane', '')
            if not (lane.startswith('p1') or lane.startswith('d1')):
                continue
            case = CASES['p1' if lane.startswith('p1') else 'd1']
            # 明确的 transformation_id 优先；旧决策退化为规范化后的原文精确去重。
            # 不用模糊相似度丢弃不同假设。
            transformation = proposal.get('transformation_id') or ' '.join(proposal['proposal'].split())
            key = (source, case, transformation)
            if key in seen:
                continue
            seen.add(key)
            identifier = hashlib.sha256((decision['id'] + ':' + str(index)).encode()).hexdigest()[:20]
            yield identifier, {'id': identifier, 'decision_id': decision['id'],
                               'source_epoch': source, 'lane': lane,
                               'case': case, 'transformation_id': transformation,
                               'proposal': proposal['proposal'],
                               'evidence_ids': proposal['evidence_ids'],
                               'evidence_snapshot': decision.get('evidence_snapshot', {})}


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


def source_reference(snapshot: Path, origin: Path):
    """主工程内记录相对路径；隔离测试路径保留绝对身份。"""
    try:
        return str(snapshot.relative_to(origin))
    except ValueError:
        return str(snapshot)


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
                    source_snapshot=str(snapshot), source_root=source_reference(snapshot, origin),
                    source_sha256=hashlib.sha256(json.dumps(source_files, sort_keys=True).encode()).hexdigest(),
                    parent_source_epoch=parent['source_epoch'],
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
        managed = os.environ.get('CODESIGN_STRUCTURE_WORKER') == '1'
        child = subprocess.Popen(argv, cwd=snapshot, env=project_env(snapshot),
                                 stdout=stdout, stderr=stderr, start_new_session=not managed)
        try:
            code = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop_stage(child)
            child.wait()
            raise TimeoutError(label + ' 超时') from None
    if code:
        raise RuntimeError(label + ' 失败；见 ' + str(err))
    return {'stdout': str(out), 'stderr': str(err)}


def stop_stage(child):
    """统一监督模式下终止整个结构任务进程组，避免留下昂贵孤儿仿真。"""
    if os.environ.get('CODESIGN_STRUCTURE_WORKER') == '1':
        os.killpg(os.getpgrp(), signal.SIGTERM)
        return
    terminate(child)


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
    """继承主项目非生成器控制代码和校验模式，保留 epoch 的算子实现。"""
    origin = origin_root()
    hashes = {}
    for name in controller_files(origin):
        source = origin / name
        if not source.is_file():
            raise ValueError('主项目缺少结构实验控制文件：' + name)
        if origin != ROOT:
            (destination / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination / name)
        hashes[name] = digest(destination / name)
    return hashes


def controller_files(origin: Path):
    source_root = origin / 'src/codesign_lab'
    names = {str(path.relative_to(origin)) for path in source_root.rglob('*.py')
             if not path.is_relative_to(source_root / 'codegen')}
    names.update(str(path.relative_to(origin)) for path in (origin / 'schemas').glob('*.json'))
    names.update(CONTROLLER_FILES)
    return sorted(names)


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
    # 结构验证只需当前基线与固定 joint28 测试锚点；其余历史 release 留在主工程。
    required_releases = {release, ROOT / 'data/releases/joint28'}
    for source in sorted(required_releases):
        if source.is_dir():
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
    (destination / 'workspace/pipeline').mkdir(parents=True)
    (destination / 'workspace/families').mkdir(parents=True)
    search_python = load(ROOT / 'configs/search-toolchain.yaml')['python']
    search_root = Path(search_python).parts[0:2]
    if tuple(search_root) != ('workspace', 'search-env'):
        raise ValueError('独立搜索环境路径与隔离快照约定不一致')
    installed_search = ROOT / 'workspace/search-env'
    if not (installed_search / 'bin/python').is_file():
        raise ValueError('主工程缺少锁定的独立搜索环境')
    (destination / 'workspace/search-env').symlink_to(installed_search, target_is_directory=True)
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


def restore_release_generator(destination: Path, release: Path):
    """只恢复已审计 release 中与 build.json 哈希完全匹配的算子源码。"""
    expected = load(release / 'build.json')['source_sha256']
    prefix = 'src/codesign_lab/codegen/'
    if not expected or any(not name.startswith(prefix) or not re.fullmatch(r'[A-Za-z0-9_./-]+\.py', name)
                           or '..' in Path(name).parts for name in expected):
        raise ValueError('release 的生成器源码清单无效')
    archive = release / 'generator-source.tar.gz'
    sources = {}
    with tarfile.open(archive, 'r:gz') as bundle:
        for member in bundle:
            if member.name not in expected:
                continue
            if not member.isfile() or member.name in sources:
                raise ValueError('生成器归档含重复或非普通文件')
            stream = bundle.extractfile(member)
            if stream is None or member.size > 4 * 1024**2:
                raise ValueError('生成器归档文件无效或过大')
            content = stream.read(4 * 1024**2 + 1)
            if hashlib.sha256(content).hexdigest() != expected[member.name]:
                raise ValueError('生成器归档与 release 构建清单不一致：' + member.name)
            sources[member.name] = content
    if set(sources) != set(expected):
        raise ValueError('生成器归档缺少构建清单文件')
    target = destination / prefix
    if target.exists():
        shutil.rmtree(target)
    for name, content in sources.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return expected


def restore_family(record_id: str, destination: Path):
    """从受保护整案恢复冻结实现族，并立即逐字节验证三个提交产物。"""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', record_id):
        raise ValueError('release ID 无效')
    release = ROOT / 'data/releases' / record_id
    record = next((row for row in read() if row['id'] == record_id), None)
    if not record or record.get('scope') != 'full' or record.get('eligible') is not True or \
            record.get('audited') is not True or not release.is_dir() or \
            load(release / 'local-grade.audit.json').get('audit_passed') is not True:
        raise ValueError('仅能恢复有受保护原件的已审计合格整案')
    candidate = destination / 'workspace/builds/anchor-build'
    if destination.exists():
        expected_source = load(release / 'build.json')['source_sha256']
        expected_artifacts = load(release / 'build.json')['sha256']
        family_manifest = destination / 'workspace/family.json'
        family_records = destination / 'data/experiments.jsonl'
        restored_record = next((row for row in (json.loads(line) for line in family_records.read_text().splitlines())
                                if row.get('id') == record_id), None) if family_records.is_file() else None
        if not (destination / '.git').is_dir() or not (destination / 'configs/anchor.json').is_file() or \
                not (destination / 'configs/best.yaml').is_file() or \
                not (destination / 'data/state.json').is_file() or \
                digest(destination / 'configs/anchor.json') != digest(release / 'config.json') or \
                digest(destination / 'configs/best.yaml') != digest(release / 'config.json') or \
                load(destination / 'data/state.json').get('promoted_record') != record_id or \
                not restored_record or restored_record.get('reproduction') != 'verified' or \
                not family_manifest.is_file() or load(family_manifest).get('record_id') != record_id or \
                any(not (destination / name).is_file() or digest(destination / name) != value
                    for name, value in expected_source.items()) or \
                any(not (destination / name).is_file() or digest(destination / name) != digest(ROOT / name)
                    for name in controller_files(ROOT)) or \
                not all((candidate / name).is_file() for name in ARTIFACTS) or \
                artifacts(candidate) != expected_artifacts:
            raise ValueError('已存在的实现族目录未通过源码或产物身份核对')
        return {'record_id': record_id, 'source': str(destination),
                'artifact_sha256': artifacts(candidate), 'reused': True}
    snapshot_project(destination, {'candidate': str(release.relative_to(ROOT))})
    restore_release_generator(destination, release)
    config = destination / 'configs/anchor.json'
    shutil.copy2(release / 'config.json', config)
    run(destination, ['git', 'add', 'src/codesign_lab/codegen', 'configs/anchor.json'],
        'anchor-add', timeout=30)
    run(destination, ['git', '-c', 'user.name=Implementation Bot',
                      '-c', 'user.email=implementation@localhost', 'commit', '-qm',
                      '恢复已审计算子源码'], 'anchor-commit', timeout=30)
    run(destination, [interpreter(), '-m', 'codesign_lab.cli', 'build', str(config),
                      '--out', str(candidate), '--verify', str(destination / 'data/releases' / record_id)],
        'anchor-build', timeout=300)
    shutil.copy2(release / 'config.json', destination / 'configs/best.yaml')
    family_state = load(destination / 'data/state.json')
    family_state['promoted_record'] = record_id
    atomic_json(destination / 'data/state.json', family_state)
    ledger = destination / 'data/experiments.jsonl'
    records = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    restored = next((row for row in records if row['id'] == record_id), None)
    if restored is None:
        raise ValueError('隔离账本缺少已审计 release 记录')
    restored['reproduction'] = 'verified'
    restored['verified_from_release'] = record_id
    ledger.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in records))
    run(destination, ['git', 'add', 'configs/best.yaml'], 'best-add', timeout=30)
    run(destination, ['git', '-c', 'user.name=Implementation Bot',
                      '-c', 'user.email=implementation@localhost', 'commit', '-qm',
                      '设置已审计实现族基线'], 'best-commit', timeout=30)
    generator_hashes = load(release / 'build.json')['source_sha256']
    control_hashes = {name: digest(destination / name) for name in controller_files(ROOT)}
    atomic_json(destination / 'workspace/source-lineage.json',
                {'generator_source': str(release), 'generator_hashes': generator_hashes,
                 'controller_source': str(ROOT), 'controller_hashes': control_hashes})
    atomic_json(destination / 'workspace/family.json', {
        'record_id': record_id, 'generator_sha256': generator_hashes,
        'controller_sha256': control_hashes, 'artifact_sha256': artifacts(candidate),
        'source_archive_sha256': digest(release / 'generator-source.tar.gz'),
        'official_manifest_sha256': digest(destination / 'vendor/official/isolation-manifest.json')})
    return {'record_id': record_id, 'source': str(destination),
            'artifact_sha256': artifacts(candidate), 'reused': False}


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
    """每个结构提案使用独立编码会话；全局规划会话保持可用。"""
    import uuid
    uuid.UUID(session_id)
    lock_root = Path(os.environ.get('CODEX_SESSION_LOCK_ROOT',
        str(ROOT / 'workspace/ai-sessions')))
    lock_root.mkdir(parents=True, exist_ok=True)
    with (lock_root / ('coder-' + item['id'] + '.session.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
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
            elif identifier in item.get('evidence_snapshot', {}):
                evidence.append(item['evidence_snapshot'][identifier])
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
                   '--json', '-o', str(output), '-']
        logs = origin_root() / 'data/agent-trace/implementation' / item['id']
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / 'codex.events.jsonl').open('w') as stdout, (logs / 'codex.stderr.log').open('w') as stderr:
            child = subprocess.Popen(command, cwd=snapshot, env=project_env(snapshot), stdin=subprocess.PIPE,
                                     stdout=stdout, stderr=stderr, text=True,
                                     start_new_session=os.environ.get('CODESIGN_STRUCTURE_WORKER') != '1')
            try:
                child.communicate(prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                stop_stage(child)
                child.wait()
                raise TimeoutError('结构实现 Codex 回合超时') from None
        if child.returncode:
            raise RuntimeError('结构实现 Codex 回合失败，见受保护轨迹')
        events = [json.loads(line) for line in (logs / 'codex.events.jsonl').read_text().splitlines()
                  if line.startswith('{')]
        seen = {event.get('thread_id') for event in events if event.get('type') == 'thread.started'}
        if len(seen) != 1 or not any(event.get('type') == 'turn.completed' for event in events):
            raise ValueError('Codex 编码会话身份或完成事件无效')
        coder_session_id = seen.pop()
        uuid.UUID(coder_session_id)
        if coder_session_id == session_id:
            raise ValueError('编码会话意外复用了全局规划会话')
        return {'output': str(output), 'coder_session_id': coder_session_id}


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
    settings['budget'].update(max_proposals=8, case_calls=24, full_calls=2,
                              ai_calls=4, wall_seconds=3600)
    settings_path = snapshot / 'configs/next-epoch.json'
    atomic_json(settings_path, settings)
    from .targets import DOMAINS
    source = snapshot_epoch(snapshot)
    name = 'p1-preload.yaml' if item['case'] == 'M1_P1' else 'd1-w2-group.yaml'
    path = snapshot / 'configs/targets/astra-global-v2' / name
    if not path.is_file():
        raise ValueError('新源码批次缺少受影响案例的初始目标模板')
    target = load(path)
    variable = ('programs.M1_P1.config.w2_preload_k' if item['case'] == 'M1_P1'
                else 'programs.M2_D1.config.w2_load_group_size')
    target.update(target_id='epoch-' + item['id'][:8] + '-' + target['lane'],
                  source_epoch=source, base_record=record_id, evidence_ids=[record_id],
                  hypothesis='新结构首个正确单案后的有界邻域；只检查受影响案例与新数据流的交互。',
                  variables={variable: DOMAINS[variable]}, max_trials=len(DOMAINS[variable]),
                  max_inflight=len(DOMAINS[variable]))
    targets = [target]
    if not targets:
        raise ValueError('新源码批次没有可验证的初始目标')
    payload = snapshot / 'workspace/next-epoch-targets.json'
    atomic_json(payload, {'targets': targets, 'session_id': session_id, 'out': settings['out'],
                          'max_proposals': settings['budget']['max_proposals']})
    code = (
        'import json,sys;from pathlib import Path;'
        'from codesign_lab.config import ROOT;'
        'from codesign_lab.search.targets import TargetPool,epoch;'
        'from codesign_lab.search.triggers import Triggers;'
        'd=json.loads(Path(sys.argv[1]).read_text());p=TargetPool(ROOT/d["out"],epoch(),max_proposals=d["max_proposals"]);'
        '[(lambda r: (_ for _ in ()).throw(ValueError(r)) if r["status"] not in ("accepted","reused") else None)'
        '(p.apply({"request_id":"seed-"+t["target_id"],"command":{"op":"add","target":t}})) for t in d["targets"]];'
        'Triggers(p.state).lane("global")["session_id"]=d["session_id"];p.save()')
    run(snapshot, [interpreter(), '-c', code, str(payload)], 'seed-next', timeout=60)
    return settings_path


def inject_research_target(campaign: Path, item: dict, record_id: str):
    """把已测过的结构来源交给当前主控，继续研究受影响案例的小邻域。"""
    from .targets import DOMAINS
    campaign_state = load(campaign / 'state.json')
    variable = ('programs.M1_P1.config.w2_preload_k' if item['case'] == 'M1_P1'
                else 'programs.M2_D1.config.w2_load_group_size')
    request_id = 'structure-' + item['id']
    target = {'schema_version': 1, 'target_id': request_id,
        'lane': item['lane'], 'hypothesis': '结构候选功能与单案时序已通过；检查新结构与既有参数的有限交互。',
        'base_record': record_id, 'source_epoch': campaign_state['source_epoch'],
        'cases': [item['case']], 'variables': {variable: DOMAINS[variable]},
        'sampler': 'enumerate', 'max_trials': len(DOMAINS[variable]),
        'priority': .8, 'evidence_ids': [record_id]}
    payload = {'request_id': request_id, 'command': {'op': 'add', 'target': target}}
    path = campaign / 'inbox' / (request_id + '.json')
    if path.exists():
        if load(path) != payload:
            raise ValueError('同一结构目标请求身份冲突')
    else:
        atomic_json(path, payload)
    return request_id


def same_campaign_room(campaign: Path):
    """剩余时间和单案调用不足时，交给新批次而不注入注定无法执行的目标。"""
    state = load(campaign / 'state.json')
    budget = state.get('budget', {})
    limits = budget.get('limits', {})
    used_cases = sum(row.get('kind') == 'case' and not row.get('reused', False)
                     for row in budget.get('reservations', {}).values())
    allocated = sum(max(entry['definition']['max_trials'], entry.get('trials_launched', 0))
                    for entry in state.get('targets', {}).values())
    target_limit = state.get('target_limit', 0)
    return budget.get('deadline', 0) - time.time() >= 600 and \
        limits.get('case', 0) - used_cases >= 3 and target_limit - allocated >= 3


def snapshot_epoch(snapshot: Path):
    files = {str(path.relative_to(snapshot)): digest(path)
             for path in sorted((snapshot / 'src').rglob('*.py'))}
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def record_research_candidate(snapshot: Path, item: dict, candidate: Path, estimate: Path,
                              gain: float, *, admission=True):
    """保存有真实单案结果的结构事实；超功耗候选仅供分析和诊断。"""
    from ..evaluation.pipeline import compact_cases
    import datetime
    identifier = 'research-' + item['id']
    data = load(estimate)
    info = data['cases'][item['case']]
    if info.get('functional_passed') is not True or 'timing' not in info or \
            data.get('hardware') != load(candidate / 'hardware.json') or \
            data.get('program_sha256', {}).get(item['case']) != digest(
                candidate / 'programs' / (item['case'] + '.asm')):
        raise ValueError('研究报告与正确性或程序产物身份不匹配')
    if not candidate.is_relative_to(snapshot) or not estimate.is_relative_to(snapshot):
        raise ValueError('研究证据必须属于隔离源码目录')
    record = {'id': identifier, 'campaign': 'implementation-' + item['id'],
              'scope': 'both', 'config': load(candidate / 'config.json'),
              'cases': compact_cases({item['case']: info}),
              'report': str(estimate.relative_to(snapshot)),
              'candidate': str(candidate.relative_to(snapshot)),
              'artifact_sha256': artifacts(candidate), 'case_gain': gain,
              'eligible': None if admission else False, 'score': None, 'audited': False,
              'reproduction': 'record_only', 'source_epoch': snapshot_epoch(snapshot),
              'parent_decision': item['decision_id'], 'research_admission': admission,
              'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    ledger = snapshot / 'data/experiments.jsonl'
    with (snapshot / 'data/.experiments.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = next((row for row in (json.loads(line) for line in ledger.read_text().splitlines() if line.strip())
                         if row['id'] == identifier), None)
        if existing:
            if existing.get('artifact_sha256') != record['artifact_sha256']:
                raise ValueError('研究记录 ID 对应不同的程序产物')
            record = existing
        else:
            with ledger.open('a') as output:
                output.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
                output.flush(); os.fsync(output.fileno())
    export_research_record(snapshot, record)
    return identifier


def export_research_record(snapshot: Path, record: dict):
    """主账本只收轻量事实；原始单案报告和构建产物保存在一份证据目录。"""
    origin = origin_root()
    if origin == snapshot:
        return
    identifier = record['id']
    candidate = snapshot / record['candidate']
    report = snapshot / record['report']
    target = origin / 'data/evidence/research' / identifier
    target.mkdir(parents=True, exist_ok=True)
    saved_candidate = target / 'candidate'
    if not saved_candidate.exists():
        shutil.copytree(candidate, saved_candidate)
    if artifacts(saved_candidate) != record['artifact_sha256']:
        raise ValueError('主工程研究证据与隔离产物哈希不一致')
    saved_report = target / 'estimate.json'
    if saved_report.exists():
        if digest(saved_report) != digest(report):
            raise ValueError('主工程已有不同内容的研究报告')
    else:
        shutil.copy2(report, saved_report)
    exported = dict(record, candidate=str(saved_candidate.relative_to(origin)),
                    report=str(saved_report.relative_to(origin)),
                    source_snapshot=str(snapshot),
                    source_root=source_reference(snapshot, origin),
                    source_sha256=snapshot_epoch(snapshot))
    ledger = origin / 'data/experiments.jsonl'
    with (origin / 'data/.experiments.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
        prior = next((row for row in existing if row['id'] == identifier), None)
        if prior:
            if prior.get('artifact_sha256') != record['artifact_sha256']:
                raise ValueError('主账本已有同 ID 不同产物的研究记录')
            return
        with ledger.open('a') as output:
            output.write(json.dumps(exported, ensure_ascii=False, allow_nan=False) + '\n')
            output.flush(); os.fsync(output.fileno())


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


def handoff_waiting(campaign: Path):
    """旧批次释放全局锁后，只交接已完成验证的结构版本。"""
    directory = ROOT / 'workspace/implementation-loop' / campaign.name
    if not directory.is_dir():
        return None
    for state_path in sorted(directory.glob('*/state.json')):
        state = load(state_path)
        if state.get('status') != 'WAITING_FOR_LAUNCH':
            continue
        launch = launch_next(Path(state['snapshot']), Path(state['next_settings']))
        if launch is not None:
            state.update(status='LAUNCHED', launch=launch, updated_wall=time.time())
            atomic_json(state_path, state)
            return launch
    return None


def pending_handoff(campaign: Path):
    directory = ROOT / 'workspace/implementation-loop' / campaign.name
    return any(load(path).get('status') == 'WAITING_FOR_LAUNCH'
               for path in directory.glob('*/state.json')) if directory.is_dir() else False


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
            export_epoch_record(snapshot, record['id'], item)
            save('RESEARCH_READY', official_score=score, current_best=current_best,
                 audit_record=record['id'], research_record=record['id'])
            return self.finish_research(item, state, save)
        isolated_state = load(snapshot / 'data/state.json')
        if isolated_state.get('promoted_record') != record['id']:
            run(snapshot, [interpreter(), '-m', 'codesign_lab.cli', 'promote', record['id']],
                'promote', timeout=600)
        export_epoch_record(snapshot, record['id'], item)
        if os.environ.get('CODESIGN_STRUCTURE_WORKER') == '1' and \
                origin_root() == ROOT and same_campaign_room(self.campaign):
            request_id = inject_research_target(self.campaign, item, record['id'])
            save('TARGET_QUEUED', target_request_id=request_id, official_score=score,
                 audit_record=record['id'])
            return state
        settings = seed_next_campaign(snapshot, item, record['id'], state['session_id'])
        save('WAITING_FOR_LAUNCH', next_settings=str(settings), official_score=score,
             audit_record=record['id'])
        launch = launch_next(snapshot, settings)
        if launch is not None:
            save('LAUNCHED', launch=launch)
        return state

    def finish_research(self, item, state, save):
        """研究准入不冒充正式成绩；只启动一个有界的受影响案例搜索。"""
        snapshot = Path(state['snapshot'])
        try:
            if os.environ.get('CODESIGN_STRUCTURE_WORKER') == '1' and \
                    origin_root() == ROOT and same_campaign_room(self.campaign):
                request_id = inject_research_target(self.campaign, item, state['research_record'])
                save('TARGET_QUEUED', target_request_id=request_id)
                return state
            settings = seed_next_campaign(snapshot, item, state['research_record'], state['session_id'])
            save('WAITING_FOR_LAUNCH', next_settings=str(settings))
            launch = launch_next(snapshot, settings)
            if launch is not None:
                save('LAUNCHED', launch=launch)
        except Exception as exc:
            attempts = state.get('research_seed_attempts', 0) + 1
            save('FAILED' if attempts >= 3 else 'RESEARCH_READY',
                 research_seed_attempts=attempts,
                 last_error=type(exc).__name__ + ': ' + str(exc))
        return state

    def run_official(self, item, state, save):
        """单案证据已固定后才占用正式验收槽位。"""
        snapshot = Path(state['snapshot'])
        candidate = snapshot / 'workspace/implementation-builds/candidate'
        report = snapshot / 'workspace/implementation-reports/official.json'
        if artifacts(candidate) != state['artifact_sha256']:
            raise ValueError('进入整案前候选产物身份发生变化')
        verify_official()
        verify_snapshot_official(snapshot)
        save('OFFICIAL', official_attempted=True)
        run(snapshot, [interpreter(), '-m', 'codesign_lab.cli', 'run', str(candidate),
            '--level', 'full', '--out', str(report)], 'official', timeout=7200)
        save('GRADED', report=str(report), official_score=load(report).get('experimental_score'))
        return self.finish_graded(item, state, save)

    def process(self, item: dict, *, phase='all'):
        if phase not in {'all', 'code', 'validate', 'official'}:
            raise ValueError('未知结构任务阶段')
        state_path = self.directory / item['id'] / 'state.json'
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state = load(state_path) if state_path.exists() else {'status': 'QUEUED', 'proposal': item}
        if state['status'] in TERMINAL:
            return state
        if phase == 'code' and state['status'] != 'QUEUED':
            return state
        if phase == 'validate' and state['status'] == 'QUEUED':
            return state
        if phase == 'official' and state['status'] != 'OFFICIAL_QUEUED':
            return state
        if state['status'] == 'OFFICIAL_QUEUED' and phase not in {'all', 'official'}:
            return state
        if state['status'] not in {'QUEUED', 'CODED', 'OFFICIAL_QUEUED', 'GRADED',
                                  'RESEARCH_READY', 'WAITING_FOR_LAUNCH'}:
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
        if state['status'] == 'RESEARCH_READY':
            return self.finish_research(item, state, save)
        if state['status'] == 'OFFICIAL_QUEUED':
            try:
                return self.run_official(item, state, save)
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
                coding = coding_turn(snapshot, self.campaign, session_id, item, baseline,
                                    self.code_timeout, self.model, self.effort)
                save('CODING', coder_session_id=coding['coder_session_id'])
                if original_hashes != {str(p.relative_to(ROOT)): digest(p)
                                       for p in (ROOT / 'src/codesign_lab/codegen').rglob('*.py')}:
                    raise ValueError('编码回合修改了原批次生成器源码')
                if digest(ROOT / 'vendor/official/isolation-manifest.json') != official_hash or \
                        digest(snapshot / 'data/experiments.jsonl') != ledger_hash or \
                        digest(snapshot / 'data/releases' / source_release / 'local-grade.json') != release_report_hash:
                    raise ValueError('编码回合修改了冻结依据或实验账本')
                save('CODED')
                if phase == 'code':
                    return state
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
            save('FUNCTIONAL', artifact_sha256=after, case_attempted=True)
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
            if timing['peak_window_power_w'] > 20:
                research_record = record_research_candidate(
                    snapshot, item, candidate, estimate, gain, admission=False)
                save('REJECTED', reason='单案峰值功耗超限', case_gain=gain,
                     research_record=research_record,
                     timing={'cycles': timing['cycles'],
                             'peak_window_power_w': timing['peak_window_power_w'],
                             'hbm_read_bytes': timing.get('hbm_read_bytes'),
                             'hbm_write_bytes': timing.get('hbm_write_bytes')})
                return state
            if gain < RESEARCH_GAIN_FLOOR:
                research_record = record_research_candidate(
                    snapshot, item, candidate, estimate, gain, admission=False)
                save('RESEARCH_PAUSED', reason='单案明显退化，暂停自动邻域并等待研究判断',
                     case_gain=gain, research_record=research_record,
                     timing={'cycles': timing['cycles'],
                             'peak_window_power_w': timing['peak_window_power_w'],
                             'hbm_read_bytes': timing.get('hbm_read_bytes'),
                             'hbm_write_bytes': timing.get('hbm_write_bytes')})
                return state
            if gain < self.min_case_gain:
                research_record = record_research_candidate(snapshot, item, candidate, estimate, gain)
                save('RESEARCH_READY', case_gain=gain, research_record=research_record,
                     session_id=session_id,
                     timing={'cycles': timing['cycles'],
                             'peak_window_power_w': timing['peak_window_power_w']})
                return self.finish_research(item, state, save)
            save('OFFICIAL_QUEUED', case_gain=gain, session_id=session_id,
                 timing={'cycles': timing['cycles'], 'peak_window_power_w': timing['peak_window_power_w']})
            if phase == 'validate':
                return state
            return self.run_official(item, state, save)
        except Exception as exc:
            save('FAILED', error=type(exc).__name__ + ': ' + str(exc))
            return state


def process_locked(loop: ImplementationLoop, item: dict, *, phase='all'):
    """独立 CLI 与主流水线 worker 竞争时同一提案只允许一个执行者。"""
    directory = loop.directory / item['id']
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'proposal.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return loop.process(item) if phase == 'all' else loop.process(item, phase=phase)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign', required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--poll-seconds', type=int, default=30)
    parser.add_argument('--max-proposals', type=int, default=2,
                        help='每轮最多启动的结构提案数；已完成提案不再占用后续轮次额度')
    parser.add_argument('--proposal-id', help='仅运行指定结构提案，便于复核单个方向')
    parser.add_argument('--retry-failed', action='store_true',
                        help='恢复已完成编码但在默认配置复现阶段失败的提案')
    parser.add_argument('--worker', action='store_true', help='由统一流水线准入的单提案执行者')
    parser.add_argument('--phase', choices=['all', 'code', 'validate', 'official'], default='all')
    parser.add_argument('--handoff-only', action='store_true', help='只等待旧批次锁并交接已验证的新版本')
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
    if args.worker and (not args.execute or not args.proposal_id or args.watch or args.retry_failed or args.phase == 'all'):
        parser.error('--worker 需要单个提案、--execute，且不能进入监视或手动恢复模式')
    if not args.worker and args.phase != 'all':
        parser.error('独立阶段只允许统一流水线 worker 执行')
    if args.handoff_only and (not args.execute or args.proposal_id or args.retry_failed or args.worker):
        parser.error('--handoff-only 只允许独立执行交接')
    if not args.execute:
        print(json.dumps({'mode': '计划', 'campaign': args.campaign,
            'proposals': selected[:args.max_proposals]}, ensure_ascii=False, indent=2))
        return 0
    if args.handoff_only:
        with (loop.directory / 'controller.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            while pending_handoff(campaign):
                if handoff_waiting(campaign) is not None:
                    return 0
                if not args.watch:
                    break
                time.sleep(args.poll_seconds)
        return 0
    if args.worker:
        state = process_locked(loop, selected[0][1], phase=args.phase)
        print(json.dumps({'proposal_id': args.proposal_id, 'status': state['status']}, ensure_ascii=False), flush=True)
        return 0 if state['status'] in TERMINAL | {'WAITING_FOR_LAUNCH'} or \
            args.phase == 'code' and state['status'] == 'CODED' or \
            args.phase == 'validate' and state['status'] == 'OFFICIAL_QUEUED' else 75
    with (loop.directory / 'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.retry_failed:
            retry_infrastructure_failure(loop.directory / args.proposal_id / 'state.json')
        while True:
            started = 0
            current = [(identifier,item) for identifier,item in proposals(campaign)
                       if args.proposal_id is None or identifier == args.proposal_id]
            for _, item in current:
                if started >= args.max_proposals:
                    break
                path = loop.directory / item['id'] / 'state.json'
                previous = load(path).get('status') if path.is_file() else 'QUEUED'
                if previous in TERMINAL:
                    continue
                state = process_locked(loop, item)
                if previous == 'QUEUED' and state['status'] != 'QUEUED':
                    started += 1
                if state['status'] not in TERMINAL:
                    continue
                print(json.dumps({'proposal_id': item['id'], 'status': state['status']}, ensure_ascii=False), flush=True)
                if state['status'] == 'LAUNCHED':
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
