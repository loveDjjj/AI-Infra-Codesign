"""轻资产整理：先保全可比较事实，再清理已结束批次的原始产物。"""
import hashlib
import json
import os
import pwd
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from .config import ROOT
from .records import compact_record, locked
from .search.decisions import compact_observation


def anchors(root, records):
    """最高已审计整案、当前晋升版本和课程回退点必须保留原件。"""
    state=json.loads((root/'data/state.json').read_text())
    eligible=[row for row in records if row.get('scope')=='full' and
              row.get('audited') is True and row.get('eligible') is True and
              isinstance(row.get('score'),(int,float)) and
              (root/'data/releases'/row['id']).is_dir()]
    best=max(eligible,key=lambda row:row['score'])['id'] if eligible else None
    names={'joint28','joint24'}
    for key in ('promoted_record','rollback_record'):
        identifier=state.get(key)
        if identifier:
            row=next((entry for entry in records if entry['id']==identifier),None)
            if row and row.get('report'):
                path=root/row['report']
                if path.is_relative_to(root/'data/releases'):
                    names.add(path.relative_to(root/'data/releases').parts[0])
    if best:
        names.add(best)
        # 跨源码最高分的两个单案仍是结构搜索的可再生起点。
        selection=root/'data/releases'/best/'selection.json'
        if selection.is_file():
            for parent in json.loads(selection.read_text()).get('parents',{}).values():
                candidate=Path(parent.get('candidate',''))
                candidate=candidate if candidate.is_absolute() else root/candidate
                if candidate.is_relative_to(root/'data/releases') and candidate.is_dir():
                    names.add(candidate.name)
    return names,best


def active_search(root):
    """删除前核查真实进程，而不是相信可能陈旧的状态 JSON。"""
    markers=('codesign_lab.search.pipeline','scripts/pipeline.py',
             'codesign_lab.search.implementation','codesign_lab.evaluation.',
             'codesign_lab.search.worker')
    found=[]
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal() or int(path.name)==os.getpid():continue
        try:
            command=(path/'cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace')
            cwd=(path/'cwd').resolve()
        except (OSError,PermissionError):continue
        coding=('codex' in command and cwd.is_relative_to(root/'workspace'))
        if coding or (any(marker in command for marker in markers) and
                      (cwd==root or root in cwd.parents or str(root) in command)):
            found.append({'pid':int(path.name),'command':command[:180]})
    return found


def byte_count(path):
    if path.is_file() or path.is_symlink():return path.lstat().st_size
    return sum(entry.lstat().st_size for entry in path.rglob('*') if entry.is_file() or entry.is_symlink())


def plan(root=ROOT):
    root=Path(root).resolve()
    records=[json.loads(line) for line in (root/'data/experiments.jsonl').read_text().splitlines() if line.strip()]
    keep,best=anchors(root,records)
    selected=[]
    workspace=root/'workspace'
    if workspace.is_dir():
        for path in sorted(workspace.iterdir()):
            if path.name in {'ai-sessions','search-env'}:continue
            if path.is_dir() and not any(path.iterdir()):continue
            selected.append((path,'已结束的构建、评估、快照或缓存；关键指标迁入账本'))
    releases=root/'data/releases'
    if releases.is_dir():
        for path in sorted(releases.iterdir()):
            if path.is_dir() and path.name not in keep:
                selected.append((path,'非最高分的旧发布原件；保留分数与指标记录'))
    evidence=root/'data/evidence'
    if evidence.is_dir():
        for path in sorted(evidence.iterdir()):
            selected.append((path,'历史诊断原件；保留关键指标与研究结论'))
    offline=root/'docs/dashboard.html'
    if offline.is_file():
        selected.append((offline,'旧离线 HTML；实时看板从账本提供最新指标'))
    entries=[{'path':str(path.relative_to(root)),'bytes':byte_count(path),'reason':reason}
             for path,reason in selected]
    return {'best_record':best,'retained_releases':sorted(keep),
            'paths':entries,'total_bytes':sum(item['bytes'] for item in entries),
            'active_search':active_search(root),'records':len(records)}


