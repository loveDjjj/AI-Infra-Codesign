"""目标级控制以实际启动状态为界，不回收已经发生的试验成本。"""
TERMINAL = {'STATIC_REJECTED', 'BUILD_FAILED', 'DUPLICATE', 'OBSERVED', 'EVALUATION_FAILED'}


def started_candidates(entry, jobs):
    return {identity for identity,candidate in entry.get('candidates',{}).items()
            if candidate['status'] in TERMINAL or candidate['status']=='EVALUATING'
            or jobs.get('build-'+identity,{}).get('status') in {'STARTING','RUNNING','FINISHED'}}


def priorities(targets):
    result={}
    for entry in targets.values():
        if entry['status']!='ACTIVE':continue
        priority=entry['definition']['priority']
        keys=list(entry.get('adaptive', {}).get('pending_jobs', []))
        keys.extend(entry.get('adaptive', {}).get('prior_build_keys', []))
        if entry.get('adaptive') and entry.get('base_key'):keys.append('build-'+entry['base_key'])
        for key in keys:result[key]=max(result.get(key,0),priority)
        for identity,candidate in entry.get('candidates',{}).items():
            keys=[]
            if candidate['status']=='BUILDING':keys=['build-'+identity,'build-'+candidate['base_key']]
            if candidate['status']=='EVALUATING':keys=candidate.get('case_keys',[])
            for key in keys:result[key]=max(result.get(key,0),priority)
    return result
