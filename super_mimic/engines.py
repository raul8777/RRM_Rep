"""Engine adapters for Super MIMIC.

Each engine accepts a DesignSpec and produces an EngineResults artifact.
Currently supports the MIMIC mode×profile sweep; backtest, Monte Carlo and
predictor engines are added in later phases.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, repo_root)

from Dev.scripts.mimic_mode_profile_sweep import run_sweep
from super_mimic.core import DesignSpec, EngineResults


def _best_recommendation(report: Dict[str, Any], profile: str) -> Optional[Dict[str, Any]]:
    """Pick the best config recommendation for the requested profile."""
    recommendations = report.get("recommendations", [])
    for rec in recommendations:
        if rec.get("profile") == profile:
            return rec
    if recommendations:
        return recommendations[0]
    return None


def _extract_recommended_params(rec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Flatten a recommendation into a config dict safe for regime_profiles."""
    if not rec:
        return {}
    params = rec.get("best_params") or {}
    overrides = rec.get("best_profile_overrides") or {}
    # Merge overrides under their profile key
    return {**params, **overrides}


def run_mimic_sweep_experiment(design: DesignSpec) -> tuple[EngineResults, Dict[str, Any]]:
    """Run a MIMIC mode×profile sweep and package results.

    Returns the engine results plus the recommended config for the requested
    profile.
    """
    output_dir = design.output_dir or os.path.join(
        repo_root, "Dev", "benchmarks", "super_mimic", "unknown"
    )

    report = run_sweep(
        window_ids=design.windows if design.windows else None,
        strategy=design.strategy,
        max_cycles_per_sim=design.max_cycles,
        max_workers=design.max_workers,
        output_dir=output_dir,
    )

    paths = {
        "status_path": os.path.join(output_dir, "mimic_sweep_status.json"),
        "results_path": os.path.join(output_dir, "mimic_sweep_results.jsonl"),
        "report_path": os.path.join(output_dir, "mimic_sweep_report.json"),
    }

    rec = _best_recommendation(report, design.profile)
    recommended_config = _extract_recommended_params(rec)

    return EngineResults(
        engine="mimic_sweep",
        summary=report.get("summary", {}),
        recommendations=report.get("recommendations", []),
        results=report.get("results", []),
        report_path=paths["report_path"],
        results_path=paths["results_path"],
        status_path=paths["status_path"],
    ), recommended_config


def run_backtest_experiment(design: DesignSpec) -> tuple[EngineResults, Dict[str, Any]]:
    """Run the same sweep infrastructure tagged as a backtest engine.

    In this phase the backtest consumes the same MIMIC windows; future versions
    may call the dedicated 90d backtest modules directly.
    """
    results, recommended_config = run_mimic_sweep_experiment(design)
    results.engine = "backtest"
    results.summary["engine_note"] = "backtest adapter uses MIMIC sweep windows"
    return results, recommended_config


def run_monte_carlo_experiment(design: DesignSpec) -> tuple[EngineResults, Dict[str, Any]]:
    """Run MIMIC sweep and add Monte Carlo bootstrap confidence intervals.

    Bootstraps the raw results to estimate stability of the recommended config.
    """
    import numpy as np

    results, recommended_config = run_mimic_sweep_experiment(design)
    results.engine = "monte_carlo"

    raw = results.results
    metric_keys = ["sharpe", "pnl_per_day_usdt", "drawdown_pct", "win_rate"]
    mc_summary = {}
    for key in metric_keys:
        vals = [float(r[key]) for r in raw if key in r and r[key] is not None]
        if len(vals) < 5:
            continue
        arr = np.array(vals)
        n = len(arr)
        boots = []
        for _ in range(1000):
            sample = np.random.choice(arr, size=n, replace=True)
            boots.append(float(np.mean(sample)))
        boots = np.array(boots)
        mc_summary[key] = {
            "mean": float(np.mean(arr)),
            "std": float(np.std(arr)),
            "ci_low": float(np.percentile(boots, 5)),
            "ci_high": float(np.percentile(boots, 95)),
        }

    results.summary["monte_carlo"] = mc_summary
    results.summary["engine_note"] = "Monte Carlo bootstrap over MIMIC sweep results"
    return results, recommended_config
