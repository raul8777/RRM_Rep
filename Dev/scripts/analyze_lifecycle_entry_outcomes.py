"""Analyze lifecycle sweep results to derive per-entry feature thresholds.

Given a sweep results JSONL file produced by mimic_mode_profile_sweep.py with
`lifecycle_raw` per run, this script:

1. Matches each entry to its subsequent exit(s) within the same simulation.
2. Computes short-term outcome metrics (PnL%, cycles to first close, etc.).
3. Suggests A/B/C profile thresholds based on features of entries with the best
   short-term outcomes.

Output is a JSON file with descriptive statistics and recommended thresholds.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class Entry:
    run_id: str
    symbol: str
    entry_cycle: int
    position_usdt: float
    features: Dict[str, Any]


def _feature(entry: Dict[str, Any], key: str, default: float = 0.0) -> float:
    sig = entry.get("signal") or {}
    feats = sig.get("features") or {}
    val = feats.get(key)
    if val is None:
        val = sig.get(key)
    if val is None:
        val = entry.get(key)
    try:
        return float(val)
    except Exception:
        return default


def load_results(path: str) -> List[Dict[str, Any]]:
    results = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                results.append(json.loads(line))
            except Exception:
                continue
    return results


def match_entries_exits(result: Dict[str, Any]) -> List[Tuple[Entry, List[Dict[str, Any]]]]:
    """Return list of (Entry, exits_for_symbol_after_entry) for one run."""
    raw = result.get("lifecycle_raw") or []
    if not raw:
        return []

    # Collect all entries and exits with cycle numbers.
    entries_by_symbol: Dict[str, List[Entry]] = defaultdict(list)
    exits_by_cycle: List[Tuple[int, Dict[str, Any]]] = []

    for cycle_idx, cycle in enumerate(raw):
        summary = cycle if isinstance(cycle, dict) and "entries" in cycle else cycle.get("summary", {})
        if not isinstance(summary, dict):
            continue
        for ent in summary.get("entries", []):
            if not ent:
                continue
            symbol = ent.get("symbol")
            if not symbol:
                continue
            sig = ent.get("signal") or {}
            sig_features = sig.get("features") or {}
            entry = Entry(
                run_id=f"{result.get('window_id','?')}_{result.get('profile','?')}_{json.dumps(result.get('params','?'), sort_keys=True)}",
                symbol=symbol,
                entry_cycle=cycle_idx,
                position_usdt=float(ent.get("position_usdt") or 0),
                features={
                    **{k: float(v) for k, v in sig_features.items() if isinstance(v, (int, float, bool))},
                    "composite_score": _feature(ent, "composite_score"),
                    "success_probability": _feature(ent, "success_probability"),
                    "signal_strength": _feature(ent, "signal_strength"),
                    "expected_pnl": _feature(ent, "expected_pnl"),
                    "rsi_4h": _feature(ent, "rsi_4h"),
                    "vol_spike": _feature(ent, "vol_spike"),
                    "range_position": _feature(ent, "range_position"),
                    "atr": _feature(ent, "atr"),
                    "category": sig.get("category", "UNKNOWN"),
                },
            )
            entries_by_symbol[symbol].append(entry)
        for ex in summary.get("exits", []):
            exits_by_cycle.append((cycle_idx, ex))

    matched: List[Tuple[Entry, List[Dict[str, Any]]]] = []
    open_idx: Dict[str, int] = defaultdict(int)
    for symbol, entries in entries_by_symbol.items():
        entries.sort(key=lambda e: e.entry_cycle)
        # FIFO: assign each exit to earliest open entry for that symbol.
        local_exits = [ex for cyc, ex in exits_by_cycle if ex.get("symbol") == symbol]
        local_exits.sort(key=lambda x: x.get("_cycle", 0))
        for ex in local_exits:
            if open_idx[symbol] >= len(entries):
                break
            entry = entries[open_idx[symbol]]
            matched.append((entry, [ex]))
            # A CLOSE_ALL consumes the entry; CLOSE_PCT keeps it open for more.
            action = (ex.get("action") or "CLOSE_ALL")
            if action in ("CLOSE_ALL", "SELL", None):
                open_idx[symbol] += 1
    return matched


def compute_outcomes(matched: List[Tuple[Entry, List[Dict[str, Any]]]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for entry, exits in matched:
        total_pnl_usdt = sum(float(ex.get("realized_pnl_usdt") or ex.get("result", {}).get("realized_pnl_usdt") or 0) for ex in exits)
        pnl_pct = (total_pnl_usdt / entry.position_usdt * 100) if entry.position_usdt > 0 else 0.0
        rows.append({
            **entry.features,
            "position_usdt": entry.position_usdt,
            "pnl_usdt": total_pnl_usdt,
            "pnl_pct": pnl_pct,
            "exits": len(exits),
        })
    return rows


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def recommend_thresholds(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {}

    def col(name: str) -> List[float]:
        return [r[name] for r in rows if name in r]

    outcomes = col("pnl_pct")
    quick_win = [r for r in rows if r["pnl_pct"] > 1.0]
    medium_win = [r for r in rows if 0.3 < r["pnl_pct"] <= 1.0]
    small_or_loss = [r for r in rows if -0.5 <= r["pnl_pct"] <= 0.3]
    big_loss = [r for r in rows if r["pnl_pct"] < -0.5]

    def thresholds(group: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not group:
            return {}
        feature_keys = [
            "composite_score",
            "success_probability",
            "signal_strength",
            "expected_pnl",
            "rsi_4h",
            "vol_spike",
            "range_position",
            "range_score",
            "vol_score",
            "rsi_score",
            "pnl_score",
            "confluence",
        ]
        return {
            "count": len(group),
            "mean_pnl_pct": round(sum(r["pnl_pct"] for r in group) / len(group), 4),
            "pnl_pct_p25": percentile([r["pnl_pct"] for r in group], 25),
            "pnl_pct_p75": percentile([r["pnl_pct"] for r in group], 75),
            "min_thresholds": {
                k: percentile([r.get(k, 0.0) for r in group], 10) for k in feature_keys
            },
            "mean_features": {
                k: round(sum(r.get(k, 0.0) for r in group) / len(group), 6) for k in feature_keys
            },
        }

    # Simple linear weights learned by correlating features with positive outcome magnitude.
    feature_keys = [
        "composite_score",
        "success_probability",
        "signal_strength",
        "expected_pnl",
        "rsi_4h",
        "vol_spike",
        "range_position",
        "range_score",
        "vol_score",
        "rsi_score",
        "pnl_score",
        "confluence",
    ]
    from statistics import mean, stdev
    weights: Dict[str, float] = {}
    for k in feature_keys:
        vals = [r.get(k, 0.0) for r in rows]
        v_std = stdev(vals) if len(set(vals)) > 1 else 1.0
        target = [1.0 if r["pnl_pct"] > 0.5 else 0.0 for r in rows]
        t_mean = mean(target) or 1e-9
        # Normalised covariance-ish weight: E[(x - x_mean)*(t - t_mean)]
        x_mean = mean(vals)
        cov = sum((r.get(k, 0.0) - x_mean) * (t - t_mean) for r, t in zip(rows, target)) / len(rows)
        weights[k] = round(cov / (v_std * t_mean * (1 - t_mean) + 1e-9), 6)

    return {
        "total_entries": len(rows),
        "outcome_percentiles": {
            "p10": percentile(outcomes, 10),
            "p25": percentile(outcomes, 25),
            "p50": percentile(outcomes, 50),
            "p75": percentile(outcomes, 75),
            "p90": percentile(outcomes, 90),
        },
        "classes": {
            "A_quick_win": thresholds(quick_win),
            "B_medium_win": thresholds(medium_win),
            "C_small_or_loss": thresholds(small_or_loss),
            "rejected_big_loss": thresholds(big_loss),
        },
        "feature_weights": weights,
    }


def main() -> int:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <results.jsonl> [output.json]")
        return 1
    results_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else results_path.replace(".jsonl", "_entry_analysis.json")

    results = load_results(results_path)
    print(f"Loaded {len(results)} sweep runs from {results_path}")

    all_rows: List[Dict[str, Any]] = []
    for result in results:
        matched = match_entries_exits(result)
        rows = compute_outcomes(matched)
        all_rows.extend(rows)

    print(f"Matched {len(all_rows)} entries to exits")
    if not all_rows:
        print("No entries found. Ensure the sweep was run with --lifecycle and lifecycle_raw present.")
        return 0

    recommendations = recommend_thresholds(all_rows)
    output = {
        "source": results_path,
        "total_entries": len(all_rows),
        "recommendations": recommendations,
        "sample": all_rows[:5],
    }

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"Wrote analysis to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
