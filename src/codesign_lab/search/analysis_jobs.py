"""持久分析调用句柄，复用独立监督和进程身份协议。"""
import subprocess
from pathlib import Path
from ..config import ROOT,load
from ..evaluation.pipeline import runtime
from ..evaluation.recovery import recover_start,reap_orphan,supervisor_lost
from .scheduler import atomic_json


class AnalysisCall:
    def __init__(self, metadata, *, launch=False):
        self.metadata=metadata;self.spec=Path(metadata['spec']);self.child=None
        self.python,self.env=runtime()
        if launch:
            with self.spec.with_suffix('.log').open('a') as log:
                self.child=subprocess.Popen([self.python,'-m','codesign_lab.evaluation.worker',str(self.spec)],
                    cwd=ROOT,env=self.env,stdout=log,stderr=log,start_new_session=True)
        elif not Path(load(self.spec)['result']).exists():
            if not self.spec.with_suffix('.lease.json').exists():
                answer=recover_start(self.spec,self.python,self.env)
                self.child=answer.get('child')
            elif supervisor_lost(self.spec):
                reap_orphan(self.spec)

    def done(self):
        if Path(load(self.spec)['result']).exists():
            if self.child is not None:self.child.wait(timeout=5)
            return True
        if self.spec.with_suffix('.lease.json').exists() and supervisor_lost(self.spec):
            reap_orphan(self.spec)
            return True
        return False

    def result(self):
        result=load(Path(load(self.spec)['result']))
        if result['status']!='completed':
            raise RuntimeError('监督分析失败：'+result['status'])
        return load(Path(self.metadata['answer']))


def prepare(directory,snapshot,*,lane,attempt,session_id,timeout,executable='codex'):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    identifier=snapshot['request']['decision_id']
    stem=directory/(lane+'-'+identifier+'-attempt'+str(attempt))
    def artifact(suffix):return Path(str(stem)+suffix)
    request=artifact('.request.json');answer=artifact('.answer.json');spec=artifact('.spec.json')
    python,_=runtime()
    atomic_json(request,{'snapshot':snapshot,'lane':lane,'session_id':session_id,
        'directory':str(directory),'timeout':timeout,'answer':str(answer),'executable':executable})
    atomic_json(spec,{'job':{'key':'ai-'+identifier+'-'+str(attempt),'stage':'ai',
        'command':[python,'-m','codesign_lab.evaluation.analysis',str(request)]},
        'cwd':str(ROOT),'result':str(artifact('.result.json')),'timeout':timeout+30})
    return {'spec':str(spec),'answer':str(answer),'decision_id':identifier,'attempt':attempt}
