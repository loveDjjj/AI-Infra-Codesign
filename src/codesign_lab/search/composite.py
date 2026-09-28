"""保存跨源码组合的两个生成器，并从独立快照逐字节再生。"""
import json
import shutil
import tarfile
from pathlib import Path

from ..config import ROOT, digest, load
from .pair_verify import ARTIFACTS, CASES, build_at, source_identity


def snapshot(candidate: Path, destination: Path):
    selection = load(candidate / 'selection.json')
    parents = selection['parents']
    destination.mkdir(parents=True, exist_ok=True)
    manifest = {'kind': 'composite', 'artifact_sha256':
                {name: digest(candidate / name) for name in ARTIFACTS}, 'parents': {}}
    for case in CASES:
        parent = parents[case]
        source = Path(parent['source_root']).resolve()
        if source_identity(source) != parent['source_sha256']:
            raise ValueError('组合来源源码已改变')
        archive = destination / (case + '-source.tar.gz')
        with tarfile.open(archive, 'w:gz') as output:
            for path in sorted((source / 'src').rglob('*.py')):
                output.add(path, arcname=str(path.relative_to(source)), recursive=False)
        config = destination / (case + '-config.json')
        config_source = Path(parent.get('config_path', Path(parent['candidate']) / 'config.json'))
        if parent.get('config_sha256') and digest(config_source) != parent['config_sha256']:
            raise ValueError('组合来源配置已改变')
        shutil.copy2(config_source, config)
        manifest['parents'][case] = {'source_archive': archive.name,
                                     'source_archive_sha256': digest(archive),
                                     'source_sha256': parent['source_sha256'],
                                     'config': config.name, 'config_sha256': digest(config)}
    (destination / 'composite.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def reproduce(snapshot_dir: Path, expected: Path, scratch: Path):
    """快照只包含源码和配置；官方工具链由当前冻结包提供。"""
    snapshot_dir, expected, scratch = snapshot_dir.resolve(), expected.resolve(), scratch.resolve()
    manifest = load(snapshot_dir / 'composite.json')
    if manifest.get('kind') != 'composite':
        raise ValueError('无效组合快照')
    if {name: digest(expected / name) for name in ARTIFACTS} != manifest['artifact_sha256']:
        raise ValueError('组合快照目标产物不匹配')
    for case in CASES:
        item = manifest['parents'][case]
        archive = snapshot_dir / item['source_archive']
        config = snapshot_dir / item['config']
        if digest(archive) != item['source_archive_sha256'] or digest(config) != item['config_sha256']:
            raise ValueError('组合快照文件已改变')
        source = materialize_source(snapshot_dir, case, scratch / case)
        output = scratch / (case + '-built')
        build_at(source, config, output, None)
        if digest(output / 'hardware.json') != manifest['artifact_sha256']['hardware.json'] or \
                digest(output / 'programs' / (case + '.asm')) != manifest['artifact_sha256']['programs/' + case + '.asm']:
            raise ValueError('组合快照再生字节不匹配')
    return {'verified': True, 'artifact_sha256': manifest['artifact_sha256']}


def materialize_source(snapshot_dir: Path, case: str, source: Path):
    """为后续搜索恢复一个冻结生成器；重复调用只校验，不覆盖源码。"""
    if case not in CASES:
        raise ValueError('未知组合案例')
    snapshot_dir, source = snapshot_dir.resolve(), source.resolve()
    item = load(snapshot_dir / 'composite.json')['parents'][case]
    archive = snapshot_dir / item['source_archive']
    if digest(archive) != item['source_archive_sha256']:
        raise ValueError('组合源码归档哈希不匹配')
    if not source.exists():
        source.mkdir(parents=True)
        with tarfile.open(archive, 'r:gz') as stream:
            for member in stream.getmembers():
                path = Path(member.name)
                if path.is_absolute() or '..' in path.parts or not member.isfile() or path.parts[0] != 'src':
                    raise ValueError('组合源码归档路径无效')
                stream.extract(member, source, filter='data')
        (source / 'configs').mkdir()
        shutil.copy2(ROOT / 'configs/toolchain.yaml', source / 'configs/toolchain.yaml')
        (source / 'vendor').mkdir()
        (source / 'vendor/official').symlink_to(ROOT / 'vendor/official', target_is_directory=True)
    if source_identity(source) != item['source_sha256'] or \
            digest(source / 'vendor/official/isolation-manifest.json') != digest(ROOT / 'vendor/official/isolation-manifest.json'):
        raise ValueError('恢复的组合来源与冻结快照不一致')
    return source
