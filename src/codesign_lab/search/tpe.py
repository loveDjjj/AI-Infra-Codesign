"""可选 Optuna 后端；仅在隔离搜索环境中运行，不导入评估进程。"""
import hashlib
import json
import math
import re
from pathlib import Path
from .locks import acquire


class OptunaSampler:
    """同硬件同案例的持久 ask/tell；约束采样不能替代项目合法性检查。"""
    def __init__(self,directory,variables,scope,*,seed=0,startup_trials=12,algorithm='tpe'):
        import optuna
        import numpy
        optuna.logging.set_verbosity(optuna.logging.WARNING)
        if optuna.__version__!='4.3.0' or numpy.__version__!='2.2.6':
            raise ValueError('搜索后端依赖版本不符合锁定环境')
        if algorithm not in {'tpe','random'} or type(seed) is not int or startup_trials<1:
            raise ValueError('采样后端设置无效')
        if set(scope)!={'source_epoch','hardware_hash','case'} or scope['case'] not in {'M1_P1','M2_D1'}:
            raise ValueError('TPE 需要固定源码、硬件与单案例身份')
        if not variables or any(not values or len(set(values))!=len(values) for values in variables.values()):
            raise ValueError('离散参数域为空或重复')
        self.optuna=optuna;self.directory=Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self.variables=variables;self.scope=scope;self.seed=seed
        self.startup=startup_trials;self.algorithm=algorithm
        self.identity={'variables':variables,'scope':scope,'seed':seed,'startup_trials':startup_trials,
                       'algorithm':algorithm,'optuna':'4.3.0','numpy':'2.2.6'}
        self.storage='sqlite:///'+str((self.directory/'trials.sqlite3').resolve())
        with self.lock():
            study=self.study(0)
            if study.user_attrs.get('identity',self.identity)!=self.identity:
                raise ValueError('恢复后端的搜索身份改变')
            study.set_user_attr('identity',self.identity)

    def lock(self):
        from contextlib import contextmanager
        @contextmanager
        def locked():
            with (self.directory/'backend.lock').open('a') as handle:
                acquire(handle)
                yield
        return locked()

    def study(self,counter):
        # 每次 ask 的确定种子由持久计数派生，不恢复未经验证的 pickle/私有 RNG。
        seed=int(hashlib.sha256(f'{self.seed}:{counter}'.encode()).hexdigest()[:8],16)
        if self.algorithm=='tpe':
            sampler=self.optuna.samplers.TPESampler(seed=seed,n_startup_trials=self.startup,
                constant_liar=True,constraints_func=lambda trial:trial.user_attrs.get('constraints',[1.0]))
        else:sampler=self.optuna.samplers.RandomSampler(seed=seed)
        return self.optuna.create_study(storage=self.storage,study_name='target',direction='minimize',
                                       sampler=sampler,load_if_exists=True)

    def pending(self):
        with self.lock():
            study=self.study(0)
            return [{'number':trial.number,'params':trial.params,'candidate_id':trial.user_attrs.get('candidate_id')}
                    for trial in study.get_trials() if trial.state==self.optuna.trial.TrialState.RUNNING]

    def finish_ask(self,study,request_id,proposal):
        if proposal is not None:
            trial=next(t for t in study.get_trials() if t.number==proposal['number'])
            proposal['completed_observations_at_ask']=trial.user_attrs.get('completed_observations_at_ask')
        """回答先持久化，随后清除意图，重放不新建 trial。"""
        if request_id is not None:
            receipts=study.user_attrs.get('ask_receipts',{})
            receipts[request_id]=proposal
            study.set_user_attr('ask_receipts',receipts)
            study.set_user_attr('pending_ask',None)
        return proposal

    def ask(self,request_id=None):
        with self.lock():
            study=self.study(0);trials=study.get_trials()
            if request_id is not None:
                import re
                if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}',request_id):
                    raise ValueError('提案请求 ID 无效')
                receipts=study.user_attrs.get('ask_receipts',{})
                if request_id in receipts:
                    pending=study.user_attrs.get('pending_ask')
                    if pending and pending['request_id']==request_id:study.set_user_attr('pending_ask',None)
                    return receipts[request_id]
            intent=study.user_attrs.get('pending_ask')
            if intent:
                if request_id!=intent['request_id']:raise RuntimeError('另一个提案意图未恢复，禁止追加')
                created=[trial for trial in trials if trial.number>=intent['first_number'] and trial.state==self.optuna.trial.TrialState.RUNNING]
                if len(created)>1:raise RuntimeError('提案意图对应多个在途 trial，拒绝猜测')
                if created:
                    trial=created[0]
                    if set(trial.params)!=set(self.variables) or any(trial.params[path] not in values for path,values in self.variables.items()):
                        raise RuntimeError('提案尚未完整保存，需核对')
                    identity=hashlib.sha256(json.dumps(trial.params,sort_keys=True).encode()).hexdigest()
                    if any(other.number!=trial.number and other.user_attrs.get('candidate_id')==identity for other in trials):
                        # 退出可能发生在建议生成后、去重之前，不能恢复成第二次评估。
                        study.tell(trial.number,state=self.optuna.trial.TrialState.FAIL)
                        trials=study.get_trials()
                    else:
                        live=self.optuna.trial.Trial(study,trial._trial_id)
                        live.set_user_attr('candidate_id',identity)
                        return self.finish_ask(study,request_id,{'number':trial.number,'params':trial.params,
                            'candidate_id':identity,'proposal_origin':trial.user_attrs.get('proposal_origin',self.algorithm)})
            known={trial.user_attrs['candidate_id'] for trial in trials if 'candidate_id' in trial.user_attrs}
            if any(trial.state==self.optuna.trial.TrialState.RUNNING and 'candidate_id' not in trial.user_attrs for trial in trials):
                raise RuntimeError('存在身份未登记的在途 trial，需核对，不盲目追加')
            if len(known)>=math.prod(len(values) for values in self.variables.values()):return self.finish_ask(study,request_id,None)
            if request_id is not None and not intent:
                study.set_user_attr('pending_ask',{'request_id':request_id,'first_number':len(trials)})
            distributions={path:self.optuna.distributions.CategoricalDistribution(values)
                           for path,values in sorted(self.variables.items())}
            for _ in range(8):
                counter=study.user_attrs.get('ask_counter',0)
                study=self.study(counter);study.set_user_attr('ask_counter',counter+1)
                trial=study.ask(distributions)
                identity=hashlib.sha256(json.dumps(trial.params,sort_keys=True).encode()).hexdigest()
                if identity in known:
                    study.tell(trial.number,state=self.optuna.trial.TrialState.FAIL)
                    continue
                trial.set_user_attr('completed_observations_at_ask',sum(t.state==self.optuna.trial.TrialState.COMPLETE for t in trials))
                trial.set_user_attr('candidate_id',identity)
                return self.finish_ask(study,request_id,{'number':trial.number,'params':trial.params,'candidate_id':identity,'proposal_origin':self.algorithm})
            # 有限空间去重可能使 TPE 始终推荐同一点；明确记录枚举回退。
            import itertools
            paths=sorted(self.variables)
            for values in itertools.product(*(self.variables[path] for path in paths)):
                params=dict(zip(paths,values))
                identity=hashlib.sha256(json.dumps(params,sort_keys=True).encode()).hexdigest()
                if identity in known:continue
                study.enqueue_trial(params,user_attrs={'proposal_origin':'enumeration_fallback'})
                trial=study.ask(distributions)
                trial.set_user_attr('completed_observations_at_ask',sum(t.state==self.optuna.trial.TrialState.COMPLETE for t in trials))
                trial.set_user_attr('candidate_id',identity)
                return self.finish_ask(study,request_id,{'number':trial.number,'params':trial.params,'candidate_id':identity,
                        'proposal_origin':'enumeration_fallback'})
            return self.finish_ask(study,request_id,None)

    def import_observation(self,params,observation):
        """导入上游已校验的历史事实，单次事务保存身份与值，重放不增样本。"""
        if set(params)!=set(self.variables) or any(type(params[path]) is not int or params[path] not in values for path,values in self.variables.items()):
            raise ValueError('先验参数超出搜索域')
        if observation.get('evidence_kind')!='historical_prior' or not isinstance(observation.get('report_sha256'),str) or not re.fullmatch(r'[0-9a-f]{64}',observation['report_sha256']):
            raise ValueError('先验缺少原件身份')
        if any(observation.get(field)!=self.scope[field] for field in ['case','hardware_hash']):raise ValueError('先验作用域不一致')
        cycles,power=observation.get('cycles'),observation.get('peak_power_w')
        if not isinstance(observation.get('id'),str) or not observation['id'] or observation.get('functional_passed') is not True or observation.get('cache_reused') or observation.get('failure_kind'):
            raise ValueError('先验不是有效原始观测')
        if any(type(value) not in (int,float) or not math.isfinite(value) for value in [cycles,power]) or cycles<=0 or power<0:
            raise ValueError('先验性能无效')
        identity=hashlib.sha256(json.dumps(params,sort_keys=True).encode()).hexdigest()
        with self.lock():
            study=self.study(0)
            for trial in study.get_trials():
                receipt=trial.user_attrs.get('receipt',{})
                if receipt.get('id')==observation['id']:
                    if receipt!=observation or trial.params!=params:raise ValueError('相同先验事实身份内容改变')
                    return {'number':trial.number,'status':'reused_prior'}
                if trial.user_attrs.get('candidate_id')==identity:raise ValueError('参数点已登记，不能重复导入先验')
            trial=self.optuna.trial.create_trial(params=params,
                distributions={path:self.optuna.distributions.CategoricalDistribution(values) for path,values in self.variables.items()},
                value=cycles,user_attrs={'candidate_id':identity,'receipt':observation,'historical_prior':True,
                                         'proposal_origin':'historical_prior','constraints':[power-20.0]})
            study.add_trial(trial)
            return {'number':study.get_trials()[-1].number,'status':'imported_prior'}

    def tell(self,number,observation):
        with self.lock():
            study=self.study(0);trial=next((t for t in study.get_trials() if t.number==number),None)
            if trial is None:raise ValueError('反馈引用未知 trial')
            if trial.state!=self.optuna.trial.TrialState.RUNNING:
                if trial.user_attrs.get('receipt')==observation:return 'reused_receipt'
                raise ValueError('已结束 trial 收到不同反馈')
            if any(observation.get(field)!=self.scope[field] for field in ['case','hardware_hash']):
                raise ValueError('反馈硬件或案例与后端身份不一致')
            if not isinstance(observation.get('id'),str) or not observation['id']:raise ValueError('反馈缺少事实 ID')
            # 缓存命中可提供当前 study 尚未学习的真实结果；重复证据仍只计一次。
            cycles,power=observation.get('cycles'),observation.get('peak_power_w')
            valid=(observation.get('functional_passed') is True
                and observation.get('failure_kind')!='infrastructure' and type(cycles) in (int,float)
                and math.isfinite(cycles) and cycles>0 and type(power) in (int,float) and math.isfinite(power) and power>=0)
            seen=study.user_attrs.get('seen_evidence',{})
            for previous in study.get_trials():
                receipt=previous.user_attrs.get('receipt',{})
                if previous.user_attrs.get('historical_prior') and receipt.get('id')==observation['id'] and previous.number!=number:valid=False
            if valid and observation['id'] in seen and seen[observation['id']]!=number:
                valid=False
            live=self.optuna.trial.Trial(study,trial._trial_id)
            live.set_user_attr('receipt',observation)
            if not valid:
                study.tell(number,state=self.optuna.trial.TrialState.FAIL)
                return 'no_performance'
            seen[observation['id']]=number
            study.set_user_attr('seen_evidence',seen)
            live.set_user_attr('constraints',[power-20.0])
            study.tell(number,cycles)
            return 'eligible' if power<=20 else 'power_failed'


