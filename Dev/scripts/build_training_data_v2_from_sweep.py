"""Build a reusable calibration dataset v2 from lifecycle sweep results.

Given one or more `results.jsonl` files produced by `mimic_predictive_profile_sweep.py`
(or `mimic_mode_profile_sweep.py` with `--lifecycle`), this script extracts every
entry/exit pair, computes the scale-invariant expected return, the realized net
return, and the regime. The output JSONL is the direct input for
`calibrate_entry_profiles_db0300.py --v2`.

The dataset is additive: you can pass multiple sweep result files and later
append new sweeps without re-running old ones.

Usage:
    python Dev/scripts/build_training_data_v2_from_sweep.py \
        Dev/benchmarks/super_mimic/predictive_sweep/results.jsonl \
        Dev/benchmarks/super_mimic/sm_*/results.jsonl \
        --output Dev/benchmarks/entry_profile_calibration/training_data_v2.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root))

from Dev.scripts.analyze_lifecycle_entry_outcomes import (
    compute_outcomes,
    load_results,
    match_entries_exits,
)

DEFAULT_OUT = repo_root / "Dev" / "benchmarks" / "entry_profile_calibration" / "training_data_v2.jsonl"


def _iter_result_paths(inputs: Iterable[str]) -> Iterable[Path]:
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            for child in sorted(p.glob("**/*.jsonl")):
                if child.name.endswith("_results.jsonl") or child.name == "results.jsonl":
                    yield child
        elif p.is_file():
            yield p
        elif "*" in item:
            yield from sorted(Path(".").glob(item))


def _build_rows(paths: Iterable[Path]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    all_rows: List[Dict[str, Any]] = []
    files_processed = 0
    total_runs = 0
    unmatched_runs = 0
    for path in paths:
        files_processed += 1
        try:
            results = load_results(str(path))
        except Exception as e:
            print(f"[Skip] {path}: {e}")
            continue
        for result in results:
            total_runs += 1
            matched = match_entries_exits(result)
            if not matched:
                unmatched_runs += 1
                continue
            rows = compute_outcomes(matched)
            all_rows.extend(rows)
    return all_rows, {
        "files_processed": files_processed,
        "total_runs": total_runs,
        "unmatched_runs": unmatched_runs,
        "total_entries": len(all_rows),
    }


def _regime_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    stats: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        regime = str(r.get("regime") or "SPOT_BULL").upper()
        bucket = stats.setdefault(regime, {"n": 0, "win": 0, "total_pnl_pct": 0.0})
        bucket["n"] += 1
        pnl = float(r.get("realized_net_pnl_pct") or 0.0)
        bucket["total_pnl_pct"] += pnl
        if pnl > 0:
            bucket["win"] += 1
    for regime, b in stats.items():
        b["win_rate"] = round(b["win"] / b["n"], 4) if b["n"] else 0.0
        b["mean_pnl_pct"] = round(b["total_pnl_pct"] / b["n"], 4) if b["n"] else 0.0
        del b["win"]
        del b["total_pnl_pct"]
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Build training_data_v2.jsonl from lifecycle sweeps")
    parser.add_argument("inputs", nargs="+", help="results.jsonl files or directories/globs")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT, help="Output JSONL path")
    parser.add_argument("--append", action="store_true", help="Append to existing output instead of overwriting")
    args = parser.parse_args()

    paths = list(_iter_result_paths(args.inputs))
    if not paths:
        print("No results.jsonl files found in provided inputs.")
        return 1

    print(f"Processing {len(paths)} file(s)...")
    rows, meta = _build_rows(paths)
    if not rows:
        print("No matched entry/exit rows found.")
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.append else "w"
    with open(args.output, mode, encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, default=str, ensure_ascii=False) + "\n")

    meta["regime_stats"] = _regime_stats(rows)
    meta["output_path"] = str(args.output)
    print(json.dumps(meta, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
