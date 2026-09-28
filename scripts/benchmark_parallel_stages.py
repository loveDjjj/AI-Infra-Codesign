#!/usr/bin/env python3
"""受控冷评估吞吐对照；真实子进程动态补位，不使用探索缓存。"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from codesign_lab.config import load,digest,verify_official
from codesign_lab.search.pipeline import source_identity
from codesign_lab.evaluation.pipeline import runtime
from codesign_lab.evaluation.monitor import sample_tree
from codesign_lab.search.scheduler import atomic_json,resources,terminate,proc_pid


def run_group(candidates,case,workers,mode,out,python,env,timeout):
    out.mkdir()
    pending=[(i,'both' if mode=='both' else 'functional') for i in range(len(candidates))]
    active={};results={};started=time.monotonic();max_active=0;max_rss=0
    memory_limit=int(resources()['available_memory_bytes']*.75)
    reservation=2*1024**3
    try:
        while pending or active:
            while pending and len(active)<workers and (len(active)+1)*reservation<=memory_limit and resources()['available_memory_bytes']>=reservation:
                i,stage=pending.pop(0);name=f'{i}-{stage}'
                stdout=(out/(name+'.stdout')).open('w');stderr=(out/(name+'.stderr')).open('w')
                command=[python,'-m','codesign_lab.evaluation.runner',str(candidates[i]),'--case',case,
                    '--mode',stage,'--seed','7','--seed','123','--out',str(out/(name+'.json'))]
                if stage=='estimate':command+=['--functional-report',str(out/(f'{i}-functional.json'))]
                child=subprocess.Popen(command,cwd=ROOT,env=env,stdout=stdout,stderr=stderr,start_new_session=True)
                active[name]={'child':child,'start':time.monotonic(),'i':i,'stage':stage,'stdout':stdout,'stderr':stderr,'peak_rss_bytes':0}
            max_active=max(max_active,len(active));rss=0
            for name,item in list(active.items()):
                try:
                    sampled=sample_tree(proc_pid(item['child'].pid))['tree_rss_bytes']
                    rss+=sampled;item['peak_rss_bytes']=max(item['peak_rss_bytes'],sampled)
                except (OSError,KeyError):pass
                elapsed=time.monotonic()-item['start']
                if elapsed>timeout and item['child'].poll() is None:terminate(item['child'])
                status=item['child'].poll()
                if status is None:continue
                item['stdout'].close();item['stderr'].close()
                results[name]={'exit_code':status,'host_seconds':elapsed,'peak_rss_bytes':item['peak_rss_bytes']}
                del active[name]
                if status==0 and item['stage']=='functional':
                    # 优先释放可运行后继，无需等待其他功能检查形成批次屏障。
                    pending.insert(0,(item['i'],'estimate'))
            max_rss=max(max_rss,rss)
            atomic_json(out/'progress.json',{'active':list(active),'queued':len(pending),'results':results})
            if not active and pending and (memory_limit<reservation or time.monotonic()-started>timeout):raise RuntimeError('内存准入无法启动任务，保留状态后退出')
            time.sleep(.2)
    finally:
        for item in active.values():
            terminate(item['child']);item['stdout'].close();item['stderr'].close()
    seconds=time.monotonic()-started
    expected=len(candidates)*(1 if mode=='both' else 2)
    summary={'wall_seconds':seconds,'max_active':max_active,'max_sampled_rss_bytes':max_rss,
             'completed_tasks':len(results),'all_completed':len(results)==expected and all(r['exit_code']==0 for r in results.values()),
             'results':results,'candidates_per_hour':len(candidates)*3600/seconds}
    atomic_json(out/'summary.json',summary)
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate',type=Path,action='append',required=True)
    parser.add_argument('--case',choices=['M1_P1','M2_D1'],default='M2_D1')
    parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--timeout',type=int,default=1800)
    args=parser.parse_args();out=args.out.resolve()
    if not out.is_relative_to(ROOT/'workspace') or out.exists():parser.error('输出必须为 workspace 下新目录')
    if args.workers<=0 or args.timeout<=0:parser.error('并发与时限必须为正整数')
    workers=min(args.workers,resources()['cpus']);candidates=[p.resolve() for p in args.candidate]
    python,env=runtime();out.mkdir(parents=True)
    pinned={'source':source_identity(),'official':verify_official(),
            'inputs':{str(candidate):{name:digest(candidate/name) for name in
                        ['hardware.json','programs/M1_P1.asm','programs/M2_D1.asm']} for candidate in candidates}}
    atomic_json(out/'identity.json',pinned)
    def check_identity():
        if source_identity()!=pinned['source'] or verify_official()!=pinned['official']:
            raise RuntimeError('评估代码或冻结工具改变，拒绝对照结论')
        for candidate,hashes in pinned['inputs'].items():
            if any(digest(Path(candidate)/name)!=expected for name,expected in hashes.items()):
                raise RuntimeError('评估输入改变，拒绝对照结论')
    summaries={}
    for mode in ['both','split']:
        print(json.dumps({'mode':mode,'workers':workers,'status':'running'}),flush=True)
        summaries[mode]=run_group(candidates,args.case,workers,mode,out/mode,python,env,args.timeout)
        check_identity()
        if not summaries[mode]['all_completed']:raise RuntimeError('任务未全部成功，保留失败原件')
    identical=True
    for i in range(len(candidates)):
        a=load(out/'both'/f'{i}-both.json')['cases'][args.case]
        b=load(out/'split'/f'{i}-estimate.json')['cases'][args.case]
        identical &= a['functional']==b['functional'] and a['timing']==b['timing']
    summary={'cache_enabled':False,'workers':workers,'candidate_count':len(candidates),
        'complete_results_identical':identical,'groups':summaries,
        'wall_ratio_both_over_split':summaries['both']['wall_seconds']/summaries['split']['wall_seconds'],
        'scope':'同批输入、同并发、冷评估吞吐；一次顺序对照，受主机共享负载影响'}
    atomic_json(out/'summary.json',summary)
    if not identical:raise RuntimeError('完整结果不一致')
    print(json.dumps({k:v for k,v in summary.items() if k!='groups'},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
