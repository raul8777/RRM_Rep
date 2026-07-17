"""Classify MIMIC sweep windows by severity for A→B→C fallback planning.

Reads a Super MIMIC / MIMIC sweep report and tags each window as passed or
failed against configurable thresholds. Failed windows are the candidates for
the next profile rescue phase (A-favorite → B-rescue → C-rescue).
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


DEFAULT_THRESHOLDS = {
    "min_profit_factor": 1.05,
    "min_win_rate": 0.45,
    "max_drawdown_pct": 15.0,
    "max_sl_rate": 0.40,
    "min_pnl_per_day_usdt": 0.0,
    "min_trades": 3,
}


@dataclass
class Thresholds:
    min_profit_factor: float = 1.05
    min_win_rate: float = 0.45
    max_drawdown_pct: float = 15.0
    max_sl_rate: float = 0.40
    min_pnl_per_day_usdt: float = 0.0
    min_trades: int = 3

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "Thresholds":
        base = DEFAULT_THRESHOLDS.copy()
        if d:
            base.update({k: v for k, v in d.items() if k in base})
        return cls(**{k.replace("min_", "min_").replace("max_", "max_"): v for k, v in base.items()})  # noqa

    def to_dict(self) -> Dict[str, Any]:
        return {
            "min_profit_factor": self.min_profit_factor,
            "min_win_rate": self.min_win_rate,
            "max_drawdown_pct": self.max_drawdown_pct,
            "max_sl_rate": self.max_sl_rate,
            "min_pnl_per_day_usdt": self.min_pnl_per_day_usdt,
            "min_trades": self.min_trades,
        }


def _sl_rate(result: Dict[str, Any]) -> float:
    """Estimate SL rate from close reasons when available."""
    detail = result.get("pnl_by_regime") or {}
    # Prefer an explicit field if the runner starts recording it
    explicit = result.get("sl_rate")
    if explicit is not None:
        return float(explicit)
    sells = float(result.get("sells", 0) or 0)
    sl_tp = float(result.get("sl_tp", 0) or 0)
    trades = float(result.get("trades", 0) or 0)
    if trades <= 0:
        return 0.0
    # Conservative proxy: count all SL/TP closes as SL-like forced exits
    return (sl_tp) / trades


def _score(result: Dict[str, Any]) -> float:
    """Composite score used to pick the best config for a window."""
    pf = result.get("profit_factor") or 0.0
    wr = result.get("win_rate") or 0.0
    dd = result.get("max_drawdown_pct") or 100.0
    pnl_day = result.get("pnl_per_day_usdt") or 0.0
    if dd <= 0:
        dd = 0.01
    # Reward PF and WR, penalise drawdown
    return float(pf * wr * 100.0 / dd + pnl_day * 0.01)


def classify_window(results: List[Dict[str, Any]], thresholds: Thresholds) -> Dict[str, Any]:
    """Pick the best result for a window and classify it."""
    if not results:
        return {"passed": False, "reasons": ["no_results"], "best_result": None}

    best = max(results, key=_score)
    pf = best.get("profit_factor")
    wr = best.get("win_rate")
    dd = best.get("max_drawdown_pct")
    pnl_day = best.get("pnl_per_day_usdt")
    trades = best.get("trades", 0)
    slr = _sl_rate(best)

    reasons: List[str] = []
    if pf is None or pf < thresholds.min_profit_factor:
        reasons.append(f"profit_factor {pf} < {thresholds.min_profit_factor}")
    if wr is None or wr < thresholds.min_win_rate:
        reasons.append(f"win_rate {wr} < {thresholds.min_win_rate}")
    if dd is None or dd > thresholds.max_drawdown_pct:
        reasons.append(f"drawdown {dd}% > {thresholds.max_drawdown_pct}%")
    if pnl_day is None or pnl_day < thresholds.min_pnl_per_day_usdt:
        reasons.append(f"pnl/day {pnl_day} < {thresholds.min_pnl_per_day_usdt}")
    if trades < thresholds.min_trades:
        reasons.append(f"trades {trades} < {thresholds.min_trades}")
    if slr > thresholds.max_sl_rate:
        reasons.append(f"sl_rate {slr:.2f} > {thresholds.max_sl_rate}")

    return {
        "passed": len(reasons) == 0,
        "reasons": reasons,
        "sl_rate": slr,
        "best_result": {
            "window_id": best.get("window_id"),
            "profile": best.get("profile"),
            "params": best.get("params"),
            "profit_factor": pf,
            "win_rate": wr,
            "max_drawdown_pct": dd,
            "pnl_per_day_usdt": pnl_day,
            "trades": trades,
            "pnl_usdt": best.get("pnl_usdt"),
            "sharpe": best.get("sharpe"),
        },
    }


def classify_report(
    report_path: str,
    thresholds: Thresholds,
    mode: str,
    profile: str,
    phase: str,
) -> Dict[str, Any]:
    with open(report_path, "r", encoding="utf-8") as f:
        report = json.load(f)

    results = report.get("results", [])
    by_window: Dict[str, List[Dict[str, Any]]] = {}
    for r in results:
        wid = r.get("window_id")
        if not wid:
            continue
        by_window.setdefault(wid, []).append(r)

    passed: List[str] = []
    failed: List[str] = []
    details: List[Dict[str, Any]] = []

    for wid in sorted(by_window):
        res = by_window[wid]
        classification = classify_window(res, thresholds)
        entry = {
            "window_id": wid,
            "passed": classification["passed"],
            "reasons": classification["reasons"],
            "sl_rate": classification["sl_rate"],
            "best": classification["best_result"],
        }
        details.append(entry)
        if classification["passed"]:
            passed.append(wid)
        else:
            failed.append(wid)

    return {
        "mode": mode,
        "profile": profile,
        "phase": phase,
        "thresholds": thresholds.to_dict(),
        "summary": {
            "total_windows": len(by_window),
            "passed": len(passed),
            "failed": len(failed),
        },
        "passed_windows": passed,
        "failed_windows": failed,
        "window_details": details,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify MIMIC sweep windows by severity")
    parser.add_argument("--report", required=True, help="Path to mimic_sweep_report.json")
    parser.add_argument("--mode", required=True, help="Regime mode (SPOT_BULL, etc.)")
    parser.add_argument("--profile", required=True, help="Profile label (A/B/C)")
    parser.add_argument("--phase", required=True, help="Phase tag (A-favorite, B-rescue, C-rescue)")
    parser.add_argument("--thresholds", default=None, help="Optional JSON file with thresholds")
    parser.add_argument("--output", required=True, help="Output JSON path")
    parser.add_argument("--failed-list", default=None, help="Optional text file with failed window IDs (one per line)")
    args = parser.parse_args()

    thresh_dict = None
    if args.thresholds:
        with open(args.thresholds, "r", encoding="utf-8") as f:
            thresh_dict = json.load(f)
    thresholds = Thresholds.from_dict(thresh_dict)

    result = classify_report(args.report, thresholds, args.mode, args.profile, args.phase)

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    if args.failed_list:
        with open(args.failed_list, "w", encoding="utf-8") as f:
            for wid in result["failed_windows"]:
                f.write(wid + "\n")

    print(f"[{args.mode}-{args.profile} {args.phase}] total={result['summary']['total_windows']} "
          f"passed={result['summary']['passed']} failed={result['summary']['failed']}")
    if result["failed_windows"]:
        print("Failed windows:", ", ".join(result["failed_windows"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
