'按真实内容缓存探索评估结果，最终官方评测不使用缓存。'
from __future__ import annotations
import hashlib
import fcntl
import json
import os
from pathlib import Path
import platform
import tempfile
import errno
import time
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
    def __init__(self, directory, engine, *, lock_timeout=3600):
        self.directory = Path(directory)
        self.engine = engine
        if lock_timeout <= 0:
            raise ValueError("缓存锁等待上限必须为正")
        self.lock_timeout = lock_timeout

    def call(self, stage, identity, compute):
        key = sha(canonical({'engine': self.engine, 'stage': stage, 'identity': identity}))
        path = self.directory / stage / (key + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        # 锁在计算前获取，等待者取得锁后重新读取结果；进程退出自动释放。
        with path.with_suffix('.lock').open('a') as lock:
            started = time.monotonic()
            delay = .02
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EINTR):
                        raise
                    if time.monotonic() - started >= self.lock_timeout:
                        raise TimeoutError(f"缓存锁等待超时：{stage}/{key}") from exc
                    time.sleep(min(delay, max(0, self.lock_timeout - (time.monotonic() - started))))
                    delay = min(delay * 1.5, .5)
            return self._call_locked(stage, identity, compute, key, path)

    def _call_locked(self, stage, identity, compute, key, path):
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
