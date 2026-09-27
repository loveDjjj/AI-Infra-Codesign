'通过未修改的公开检查器执行逐案实验。'
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
from ..config import bootstrap
ROOT = bootstrap()

import numpy as np
from codesign.challenge.hardware import Hardware
from codesign.challenge.hbm_race import validate_hbm_races
from codesign.challenge.runner import check_case, estimate_case, provenance
from .cache import ResultCache, engine_identity


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--mode", choices=("functional", "estimate", "both"), default="both")
    parser.add_argument("--case", choices=("M1_P1", "M2_D1"), action="append")
    parser.add_argument("--seed", type=int, action="append")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, help="Exact exploration cache; never used by final public grade")
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError(args.out)
    started = time.monotonic()
    hardware = Hardware.from_dict(json.loads((args.candidate / "hardware.json").read_text()))
    programs = {case: (args.candidate / "programs" / f"{case}.asm").read_text() for case in ("M1_P1", "M2_D1")}
    hashes = {case: hashlib.sha256(text.encode()).hexdigest() for case, text in programs.items()}
    cache = ResultCache(args.cache_dir, engine_identity(ROOT)) if args.cache_dir else None
    report = {
        "kind": "project-local-experiment", "mode": args.mode,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "candidate": str(args.candidate.resolve()),
        "runtime": {"python": platform.python_version(), "numpy": np.__version__},
        "hardware": hardware.to_dict(), "area_mm2": hardware.area_mm2(),
        "program_sha256": hashes,
        "provenance": provenance(hardware, programs), "cases": {},
    }
    progress_path = args.out.with_suffix(".progress.json")
    if progress_path.exists():
        raise FileExistsError(progress_path)

    def checkpoint():
        report["elapsed_seconds"] = time.monotonic() - started
        report["peak_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        args.out.parent.mkdir(parents=True, exist_ok=True)
        temporary = progress_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(progress_path)

    checkpoint()
    passed = True
    for case in args.case or ("M1_P1", "M2_D1"):
        current = report["cases"][case] = {}
        identity = {"case": case, "hardware": hardware.to_dict(), "program_sha256": hashes[case]}
        current["cache_hits"] = []
        def cached(stage, key, compute):
            if cache is None:
                return compute()
            value, hit = cache.call(stage, key, compute)
            if hit:
                current["cache_hits"].append({"stage": stage, "seed": key.get("seed")})
            return value
        try:
            report["active_stage"] = {"case": case, "stage": "race_validation"}
            checkpoint()
            stage_started = time.monotonic()
            def validate():
                validate_hbm_races(programs[case])
                return {"passed": True}
            validated = cached("race", {"program_sha256": hashes[case]}, validate)
            if validated != {"passed": True}:
                raise RuntimeError("Invalid cached race validation result")
            current["race_validation_passed"] = True
            current["race_validation_seconds"] = time.monotonic() - stage_started
            checkpoint()
            if args.mode in ("functional", "both"):
                stage_started = time.monotonic()
                current["functional"] = []
                for seed in args.seed or [7]:
                    report["active_stage"] = {"case": case, "stage": "functional", "seed": seed}
                    checkpoint()
                    current["functional"].append(cached("functional", identity | {"seed": seed},
                        lambda seed=seed: check_case(case, seed, hardware, programs[case], hbm_races_validated=True)))
                current["functional_seconds"] = time.monotonic() - stage_started
                current["functional_passed"] = all(c["passed"] for c in current["functional"])
                passed &= current["functional_passed"]
                checkpoint()
            if args.mode in ("estimate", "both") and current.get("functional_passed", True):
                report["active_stage"] = {"case": case, "stage": "timing"}
                checkpoint()
                stage_started = time.monotonic()
                current["timing"] = cached("timing", identity,
                    lambda: estimate_case(case, hardware, programs[case], hbm_races_validated=True))
                current["timing_seconds"] = time.monotonic() - stage_started
        except Exception:
            current["error"] = traceback.format_exc()
            passed = False
        checkpoint()
        print(json.dumps({"case": case, "functional_passed": current.get("functional_passed"), "cycles": current.get("timing", {}).get("cycles"), "error": current.get("error")}), flush=True)
    report["elapsed_seconds"] = time.monotonic() - started
    report["active_stage"] = {"stage": "finished"}
    report["peak_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    # 仅性能估计的报告不声明功能合格。
    report["completed_without_error"] = passed
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as output:
        json.dump(report, output, indent=2)
        output.write("\n")
    progress_path.unlink()
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
