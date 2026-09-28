'收集官方时序引擎的可选事件轨迹及同步阶段。'
import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from ..config import bootstrap,digest
ROOT = bootstrap()
from codesign.challenge.abi import build_layout
from codesign.challenge.hardware import Hardware
from codesign.challenge.isa import iter_parse
from codesign.challenge.pipeline import estimate_pipeline
from codesign.challenge.runner import required_hbm_words, step_requirements
from codesign.challenge.workload import MODELS

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("candidate", type=Path)
parser.add_argument("--case", required=True)
parser.add_argument("--compare", type=Path, required=True)
parser.add_argument("--out", type=Path, required=True)
args = parser.parse_args()
if args.out.exists():
    existing=json.loads(args.out.read_text())
    expected={'hardware':digest(args.candidate/'hardware.json'),
              'program':digest(args.candidate/'programs'/f'{args.case}.asm')}
    previous=json.loads(args.compare.read_text())['cases'][args.case]['timing']
    if existing.get('complete_timing_identical') is True and existing.get('input_sha256')==expected and existing.get('compare_sha256')==digest(args.compare) and existing.get('timing')==previous:
        print(json.dumps({'reused_complete_trace':True,'cycles':previous['cycles']}))
        raise SystemExit(0)
    raise ValueError('已有轨迹不匹配本次输入或报告，拒绝覆盖')
hardware = Hardware.from_dict(json.loads((args.candidate / "hardware.json").read_text()))
program = (args.candidate / "programs" / f"{args.case}.asm").read_text()
model, scenario = args.case.split("_")
layout = build_layout(MODELS[model], scenario)
trace = {}
timing = asdict(estimate_pipeline(hardware, iter_parse(program), required_hbm_words(program, layout),
                                step_requirements(args.case, layout), trace=trace))
previous = json.loads(args.compare.read_text())["cases"][args.case]["timing"]
if timing != previous:
    raise ValueError("Traced timing differs from the completed public report")
stages, events, previous_finish = [], [], 0
for instruction in iter_parse(program):
    event = instruction.args.get("event")
    if event:
        events.append(event)
    if instruction.op in ("BARRIER", "STEP.COMMIT") and events:
        end = max(trace[event]["finish"] for event in events)
        stages.append({"stage": len(stages), "sync": instruction.op,
                       "events": len(events), "start": min(trace[event]["issue"] for event in events),
                       "finish": end, "elapsed_since_previous_stage": end - previous_finish})
        events, previous_finish = [], end
from .profile import operator_spans
mapping_path=args.candidate / 'operator-map.json'
operators=operator_spans(program,json.loads(mapping_path.read_text()).get(args.case,[]),trace) if mapping_path.exists() else []
from ..search.scheduler import atomic_json
atomic_json(args.out,{"complete_timing_identical": True, "timing": timing,
               "stages": stages, "operator_spans": operators, "event_trace": trace,
               'input_sha256':{'hardware':digest(args.candidate/'hardware.json'),
                               'program':digest(args.candidate/'programs'/f'{args.case}.asm')},
               'compare_sha256':digest(args.compare)})
print(json.dumps({"cycles": timing["cycles"], "stages": len(stages), "events": len(trace)}))
