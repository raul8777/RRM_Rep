"""MIMIC — Bayesian optimization with Optuna.

Uses Optuna to efficiently search the parameter space, pruning trials that
exceed max_drawdown or have too few trades.  Objective is a composite score:
  score = sharpe * 0.5 + profit_factor_norm * 0.3 + capital_deployed * 0.2

Falls back gracefully if Optuna is not installed (prints install instructions).
"""
from __future__ import annotations

import argparse
import json
import logging
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
_logger = logging.getLogger("mimic_bayesian")

BAYESIAN_REPORT_PATH = os.path.join(repo_root, "Dev", "benchmarks", "mimic_bayesian_report.json")

MAX_DRAWDOWN_PRUNE = 30.0   # prune if drawdown > 30%
MIN_TRADES_PRUNE = 3        # prune if fewer than 3 closed trades


def _composite_score(m: Dict[str, Any]) -> float:
    """Higher is better. Returns NaN if hard constraints violated."""
    sharpe = m.get("sharpe") or 0.0
    pf = m.get("profit_factor") or 0.0
    cd = m.get("capital_deployed_ratio") or 0.0
    pf_norm = min(pf / 3.0, 1.0)  # normalise PF to [0,1] capped at 3
    return sharpe * 0.5 + pf_norm * 0.3 + cd * 0.2


def _run_trial(
    window: Dict[str, Any],
    profile: str,
    params: Dict[str, Any],
    max_cycles: Optional[int],
) -> Dict[str, Any]:
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


def run_bayesian(
    window_id: Optional[str] = None,
    profile: str = "B",
    n_trials: int = 50,
    max_cycles: Optional[int] = None,
    timeout_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
    except ImportError:
        print("Optuna not installed. Run: pip install optuna")
        print("Falling back to random search (50 trials).")
        return _run_random_search(window_id, profile, n_trials, max_cycles)

    windows = _load_windows([window_id] if window_id else None)
    if not windows:
        raise RuntimeError(f"No windows found in {WINDOWS_DIR}")
    window = windows[0]
    _logger.info("Bayesian opt: window=%s profile=%s n_trials=%d", window.get("window_id"), profile, n_trials)

    trial_results: List[Dict[str, Any]] = []

    def objective(trial: "optuna.Trial") -> float:
        params: Dict[str, Any] = {}
        for param, values in PARAM_GRID.items():
            if not values:
                continue
            if all(isinstance(v, float) for v in values):
                params[param] = trial.suggest_float(param, min(values), max(values))
            elif all(isinstance(v, int) for v in values):
                params[param] = trial.suggest_int(param, min(values), max(values))
            else:
                params[param] = trial.suggest_categorical(param, values)

        try:
            m = _run_trial(window, profile, params, max_cycles)
        except Exception as e:
            _logger.warning("Trial failed: %s", e)
            raise optuna.exceptions.TrialPruned()

        # Prune bad trials early
        if (m.get("max_drawdown_pct") or 0.0) > MAX_DRAWDOWN_PRUNE:
            raise optuna.exceptions.TrialPruned()
        if (m.get("trades") or 0) < MIN_TRADES_PRUNE:
            raise optuna.exceptions.TrialPruned()

        score = _composite_score(m)
        trial_results.append({"params": params, "metrics": m, "score": score})
        _logger.info(
            "Trial %d: score=%.4f sharpe=%s pf=%s pnl=%.2f dd=%.2f",
            trial.number, score,
            m.get("sharpe"), m.get("profit_factor"), m.get("pnl_usdt", 0), m.get("max_drawdown_pct", 0),
        )
        return score

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=n_trials, timeout=timeout_seconds, catch=(Exception,))

    best = study.best_trial
    best_params = best.params
    best_metrics = next(
        (r["metrics"] for r in trial_results if r["params"] == best_params),
        {}
    )

    # Sort all results for report
    trial_results.sort(key=lambda x: x["score"], reverse=True)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_id": window.get("window_id"),
        "profile": profile,
        "n_trials": n_trials,
        "best_score": best.value,
        "best_params": best_params,
        "best_metrics": best_metrics,
        "top_10": trial_results[:10],
        "all_trials": len(trial_results),
    }

    os.makedirs(os.path.dirname(BAYESIAN_REPORT_PATH), exist_ok=True)
    with open(BAYESIAN_REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    _logger.info("Bayesian report saved to %s", BAYESIAN_REPORT_PATH)

    print(f"\n=== BAYESIAN OPT BEST (profile={profile}) ===")
    print(f"  Score   : {best.value:.4f}")
    print(f"  Sharpe  : {best_metrics.get('sharpe')}")
    print(f"  PF      : {best_metrics.get('profit_factor')}")
    print(f"  PnL     : {best_metrics.get('pnl_usdt')} USDT")
    print(f"  Drawdown: {best_metrics.get('max_drawdown_pct')}%")
    print(f"  Params  : {best_params}")
    return report


def _run_random_search(
    window_id: Optional[str],
    profile: str,
    n_trials: int,
    max_cycles: Optional[int],
) -> Dict[str, Any]:
    import random
    import itertools
    windows = _load_windows([window_id] if window_id else None)
    if not windows:
        raise RuntimeError(f"No windows found in {WINDOWS_DIR}")
    window = windows[0]
    all_combos = list(itertools.product(*PARAM_GRID.values()))
    random.shuffle(all_combos)
    keys = list(PARAM_GRID.keys())
    trial_results = []
    for combo in all_combos[:n_trials]:
        params = dict(zip(keys, combo))
        try:
            m = _run_trial(window, profile, params, max_cycles)
            score = _composite_score(m)
            trial_results.append({"params": params, "metrics": m, "score": score})
        except Exception as e:
            _logger.warning("Trial failed: %s", e)
    trial_results.sort(key=lambda x: x["score"], reverse=True)
    best = trial_results[0] if trial_results else {}
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_id": window.get("window_id"),
        "profile": profile,
        "method": "random_search",
        "n_trials": len(trial_results),
        "best_score": best.get("score"),
        "best_params": best.get("params"),
        "best_metrics": best.get("metrics"),
        "top_10": trial_results[:10],
    }
    os.makedirs(os.path.dirname(BAYESIAN_REPORT_PATH), exist_ok=True)
    with open(BAYESIAN_REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="MIMIC Bayesian parameter optimization")
    parser.add_argument("--window", default=None, help="Window ID (default: first available)")
    parser.add_argument("--profile", default="B", choices=["A", "B", "C"])
    parser.add_argument("--trials", type=int, default=50)
    parser.add_argument("--max-cycles", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=None, help="Timeout in seconds")
    args = parser.parse_args()
    run_bayesian(
        window_id=args.window,
        profile=args.profile,
        n_trials=args.trials,
        max_cycles=args.max_cycles,
        timeout_seconds=args.timeout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
