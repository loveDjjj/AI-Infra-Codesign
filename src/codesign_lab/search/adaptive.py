"""动态目标的异步 TPE 提案与反馈；主控只消费监督结果。"""
import copy
import hashlib
import json
from pathlib import Path

from ..config import ROOT,load,digest
from .scheduler import atomic_json
from .samplers import target_seed
from .prune import reject


def identity(value):return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def request_job(p,target_id,entry,operation,request_id,fields=None):
    state=entry['adaptive'];base=p.out/'builds'/entry['base_key']
    key='sample-'+identity([target_id,request_id])
    spec=p.out/'sampling'/(key+'.request.json');report=p.out/'sampling'/(key+'.answer.json')
    payload={'client':{'directory':str(p.out/'samplers'/target_id),
        'variables':entry['definition']['variables'],
        'scope':{'source_epoch':p.pool.source_epoch,'hardware_hash':digest(base/'hardware.json'),
                 'case':entry['definition']['cases'][0],
                 **({'family_sha256':entry['definition']['execution_sha256']}
                    if entry['definition'].get('execution_root') else {})},
        'seed':target_seed(entry['definition']),'startup_trials':12},
        'operation':operation,'request_id':request_id,'fields':fields or {}}
    if spec.exists() and load(spec)!=payload:raise ValueError('采样请求身份改变')
    if not spec.exists():atomic_json(spec,payload)
    if key not in state.setdefault('pending_jobs',[]):state['pending_jobs'].append(key)
    job={'key':key,'stage':'sample','target_id':target_id,'operation':operation,'request_id':request_id,
        'request_spec':str(spec),'report':report,'target_owned':not request_id.startswith('discard-'),'priority':entry['definition']['priority'],
        'memory_bytes':512*1024**2,'command':[p.python,'-m','codesign_lab.search.tpe_client',str(spec),'--out',str(report)]}
    if operation=='ask':state['ask_job']=key
    p.enqueue(job)
    return key


def settle_unused(p,target_id,entry):
    """关闭已创建却未执行的 trial，不填性能；收尾不属于停止目标的探索订阅。"""
    state=entry.get('adaptive',{})
    for candidate_id,trial in state.get('trials',{}).items():
        if entry.get('candidates',{}).get(candidate_id,{}).get('status')!='CANCELLED' or trial.get('told'):continue
        prior=p.pool.state.get('jobs',{}).get(trial.get('tell_job'),{})
        if trial.get('tell_job') and prior.get('status')!='CANCELLED':continue
        discarded=state.setdefault('unconsumed_proposals',[])
        if not any(item['proposal']['number']==trial['number'] for item in discarded):
            discarded.append({'proposal':{'number':trial['number'],'candidate_id':trial['backend_id']},
                              'reason':'候选取消；关闭在途 trial，不填性能'})
    for proposal in state.get('unconsumed_proposals',[]):
        if proposal.get('tell_job') or proposal.get('told'):continue
        number=proposal['proposal']['number']
        observation={'id':target_id+'-discard-'+str(number),
            'case':entry['definition']['cases'][0],
            'hardware_hash':digest(p.out/'builds'/entry['base_key']/'hardware.json'),
            'functional_passed':None,'failure_kind':'cancelled'}
        proposal['tell_job']=request_job(p,target_id,entry,'tell','discard-'+str(number),
                                       {'number':number,'observation':observation})


