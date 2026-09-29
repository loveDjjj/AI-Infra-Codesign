"""冻结实现族能力清单；代表种子与后续有限搜索域分别校验。"""
import copy
import hashlib
import json
from pathlib import Path
import re
import math

from ..config import load


def _value(config, path):
    node = config
    for part in path.split('.'):
        if not isinstance(node, dict) or part not in node:
            raise ValueError('能力变量不是生成器配置字段：' + path)
        node = node[part]
    return node


def _set(config, path, value):
    node = config
    parts = path.split('.')
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value


def validate_manifest(manifest, *, base_config, source_sha256, case, base_record_id):
    """校验有限合法域及冻结来源；不信任 AI 自述的代码能力。"""
    required = {'schema_version', 'family_id', 'mechanism_id', 'case', 'operator',
                'base_record_id', 'registered_source_sha256', 'variables',
                'seed_variants', 'critical_checks', 'stop_if'}
    optional = {'candidate_budget', 'expansion_gain', 'sampler'}
    if not isinstance(manifest, dict) or set(manifest) - optional != required or manifest['schema_version'] != 1:
        raise ValueError('实现族能力清单字段无效')
    for field in ('family_id', 'mechanism_id'):
        if not isinstance(manifest[field], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', manifest[field]):
            raise ValueError(field + ' 无效')
    if manifest['case'] != case or manifest['base_record_id'] != base_record_id or \
            manifest['registered_source_sha256'] != source_sha256:
        raise ValueError('实现族案例、基线或冻结源码身份不匹配')
    if not isinstance(manifest['operator'], str) or not manifest['operator'] or \
            not isinstance(manifest['stop_if'], str) or not manifest['stop_if']:
        raise ValueError('实现族缺少算子或停止条件')
    checks = manifest['critical_checks']
    if not isinstance(checks, list) or not checks or any(not isinstance(x, str) or not x for x in checks):
        raise ValueError('实现族缺少有效的静态/功能检查说明')
    variables = manifest['variables']
    prefix = 'programs.' + case + '.config.'
    if not isinstance(variables, dict) or not 1 <= len(variables) <= 8:
        raise ValueError('实现族必须声明 1–8 个真实变量')
    for path, domain in variables.items():
        allowed = isinstance(path, str) and (path.startswith(prefix) if case != 'both' else
                   path.startswith(('hardware.', 'programs.M1_P1.config.',
                                     'programs.M2_D1.config.')))
        if not isinstance(path, str) or not allowed or \
                not re.fullmatch(r'[A-Za-z0-9_.]+', path):
            raise ValueError('实现族变量仅能作用于受影响案例配置')
        current = _value(base_config, path)
        if not isinstance(domain, list) or not 1 <= len(domain) <= 8 or \
                any(type(v) not in (bool, int, str) or type(v) is not type(current) for v in domain):
            raise ValueError('实现族变量域类型或大小无效：' + path)
        if len({json.dumps(v, sort_keys=True) for v in domain}) != len(domain):
            raise ValueError('实现族变量域含重复值')
    variants = manifest['seed_variants']
    if not isinstance(variants, list) or not 1 <= len(variants) <= 6:
        raise ValueError('实现族应交付 1–6 个有限种子')
    seen = set()
    for variant in variants:
        if not isinstance(variant, dict) or set(variant) != set(variables):
            raise ValueError('种子必须完整指定已声明变量')
        for path, value in variant.items():
            if value not in variables[path] or type(value) is not type(_value(base_config, path)):
                raise ValueError('种子超出已声明合法域：' + path)
        config = copy.deepcopy(base_config)
        for path, value in variant.items():
            _set(config, path, value)
        if config == base_config:
            raise ValueError('种子与基线完全相同')
        identity = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        if identity in seen:
            raise ValueError('种子配置重复')
        seen.add(identity)
    budget = manifest.get('candidate_budget', len(variants))
    if type(budget) is not int or not len(variants) <= budget <= 32 or \
            budget > math.prod(len(domain) for domain in variables.values()) - int(all(
                _value(base_config, path) in domain for path, domain in variables.items())):
        raise ValueError('实现族候选预算必须位于种子数与合法离散空间之间，且不超过32')
    gain = manifest.get('expansion_gain', 0.002)
    if type(gain) not in (int, float) or not math.isfinite(gain) or not 0 <= gain <= 0.2:
        raise ValueError('实现族扩展门槛无效')
    if manifest.get('sampler', 'enumerate') not in {'enumerate', 'random'}:
        raise ValueError('实现族仅支持确定性枚举或随机扩展')
    return copy.deepcopy(manifest)


def load_manifest(source_root, relative_path, *, base_config, source_sha256, case, base_record_id):
    """清单必须位于冻结源码工作区，禁止外部路径或符号链接跳转。"""
    root = Path(source_root).resolve()
    path = (root / relative_path).resolve()
    if not path.is_relative_to(root / 'workspace/implementation-input') or not path.is_file():
        raise ValueError('能力清单不在实现族输入目录')
    return validate_manifest(load(path), base_config=base_config,
        source_sha256=source_sha256, case=case, base_record_id=base_record_id)


def configurations(base_config, variants):
    """按清单顺序生成精确种子，绝不构造未经声明的笛卡尔积。"""
    for variant in variants:
        config = copy.deepcopy(base_config)
        for path, value in variant.items():
            _set(config, path, value)
        identity = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        yield identity, config


def expansion_signal(*, base_cycles, global_cycles, pilot_cycles, threshold):
    """只在接近或超过全局单案最佳时给族内扩展预算。"""
    valid = [value for value in pilot_cycles if type(value) is int and value > 0]
    if not valid:
        return False
    best = min(valid)
    if type(global_cycles) is int and global_cycles > 0:
        if best <= global_cycles * (1 - threshold):
            return True
        return (type(base_cycles) is int and base_cycles > 0 and
                best <= base_cycles * (1 - max(threshold, 0.01)) and
                best <= global_cycles * 1.02)
    return type(base_cycles) is int and base_cycles > 0 and best <= base_cycles * (1 - threshold)
