"""历史观测必须对应当前再生产物与冻结工具，不能只比较配置名字。"""
import copy
import math
from pathlib import Path
from ..config import ROOT,bootstrap,load,digest
from ..evaluation.stages import functional_evidence


def validated_prior(record,candidate,case,variables,seeds=(7,123)):
    """调用者先隔离构建该历史配置；这里只校验原件，不运行模拟。"""
    candidate=Path(candidate)
    report_path=ROOT/record['report']
    report=load(report_path)
    if report.get('mode') not in {'both','estimate'}:raise ValueError('历史证据没有时序阶段')
    if report.get('runtime',{}).get('numpy')!='2.5.3' or not report.get('runtime',{}).get('python','').startswith('3.12.'):
        raise ValueError('历史评估运行环境不匹配')
    bootstrap()
    from codesign.challenge.hardware import Hardware
    from codesign.challenge.runner import provenance
    hardware=load(candidate/'hardware.json')
    programs={name:(candidate/'programs'/(name+'.asm')).read_text() for name in ['M1_P1','M2_D1']}
    expected=provenance(Hardware.from_dict(hardware),programs)
    # estimate 报告含已验证依赖的完整功能检查，校验所有字段，不修改原件。
    merged=copy.deepcopy(report);merged['mode']='both'
    functional_evidence(merged,case,hardware,digest(candidate/'programs'/(case+'.asm')),expected,list(seeds))
    info=report['cases'][case];timing=info.get('timing',{})
    if any(hit['stage']=='timing' for hit in info.get('cache_hits',[])):raise ValueError('缓存请求不是历史原始观测')
    cycles,power=timing.get('cycles'),timing.get('peak_window_power_w')
    if any(type(value) not in (int,float) or not math.isfinite(value) for value in [cycles,power]) or cycles<=0 or power<0:
        raise ValueError('历史性能指标无效')
    recorded=record.get('cases',{}).get(case,{}).get('timing',{})
    if recorded.get('cycles')!=cycles or recorded.get('peak_window_power_w')!=power:
        raise ValueError('历史账本与原始报告不一致')
    config=load(candidate/'config.json');params={}
    for path,values in variables.items():
        node=config
        for part in path.split('.'):node=node[part]
        if type(node) is not int or node not in values:raise ValueError('历史参数不在当前搜索域')
        params[path]=node
    return params,{'id':record['id'],'case':case,'hardware_hash':digest(candidate/'hardware.json'),
        'functional_passed':True,'cycles':cycles,'peak_power_w':power,'cache_reused':False,
        'evidence_kind':'historical_prior','report_sha256':digest(report_path)}


def prepare_priors(p,target_id,entry):
    """先验构建与校验均用监督任务；未处理完先验时不允许提案。"""
    from .adaptive import identity
    from ..records import read
    from .scheduler import atomic_json
    state=entry['adaptive'];priors=state.setdefault('priors',{})
    if not state.get('prior_snapshot_complete'):
        records={record['id']:record for record in read()}
        for identifier in entry['definition'].get('prior_record_ids',[]):
            record=copy.deepcopy(records[identifier])
            priors[identifier]={'record':record,'report_sha256':digest(ROOT/record['report']),'status':'QUEUED'}
        state['prior_snapshot_complete']=True;p.pool.save()
    for identifier,prior in priors.items():
        if prior['status'] in {'DONE','REJECTED','IMPORTING','VALIDATING'}:continue
        config=prior['record']['config'];build_key=identity(config)
        candidate=p.out/'builds'/build_key;path=p.out/'configs'/(build_key+'.json')
        build_job='build-'+build_key
        if build_job not in state.setdefault('prior_build_keys',[]):state['prior_build_keys'].append(build_job)
        if not path.exists():atomic_json(path,config)
        p.enqueue({'key':build_job,'stage':'build','candidate':candidate,'cases':[],'seeds':[7,123],
            'target_owned':True,'memory_bytes':1024**3,'priority':entry['definition']['priority'],
            'command':[p.python,'-m','codesign_lab.cli','build',str(path),'--out',str(candidate)]})
        result=p.out/'jobs'/(build_job+'.result.json')
        if not result.exists():continue
        if load(result)['status']!='completed':entry.update(status='FAILED',error='历史先验再生失败');return False
        request_id='prior-'+identity(identifier)
        key='sample-'+identity([target_id,request_id+'-validate'])
        spec=p.out/'sampling'/(key+'.request.json');report=p.out/'sampling'/(key+'.answer.json')
        payload={'record':prior['record'],'report_sha256':prior['report_sha256'],'candidate':str(candidate),
                 'case':entry['definition']['cases'][0],'variables':entry['definition']['variables'],
                 'hardware_hash':digest(p.out/'builds'/entry['base_key']/'hardware.json'),'request_id':request_id}
        if not spec.exists():atomic_json(spec,payload)
        elif load(spec)!=payload:raise ValueError('先验校验请求身份改变')
        prior.update(status='VALIDATING',validation_job=key)
        if key not in state['pending_jobs']:state['pending_jobs'].append(key)
        p.enqueue({'key':key,'stage':'sample','operation':'validate_prior','prior_id':identifier,
            'target_id':target_id,'request_id':request_id,'request_spec':str(spec),'report':report,
            'target_owned':True,'priority':entry['definition']['priority'],'memory_bytes':1024**3,
            'command':[p.python,'-m','codesign_lab.search.priors',str(spec),'--out',str(report)]})
    return all(prior['status'] in {'DONE','REJECTED'} for prior in priors.values())


def main():
    """冻结评估环境中的先验校验任务；非法历史不产生模型观测。"""
    import argparse
    from .scheduler import atomic_json
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('spec',type=Path);parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    if not all(path.resolve().is_relative_to(ROOT/'workspace') for path in [args.spec,args.out]):raise ValueError('先验任务必须位于 workspace')
    spec=load(args.spec);fingerprint=digest(args.spec)
    if args.out.exists():
        if load(args.out).get('spec_sha256')!=fingerprint:raise ValueError('先验回答身份改变')
        return
    try:
        if digest(ROOT/spec['record']['report'])!=spec['report_sha256']:raise ValueError('历史原件在运行期间改变')
        params,observation=validated_prior(spec['record'],spec['candidate'],spec['case'],spec['variables'])
        if observation['hardware_hash']!=spec['hardware_hash']:raise ValueError('历史先验硬件与目标不同')
        answer={'params':params,'observation':observation}
    except ValueError as exc:answer={'rejected':str(exc)}
    atomic_json(args.out,{'spec_sha256':fingerprint,'request_id':spec['request_id'],'answer':answer})


if __name__=='__main__':main()
