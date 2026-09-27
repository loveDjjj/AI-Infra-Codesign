'整理实测资源压力，并明确标记非因果的等待估计。'
def summarize(timing):
    stats=timing.get('resource_stats',{});cycles=timing.get('cycles',0)
    resources=[]
    for group,entities in [('shared',{'shared':stats.get('shared',{})}),('sm',stats.get('sms',{}))]:
        for entity,data in entities.items():
            for resource,busy in data.get('busy_cycles',{}).items():
                count=data.get('entity_count',{}).get(resource,1)
                resources.append({'scope':group,'entity':entity,'resource':resource,'busy_cycles':busy,'capacity':count,'utilization':busy/(cycles*count) if cycles and count else None,'basis':'measured reservation service / entity capacity'})
    ranked=sorted(resources,key=lambda r:r['utilization'] or 0,reverse=True)
    pressure=ranked[0] if ranked else None
    return {'cycles':cycles,'resources':resources,'pressure_leader':pressure,
        'wait_estimate':{'method':'1 - maximum entity-normalized busy ratio','fraction':1-pressure['utilization'] if pressure else None,'classification':'proxy, not measured stall','confidence':'low','reason':'Uncovered capacity can reflect dependencies, imbalance, idle workgroups or scheduling. Concurrent resources overlap; ratios cannot identify or partition wait causes.'},
        'aggregate_issue_wait_cycles':{key:data.get('issue_wait_cycles') for key,data in stats.get('sms',{}).items()},
        'issue_wait_note':'Aggregate across issue attempts/workgroups; may exceed total cycles. Cause attribution unavailable.',
        'hypotheses':hypotheses(resources),'timeline':stats.get('timeline'),'stage_status':'Event trace required for measured operator spans'}

def operator_spans(program,operator_map,trace):
    '展开 ISA 循环，按源指令行将模板范围关联到具体事件。'
    from ..config import bootstrap
    bootstrap()
    from codesign.challenge.isa import iter_parse
    matched={}
    for ins in iter_parse(program):
        event=ins.args.get('event')
        if event:matched.setdefault(ins.line,[]).append(event)
    result=[]
    for stage in operator_map:
        events={event for line,values in matched.items() if stage['source_line_start']<=line<=stage['source_line_end'] for event in values if event in trace}
        if events:
            start=min(trace[e]['issue'] for e in events);end=max(trace[e]['finish'] for e in events)
            result.append(stage|stage_labels(stage)|{'start':start,'finish':end,'span_cycles':end-start,'events':len(events),'basis':'measured event issue-to-finish; spans can overlap'})
    return result

def hypotheses(resources):
    ranked=sorted(resources,key=lambda item:item['utilization'] or 0,reverse=True)
    if not ranked:return []
    leader=ranked[0];name=leader['resource']
    if name=='noc' or name.startswith('hbm') or name.startswith('noc_'):reason='Data movement is a priority hypothesis; compare residency, transfer size and multicast alignment.'
    elif name.startswith('rf'):reason='Register access is a priority hypothesis; examine operand reuse and RF traffic.'
    elif name in ['tc','vec','sfu','reduce']:reason='Arithmetic service is a priority hypothesis; inspect tiling, precision and lane occupancy.'
    else:reason='Issue capacity may matter; inspect instruction density and dependencies.'
    result=[{'hypothesis':reason,'basis':leader,'confidence':'low','status':'requires intervention experiment; busy ratio alone does not establish a bound'}]
    tc=[item['utilization'] for item in resources if item['scope']=='sm' and item['resource']=='tc']
    if tc:result.append({'hypothesis':'SM compute load imbalance may limit completion','basis':{'tc_max_minus_min':max(tc)-min(tc)},'confidence':'low','status':'Check stage-level work assignment before changing scheduling'})
    return result

def stage_pressure(stage,timeline):
    '按时间重叠加权平均资源分箱；这只是近似压力，不是实测等待。'
    if not timeline:return None
    edges=timeline['edges_cycles'];ranked=[]
    groups=[('shared',timeline.get('shared',{}))]+[(key,value) for key,value in timeline.get('sms',{}).items()]
    for entity,group in groups:
        for name,values in group.get('utilization',{}).items():
            total=0;duration=0
            for lo,hi,value in zip(edges,edges[1:],values):
                overlap=max(0,min(hi,stage['finish'])-max(lo,stage['start']))
                total+=overlap*value;duration+=overlap
            if duration:ranked.append({'entity':entity,'resource':name,'fraction':total/duration})
    ranked.sort(key=lambda item:item['fraction'],reverse=True)
    return {'method':'overlap-weighted utilization bins; shared traffic may come from concurrent operators','leader':ranked[0] if ranked else None,'confidence':'low','cause':'unresolved; pressure proxy only'}

def stage_labels(stage):
    import re
    name=stage.get('name','')
    layer=re.search(r'l(\d+)',name);step=re.match(r'd(\d+)',name)
    suffix=next((kind for kind in ['qkv','ln1','ln2','w1','w2','wo'] if name.endswith(kind)),None)
    return {'phase':'prompt' if name.startswith('p') else 'decode' if step or name.startswith('s') else 'unknown',
            'layer':int(layer.group(1)) if layer else None,'decode_step':int(step.group(1)) if step else 0 if name.startswith('s') else None,
            'semantic_operator':suffix or stage.get('operator'),'fusion_note':'Bias, residual and activation may be fused in GEMM; inspect implementation/config'}
