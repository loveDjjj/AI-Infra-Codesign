"""按可执行工作余量维护研究供给；不把 CPU 空闲本身当成实验目标。"""
import hashlib
import json
import statistics
import time


READY = {'build', 'functional', 'case', 'sample', 'verify', 'full', 'audit',
         'profile_build', 'profile'}
DEFAULT_SECONDS = {'build': 10, 'functional': 40, 'case': 480, 'sample': 480,
                   'verify': 30, 'full': 700, 'audit': 30,
                   'profile_build': 10, 'profile': 700}


def review(state, *, pending, active, completed, budget, workers, source_epoch, now=None,
           lead_seconds=1200, desired_families=2):
    """返回至多一个幂等规划请求；空答复有限退避后明确记录断供。"""
    now = time.time() if now is None else now
    supply = state.setdefault('supply', {})
    durations = {}
    for stage in READY:
        samples = [item.get('wall_seconds') for item in completed
                   if item.get('stage') == stage and isinstance(item.get('wall_seconds'), (int, float))
                   and item['wall_seconds'] > 0]
        durations[stage] = statistics.median(samples[-12:]) if samples else DEFAULT_SECONDS[stage]
    jobs = list(pending) + [item['job'] for item in active.values()]
    ready = [job for job in jobs if job.get('stage') in READY]
    # 只计可执行任务的估计工作量；结构编码和未满足依赖不是待测候选。
    coverage = sum(durations[job['stage']] for job in ready) / max(1, min(workers, len(ready)))
    family_jobs = [job for job in jobs if job.get('stage', '').startswith('implementation_')]
    decisions = state.get('applied_decisions', {})
    mechanisms = sorted({proposal.get('transformation_id') or proposal.get('proposal')
        for row in decisions.values() for proposal in row.get('decision', {}).get('implementation_proposals', [])})
    targets = state.get('targets', {})
    active_mechanisms = {job.get('mechanism_id', job.get('proposal_id', job['key']))
                         for job in family_jobs}
    ever_launched = {entry.get('job', {}).get('mechanism_id')
                     for entry in state.get('jobs', {}).values()
                     if entry.get('job', {}).get('stage', '').startswith('implementation_')}
    reserve_hypotheses = sum(item.get('transformation_id') not in ever_launched
                             for item in state.get('hypotheses', []))
    expandable = sum(max(0, entry['definition'].get('max_trials', 0) -
                          entry.get('trials_launched', 0))
                     for entry in targets.values() if entry.get('status') in {'QUEUED', 'ACTIVE'}
                     and entry.get('family_phase') == 'expand')
    signature = hashlib.sha256(json.dumps({
        'source': source_epoch,
        'targets': [(key, entry.get('status'), entry.get('trials_completed')) for key, entry in sorted(targets.items())],
        'mechanisms': mechanisms,
        'observations': state.get('analysis', {}).get('lanes', {}).get('global', {}).get('seen', [])[-8:],
    }, sort_keys=True).encode()).hexdigest()
    if signature != supply.get('revision'):
        supply.update(revision=signature, attempts=0,
                      next_review_wall=max(supply.get('next_review_wall', 0),
                                           supply.get('last_requested_wall', 0) + 300),
                      blocked_reason=None,
                      last_request_id=None)
    request_id = supply.get('last_request_id')
    if request_id and request_id in decisions:
        decision = decisions[request_id]['decision']
        progress = bool(decision.get('new_targets') or decision.get('implementation_proposals')
                        or decision.get('profile_requests'))
        supply['last_request_id'] = None
        supply['next_review_wall'] = now + (300 if progress else 600 * min(3, supply['attempts']))
        if not progress:
            supply['blocked_reason'] = ('相同证据下连续两次无有效补给；等待新事实或新源码批次'
                if supply['attempts'] >= 2 else 'AI 未提出有证据的新目标；等待新事实或有限退避')
    remaining = budget.snapshot()
    supply.update(coverage_seconds=round(coverage, 1), ready_tasks=len(ready),
                  active_family_tasks=len(family_jobs), active_mechanisms=len(active_mechanisms),
                  expandable_candidates=expandable, reserved_hypotheses=reserve_hypotheses,
                  mechanisms=len(mechanisms),
                  lead_seconds=lead_seconds)
    if remaining['remaining_wall_seconds'] <= 0 or remaining['used']['ai'] >= remaining['limits']['ai']:
        supply['blocked_reason'] = 'AI 或运行时间预算已用尽'
        return None
    if remaining['used']['case'] >= remaining['limits']['case']:
        supply['blocked_reason'] = '单案评估预算已用尽'
        return None
    if coverage >= lead_seconds or (len(active_mechanisms) >= desired_families and
                                    (expandable >= max(1, workers // 2) or reserve_hypotheses)):
        supply['blocked_reason'] = None
        return None
    if supply.get('last_request_id') or now < supply.get('next_review_wall', 0):
        return None
    if supply['attempts'] >= 2:
        supply['blocked_reason'] = '相同证据下连续两次无有效补给；等待新事实或新源码批次'
        return None
    lane = state.get('analysis', {}).get('lanes', {}).get('global', {})
    if lane.get('pending') or lane.get('running_decision') or lane.get('running_job'):
        return None
    supply['attempts'] += 1
    supply['last_requested_wall'] = now
    identity = hashlib.sha256(json.dumps([signature, supply['attempts']], sort_keys=True).encode()).hexdigest()
    supply['last_request_id'] = identity
    supply['blocked_reason'] = '候选供给余量不足，等待研究规划'
    return {'revision': signature, 'decision_id': identity,
            'coverage_seconds': round(coverage, 1), 'ready_tasks': len(ready),
            'active_family_tasks': len(family_jobs)}
