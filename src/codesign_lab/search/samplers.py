"""有限离散空间的确定性选点；采样不替代合法性与产物去重。"""
import copy
import hashlib
import json
import math
import random


def target_seed(target):
    """显式种子优先；旧目标从固定身份派生，恢复不依赖进程随机状态。"""
    if 'seed' in target:
        return target['seed']
    identity = [target['source_epoch'], target['target_id'], target['base_record']]
    return int(hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:16], 16)


def candidates(base, variables, limit, *, sampler='enumerate', seed=0):
    """按稳定前缀生成不重复的参数点，空间内存开销与抽样数成正比。"""
    if sampler not in {'enumerate', 'random'}:
        raise ValueError('不支持的采样器')
    if type(limit) is not int or limit < 0:
        raise ValueError('候选数量必须为非负整数')
    # 枚举沿用历史变量顺序，Random 规范化变量顺序以消除 JSON 字段顺序影响。
    paths = list(variables) if sampler == 'enumerate' else sorted(variables)
    domains = [variables[path] for path in paths]
    size = math.prod(len(domain) for domain in domains)
    rng = random.Random(seed)
    swaps = {}
    seen = set()
    for offset in range(min(limit, size)):
        if sampler == 'random':
            selected = rng.randrange(offset, size)
            index = swaps.get(selected, selected)
            swaps[selected] = swaps.get(offset, offset)
            swaps.pop(offset, None)
        else:
            index = offset
        values = []
        for domain in reversed(domains):
            index, digit = divmod(index, len(domain))
            values.append(domain[digit])
        config = copy.deepcopy(base)
        for path, value in zip(paths, reversed(values)):
            node = config
            parts = path.split('.')
            for part in parts[:-1]:
                node = node[part]
            node[parts[-1]] = value
        identity = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        if identity not in seen:
            seen.add(identity)
            yield identity, config


class FiniteSampler:
    """可恢复的枚举/随机 ask-tell；记录真实反馈，不伪称自适应选点。"""
    def __init__(self, base, variables, *, sampler='enumerate', seed=0, state=None):
        self.base=copy.deepcopy(base);self.variables=copy.deepcopy(variables)
        self.sampler=sampler;self.seed=seed
        identity=hashlib.sha256(json.dumps([base,variables,sampler,seed],sort_keys=True).encode()).hexdigest()
        self.state=state if state is not None else {}
        if self.state and self.state.get('identity')!=identity:
            raise ValueError('采样器恢复空间或种子身份不一致')
        self.state.setdefault('identity',identity)
        self.state.setdefault('cursor',0)
        self.state.setdefault('asked',{})
        self.state.setdefault('receipts',{})
        self.state.setdefault('observations',[])

    def remember(self, identifier, config):
        """登记主控已选出的参数点，恢复不会另计一次提案。"""
        params={}
        for path,domain in self.variables.items():
            node=config
            for part in path.split('.'):node=node[part]
            if node not in domain:raise ValueError('反馈参数点超出搜索域')
            params[path]=copy.deepcopy(node)
        prior=self.state['asked'].get(identifier)
        if prior is not None and prior!=params:raise ValueError('同候选身份对应不同参数')
        self.state['asked'][identifier]=params

    def ask(self):
        """空间耗尽返回 None；不重复返回已在途或已结束的点。"""
        size=math.prod(len(domain) for domain in self.variables.values())
        for position,(identifier,config) in enumerate(candidates(self.base,self.variables,size,
                sampler=self.sampler,seed=self.seed)):
            if position<self.state['cursor']:continue
            self.state['cursor']=position+1
            if identifier in self.state['asked']:continue
            self.remember(identifier,config)
            return identifier,config
        return None

    def tell(self, identifier, observation):
        """失败保留类别，超功耗保留真实数值，缓存及基础设施错误不训练。"""
        if identifier not in self.state['asked']:raise ValueError('不能反馈未提出的候选')
        record_id=observation.get('id')
        if not isinstance(record_id,str) or not record_id:raise ValueError('反馈缺少观测身份')
        if record_id in self.state['receipts']:return self.state['receipts'][record_id]
        cycles,power=observation.get('cycles'),observation.get('peak_power_w')
        if observation.get('cache_reused'):
            status='reused'
        elif observation.get('failure_kind')=='infrastructure':
            status='infrastructure_failed'
        elif observation.get('functional_passed') is False:
            status='functional_failed'
        elif observation.get('functional_passed') is not True:
            status='no_performance'
        elif (type(cycles) not in (int,float) or not math.isfinite(cycles) or cycles<=0
              or type(power) not in (int,float) or not math.isfinite(power) or power<0
              or not observation.get('hardware_hash') or observation.get('case') not in {'M1_P1','M2_D1'}):
            status='no_performance'
        else:
            status='eligible' if power<=20 else 'power_failed'
            self.state['observations'].append({'id':record_id,'candidate_id':identifier,
                'case':observation['case'],'hardware_hash':observation['hardware_hash'],
                'cycles':cycles,'peak_power_w':power,'status':status})
        self.state['receipts'][record_id]=status
        return status
