import json,time
from ..config import ROOT,load
from .space import candidates
from .prune import reject
def prepare(path):
    start=time.monotonic();spec=load(path);base=load(ROOT/spec['base']);accepted=[];reasons={}
    for key,config in candidates(base,spec['variables'],spec['max_candidates']):
        reason=reject(config)
        if reason:reasons[reason]=reasons.get(reason,0)+1;continue
        directory=ROOT/'workspace/search/candidates';directory.mkdir(parents=True,exist_ok=True)
        target=directory/(key+'.yaml');target.write_text(json.dumps(config,indent=2)+'\n');accepted.append(str(target))
    summary={'accepted':accepted,'pruned':reasons,'elapsed_seconds':time.monotonic()-start,'evaluation_calls':0,'scope':'candidate preparation only; run explicitly after review'}
    (ROOT/'workspace/search/summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    return summary