def prepare(p,target_id,entry):
    settle_unused(p,target_id,entry)
    if entry['status'] not in {'QUEUED','ACTIVE'}:return
    entry['status']='ACTIVE';entry.setdefault('candidates',{})
    state=entry.setdefault('adaptive',{'counter':0,'trials':{},'pending_jobs':[],'processed':[]})
    source_root=Path(entry['definition'].get('execution_root',ROOT))
    base_key=p.design_key(entry['base_config'],source_root);entry['base_key']=base_key
    base=p.out/'builds'/base_key;config=p.out/'configs'/(base_key+'.json')
    if not config.exists():atomic_json(config,entry['base_config'])
    p.enqueue({'key':'build-'+base_key,'stage':'build','candidate':base,'cases':[],'seeds':[7,123],
        'target_owned':True,'memory_bytes':1024**3,'source_root':str(source_root),
        'command':p.build_command(source_root,config,base)})
    result=p.out/'jobs'/('build-'+base_key+'.result.json')
    if not result.exists():return
    if load(result)['status']!='completed':entry.update(status='FAILED',error='TPE 基准构建失败');return
    from .priors import prepare_priors
    try:
        if not prepare_priors(p,target_id,entry):return
    except (ValueError,KeyError,OSError) as exc:
        entry.update(status='FAILED',error='历史先验准备失败：'+str(exc));return
    # 先提交已经结束候选的反馈，再允许下一次提案使用更新后的模型。
    for candidate_id,trial in state['trials'].items():
        if trial.get('tell_job') or trial.get('told'):continue
        candidate=entry['candidates'][candidate_id]
        artifact=p.out/'builds'/candidate_id
        for case_key in candidate.get('case_keys',[]):
            meta=p.pool.state.get('jobs',{}).get(case_key,{})
            job=meta.get('job',{})
            if meta.get('status')!='FINISHED' or job.get('stage')!='case':continue
            report=Path(job['report'])
            if not report.exists():continue
            data=load(report);case=entry['definition']['cases'][0]
            if data.get('hardware')!=load(artifact/'hardware.json') or data.get('program_sha256',{}).get(case)!=digest(artifact/'programs'/(case+'.asm')):continue
            info=data.get('cases',{}).get(case,{});timing=info.get('timing',{})
            execution=load(p.out/'jobs'/(case_key+'.result.json'))
            observation={'id':p.out.name+'-'+case_key,'case':case,'hardware_hash':digest(base/'hardware.json'),
                'functional_passed':info.get('functional_passed'),'cycles':timing.get('cycles'),
                'peak_power_w':timing.get('peak_window_power_w'),
                'cache_reused':any(hit['stage']=='timing' for hit in info.get('cache_hits',[])),
                'failure_kind':'infrastructure' if execution['status']!='completed' and info.get('functional_passed') is not False else None}
            trial['tell_job']=request_job(p,target_id,entry,'tell','tell-'+str(trial['number']),
                                        {'number':trial['number'],'observation':observation})
            break
        if not trial.get('tell_job') and candidate['status'] in {'STATIC_REJECTED','BUILD_FAILED','EVALUATION_FAILED','CANCELLED'}:
            observation={'id':target_id+'-'+candidate_id,'case':entry['definition']['cases'][0],
                'hardware_hash':digest(base/'hardware.json'),'functional_passed':None,
                'failure_kind':'infrastructure' if candidate['status']=='EVALUATION_FAILED' else 'design'}
            trial['tell_job']=request_job(p,target_id,entry,'tell','tell-'+str(trial['number']),{'number':trial['number'],'observation':observation})
    count=sum(c['status']!='CANCELLED' for c in entry['candidates'].values())
    unfinished=sum(c['status'] in {'BUILDING','EVALUATING'} for c in entry['candidates'].values())
    if state.get('ask_job') or state.get('exhausted') or count>=entry['definition']['max_trials']:return
    if any(trial.get('tell_job') and not trial.get('told') for trial in state['trials'].values()):return
    if unfinished>=entry['definition'].get('max_inflight',1):return
    request_id='ask-'+str(state['counter']);state['counter']+=1
    request_job(p,target_id,entry,'ask',request_id)


def complete(p,job,result):
    """异常回答隔离到目标，不停止其他探索和验收。"""
    try:
        _complete(p,job,result)
    except (ValueError,KeyError,TypeError,OSError) as exc:
        entry=p.pool.state['targets'][job['target_id']]
        entry.update(status='FAILED',error='采样回答校验失败：'+str(exc))
        state=entry['adaptive']
        if job['key'] in state['pending_jobs']:state['pending_jobs'].remove(job['key'])
        if job['key'] not in state['processed']:state['processed'].append(job['key'])
        p.pool.save()


