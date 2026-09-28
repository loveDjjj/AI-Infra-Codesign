"""把已接受分析的阶段跟踪请求转为有预算的确定性任务。"""
from ..config import ROOT,load,resolve_reference,digest,bootstrap


def inputs(record,case):
    if case not in {'M1_P1','M2_D1'}:raise ValueError('profile 案例无效')
    if not record.get('report') or not record.get('config'):
        raise ValueError('profile 缺少可取回输入与配置')
    report=resolve_reference(record['report'])
    if record.get('candidate'):
        candidate=resolve_reference(record['candidate'])
    elif record.get('audited') and record.get('reproduction')=='verified' and report.is_relative_to(ROOT/'data/releases'):
        # 旧发布记录没有路径字段；只接受实际保全目录，随后核对原始报告身份。
        candidate=report.parent
    else:
        raise ValueError('profile 缺少可取回候选')
    if not candidate.is_dir() or not report.is_file():raise ValueError('profile 原件已缺失')
    data=load(report);metrics=data.get('cases',{}).get(case,{})
    if metrics.get('functional_passed') is not True or not metrics.get('timing'):
        raise ValueError('profile 需要功能通过且有真实时序的案例')
    if not (candidate/'hardware.json').is_file() or not (candidate/'programs'/f'{case}.asm').is_file():
        raise ValueError('profile 候选文件缺失')
    expected=data.get('program_sha256',data.get('provenance',{}).get('program_sha256',{})).get(case)
    if expected != digest(candidate/'programs'/f'{case}.asm'):
        raise ValueError('profile 程序字节与报告不匹配')
    if data.get('provenance'):
        bootstrap()
        from codesign.challenge.hardware import Hardware
        from codesign.challenge.runner import provenance
        actual=provenance(Hardware.from_dict(load(candidate/'hardware.json')),
            {name:(candidate/'programs'/f'{name}.asm').read_text() for name in ['M1_P1','M2_D1']})
        if actual!=data['provenance']:raise ValueError('profile 官方来源与候选不匹配')
    elif data.get('hardware')!=load(candidate/'hardware.json'):
        raise ValueError('profile 硬件与报告不匹配')
    return candidate,report
