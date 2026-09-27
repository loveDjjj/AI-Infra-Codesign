import argparse,json,subprocess,sys,time,os
from pathlib import Path
from .config import ROOT,load,reference,workspace_output

def main():
    parser=argparse.ArgumentParser(prog='lab');sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('build');p.add_argument('config',type=Path);p.add_argument('--out',type=Path,required=True);p.add_argument('--verify',type=Path)
    p=sub.add_parser('run');p.add_argument('candidate',type=Path);p.add_argument('--level',choices=['functional','estimate','both','full'],default='functional');p.add_argument('--case',choices=['M1_P1','M2_D1'],action='append');p.add_argument('--seed',type=int,action='append');p.add_argument('--out',type=Path,required=True);p.add_argument('--no-cache',action='store_true')
    sub.add_parser('report')
    p=sub.add_parser('search');p.add_argument('config',type=Path)
    p=sub.add_parser('promote');p.add_argument('id')
    p=sub.add_parser('audit');p.add_argument('id')
    p=sub.add_parser('verify');p.add_argument('archive',type=Path)
    p=sub.add_parser('package');p.add_argument('id');p.add_argument('--out',type=Path,required=True)
    p=sub.add_parser('clean');p.add_argument('--dry-run',action='store_true',required=True)
    p=sub.add_parser('profile');p.add_argument('candidate',type=Path);p.add_argument('--case',required=True);p.add_argument('--compare',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    if hasattr(args,'out'):
        category={'build':'builds','run':'evaluations','profile':'profiles','package':'reports'}[args.command]
        args.out=workspace_output(args.out,category)
    if args.command=='build':
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
            if not args.no_cache:command+=['--cache-dir',str(ROOT/'workspace/search/cache')]
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
        if args.command=='run':
            from .report import generate
            generate()
        return code
    elif args.command=='report':
        from .report import generate
        result=generate()
    elif args.command=='search':
        from .search.runner import prepare
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
        from .release import clean
        result=clean()
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0
if __name__=='__main__':raise SystemExit(main())
