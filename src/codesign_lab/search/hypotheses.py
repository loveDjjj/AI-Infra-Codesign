"""人工审阅过的结构假设池；只保存机制和证据，不绕过隔离实现关卡。"""
import hashlib
import json
from pathlib import Path
import re

from ..config import ROOT, load
from ..records import read


def load_pool(path):
    path = Path(path).resolve()
    if not path.is_relative_to((ROOT/'configs').resolve()) or not path.is_file():
        raise ValueError('结构假设配置必须位于 configs')
    value = load(path)
    if not isinstance(value, dict) or set(value) != {'schema_version', 'hypotheses'} or value['schema_version'] != 1:
        raise ValueError('结构假设池格式无效')
    entries = value['hypotheses']
    if not isinstance(entries, list) or len(entries) > 8:
        raise ValueError('结构假设池最多八项')
    valid = {row['id'] for row in read()}
    seen = set()
    for item in entries:
        if not isinstance(item, dict) or set(item) - {'parent_record'} != {'transformation_id','lane','proposal','evidence_ids'}:
            raise ValueError('结构假设字段无效')
        if 'parent_record' in item and item['parent_record'] not in valid:
            raise ValueError('结构假设的父版记录不存在')
        identity = item['transformation_id']
        if not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',identity) or identity in seen:
            raise ValueError('结构机制 ID 无效或重复')
        seen.add(identity)
        if not isinstance(item['lane'],str) or not item['lane'].startswith(('p1','d1')) or \
                not isinstance(item['proposal'],str) or not 40 <= len(item['proposal']) <= 4000:
            raise ValueError('结构假设缺少受影响案例或可否证说明')
        evidence=item['evidence_ids']
        if not isinstance(evidence,list) or not evidence or any(x not in valid for x in evidence):
            raise ValueError('结构假设引用的本项目实验记录不存在')
    return entries, hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
