import json,fcntl,datetime,tempfile,os
from contextlib import contextmanager
from .config import ROOT
LEDGER=ROOT/'data/experiments.jsonl'
@contextmanager
def locked():
    with (LEDGER.parent/'.experiments.lock').open('a') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX)
        yield

def read():return [json.loads(line) for line in LEDGER.read_text().splitlines() if line.strip()] if LEDGER.exists() else []
def append(record):
    record=compact_record(record)
    record.setdefault('timestamp',datetime.datetime.now(datetime.timezone.utc).isoformat())
    with locked():
        if record['id'] in [entry['id'] for entry in read()]:raise ValueError('Duplicate experiment ID')
        with LEDGER.open('a') as stream:stream.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n');stream.flush()
def update(identifier,fields):
    with locked():
        entries=read();entry=next(entry for entry in entries if entry['id']==identifier);entry.update(fields)
        entry.update(compact_record(entry))
        with tempfile.NamedTemporaryFile(mode='w',dir=LEDGER.parent,delete=False) as output:
            for entry in entries:output.write(json.dumps(entry,ensure_ascii=False,allow_nan=False)+'\n')
            output.flush();os.fsync(output.fileno());path=output.name
        os.replace(path,LEDGER)
    return next(entry for entry in entries if entry['id']==identifier)
def state():return json.loads((ROOT/'data/state.json').read_text())

def compact_record(record):
    """账本只保留可比较事实；详细资源日历属于受保护最佳版本的报告。"""
    record=dict(record)
    cases={}
    for case, info in record.get('cases',{}).items():
        if not isinstance(info,dict):
            cases[case]=info;continue
        item={key:value for key,value in info.items() if key!='resource_stats'}
        if isinstance(item.get('timing'),dict):
            item['timing']={key:value for key,value in item['timing'].items()
                            if key!='resource_stats'}
        cases[case]=item
    if 'cases' in record:record['cases']=cases
    profiles={}
    for case, info in record.get('profile',{}).items():
        if not isinstance(info,dict):continue
        keep={key:info[key] for key in ('cycles','pressure_leader','wait_estimate',
              'aggregate_issue_wait_cycles','issue_wait_note','stage_status','hypotheses')
              if key in info}
        resources=info.get('resources',[])
        if isinstance(resources,list):
            keep['top_resources']=sorted((value for value in resources if isinstance(value,dict)),
                key=lambda value:value.get('utilization') or 0,reverse=True)[:5]
        profiles[case]=keep
    if 'profile' in record:record['profile']=profiles
    return record
