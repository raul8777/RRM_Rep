#!/usr/bin/env python3
"""Overnight threshold-tuning sweep for predictive A/B/C profiles.

Runs several peak_quality threshold variants for SPOT_BULL and RANGE_SCALP,
saves each variant's report under Dev/benchmarks/super_mimic/predictive_sweep/tuning/,
and writes a final comparison summary.

Intended to be left running overnight.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root))

import Dev.scripts.mimic_predictive_profile_sweep as sweep_module
import Dev.scripts.mimic_mode_profile_sweep as base_sweep


def _variant_dir(variant_name: str) -> Path:
    d = repo_root / "Dev" / "benchmarks" / "super_mimic" / "predictive_sweep" / "tuning" / variant_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_variant(
    regime: str,
    variant_name: str,
    params: Dict[str, Any],
    windows: List[Dict[str, Any]],
    max_cycles: int,
    max_workers: int,
) -> Dict[str, Any]:
    out_dir = _variant_dir(variant_name)
    # Redirect sweep outputs to a per-variant folder
    sweep_module.OUTPUT_DIR = str(out_dir)
    base_sweep.OUTPUT_DIR = str(out_dir)

    log_path = out_dir / "tune.log"
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting {variant_name} -> {out_dir}")
    t0 = time.time()
    try:
        report = sweep_module.run_predictive_sweep(
            windows=windows,
            max_cycles=max_cycles,
            regime_filter=regime,
            log_path=str(log_path),
            max_workers=max_workers,
            params=params,
        )
    except Exception as exc:
        print(f"[{datetime.now(timezone.utc).isoformat()}] FAILED {variant_name}: {exc}")
        return {
            "variant": variant_name,
            "regime": regime,
            "params": params,
            "ok": False,
            "error": str(exc),
            "elapsed_min": round((time.time() - t0) / 60, 2),
        }

    elapsed = round((time.time() - t0) / 60, 2)
    # Persist variant report with variant id
    report["variant"] = variant_name
    report["regime"] = regime
    report["params"] = params
    report["elapsed_min"] = elapsed
    (out_dir / "variant_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"[{datetime.now(timezone.utc).isoformat()}] Finished {variant_name} in {elapsed} min")
    return report


def _build_variants() -> List[Dict[str, Any]]:
    """Return list of (regime, variant_name, params) dicts."""
    variants: List[Dict[str, Any]] = []
    # Threshold keys in predictive_*_config_from_params are <PROFILE>_<field>
    spot_sets = [
        ("spot_current", {"A_min_peak_quality": 0.20, "B_min_peak_quality": -0.30, "C_min_peak_quality": -0.80}),
        ("spot_reject_c", {"A_min_peak_quality": 0.20, "B_min_peak_quality": -0.30, "C_min_peak_quality": 10.0}),
        ("spot_top_decile", {"A_min_peak_quality": 0.46, "B_min_peak_quality": -0.60, "C_min_peak_quality": -0.95}),
        ("spot_balanced", {"A_min_peak_quality": 0.10, "B_min_peak_quality": -0.40, "C_min_peak_quality": -0.85}),
        ("spot_a_mostly", {"A_min_peak_quality": 0.0, "B_min_peak_quality": 10.0, "C_min_peak_quality": 10.0}),
    ]
    range_sets = [
        ("range_current", {"A_min_peak_quality": 0.15, "B_min_peak_quality": -0.35, "C_min_peak_quality": -0.80}),
        ("range_reject_c", {"A_min_peak_quality": 0.15, "B_min_peak_quality": -0.35, "C_min_peak_quality": 10.0}),
        ("range_top_decile", {"A_min_peak_quality": 0.36, "B_min_peak_quality": -0.57, "C_min_peak_quality": -0.84}),
        ("range_balanced", {"A_min_peak_quality": 0.05, "B_min_peak_quality": -0.45, "C_min_peak_quality": -0.85}),
        ("range_a_mostly", {"A_min_peak_quality": 0.0, "B_min_peak_quality": 10.0, "C_min_peak_quality": 10.0}),
    ]
    for name, params in spot_sets:
        variants.append({"regime": "SPOT_BULL", "name": name, "params": params})
    for name, params in range_sets:
        variants.append({"regime": "RANGE_SCALP", "name": name, "params": params})
    return variants


def main() -> None:
    parser = argparse.ArgumentParser(description="Overnight predictive profile threshold tuning")
    parser.add_argument("--max-cycles", type=int, default=2000)
    parser.add_argument("--max-workers", type=int, default=10)
    parser.add_argument("--summary", default="Dev/benchmarks/super_mimic/predictive_sweep/tuning/summary.json")
    args = parser.parse_args()

    base_sweep.WINDOWS_DIR = str(repo_root / "Dev" / "benchmarks" / "windows")
    windows = base_sweep._load_windows(None)
    if not windows:
        print("No windows found")
        return

    summary = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "max_cycles": args.max_cycles,
        "max_workers": args.max_workers,
        "variants": [],
    }

    for variant in _build_variants():
        report = _run_variant(
            regime=variant["regime"],
            variant_name=variant["name"],
            params=variant["params"],
            windows=windows,
            max_cycles=args.max_cycles,
            max_workers=args.max_workers,
        )
        summary["variants"].append(report)
        # Write incremental summary after each variant
        summary["updated_at"] = datetime.now(timezone.utc).isoformat()
        summary_path = repo_root / args.summary
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    summary_path = repo_root / args.summary
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"All variants done. Summary: {summary_path}")


if __name__ == "__main__":
    main()
