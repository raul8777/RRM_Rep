#!/usr/bin/env python3
"""Calibrate A/B/C entry profile thresholds from historical peak outcomes.

Input: Dev/benchmarks/super_mimic/predictive_sweep/signal_peak_outcomes.jsonl
Output: Dev/benchmarks/entry_profile_calibration/entry_profile_thresholds.json
        Dev/benchmarks/entry_profile_calibration/calibration_report.json

The script is memory-light: it streams the JSONL file and writes results once.
Designed to run unattended with 10 workers if extended to regenerate the
underlying peak-outcome dataset.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from entry_profile_gate import calibrate, save_calibration

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("calibrate_entry_profiles")

IN_JSONL = Path("Dev/benchmarks/super_mimic/predictive_sweep/signal_peak_outcomes.jsonl")
OUT_DIR = Path("Dev/benchmarks/entry_profile_calibration")
OUT_THRESHOLDS = OUT_DIR / "entry_profile_thresholds.json"
OUT_REPORT = OUT_DIR / "calibration_report.json"


def stream_records(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    if not path.exists():
        logger.error(f"Input file not found: {path}")
        return records
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                logger.warning(f"Skipping malformed line: {e}")
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Calibrate entry profile A/B/C thresholds")
    parser.add_argument("--input", type=Path, default=IN_JSONL, help="Path to training data JSONL")
    parser.add_argument("--output", type=Path, default=OUT_THRESHOLDS, help="Output thresholds JSON path")
    parser.add_argument("--report", type=Path, default=OUT_REPORT, help="Output report JSON path")
    parser.add_argument("--outcome-col", type=str, default=None, help="Outcome column (e.g. realized_net_pnl_pct). Defaults to peak_4h minus cost.")
    parser.add_argument("--score-col", type=str, default=None, help="Score column for tiering (e.g. expected_pnl_pct). Defaults to compute_quality_score.")
    parser.add_argument("--horizon", type=str, default="peak_4h", help="Outcome horizon when --outcome-col is not used")
    parser.add_argument("--cost", type=float, default=0.0025, help="Round-trip cost for default outcome")
    parser.add_argument("--target-coverage-A", type=float, default=0.10)
    parser.add_argument("--target-coverage-B", type=float, default=0.25)
    parser.add_argument("--target-coverage-C", type=float, default=0.45)
    parser.add_argument("--min-win-rate-A", type=float, default=0.85)
    parser.add_argument("--min-win-rate-B", type=float, default=0.75)
    parser.add_argument("--min-win-rate-C", type=float, default=0.65)
    parser.add_argument("--min-mean-net-A", type=float, default=0.010)
    parser.add_argument("--min-mean-net-B", type=float, default=0.005)
    parser.add_argument("--min-mean-net-C", type=float, default=0.002)
    args = parser.parse_args()

    logger.info(f"Reading outcomes from {args.input}")
    records = stream_records(args.input)
    logger.info(f"Loaded {len(records)} records")

    if len(records) < 1000:
        logger.error("Insufficient data for calibration (<1000 records)")
        sys.exit(1)

    result = calibrate(
        records,
        horizon=args.horizon,
        cost=args.cost,
        target_coverage_A=args.target_coverage_A,
        target_coverage_B=args.target_coverage_B,
        target_coverage_C=args.target_coverage_C,
        min_win_rate_A=args.min_win_rate_A,
        min_win_rate_B=args.min_win_rate_B,
        min_win_rate_C=args.min_win_rate_C,
        min_mean_net_A=args.min_mean_net_A,
        min_mean_net_B=args.min_mean_net_B,
        min_mean_net_C=args.min_mean_net_C,
        outcome_col=args.outcome_col,
        score_col=args.score_col,
    )

    args.report.parent.mkdir(parents=True, exist_ok=True)
    save_calibration(result, args.output)

    with open(args.report, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    logger.info(f"Calibration report written to {args.report}")
    logger.info("Thresholds by regime:")
    for regime, th in result["thresholds"].items():
        logger.info(f"  {regime}: A>={th.get('A'):.4f} B>={th.get('B'):.4f} C>={th.get('C'):.4f}")


if __name__ == "__main__":
    main()
