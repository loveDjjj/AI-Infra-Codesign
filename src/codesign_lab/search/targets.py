"""校验研究目标并持久化控制请求；只由主控修改目标状态。"""
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import uuid
import time

from ..config import ROOT, load, digest
from ..records import read
from .scheduler import atomic_json

# 只注册当前实现已验证可构建的域，功耗和性能仍需真实评估。
DOMAINS = {
    'programs.M2_D1.config.w2_load_group_size': [4, 8, 16],
    'programs.M1_P1.config.attention_query_tile': [8, 16, 32],
    'programs.M1_P1.config.attention_key_tile': [32, 64],
    'programs.M1_P1.config.attention_value_tile': [32, 64],
    'programs.M1_P1.config.w1_preload_k': [16, 32, 64],
    'programs.M1_P1.config.w2_preload_k': [16, 32, 64],
    'programs.M1_P1.config.gemm_m_tile': [8, 16, 32],
    'programs.M1_P1.config.gemm_k_tile': [32, 64],
    'programs.M1_P1.config.gemm_n_group': [1, 2],
    'programs.M1_P1.config.vector_tile': [512, 1024],
    'hardware.reduction_units': [1, 2],
    'hardware.cache_mib': [0, 1],
    'hardware.sfu_lanes': [4, 8],
}
FIELDS = {'schema_version', 'target_id', 'lane', 'hypothesis', 'base_record',
          'source_epoch', 'cases', 'variables', 'sampler', 'max_trials', 'priority', 'evidence_ids'}


