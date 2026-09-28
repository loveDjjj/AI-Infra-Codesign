"""同预算采样对照：先检查真实产物空间，再显式启动冷评估。"""
import argparse
import fcntl
import json
import re
import subprocess
import time
from pathlib import Path
from ..config import ROOT,load,digest,verify_official
from ..records import read
from .targets import validate_target,epoch,inject
from .samplers import candidates
from .scheduler import atomic_json,run,resources
from ..evaluation.pipeline import runtime


def plan(config,out):
    settings=load(config)
    required={'base_record','case','variables','trials','seeds','workers','max_inflight','wall_seconds','timeout_seconds'}
    if set(settings)!=required:raise ValueError('对照配置字段不完整或未知')
    if settings['case'] not in {'M1_P1','M2_D1'}:raise ValueError('对照必须固定硬件单案例')
    if type(settings['trials']) is not int or settings['trials']<=12:raise ValueError('trial预算必须超过当前TPE启动阶段12点')
    seeds=settings['seeds']
    if not isinstance(seeds,list) or len(seeds)<2 or any(type(seed) is not int or not 0<=seed<2**64 for seed in seeds) or len(set(seeds))!=len(seeds):
        raise ValueError('对照至少需要两个不同有效seed')
    for field in ['workers','max_inflight','wall_seconds','timeout_seconds']:
        if type(settings[field]) is not int or settings[field]<=0:raise ValueError('资源或时间预算无效')
    if settings['workers']<3 or settings['max_inflight']>settings['trials']:raise ValueError('并发窗口与资源预算不匹配')
    out=Path(out).resolve()
    if out.parent!=ROOT/'workspace/pipeline' or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,39}',out.name):raise ValueError('对照入口必须是workspace/pipeline下的简短批次名')
    records=read();base=next((record for record in records if record['id']==settings['base_record']),None)
    if not base:raise ValueError('缺少基准账本记录')
    target={'schema_version':1,'target_id':'benchmark','lane':'sampler_benchmark','hypothesis':'同有效空间、冷调用预算和资源的采样对照',
        'base_record':base['id'],'source_epoch':epoch(),'cases':[settings['case']], 'variables':settings['variables'],
        'sampler':'tpe','max_trials':settings['trials'],'max_inflight':settings['max_inflight'],
        'priority':.5,'evidence_ids':[base['id']]}
    validate_target(target,epoch(),records,max_trials=settings['trials'])
    size=1
    for values in settings['variables'].values():size*=len(values)
    if size<=settings['trials']:raise ValueError('空间必须大于预算，否则采用枚举，不比较TPE优势')
    manifest={'settings':settings,'source_epoch':epoch(),'official':verify_official(),
        'base_config':base['config'],'nominal_space':size,'target':target,'resources_at_plan':resources()}
    out.mkdir(parents=True,exist_ok=True)
    path=out/'plan.json'
    if path.exists():
        previous=load(path)
        if any(previous[key]!=manifest[key] for key in ['settings','source_epoch','official','base_config']):raise ValueError('对照身份已改变，需要新目录')
        return previous
    atomic_json(path,manifest);return manifest


def preflight(manifest,out):
    """构建完整小空间，报告唯一硬件/ASM数量，不调用模拟器。"""
    path=out/'preflight.json'
    if path.exists():return load(path)
    python,env=runtime();settings=manifest['settings'];jobs=[]
    for identity,config in candidates(manifest['base_config'],settings['variables'],manifest['nominal_space']):
        config_path=out/'configs'/(identity+'.json');atomic_json(config_path,config)
        build=out/'builds'/identity
        jobs.append({'key':'build-'+identity,'candidate':str(build),'memory_bytes':1024**3,
            'command':[python,'-m','codesign_lab.cli','build',str(config_path),'--out',str(build)]})
    started=time.monotonic()
    completed=run(jobs,out/'preflight-jobs',workers=settings['workers'],memory_fraction=.75,
        timeout=settings['timeout_seconds'],budget=settings['wall_seconds'],env=env)
    if epoch()!=manifest['source_epoch'] or verify_official()!=manifest['official']:raise ValueError('预检查期间源码或官方身份改变')
    results={row['key']:row for row in completed['jobs']};unique={};failed=[]
    for job in jobs:
        result=results.get(job['key'],{})
        if result.get('status')!='completed':failed.append({'key':job['key'],'result':result});continue
        candidate=Path(job['candidate']);hardware=digest(candidate/'hardware.json');program=digest(candidate/'programs'/(settings['case']+'.asm'))
        unique.setdefault(hardware+':'+program,[]).append(candidate.name)
    report={'nominal':manifest['nominal_space'],'unique_artifacts':len(unique),'artifact_groups':unique,
        'failed':failed,'host_seconds':time.monotonic()-started,'simulator_calls':0,
        'comparison_ready':not failed and len(unique)>settings['trials']}
    atomic_json(path,report);return report


