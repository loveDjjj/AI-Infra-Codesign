#!/usr/bin/env python3
"""统一滚动探索与优先官方验收；默认仅检查，显式执行才启动任务。"""
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
import uuid

from ..config import ROOT
from codesign_lab.config import load, digest, verify_official, reference, bootstrap
from codesign_lab.evaluation.pipeline import runtime, task, record_result
from codesign_lab.records import read, append
from codesign_lab.search.scheduler import atomic_json, resources, proc_pid, terminate
from codesign_lab.evaluation.monitor import sample_tree
from codesign_lab.search.space import candidates
from codesign_lab.search.prune import reject


def key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def source_identity():
    return source_identity_at(ROOT)


def source_identity_at(root):
    root = Path(root)
    return {str(path.relative_to(root)): digest(path)
            for path in sorted((root / 'src').rglob('*.py'))}


def generator_identity(root):
    root = Path(root)
    return {str(path.relative_to(root)): digest(path)
            for path in sorted((root / 'src/codesign_lab/codegen').rglob('*.py'))}


def select_jobs(pending, active, external, workers, full_slots, memory_budget, available):
    """官方验收优先，探索始终保留验收容量，实时内存不足时不启动。"""
    selected = []
    reserved = sum(x['memory_bytes'] for x in active)
    official = {'full', 'verify', 'audit', 'implementation_official'}
    # 回归、功能与单案时序使用探索槽；结构整案另留一个普通验收槽。
    full_count = sum(x['stage'] in official for x in active)
    structure_count = sum(x['stage'] == 'implementation_official' for x in active)
    explore_count = len(active) - full_count + external
    for job in sorted(pending, key=lambda x: (0 if x['stage'] in official else
                                             1 if x['stage'].startswith('implementation_') else 2,
                                             -x.get('priority', 0))):
        if job.get('retry_after', 0) > time.time():
            continue
        if len(active) + external + len(selected) >= workers:
            break
        if job['stage'] == 'implementation_official':
            if structure_count >= full_slots - 1 or full_count >= full_slots:
                continue
        elif job['stage'] in official:
            if full_count >= full_slots:
                continue
        elif explore_count >= workers - full_slots:
            continue
        if reserved + job['memory_bytes'] > memory_budget or job['memory_bytes'] > available:
            continue
        selected.append(job)
        reserved += job['memory_bytes']
        available -= job['memory_bytes']
        if job['stage'] in official:
            full_count += 1
            structure_count += job['stage'] == 'implementation_official'
        else:
            explore_count += 1
    return selected