def _complete(p,job,result):
    entry=p.pool.state['targets'][job['target_id']];state=entry['adaptive']
    if job['key'] in state['processed']:return
    if job['key'] in state['pending_jobs']:state['pending_jobs'].remove(job['key'])
    if result['status']!='completed':
        entry.update(status='FAILED',error='采样监督任务失败：'+result['status'])
        state['processed'].append(job['key']);p.pool.save();return
    response=load(job['report'])
    if response.get('spec_sha256')!=digest(Path(job['request_spec'])) or response.get('request_id')!=job['request_id']:
        raise ValueError('采样监督回答不属于请求')
    answer=response['answer']
    if job['operation']=='validate_prior':
        prior=state['priors'][job['prior_id']]
        if 'rejected' in answer:prior.update(status='REJECTED',error=answer['rejected'])
        elif entry['status']=='ACTIVE':
            prior.update(status='IMPORTING',validated=answer)
            prior['import_job']=request_job(p,job['target_id'],entry,'import',job['request_id'],answer)
        else:prior.update(status='REJECTED',error='目标停止，未导入先验')
    elif job['operation']=='import':
        observation=load(Path(job['request_spec']))['fields']['observation']
        prior=state['priors'][observation['id']]
        prior.update(status='DONE',outcome=answer)
    elif job['operation']=='tell':
        number=load(Path(job['request_spec']))['fields']['number']
        matched=False
        for trial in state['trials'].values():
            if trial['number']==number:trial.update(told=True,outcome=answer);matched=True
        for proposal in state.get('unconsumed_proposals',[]):
            if proposal['proposal']['number']==number:proposal.update(told=True,outcome=answer);matched=True
        if not matched:raise ValueError('反馈 trial 没有对应提案')
    else:
        state['ask_job']=None
        if answer is None:state['exhausted']=True;entry['proposal_exhausted']=True
        elif entry['status']=='ACTIVE' and sum(c['status']!='CANCELLED' for c in entry['candidates'].values())<entry['definition']['max_trials']:
            params=answer['params'];variables=entry['definition']['variables']
            if set(params)!=set(variables) or any(type(params[path]) is not int or params[path] not in values for path,values in variables.items()):raise ValueError('模型提案超出合法域')
            config=copy.deepcopy(entry['base_config'])
            for path,value in params.items():
                node=config;parts=path.split('.')
                for part in parts[:-1]:node=node[part]
                node[parts[-1]]=value
            source_root=Path(entry['definition'].get('execution_root',ROOT))
            candidate_id=p.design_key(config,source_root)
            if candidate_id not in entry['candidates']:
                reason=reject(config);entry['trials_launched']+=1
                entry['candidates'][candidate_id]={'status':'STATIC_REJECTED' if reason else 'BUILDING',
                    'reason':reason,'base_key':entry['base_key'],'cases':entry['definition']['cases'],'case_keys':[]}
                state['trials'][candidate_id]={'number':answer['number'],'backend_id':answer['candidate_id'],
                    'origin':answer.get('proposal_origin'),'params':params,
                    'completed_observations_at_ask':answer.get('completed_observations_at_ask')}
                if not reason:
                    path=p.out/'configs'/(candidate_id+'.json');atomic_json(path,config)
                    p.enqueue({'key':'build-'+candidate_id,'stage':'build','candidate':p.out/'builds'/candidate_id,
                        'cases':[],'seeds':[7,123],'target_owned':True,'priority':entry['definition']['priority'],
                        'memory_bytes':1024**3,'source_root':str(source_root),
                        'command':p.build_command(source_root,path,p.out/'builds'/candidate_id)})
    if job['operation']=='ask' and answer is not None and entry['status']!='ACTIVE':
        state.setdefault('unconsumed_proposals',[]).append({'request_id':job['request_id'],
            'proposal':answer,'reason':'目标已停止或失败；保留后端 trial 身份，不构建'})
    elif job['operation']=='ask' and answer is not None and answer.get('candidate_id') not in {
            trial.get('backend_id') for trial in state['trials'].values()}:
        state.setdefault('unconsumed_proposals',[]).append({'request_id':job['request_id'],
            'proposal':answer,'reason':'预算已收缩或候选已存在；不增加试验'})
    settle_unused(p,job['target_id'],entry)
    state['processed'].append(job['key']);p.pool.save()
