import itertools,copy,hashlib,json
def candidates(base,variables,limit):
    seen=set()
    for values in itertools.islice(itertools.product(*variables.values()),limit):
        candidate=copy.deepcopy(base)
        for path,value in zip(variables,values):
            node=candidate;parts=path.split('.')
            for part in parts[:-1]:node=node[part]
            node[parts[-1]]=value
        key=hashlib.sha256(json.dumps(candidate,sort_keys=True).encode()).hexdigest()
        if key not in seen:seen.add(key);yield key,candidate