class Pipeline:
    def __init__(self, args):
        self.args = args
        self.out = args.out.resolve()
        if not any(self.out.is_relative_to(ROOT / path) for path in ['workspace/search', 'workspace/pipeline']):
            raise ValueError('流水线目录必须位于 workspace/search 或 workspace/pipeline')
        self.out.mkdir(parents=True, exist_ok=True)
        self.python, self.env = runtime()
        self.manifest = verify_official()
        self.source = source_identity()
        self.family_sources = {}
        for source_name, _config_name in getattr(args, 'family_config', []):
            source_root = Path(source_name).resolve()
            if not source_root.is_relative_to(ROOT / 'workspace/families') or \
                    digest(source_root / 'vendor/official/isolation-manifest.json') != \
                    digest(ROOT / 'vendor/official/isolation-manifest.json'):
                raise ValueError('实现族必须位于 workspace/families 且使用同一冻结官方工具链')
            self.family_sources[str(source_root)] = source_identity_at(source_root)
        self.identity = key({'source': self.source, 'official': self.manifest,
            'configs': {str(p): digest(p) for p in args.config}, 'watch': args.watch,
            'family_configs': [(str(Path(source).resolve()), str(Path(config).resolve()),
                                self.family_sources[str(Path(source).resolve())], digest(config))
                               for source, config in getattr(args, 'family_config', [])],
            'workers': args.workers, 'full_slots': args.full_slots,
            'cache_dir': str(getattr(args, 'cache_dir', None)),
            'auto_audit': not getattr(args, 'no_auto_audit', False),
            'max_retries': getattr(args, 'max_retries', 1)})
        self.identity = key({'identity': self.identity,
            'settings': {name: getattr(args, name, default) for name, default in {
                'budget': 7200, 'timeout': 3600, 'max_proposals': 128,
                'max_case_calls': 48, 'max_full_calls': 6, 'max_ai_calls': 10,
                'memory_fraction': .75, 'min_predicted_gain': 0,
                'max_pending_full': 3,
                'analysis_batch_size': 8, 'analysis_cooldown': 120, 'analysis_low_watermark': 8,
                'stagnation_trials': 12, 'improvement_threshold': .002,
                'failure_window': 6, 'failure_threshold': 5,
                'report_interval': 30,
                'max_profile_calls': 2,
                'ai_enabled': False, 'ai_timeout': 600, 'analysis_mode': 'per_lane', 'stay_open': False,
                'analysis_model': 'gpt-6-astra', 'analysis_effort': 'medium',
                'stop_on_exhaustion': False,
                'implementation_enabled': False, 'implementation_model': 'gpt-6-astra',
                'implementation_effort': 'medium', 'implementation_max_proposals': 2,
                'implementation_min_case_gain': .002,
                'implementation_min_score_gain': 100}.items()}})
        meta = self.out / 'identity.json'
        if meta.exists() and load(meta)['identity'] != self.identity:
            raise ValueError('输入身份已改变，请使用新的流水线目录')
        atomic_json(meta, {'identity': self.identity, 'source': self.source})
        from .targets import TargetPool, epoch
        self.pool = TargetPool(self.out, epoch(), max_proposals=getattr(args, 'max_proposals', 128))
        for entry in self.pool.state['targets'].values():
            definition = entry['definition']
            if definition.get('execution_root'):
                source = Path(definition['execution_root'])
                identity = source_identity_at(source)
                if key(identity) != definition['execution_sha256']:
                    raise ValueError('恢复的动态目标冻结源码已改变')
                self.family_sources[str(source)] = identity
        from .budget import Budget
        self.budget = Budget(self.pool.state, wall_seconds=args.budget,
            case_calls=getattr(args, 'max_case_calls', 48), full_calls=getattr(args, 'max_full_calls', 6),
            ai_calls=getattr(args, 'max_ai_calls', 10), profile_calls=getattr(args, 'max_profile_calls', 2))
        from .triggers import Triggers
        self.triggers = Triggers(self.pool.state,
            batch_size=getattr(args, 'analysis_batch_size', 8), cooldown=getattr(args, 'analysis_cooldown', 120),
            low_watermark=getattr(args, 'analysis_low_watermark', 8),
            stagnation_trials=getattr(args, 'stagnation_trials', 12), improvement_threshold=getattr(args, 'improvement_threshold', .002),
            failure_window=getattr(args, 'failure_window', 6), failure_threshold=getattr(args, 'failure_threshold', 5),
            global_only=getattr(args, 'analysis_mode', 'per_lane') == 'global')
        if self.triggers.global_only and not self.triggers.lane('global').get('session_id'):
            session_file=ROOT/'workspace/ai-sessions/global.json'
            if session_file.is_file():
                session_id=load(session_file).get('session_id')
                uuid.UUID(session_id)
                self.triggers.lane('global')['session_id']=session_id
        self.pool.save()
        self.pending, self.active, self.done = [], {}, []
        self.seen, self.full_seen = set(), set()
        self.best = {}
        self.started = time.monotonic()
        self.workers = min(args.workers, resources()['cpus'])
        self.memory_fraction = getattr(args, 'memory_fraction', .75)
        self.memory_budget = int(resources()['available_memory_bytes'] * self.memory_fraction)
        bootstrap()
        from codesign.challenge.score import evaluate_score, ScenarioMetric
        self.evaluate_score, self.Metric = evaluate_score, ScenarioMetric
        self.baseline = load(ROOT / 'vendor/official/baseline_manifest.json')['baseline_cycles']
        baseline_score = load(ROOT / 'data/releases/joint28/local-grade.json')['experimental_score']
        self.initial_score = max([baseline_score] + [r['score'] for r in read()
            if r.get('scope') == 'full' and r.get('audited') and r.get('eligible') and isinstance(r.get('score'), (int, float))])
        self.accept_release()
        self.restore_jobs()
        from .analyst import Analyst
        self.analyst = Analyst(self.pool, self.triggers, self.budget, self.out / 'ai',
            enabled=getattr(args, 'ai_enabled', False), timeout=getattr(args, 'ai_timeout', 600),
            model=getattr(args, 'analysis_model', 'gpt-6-astra'),
            reasoning_effort=getattr(args, 'analysis_effort', 'medium'))

    def accept_release(self):
        """将已审计且本目录可取回的最佳整案作为组合参照。"""
        releases = [ROOT / 'data/releases/joint28']
        state_path = ROOT / 'data/state.json'
        ledger = ROOT / 'data/experiments.jsonl'
        rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()] if ledger.is_file() else []
        if state_path.is_file():
            promoted = load(state_path).get('promoted_record')
            if promoted:
                record = next((row for row in rows if row.get('id') == promoted), None)
                if record and record.get('eligible') is True and record.get('audited') is True:
                    name = record.get('candidate')
                    candidate = (Path(name) if Path(name).is_absolute() else ROOT / name).resolve() \
                        if isinstance(name, str) and name else None
                    if candidate and candidate.is_relative_to((ROOT / 'data/releases').resolve()) and \
                            candidate.is_dir() and candidate not in releases:
                        releases.append(candidate)
        # 已审计组合可能优于已晋升版本；其分案来源已被各自冻结归档。
        for record in rows:
            if record.get('audited') is not True or record.get('eligible') is not True or \
                    record.get('reproduction') != 'verified_composite':
                continue
            name = record.get('candidate')
            candidate = (Path(name) if Path(name).is_absolute() else ROOT / name).resolve() \
                if isinstance(name, str) and name else None
            if candidate and candidate.is_relative_to((ROOT / 'data/releases').resolve()) and \
                    (candidate / 'composite.json').is_file() and candidate not in releases:
                releases.append(candidate)
        for root in releases:
            grade = load(root / 'local-grade.json')
            source_roots = {}
            if (root / 'composite.json').is_file():
                from .composite import materialize_source
                for case in ('M1_P1', 'M2_D1'):
                    source_roots[case] = materialize_source(root, case,
                        ROOT / 'workspace/composite-sources' / root.name / case)
            for case, info in grade['cases'].items():
                timing = info['timing']
                config_path = root / (case + '-config.json') if source_roots else None
                self.observe(root, case, timing['cycles'], timing['peak_window_power_w'],
                             source_root=source_roots.get(case), config_path=config_path)

    def observe(self, candidate, case, cycles, power, source_root=None, config_path=None):
        if power > 20 or cycles <= 0:
            return
        hw = digest(candidate / 'hardware.json')
        row = self.best.setdefault(hw, {})
        if case not in row or cycles < row[case]['cycles']:
            row[case] = {'candidate': candidate, 'cycles': cycles, 'power': power,
                         'source_root': Path(source_root or ROOT),
                         'config_path': Path(config_path) if config_path else Path(candidate) / 'config.json',
                         'config_sha256': digest(config_path or Path(candidate) / 'config.json'),
                         'generator_sha256': generator_identity(source_root or ROOT),
                         'source_sha256': source_identity_at(source_root or ROOT)}

    def design_key(self, config, source_root):
        original = key(config)
        if source_root == ROOT:
            return original
        identity = self.family_sources[str(source_root)]
        return key([str(source_root), original, identity])

    def build_command(self, source_root, config_path, candidate):
        if source_root == ROOT:
            return [self.python, '-m', 'codesign_lab.cli', 'build',
                    str(config_path), '--out', str(candidate)]
        identity_path = self.out / 'sources' / (key(str(source_root)) + '.json')
        atomic_json(identity_path, self.family_sources[str(source_root)])
        return [self.python, '-m', 'codesign_lab.search.family_build',
                str(source_root), str(config_path), '--out', str(candidate),
                '--source-identity', str(identity_path)]

    def persist_job(self, job, status, **fields):
        if status == 'QUEUED':
            job.setdefault('queued_wall', time.time())
        jobs = self.pool.state.setdefault('jobs', {})
        jobs.setdefault(job['key'], {})
        jobs[job['key']].update({'job': json.loads(json.dumps(job, default=str)), 'status': status, **fields})
        self.pool.save()

    def restore_jobs(self):
        """成功结果重放依赖；存活监督进程只接管监控，不重复启动。"""
        from ..evaluation.monitor import sample
        entries = list(self.pool.state.get('jobs', {}).values())
        self.seen.update(entry['job']['key'] for entry in entries if entry['status'] != 'CANCELLED')
        for entry in entries:
            if entry['status'] != 'CANCELLED' and entry['job'].get('pair_id'):
                self.full_seen.add(entry['job']['pair_id'])
            if entry['status'] == 'CANCELLED':
                continue
            job = dict(entry['job'])
            for field in ['candidate', 'report']:
                if field in job:
                    job[field] = Path(job[field])
            self.seen.add(job['key'])
            result = self.out / 'jobs' / (job['key'] + '.result.json')
            if entry['status'] == 'QUEUED':
                self.pending.append(job)
                continue
            attempt_result = Path(entry['attempt_result']) if entry.get('attempt_result') else None
            if not result.exists() and attempt_result and attempt_result.exists():
                self.finish_attempt(job, load(attempt_result))
                continue
            if result.exists():
                self.completed(job, load(result))
                continue
            lease_path = Path(entry['spec']).with_suffix('.lease.json')
            recovered_child = None
            if not lease_path.exists():
                from ..evaluation.recovery import recover_start
                recovered = recover_start(entry['spec'], self.python, self.env)
                if 'result' in recovered:
                    self.finish_attempt(job, recovered['result'])
                    continue
                recovered_child = recovered['child']
            lease = load(lease_path)
            try:
                current = sample(lease['pid'])
            except FileNotFoundError:
                current = None
            if current is None or current['start_ticks'] != lease['start_ticks'] or current['state'] == 'Z':
                from ..evaluation.recovery import reap_orphan
                self.finish_attempt(job, reap_orphan(entry['spec']))
                continue
            self.active[job['key']] = {'job': job, 'child': recovered_child, 'pid': lease['pid'],
                'start': time.monotonic() - max(0, time.time() - lease['started_wall']),
                'stdout': None, 'stderr': None, 'peak': 0, 'attempt_result': attempt_result}

    def enqueue(self, job):
        if job['key'] in self.seen:
            if not job.get('target_owned'):
                for prior in self.pending:
                    if prior['key'] == job['key'] and prior.get('target_owned'):
                        prior['target_owned'] = False
                        self.persist_job(prior, 'QUEUED')
            return
        self.seen.add(job['key'])
        result = self.out / 'jobs' / (job['key'] + '.result.json')
        if result.exists():
            self.completed(job, load(result))
            return
        self.persist_job(job, 'QUEUED')
        self.pending.append(job)

    def trim_finalists(self):
        """限制等待验收的组合，优先更高预测分，运行任务不抢占。"""
        waiting = sorted([job for job in self.pending if job['stage'] in {'verify', 'full'}],
                         key=lambda job: (-job.get('priority', 0), job['key']))
        best_by_hw = {}
        for item in self.active.values():
            job = item['job']
            if job['stage'] in {'verify', 'full'} and job.get('hardware_hash'):
                hw = job['hardware_hash']
                best_by_hw[hw] = max(best_by_hw.get(hw, float('-inf')), job.get('priority', 0))
        retained = 0
        for job in waiting:
            score = job.get('priority', 0)
            hw = job.get('hardware_hash')
            inferior = hw and best_by_hw.get(hw, float('-inf')) >= score
            if score <= self.initial_score + getattr(self.args, 'min_predicted_gain', 0) or inferior or retained >= getattr(self.args, 'max_pending_full', 3):
                self.pending.remove(job)
                self.persist_job(job, 'CANCELLED', cancel_reason='预测门槛、同硬件更好组合或等待队列上限')
                if job.get('pair_id'):
                    self.full_seen.discard(job['pair_id'])
                    self.seen.discard(job['key'])
                continue
            retained += 1
            if hw:
                best_by_hw[hw] = score

    def finalist_capacity(self, score, hardware_hash):
        """排队优先级只延后候选，不将其记作永久非法。"""
        waiting = [job for job in self.pending if job['stage'] in {'verify', 'full'}]
        running = [item['job'] for item in self.active.values() if item['job']['stage'] in {'verify', 'full'}]
        if any(job.get('hardware_hash') == hardware_hash and job.get('priority', 0) >= score for job in waiting + running):
            return False
        limit = getattr(self.args, 'max_pending_full', 3)
        return len(waiting) < limit or score > min(job.get('priority', 0) for job in waiting)

    def finish_attempt(self, job, result):
        """只重试已确认结束的基础设施故障，每次尝试独立留证和计费。"""
        result = dict(result)
        result.setdefault('queue_wait_seconds', job.get('queue_wait_seconds'))
        attempt = job.get('attempt', 1)
        entry = self.pool.state.setdefault('jobs', {}).setdefault(job['key'], {})
        attempts = entry.setdefault('attempts', [])
        if not any(item['attempt'] == attempt for item in attempts):
            attempts.append({'attempt': attempt, 'result': result})
        infrastructure = result['status'] in {'timeout', 'infrastructure_failed'} or (
            result['status'] == 'failed' and result.get('exit_code', 0) < 0)
        if job['stage'] in {'case', 'functional'} and job.get('report') and Path(job['report']).exists():
            info = load(Path(job['report'])).get('cases', {}).get(job['case'], {})
            if info.get('functional_passed') is False:
                infrastructure = False
        stopped = job.get('target_owned') and not any(
            target['status'] == 'ACTIVE' and (job['key'] in target.get('adaptive', {}).get('pending_jobs', [])
                or job['key'] in target.get('adaptive', {}).get('prior_build_keys', [])
                or (target.get('adaptive') and job['key'] == 'build-' + target.get('base_key', ''))
                or any(job['key'] in candidate.get('case_keys', [])
                or job['key'] in {'build-' + identity, 'build-' + candidate.get('base_key', '')}
                for identity, candidate in target.get('candidates', {}).items()))
            for target in self.pool.state['targets'].values())
        if infrastructure and not job['stage'].startswith('implementation_') and not stopped and not self.budget.expired() and attempt <= getattr(self.args, 'max_retries', 1):
            retry = dict(job, attempt=attempt + 1, retry_after=time.time() + 30 * attempt, queued_wall=time.time())
            self.persist_job(retry, 'QUEUED')
            self.pending.append(retry)
            return
        atomic_json(self.out / 'jobs' / (job['key'] + '.result.json'), result)
        self.persist_job(job, 'FINISHED')
        self.completed(job, result)

    def prepare(self):
        sources = [(ROOT, path) for path in self.args.config]
        sources += [(Path(source).resolve(), Path(path).resolve())
                    for source, path in getattr(self.args, 'family_config', [])]
        for source_root, path in sources:
            spec = load(path)
            base = load(source_root / spec['base'])
            for config_key, config in [('anchor-' + key(base), base), *candidates(base, spec['variables'], spec['max_candidates'])]:
                if reject(config):
                    continue
                if source_root != ROOT:
                    config_key = self.design_key(config, source_root)
                candidate = self.out / 'builds' / config_key
                config_path = self.out / 'configs' / (config_key + '.json')
                atomic_json(config_path, config)
                command = self.build_command(source_root, config_path, candidate)
                job = {'key': 'build-' + config_key, 'stage': 'build', 'candidate': candidate,
                    'cases': spec.get('cases', ['M1_P1', 'M2_D1']), 'seeds': spec.get('functional_seeds', [7, 123]),
                    'source_root': str(source_root), 'memory_bytes': 1024**3, 'command': command}
                # 相同配置由多个空间提出时合并案例，避免漏掉第二个工作负载。
                prior = next((x for x in self.pending if x['key'] == job['key']), None)
                if prior:
                    prior['cases'] = sorted(set(prior['cases'] + job['cases']))
                else:
                    self.enqueue(job)

    def process_targets(self):
        """运行期间接收新目标；完成构建即可按真实产物释放评估。"""
        self.pool.consume()
        self.triggers.state = self.pool.state.setdefault('analysis', {'lanes': {}})
        self.process_profile_requests()
        self.reconcile_target_controls()
        self.cancel_stopped_targets()
        self.triggers.state = self.pool.state.setdefault('analysis', {'lanes': {}})
        from .samplers import candidates as generate, target_seed, FiniteSampler
        from .targets import DOMAINS
        for target_id, entry in self.pool.state['targets'].items():
            definition = entry['definition']
            source_root = Path(definition.get('execution_root', ROOT))
            if source_root != ROOT:
                identity = source_identity_at(source_root)
                if key(identity) != definition['execution_sha256']:
                    raise ValueError('动态目标冻结源码已改变')
                self.family_sources[str(source_root)] = identity
            adaptive = definition.get('sampler') == 'tpe'
            if adaptive:
                from .adaptive import prepare
                if entry.pop('expand', False):entry['status'] = 'ACTIVE'
                prepare(self, target_id, entry)
            if not adaptive and (entry['status'] == 'QUEUED' or entry.pop('expand', False) or (entry['status'] == 'ACTIVE' and 'max_inflight' in definition)):
                entry['status'] = 'ACTIVE'
                entry.setdefault('candidates', {})
                if definition['max_trials'] == 0:
                    entry['status'] = 'DONE'
                    continue
                base = entry['base_config']
                base_key = self.design_key(base, source_root)
                base_path = self.out / 'builds' / base_key
                cfg = self.out / 'configs' / (base_key + '.json')
                atomic_json(cfg, base)
                self.enqueue({'key': 'build-' + base_key, 'stage': 'build', 'candidate': base_path,
                    'target_owned': True,
                    'cases': [], 'seeds': [7, 123], 'memory_bytes': 1024**3,
                    'source_root': str(source_root),
                    'command': self.build_command(source_root, cfg, base_path)})
                entry['proposal_exhausted'] = False
                for _proposed_key, config in generate(base, definition['variables'], self.pool.max_proposals,
                        sampler=definition.get('sampler', 'enumerate'),
                        seed=target_seed(definition) if definition.get('sampler') == 'random' else 0):
                    config_key = (_proposed_key if source_root == ROOT else
                                  self.design_key(config, source_root))
                    prior = entry['candidates'].get(config_key)
                    if prior and prior['status'] != 'CANCELLED':
                        continue
                    if prior and prior.get('cancel_reason') != '目标剩余预算缩减':
                        continue
                    if sum(c['status'] != 'CANCELLED' for c in entry['candidates'].values()) >= definition['max_trials']:
                        break
                    if sum(c['status'] in {'BUILDING', 'EVALUATING'} for c in entry['candidates'].values()) >= definition.get('max_inflight', definition['max_trials']):
                        break
                    feedback = FiniteSampler(base, definition['variables'],
                        sampler=definition.get('sampler', 'enumerate'),
                        seed=target_seed(definition) if definition.get('sampler') == 'random' else 0,
                        state=entry.setdefault('sampler_state', {}))
                    feedback.remember(config_key, config)
                    reason = reject(config)
                    if not prior:entry['trials_launched'] += 1
                    entry['candidates'][config_key] = {'status': 'STATIC_REJECTED' if reason else 'BUILDING',
                        'reason': reason, 'base_key': base_key, 'cases': definition['cases'], 'case_keys': []}
                    if reason:
                        continue
                    candidate = self.out / 'builds' / config_key
                    cfg = self.out / 'configs' / (config_key + '.json')
                    atomic_json(cfg, config)
                    self.enqueue({'key': 'build-' + config_key, 'stage': 'build', 'candidate': candidate,
                        'target_owned': True,
                        'cases': [], 'seeds': [7, 123], 'priority': definition['priority'], 'memory_bytes': 1024**3,
                        'source_root': str(source_root),
                        'command': self.build_command(source_root, cfg, candidate)})
                else:
                    entry['proposal_exhausted'] = True
            if entry['status'] not in {'ACTIVE', 'DRAINING'}:
                continue
            for config_key, candidate_state in entry.get('candidates', {}).items():
                if candidate_state['status'] == 'BUILDING':
                    build_result = self.out / 'jobs' / ('build-' + config_key + '.result.json')
                    if not build_result.exists():
                        continue
                    if load(build_result)['status'] != 'completed':
                        candidate_state['status'] = 'BUILD_FAILED'
                        continue
                    candidate = self.out / 'builds' / config_key
                    base = self.out / 'builds' / candidate_state['base_key']
                    if not (base / 'build.json').exists():
                        continue
                    changed = [case for case in candidate_state['cases']
                        if digest(candidate / 'hardware.json') != digest(base / 'hardware.json')
                        or digest(candidate / 'programs' / (case + '.asm')) != digest(base / 'programs' / (case + '.asm'))]
                    if entry['status'] == 'DRAINING':
                        candidate_state['status'] = 'CANCELLED'
                        continue
                    # 基准也要有真实性能证据；哈希去重令多个目标共享一次评估。
                    for origin, cases in [(base, candidate_state['cases']), (candidate, changed)]:
                        for case in cases:
                            evaluation = self.case_job(origin, case, [7, 123], source_root=source_root)
                            evaluation['target_owned'] = True
                            candidate_state['case_keys'].extend([evaluation['key'], evaluation['followup']['key']])
                            self.enqueue(evaluation)
                    candidate_state['case_keys'] = sorted(set(candidate_state['case_keys']))
                    candidate_state['duplicate'] = not changed
                    candidate_state['status'] = 'EVALUATING'
                if candidate_state['status'] == 'EVALUATING':
                    paths = [self.out / 'jobs' / (identity + '.result.json') for identity in candidate_state['case_keys']]
                    if all(path.exists() for path in paths):
                        candidate_state['status'] = ('DUPLICATE' if candidate_state.get('duplicate') else 'OBSERVED') if all(load(path)['status'] == 'completed' for path in paths) else 'EVALUATION_FAILED'
            terminal = {'STATIC_REJECTED', 'BUILD_FAILED', 'DUPLICATE', 'OBSERVED', 'EVALUATION_FAILED', 'CANCELLED'}
            entry['trials_completed'] = sum(c['status'] in terminal for c in entry.get('candidates', {}).values())
            all_proposed = ('max_inflight' not in definition or entry.get('proposal_exhausted')
                or sum(c['status'] != 'CANCELLED' for c in entry.get('candidates', {}).values()) >= definition['max_trials'])
            feedback_done = not adaptive or (not entry.get('adaptive', {}).get('pending_jobs') and all(
                trial.get('told') for trial in entry.get('adaptive', {}).get('trials', {}).values()))
            if feedback_done and entry['trials_completed'] == entry['trials_launched'] and (all_proposed or entry['status'] == 'DRAINING'):
                entry['status'] = 'STOPPED' if entry['status'] == 'DRAINING' else 'DONE'
        self.reconcile_target_controls()
        open_jobs = [job for job in self.pending if job['stage'] != 'report']
        open_jobs += [item['job'] for item in getattr(self, 'active', {}).values()
                      if item['job']['stage'] != 'report']
        requests = self.triggers.poll(self.pool.state['targets'], len(open_jobs),
            review_exhaustion=getattr(getattr(self, 'args', None), 'ai_enabled', False)
                and not open_jobs and not self.external())
        for request in requests:
            lane = self.triggers.lane(request['lane'])
            wanted = set(request['observation_ids'])
            snapshot = {'source_epoch': self.pool.source_epoch, 'campaign': self.out.name,
                'request': request, 'observations': [o for o in lane['observations'] if o['id'] in wanted],
                'recent_observations': lane['observations'][-max(self.triggers.stagnation_trials, self.triggers.batch_size):],
                'completed_profiles': {identifier: lane.get('profiles',{}).get(identifier)
                                       for identifier in request.get('profile_ids',[])},
                'trigger_policy': {'stagnation_trials': self.triggers.stagnation_trials,
                                   'improvement_threshold': self.triggers.improvement_threshold,
                                   'comparison': '同硬件同案例，功能通过且峰值功耗不超过 20 W 的独立观测'},
                'remaining_budget': self.budget.snapshot(), 'allowed_variables': DOMAINS,
                'scheduler': {'workers': self.workers, 'official_slots': self.args.full_slots,
                    'active_jobs': len(self.active), 'pending_jobs': len(self.pending),
                    'memory_budget_bytes': self.memory_budget}}
            if request['lane'] == 'global':
                snapshot['cross_lane'] = self.global_snapshot()
                exhausted = 'pool_exhausted' in request['reasons']
                snapshot['analysis_task'] = (
                    '你是此批次唯一的全局研究分析者。检查同硬件P1/D1、官方整案成绩、各目标完成和失败、剩余评估预算；'
                    '优先找可能带来百分比级收益的结构与交互，而非反复追逐千分之一的参数抖动。'
                    '如果当前目标仍在运行，允许先分析已完成方向并提出不同方向的互补目标，不要等待整池清空。'
                    '提出2至4个有证据、互不重复的合法目标，并明确变量、起点、优先级和有限试验预算；'
                    '只能从 available_base_records 选择参数目标的 base_record；最高分跨源码组合只用于整案比较，'
                    '不能当作单生成器搜索起点。没有可用新参数时请提出结构假设。'
                    '硬件改变必须覆盖P1和D1。若只有小收益且可表达的参数空间已经覆盖，转而提出具体实现变更假设。'
                    '结构任务的预算耗尽和基础设施错误不是性能观测，不得据此判定结构无效。'
                    '审阅 docs/architecture.md、docs/knowledge.md、docs/automation-plan.md 与相关源码；'
                    '可以按需读取仓库文件，但不能改文件或运行昂贵评估。'
                    '不得参考其他参与者的实现或代理输出；不得把别人的成绩当成本项目事实。'
                    + ('本次目标池已耗尽，尤其需要补充新方向。' if exhausted else '多个方向仍在并行评估，可立即补充不同方向。'))
            atomic_json(self.out / 'ai' / (request['decision_id'] + '.snapshot.json'), snapshot)
        self.pool.save()

    def global_snapshot(self):
        """跨方向只给短事实及记录ID；可读原件仍由只读分析者按需查看。"""
        rows=read()
        facts=[]
        for record in rows:
            if record.get('campaign') != self.out.name or record.get('scope') not in {'both','full'}:
                continue
            config=record.get('config',{})
            hardware=config.get('hardware',{})
            settings=config.get('programs',{}).get('M1_P1',{}).get('config',{})
            for case,info in record.get('cases',{}).items():
                timing=info.get('timing',{})
                if timing.get('cycles') is None:
                    continue
                facts.append({'record_id':record['id'],'case':case,'cycles':timing['cycles'],
                    'peak_power_w':timing.get('peak_window_power_w'),
                    'functional_passed':info.get('functional_passed'),
                    'source_root':record.get('source_root'),
                    'hardware':{name:hardware.get(name) for name in ('cache_mib','reduction_units','sfu_lanes')},
                    'p1':{name:settings.get(name) for name in ('w1_preload_k','w2_preload_k',
                        'attention_query_tile','attention_key_tile','attention_value_tile')},
                    'full_score':record.get('score') if record.get('scope')=='full' else None})
        audited=sorted((record for record in rows if record.get('scope')=='full' and
            record.get('audited') and record.get('eligible') and isinstance(record.get('score'),(int,float))),
            key=lambda record:record['score'],reverse=True)[:3]
        family_bases=[{'record_id':record['id'], 'source_root':record['source_root'],
                       'cases':{case:info.get('timing',{}).get('cycles')
                                for case,info in record.get('cases',{}).items()}}
                      for record in rows if record.get('source_root','').startswith((
                          'workspace/families/', 'workspace/implementation-loop/'))
                      and record.get('source_sha256') and isinstance(record.get('config'),dict)
                      and any(info.get('timing',{}).get('cycles') for info in record.get('cases',{}).values())]
        promoted=load(ROOT/'data/state.json').get('promoted_record')
        available=[record['id'] for record in rows if isinstance(record.get('config'),dict)
                   and record.get('source_available') is not False
                   and record.get('reproduction')!='verified_composite'
                   and (record['id']==promoted or record.get('candidate') and
                        (ROOT/record['candidate']).exists())]
        return {'targets':[{'target_id':key,'lane':entry['definition']['lane'],
                    'status':entry['status'],'variables':entry['definition']['variables'],
                    'proposed':entry.get('trials_launched'),'completed':entry.get('trials_completed')}
                    for key,entry in self.pool.state['targets'].items()],
                'case_facts':facts[-80:],
                'available_family_bases':family_bases[-8:],
                'available_base_records':available[-12:],
                'best_audited':[{'record_id':record['id'],'score':record['score']}
                                for record in audited]}

    def process_profile_requests(self):
        """profile 先重建核对字节，再占探索槽位进行真实时序跟踪。"""
        from .profiles import inputs
        records={record['id']:record for record in read()}
        statuses=self.pool.state.setdefault('profiles',{})
        for decision_id,accepted in self.pool.state.get('applied_decisions',{}).items():
            for request in accepted['decision']['profile_requests']:
                identity=key([request['record_id'],request['case'],self.pool.source_epoch])
                if identity in statuses:
                    entry=statuses[identity]
                    lanes=entry.setdefault('lanes',[entry['lane']])
                    if accepted['lane'] not in lanes:
                        lanes.append(accepted['lane'])
                        if entry['status'] in {'DONE','FAILED','REJECTED'}:self.notify_profile(identity)
                    continue
                entry={'record_id':request['record_id'],'case':request['case'],'lane':accepted['lane'],
                       'lanes':[accepted['lane']],'decision_id':decision_id,'status':'QUEUED','reason':request['reason']}
                statuses[identity]=entry
                try:
                    record=records[request['record_id']]
                    original,report=inputs(record,request['case'])
                    source_root=Path(record.get('source_root') or ROOT)
                    source_root=(source_root if source_root.is_absolute() else ROOT/source_root).resolve()
                    if source_root != ROOT:
                        if not (source_root.is_relative_to(ROOT/'workspace/families') or
                                source_root.is_relative_to(ROOT/'workspace/implementation-loop')):
                            raise ValueError('profile 来源不在受控实现族目录')
                        source_files=source_identity_at(source_root)
                        if key(source_files)!=record.get('source_sha256') or \
                                digest(source_root/'vendor/official/isolation-manifest.json')!= \
                                digest(ROOT/'vendor/official/isolation-manifest.json'):
                            raise ValueError('profile 冻结源码或官方工具身份不匹配')
                        self.family_sources[str(source_root)]=source_files
                    root=self.out/'profiles'/identity
                    config=root/'config.json';atomic_json(config,record['config'])
                    candidate=root/'candidate';output=root/'trace.json'
                    followup={'key':'profile-'+identity,'stage':'profile','profile_id':identity,
                        'record_id':record['id'],'case':request['case'],'report':str(output),
                        'candidate':str(candidate),'memory_bytes':2*1024**3,
                        'command':[self.python,'-m','codesign_lab.evaluation.trace',str(candidate),
                                   '--case',request['case'],'--compare',str(report),'--out',str(output)]}
                    self.enqueue({'key':'profile-build-'+identity,'stage':'profile_build','profile_id':identity,
                        'candidate':candidate,'memory_bytes':1024**3,'followup':followup,
                        'command':self.build_command(source_root,config,candidate)+
                                   ['--verify',str(original)]})
                except (KeyError,ValueError,OSError) as exc:
                    entry.update(status='REJECTED',error=str(exc))
                    self.notify_profile(identity)

    def notify_profile(self,identity):
        """成功与失败均通知订阅 lane；失败没有虚构性能指标。"""
        entry=self.pool.state['profiles'][identity]
        summary=(entry.get('summary') or {'record_id':entry['record_id'],'case':entry['case'],
            'status':'DONE','evidence':entry.get('evidence'),'scope':'旧诊断已完成，摘要未保留'}) if entry['status']=='DONE' else {
            'record_id':entry['record_id'],'case':entry['case'],'status':entry['status'],
            'error':entry.get('error'),'stage':entry.get('failed_stage'),
            'scope':'诊断请求失败，不是性能观测'}
        for lane in entry.get('lanes',[entry['lane']]):
            self.triggers.profile_ready(lane,identity,summary)

    def reconcile_target_controls(self):
        """缩减预算只撤回未启动候选，共享任务随后按剩余订阅处理。"""
        from .target_control import started_candidates, priorities
        for entry in self.pool.state['targets'].values():
            if not entry.pop('budget_reconcile', False):continue
            protected = started_candidates(entry, self.pool.state.get('jobs', {}))
            slots = max(0, entry['definition']['max_trials'] - len(protected))
            for identity,candidate in entry.get('candidates',{}).items():
                if identity in protected or candidate['status'] != 'BUILDING':continue
                if slots:
                    slots -= 1
                else:
                    candidate['status'] = 'CANCELLED'
                    candidate['cancel_reason'] = '目标剩余预算缩减'
        weights = priorities(self.pool.state['targets'])
        for job in self.pending:
            if job['key'] not in weights and 'legacy_priority' not in job:continue
            if not job.get('target_owned'):
                job.setdefault('legacy_priority',job.get('priority',0))
            value = max(weights.get(job['key'],0),job.get('legacy_priority',0))
            if job.get('priority',0) != value:
                job['priority'] = value
                self.persist_job(job,'QUEUED')

    def cancel_stopped_targets(self):
        """撤回停止目标的订阅，保留其他目标及旧配置仍需要的任务。"""
        required = set()
        for entry in self.pool.state['targets'].values():
            if entry['status'] in {'DRAINING', 'STOPPED'}:
                for candidate in entry.get('candidates', {}).values():
                    if candidate['status'] in {'BUILDING', 'EVALUATING'}:
                        candidate['status'] = 'CANCELLED'
                entry['trials_completed'] = entry['trials_launched']
                entry['status'] = 'STOPPED'
            elif entry['status'] == 'ACTIVE':
                required.update(entry.get('adaptive', {}).get('pending_jobs', []))
                required.update(entry.get('adaptive', {}).get('prior_build_keys', []))
                if entry.get('adaptive') and entry.get('base_key'):
                    required.add('build-' + entry['base_key'])
                for identity, candidate in entry.get('candidates', {}).items():
                    if candidate['status'] == 'BUILDING':
                        required.update({'build-' + identity, 'build-' + candidate['base_key']})
                    if candidate['status'] == 'EVALUATING':
                        required.update(candidate.get('case_keys', []))
        for job in list(self.pending):
            if job.get('target_owned') and job['key'] not in required:
                self.pending.remove(job)
                self.persist_job(job, 'CANCELLED')
                # 之后的新目标仍可以重新订阅尚未执行的相同任务。
                self.seen.discard(job['key'])

    def case_job(self, candidate, case, seeds, source_root=None):
        from ..evaluation.cache import engine_identity
        identity = key([digest(candidate / 'hardware.json'), digest(candidate / 'programs' / (case + '.asm')),
                        case, seeds, engine_identity(ROOT / 'vendor/official'), self.manifest['baseline_sha256']])
        report = self.out / 'reports' / (identity + '.json')
        functional_report = self.out / 'reports' / (identity + '.functional.json')
        estimate = task(self.python, candidate, case, seeds, report, 'case-' + identity,
                        (2 if case == 'M1_P1' else 1) * 1024**3,
                        cache=getattr(getattr(self,'args',None),'cache_dir',None) or True, mode='estimate', functional_report=functional_report)
        estimate.update(stage='case', stage_mode='estimate', candidate=candidate, case=case,
                        report=report, seeds=seeds, source_root=str(source_root or ROOT))
        functional = task(self.python, candidate, case, seeds, functional_report,
                          'functional-' + identity, (2 if case == 'M1_P1' else 1) * 1024**3,
                          cache=getattr(getattr(self,'args',None),'cache_dir',None) or True, mode='functional')
        functional.update(stage='functional', candidate=candidate, case=case,
                          report=functional_report, seeds=seeds, followup=estimate,
                          source_root=str(source_root or ROOT))
        return functional

    def complete_functional(self, job, result):
        """只有完整功能证据才释放时序，失败保存终态供订阅者恢复。"""
        from ..evaluation.stages import functional_evidence
        data = load(job['report']) if Path(job['report']).exists() else {}
        info = data.get('cases', {}).get(job['case'], {})
        followup = dict(job['followup'])
        for field in ['candidate', 'report']:
            followup[field] = Path(followup[field])
        if job.get('target_owned'):
            followup['target_owned'] = True
        try:
            if result['status'] != 'completed':
                raise ValueError('功能阶段未正常完成')
            bootstrap()
            from codesign.challenge.hardware import Hardware
            from codesign.challenge.runner import provenance
            hardware = load(Path(job['candidate']) / 'hardware.json')
            programs = {case: (Path(job['candidate']) / 'programs' / (case + '.asm')).read_text()
                        for case in ['M1_P1', 'M2_D1']}
            expected = provenance(Hardware.from_dict(hardware), programs)
            functional_evidence(data, job['case'], hardware,
                digest(Path(job['candidate']) / 'programs' / (job['case'] + '.asm')),
                expected, job['seeds'])
        except ValueError as exc:
            failed = dict(result, status='failed', dependency_error=str(exc))
            # 使用真实功能报告登记失败，不运行任何时序命令。
            followup['report'] = Path(job['report'])
            atomic_json(self.out / 'jobs' / (followup['key'] + '.result.json'), failed)
            self.persist_job(followup, 'FINISHED')
            self.seen.add(followup['key'])
            self.completed(followup, failed)
            return
        hits = {hit.get('seed') for hit in info.get('cache_hits', []) if hit['stage'] == 'functional'}
        if set(job['seeds']) <= hits:
            self.budget.reused(job.get('budget_key', job['key']))
        self.enqueue(followup)
        self.pool.save()

    def evaluate_candidate(self, job):
        for case in job['cases']:
            self.enqueue(self.case_job(job['candidate'], case, job['seeds'],
                                       source_root=job.get('source_root')))

    def scan(self):
        # 只接纳指定批次的最终报告，不将正在写入的检查点当作成功结果。
        for name in self.args.watch:
            root = ROOT / 'workspace/search' / name
            for report in (root / 'reports').glob('*.json'):
                if '.progress.' in report.name:
                    continue
                data = load(report)
                if not data.get('completed_without_error'):
                    continue
                candidate = Path(data['candidate'])
                if not candidate.is_relative_to(ROOT) or not candidate.exists():
                    continue
                if data['hardware'] != load(candidate / 'hardware.json'):
                    continue
                for case, info in data['cases'].items():
                    if info.get('functional_passed') and 'timing' in info and data['program_sha256'][case] == digest(candidate / 'programs' / (case + '.asm')):
                        timing = info['timing']
                        self.observe(candidate, case, timing['cycles'], timing['peak_window_power_w'])
        self.shortlist()

    def shortlist(self):
        choices = []
        for hw, row in self.best.items():
            if set(row) != {'M1_P1', 'M2_D1'}:
                continue
            p1, d1 = row['M1_P1'], row['M2_D1']
            area = load(p1['candidate'] / 'hardware.json')
            from codesign.challenge.hardware import Hardware
            score = self.evaluate_score(Hardware.from_dict(area).area_mm2(),
                {case: self.Metric(r['cycles'], r['power'], True) for case, r in row.items()}, self.baseline)
            if not score['eligible'] or score['score'] <= self.initial_score + getattr(self.args, 'min_predicted_gain', 0):
                continue
            identity = key([hw, digest(p1['candidate'] / 'programs/M1_P1.asm'), digest(d1['candidate'] / 'programs/M2_D1.asm')])
            if identity in self.full_seen:
                continue
            if not self.finalist_capacity(score['score'], digest(p1['candidate'] / 'hardware.json')):
                continue
            choices.append((score['score'], identity, p1, d1))
        for score, identity, p1, d1 in sorted(choices, key=lambda x: x[0], reverse=True)[:3]:
            candidate = self.out / 'finalists' / identity
            candidate.mkdir(parents=True, exist_ok=True)
            (candidate / 'programs').mkdir(exist_ok=True)
            shutil.copy2(p1['candidate'] / 'hardware.json', candidate / 'hardware.json')
            for case, r in [('M1_P1', p1), ('M2_D1', d1)]:
                shutil.copy2(r['candidate'] / 'programs' / (case + '.asm'), candidate / 'programs' / (case + '.asm'))
            config = load(p1['candidate'] / 'config.json')
            config['programs']['M2_D1'] = load(d1['candidate'] / 'config.json')['programs']['M2_D1']
            atomic_json(candidate / 'config.json', config)
            atomic_json(candidate / 'selection.json', {'predicted_score': score,
                'parents': {case: {'candidate': str(row['candidate']),
                                   'source_root': str(row['source_root']),
                                   'config_path': str(row['config_path']),
                                   'config_sha256': row['config_sha256'],
                                   'generator_sha256': row['generator_sha256'],
                                   'source_sha256': row['source_sha256']}
                            for case, row in [('M1_P1', p1), ('M2_D1', d1)]},
                'scope': '单案推算，非正式成绩；来源在正式验收前逐字节再生'})
            report = self.out / 'grades' / (identity + '.json')
            self.full_seen.add(identity)
            followup = {'key': 'full-' + identity, 'stage': 'full', 'candidate': str(candidate),
                'hardware_hash': digest(candidate / 'hardware.json'), 'pair_id': identity,
                'report': str(report), 'priority': score, 'memory_bytes': 4 * 1024**3,
                'command': [self.python, '-m', 'codesign_lab.evaluation.official', str(candidate), '--report', str(report)]}
            self.enqueue({'key': 'verify-' + identity, 'stage': 'verify', 'candidate': candidate,
                'hardware_hash': digest(candidate / 'hardware.json'), 'pair_id': identity,
                'priority': score, 'memory_bytes': 1024**3, 'followup': followup,
                'command': [self.python, '-m', 'codesign_lab.search.pair_verify',
                    str(candidate), '--out', str(self.out / 'verified' / identity)]})
        self.trim_finalists()


    def external(self):
        count = 0
        for name in self.args.watch:
            status = ROOT / 'workspace/search' / name / 'eval-jobs/status.json'
            if status.exists():
                for item in load(status).get('active', []):
                    try:
                        sample = sample_tree(item['pid'])
                        if sample.get('state') != 'Z':
                            count += 1
                    except (OSError, ValueError, IndexError):
                        pass
        return count

    def process_implementations(self):
        """把结构提案放入现有持久任务队列，由同一调度器准入和恢复。"""
        if not getattr(self.args, 'implementation_enabled', False):
            return
        from .implementation import proposals, TERMINAL
        directory = ROOT / 'workspace/implementation-loop' / self.out.name
        lane = self.pool.state.get('analysis', {}).get('lanes', {}).get('global', {})
        if not lane.get('session_id'):
            return
        active = sum(job['stage'].startswith('implementation_') for job in self.pending)
        active += sum(item['job']['stage'].startswith('implementation_') for item in self.active.values())
        for identifier, item in proposals(self.out):
            if active >= self.args.implementation_max_proposals:
                break
            state = directory / identifier / 'state.json'
            state_data = load(state) if state.exists() else {}
            status = state_data.get('status', 'QUEUED')
            if status in TERMINAL | {'WAITING_FOR_LAUNCH'} or status not in {
                    'QUEUED', 'CODED', 'OFFICIAL_QUEUED', 'GRADED', 'RESEARCH_READY'}:
                continue
            phase = ('code' if status == 'QUEUED' else
                     'official' if status == 'OFFICIAL_QUEUED' else 'validate')
            stage = 'implementation_' + phase
            retry = int(state_data.get('recovery_attempts', 0))
            job_key = stage + '-' + identifier + ('-recovery' + str(retry) if retry else '')
            if job_key in self.seen:
                continue
            self.enqueue({'key': job_key, 'stage': stage,
                'proposal_id': identifier, 'priority': -1,
                'memory_bytes': (2 if phase == 'code' else 8) * 1024**3,
                'timeout': 2400 if phase == 'code' else 10800 if phase == 'official' else 7200,
                'command': [self.python, '-m', 'codesign_lab.search.implementation',
                    '--campaign', self.out.name, '--proposal-id', identifier, '--execute',
                    '--worker', '--phase', phase, '--model', self.args.implementation_model,
                    '--reasoning-effort', self.args.implementation_effort,
                    '--min-case-gain', str(self.args.implementation_min_case_gain),
                    '--min-score-gain', str(self.args.implementation_min_score_gain)]})
            active += 1

    def completed(self, job, result):
        self.done.append(result)
        if job['stage'] in {'implementation_code', 'implementation_validate', 'implementation_official'}:
            state_path = ROOT / 'workspace/implementation-loop' / self.out.name / job['proposal_id'] / 'state.json'
            state = load(state_path) if state_path.exists() else {}
            if state.get('status') == 'TARGET_QUEUED' and result['status'] == 'completed':
                self.pool.consume()
                receipt = self.pool.state.get('requests', {}).get(state['target_request_id'])
                if receipt and receipt.get('status') in {'accepted', 'reused'}:
                    state.update(status='TARGET_INJECTED', target_id=receipt.get('target_id'),
                                 updated_wall=time.time())
                    if isinstance(state.get('official_score'), (int, float)):
                        self.initial_score = max(self.initial_score, state['official_score'])
                else:
                    state.update(status='FAILED', updated_wall=time.time(),
                                 error='结构研究目标注入失败：' + str(receipt))
                atomic_json(state_path, state)
            if result['status'] == 'budget_exhausted' and state.get('status', 'QUEUED') in {
                    'QUEUED', 'CODED', 'OFFICIAL_QUEUED'}:
                state.update(status='BUDGET_EXHAUSTED', updated_wall=time.time(),
                    error='本批次官方整案调用预算已用尽，结构提案没有启动')
                atomic_json(state_path, state)
            elif result['status'] != 'completed' and state.get('status') not in {
                    'REJECTED', 'FAILED', 'WAITING_FOR_LAUNCH', 'LAUNCHED', 'AUDITED_NO_PROMOTION'}:
                state.update(status='FAILED', updated_wall=time.time(),
                    error='结构任务由统一监督器结束：' + result['status'] +
                          ('；' + str(result['error']) if result.get('error') else ''))
                atomic_json(state_path, state)
            status = state.get('status', 'MISSING')
            if ((job['stage'] == 'implementation_code' and status == 'CODED') or
                    (job['stage'] == 'implementation_validate' and status == 'OFFICIAL_QUEUED')) and \
                    result['status'] == 'completed':
                self.process_implementations()
                return
            if job['stage'] == 'implementation_official' and job.get('budget_key') and \
                    not state.get('official_attempted'):
                self.budget.reused(job['budget_key'])
            if job['stage'] == 'implementation_validate' and job.get('budget_key') and \
                    not state.get('case_attempted'):
                self.budget.reused(job['budget_key'])
            if job['stage'] == 'implementation_validate' and status == 'FAILED' and \
                    result['status'] == 'completed' and \
                    state.get('recovery_attempts', 0) < 1 and not self.budget.expired() and \
                    str(state.get('error', '')).startswith((
                        'RuntimeError: default-off 失败', 'RuntimeError: regression 失败')):
                from .implementation import retry_infrastructure_failure
                try:
                    state = retry_infrastructure_failure(state_path)
                    state['recovery_attempts'] = 1
                    atomic_json(state_path, state)
                    self.process_implementations()
                    status = state['status']
                except (ValueError, OSError):
                    # 校验不满足可恢复条件时保留原失败证据，交给全局分析。
                    state = load(state_path)
                    status = state['status']
            if status in {'REJECTED', 'RESEARCH_PAUSED', 'FAILED', 'WAITING_FOR_LAUNCH', 'LAUNCHED',
                          'TARGET_INJECTED',
                          'AUDITED_NO_PROMOTION'}:
                # 同一提案的恢复验证可能产生新的事实；按持久任务身份区分，
                # 恢复启动时重放相同任务仍会被触发器去重。
                task_identity = job.get('key', job['stage'] + '-' + job['proposal_id'])
                timing = state.get('timing')
                if isinstance(timing, dict):
                    timing = {name: value for name, value in timing.items()
                              if name != 'resource_stats'}
                self.triggers.observe('global', {'id': 'implementation-' + task_identity,
                    'observation_kind': 'implementation', 'proposal_id': job['proposal_id'],
                    'case': state.get('proposal', {}).get('case'), 'proposal_status': status,
                    'case_gain': state.get('case_gain'), 'official_score': state.get('official_score'),
                    'timing': timing, 'research_record': state.get('research_record'),
                    'target_id': state.get('target_id'),
                    'error': state.get('error') or state.get('last_error')})
                self.pool.save()
            print(json.dumps({'event': 'implementation_finished', 'proposal_id': job['proposal_id'],
                'task_status': result['status'], 'proposal_status': status}, ensure_ascii=False), flush=True)
            return
        if job['stage'] == 'sample':
            from .adaptive import complete
            complete(self, job, result)
            return
        if job.get('profile_id') and result['status'] != 'completed':
            self.pool.state['profiles'][job['profile_id']].update(status='FAILED',
                error=result.get('error',result['status']),failed_stage=job['stage'])
            self.notify_profile(job['profile_id'])
            self.pool.save()
        if result['status'] != 'completed' and job['stage'] not in {'case', 'functional', 'audit'}:
            return
        if job['stage'] == 'functional':
            self.complete_functional(job, result)
        elif job['stage'] in {'verify', 'profile_build'}:
            if job['stage'] == 'verify' and job.get('pair_id'):
                evidence = self.out / 'verified' / job['pair_id'] / 'pair-verify.json'
                if evidence.is_file() and load(evidence).get('mode') == 'mixed_sources':
                    self.pool.state.setdefault('mixed_pairs', {})[job['pair_id']] = {
                        'status': 'VERIFIED_QUEUED_FOR_FULL_GRADE',
                        'candidate': str(job['candidate']), 'evidence': str(evidence)}
                    self.pool.save()
            next_job = dict(job['followup'])
            next_job['candidate'] = Path(next_job['candidate'])
            next_job['report'] = Path(next_job['report'])
            self.enqueue(next_job)
        elif job['stage'] == 'profile':
            from ..evaluation.profile import summarize,stage_labels,stage_pressure
            from ..records import update
            try:
                traced=load(job['report'])
                if traced.get('complete_timing_identical') is not True:
                    raise ValueError('profile 未证明时序一致')
            except (ValueError,OSError) as exc:
                self.pool.state['profiles'][job['profile_id']].update(status='FAILED',error=str(exc),failed_stage='validation')
                self.notify_profile(job['profile_id']);self.pool.save();return
            summary=summarize(traced['timing'])
            operators=[stage|stage_labels(stage)|{'pressure_estimate':stage_pressure(stage,summary.get('timeline'))}
                       for stage in traced.get('operator_spans',[])]
            summary.update(timeline=None,operator_spans=operators,
                           stage_status='实测事件轨迹，时序与原报告完全一致')
            record=next(record for record in read() if record['id']==job['record_id'])
            profiles=dict(record.get('profile',{}));profiles[job['case']]=summary
            evidence=dict(record.get('profile_evidence',{}))
            evidence[job['case']]={'path':reference(job['report']),'sha256':digest(job['report'])}
            update(record['id'],{'profile':profiles,'profile_evidence':evidence})
            entry=self.pool.state['profiles'][job['profile_id']]
            entry.update(status='DONE',evidence=evidence[job['case']])
            entry['summary']={'record_id':record['id'],'case':job['case'],'evidence':entry['evidence'],
                 'cycles':summary.get('cycles'),'pressure_leader':summary.get('pressure_leader'),
                 'operator_count':len(operators),'status':'DONE','scope':'实测阶段，资源压力是推算'}
            self.notify_profile(job['profile_id'])
            self.pool.save()
        elif job['stage'] == 'build':
            self.evaluate_candidate(job)
        elif job['stage'] == 'case':
            record_result(self.out.name + '-' + job['key'], self.out.name,
                          job['candidate'], job['case'], job['report'], result,
                          source_root=job.get('source_root'))
            data = load(job['report']) if job['report'].exists() else {}
            info = data.get('cases', {}).get(job['case'], {})
            hits = {hit['stage'] for hit in info.get('cache_hits', [])}
            functional_hits = [hit.get('seed') for hit in info.get('cache_hits', []) if hit['stage'] == 'functional']
            if 'timing' in hits and (job.get('stage_mode') == 'estimate' or set(job.get('seeds', [7, 123])) <= set(functional_hits)):
                self.budget.reused(job.get('budget_key', job['key']))
                self.pool.save()
            observation={'id': self.out.name + '-' + job['key'],
                'observation_kind': 'case_result', 'record_id': self.out.name + '-' + job['key'],
                'case': job['case'], 'functional_passed': info.get('functional_passed'),
                'hardware_hash': digest(job['candidate'] / 'hardware.json'),
                'cycles': info.get('timing', {}).get('cycles'),
                'peak_power_w': info.get('timing', {}).get('peak_window_power_w'),
                'cache_reused': 'timing' in hits,
                'failure_kind': 'infrastructure' if (info.get('error') or result['status'] != 'completed')
                    and info.get('functional_passed') is not False else None}
            if self.triggers.global_only:
                self.triggers.observe('global', observation)
            for target in self.pool.state['targets'].values():
                if target['status'] == 'STOPPED':
                    continue
                if any(job['key'] in c.get('case_keys', []) for c in target.get('candidates', {}).values()):
                    self.triggers.observe(target['definition']['lane'], observation)
            from .samplers import FiniteSampler, target_seed
            for target in self.pool.state['targets'].values():
                state = target.get('sampler_state')
                candidate_id = job['candidate'].name
                if target['status'] == 'STOPPED' or not state or candidate_id not in state['asked']:
                    continue
                definition = target['definition']
                feedback = FiniteSampler(target['base_config'], definition['variables'],
                    sampler=definition.get('sampler', 'enumerate'),
                    seed=target_seed(definition) if definition.get('sampler') == 'random' else 0,state=state)
                feedback.tell(candidate_id, {'id': self.out.name + '-' + job['key'], 'case':job['case'],
                    'hardware_hash':digest(job['candidate']/'hardware.json'),
                    'functional_passed':info.get('functional_passed'),
                    'cycles':info.get('timing',{}).get('cycles'),
                    'peak_power_w':info.get('timing',{}).get('peak_window_power_w'),
                    'cache_reused':'timing' in hits,
                    'failure_kind':'infrastructure' if (info.get('error') or result['status']!='completed') and info.get('functional_passed') is not False else None})
            if info.get('functional_passed') and 'timing' in info:
                timing = info['timing']
                self.observe(job['candidate'], job['case'], timing['cycles'], timing['peak_window_power_w'],
                             source_root=job.get('source_root'))
                self.shortlist()
        elif job['stage'] == 'full':
            data = load(job['report']);identifier = self.out.name + '-' + job['key']
            if identifier not in {r['id'] for r in read()}:
                from ..evaluation.pipeline import compact_cases
                append({'id': identifier, 'campaign': self.out.name, 'scope': 'full',
                    'config': load(job['candidate'] / 'config.json'),
                    'cases': compact_cases(data.get('cases', {})),
                    'eligible': data.get('eligible'), 'score': data.get('experimental_score'),
                    'report': reference(job['report']), 'candidate': reference(job['candidate']),
                    'provenance': data.get('provenance'), 'audited': False,
                    'reproduction': 'record_only', 'host_seconds': result['wall_seconds']})
            print(json.dumps({'event': 'official_grade_finished', 'record': identifier,
                              'eligible': data.get('eligible'), 'score': data.get('experimental_score')}, ensure_ascii=False), flush=True)
            if data.get('eligible') is not True or getattr(self.args, 'no_auto_audit', False):
                self.triggers.observe('global', {'id': 'official-' + identifier,
                    'observation_kind': 'official_result', 'record_id': identifier,
                    'eligible': data.get('eligible'), 'score': data.get('experimental_score'),
                    'audited': False, 'report': reference(job['report'])})
                self.pool.save()
            if data.get('eligible') is True and not getattr(self.args, 'no_auto_audit', False):
                self.enqueue({'key': 'audit-' + job['key'], 'stage': 'audit', 'record_id': identifier,
                    'priority': data.get('experimental_score', 0), 'memory_bytes': 1024**3,
                    'command': [self.python, '-m', 'codesign_lab.evaluation.audit', identifier]})
        elif job['stage'] == 'audit':
            record = next(r for r in read() if r['id'] == job['record_id'])
            if record.get('audited') and record.get('eligible'):
                self.initial_score = max(self.initial_score, record['score'])
            self.triggers.observe('global', {'id': 'official-' + record['id'],
                'observation_kind': 'official_result', 'record_id': record['id'],
                'eligible': record.get('eligible'), 'score': record.get('score'),
                'audited': record.get('audited'), 'report': record.get('report'),
                'audit_error': None if result['status'] == 'completed' else result.get('error', result['status'])})
            self.pool.save()
            print(json.dumps({'event': 'official_audit_finished', 'record': record['id'],
                'audited': record.get('audited'), 'report': record.get('report')}, ensure_ascii=False), flush=True)

    def run(self):
        self.prepare()
        try:
            while True:
                self.process_targets()
                self.scan()
                self.analyst.poll()
                self.process_implementations()
                self.trim_finalists()
                elapsed = time.monotonic() - self.started
                expired = self.budget.expired()
                if source_identity() != self.source:
                    raise ValueError('源码在运行期间改变，停止流水线')
                if any(source_identity_at(Path(source)) != identity
                       for source, identity in self.family_sources.items()):
                    raise ValueError('冻结实现族源码在运行期间改变，停止流水线')
                external = self.external()
                if not expired or any(job['stage'] == 'audit' for job in self.pending):
                    eligible_jobs = self.pending if not expired else [job for job in self.pending if job['stage'] == 'audit']
                    choices = select_jobs(eligible_jobs, [x['job'] for x in self.active.values()], external,
                        self.workers, self.args.full_slots, self.memory_budget,
                        int(resources()['available_memory_bytes'] * self.memory_fraction))
                    for job in choices:
                        job['queue_wait_seconds'] = max(0, time.time() - job.get('queued_wall', time.time()))
                        attempt = job.get('attempt', 1)
                        job['budget_key'] = job['key'] if attempt == 1 else job['key'] + '-attempt' + str(attempt)
                        budget_kind = ('case' if job['stage'] in {'functional', 'implementation_validate'} else
                                       'full' if job['stage'] == 'implementation_official' else job['stage'])
                        if job['stage'] != 'audit' and not self.budget.reserve(job['budget_key'], budget_kind):
                            self.pending.remove(job)
                            result = {'key': job['key'], 'stage': job['stage'], 'status': 'budget_exhausted', 'wall_seconds': 0}
                            atomic_json(self.out / 'jobs' / (job['key'] + '.result.json'), result)
                            self.persist_job(job, 'FINISHED')
                            self.completed(job, result)
                            continue
                        self.pool.save()
                        logs = self.out / 'jobs';logs.mkdir(exist_ok=True)
                        stdout = (logs / (job['key'] + '.attempt' + str(attempt) + '.stdout.log')).open('w')
                        stderr = (logs / (job['key'] + '.attempt' + str(attempt) + '.stderr.log')).open('w')
                        spec = logs / (job['key'] + '.attempt' + str(attempt) + '.spec.json')
                        attempt_result = spec.with_suffix('.result.json')
                        atomic_json(spec, {'job': json.loads(json.dumps(job, default=str)),
                            'cwd': str(ROOT), 'result': str(attempt_result),
                            'timeout': job.get('timeout', self.args.timeout)})
                        self.persist_job(job, 'STARTING', spec=str(spec), attempt_result=str(attempt_result))
                        child = subprocess.Popen([self.python, '-m', 'codesign_lab.evaluation.worker', str(spec)],
                            cwd=ROOT, env=self.env, stdout=stdout, stderr=stderr, start_new_session=True)
                        self.active[job['key']] = {'job': job, 'child': child, 'pid': proc_pid(child.pid),
                            'start': time.monotonic(), 'stdout': stdout, 'stderr': stderr, 'peak': 0,
                            'attempt_result': attempt_result}
                        self.persist_job(job, 'RUNNING', pid=proc_pid(child.pid))
                        self.pending.remove(job)
                for identity, item in list(self.active.items()):
                    from ..evaluation.recovery import supervisor_lost
                    duration = time.monotonic() - item['start']
                    try:
                        item['peak'] = max(item['peak'], sample_tree(item['pid'])['tree_rss_bytes'])
                    except (OSError, ValueError, IndexError):
                        pass
                    if item['attempt_result'].exists():
                        result = load(item['attempt_result'])
                        if item['child'] is not None:
                            item['child'].wait(timeout=5)
                        for field in ['stdout', 'stderr']:
                            if item[field] is not None:
                                item[field].close()
                        del self.active[identity]
                        self.finish_attempt(item['job'], result)
                    elif (item['child'] is not None and item['child'].poll() is not None) or (item['child'] is None and supervisor_lost(self.pool.state['jobs'][identity]['spec'])):
                        from ..evaluation.recovery import reap_orphan
                        spec = self.pool.state['jobs'][identity]['spec']
                        result = reap_orphan(spec)
                        del self.active[identity]
                        for field in ['stdout', 'stderr']:
                            if item[field] is not None: item[field].close()
                        self.finish_attempt(item['job'], result)
                from .status import queue_details, attempt_costs
                current_available = int(resources()['available_memory_bytes'] * self.memory_fraction)
                active_jobs = [item['job'] for item in self.active.values()]
                atomic_json(self.out / 'status.json', {'workers_limit': self.workers, 'full_slots': self.args.full_slots,
                    'memory_budget_bytes': self.memory_budget,
                    'reserved_memory_bytes': sum(job['memory_bytes'] for job in active_jobs),
                    'available_memory_admission_bytes': current_available,
                    'queue': queue_details(self.pending, active_jobs, workers=self.workers,
                        full_slots=self.args.full_slots, external=external, memory_budget=self.memory_budget,
                        available=current_available, expired=expired),
                    'attempt_costs': attempt_costs(self.pool.state),
                    'external_active': external, 'pending': {s: sum(x['stage'] == s for x in self.pending) for s in ['build', 'functional', 'case', 'sample', 'verify', 'full', 'audit', 'report','profile_build','profile','implementation_code','implementation_validate','implementation_official']},
                    'active': [{'key': k, 'stage': x['job']['stage'], 'pid': x['pid'],
                        'start_ticks': load(Path(self.pool.state['jobs'][k]['spec']).with_suffix('.lease.json'))['start_ticks'] if Path(self.pool.state['jobs'][k]['spec']).with_suffix('.lease.json').exists() else None,
                        'queue_wait_seconds': x['job'].get('queue_wait_seconds'),
                        'wall_seconds': time.monotonic()-x['start'], 'peak_rss_bytes': x['peak']} for k,x in self.active.items()],
                    'budget': self.budget.snapshot(), 'completed': len(self.done), 'elapsed_seconds': elapsed, 'admission_closed': expired})
                target_work_pending = any(entry['status'] in {'QUEUED', 'ACTIVE', 'DRAINING'}
                                          for entry in self.pool.state['targets'].values())
                if (getattr(self.args, 'stop_on_exhaustion', False) and
                    getattr(self.args, 'ai_enabled', False) and not expired and
                    not self.active and not self.pending and not self.analyst.active and
                    not external and not target_work_pending):
                    revision=self.triggers.pool_revision(self.pool.state['targets'])
                    global_lane=self.triggers.lane('global')
                    if (revision is not None and global_lane.get('reviewed_pool_revision') == revision
                            and not global_lane.get('pending')):
                        print(json.dumps({'event':'search_exhausted',
                            'reason':'全局分析已确认该目标池版本，无可执行新目标；提前结束而非空转至预算截止',
                            'pool_revision':revision},ensure_ascii=False),flush=True)
                        break
                if not self.active and not self.analyst.active and not any(job['stage'] == 'audit' for job in self.pending) and (expired or (not self.pending and not external and not target_work_pending and not getattr(self.args, 'stay_open', False))):
                    break
                time.sleep(1)
        finally:
            self.analyst.close()
            for item in self.active.values():
                # 主控退出后监督 worker 继续保存结果；下次运行接管同一任务。
                for field in ['stdout', 'stderr']:
                    if item[field] is not None:
                        item[field].close()
            self.pool.save()
        verify_official()
        atomic_json(self.out / 'summary.json', {'jobs': self.done, 'remaining': [x['key'] for x in self.pending], 'wall_seconds': time.monotonic()-self.started})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, action='append', default=[])
    parser.add_argument('--family-config', nargs=2, action='append', default=[],
                        metavar=('FAMILY_ROOT','SEARCH_CONFIG'),
                        help='在同一调度器中运行冻结实现族的搜索配置')
    parser.add_argument('--watch', action='append', default=[])
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--cache-dir',type=Path,help='隔离探索缓存目录，必须位于 workspace；默认共享精确缓存')
    parser.add_argument('--workers', type=int, default=56)
    parser.add_argument('--full-slots', type=int, default=2)
    parser.add_argument('--budget', type=int, default=7200)
    parser.add_argument('--timeout', type=int, default=3600)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--resume', action='store_true', help='恢复同一输入身份的持久任务')
    parser.add_argument('--stay-open', action='store_true', help='队列空闲时保持主控，以便动态注入')
    parser.add_argument('--stop-on-exhaustion', action='store_true',
                        help='全局分析确认目标池耗尽后提前完成本批次')
    parser.add_argument('--max-proposals', type=int, default=128)
    parser.add_argument('--max-case-calls', type=int, default=48)
    parser.add_argument('--max-full-calls', type=int, default=6)
    parser.add_argument('--min-predicted-gain', type=float, default=0)
    parser.add_argument('--ai-enabled', action='store_true')
    parser.add_argument('--ai-timeout', type=int, default=600)
    parser.add_argument('--analysis-model', default='gpt-6-astra')
    parser.add_argument('--analysis-effort', choices=['low','medium','high','xhigh','max'], default='medium')
    parser.add_argument('--analysis-mode', choices=['per_lane','global'], default='per_lane')
    parser.add_argument('--implementation-enabled', action='store_true')
    parser.add_argument('--implementation-model', default='gpt-6-astra')
    parser.add_argument('--implementation-effort', default='medium')
    parser.add_argument('--implementation-max-proposals', type=int, default=2)
    parser.add_argument('--implementation-min-case-gain', type=float, default=.002)
    parser.add_argument('--implementation-min-score-gain', type=float, default=100)
    parser.add_argument('--no-auto-audit', action='store_true', help='关闭合格完整验收后的自动审计与证据保全')
    parser.add_argument('--max-retries', type=int, choices=range(0, 4), default=1, help='已确认基础设施失败的最大重试次数')
    parser.add_argument('--max-ai-calls', type=int, default=10)
    parser.add_argument('--memory-fraction', type=float, default=.75)
    parser.add_argument('--max-pending-full', type=int, default=3)
    parser.add_argument('--analysis-batch-size', type=int, default=8)
    parser.add_argument('--analysis-cooldown', type=int, default=120)
    parser.add_argument('--analysis-low-watermark', type=int, default=8)
    parser.add_argument('--stagnation-trials', type=int, default=12)
    parser.add_argument('--improvement-threshold', type=float, default=.002)
    parser.add_argument('--failure-window', type=int, default=6)
    parser.add_argument('--failure-threshold', type=int, default=5)
    parser.add_argument('--report-interval', type=int, default=30)
    parser.add_argument('--max-profile-calls', type=int, default=2)
    args = parser.parse_args(argv)
    if not 0 < args.full_slots < args.workers or args.budget <= 0 or args.timeout <= 0:
        parser.error('并发、验收槽位和预算无效')
    if args.cache_dir is not None:
        args.cache_dir=(ROOT/args.cache_dir).resolve()
        if not args.cache_dir.is_relative_to(ROOT/'workspace'):
            parser.error('探索缓存必须位于 workspace')
    if not 0 < args.memory_fraction <= .9 or min(args.max_proposals, args.max_case_calls, args.max_full_calls, args.max_ai_calls, args.max_pending_full, args.max_profile_calls, args.implementation_max_proposals) <= 0:
        parser.error('内存比例或累计预算无效')
    if not 0 <= args.implementation_min_case_gain < 1 or args.implementation_min_score_gain < 0:
        parser.error('结构实验门槛无效')
    if args.resume and not args.execute:
        parser.error('--resume 需要 --execute')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+',args.analysis_model):
        parser.error('分析模型无效')
    if min(args.analysis_batch_size, args.stagnation_trials, args.failure_window, args.failure_threshold, args.report_interval) <= 0 or args.failure_threshold > args.failure_window or args.analysis_cooldown < 0 or args.analysis_low_watermark < 0 or not 0 < args.improvement_threshold < 1:
        parser.error('分析触发阈值无效')
    if any(Path(name).name != name for name in args.watch):
        parser.error('watch 必须是 workspace/search 下的批次名称')
    for source, config in args.family_config:
        root = Path(source).resolve()
        if not root.is_relative_to(ROOT / 'workspace/families') or not (root / 'src').is_dir() or \
                not Path(config).is_file():
            parser.error('--family-config 需要 workspace/families 下的冻结源码及现有搜索配置')
    if args.out.exists() and (args.out / 'identity.json').exists() and args.execute and not args.resume:
        parser.error('已有流水线状态，继续运行必须显式 --resume')
    if not args.execute:
        print(json.dumps({'mode': '仅计划，不启动任务', 'workers': min(args.workers, resources()['cpus']),
            'full_slots': args.full_slots, 'watch': args.watch, 'configs': [str(p) for p in args.config],
            'family_configs': args.family_config,
            'out': str(args.out), 'memory_fraction': args.memory_fraction,
            'budget': {'wall_seconds': args.budget, 'case_calls': args.max_case_calls,
                       'full_calls': args.max_full_calls, 'ai_calls': args.max_ai_calls,
                       'profile_calls': args.max_profile_calls,
                       'max_proposals': args.max_proposals},
            'ai_enabled': args.ai_enabled, 'auto_audit': not args.no_auto_audit,
            'analysis_model': args.analysis_model, 'analysis_effort': args.analysis_effort,
            'max_retries': args.max_retries, 'max_pending_full': args.max_pending_full}, ensure_ascii=False))
        return
    lock_path = Path(os.environ.get('CODESIGN_PIPELINE_LOCK_PATH',
        str(ROOT / 'workspace/search/.pipeline.lock'))).resolve()
    if not lock_path.is_relative_to(ROOT / 'workspace') and 'CODESIGN_PIPELINE_LOCK_PATH' not in os.environ:
        parser.error('流水线锁路径无效')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        Pipeline(args).run()
    if args.implementation_enabled:
        from .implementation import handoff_waiting, pending_handoff
        handoff_waiting(args.out)
        if pending_handoff(args.out):
            directory = ROOT / 'workspace/implementation-loop' / args.out.name
            python, environment = runtime()
            with (directory / 'handoff.log').open('a') as log:
                subprocess.Popen([python,
                    '-m', 'codesign_lab.search.implementation', '--campaign', args.out.name,
                    '--execute', '--handoff-only', '--watch'], cwd=ROOT,
                    env=environment, stdout=log, stderr=log, start_new_session=True)


if __name__ == '__main__':
    main()
