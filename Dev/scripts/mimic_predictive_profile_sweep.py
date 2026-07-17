"""mimic_predictive_profile_sweep.py — focused sweep for per-position A/B/C profiles.

This script reuses the low-level runner from mimic_mode_profile_sweep.py but
replaces the single-profile lifecycle config with the new predictive
(A → B → C) builders.  Each window is run once (no huge grid) so that we can
rapidly validate profile assignment, per-profile sizing, and short-term PnL.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo_root)

from Dev.scripts import mimic_mode_profile_sweep as base_sweep
from simulation.mimic_lifecycle_adapter import (
    predictive_spot_bull_config_from_params,
    predictive_range_scalp_config_from_params,
)

_logger: Optional[logging.Logger] = None


OUTPUT_DIR = os.path.join(repo_root, "Dev", "benchmarks", "super_mimic", "predictive_sweep")


def _configure_logging(log_path: str) -> logging.Logger:
    global _logger
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    logger = logging.getLogger("mimic_predictive_sweep")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        logger.addHandler(logging.FileHandler(log_path, mode="w"))
        logger.addHandler(logging.StreamHandler(sys.stdout))
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    for handler in logger.handlers:
        handler.setFormatter(fmt)
    _logger = logger
    base_sweep._logger = logger
    return logger


def _regime_for_window(window: Dict[str, Any]) -> str:
    dominant = window.get("dominant_regime")
    if dominant:
        return str(dominant).upper()
    regimes = window.get("regime_pct", {}) or {}
    if regimes:
        return max(regimes, key=regimes.get)
    return "SPOT_BULL"


def run_predictive_sweep(
    windows: List[Dict[str, Any]],
    max_cycles: int = 50,
    regime_filter: Optional[str] = None,
    log_path: Optional[str] = None,
    max_workers: int = 10,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    log_path = log_path or os.path.join(OUTPUT_DIR, "sweep.log")
    logger = _configure_logging(log_path)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    results_path = os.path.join(OUTPUT_DIR, "results.jsonl")
    report_path = os.path.join(OUTPUT_DIR, "report.json")
    if os.path.exists(results_path):
        os.remove(results_path)

    total = len(windows)
    results: List[Dict[str, Any]] = []
    summary = {"total_windows": total, "ok": 0, "failed": 0, "errors": []}

    def _run_one(idx_window):
        idx, window = idx_window
        window_id = window.get("window_id", f"unknown_{idx}")
        regime = _regime_for_window(window)
        if regime_filter and regime != regime_filter:
            logger.info("Skipping window %s (regime=%s, filter=%s)", window_id, regime, regime_filter)
            return None

        if regime == "RANGE_SCALP":
            config_factory = predictive_range_scalp_config_from_params
        else:
            config_factory = predictive_spot_bull_config_from_params

        logger.info("[%d/%d] Running %s with predictive config (regime=%s)", idx, total, window_id, regime)
        try:
            result = base_sweep._run_single_config(
                window=window,
                profile="PREDICTIVE",
                params=params or {},
                profile_overrides=base_sweep._build_profile_overrides("PREDICTIVE"),
                max_cycles=max_cycles,
                lifecycle=True,
                lifecycle_config_factory=config_factory,
            )
            result["window_id"] = window_id
            result["regime"] = regime
            return result
        except Exception as exc:
            logger.exception("Failed window %s", window_id)
            return {"_error": True, "window_id": window_id, "regime": regime, "error": str(exc)}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for result in executor.map(_run_one, enumerate(windows, 1)):
            if result is None:
                continue
            if result.get("_error"):
                summary["failed"] += 1
                summary["errors"].append(f"{result['window_id']}: {result['error']}")
                continue
            results.append(result)
            summary["ok"] += 1
            with open(results_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(result, default=str) + "\n")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "max_cycles": max_cycles,
        "regime_filter": regime_filter,
        "summary": summary,
        "windows": [
            {
                "window_id": r.get("window_id"),
                "regime": r.get("regime"),
                "pnl_usdt": r.get("pnl_usdt"),
                "pnl_pct": r.get("pnl_pct"),
                "win_rate": r.get("win_rate"),
                "profit_factor": r.get("profit_factor"),
                "total_trades": r.get("total_trades"),
                "max_drawdown_pct": r.get("max_drawdown_pct"),
                "profile": r.get("profile"),
                "profile_entry_counts": r.get("profile_entry_counts"),
                "cycles": r.get("cycles"),
                "trades": r.get("trades"),
                "buys": r.get("buys"),
                "sells": r.get("sells"),
            }
            for r in results
        ],
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    logger.info("Predictive sweep complete: %s", summary)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run predictive A/B/C lifecycle sweep")
    parser.add_argument("--windows", nargs="*", help="Specific window IDs")
    parser.add_argument("--max-cycles", type=int, default=50)
    parser.add_argument("--regime", choices=["SPOT_BULL", "RANGE_SCALP"], default=None)
    parser.add_argument("--max-workers", type=int, default=10)
    parser.add_argument("--log", default=None)
    args = parser.parse_args()

    base_sweep.WINDOWS_DIR = os.path.join(repo_root, "Dev", "benchmarks", "windows")
    windows = base_sweep._load_windows(args.windows or None)
    if not windows:
        print("No windows found in", base_sweep.WINDOWS_DIR)
        return

    run_predictive_sweep(
        windows=windows,
        max_cycles=args.max_cycles,
        regime_filter=args.regime,
        log_path=args.log,
        max_workers=args.max_workers,
    )


if __name__ == "__main__":
    main()
