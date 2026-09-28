"""在正式整案前按各自冻结生成器逐字节再生 P1/D1 组合。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from ..config import ROOT, digest, load
from .scheduler import atomic_json

CASES = ('M1_P1', 'M2_D1')
ARTIFACTS = ('hardware.json', 'programs/M1_P1.asm', 'programs/M2_D1.asm')


def generator_identity(root: Path):
    return {str(path.relative_to(root)): digest(path)
            for path in sorted((root / 'src/codesign_lab/codegen').rglob('*.py'))}


def build_at(source: Path, config: Path, output: Path, expected: Path):
    environment = os.environ.copy()
    environment.update(PYTHONPATH=str(source / 'src'), OPENBLAS_NUM_THREADS='1',
                       OMP_NUM_THREADS='1', PYTHONDONTWRITEBYTECODE='1')
    command = [sys.executable, '-m', 'codesign_lab.cli', 'build', str(config),
               '--out', str(output), '--verify', str(expected)]
    result = subprocess.run(command, cwd=source, env=environment,
                            capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise ValueError('来源生成器逐字节再生失败：' + result.stderr[-1200:])


def verify_pair(candidate: Path, output: Path):
    candidate, output = candidate.resolve(), output.resolve()
    if not candidate.is_relative_to(ROOT / 'workspace') or not output.is_relative_to(ROOT / 'workspace'):
        raise ValueError('组合验证只接受流水线工作区路径')
    selection = load(candidate / 'selection.json')
    parents = selection['parents']
    if set(parents) != set(CASES):
        raise ValueError('组合必须有 P1 和 D1 两份来源')
    expected_final = {name: digest(candidate / name) for name in ARTIFACTS}
    official_hash = digest(ROOT / 'vendor/official/isolation-manifest.json')
    sources = {}
    for case in CASES:
        item = parents[case]
        source = Path(item['source_root']).resolve()
        parent = Path(item['candidate']).resolve()
        if not (source / 'src/codesign_lab/codegen').is_dir() or not parent.is_dir():
            raise ValueError('组合来源源码或候选不存在')
        if generator_identity(source) != item['generator_sha256']:
            raise ValueError('组合来源生成器身份已改变')
        if digest(source / 'vendor/official/isolation-manifest.json') != official_hash:
            raise ValueError('组合来源使用不同官方工作负载')
        if digest(parent / 'hardware.json') != expected_final['hardware.json'] or \
                digest(parent / 'programs' / (case + '.asm')) != expected_final['programs/' + case + '.asm']:
            raise ValueError('组合产物与来源案例不一致')
        sources[case] = {'root': source, 'candidate': parent}
    evidence = output / 'pair-verify.json'
    if evidence.is_file():
        previous = load(evidence)
        if previous.get('artifact_sha256') != expected_final or previous.get('parents') != parents:
            raise ValueError('已有组合验证证据与当前输入不一致')
        return previous
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    same_source = sources['M1_P1']['root'] == sources['M2_D1']['root']
    if same_source:
        build_at(sources['M1_P1']['root'], candidate / 'config.json',
                 output / 'combined', candidate)
    else:
        for case in CASES:
            item = sources[case]
            rebuilt = output / case
            build_at(item['root'], item['candidate'] / 'config.json', rebuilt, item['candidate'])
            if digest(rebuilt / 'hardware.json') != expected_final['hardware.json'] or \
                    digest(rebuilt / 'programs' / (case + '.asm')) != expected_final['programs/' + case + '.asm']:
                raise ValueError('来源再生与组合案例不一致')
    for case in CASES:
        if generator_identity(sources[case]['root']) != parents[case]['generator_sha256']:
            raise ValueError('验证期间来源生成器发生变化')
    result = {'verified': True, 'mode': 'single_source' if same_source else 'mixed_sources',
              'artifact_sha256': expected_final, 'parents': parents,
              'official_manifest_sha256': official_hash}
    atomic_json(evidence, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('candidate', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(verify_pair(args.candidate, args.out), ensure_ascii=False))


if __name__ == '__main__':
    main()
