#!/usr/bin/env python3
"""默认本地检查；加 --submit 才向公网课程站上传，不自动重试。"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PORTAL = 'https://linux-slai.tail6d76d1.ts.net:8443'


def request(url, data=None, headers=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers or {}), timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'HTTP {error.code}：{error.read().decode("utf-8", errors="replace")}') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', nargs='?', type=Path, default=ROOT / 'data/releases/joint28/submission.zip')
    parser.add_argument('--student-id', default='260010081')
    parser.add_argument('--name', default='GPT-6-Astra-Ultra')
    parser.add_argument('--notes', default='')
    parser.add_argument('--submit', action='store_true')
    parser.add_argument('--lookup', type=Path, help='用回执查询状态，不上传')
    args = parser.parse_args()
    if args.lookup:
        if args.submit:
            raise ValueError('--lookup 与 --submit 不能同时使用')
        from urllib.parse import quote
        receipt = json.loads(args.lookup.read_text())['receipt']
        result = request(PORTAL + '/api/transformer/submissions/' + quote(receipt['id'], safe=''),
                         headers={'X-Receipt-Key': receipt['receipt_key']})
        result.pop('receipt_key', None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if not args.student_id.strip() or not args.name.strip() or len(args.student_id) > 80 or len(args.name) > 80 or len(args.notes) > 1000:
        raise ValueError('学号、名称或备注不符合表单要求')
    content = args.archive.read_bytes()
    if not 0 < len(content) <= 25 * 1024**2:
        raise ValueError('ZIP 必须非空且不超过 25 MiB')
    with zipfile.ZipFile(args.archive) as archive:
        names = archive.namelist()
        required = ['hardware.json', 'programs/M1_P1.asm', 'programs/M2_D1.asm', 'local-grade.json']
        if len(names) != len(set(names)) or not all(name in names for name in required):
            raise ValueError('ZIP 路径重复或缺少硬件、两份程序、根目录 local-grade.json')
        expanded = sum(info.file_size for info in archive.infolist())
        if expanded > 100 * 1024**2 or archive.testzip() is not None:
            raise ValueError('ZIP 解压超过 100 MiB 或完整性检查失败')
        json.loads(archive.read('hardware.json'))
        grade = json.loads(archive.read('local-grade.json'))
        score = grade.get('experimental_score')
        if grade.get('eligible') is not True or not isinstance(score, (int, float)):
            raise ValueError('提交包 local-grade.json 必须包含合格的完整评分')
        trace = any(name.startswith('agent-trace/') and not name.endswith('/') for name in names)
    record = {'zip_sha256': hashlib.sha256(content).hexdigest(), 'zip_bytes': len(content),
              'expanded_bytes': expanded, 'agent_trace_present': trace,
              'student_id': args.student_id, 'name': args.name, 'score': score, 'portal': PORTAL}
    # 晋升后的新版本不能成为自身的提交对照；固定初始对照，后续使用回执。
    known_scores = [float(json.loads((ROOT / 'data/releases/joint28/local-grade.json').read_text())['experimental_score'])]
    receipts_dir = ROOT / 'data/submissions'
    prior_uploads = []
    if receipts_dir.exists():
        for saved in receipts_dir.glob('*.json'):
            try:
                previous = json.loads(saved.read_text())
                if previous.get('student_id') == args.student_id:
                    if previous.get('status') == 'received' and isinstance(previous.get('score'), (int, float)):
                        known_scores.append(float(previous['score']))
                    stamp = previous.get('uploaded_at')
                    if stamp:
                        prior_uploads.append(datetime.fromisoformat(stamp))
            except (OSError, ValueError):
                continue
    best_known = max(known_scores, default=0.0)
    target_score = best_known + 1000
    prior_best = best_known
    record['required_minimum_score'] = target_score
    print(json.dumps(record, ensure_ascii=False, indent=2))
    if not args.submit:
        print('本地检查完成，未联网、未上传。实际上传需添加 --submit。')
        return
    if score < target_score:
        raise ValueError(f'当前成绩 {score:.2f} 未达到至少提升 1000 分的门槛 {target_score:.2f}；不上传')
    board = request(PORTAL + '/api/transformer/leaderboard')
    for entry in board.get('entries', []):
        if entry.get('name') == args.name:
            target_score = max(target_score, float(entry.get('score', 0)) + 1000)
        if entry.get('name') == args.name or (prior_best and abs(float(entry.get('score', 0)) - prior_best) < 0.01):
            stamp = entry.get('created_at')
            if stamp:
                prior_uploads.append(datetime.fromisoformat(stamp.replace('Z', '+00:00')))
    if score < target_score:
        raise ValueError(f'当前成绩未超过已知已提交成绩至少 1000 分，要求 {target_score:.2f}；不上传')
    if prior_uploads:
        latest = max(prior_uploads)
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        remaining = 600 - (datetime.now(timezone.utc) - latest.astimezone(timezone.utc)).total_seconds()
        if remaining > 0:
            raise ValueError(f'距上次脚本上传未满 10 分钟，还需等待 {remaining:.0f} 秒；不上传')
    health = request(PORTAL + '/api/health')
    if not health.get('ok') or health.get('uploads_locked'):
        raise RuntimeError('服务不可用或已暂停上传')
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in {'student_id': args.student_id, 'name': args.name, 'notes': args.notes}.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="artifact"; filename="submission.zip"\r\nContent-Type: application/zip\r\n\r\n'.encode())
    parts.extend([content, f'\r\n--{boundary}--\r\n'.encode()])
    # 先创建可写回执文件。连接中断时保留尝试信息，避免盲目重传。
    directory = ROOT / 'data/submissions'
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (uuid.uuid4().hex + '.json')
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        record.update(status='attempted', receipt=None,
                      uploaded_at=datetime.now(timezone.utc).isoformat())
        json.dump(record, output, ensure_ascii=False, indent=2)
        output.flush()
        print(f'本次尝试记录：{destination}', flush=True)
        receipt = request(PORTAL + '/api/transformer/submissions', b''.join(parts),
                          {'Content-Type': 'multipart/form-data; boundary=' + boundary})
        record.update(status='received', receipt=receipt)
        output.seek(0)
        json.dump(record, output, ensure_ascii=False, indent=2)
        output.truncate()
    print(f'回执已保存：{destination}')
    print(f'提交 ID：{receipt.get("id", "未返回")}；状态：{receipt.get("judge_status", "未返回")}')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'操作失败：{error}\n不会自动重试；若上传连接中断，服务器可能已接收，请先核实。', file=sys.stderr)
        sys.exit(1)
