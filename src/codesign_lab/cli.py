import argparse,json,subprocess,sys,time,os
from pathlib import Path
from .config import ROOT,load,reference,workspace_output

def main():
    parser=argparse.ArgumentParser(prog='lab');sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('build');p.add_argument('config',type=Path);p.add_argument('--out',type=Path,required=True);p.add_argument('--verify',type=Path)
    p=sub.add_parser('run');p.add_argument('candidate',type=Path);p.add_argument('--level',choices=['functional','estimate','both','full'],default='functional');p.add_argument('--case',choices=['M1_P1','M2_D1'],action='append');p.add_argument('--seed',type=int,action='append');p.add_argument('--out',type=Path,required=True);p.add_argument('--no-cache',action='store_true')
    sub.add_parser('report')
    p=sub.add_parser('dashboard');p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=8765)
    p=sub.add_parser('pipeline');p.add_argument('config',type=Path);p.add_argument('--execute',action='store_true');p.add_argument('--resume',action='store_true')
    p=sub.add_parser('implementation-loop');p.add_argument('--campaign',required=True);p.add_argument('--execute',action='store_true');p.add_argument('--watch',action='store_true');p.add_argument('--proposal-id');p.add_argument('--retry-failed',action='store_true');p.add_argument('--max-proposals',type=int,default=2);p.add_argument('--min-case-gain',type=float,default=.002);p.add_argument('--min-score-gain',type=float,default=100);p.add_argument('--model',default='gpt-6-astra');p.add_argument('--reasoning-effort',default='medium')
    p=sub.add_parser('family-restore');p.add_argument('record_id');p.add_argument('--out',type=Path,required=True)
    p=sub.add_parser('pipeline-status');p.add_argument('--campaign',required=True)
    p=sub.add_parser('pipeline-inject');p.add_argument('target',type=Path);p.add_argument('--campaign',required=True);p.add_argument('--request-id')
    p=sub.add_parser('pipeline-analyze');p.add_argument('--campaign',required=True);p.add_argument('--lane',required=True);p.add_argument('--execute',action='store_true');p.add_argument('--timeout',type=int,default=600)
    p=sub.add_parser('pipeline-decision');p.add_argument('decision',type=Path);p.add_argument('--campaign',required=True);p.add_argument('--request-id')
    p=sub.add_parser('pipeline-stop-target');p.add_argument('target_id');p.add_argument('--campaign',required=True);p.add_argument('--request-id')
    p=sub.add_parser('pipeline-priority');p.add_argument('target_id');p.add_argument('priority',type=float);p.add_argument('--campaign',required=True);p.add_argument('--request-id')
    p=sub.add_parser('pipeline-target-budget');p.add_argument('target_id');p.add_argument('remaining',type=int);p.add_argument('--campaign',required=True);p.add_argument('--request-id')
    p=sub.add_parser('search');p.add_argument('config',type=Path);p.add_argument('--execute',action='store_true');p.add_argument('--workers',type=int);p.add_argument('--out',type=Path);p.add_argument('--resume',action='store_true')
    p=sub.add_parser('promote');p.add_argument('id')
    p=sub.add_parser('audit');p.add_argument('id')
    p=sub.add_parser('verify');p.add_argument('archive',type=Path)
    p=sub.add_parser('package');p.add_argument('id');p.add_argument('--out',type=Path,required=True)
    p=sub.add_parser('clean');mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run',action='store_true');mode.add_argument('--apply',action='store_true')
    p=sub.add_parser('profile');p.add_argument('candidate',type=Path);p.add_argument('--case',required=True);p.add_argument('--compare',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    if args.command=='dashboard':
        from .dashboard import serve
        serve(args.host,args.port)
        return 0
    if args.command=='pipeline':
        from .search.settings import arguments
        from .search.pipeline import main as pipeline_main
        pipeline_main(arguments(args.config,execute=args.execute,resume=args.resume))
        return
    if args.command=='implementation-loop':
        from .search.implementation import main as implementation_main
        options=['--campaign',args.campaign,'--max-proposals',str(args.max_proposals),
                 '--min-case-gain',str(args.min_case_gain),'--min-score-gain',str(args.min_score_gain),
                 '--model',args.model,'--reasoning-effort',args.reasoning_effort]
        if args.execute:options.append('--execute')
        if args.watch:options.append('--watch')
        if args.proposal_id:options.extend(['--proposal-id',args.proposal_id])
        if args.retry_failed:options.append('--retry-failed')
        return implementation_main(options)
    if args.command=='family-restore':
        from .search.implementation import restore_family
        destination=args.out.resolve()
        if not destination.is_relative_to((ROOT/'workspace/families').resolve()):
            raise ValueError('实现族只能恢复到 workspace/families')
        print(json.dumps(restore_family(args.record_id,destination),ensure_ascii=False,indent=2))
        return
    if hasattr(args,'out') and args.out is not None:
        category={'build':'builds','run':'evaluations','profile':'profiles','package':'reports','search':'search'}[args.command]
        args.out=workspace_output(args.out,category)
    if args.command in ['pipeline-status','pipeline-inject','pipeline-stop-target','pipeline-decision','pipeline-analyze','pipeline-priority','pipeline-target-budget']:
        from .search.targets import campaign_path,inject
        directory=campaign_path(args.campaign)
        if args.command=='pipeline-analyze':
            from .ai_bridge import capabilities
            from .search.targets import epoch
            runtime_state=load(directory/'state.json')
            if runtime_state['source_epoch']!=epoch():raise ValueError('该批次源码身份已改变，禁止继续 AI 分析')
            lane=runtime_state.get('analysis',{}).get('lanes',{}).get(args.lane,{})
            pending=lane.get('pending')
            if not pending:raise ValueError('此 lane 没有待分析请求')
            if not args.execute:
                result={'mode':'只检查，不调用模型','capabilities':capabilities(),'request':pending}
            else:
                result=inject(directory,{'op':'analyze','lane':args.lane,
                    'decision_id':pending['decision_id'],'timeout':args.timeout})
        elif args.command=='pipeline-status':
            result={'targets':load(directory/'state.json') if (directory/'state.json').exists() else None,
                    'scheduler':load(directory/'status.json') if (directory/'status.json').exists() else None}
        else:
            if args.command=='pipeline-decision':
                command={'op':'decision','decision':load(args.decision)}
            elif args.command=='pipeline-priority':
                command={'op':'reprioritize','target_id':args.target_id,'priority':args.priority}
            elif args.command=='pipeline-target-budget':
                command={'op':'set_remaining_budget','target_id':args.target_id,'remaining':args.remaining}
            else:
                command={'op':'add','target':load(args.target)} if args.command=='pipeline-inject' else {'op':'stop','target_id':args.target_id}
            result=inject(directory,command,args.request_id)
    elif args.command=='build':
        from .build import build
        result=build(args.config,args.out,args.verify)
    elif args.command in ['run','profile']:
        args.out=args.out.resolve();args.out.parent.mkdir(parents=True,exist_ok=True)
        if args.command=='profile':command=[sys.executable,'-m','codesign_lab.evaluation.trace',str(args.candidate.resolve()),'--case',args.case,'--compare',str(args.compare.resolve()),'--out',str(args.out)]
        elif args.level=='full':command=[sys.executable,'-m','codesign_lab.evaluation.official',str(args.candidate.resolve()),'--report',str(args.out)]
        else:
            command=[sys.executable,'-m','codesign_lab.evaluation.runner',str(args.candidate.resolve()),'--mode',args.level,'--out',str(args.out)]
            for case in args.case or []:command+=['--case',case]
            for seed in args.seed or []:command+=['--seed',str(seed)]
            if not args.no_cache:
                from .evaluation.pipeline import cache_root
                command+=['--cache-dir',str(cache_root())]
        start=time.monotonic();child=subprocess.Popen(command)
        proc_children=Path('/proc/self/task/'+os.readlink('/proc/thread-self').split('/')[-1]+'/children').read_text().split()
        proc_pid=next((pid for pid in proc_children if Path('/proc/'+pid+'/status').read_text().split('NSpid:')[1].splitlines()[0].split()[-1]==str(child.pid)),None)
        monitor=subprocess.Popen([sys.executable,'-m','codesign_lab.evaluation.monitor','--pid',str(proc_pid),'--out',str(args.out.with_suffix('.host.jsonl')),'--interval','1'])
        code=child.wait()
        try:monitor.wait(timeout=5)
        except subprocess.TimeoutExpired:monitor.terminate();monitor.wait()
        if args.command=='run' and args.out.exists():
            from .records import append
            report=load(args.out);config=load(args.candidate/'config.json') if (args.candidate/'config.json').exists() else None
            from .evaluation.profile import summarize
            cases={case:{key:value for key,value in info.items() if key!='timing'}|{'timing':{key:value for key,value in info.get('timing',{}).items() if key!='resource_stats'}} for case,info in report.get('cases',{}).items()}
            profiles={case:{key:value for key,value in summarize(info['timing']).items() if key!='timeline'} for case,info in report.get('cases',{}).items() if 'resource_stats' in info.get('timing',{})}
            append({'id':args.out.stem+'-'+str(time.time_ns()),'scope':args.level,'cases':cases,'profile':profiles,'config':config,'eligible':report.get('eligible'),'score':report.get('experimental_score'),'audited':False,'reproduction':'record_only','report':reference(args.out),'candidate':reference(args.candidate),'provenance':report.get('provenance'),'host_seconds':time.monotonic()-start})
        return code
    elif args.command=='report':
        from .report import generate
        result=generate()
    elif args.command=='search':
        from .search.runner import prepare,execute
        if args.execute:
            if args.out is None:parser.error('search --execute 必须指定 --out')
            result=execute(args.config,args.out,workers=args.workers,resume=args.resume)
        else:
            if args.resume:parser.error('--resume 需要 --execute')
            result=prepare(args.config)
    elif args.command=='audit':
        from .release import audit
        result=audit(args.id)
    elif args.command=='verify':
        from .release import verify
        result=verify(args.archive)
    elif args.command=='package':
        from .release import package
        result=package(args.id,args.out)
    elif args.command=='promote':
        from .release import promote
        result=promote(args.id)
    else:
        from .maintenance import plan,apply
        result=apply() if args.apply else plan()
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0
if __name__=='__main__':raise SystemExit(main())
