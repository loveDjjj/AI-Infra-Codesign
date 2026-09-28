"""只读调用 Codex CLI，核对会话身份与结构化输出；不直接修改目标队列。"""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

from .config import ROOT, load
from .search.decisions import schema_check
from .search.scheduler import terminate


def capabilities(executable='codex'):
    version=subprocess.run([executable,'--version'],capture_output=True,text=True,timeout=10,check=True).stdout.strip()
    help_text=subprocess.run([executable,'exec','resume','--help'],capture_output=True,text=True,timeout=10,check=True).stdout
    if not all(flag in help_text for flag in ['--output-schema','--json','--output-last-message']):
        raise ValueError('本机 CLI 缺少必要结构化恢复参数')
    return {'version':version,'resume_schema_supported':True}


def analyze(snapshot, *, lane, directory, session_id=None, executable='codex', timeout=600,
            isolated_session=True, model='gpt-6-astra', reasoning_effort='medium'):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',lane):
        raise ValueError('lane 无效')
    if session_id is not None:
        uuid.UUID(session_id)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+',model) or reasoning_effort not in {'low','medium','high','xhigh','max'}:
        raise ValueError('分析模型或推理档位无效')
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    # 同一会话跨源码批次共享锁；无会话的首次调用沿用 lane 锁。
    lock_root=Path(os.environ.get('CODEX_SESSION_LOCK_ROOT',
        str(ROOT/'workspace/ai-sessions'))) if session_id else directory
    lock_root.mkdir(parents=True,exist_ok=True)
    lock_name=(session_id if session_id else lane)+'.session.lock'
    with (lock_root/lock_name).open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        version=capabilities(executable)
        attempt=str(uuid.uuid4());output=directory/(attempt+'.decision.json')
        command=[executable,'exec','--sandbox','read-only','--model',model,
                 '-c','model_reasoning_effort='+reasoning_effort]
        if session_id:
            command+=['resume',session_id]
        command+=['--json','--output-schema',str(ROOT/'schemas/ai-decision.schema.json'),'-o',str(output),'-']
        prompt=('你是此项目的只读研究分析者。不得修改源码、上传网站或运行昂贵评测。'
            '只使用提供的事实和项目自己的证据。返回符合 schema 的研究决策。'
            '每次结果回调先整理新观测：在 summary 写清结果、相对基准及下一步；'
            '在 conclusions 中把可核查结论与 evidence_ids 关联，并区分实测事实和待验证假设。'
            '即使没有值得新增的目标，也要完成整理；不得编造缺失的周期、功耗或分数。'
            'decision_id 和 lane 必须原样返回；目标必须使用给定 epoch、允许域和明确 base_record。'
            '当本次 lane 为 global 时，决策顶层 lane 仍为 global，'
            '但每个 new_targets 的 lane 必须是具体研究方向，例如 p1_joint、p1_attention、p1_w2、d1_decode 或 hardware；'
            '新目标不得使用 global 作为 lane。'
            '结构提案请填写稳定的 transformation_id：完全相同的机制跨分析回合保持相同 ID；'
            '若针对已测机制增加一个可独立验证的新控制因素（如功耗限流），使用新的 ID，'
            '在 proposal 中说明原提案 ID、唯一改动和验证依据。不要仅换说法重复提案。'
            '允许提出空 new_targets；空间已覆盖时不要为了填满队列重复目标。\n'
            +json.dumps(snapshot,ensure_ascii=False,allow_nan=False))
        child=subprocess.Popen(command,cwd=ROOT,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                               text=True,start_new_session=isolated_session)
        try:
            stdout,stderr=child.communicate(prompt,timeout=timeout)
        except subprocess.TimeoutExpired:
            if isolated_session:
                terminate(child)
            else:
                child.terminate()
                try:child.wait(timeout=3)
                except subprocess.TimeoutExpired:child.kill();child.wait()
            stdout,stderr=child.communicate()
            write_trace(lane,attempt,stdout,stderr)
            raise TimeoutError('Codex 分析超过时限，未注入目标') from None
        write_trace(lane,attempt,stdout,stderr)
        if child.returncode:
            raise RuntimeError(f'Codex 分析退出码 {child.returncode}；详见受保护调用轨迹，未注入目标')
        events=[]
        for line in stdout.splitlines():
            try:events.append(json.loads(line))
            except ValueError:continue
        sessions={event.get('thread_id') for event in events if event.get('type')=='thread.started'}
        if len(sessions)!=1:
            raise ValueError('CLI 没有返回唯一会话身份')
        actual=sessions.pop();uuid.UUID(actual)
        if session_id and actual!=session_id:
            raise ValueError('恢复返回了不同会话，拒绝注入')
        if not any(event.get('type')=='turn.completed' for event in events):
            raise ValueError('CLI 未确认分析回合完成')
        decision=load(output);schema_check(decision,load(ROOT/'schemas/ai-decision.schema.json'))
        if decision['lane']!=lane or decision['decision_id']!=snapshot['request']['decision_id']:
            raise ValueError('输出决策身份与分析请求不一致')
        return {'session_id':actual,'decision':decision,'cli':version,'attempt':attempt}


def write_trace(lane,attempt,stdout,stderr):
    origin=Path(os.environ.get('CODESIGN_ORIGIN_ROOT',str(ROOT))).resolve()
    destination=origin/'data/agent-trace/pipeline'/lane
    destination.mkdir(parents=True,exist_ok=True)
    (destination/(attempt+'.events.jsonl')).write_text(stdout)
    (destination/(attempt+'.stderr.log')).write_text(stderr)
