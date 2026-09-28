#!/usr/bin/env python3
"""比较原 both 与独立阶段的冷评估结果及主机成本，不使用探索缓存。"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from codesign_lab.evaluation.pipeline import runtime
from codesign_lab.search.scheduler import atomic_json
from codesign_lab.config import load


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate',type=Path,default=ROOT/'data/releases/joint28')
    parser.add_argument('--case',choices=['M1_P1','M2_D1'],default='M2_D1')
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--timeout',type=int,default=1800)
    args=parser.parse_args()
    out=args.out.resolve()
    if not out.is_relative_to(ROOT/'workspace') or out.exists():
        parser.error('输出必须是 workspace 下尚不存在的目录')
    if args.timeout<=0:parser.error('timeout 必须为正整数')
    python,env=runtime();out.mkdir(parents=True)
    costs={}
    for mode,name,extra in [('both','both',[]),('functional','functional',[]),
            ('estimate','estimate',['--functional-report',str(out/'functional.json')])]:
        command=[python,'-m','codesign_lab.evaluation.runner',str(args.candidate.resolve()),
            '--mode',mode,'--case',args.case,'--seed','7','--seed','123','--out',str(out/(name+'.json'))]+extra
        print(json.dumps({'stage':name,'status':'running'},ensure_ascii=False),flush=True)
        started=time.monotonic()
        with (out/(name+'.stdout')).open('w') as stdout,(out/(name+'.stderr')).open('w') as stderr:
            result=subprocess.run(command,cwd=ROOT,env=env,stdout=stdout,stderr=stderr,timeout=args.timeout)
        costs[name]={'host_seconds':time.monotonic()-started,'exit_code':result.returncode}
        atomic_json(out/'progress.json',{'costs':costs,'cache_enabled':False})
        if result.returncode:raise RuntimeError('阶段失败，检查 '+str(out/(name+'.stderr')))
    both=load(out/'both.json');split=load(out/'estimate.json')
    original=both['cases'][args.case];separate=split['cases'][args.case]
    identical=original['functional']==separate['functional'] and original['timing']==separate['timing']
    summary={'case':args.case,'cache_enabled':False,'complete_results_identical':identical,
        'cycles':separate['timing']['cycles'],'costs':costs,
        'split_host_seconds':costs['functional']['host_seconds']+costs['estimate']['host_seconds'],
        'scope':'单案串行冷成本；不代表并行搜索吞吐或采样算法效果'}
    atomic_json(out/'summary.json',summary)
    if not identical:raise RuntimeError('分阶段结果与 both 不一致')
    print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
