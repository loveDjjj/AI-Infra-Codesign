"""校验结构化研究决策，事务式更新目标池并持久记录；不执行模型代码。"""
import copy
import json
import math
import re
import os
from ..config import ROOT, load
from ..records import read, locked
from .targets import validate_target


def schema_check(value, schema, path='$'):
    """校验项目生成 schema 使用的有限关键字；不宣称支持任意 JSON Schema。"""
    supported = {'type','properties','required','additionalProperties','items','enum','const','minimum','maximum','minProperties'}
    unknown = set(schema)-supported
    if unknown:
        raise ValueError('schema 含未实现关键字：'+str(sorted(unknown)))
    kind = schema.get('type')
    correct = {'object':isinstance(value,dict),'array':isinstance(value,list),'string':isinstance(value,str),
        'integer':type(value) is int,'number':type(value) in (int,float) and math.isfinite(value)}
    if kind and not correct.get(kind,False):
        raise ValueError(path+' 类型无效')
    if 'const' in schema and value != schema['const']:
        raise ValueError(path+' 常量不匹配')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(path+' 不在允许域')
    if isinstance(value,dict):
        if not set(schema.get('required',[]))<=set(value):
            raise ValueError(path+' 缺少必要字段')
        props=schema.get('properties',{})
        if schema.get('additionalProperties') is False and set(value)-set(props):
            raise ValueError(path+' 含未知字段')
        if len(value)<schema.get('minProperties',0):
            raise ValueError(path+' 必須提供变量')
        for name,item in value.items():
            if name in props:schema_check(item,props[name],path+'.'+name)
    if isinstance(value,list):
        for i,item in enumerate(value):schema_check(item,schema['items'],path+'['+str(i)+']')
    for op in ['minimum','maximum']:
        if op in schema and ((op=='minimum' and value<schema[op]) or (op=='maximum' and value>schema[op])):
            raise ValueError(path+' 超过数值范围')


def append_decision(record):
    path=ROOT/'data/decisions.jsonl'
    with locked():
        rows=[json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
        previous=next((r for r in rows if r['id']==record['id']),None)
        if previous:
            if previous!=record:raise ValueError('同一决策 ID 内容冲突')
            return
        with path.open('a') as stream:
            stream.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n');stream.flush();os.fsync(stream.fileno())


def apply_decision(pool,triggers,decision):
    schema_check(decision,load(ROOT/'schemas/ai-decision.schema.json'))
    identifier=decision['decision_id'];lane=decision['lane']
    if not re.fullmatch('[a-f0-9]{64}',identifier):raise ValueError('决策 ID 无效')
    applied=pool.state.get('applied_decisions',{})
    if identifier in applied:
        if applied[identifier]['decision']!=decision:raise ValueError('重复决策内容不一致')
        append_decision(applied[identifier]);return {'status':'reused','decision_id':identifier}
    pending=triggers.state['lanes'].get(lane,{}).get('pending')
    if not pending or pending['decision_id']!=identifier:raise ValueError('决策没有对应待分析请求')
    records=read();valid_ids={r['id'] for r in records}
    commands=[];seen_targets=set()
    for raw in decision['new_targets']:
        target=copy.deepcopy(raw)
        pairs=target['variables']
        if len({pair['path'] for pair in pairs})!=len(pairs):raise ValueError('重复变量定义')
        target['variables']={pair['path']:pair['values'] for pair in pairs}
        if lane != 'global' and target['lane']!=lane:
            raise ValueError('分方向专家只允许提出本 lane 目标')
        if lane == 'global' and target['lane'] == 'global':
            raise ValueError('全局分析目标必须指定具体研究方向')
        validate_target(target,pool.source_epoch,records,max_trials=pool.max_proposals)
        if target['target_id'] in seen_targets:raise ValueError('重复目标 ID')
        seen_targets.add(target['target_id']);commands.append({'op':'add','target':target})
    for target_id in decision['stop_targets']:
        entry=pool.state['targets'].get(target_id)
        if not entry or (lane != 'global' and entry['definition']['lane']!=lane):
            raise ValueError('停止目标不属于当前 lane')
        commands.append({'op':'stop','target_id':target_id})
    for item in decision['conclusions']+decision['implementation_proposals']:
        if not set(item['evidence_ids'])<=valid_ids:raise ValueError('决策引用不存在的证据')
    for item in decision['implementation_proposals']:
        identity=item.get('transformation_id')
        if identity is not None and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',identity) is None:
            raise ValueError('结构机制 ID 无效')
    for item in decision['profile_requests']:
        if item['record_id'] not in valid_ids:raise ValueError('profile 引用不存在的记录')
    # 暂存完整状态；任一目标越域或超预算时不提交任何队列变化。
    staged=copy.copy(pool);staged.state=copy.deepcopy(pool.state);staged.save=lambda:None
    for index,command in enumerate(commands):
        result=staged.apply({'request_id':identifier+'-'+str(index),'command':command})
        if result['status']!='accepted':raise ValueError(result['error'])
    from .triggers import Triggers
    staged_triggers=Triggers(staged.state)
    staged_triggers.acknowledge(lane,identifier)
    record={'id':identifier,'lane':lane,'source_epoch':pool.source_epoch,'decision':decision}
    staged.state.setdefault('applied_decisions',{})[identifier]=record
    pool.state.clear();pool.state.update(staged.state)
    # triggers 的引用重新指向事务提交后的状态，不能保留旧字典。
    triggers.state=pool.state['analysis']
    pool.save();append_decision(record)
    return {'status':'accepted','decision_id':identifier,'targets_added':len(decision['new_targets'])}