def summarize(directory,case):
    """从实际结束报告生成轨迹；功耗违规保留数值，但不计最佳合格周期。"""
    state=load(directory/'state.json');entry=state['targets']['benchmark']
    timeline=[];seen=set()
    for key,candidate in entry.get('candidates',{}).items():
        artifact=directory/'builds'/key
        if not artifact.exists():continue
        hardware=digest(artifact/'hardware.json');program=digest(artifact/'programs'/(case+'.asm'))
        for task_key in candidate.get('case_keys',[]):
            if not task_key.startswith('case-'):continue
            metadata=state['jobs'][task_key];report_path=Path(metadata['job']['report'])
            if not report_path.exists():continue
            report=load(report_path);info=report.get('cases',{}).get(case,{})
            if report.get('program_sha256',{}).get(case)!=program or task_key in seen:continue
            seen.add(task_key);timing=info.get('timing',{})
            timeline.append({'task':task_key,'candidate':key,'hardware_hash':hardware,'asm_hash':program,
                'functional_passed':info.get('functional_passed'),'cycles':timing.get('cycles'),
                'power_w':timing.get('peak_window_power_w'),'cache_reused':any(h['stage']=='timing' for h in info.get('cache_hits',[])),
                'completed_wall':report_path.stat().st_mtime,'report':str(report_path)})
    timeline.sort(key=lambda row:(row['completed_wall'],row['task']))
    best=None
    cold_count=0
    campaign_start=state['budget']['deadline']-state['budget']['limits']['wall_seconds']
    for row in timeline:
        if row['functional_passed'] is True and row['cycles'] is not None and not row['cache_reused']:cold_count+=1
        row['cold_observation_index']=cold_count
        row['campaign_wall_seconds']=max(0,row['completed_wall']-campaign_start)
        if row['functional_passed'] is True and row['cycles'] and row['power_w'] is not None and row['power_w']<=20:
            best=min(best,row['cycles']) if best is not None else row['cycles']
        row['best_eligible_cycles']=best
    adaptive=entry.get('adaptive',{})
    trained_asks=[trial.get('completed_observations_at_ask') for trial in adaptive.get('trials',{}).values() if trial.get('origin')=='tpe']
    return {'tpe_model_phase_exercised':any(value is not None and value>=12 for value in trained_asks),
            'completed_observations_at_asks':trained_asks,
            'status':entry['status'],'timeline':timeline,'best_eligible_cycles':best,
            'cold_unique_observations':sum(row['functional_passed'] is True and row['cycles'] is not None and not row['cache_reused'] for row in timeline),
            'budget':load(directory/'status.json')['budget'],'candidate_count':len(entry.get('candidates',{})),
            'scope':'探索单案，不是正式整案成绩'}


def execute(manifest,out):
    if not preflight(manifest,out)['comparison_ready']:raise ValueError('唯一产物不足或构建失败，不执行算法对照')
    python,env=runtime();settings=manifest['settings'];summary_path=out/'comparison.json'
    summary=load(summary_path) if summary_path.exists() else {'runs':[],'scope':'固定硬件单案冷评估；不声称正式评分或统计显著性'}
    for seed in settings['seeds']:
        for sampler in ['random','tpe']:
            if epoch()!=manifest['source_epoch'] or verify_official()!=manifest['official']:raise ValueError('对照源码或官方身份变化，需要新批次')
            name=out.name+'-'+sampler+'-'+str(seed);directory=out.parent/name
            existing=next((item for item in summary['runs'] if item['seed']==seed and item['sampler']==sampler),None)
            if existing and existing['status']=='DONE':continue
            target=manifest['target']|{'sampler':sampler,'seed':seed}
            if not (directory/'identity.json').exists():inject(directory,{'op':'add','target':target},'initial-target')
            command=[python,'-m','codesign_lab.search.pipeline','--out',str(directory),'--execute',
                '--workers',str(settings['workers']),'--full-slots','2','--budget',str(settings['wall_seconds']),
                '--timeout',str(settings['timeout_seconds']),'--max-case-calls',str(2*(settings['trials']+1)),
                '--min-predicted-gain','999999','--report-interval','3600','--cache-dir',str(directory/'cold-cache')]
            if (directory/'identity.json').exists():command.append('--resume')
            directory.mkdir(parents=True,exist_ok=True)
            started=time.monotonic()
            with (directory/'benchmark-driver.log').open('a') as log:
                result=subprocess.run(command,cwd=ROOT,env=env,stdout=log,stderr=log)
            if result.returncode:raise RuntimeError('对照worker失败，保留日志：'+str(directory))
            report=summarize(directory,settings['case'])|{'sampler':sampler,'seed':seed,'host_seconds_this_attempt':time.monotonic()-started,'campaign':name}
            summary['runs']=[item for item in summary['runs'] if (item['seed'],item['sampler'])!=(seed,sampler)]+[report]
            atomic_json(summary_path,summary)
            print(json.dumps({key:report[key] for key in ['sampler','seed','status','best_eligible_cycles','cold_unique_observations']},ensure_ascii=False),flush=True)
            if report['status']!='DONE':raise RuntimeError('预算或目标未完成，不能继续宣称同预算对照完成')
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config',type=Path);parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--preflight',action='store_true');parser.add_argument('--execute',action='store_true')
    args=parser.parse_args();out=args.out.resolve()
    if out.parent!=ROOT/'workspace/pipeline':raise ValueError('对照输出必须位于workspace/pipeline')
    out.mkdir(parents=True,exist_ok=True)
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        manifest=plan(args.config,out)
        if args.execute:result=execute(manifest,out)
        elif args.preflight:result=preflight(manifest,out)
        else:result={'nominal_space':manifest['nominal_space'],'status':'仅规划，未构建或评估'}
        print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