def _removed(reference,root,removed):
    if not isinstance(reference,str) or not reference:return False
    path=Path(reference)
    path=(path if path.is_absolute() else root/path).absolute()
    return (any(path==parent or parent in path.parents for parent in removed) or
            (not path.exists() and any(path.is_relative_to(root/name)
             for name in ('workspace','data/evidence'))))


def _write_jsonl(path,rows):
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=path.parent,delete=False) as stream:
        for row in rows:stream.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
        stream.flush();os.fsync(stream.fileno());temporary=Path(stream.name)
    os.replace(temporary,path)


def _writable_tree(path):
    """隔离副本可能把目录设为只读；仅放开待删副本的属主写权限。"""
    for base, directories, _ in os.walk(path):
        for directory in [base, *(str(Path(base)/name) for name in directories)]:
            target=Path(directory)
            info=target.stat(follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & stat.S_IWUSR:
                continue
            try:target.chmod(info.st_mode | stat.S_IWUSR)
            except PermissionError:
                if os.geteuid()!=0:raise
                owner=pwd.getpwuid(info.st_uid).pw_name
                subprocess.run(['runuser','-u',owner,'--','chmod','u+w',str(target)],
                               check=True,capture_output=True,text=True)


def apply(root=ROOT):
    root=Path(root).resolve()
    current=plan(root)
    if current['active_search']:
        raise RuntimeError('搜索或评估仍在运行，禁止清理：'+json.dumps(current['active_search'],ensure_ascii=False))
    removed=[root/item['path'] for item in current['paths']]
    ledger=root/'data/experiments.jsonl'
    decisions=root/'data/decisions.jsonl'
    with locked():
        records=[json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
        updated=[]
        for original in records:
            row=compact_record(original)
            for key in ('report','candidate','audit_path','reproduction_evidence',
                        'profile_evidence','source_root','source_snapshot'):
                if _removed(row.get(key),root,removed):
                    if key=='report':
                        path=Path(row[key]);path=path if path.is_absolute() else root/path
                        if path.is_file():row['report_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
                    row.pop(key,None)
                    if key in {'report','candidate'}:row['reproduction']='record_only'
                    if key=='source_root':row['source_available']=False
            row.pop('historical_locations',None)
            for case,evidence in list(row.get('profile_evidence',{}).items()):
                if isinstance(evidence,dict) and _removed(evidence.get('path'),root,removed):
                    del row['profile_evidence'][case]
            if row.get('profile_evidence')=={}:row.pop('profile_evidence',None)
            if row.get('reproduction')=='record_only' and not row.get('candidate'):
                row['source_available']=False
            updated.append(row)
        changed=sum(left!=right for left,right in zip(records,updated))
        _write_jsonl(ledger,updated)
        decision_changes=0
        if decisions.is_file():
            rows=[json.loads(line) for line in decisions.read_text().splitlines() if line.strip()]
            compact=[]
            for row in rows:
                item=dict(row)
                item['evidence_snapshot']={key:compact_observation(value)
                     for key,value in row.get('evidence_snapshot',{}).items()}
                compact.append(item)
            decision_changes=sum(left!=right for left,right in zip(rows,compact))
            _write_jsonl(decisions,compact)
    # 账本已原子落盘后才删除；再次核对进程，避免清理期间有新批次启动。
    if active_search(root):raise RuntimeError('账本已压缩，但检测到新评估进程；未删除任何产物')
    deleted=[]
    for item in current['paths']:
        path=root/item['path']
        if path.is_symlink() or path.is_file():path.unlink()
        elif path.is_dir():
            _writable_tree(path)
            shutil.rmtree(path)
        deleted.append(item['path'])
    (root/'workspace/pipeline').mkdir(parents=True,exist_ok=True)
    (root/'workspace/families').mkdir(parents=True,exist_ok=True)
    (root/'workspace/search').mkdir(parents=True,exist_ok=True)
    return {'best_record':current['best_record'],'retained_releases':current['retained_releases'],
            'records_compacted':changed,'decisions_compacted':decision_changes,
            'paths_deleted':deleted,'planned_bytes_released':current['total_bytes']}
