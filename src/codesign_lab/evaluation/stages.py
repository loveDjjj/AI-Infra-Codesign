"""阶段依赖验证与报告合并；缺少功能证据时不允许释放时序任务。"""
import copy


def functional_evidence(report, case, hardware, program_hash, provenance, seeds):
    """核对输入、工具身份和所有种子，不从成功退出码推断正确性。"""
    if report.get('mode') not in {'functional', 'both'}:
        raise ValueError('依赖报告没有功能检查阶段')
    if report.get('hardware') != hardware or report.get('program_sha256', {}).get(case) != program_hash:
        raise ValueError('功能依赖输入身份不一致')
    # 逐案任务允许另一案 ASM 不同，官方源码/工作负载/硬件身份仍逐项相同。
    def case_provenance(value):
        if not isinstance(value, dict):
            return value
        normalized = copy.deepcopy(value)
        if 'program_sha256' in normalized:
            normalized['program_sha256'] = {case: normalized['program_sha256'].get(case)}
        return normalized
    if case_provenance(report.get('provenance')) != case_provenance(provenance):
        raise ValueError('功能依赖工具身份不一致')
    info = report.get('cases', {}).get(case, {})
    checks = info.get('functional', [])
    if (report.get('completed_without_error') is not True or info.get('error')
            or info.get('functional_passed') is not True
            or info.get('race_validation_passed') is not True):
        raise ValueError('功能依赖未完整通过')
    # runner 写入实际检查的种子身份；旧报告不能凭数量代替身份校验。
    if report.get('functional_seeds') != list(seeds) or len(checks) != len(seeds):
        raise ValueError('功能依赖种子身份缺失或不一致')
    if any(check.get('passed') is not True for check in checks):
        raise ValueError('功能依赖含失败种子')
    return copy.deepcopy(info)
