"""主控调用独立搜索解释器；请求/回答可重放，主控不导入 Optuna。"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from ..config import ROOT,load
from .scheduler import atomic_json
from .locks import acquire


class TPEClient:
    def __init__(self,directory,variables,scope,*,seed=0,startup_trials=12):
        self.directory=Path(directory).resolve()
        if not self.directory.is_relative_to(ROOT/'workspace'):raise ValueError('TPE 状态必须位于 workspace')
        self.variables=variables;self.scope=scope;self.seed=seed;self.startup=startup_trials
        config=load(ROOT/'configs/search-toolchain.yaml')
        self.python=ROOT/config['python']
        if not self.python.is_file():raise ValueError('独立搜索环境尚未安装')
        self.directory.mkdir(parents=True,exist_ok=True)

    def call(self,operation,request_id,**fields):
        if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}',request_id):
            raise ValueError('后端请求 ID 无效')
        request={'directory':str(self.directory),'variables':self.variables,'scope':self.scope,
            'seed':self.seed,'startup_trials':self.startup,'operation':operation,'request_id':request_id}|fields
        fingerprint=hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()
        path=self.directory/'requests'/(request_id+'.json')
        out=self.directory/'answers'/(request_id+'.json')
        with (self.directory/'client.lock').open('a') as lock:
            acquire(lock)
            if path.exists() and load(path)!=request:raise ValueError('同请求 ID 内容改变')
            if not path.exists():atomic_json(path,request)
            if not out.exists():
                env=os.environ.copy();env.pop('PYTHONHOME',None)
                env.update(PYTHONPATH=str(ROOT/'src'),OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1')
                result=subprocess.run([str(self.python),'-m','codesign_lab.search.tpe',str(path),'--out',str(out)],
                    cwd=ROOT,env=env,capture_output=True,text=True,timeout=60)
                if result.returncode:raise RuntimeError('TPE 后端失败：'+result.stderr[-2000:])
            answer=load(out)
            if answer.get('request_sha256')!=fingerprint or answer.get('operation')!=operation or answer.get('optuna_version')!='4.3.0':
                raise ValueError('TPE 回答身份不一致')
            return answer['answer']

    def ask(self,request_id):return self.call('ask',request_id)
    def tell(self,request_id,number,observation):return self.call('tell',request_id,number=number,observation=observation)


def main():
    """供监督 worker 调用，不在主控事件循环等待模型。"""
    import argparse
    from ..config import digest
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('spec',type=Path);parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();spec=load(args.spec)
    if not args.spec.resolve().is_relative_to(ROOT/'workspace') or not args.out.resolve().is_relative_to(ROOT/'workspace'):
        raise ValueError('监督请求/回答必须在 workspace')
    identity=digest(args.spec)
    if args.out.exists():
        if load(args.out).get('spec_sha256')!=identity:raise ValueError('已有监督回答身份冲突')
        return
    client=TPEClient(**spec['client'])
    answer=client.call(spec['operation'],spec['request_id'],**spec.get('fields',{}))
    atomic_json(args.out,{'spec_sha256':identity,'request_id':spec['request_id'],'answer':answer})


if __name__=='__main__':main()
