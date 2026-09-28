from .config import ROOT,load,digest,verify_official,reference,resolve_reference
import json
from .records import read,state

def clean():
    '只预览明确的清理清单，不遍历长期数据目录。'
    return [{'path':str(p),'bytes':p.stat().st_size,'reason':'temporary workspace artifact'} for p in (ROOT/'workspace').rglob('*') if p.is_file() and not p.is_symlink()]
def retain_release(record):
    '晋升前保存独立证据，使版本不依赖可丢弃的工作区文件。'
    from pathlib import Path
    import tempfile,shutil,tarfile,re
    from .build import build
    from .records import update
    report=resolve_reference(record['report'])
    if report.is_relative_to(ROOT/'data/releases'):return record
    identifier=record['id']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+',identifier) or identifier in ['.','..']:raise ValueError('Invalid release ID')
    target=ROOT/'data/releases'/identifier
    if target.exists():raise FileExistsError(target)
    grade=load(report);audit_path=resolve_reference(record['audit_path'])
    if load(audit_path).get('grade_sha256')!=digest(report):raise ValueError('Release report audit mismatch')
    composite=record.get('reproduction')=='verified_composite'
    with tempfile.TemporaryDirectory() as directory:
        temporary=Path(directory);config=temporary/'config.json';config.write_text(json.dumps(record['config']))
        if composite:
            from .search.composite import snapshot,reproduce
            source_candidate=resolve_reference(record['candidate'])
            shutil.copytree(source_candidate,target)
            snapshot(source_candidate,target)
            reproduce(target,target,temporary/'rebuild')
            candidate=target
        else:
            build(config,temporary/'candidate')
            candidate=temporary/'candidate'
        from .config import bootstrap
        bootstrap()
        from codesign.challenge.hardware import Hardware
        from codesign.challenge.runner import provenance
        hardware=Hardware.from_dict(load(candidate/'hardware.json'))
        programs={case:(candidate/'programs'/f'{case}.asm').read_text() for case in ['M1_P1','M2_D1']}
        if provenance(hardware,programs)!=grade.get('provenance'):raise ValueError('Current source differs from the graded release')
        if not composite:shutil.copytree(candidate,target)
    shutil.copy2(report,target/'local-grade.json')
    shutil.copy2(report.with_suffix('.isolated-run.json'),target/'local-grade.isolated-run.json')
    shutil.copy2(audit_path,target/'local-grade.audit.json')
    if not composite:
        with tarfile.open(target/'generator-source.tar.gz','w:gz') as archive:
            for path in sorted((ROOT/'src').rglob('*.py')):archive.add(path,arcname=str(path.relative_to(ROOT)),recursive=False)
    return update(identifier,{'report':reference(target/'local-grade.json'),'audit_path':reference(target/'local-grade.audit.json'),'reproduction_evidence':reference(target/'build.json'),'candidate':reference(target)})

def promote(identifier):
    record=next(r for r in read() if r['id']==identifier)
    if not (record.get('audited') and record.get('eligible') and record.get('reproduction') in {'verified','verified_composite'}):raise ValueError('Promotion needs eligible audited full grade and verified reproduction')
    record=retain_release(record)
    current=state();current['promoted_record']=identifier
    (ROOT/'data/state.json').write_text(json.dumps(current,indent=2)+'\n')
    best=({'kind':'frozen-artifacts','directory':f'data/releases/{identifier}' }
          if record.get('reproduction')=='verified_composite' else record['config'])
    (ROOT/'configs/best.yaml').write_text(json.dumps(best,indent=2)+'\n')
    return current

