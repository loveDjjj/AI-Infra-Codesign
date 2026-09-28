"""由统一主控调用冻结实现族的生成器，产物仍进入主控工作区。"""
import argparse
import json
from pathlib import Path

from ..config import digest, load
from .pair_verify import ARTIFACTS, build_at, source_identity


def build_family(source: Path, config: Path, output: Path, expected_source: Path):
    source = source.resolve()
    if source_identity(source) != load(expected_source):
        raise ValueError('冻结实现族源码身份已改变')
    build_at(source, config.resolve(), output.resolve(), None)
    if source_identity(source) != load(expected_source):
        raise ValueError('冻结实现族在构建期间发生变化')
    return {'directory': str(output), 'sha256':
            {name: digest(output / name) for name in ARTIFACTS}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('config', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--source-identity', type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(build_family(args.source, args.config, args.out, args.source_identity)))


if __name__ == '__main__':
    main()
