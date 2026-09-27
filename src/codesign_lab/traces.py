'导出本课程的完整原生代理会话树，不包含无关会话。'
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT_THREAD = "01a0dde1-13b5-73a1-b529-2ab96a7360cf"


def export(destination: Path, state_database: Path, root_thread: str) -> dict:
    connection = sqlite3.connect(f"file:{state_database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """WITH RECURSIVE related(id) AS (
               SELECT ? UNION
               SELECT e.child_thread_id FROM thread_spawn_edges e
               JOIN related r ON e.parent_thread_id=r.id
           ) SELECT t.id,t.rollout_path,t.agent_path FROM threads t
             JOIN related r ON t.id=r.id ORDER BY t.created_at_ms,t.id""",
        (root_thread,),
    ).fetchall()
    connection.close()
    if not rows:
        raise ValueError("No session records found for the homework root thread")
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
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "agent-trace")
    parser.add_argument("--state", type=Path, default=Path("/root/.codex/state_5.sqlite"))
    parser.add_argument("--root-thread", default=ROOT_THREAD)
    args = parser.parse_args()
    result = export(args.out, args.state, args.root_thread)
    print(json.dumps({"sessions": len(result["sessions"]), "bytes": sum(s["bytes"] for s in result["sessions"]), "out": str(args.out)}))
