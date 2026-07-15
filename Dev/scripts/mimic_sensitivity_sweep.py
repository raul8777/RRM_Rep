"""MIMIC — Sensitivity sweep: vary one parameter at a time to find dominant parameters.

Runs MIMIC over a single window with each parameter varied linearly while holding
all others at their baseline values.  Outputs R² of each parameter vs key metrics
(Sharpe, PnL, profit_factor) so the bayesian sweep can focus on the most impactful
parameters first.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo_root)

from Dev.scripts.mimic_mode_profile_sweep import (
    PARAM_GRID,
    WINDOWS_DIR,
    _base_settings,
    _build_profile_overrides,
    _clear_global_caches,
    _derive_metrics,
    _load_windows,
)
from simulation.mimic_runner import SimulationRunner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_logger = logging.getLogger("mimic_sensitivity")

SENSITIVITY_REPORT_PATH = os.path.join(repo_root, "Dev", "benchmarks", "mimic_sensitivity_report.json")

BASELINE_PARAMS: Dict[str, Any] = {
    "min_signal_strength": 0.55,
    "min_success_probability": 0.55,
    "stop_loss_pct": 3.36,
    "kelly_max_usdt": 1000.0,
    "kelly_dynamic_base_pct": 0.18,
}


def _run_one(window: Dict[str, Any], profile: str, params: Dict[str, Any], max_cycles: Optional[int]) -> Dict[str, Any]:
    _clear_global_caches()
    symbols = window.get("symbols", [])
    settings = _base_settings(profile, symbols)
    for k, v in params.items():
        setattr(settings, k, v)
    profile_overrides = _build_profile_overrides(profile)
    start = datetime.fromisoformat(window["start"])
    end = datetime.fromisoformat(window["end"])
    data_dir = os.path.join(repo_root, "historical_data")
    runner = SimulationRunner(
        settings=settings,
        symbols=symbols,
        start=start,
        end=end,
        initial_usdt=2910.0,
        data_dir=data_dir,
        forced_profile=profile,
        profile_overrides=profile_overrides,
    )
    result = runner.run(max_cycles=max_cycles)
    return _derive_metrics(result.get("results", []), 2910.0)


def _pearson_r2(xs: List[float], ys: List[float]) -> Optional[float]:
    """Pearson R² between two equal-length lists."""
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mx) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - my) ** 2 for y in ys))
    if den_x == 0 or den_y == 0:
        return None
    r = num / (den_x * den_y)
    return round(r ** 2, 4)


def run_sensitivity(
    window_id: Optional[str] = None,
    profile: str = "B",
    max_cycles: Optional[int] = None,
) -> Dict[str, Any]:
    windows = _load_windows([window_id] if window_id else None)
    if not windows:
        raise RuntimeError(f"No windows found in {WINDOWS_DIR}")
    window = windows[0]
    _logger.info("Sensitivity sweep: window=%s profile=%s", window.get("window_id"), profile)

    # Baseline run
    _logger.info("Running baseline...")
    baseline_metrics = _run_one(window, profile, BASELINE_PARAMS, max_cycles)
    _logger.info("Baseline: pnl=%.2f sharpe=%s pf=%s", baseline_metrics["pnl_usdt"],
                 baseline_metrics["sharpe"], baseline_metrics["profit_factor"])

    sensitivity: Dict[str, Any] = {}
    target_keys = ["pnl_pct", "sharpe", "profit_factor", "capital_deployed_ratio", "max_drawdown_pct"]

    for param, values in PARAM_GRID.items():
        if len(values) < 2:
            continue
        _logger.info("Varying param=%s over %d values...", param, len(values))
        param_results = []
        for val in values:
            run_params = {**BASELINE_PARAMS, param: val}
            try:
                m = _run_one(window, profile, run_params, max_cycles)
                m["param_value"] = val
                param_results.append(m)
            except Exception as e:
                _logger.warning("param=%s val=%s failed: %s", param, val, e)

        if len(param_results) < 2:
            continue

        xs = [r["param_value"] for r in param_results]
        r2_by_target = {}
        for tgt in target_keys:
            ys = [r.get(tgt) for r in param_results]
            if any(y is None for y in ys):
                continue
            r2_by_target[tgt] = _pearson_r2(xs, ys)

        sensitivity[param] = {
            "values_tested": values,
            "r2_vs_metrics": r2_by_target,
            "max_r2": max((v for v in r2_by_target.values() if v is not None), default=None),
            "metric_best_value": {
                tgt: round(param_results[
                    max(range(len(param_results)), key=lambda i: param_results[i].get(tgt) or -1e9)
                ]["param_value"], 6)
                for tgt in target_keys
                if any(r.get(tgt) is not None for r in param_results)
            },
            "results": param_results,
        }
        _logger.info("  param=%s max_R²=%.4f", param, sensitivity[param]["max_r2"] or 0)

    # Rank params by max R² across all targets
    ranked = sorted(
        [(p, d["max_r2"] or 0.0) for p, d in sensitivity.items()],
        key=lambda x: x[1], reverse=True,
    )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_id": window.get("window_id"),
        "profile": profile,
        "baseline_metrics": baseline_metrics,
        "sensitivity": sensitivity,
        "ranked_params": [{"param": p, "max_r2": r} for p, r in ranked],
    }

    os.makedirs(os.path.dirname(SENSITIVITY_REPORT_PATH), exist_ok=True)
    with open(SENSITIVITY_REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    _logger.info("Sensitivity report saved to %s", SENSITIVITY_REPORT_PATH)

    print("\n=== PARAMETER SENSITIVITY RANKING ===")
    for p, r2 in ranked:
        print(f"  {p:40s}  max R²={r2:.4f}")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="MIMIC sensitivity sweep: one param at a time")
    parser.add_argument("--window", default=None, help="Window ID to use (default: first available)")
    parser.add_argument("--profile", default="B", choices=["A", "B", "C"])
    parser.add_argument("--max-cycles", type=int, default=None)
    args = parser.parse_args()
    run_sensitivity(window_id=args.window, profile=args.profile, max_cycles=args.max_cycles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
