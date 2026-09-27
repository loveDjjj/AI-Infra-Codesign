'使用未修改且通过哈希检查的官方评分器评测最终提交。'
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from ..config import ROOT, load
OFFICIAL = (ROOT / load(ROOT / 'configs/toolchain.yaml')['official_root']).resolve()

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def verify():
    manifest = json.loads((OFFICIAL / 'isolation-manifest.json').read_text())
    for name, expected in manifest['files_sha256'].items():
        if digest(OFFICIAL / name) != expected:
            raise RuntimeError(f'Isolated official file changed: {name}')
    return manifest

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('candidate', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    manifest = verify()
    candidate = args.candidate.resolve()
    inputs = [candidate / 'hardware.json', candidate / 'programs/M1_P1.asm', candidate / 'programs/M2_D1.asm']
    before = {str(path): digest(path) for path in inputs}
    report = (args.report or candidate / 'local-grade.json').resolve()
    command = [sys.executable, str(OFFICIAL / 'challenge.py'), 'grade',
               '--hardware', str(inputs[0]), '--program-p1', str(inputs[1]),
               '--program-d1', str(inputs[2]), '--baseline', str(OFFICIAL / 'baseline_manifest.json'),
               '--seed', '7', '--report', str(report)]
    if args.verify_only:
        print(json.dumps({'verified': True, 'official_root': str(OFFICIAL), 'command': command,
                          'input_sha256': before, 'baseline_sha256': manifest['baseline_sha256']}, indent=2))
        return 0
    if report.exists():
        raise FileExistsError(report)
    report.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for key in ('PYTHONPATH', 'PYTHONHOME'):
        env.pop(key, None)
    env.update(OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', PYTHONDONTWRITEBYTECODE='1')
    with report.with_suffix('.stdout.log').open('x') as stdout, report.with_suffix('.stderr.log').open('x') as stderr:
        result = subprocess.run(command, cwd=OFFICIAL, env=env, stdout=stdout, stderr=stderr)
    verify()
    if before != {str(path): digest(path) for path in inputs}:
        raise RuntimeError('Candidate inputs changed during final grading')
    audit = {'command': command, 'exit_code': result.returncode, 'input_sha256': before,
             'official_root': str(OFFICIAL), 'starter_sha256': manifest['starter_sha256'],
             'baseline_sha256': manifest['baseline_sha256'],
             'report_sha256': digest(report) if report.exists() else None}
    report.with_suffix('.isolated-run.json').write_text(json.dumps(audit, indent=2) + '\n')
    return result.returncode

if __name__ == '__main__':
    raise SystemExit(main())
