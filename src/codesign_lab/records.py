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
    record.setdefault('timestamp',datetime.datetime.now(datetime.timezone.utc).isoformat())
    with locked():
        if record['id'] in [entry['id'] for entry in read()]:raise ValueError('Duplicate experiment ID')
        with LEDGER.open('a') as stream:stream.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n');stream.flush()
def update(identifier,fields):
    with locked():
        entries=read();entry=next(entry for entry in entries if entry['id']==identifier);entry.update(fields)
        with tempfile.NamedTemporaryFile(mode='w',dir=LEDGER.parent,delete=False) as output:
            for entry in entries:output.write(json.dumps(entry,ensure_ascii=False,allow_nan=False)+'\n')
            output.flush();os.fsync(output.fileno());path=output.name
        os.replace(path,LEDGER)
    return next(entry for entry in entries if entry['id']==identifier)
def state():return json.loads((ROOT/'data/state.json').read_text())
