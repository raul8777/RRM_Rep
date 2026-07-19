"""Validate the entry_profile_gate against labeled historical outcomes.

Streams the signal_peak_outcomes.jsonl dataset and compares the gate's
A/B/C assignments with the realized post-cost returns. The script is designed
for memory safety on large files.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple


repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root))

import entry_profile_gate  # noqa: E402


COST = 0.0025


def _net_return(record: Dict[str, Any], horizon: str = "peak_4h") -> float:
    return _as_float(record.get(horizon, 0.0)) - COST


def _as_float(v: Any, default: float = 0.0) -> float:
    try:
        if isinstance(v, dict):
            v = v.get("value", v.get("v"))
        return float(v)
    except Exception:
        return default


def stream_records(path: Path) -> Iterable[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate entry_profile_gate")
    parser.add_argument(
        "--outcomes",
        default="Dev/benchmarks/super_mimic/predictive_sweep/signal_peak_outcomes.jsonl",
        help="Path to signal_peak_outcomes.jsonl",
    )
    parser.add_argument(
        "--thresholds",
        default="Dev/benchmarks/entry_profile_calibration/entry_profile_thresholds.json",
        help="Path to calibrated thresholds",
    )
    parser.add_argument("--limit", type=int, default=None, help="Max records to process")
    args = parser.parse_args()

    outcomes_path = Path(args.outcomes)
    thresholds_path = Path(args.thresholds)

    if not outcomes_path.exists():
        print(f"Outcomes file not found: {outcomes_path}", file=sys.stderr)
        return 1
    if not thresholds_path.exists():
        print(f"Thresholds file not found: {thresholds_path}", file=sys.stderr)
        return 1

    stats: Dict[str, Dict[str, list]] = defaultdict(
        lambda: {"A": [], "B": [], "C": [], "DISCARDED": []}
    )
    confusion: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    processed = 0

    for record in stream_records(outcomes_path):
        if args.limit is not None and processed >= args.limit:
            break
        processed += 1

        regime = str(record.get("regime", "SPOT_BULL")).strip().upper() or "SPOT_BULL"
        symbol = record.get("symbol", "")
        profile, info = entry_profile_gate.classify(
            record, regime, "LONG", symbol, thresholds_path=str(thresholds_path)
        )
        profile = profile or "DISCARDED"
        actual = record.get("peak_label", "REJECT")
        confusion[regime][f"{profile}_vs_{actual}"] += 1

        net = _net_return(record)
        stats[regime][profile].append(net)

    print(f"Processed {processed:,} records from {outcomes_path}")
    print(f"Thresholds loaded from {thresholds_path}")
    print()

    for regime in sorted(stats.keys()):
        print(f"## {regime}")
        rows = stats[regime]
        total = sum(len(v) for v in rows.values())
        for tier in ("A", "B", "C", "DISCARDED"):
            vals = rows[tier]
            n = len(vals)
            if n:
                mean_net = sum(vals) / n
                win_rate = sum(1 for v in vals if v > 0) / n
                print(
                    f"  {tier}: n={n:>6} ({n/total:>6.1%}) | "
                    f"mean_net={mean_net:+.4f} | win_rate={win_rate:.1%}"
                )
            else:
                print(f"  {tier}: n=0")
        print()

    print("## Confusion matrix (assigned vs actual peak_label)")
    for regime in sorted(confusion.keys()):
        print(f"  {regime}:")
        pairs = sorted(confusion[regime].items(), key=lambda x: -x[1])
        for pair, count in pairs:
            print(f"    {pair}: {count}")
    print()

    # Final acceptance criteria: every accepted tier has positive mean net return
    ok = True
    for regime, rows in stats.items():
        for tier in ("A", "B", "C"):
            vals = rows[tier]
            if vals and (sum(vals) / len(vals)) <= 0:
                print(
                    f"FAIL: {regime} tier {tier} has non-positive mean net return",
                    file=sys.stderr,
                )
                ok = False
    if ok:
        print("PASS: all accepted A/B/C tiers have positive mean net return")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
