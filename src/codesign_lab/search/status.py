"""轻量主机调度摘要：明确准入等待原因，不推断芯片等待。"""
import time
from ..evaluation.monitor import sample

OFFICIAL = {'verify', 'full', 'audit'}


def queue_details(pending, active, *, workers, full_slots, external, memory_budget, available,
                  expired=False, now=None):
    now=time.time() if now is None else now
    used=sum(job['memory_bytes'] for job in active)
    official=sum(job['stage'] in OFFICIAL for job in active)
    rows=[]
    for job in pending:
        if expired and job['stage']!='audit':reason='累计截止时间已到'
        elif job.get('retry_after',0)>now:reason='重试退避'
        elif len(active)+external>=workers:reason='总进程槽位已满'
        elif job['stage'] in OFFICIAL and official>=full_slots:reason='验收预留槽位已满'
        elif job['stage'] not in OFFICIAL and len(active)-official+external>=workers-full_slots:reason='探索槽位已满，保留验收容量'
        elif used+job['memory_bytes']>memory_budget or job['memory_bytes']>available:reason='内存准入限制'
        else:reason='可准入，等待本轮优先级调度'
        rows.append({'key':job['key'],'stage':job['stage'], 'reason':reason,
            'queue_seconds':max(0,now-job['queued_wall']) if job.get('queued_wall') is not None else None,
            'memory_bytes':job['memory_bytes'],'attempt':job.get('attempt',1)})
    return rows


def process_identity(pid, start_ticks=None):
    """报告生成时再次核对进程；旧文件不作为活进程证明。"""
    try:identity=sample(pid)
    except FileNotFoundError:return '已退出'
    except (OSError,ValueError):return '无法核对'
    if start_ticks is None:return '缺少启动身份'
    if identity['start_ticks']!=start_ticks:return 'PID 已复用'
    return '已退出' if identity['state']=='Z' else '身份匹配，生成时存活'


def attempt_costs(runtime):
    attempts=[item for job in runtime.get('jobs',{}).values() for item in job.get('attempts',[])]
    return {'attempts':len(attempts),
            'attempt_host_seconds':sum(item['result'].get('wall_seconds',0) for item in attempts),
            'retry_host_seconds':sum(item['result'].get('wall_seconds',0) for item in attempts if item['attempt']>1),
            'estimated_attempts':sum(bool(item['result'].get('wall_seconds_estimated')) for item in attempts)}


def target_summary(identifier,entry):
    """采样运行状态只展示事实计数，不把缓存反馈当独立观测。"""
    definition=entry['definition'];adaptive=entry.get('adaptive',{})
    trials=list(adaptive.get('trials',{}).values())
    pending={trial['number'] for trial in trials if not trial.get('told')}
    pending.update(item['proposal']['number'] for item in adaptive.get('unconsumed_proposals',[]) if not item.get('told'))
    origins={}
    for trial in trials:
        name=trial.get('origin','未记录');origins[name]=origins.get(name,0)+1
    independent=sum(trial.get('outcome') in {'eligible','power_failed'} for trial in trials)
    if definition.get('sampler')!='tpe':independent=len(entry.get('sampler_state',{}).get('observations',{}))
    return {'id':identifier,'lane':definition['lane'],'status':entry['status'],
        'hypothesis':definition['hypothesis'],'sampler':definition.get('sampler','enumerate'),
        'launched':entry.get('trials_launched',0),'completed':entry.get('trials_completed',0),
        'budget':definition.get('max_trials'),'origins':origins,'feedback_pending':len(pending),
        'sampling_jobs_pending':len(adaptive.get('pending_jobs',[])),
        'independent_observations':independent,
        'historical_priors':sum(prior['status']=='DONE' for prior in adaptive.get('priors',{}).values()),
        'priors_rejected':sum(prior['status']=='REJECTED' for prior in adaptive.get('priors',{}).values()),
        'priors_pending':sum(prior['status'] not in {'DONE','REJECTED'} for prior in adaptive.get('priors',{}).values()),
        'error':entry.get('error')}