def package(identifier,output):
    import hashlib,lzma,zipfile,tempfile
    from pathlib import Path
    from .build import build
    from .traces import export,ROOT_THREAD
    verify_official()
    record=next(record for record in read() if record['id']==identifier)
    if not (record.get('eligible') and record.get('audited') and record.get('reproduction') in {'verified','verified_composite'}):raise ValueError('Release needs audited, eligible and reproduced full grade')
    report_path=Path(record['report']);report_path=report_path if report_path.is_absolute() else ROOT/report_path
    audit_path=resolve_reference(record['audit_path']) if record.get('audit_path') else report_path.parent/'public-grade-audit.json';audit=load(audit_path)
    if not audit.get('audit_passed') or audit.get('grade_sha256')!=digest(report_path):raise ValueError('Original report audit does not match')
    report=load(report_path)
    from .config import bootstrap
    bootstrap()
    from codesign.challenge.hardware import Hardware
    from codesign.challenge.runner import provenance,seed_digest
    out=Path(output)
    if out.exists():raise FileExistsError(out)
    files={}
    def add(path,name):files[name]=Path(path).read_bytes()
    with tempfile.TemporaryDirectory() as temporary:
        source=Path(temporary);config=source/'config.json';config.write_text(json.dumps(record['config']))
        candidate=source/'candidate'
        if record.get('reproduction')=='verified_composite':
            from .search.composite import reproduce
            retained=resolve_reference(record['candidate'])
            reproduce(retained,retained,source/'rebuild')
            import shutil
            shutil.copytree(retained,candidate)
        else:build(config,candidate,report_path.parent)
        hardware=Hardware.from_dict(load(candidate/'hardware.json'))
        programs={case:(candidate/'programs'/f'{case}.asm').read_text() for case in ['M1_P1','M2_D1']}
        if report.get('provenance')!=provenance(hardware,programs):raise ValueError('Grade provenance mismatch')
        if report.get('baseline_manifest_sha256')!=digest(ROOT/'vendor/official/baseline_manifest.json') or seed_digest(7) not in report.get('seed_sha256',[]):raise ValueError('Frozen baseline/seed mismatch')
        for name in ['hardware.json','programs/M1_P1.asm','programs/M2_D1.asm']:add(candidate/name,name)
    add(report_path,'local-grade.json');add(report_path,'project/selected-grade.json')
    add(ROOT/'data/official-starter.zip','project/official-starter.zip')
    starter_hash=hashlib.sha256(files['project/official-starter.zip']).hexdigest()
    if starter_hash!='df12b939f82c592540a1e010baf382cf4338772a85fb231affeeb6e6a7ba4c08':raise ValueError('Official starter changed')
    add(ROOT/'data/iteration-log.md','project/iteration-log.md')
    files['project/selected-candidate/design.json']=(json.dumps(record['config'],indent=2)+'\n').encode()
    if record.get('reproduction')=='verified_composite':
        retained=resolve_reference(record['candidate'])
        for name in ['composite.json','M1_P1-source.tar.gz','M2_D1-source.tar.gz',
                     'M1_P1-config.json','M2_D1-config.json']:
            add(retained/name,'project/selected-candidate/'+name)
    # 显式列出打包目录；不递归收录旧工程和临时工作区。
    for directory in ['src','configs','docs','tests','vendor/official']:
        for path in sorted((ROOT/directory).rglob('*')):
            # 看板由账本生成，完整 HTML 体积大；提交包只保留人工可读的源码与说明。
            if path == ROOT/'docs/dashboard.html':continue
            if path.is_file() and '__pycache__' not in path.parts and path.suffix not in ['.pyc','.zip'] and path.name!='submission-verification.json':add(path,'project/'+str(path.relative_to(ROOT)))
    for name in ['README.md','AGENTS.md','lab','pyproject.toml','requirements.lock','data/state.json']:
        add(ROOT/name,'project/'+name)
    # 完整账本留在本地；包内保存被选版本的原始记录，避免重复打包所有历史时序细节。
    files['project/selected-experiment.json']=(json.dumps(record,ensure_ascii=False,indent=2)+'\n').encode()
    add(audit_path,'project/selected-evidence/audit.json')
    files['project/build_candidate.py']=b"import sys,json,tempfile,argparse\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).resolve().parent/'src'))\nfrom codesign_lab.build import build\np=argparse.ArgumentParser();p.add_argument('design');p.add_argument('--verify',required=True);a=p.parse_args()\nwith tempfile.TemporaryDirectory() as d:\n if (Path(a.design).parent/'composite.json').exists():\n  from codesign_lab.search.composite import reproduce\n  print(json.dumps(reproduce(Path(a.design).parent,Path(a.verify),Path(d))))\n else: print(json.dumps(build(a.design,Path(d)/'out',a.verify)))\n"
    export(ROOT/'data/agent-trace',Path('/root/.codex/state_5.sqlite'),ROOT_THREAD)
    for path in sorted((ROOT/'data/agent-trace').glob('*')):
        if not path.is_file() or path.suffix not in ['.jsonl','.json']:continue
        data=path.read_bytes()
        # 大型原生会话使用无损 XZ，控制课程 ZIP 的压缩与解压后上限。
        name='agent-trace/'+path.name
        files[name+'.xz' if path.suffix=='.jsonl' and len(data)>8*1024**2 else name]=(
            lzma.compress(data,preset=6) if path.suffix=='.jsonl' and len(data)>8*1024**2 else data)
    files['agent-trace/README.md']=('课程代理轨迹为完整原生 JSONL。较大的 .jsonl.xz 是无损 XZ；'
        '可用 xz -dk <文件名> 解压。trace-manifest.json 记录原始字节数与 SHA-256，'
        'lab verify 会逐份解压核对。\n').encode()
    if not files['project/iteration-log.md'].strip() or not any(name.startswith('agent-trace/') and (name.endswith('.jsonl') or name.endswith('.jsonl.xz')) for name in files):raise ValueError('Missing manual review records')
    manifest={'baseline_manifest_sha256':report['baseline_manifest_sha256'],'official_starter_sha256':starter_hash,'local_experimental_score':record['score'],'record_id':identifier,'files_sha256':{name:hashlib.sha256(data).hexdigest() for name,data in files.items()}}
    files['project/package-manifest.json']=(json.dumps(manifest,indent=2)+'\n').encode()
    expanded=sum(map(len,files.values()))
    if expanded>100*1024**2:raise ValueError('Expanded archive exceeds 100 MiB')
    out.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(out,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for name,data in sorted(files.items()):archive.writestr(name,data)
    if out.stat().st_size>25*1024**2:raise ValueError('Compressed archive exceeds 25 MiB')
    return {'archive':str(out),'zip_bytes':out.stat().st_size,'expanded_bytes':expanded,'files':len(files),'uploaded':False}

def audit(identifier):
    import tempfile
    from pathlib import Path
    from .build import build
    from .records import update
    from .config import bootstrap
    manifest=verify_official();bootstrap()
    from codesign.challenge.hardware import Hardware
    from codesign.challenge.runner import provenance,seed_digest
    record=next(record for record in read() if record['id']==identifier)
    if record.get('scope')!='full' or record.get('eligible') is not True:raise ValueError('Audit needs an eligible completed full grade')
    path=Path(record['report']);path=path if path.is_absolute() else ROOT/path
    report=load(path);isolated_path=path.with_suffix('.isolated-run.json')
    if not isolated_path.exists():raise ValueError('Missing original isolated grader provenance')
    isolated=load(isolated_path)
    if isolated.get('report_sha256')!=digest(path) or isolated.get('exit_code')!=0:raise ValueError('Isolated report mismatch')
    if isolated.get('baseline_sha256')!=manifest['baseline_sha256'] or isolated.get('starter_sha256')!=manifest['starter_sha256']:raise ValueError('Isolated toolchain mismatch')
    if report.get('baseline_status')!='frozen' or report.get('baseline_manifest_sha256')!=manifest['baseline_sha256'] or seed_digest(7) not in report.get('seed_sha256',[]):raise ValueError('Frozen baseline/seed mismatch')
    selection_path=resolve_reference(record['candidate'])/'selection.json'
    composite=selection_path.is_file() and \
        len({str(Path(load(selection_path)['parents'][case]['source_root']).resolve())
             for case in ['M1_P1','M2_D1']})>1
    with tempfile.TemporaryDirectory(dir=ROOT/'workspace') as directory:
        temporary=Path(directory);config=temporary/'config.json';config.write_text(json.dumps(record['config']))
        if composite:
            from .search.pair_verify import verify_pair
            from .search.composite import snapshot,reproduce
            candidate=resolve_reference(record['candidate'])
            proof=verify_pair(candidate,temporary/'pair-proof')
            if proof['mode']!='mixed_sources':raise ValueError('组合来源身份不一致')
            snapshot(candidate,temporary/'snapshot')
            reproduce(temporary/'snapshot',candidate,temporary/'rebuild')
            result={'sha256':proof['artifact_sha256']}
        else:
            result=build(config,temporary/'candidate');candidate=temporary/'candidate'
        hardware=Hardware.from_dict(load(candidate/'hardware.json'))
        programs={case:(candidate/'programs'/f'{case}.asm').read_text() for case in ['M1_P1','M2_D1']}
        if report.get('provenance')!=provenance(hardware,programs):raise ValueError('Current generator differs from the graded design')
        if set(result['sha256'].values())!=set(isolated['input_sha256'].values()):raise ValueError('Original input file bytes differ')
    audit_path=path.with_suffix('.audit.json')
    audit_path.write_text(json.dumps({'audit_passed':True,'grade_sha256':digest(path),'input_sha256':result['sha256'],'official_manifest':manifest,'scope':'verified isolated public grade and source byte reproduction; no new timing simulation','composite':composite},indent=2)+'\n')
    verify_official()
    return update(identifier,{'audited':True,'reproduction':'verified_composite' if composite else 'verified','audit_path':reference(audit_path),'reproduction_evidence':reference(audit_path)})

import hashlib,subprocess,sys,tempfile,zipfile
from pathlib import Path,PurePosixPath

STARTER_SHA256 = "df12b939f82c592540a1e010baf382cf4338772a85fb231affeeb6e6a7ba4c08"


def unpack(archive: zipfile.ZipFile, destination: Path) -> None:
    for name in archive.namelist():
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise ValueError(f"Unsafe archive path: {name}")
    archive.extractall(destination)


def verify(path: Path) -> dict:
    import gzip,lzma
    zip_bytes = path.stat().st_size
    if not 0 < zip_bytes <= 25 * 1024**2:
        raise ValueError('ZIP must be nonempty and at most 25 MiB')
    with zipfile.ZipFile(path) as archive:
        expanded_bytes = sum(info.file_size for info in archive.infolist())
        if expanded_bytes > 100 * 1024**2:
            raise ValueError(f'Expanded ZIP exceeds 100 MiB: {expanded_bytes}')
        if len(set(archive.namelist())) != len(archive.namelist()):
            raise ValueError("Duplicate archive paths")
        if 'local-grade.json' not in archive.namelist():
            raise ValueError('Missing public grade report at ZIP root: local-grade.json')
        if archive.read('local-grade.json') != archive.read('project/selected-grade.json'):
            raise ValueError('Root local-grade.json differs from the selected original grade')
        if archive.testzip() is not None:
            raise ValueError("Archive CRC check failed")
        manifest = json.loads(archive.read("project/package-manifest.json"))
        grade = json.loads(archive.read('local-grade.json'))
        seed7 = hashlib.sha256(b'challenge-seed-v1:7').hexdigest()
        if (grade.get('baseline_status') != 'frozen'
                or grade.get('baseline_manifest_sha256') != manifest.get('baseline_manifest_sha256')
                or seed7 not in grade.get('seed_sha256', [])
                or manifest.get('official_starter_sha256') != STARTER_SHA256):
            raise ValueError('Local grade must use the current starter, frozen baseline, and seed 7')
        recorded = manifest["files_sha256"]
        if set(archive.namelist()) != set(recorded) | {"project/package-manifest.json"}:
            raise ValueError("Archive and package manifest paths differ")
        for name, expected in recorded.items():
            if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                raise ValueError(f"Package hash mismatch: {name}")
        trace_manifest = json.loads(archive.read('agent-trace/trace-manifest.json'))
        for session in trace_manifest['sessions']:
            plain = 'agent-trace/' + session['file']
            compressed = plain + '.gz'
            xz = plain + '.xz'
            if plain in recorded:
                original = archive.read(plain)
            elif xz in recorded:
                original = lzma.decompress(archive.read(xz))
            elif compressed in recorded:
                original = gzip.decompress(archive.read(compressed))
            else:
                raise ValueError(f"Missing native agent trace: {plain}")
            if len(original) != session['bytes'] or hashlib.sha256(original).hexdigest() != session['sha256']:
                raise ValueError(f"Native agent trace mismatch: {plain}")
        starter = archive.read("project/official-starter.zip")
        if hashlib.sha256(starter).hexdigest() != STARTER_SHA256:
            raise ValueError("Official starter hash mismatch")
        with tempfile.TemporaryDirectory(prefix="codesign-reproduce-") as directory:
            root = Path(directory)
            starter_path = root / "starter.zip"
            starter_path.write_bytes(starter)
            with zipfile.ZipFile(starter_path) as baseline:
                unpack(baseline, root)
            unpack(archive, root)
            result = subprocess.run(
                [sys.executable, "project/build_candidate.py",
                 "project/selected-candidate/design.json", "--verify", "."],
                cwd=root, text=True, capture_output=True,
            )
            if result.returncode:
                raise ValueError('提交包独立再生失败：'+result.stderr[-1600:])
            reproduced = json.loads(result.stdout)
    return {
        "zip": str(path.resolve()),
        "zip_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": zip_bytes,
        "expanded_bytes": expanded_bytes,
        "files_verified": len(recorded),
        "isolated_regeneration": reproduced,
        "local_score": manifest["local_experimental_score"],
    }