def epoch():
    files = {str(p.relative_to(ROOT)): digest(p) for p in sorted((ROOT / 'src').rglob('*.py'))}
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def campaign_path(name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', name):
        raise ValueError('campaign 名称无效')
    return ROOT / 'workspace/pipeline' / name


def validate_target(target, source_epoch, records=None, max_trials=128):
    if not isinstance(target, dict) or set(target) - {'seed', 'max_inflight', 'prior_record_ids'} != FIELDS:
        raise ValueError('目标缺少必要字段或含未知字段')
    target = copy.deepcopy(target)
    if type(target['schema_version']) is not int or target['schema_version'] != 1:
        raise ValueError('目标 schema_version 必须为 1')
    for field in ['target_id', 'lane']:
        if not isinstance(target[field], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', target[field]):
            raise ValueError(f'{field} 无效')
    if not isinstance(target['hypothesis'], str) or not 1 <= len(target['hypothesis']) <= 4000:
        raise ValueError('hypothesis 必须为非空简短文本')
    if target['source_epoch'] != source_epoch:
        raise ValueError('目标源码 epoch 与当前主控不匹配')
    cases = target['cases']
    if not isinstance(cases, list) or not cases or any(not isinstance(case, str) for case in cases) or len(set(cases)) != len(cases) or not set(cases) <= {'M1_P1', 'M2_D1'}:
        raise ValueError('目标 cases 无效')
    if target['sampler'] not in {'enumerate', 'random', 'tpe'}:
        raise ValueError('当前支持 enumerate/random/tpe')
    if 'seed' in target and (type(target['seed']) is not int or not 0 <= target['seed'] < 2**64):
        raise ValueError('seed 必须为 0 到 2**64-1 的整数')
    if type(target['max_trials']) is not int or not 0 < target['max_trials'] <= max_trials:
        raise ValueError('目标 trial 预算无效或超过允许额度')
    if 'max_inflight' in target and (type(target['max_inflight']) is not int or not 1 <= target['max_inflight'] <= target['max_trials']):
        raise ValueError('max_inflight 必须为 1 到 max_trials 的整数')
    priority = target['priority']
    if type(priority) not in (int, float) or not math.isfinite(priority) or not 0 <= priority <= 1:
        raise ValueError('priority 必须在 0 到 1 之间')
    variables = target['variables']
    if not isinstance(variables, dict) or not variables:
        raise ValueError('目标必须提供真实可搜索变量')
    for path, values in variables.items():
        if path not in DOMAINS:
            raise ValueError('未知或未注册的有效变量：' + path)
        if not isinstance(values, list) or not values or len(values) > len(DOMAINS[path]):
            raise ValueError('变量值域无效：' + path)
        if any(type(value) is not int or value not in DOMAINS[path] for value in values) or len(set(values)) != len(values):
            raise ValueError('变量含不受支持的值：' + path)
        if path.startswith('programs.') and path.split('.')[1] not in cases:
            raise ValueError('变量作用案例与目标 cases 不一致')
        if path.startswith('hardware.') and set(cases) != {'M1_P1', 'M2_D1'}:
            raise ValueError('硬件搜索必须覆盖两案')
    if target['sampler']=='tpe':
        if len(cases)!=1 or any(path.startswith('hardware.') for path in variables):
            raise ValueError('TPE 当前要求固定硬件单案例')
        config=load(ROOT/'configs/search-toolchain.yaml')
        if not (ROOT/config['python']).is_file():raise ValueError('独立 TPE 搜索环境尚未安装')
        target.setdefault('max_inflight',1)
    records = read() if records is None else records
    by_id = {r['id']: r for r in records}
    base = by_id.get(target['base_record'])
    if not base or not isinstance(base.get('config'), dict):
        raise ValueError('base_record 不存在或缺少完整配置')
    origin = base.get('source_root')
    if origin:
        source = Path(origin)
        source = (source if source.is_absolute() else ROOT / source).resolve()
        if source != ROOT:
            if not source.is_relative_to(ROOT / 'workspace/families') or not (source / 'src').is_dir():
                raise ValueError('历史记录来源不是可用的冻结实现族')
            files = {str(path.relative_to(source)): digest(path)
                     for path in sorted((source / 'src').rglob('*.py'))}
            actual = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
            if actual != base.get('source_sha256') or \
                    digest(source / 'vendor/official/isolation-manifest.json') != \
                    digest(ROOT / 'vendor/official/isolation-manifest.json'):
                raise ValueError('冻结实现族来源身份与历史记录不一致')
            if target['sampler'] == 'tpe' and target.get('prior_record_ids'):
                raise ValueError('冻结实现族 TPE 暂不导入旧源码先验')
            target['execution_root'] = str(source)
            target['execution_sha256'] = actual
    if 'prior_record_ids' in target:
        identifiers=target['prior_record_ids']
        if target['sampler']!='tpe' or not isinstance(identifiers,list) or len(identifiers)>16 or any(not isinstance(identifier,str) or identifier not in by_id for identifier in identifiers) or len(set(identifiers))!=len(identifiers):
            raise ValueError('prior_record_ids 仅支持TPE的最多16个不同历史记录')
        if any(not isinstance(by_id[identifier].get('config'),dict) or not isinstance(by_id[identifier].get('report'),str) for identifier in identifiers):
            raise ValueError('历史先验缺少配置或原始报告')
    evidence = target['evidence_ids']
    if not isinstance(evidence, list) or any(not isinstance(identifier, str) or identifier not in by_id for identifier in evidence):
        raise ValueError('evidence_ids 引用无效')
    # 固定完整配置，后续晋升不改变本目标的起点。
    return target, copy.deepcopy(base['config'])


def inject(directory, command, request_id=None):
    directory = Path(directory)
    if not directory.is_relative_to(ROOT / 'workspace/pipeline'):
        raise ValueError('控制请求必须位于 workspace/pipeline')
    request_id = request_id or str(uuid.uuid4())
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', request_id):
        raise ValueError('request_id 无效')
    payload = {'request_id': request_id, 'command': command}
    # 每个注入者使用唯一临时文件，避免同 request_id 并发写入破坏内容。
    inbox = directory / 'inbox';inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / (request_id + '-' + str(uuid.uuid4()) + '.json')
    atomic_json(path, payload)
    return {'request_id': request_id, 'request': str(path), 'status': '待主控确认'}


class TargetPool:
    def __init__(self, directory, source_epoch, *, max_proposals=128):
        self.directory = Path(directory)
        self.path = self.directory / 'state.json'
        self.source_epoch = source_epoch
        self.max_proposals = max_proposals
        self.state = load(self.path) if self.path.exists() else {
            'schema_version': 1, 'source_epoch': source_epoch, 'targets': {}, 'requests': {}}
        if self.state['source_epoch'] != source_epoch:
            raise ValueError('目标状态属于其他源码 epoch')
        self.state.setdefault('campaign', self.directory.name)
        self.save()

    def save(self):
        atomic_json(self.path, self.state)

    def apply(self, request):
        identifier = request['request_id']
        if identifier in self.state['requests']:
            return self.state['requests'][identifier]
        command = request['command']
        try:
            if not isinstance(command, dict):
                raise ValueError('command 必须为对象')
            operation = command.get('op')
            if operation == 'analyze':
                if set(command) != {'op', 'lane', 'decision_id', 'timeout'}:
                    raise ValueError('手动分析控制字段无效')
                lane_name = command['lane']
                entry = self.state.get('analysis', {}).get('lanes', {}).get(lane_name, {})
                pending = entry.get('pending')
                if not pending or pending['decision_id'] != command['decision_id']:
                    raise ValueError('手动分析身份与待处理请求不一致')
                if type(command['timeout']) is not int or not 0 < command['timeout'] <= 3600:
                    raise ValueError('手动分析时限必须是 1–3600 秒')
                if entry.get('recovery_blocked'):
                    raise ValueError('分析恢复身份待核对，禁止替代调用')
                if entry.get('running_job') and entry['running_job']['decision_id'] != command['decision_id']:
                    raise ValueError('同 lane 已有另一在途分析')
                manual = entry.get('manual_request')
                reused = bool(entry.get('running_job') or manual and manual['decision_id'] == command['decision_id'])
                if not entry.get('running_job'):
                    budget = self.state.get('budget', {})
                    used = sum(r['kind'] == 'ai' and not r.get('reused', False)
                               for r in budget.get('reservations', {}).values())
                    if time.time() >= budget.get('deadline', 0) or used >= budget.get('limits', {}).get('ai', 0):
                        raise ValueError('累计 AI 预算已耗尽或到期')
                    if entry.get('attempts', {}).get(command['decision_id'], 0) >= 2:
                        raise ValueError('该分析请求已经用尽尝试次数')
                    if not reused:
                        entry['manual_request'] = {'decision_id': command['decision_id'], 'timeout': command['timeout']}
                result = {'status': 'reused' if reused else 'accepted', 'lane': lane_name,
                          'decision_id': command['decision_id'], 'scope': '由主控预算准入，未在 CLI 直接调用模型'}
                self.state['requests'][identifier] = result
                self.save()
                return result
            if operation == 'decision':
                from .decisions import apply_decision
                from .triggers import Triggers
                session_id = command.get('session_id')
                lane_name = command['decision']['lane']
                existing = self.state.get('analysis', {}).get('lanes', {}).get(lane_name, {}).get('session_id')
                if session_id:
                    uuid.UUID(session_id)
                    if existing and existing != session_id:
                        raise ValueError('分析 session 身份发生变化')
                result = apply_decision(self, Triggers(self.state), command['decision'])
                if session_id:
                    self.state['analysis']['lanes'][lane_name]['session_id'] = session_id
                self.state['requests'][identifier] = result
                self.save()
                return result
            if operation == 'add':
                target, base = validate_target(command['target'], self.source_epoch, max_trials=self.max_proposals)
                target_id = target['target_id']
                if target_id in self.state['targets']:
                    raise ValueError('target_id 已存在')
                allocated = sum(max(t['definition']['max_trials'],t.get('trials_launched',0)) for t in self.state['targets'].values())
                if allocated + target['max_trials'] > self.max_proposals:
                    raise ValueError('目标池累计提案预算不足')
                self.state['targets'][target_id] = {'definition': target, 'base_config': base,
                    'status': 'QUEUED', 'trials_launched': 0, 'trials_completed': 0, 'tasks': []}
            elif operation in {'stop', 'reprioritize', 'set_remaining_budget'}:
                target_id = command['target_id'];entry = self.state['targets'][target_id]
                if operation == 'stop':
                    entry['status'] = 'DRAINING' if entry['trials_launched'] > entry['trials_completed'] else 'STOPPED'
                elif operation == 'reprioritize':
                    value = command['priority']
                    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                        raise ValueError('priority 无效')
                    entry['definition']['priority'] = value
                else:
                    value = command['remaining']
                    if type(value) is not int or value < 0:
                        raise ValueError('剩余预算必须为非负整数')
                    if entry['status'] in {'STOPPED', 'DRAINING', 'FAILED'}:
                        raise ValueError('已停止目标不能通过预算修改恢复')
                    from .target_control import started_candidates
                    total = len(started_candidates(entry, self.state.get('jobs', {}))) + value
                    allocated = sum(max(t['definition']['max_trials'],t.get('trials_launched',0)) for tid,t in self.state['targets'].items() if tid != target_id)
                    if allocated + max(total,entry['trials_launched']) > self.max_proposals:
                        raise ValueError('目标池预算不足')
                    entry['definition']['max_trials'] = total
                    entry['budget_reconcile'] = True
                    entry['expand'] = value > 0
                    if entry['status'] == 'DONE' and value > 0:
                        entry['status'] = 'ACTIVE'
            else:
                raise ValueError('未知控制操作')
            result = {'status': 'accepted', 'target_id': target_id}
        except (ValueError, KeyError, TypeError) as exc:
            result = {'status': 'rejected', 'error': str(exc)}
        self.state['requests'][identifier] = result
        self.save()
        return result

    def consume(self):
        results = []
        for path in sorted((self.directory / 'inbox').glob('*.json')):
            try:
                request = load(path)
                result = self.apply(request)
            except (ValueError, KeyError, TypeError) as exc:
                request = {'request_id': path.stem};result = {'status': 'rejected', 'error': str(exc)}
            ack = self.directory / 'processed' / path.name
            atomic_json(ack, {'request': request, 'result': result})
            path.unlink()
            results.append(result)
        return results
