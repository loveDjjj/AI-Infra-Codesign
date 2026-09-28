"""主控轮询独立分析并发，工作线程只等待外部 CLI；队列变更只由主控提交。"""
import copy
from concurrent.futures import ThreadPoolExecutor
import time
from pathlib import Path

from ..ai_bridge import analyze
from ..config import load


class Analyst:
    def __init__(self, pool, triggers, budget, directory, *, enabled=False, max_inflight=2,
                 timeout=600, callback=None):
        self.pool=pool;self.triggers=triggers;self.budget=budget;self.directory=directory
        self.enabled=enabled;self.max_inflight=max_inflight;self.timeout=timeout
        self.callback=callback or analyze
        self.executor=ThreadPoolExecutor(max_workers=max_inflight) if callback else None
        self.active={}
        if callback is None:
            from .analysis_jobs import AnalysisCall
            for lane,entry in self.triggers.state['lanes'].items():
                if entry.get('running_job'):
                    # 关闭时不补启动未知外部调用；已经结束的本地结果仍可消费。
                    metadata=entry['running_job']
                    spec=Path(metadata['spec'])
                    if enabled or entry.get('manual_request') or Path(load(spec)['result']).exists() or spec.with_suffix('.lease.json').exists():
                        try:
                            self.active[lane]=AnalysisCall(metadata)
                        except Exception as exc:
                            entry['recovery_blocked']=True
                            entry['last_error']='分析恢复身份待核对：'+str(exc)
                            self.pool.save()

    def poll(self):
        for lane,future in list(self.active.items()):
            try:
                if not future.done():continue
            except Exception as exc:
                entry=self.triggers.lane(lane)
                entry['recovery_blocked']=True
                entry['last_error']='分析恢复身份待核对：'+str(exc)
                del self.active[lane]
                self.pool.save()
                continue
            entry=self.triggers.lane(lane)
            accepted=False
            try:
                answer=future.result()
                decision=answer['decision']
                attempt=entry.get('attempts',{}).get(decision['decision_id'],1)
                result=self.pool.apply({'request_id':'auto-ai-'+decision['decision_id']+'-'+str(attempt),
                    'command':{'op':'decision','decision':decision,'session_id':answer['session_id']}})
                self.triggers.state=self.pool.state['analysis']
                entry=self.triggers.lane(lane)
                if result['status'] not in {'accepted','reused'}:raise ValueError(result.get('error','决策拒绝'))
                entry['last_error']=None
                accepted=True
            except Exception as exc:
                entry=self.triggers.lane(lane)
                entry['last_error']=type(exc).__name__+': '+str(exc)
                entry['retry_after_wall']=time.time()+120
            finally:
                entry.pop('running_decision',None)
                entry.pop('running_job',None)
                entry.pop('recovery_blocked',None)
                if accepted:entry.pop('manual_request',None)
                del self.active[lane]
                self.pool.save()
        if self.budget.expired():return
        for lane,entry in list(self.triggers.state['lanes'].items()):
            pending=entry.get('pending')
            manual=entry.get('manual_request')
            if not self.enabled and not manual:continue
            if manual and (not pending or manual['decision_id'] != pending['decision_id']):
                entry.pop('manual_request',None);self.pool.save();continue
            if entry.get('running_job') or entry.get('recovery_blocked'):continue
            if not pending or lane in self.active or len(self.active)>=self.max_inflight:continue
            if time.time()<entry.get('retry_after_wall',0):continue
            attempts=entry.setdefault('attempts',{});identifier=pending['decision_id']
            attempt=attempts.get(identifier,0)+1
            if attempt>2:continue
            if not self.budget.reserve('ai-'+identifier+'-'+str(attempt),'ai'):continue
            snapshot=load(self.directory/(identifier+'.snapshot.json'))
            snapshot['active_targets']=[t['definition'] for t in self.pool.state['targets'].values()
                if lane == 'global' or t['definition']['lane']==lane]
            snapshot['last_analysis_error']=entry.get('last_error')
            attempts[identifier]=attempt;entry['running_decision']=identifier
            timeout=manual['timeout'] if manual else self.timeout
            if self.executor is None:
                from .analysis_jobs import prepare,AnalysisCall
                metadata=prepare(self.directory,copy.deepcopy(snapshot),lane=lane,attempt=attempt,
                    session_id=entry.get('session_id'),timeout=timeout)
                entry['running_job']=metadata
                self.pool.save()
                self.active[lane]=AnalysisCall(metadata,launch=True)
                continue
            self.pool.save()
            self.active[lane]=self.executor.submit(self.callback,copy.deepcopy(snapshot),lane=lane,
                directory=self.directory,session_id=entry.get('session_id'),timeout=timeout)

    def close(self):
        if self.executor is not None:self.executor.shutdown(wait=False,cancel_futures=True)
