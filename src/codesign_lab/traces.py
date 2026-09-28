'导出本课程的完整原生代理会话树，不包含无关会话。'
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT_THREAD = "01a0dde1-13b5-73a1-b529-2ab96a7360cf"


def register_sessions(destination: Path, events: str, *, role: str, key: str) -> set[str]:
    """仅保存独立会话身份；调用原始日志留在可清理工作区。"""
    ids = set()
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get('type') == 'thread.started' and isinstance(event.get('thread_id'), str):
            ids.add(event['thread_id'])
    if ids:
        destination.mkdir(parents=True, exist_ok=True)
        with (destination / 'session-links.jsonl').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            for identifier in sorted(ids):
                stream.write(json.dumps({'thread_id': identifier, 'role': role, 'key': key}) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
    return ids


def export(destination: Path, state_database: Path, root_thread: str) -> dict:
    # 独立启动的规划与编码会话不在根会话的子代理树中；从调用事件补齐其原生轨迹。
    linked = set()
    previous_manifest = destination / 'trace-manifest.json'
    if previous_manifest.is_file():
        linked.update(entry['thread_id'] for entry in json.loads(previous_manifest.read_text())['sessions'])
    links = destination / 'session-links.jsonl'
    if links.is_file():
        linked.update(json.loads(line)['thread_id'] for line in links.read_text().splitlines()
                      if line.strip())
    for event_file in destination.rglob('*.events.jsonl') if destination.exists() else []:
        if any(part.startswith('test-analysis-') for part in event_file.parts):
            continue
        for line in event_file.read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('type') == 'thread.started' and isinstance(event.get('thread_id'), str):
                linked.add(event['thread_id'])
    connection = sqlite3.connect(f"file:{state_database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    root_rows = connection.execute(
        """WITH RECURSIVE related(id) AS (
               SELECT ? UNION
               SELECT e.child_thread_id FROM thread_spawn_edges e
               JOIN related r ON e.parent_thread_id=r.id
           ) SELECT t.id,t.rollout_path,t.agent_path FROM threads t
             JOIN related r ON t.id=r.id ORDER BY t.created_at_ms,t.id""",
        (root_thread,),
    ).fetchall()
    linked_rows = connection.execute(
        'SELECT id,rollout_path,agent_path FROM threads WHERE id IN (' +
        ','.join('?' for _ in linked) + ')', tuple(sorted(linked))
    ).fetchall() if linked else []
    connection.close()
    if not root_rows:
        raise ValueError("No session records found for the homework root thread")
    found = {row['id'] for row in linked_rows}
    if linked - found:
        raise ValueError('存在无法导出原生轨迹的独立代理会话')
    rows = list({row['id']: row for row in [*root_rows, *linked_rows]}.values())
    destination.mkdir(parents=True, exist_ok=True)
    entries = []
    for row in rows:
        source = Path(row["rollout_path"])
        data = source.read_bytes()
        # 会话可能正在写入；只保存完整的原生记录。
        if not data.endswith(b"\n"):
            data = data[: data.rfind(b"\n") + 1]
        if not data:
            raise ValueError(f"Empty trace for {row['id']}")
        for line in data.splitlines():
            json.loads(line)
        target = destination / source.name
        temporary = target.with_suffix(".jsonl.tmp")
        temporary.write_bytes(data)
        temporary.replace(target)
        entries.append({
            "thread_id": row["id"], "agent_path": row["agent_path"],
            "file": target.name, "bytes": len(data),
            "records": len(data.splitlines()),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    manifest = {
        "root_thread": root_thread,
        "snapshot_utc": datetime.now(timezone.utc).isoformat(),
        "format": "Native Codex rollout JSONL, complete records at snapshot time",
        "sessions": entries,
    }
    (destination / "trace-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().parents[2] / "data/agent-trace")
    parser.add_argument("--state", type=Path, default=Path("/root/.codex/state_5.sqlite"))
    parser.add_argument("--root-thread", default=ROOT_THREAD)
    args = parser.parse_args()
    result = export(args.out, args.state, args.root_thread)
    print(json.dumps({"sessions": len(result["sessions"]), "bytes": sum(s["bytes"] for s in result["sessions"]), "out": str(args.out)}))
