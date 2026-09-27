'按真实内容缓存探索评估结果，最终官方评测不使用缓存。'
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import platform
import tempfile
import numpy as np

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()

def sha(value):
    return hashlib.sha256(value).hexdigest()

def engine_identity(root):
    paths = [root / 'challenge.py'] + sorted((root / 'codesign').rglob('*.py')) + sorted((root / 'codesign').rglob('*.json'))
    return {'source_sha256': sha(canonical({str(p.relative_to(root)): sha(p.read_bytes()) for p in paths})),
            'python': platform.python_version(), 'numpy': np.__version__, 'schema': 1}

class ResultCache:
    def __init__(self, directory, engine):
        self.directory = Path(directory)
        self.engine = engine

    def call(self, stage, identity, compute):
        key = sha(canonical({'engine': self.engine, 'stage': stage, 'identity': identity}))
        path = self.directory / stage / (key + '.json')
        if path.exists():
            try:
                record = json.loads(path.read_text())
                if record['key'] == key and record['result_sha256'] == sha(canonical(record['result'])):
                    return record['result'], True
            except (ValueError, KeyError, TypeError):
                pass
        result = compute()
        record = {'key': key, 'stage': stage, 'engine': self.engine, 'identity': identity,
                  'result': result, 'result_sha256': sha(canonical(result))}
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(canonical(record))
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return result, False
