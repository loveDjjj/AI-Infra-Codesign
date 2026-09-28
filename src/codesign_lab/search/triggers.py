"""持久反馈游标：合并分析触发，缓存与基础设施错误不冒充独立观测。"""
import hashlib
import json
import math
import time


class Triggers:
    def __init__(self, state, *, batch_size=8, cooldown=120, low_watermark=8,
                 stagnation_trials=12, improvement_threshold=.002, failure_window=6, failure_threshold=5,
                 global_only=False):
        if min(batch_size, stagnation_trials, failure_window, failure_threshold) < 1 or failure_threshold > failure_window or cooldown < 0 or low_watermark < 0 or not 0 < improvement_threshold < 1:
            raise ValueError('分析触发阈值无效')
        self.identity = {'source_epoch': state.get('source_epoch'), 'campaign': state.get('campaign')}
        self.state = state.setdefault('analysis', {'lanes': {}})
        self.batch_size = batch_size
        self.cooldown = cooldown
        self.low_watermark = low_watermark
        self.stagnation_trials = stagnation_trials
        self.improvement_threshold = improvement_threshold
        self.failure_window = failure_window
        self.failure_threshold = failure_threshold
        self.global_only = global_only

    def trends(self, observations, new):
        """同硬件同案例的合格独立样本才形成改善与停滞证据。"""
        fresh = {row['id'] for row in new}
        groups = {}
        bests = {}
        reasons = set()
        for row in observations:
            cycles, power = row.get('cycles'), row.get('peak_power_w')
            if not row.get('hardware_hash') or row.get('case') not in {'M1_P1', 'M2_D1'} or row.get('functional_passed') is not True:
                continue
            if type(cycles) not in (int, float) or not math.isfinite(cycles) or cycles <= 0 or type(power) not in (int, float) or not math.isfinite(power) or not 0 <= power <= 20:
                continue
            identity = (row['hardware_hash'], row['case'])
            group = groups.setdefault(identity, [])
            best_before = bests.get(identity, cycles)
            if row['id'] in fresh and (best_before - cycles) / best_before >= self.improvement_threshold:
                reasons.add('improvement')
            group.append((row, best_before))
            bests[identity] = min(best_before, cycles)
        for group in groups.values():
            window = group[-self.stagnation_trials:]
            if len(window) < self.stagnation_trials or not any(row['id'] in fresh for row, _ in window):
                continue
            baseline = window[0][1]
            best = min(row['cycles'] for row, _ in window)
            if (baseline - best) / baseline < self.improvement_threshold:
                reasons.add('stagnation')
        return sorted(reasons)

    def lane(self, name):
        return self.state['lanes'].setdefault(name, {'observations': [], 'seen': [],
            'acknowledged': [], 'completed_targets': [], 'pending': None, 'last_analysis_wall': 0})

    def completion_revision(self, target):
        """完成身份由已发生的候选和结果身份决定，不因重启或额度数字变化。"""
        content = {'status': target['status'],
            'candidates': {identifier: {'status': candidate['status'],
                'case_keys': sorted(candidate.get('case_keys', [])), 'reason': candidate.get('reason')}
                for identifier, candidate in target.get('candidates', {}).items()}}
        return hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest()

    def profile_ready(self,lane,identifier,summary):
        if self.global_only:lane='global'
        self.lane(lane).setdefault('profiles',{})[identifier]=summary

    def observe(self, lane, observation):
        if self.global_only:lane='global'
        entry = self.lane(lane)
        if observation['id'] in entry['seen']:
            return
        entry['seen'].append(observation['id'])
        if observation.get('cache_reused') or observation.get('failure_kind') == 'infrastructure':
            return
        entry['observations'].append(observation)

    def pool_revision(self, targets):
        """全池终态的稳定身份；相同结果只请求一次全局分析。"""
        if any(target['status'] not in {'DONE','STOPPED','FAILED'} for target in targets.values()):
            return None
        content={identifier:self.completion_revision(target) for identifier,target in sorted(targets.items())}
        return hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest()

    def poll(self, targets, queued_tasks, now=None, *, review_exhaustion=False):
        now = time.time() if now is None else now
        ready = []
        lanes = {'global'} if self.global_only else {entry['definition']['lane'] for entry in targets.values()} | set(self.state['lanes'])
        pool_rev = self.pool_revision(targets) if review_exhaustion and queued_tasks == 0 else None
        if pool_rev is not None:
            lanes.add('global')
        for name in sorted(lanes):
            entry = self.lane(name)
            if entry.get('running_decision'):
                continue
            new = [o for o in entry['observations'] if o['id'] not in entry['acknowledged']]
            profiles=sorted(set(entry.get('profiles',{}))-set(entry.get('acknowledged_profiles',[])))
            completed_revisions = entry.setdefault('completed_revisions', {})
            revisions = {}
            for identifier,target in targets.items():
                if (not self.global_only and target['definition']['lane'] != name) or target['status'] not in {'DONE','STOPPED','FAILED'}:
                    continue
                target_rev = self.completion_revision(target)
                if identifier in entry['completed_targets'] and identifier not in completed_revisions:
                    # 老记录只有已确认 ID，无法反推旧候选快照；首次迁移不重复分析。
                    completed_revisions[identifier] = target_rev
                if completed_revisions.get(identifier) != target_rev:
                    revisions[identifier] = target_rev
            completed = sorted(revisions)
            reasons = self.trends(entry['observations'], new)
            # 每份新评估证据立即交给同一 lane 的分析会话；并发完成的结果合并成一次快照。
            result_ready = any(item.get('observation_kind') in {'case_result', 'official_result'} for item in new)
            if result_ready:
                reasons.append('result_ready')
            if any(item.get('observation_kind') == 'implementation' for item in new):
                reasons.append('implementation_result')
                result_ready = True
            if any(entry['profiles'][identifier].get('status') in {'FAILED','REJECTED'} for identifier in profiles):reasons.append('profile_failed')
            if any(entry['profiles'][identifier].get('status') not in {'FAILED','REJECTED'} for identifier in profiles):reasons.append('profile_ready')
            if completed:
                reasons.append('target_completion')
            if len(new) >= self.batch_size:
                reasons.append('batch')
            if new and queued_tasks < self.low_watermark:
                reasons.append('low_watermark')
            failures = [o for o in new[-self.failure_window:] if o.get('functional_passed') is False]
            if len(failures) >= self.failure_threshold:
                reasons.append('functional_failures')
            if name == 'global' and pool_rev is not None and entry.get('reviewed_pool_revision') != pool_rev:
                reasons.append('pool_exhausted')
            if not reasons or (not result_ready and entry['last_analysis_wall'] and now - entry['last_analysis_wall'] < self.cooldown):
                continue
            if entry['pending']:
                pending = entry['pending']
                pending['reasons'] = sorted(set(pending['reasons'] + reasons))
                pending['observation_ids'] = sorted(set(pending['observation_ids'] + [o['id'] for o in new]))
                pending['target_ids'] = sorted(set(pending['target_ids'] + completed))
                pending.setdefault('target_revisions', {}).update(revisions)
                pending['profile_ids']=sorted(set(pending.get('profile_ids',[])+profiles))
                if name == 'global' and pool_rev is not None:
                    pending['pool_revision']=pool_rev
            else:
                content = {**self.identity, 'lane': name, 'observations': [o['id'] for o in new], 'targets': revisions,'profiles':profiles}
                if name == 'global' and pool_rev is not None:
                    content['pool_revision']=pool_rev
                identifier = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
                entry['pending'] = {'decision_id': identifier, 'lane': name, 'reasons': reasons,
                    'observation_ids': [o['id'] for o in new], 'target_ids': completed,
                    'target_revisions': revisions, 'created_wall': now}
                entry['pending']['profile_ids']=profiles
                if name == 'global' and pool_rev is not None:
                    entry['pending']['pool_revision']=pool_rev
            ready.append(entry['pending'])
        return ready

    def acknowledge(self, lane, decision_id, now=None):
        entry = self.lane(lane)
        pending = entry['pending']
        if not pending or pending['decision_id'] != decision_id:
            raise ValueError('分析确认身份与待处理决策不匹配')
        entry['acknowledged'] = sorted(set(entry['acknowledged'] + pending['observation_ids']))
        entry['completed_targets'] = sorted(set(entry['completed_targets'] + pending['target_ids']))
        entry.setdefault('completed_revisions', {}).update(pending.get('target_revisions', {}))
        if lane == 'global' and pending.get('pool_revision'):
            entry['reviewed_pool_revision']=pending['pool_revision']
        entry['acknowledged_profiles']=sorted(set(entry.get('acknowledged_profiles',[])+pending.get('profile_ids',[])))
        entry['last_analysis_wall'] = time.time() if now is None else now
        entry['pending'] = None
