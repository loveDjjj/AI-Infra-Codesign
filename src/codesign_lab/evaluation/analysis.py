"""监督进程中的只读分析任务，原子保存校验后的结果。"""
import argparse
from pathlib import Path
from ..ai_bridge import analyze
from ..config import load
from ..search.scheduler import atomic_json


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('request',type=Path)
    args=parser.parse_args();request=load(args.request)
    answer=analyze(request['snapshot'],lane=request['lane'],directory=request['directory'],
        session_id=request['session_id'],executable=request.get('executable','codex'),
        timeout=request['timeout'],isolated_session=False,
        model=request.get('model','gpt-6-astra'),
        reasoning_effort=request.get('reasoning_effort','medium'))
    atomic_json(Path(request['answer']),answer)


if __name__=='__main__':main()
