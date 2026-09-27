import importlib,json,functools,inspect,dataclasses
from pathlib import Path
from .config import ROOT,load,bootstrap,digest

def instrument(module):
    '收集生成指令范围，不修改输出汇编。'
    spans=[]; patches=[]
    cls=getattr(module,"ParallelBuilder",None) or module.SplitW2Builder
    names=['gemm','gemm_persistent_w1','gemm_persistent_w2','gemm_resident_qkv','gemm_resident_wo','gemm_decode_waves','gemm_partitioned','layernorm','gelu','attention','attention_decode','add_rows','add_bias','copy_rows']
    for name in names:
        if not hasattr(cls,name):continue
        original=getattr(cls,name); local=name in cls.__dict__
        def wrap(fn,operator):
            @functools.wraps(fn)
            def call(self,*args,**kwargs):
                start=len(self.lines); result=fn(self,*args,**kwargs)
                bound=inspect.signature(fn).bind(self,*args,**kwargs)
                spans.append({'operator':operator,'name':str(bound.arguments.get('name','')),'source_line_start':start+1,'source_line_end':len(self.lines),'basis':'generator call; instruction ranges may nest'})
                return result
            return call
        setattr(cls,name,wrap(original,name));patches.append((name,original,local))
    return spans,lambda:[setattr(cls,n,o) if local else delattr(cls,n) for n,o,local in patches]

def build(config,out,verify=None):
    bootstrap()
    from codesign.challenge.hardware import Hardware
    cfg=load(config);out=Path(out)
    if out.exists():raise FileExistsError(out)
    out.mkdir(parents=True);(out/'programs').mkdir()
    if cfg.get('kind')=='frozen-artifacts':
        import shutil
        source=ROOT/cfg['directory']
        shutil.copy2(source/'hardware.json',out/'hardware.json')
        for case in ['M1_P1','M2_D1']:shutil.copy2(source/'programs'/f'{case}.asm',out/'programs'/f'{case}.asm')
    else:
        hardware=Hardware.from_dict(cfg['hardware']); stages={}
        for case,package in [('M1_P1','p1'),('M2_D1','d1')]:
            c=importlib.import_module(f'codesign_lab.codegen.{package}.compiler')
            p=importlib.import_module(f'codesign_lab.codegen.{package}.parallel_compiler')
            setting=cfg['programs'][case];spans,restore=instrument(p)
            try:
                resolved=c.CompilerConfig(**setting['config'])
                cfg['programs'][case]['config']=dataclasses.asdict(resolved)
                options={'config':resolved,'schedule':setting.get('schedule','operator')}
                if setting.get('batch_epochs'):options['batch_epochs']=True
                if options['schedule']=='single':
                    fn=c.generate_m1_p1 if case=='M1_P1' else c.generate_m1_d1
                    asm=fn(config=options['config'])[0]
                else:asm=p.generate(case,setting.get('sm_count',hardware.sm_count),**options)[0]
            finally:restore()
            (out/'programs'/f'{case}.asm').write_text(asm);stages[case]=spans
        (out/'hardware.json').write_text(json.dumps(hardware.to_dict(),indent=2)+'\n')
        (out/'operator-map.json').write_text(json.dumps(stages,indent=2)+'\n')
    files=['hardware.json','programs/M1_P1.asm','programs/M2_D1.asm']
    hashes={name:digest(out/name) for name in files}
    if verify:
        for name in files:
            if digest(Path(verify)/name)!=hashes[name]:raise ValueError('Reproduction mismatch: '+name)
    (out/'config.json').write_text(json.dumps(cfg,indent=2)+'\n')
    (out/'build.json').write_text(json.dumps({'sha256':hashes,'source_sha256':{str(path.relative_to(ROOT)):digest(path) for path in sorted((ROOT/'src/codesign_lab/codegen').rglob('*.py'))},'verified_against':str(verify) if verify else None},indent=2)+'\n')
    return {'directory':str(out),'sha256':hashes,'verified':bool(verify)}