def main():
    """只接收本地结构化请求，不执行请求中的命令或源码。"""
    import argparse
    from ..config import ROOT,load
    from .scheduler import atomic_json
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('request',type=Path)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    for path in [args.request.resolve(),args.out.resolve()]:
        if not path.is_relative_to(ROOT/'workspace'):raise ValueError('后端请求/回答必须位于 workspace')
    request=load(args.request)
    required={'directory','variables','scope','seed','startup_trials','operation','request_id'}
    extra={'tell':{'number','observation'},'ask':set(),'pending':set(),'import':{'params','observation'}}
    operation=request.get('operation')
    if operation not in extra or set(request)!=required|extra[operation]:raise ValueError('后端请求字段无效')
    directory=Path(request['directory']).resolve()
    if not directory.is_relative_to(ROOT/'workspace'):raise ValueError('后端状态必须位于 workspace')
    identity=hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()
    if args.out.exists():
        if load(args.out).get('request_sha256')!=identity:raise ValueError('回答属于另一个请求')
        return
    backend=OptunaSampler(directory,request['variables'],request['scope'],seed=request['seed'],startup_trials=request['startup_trials'])
    if operation=='ask':answer=backend.ask(request['request_id'])
    elif operation=='tell':answer=backend.tell(request['number'],request['observation'])
    elif operation=='import':answer=backend.import_observation(request['params'],request['observation'])
    else:answer=backend.pending()
    atomic_json(args.out,{'request_sha256':identity,'operation':operation,'answer':answer,'optuna_version':'4.3.0'})


if __name__=='__main__':main()
