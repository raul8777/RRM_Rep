"""tune_peak_exit_policy.py

Search over lifecycle ExitPolicy parameters in --peak-signal-mode using the
peak oracle as the upper bound. Keeps entries and sizing fixed; only exit
policy (including the new expected-gain TP and time-stop rules) is varied.

Usage:
    python -m Dev.scripts.tune_peak_exit_policy \
        --n-samples 100 \
        --max-workers 12 \
        --output-dir Dev/benchmarks/peak_lifecycle_tuning
"""

from __future__ import annotations

import argparse
import copy
import itertools
import json
import logging
import math
import os
import random
import sys
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo_root)

import numpy as np

from config import get_settings
from lifecycle import (
    ExitPolicy,
    LifecycleConfig,
    MomentumExitRule,
    PartialTake,
    StagnationRule,
)
from peak_signal_source import build_peak_signal_schedule
from simulation.mimic_lifecycle_adapter import (
    predictive_spot_bull_config,
    run_lifecycle_simulation,
)

logger = logging.getLogger("tune_peak_exit")

WINDOWS_DIR = os.path.join(repo_root, "Dev", "benchmarks", "windows")
OUTPUT_DIR = os.path.join(repo_root, "Dev", "benchmarks", "peak_lifecycle_tuning")
DEFAULT_PEAK_PARQUET = os.path.join(repo_root, "Dev", "benchmarks", "peaks_dataset", "peaks_all.parquet")
DEFAULT_PEAK_MODEL_DIR = os.path.join(repo_root, "Dev", "benchmarks", "peaks_dataset", "peak_quality_model")

PROFILES = ("A", "B", "C")

# Per-profile exit parameter search space.  We deliberately keep partial takes
# and momentum thresholds small / discrete so the random search does not drown
# in dimensions, while the dominant levers (SL, TP, trailing, expected-gain TP,
# time stop, stagnation) are explored broadly.
EXIT_PARAM_SPACE: Dict[str, Dict[str, Any]] = {
    "hard_sl_atr_mult": {"type": "float", "low": 0.5, "high": 4.0},
    "hard_tp_atr_mult": {"type": "float", "low": 1.0, "high": 8.0},
    "trailing_activation_pct": {"type": "float", "low": 0.0, "high": 0.05},
    "trailing_atr_mult": {"type": "float", "low": 0.0, "high": 3.0},
    "breakeven_activation_pct": {"type": "float", "low": 0.0, "high": 0.03},
    "stagnation_min_hours": {"type": "int", "low": 1, "high": 12},
    "stagnation_max_drawdown_from_peak_pct": {"type": "float", "low": 0.01, "high": 0.15},
    "momentum_rsi_overbought": {"type": "int", "low": 65, "high": 90},
    "momentum_rsi_oversold": {"type": "int", "low": 10, "high": 35},
    "momentum_fng_extreme_greed": {"type": "int", "low": 70, "high": 95},
    "momentum_fng_extreme_fear": {"type": "int", "low": 5, "high": 30},
    "partial_take_mode": {
        "type": "choice",
        "choices": [
            "none",
            "tp1_25_at_1.5",
            "tp1_25_at_2.0",
            "tp1_25_at_1.5_tp2_25_at_2.5",
        ],
    },
    "use_expected_gain_for_tp": {"type": "bool"},
    "expected_gain_mult": {"type": "float", "low": 0.3, "high": 1.5},
    "time_stop_hours": {"type": "float", "low": 0.5, "high": 24.0},
}


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def load_windows(windows_dir: str, window_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    windows: List[Dict[str, Any]] = []
    if not os.path.isdir(windows_dir):
        return windows
    for fname in sorted(os.listdir(windows_dir)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(windows_dir, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                w = json.load(f)
            if window_ids is None or w.get("window_id") in window_ids:
                windows.append(w)
        except Exception as e:
            logger.warning("Could not load window %s: %s", path, e)
    return windows


def build_window_schedule(
    windows: List[Dict[str, Any]],
    parquet_path: str,
    model_dir: str,
    n_jobs: int = 12,
    batch_size: int = 200_000,
) -> Dict[str, List[Dict[str, Any]]]:
    window_ids = [w["window_id"] for w in windows]
    schedule = build_peak_signal_schedule(
        parquet_path=parquet_path,
        model_dir=model_dir,
        batch_size=batch_size,
        n_jobs=n_jobs,
        include_profiles=PROFILES,
        only_window_ids=window_ids,
    )
    for w in windows:
        wid = w["window_id"]
        if wid not in schedule:
            schedule[wid] = []
        else:
            # sort by peak_start_dt just in case
            schedule[wid].sort(key=lambda x: x["peak_start_dt"])
    return schedule


def _partial_takes_from_mode(mode: str) -> Tuple[PartialTake, ...]:
    if mode == "none":
        return ()
    if mode == "tp1_25_at_1.5":
        return (PartialTake(0.25, 1.5, "tp1"),)
    if mode == "tp1_25_at_2.0":
        return (PartialTake(0.25, 2.0, "tp1"),)
    if mode == "tp1_25_at_1.5_tp2_25_at_2.5":
        return (
            PartialTake(0.25, 1.5, "tp1"),
            PartialTake(0.25, 2.5, "tp2"),
        )
    return ()


def sample_exit_params(profile: str, rng: random.Random) -> Dict[str, Any]:
    """Sample one set of exit parameters for a profile."""
    params: Dict[str, Any] = {}
    for key, spec in EXIT_PARAM_SPACE.items():
        if spec["type"] == "float":
            params[key] = rng.uniform(spec["low"], spec["high"])
        elif spec["type"] == "int":
            params[key] = rng.randint(spec["low"], spec["high"])
        elif spec["type"] == "bool":
            params[key] = rng.random() < 0.5
        elif spec["type"] == "choice":
            params[key] = rng.choice(spec["choices"])
        else:
            params[key] = None
    return params


def build_exit_policy_from_params(params: Dict[str, Any], profile: str) -> ExitPolicy:
    return ExitPolicy(
        regime="SPOT_BULL",
        profile=profile,
        hard_sl_atr_mult=float(params["hard_sl_atr_mult"]),
        hard_tp_atr_mult=float(params["hard_tp_atr_mult"]),
        trailing_activation_pct=float(params["trailing_activation_pct"]),
        trailing_atr_mult=float(params["trailing_atr_mult"]),
        breakeven_activation_pct=float(params["breakeven_activation_pct"]),
        partial_takes=_partial_takes_from_mode(str(params["partial_take_mode"])),
        momentum_exit=MomentumExitRule(
            rsi_overbought=int(params["momentum_rsi_overbought"]),
            rsi_oversold=int(params["momentum_rsi_oversold"]),
            fng_extreme_greed=int(params["momentum_fng_extreme_greed"]),
            fng_extreme_fear=int(params["momentum_fng_extreme_fear"]),
        ),
        stagnation_rule=StagnationRule(
            min_hours=float(params["stagnation_min_hours"]),
            max_drawdown_from_peak_pct=float(params["stagnation_max_drawdown_from_peak_pct"]),
            decay_score_threshold=0.5,
        ),
        use_expected_gain_for_tp=bool(params["use_expected_gain_for_tp"]),
        expected_gain_mult=float(params["expected_gain_mult"]),
        time_stop_hours=float(params["time_stop_hours"]),
    )


def build_config_from_flat_params(flat_params: Dict[str, Any]) -> LifecycleConfig:
    """Build a LifecycleConfig from a flat parameter dict keyed like A_hard_sl_atr_mult."""
    base = predictive_spot_bull_config()
    profile_exit_policies = {}
    for prof in PROFILES:
        prof_params = {k: flat_params[f"{prof}_{k}"] for k in EXIT_PARAM_SPACE}
        profile_exit_policies[prof] = build_exit_policy_from_params(prof_params, prof)

    return LifecycleConfig(
        regime=base.regime,
        profile=base.profile,
        signal_policy=base.signal_policy,
        entry_policy=base.entry_policy,
        sizing_policy=base.sizing_policy,
        exit_policy=base.exit_policy,
        profile_order=base.profile_order,
        profile_thresholds=base.profile_thresholds,
        profile_sizing_policies=base.profile_sizing_policies,
        profile_exit_policies=profile_exit_policies,
    )


def sample_random_config(rng: random.Random) -> Tuple[LifecycleConfig, Dict[str, Any]]:
    """Return a LifecycleConfig and a flat parameter dict for one random sample."""
    flat: Dict[str, Any] = {}
    for prof in PROFILES:
        prof_params = sample_exit_params(prof, rng)
        for k, v in prof_params.items():
            flat[f"{prof}_{k}"] = v
    return build_config_from_flat_params(flat), flat


def config_to_flat_params(config: LifecycleConfig) -> Dict[str, Any]:
    """Serialize a LifecycleConfig's per-profile exit policies to a flat dict."""
    flat: Dict[str, Any] = {}
    for prof in PROFILES:
        ep = config.profile_exit_policies.get(prof, config.exit_policy)
        prefix = f"{prof}_"
        flat[prefix + "hard_sl_atr_mult"] = ep.hard_sl_atr_mult
        flat[prefix + "hard_tp_atr_mult"] = ep.hard_tp_atr_mult
        flat[prefix + "trailing_activation_pct"] = ep.trailing_activation_pct
        flat[prefix + "trailing_atr_mult"] = ep.trailing_atr_mult
        flat[prefix + "breakeven_activation_pct"] = ep.breakeven_activation_pct
        flat[prefix + "stagnation_min_hours"] = ep.stagnation_rule.min_hours
        flat[prefix + "stagnation_max_drawdown_from_peak_pct"] = ep.stagnation_rule.max_drawdown_from_peak_pct
        flat[prefix + "momentum_rsi_overbought"] = ep.momentum_exit.rsi_overbought
        flat[prefix + "momentum_rsi_oversold"] = ep.momentum_exit.rsi_oversold
        flat[prefix + "momentum_fng_extreme_greed"] = ep.momentum_exit.fng_extreme_greed
        flat[prefix + "momentum_fng_extreme_fear"] = ep.momentum_exit.fng_extreme_fear
        if ep.partial_takes:
            pt = ep.partial_takes
            if len(pt) == 1 and pt[0].pct == 0.25 and abs(pt[0].trigger_atr_mult - 1.5) < 0.01:
                flat[prefix + "partial_take_mode"] = "tp1_25_at_1.5"
            elif len(pt) == 1 and pt[0].pct == 0.25 and abs(pt[0].trigger_atr_mult - 2.0) < 0.01:
                flat[prefix + "partial_take_mode"] = "tp1_25_at_2.0"
            elif len(pt) == 2 and pt[0].pct == 0.25 and abs(pt[0].trigger_atr_mult - 1.5) < 0.01 and pt[1].pct == 0.25 and abs(pt[1].trigger_atr_mult - 2.5) < 0.01:
                flat[prefix + "partial_take_mode"] = "tp1_25_at_1.5_tp2_25_at_2.5"
            else:
                flat[prefix + "partial_take_mode"] = "none"
        else:
            flat[prefix + "partial_take_mode"] = "none"
        flat[prefix + "use_expected_gain_for_tp"] = ep.use_expected_gain_for_tp
        flat[prefix + "expected_gain_mult"] = ep.expected_gain_mult
        flat[prefix + "time_stop_hours"] = ep.time_stop_hours or 0.0
    return flat


def derive_metrics(results: List[Dict[str, Any]], initial_usdt: float) -> Dict[str, Any]:
    """Compute summary metrics from a lifecycle result list."""
    equity = [initial_usdt]
    usdt_bals = [initial_usdt]
    pnl_usdt = 0.0
    max_dd = 0.0
    peak = initial_usdt

    for r in results:
        tv = r.get("total_value", initial_usdt)
        ub = r.get("usdt_balance", initial_usdt)
        equity.append(tv)
        usdt_bals.append(ub)
        peak = max(peak, tv)
        dd = (peak - tv) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    if len(equity) > 1:
        pnl_usdt = equity[-1] - equity[0]
        pnl_pct = pnl_usdt / equity[0] * 100.0
    else:
        pnl_pct = 0.0

    # simple returns per cycle for Sharpe/Calmar approximations
    rets = [(equity[i] - equity[i - 1]) / equity[i - 1] for i in range(1, len(equity)) if equity[i - 1] > 0]
    sharpe = None
    sortino = None
    calmar = None
    if len(rets) > 2:
        mean_ret = sum(rets) / len(rets)
        std_ret = (sum((r - mean_ret) ** 2 for r in rets) / len(rets)) ** 0.5
        if std_ret > 0:
            sharpe = mean_ret / std_ret * (24 * 365) ** 0.5
        downside = [r for r in rets if r < 0]
        if downside:
            downside_std = (sum((r - sum(downside) / len(downside)) ** 2 for r in downside) / len(downside)) ** 0.5
            if downside_std > 0:
                sortino = mean_ret / downside_std * (24 * 365) ** 0.5
        if max_dd > 0:
            calmar = (pnl_pct / 100.0) / max_dd * (365 / max(1, len(results) / 24))

    buys = 0
    sells = 0
    sl_tp = 0
    gross_wins = 0.0
    gross_losses = 0.0
    winning_trades = 0
    total_pnl_trades = 0
    profile_entry_counts: Dict[str, int] = {}

    for r in results:
        summary = r.get("summary", {})
        entries = summary.get("entries", [])
        exits = summary.get("exits", [])
        buys += len(entries)
        sells += len(exits)
        for e in entries:
            prof = e.get("profile") or "UNKNOWN"
            profile_entry_counts[prof] = profile_entry_counts.get(prof, 0) + 1
        idx = results.index(r)
        if idx > 0:
            cycle_pnl = equity[idx + 1] - equity[idx]
            per_exit_pnl = cycle_pnl / max(1, len(exits))
        else:
            per_exit_pnl = 0.0
        for _ in exits:
            total_pnl_trades += 1
            if per_exit_pnl > 0:
                gross_wins += per_exit_pnl
                winning_trades += 1
            else:
                gross_losses += abs(per_exit_pnl)

    profit_factor = round(gross_wins / gross_losses, 4) if gross_losses > 0 else None
    win_rate = round(winning_trades / total_pnl_trades, 4) if total_pnl_trades > 0 else None
    window_days = max(1, len(results) / 24)
    return {
        "pnl_usdt": round(pnl_usdt, 2),
        "pnl_pct": round(pnl_pct, 4),
        "pnl_per_day_usdt": round(pnl_usdt / window_days, 2),
        "max_drawdown_pct": round(max_dd * 100, 4),
        "trades": buys + sells + sl_tp,
        "buys": buys,
        "sells": sells,
        "cycles": len(results),
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "profit_factor": profit_factor,
        "win_rate": win_rate,
        "profile_entry_counts": profile_entry_counts,
    }


def run_single_simulation(
    repo_root: str,
    window: Dict[str, Any],
    config_dict: Dict[str, Any],
    peak_entry_schedule: List[Dict[str, Any]],
    data_dir: str,
    initial_usdt: float = 2910.0,
    max_cycles_per_sim: Optional[int] = None,
) -> Dict[str, Any]:
    """Worker function; runs one lifecycle simulation for one window and config."""
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    from datetime import datetime

    from config import get_settings

    config = build_config_from_flat_params(config_dict)
    settings = get_settings()

    symbols = window.get("symbols", []) or window.get("baseline_symbols", [])
    # restrict to symbols with parquet data (same logic as mimic_mode_profile_sweep)
    available = [s for s in symbols if os.path.exists(os.path.join(data_dir, f"{s}_1h.parquet"))]
    if available:
        symbols = available

    start = datetime.fromisoformat(window["start"])
    end = datetime.fromisoformat(window["end"])

    try:
        raw = run_lifecycle_simulation(
            settings=settings,
            symbols=symbols,
            interval="1h",
            start=start,
            end=end,
            initial_usdt=initial_usdt,
            fear_greed_value=50.0,
            data_dir=data_dir,
            forced_profile="A",
            profile_overrides={},
            max_cycles=max_cycles_per_sim,
            lifecycle_config=config,
            peak_entry_schedule=peak_entry_schedule,
        )
        metrics = derive_metrics(raw.get("results", []), initial_usdt)
    except Exception as e:
        return {
            "window_id": window.get("window_id"),
            "error": str(e),
            "traceback": traceback.format_exc(),
            "pnl_usdt": -1e9,
            "pnl_pct": -100.0,
            "pnl_per_day_usdt": -1e9,
        }

    return {
        "window_id": window.get("window_id"),
        "window_start": window["start"],
        "window_end": window["end"],
        "params": config_dict,
        **metrics,
    }


def config_fingerprint(config_dict: Dict[str, Any]) -> str:
    import hashlib
    payload = json.dumps(config_dict, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def append_result(result: Dict[str, Any], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")


def load_existing_results(path: str) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return results
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                results.append(json.loads(line))
            except Exception:
                pass
    return results


def aggregate_results(results: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Aggregate per-config results across windows and return ranked configs + best."""
    by_fp: Dict[str, Dict[str, Any]] = {}
    for r in results:
        if "error" in r:
            continue
        fp = config_fingerprint(r.get("params", {}))
        if fp not in by_fp:
            by_fp[fp] = {
                "params": r["params"],
                "window_results": [],
            }
        by_fp[fp]["window_results"].append(r)

    ranked = []
    for fp, data in by_fp.items():
        wres = data["window_results"]
        valid = [w for w in wres if "error" not in w]
        if not valid:
            continue
        pnl_per_day = [w.get("pnl_per_day_usdt", 0.0) for w in valid]
        pnl_pct = [w.get("pnl_pct", 0.0) for w in valid]
        max_dd = [w.get("max_drawdown_pct", 100.0) for w in valid]
        trades = sum(w.get("trades", 0) for w in valid)
        profit_factor_values = [w.get("profit_factor") for w in valid if w.get("profit_factor") is not None]
        avg_pf = sum(profit_factor_values) / len(profit_factor_values) if profit_factor_values else 0.0
        # enforce deploy-style gates
        fails_gate = any(w.get("max_drawdown_pct", 100.0) >= 15.0 for w in valid) or any(w.get("trades", 0) < 3 for w in valid) or avg_pf < 1.0
        pnl_per_day_mean = sum(pnl_per_day) / len(pnl_per_day)
        pnl_pct_mean = sum(pnl_pct) / len(pnl_pct)
        max_dd_mean = sum(max_dd) / len(max_dd)

        # primary objective: pnl per day, but heavily penalise gate failures
        objective = pnl_per_day_mean
        if fails_gate:
            objective = objective - 1000.0

        ranked.append({
            "fingerprint": fp,
            "params": data["params"],
            "objective": objective,
            "pnl_per_day_usdt_mean": pnl_per_day_mean,
            "pnl_pct_mean": pnl_pct_mean,
            "max_drawdown_pct_mean": max_dd_mean,
            "total_trades": trades,
            "avg_profit_factor": avg_pf,
            "windows_run": len(valid),
            "window_results": valid,
        })

    ranked.sort(key=lambda x: x["objective"], reverse=True)
    best = ranked[0] if ranked else {}
    return ranked, best


def save_report(ranked: List[Dict[str, Any]], best: Dict[str, Any], output_dir: str) -> None:
    os.makedirs(output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, "tuning_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        # drop heavy window_results from top list for report
        top = []
        for r in ranked[:20]:
            rc = r.copy()
            rc.pop("window_results", None)
            top.append(rc)
        json.dump(
            {
                "best": best.get("fingerprint"),
                "best_params": best.get("params"),
                "best_metrics": {
                    k: v for k, v in best.items()
                    if k not in ("params", "window_results", "fingerprint")
                },
                "top_20": top,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            f,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    logger.info("Saved tuning report to %s", report_path)


def perturb_value(key: str, base: Any, rng: random.Random, perturb: float) -> Any:
    """Return a perturbed copy of a single flat parameter value."""
    spec = EXIT_PARAM_SPACE.get(key, {})
    if key == "partial_take_mode" or (key.endswith("_partial_take_mode") and isinstance(base, str)):
        return rng.choice(EXIT_PARAM_SPACE["partial_take_mode"]["choices"])
    if key.endswith("_use_expected_gain_for_tp"):
        return rng.random() < 0.5
    if isinstance(base, bool):
        return base if rng.random() < 0.7 else not base
    if isinstance(base, int):
        low = int(spec.get("low", 0))
        high = int(spec.get("high", base * 2))
        delta = max(1, int(abs(base) * perturb))
        return rng.randint(max(low, base - delta), min(high, base + delta))
    # float
    low = float(spec.get("low", 0.0))
    high = float(spec.get("high", base * 2 if base > 0 else 1.0))
    delta = abs(base) * perturb
    new = rng.uniform(max(low, base - delta), min(high, base + delta))
    return new


def perturb_configs(
    base_configs: List[Dict[str, Any]],
    n_per_base: int,
    perturb: float,
    seed: int,
) -> List[Dict[str, Any]]:
    """Generate local perturbations around a list of base flat configs."""
    rng = random.Random(seed)
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for base in base_configs:
        for _ in range(n_per_base):
            new = {}
            for k, v in base.items():
                if k.startswith(("A_", "B_", "C_")):
                    # k = "A_hard_sl_atr_mult" => profile_key = hard_sl_atr_mult
                    _, rest = k.split("_", 1)
                    new[k] = perturb_value(rest, v, rng, perturb)
                else:
                    new[k] = v
            fp = config_fingerprint(new)
            if fp in seen:
                continue
            seen.add(fp)
            out.append(new)
    return out


def run_search(
    windows: List[Dict[str, Any]],
    schedule: Dict[str, List[Dict[str, Any]]],
    max_workers: int,
    output_dir: str,
    data_dir: str,
    n_samples: Optional[int] = None,
    configs: Optional[List[Dict[str, Any]]] = None,
    seed: int = 42,
    max_pending: int = 24,
    max_cycles_per_sim: Optional[int] = None,
    initial_usdt: float = 2910.0,
) -> None:
    """Run a random search over exit-policy configurations."""
    os.makedirs(output_dir, exist_ok=True)
    results_path = os.path.join(output_dir, "tuning_results.jsonl")
    existing = load_existing_results(results_path)
    completed_fingerprints: set = set()
    for r in existing:
        if "error" not in r:
            completed_fingerprints.add((config_fingerprint(r.get("params", {})), r.get("window_id")))

    if configs is None:
        assert n_samples is not None, "n_samples required when configs not provided"
        rng = random.Random(seed)
        configs = []
        seen: set = set()
        while len(configs) < n_samples:
            _, flat = sample_random_config(rng)
            fp = config_fingerprint(flat)
            if fp in seen:
                continue
            seen.add(fp)
            configs.append(flat)

    jobs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = [
        (window, cfg) for window in windows for cfg in (configs or [])
    ]
    total = len(jobs)
    logger.info("Total jobs: %d windows x %d configs = %d", len(windows), len(configs), total)

    started = datetime.now(timezone.utc)
    submitted = 0
    completed = 0
    failed = 0

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures: Dict[Any, Tuple[Dict[str, Any], Dict[str, Any]]] = {}

        def _submit_next() -> bool:
            nonlocal submitted
            while submitted < total and len(futures) < max_pending:
                window, cfg = jobs[submitted]
                submitted += 1
                fp = config_fingerprint(cfg)
                wid = window.get("window_id")
                if (fp, wid) in completed_fingerprints:
                    continue
                peak_entry_schedule = schedule.get(wid, [])
                fut = executor.submit(
                    run_single_simulation,
                    repo_root,
                    window,
                    cfg,
                    peak_entry_schedule,
                    data_dir,
                    initial_usdt,
                    max_cycles_per_sim,
                )
                futures[fut] = (window, cfg)
            return len(futures) > 0

        _submit_next()
        while futures:
            done = list(as_completed(futures))
            for fut in done:
                window, cfg = futures.pop(fut)
                try:
                    res = fut.result()
                    append_result(res, results_path)
                    completed_fingerprints.add((config_fingerprint(cfg), res.get("window_id")))
                    if "error" in res:
                        failed += 1
                        logger.warning("Job failed for window %s: %s", res.get("window_id"), res.get("error"))
                    else:
                        completed += 1
                except Exception as e:
                    failed += 1
                    logger.exception("Worker exception: %s", e)
                    res = {
                        "window_id": window.get("window_id"),
                        "error": str(e),
                        "traceback": traceback.format_exc(),
                        "pnl_usdt": -1e9,
                        "pnl_pct": -100.0,
                        "pnl_per_day_usdt": -1e9,
                        "params": cfg,
                    }
                    append_result(res, results_path)

                # progress
                if (completed + failed) % 10 == 0:
                    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                    rate = (completed + failed) / max(1, elapsed)
                    remaining = (total - completed - failed) / max(1, rate * max_workers) if rate > 0 else None
                    logger.info(
                        "Progress: %d/%d completed, %d failed, %d pending, ETA %.0fs",
                        completed,
                        total,
                        failed,
                        len(futures),
                        remaining or 0.0,
                    )

            _submit_next()

    logger.info("Search finished. Completed: %d, failed: %d", completed, failed)


def main() -> None:
    parser = argparse.ArgumentParser(description="Tune lifecycle exit policy for peak MIMIC")
    parser.add_argument("--windows-dir", default=WINDOWS_DIR)
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    parser.add_argument("--peak-parquet", default=DEFAULT_PEAK_PARQUET)
    parser.add_argument("--peak-model-dir", default=DEFAULT_PEAK_MODEL_DIR)
    parser.add_argument("--n-samples", type=int, default=50)
    parser.add_argument("--max-workers", type=int, default=10)
    parser.add_argument("--window-ids", nargs="*", default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-cycles-per-sim", type=int, default=None)
    parser.add_argument("--max-pending", type=int, default=24)
    parser.add_argument("--data-dir", default=os.path.join(repo_root, "historical_data"))
    parser.add_argument("--initial-usdt", type=float, default=2910.0)
    parser.add_argument("--only-analyze", action="store_true")
    parser.add_argument("--refine-from", type=str, default=None, help="Path to tuning_report.json to refine from")
    parser.add_argument("--refine-top-n", type=int, default=10)
    parser.add_argument("--refine-per-base", type=int, default=10)
    parser.add_argument("--refine-perturb", type=float, default=0.3)
    args = parser.parse_args()

    configure_logging()

    if not args.only_analyze:
        windows = load_windows(args.windows_dir, args.window_ids)
        if not windows:
            logger.error("No windows found in %s", args.windows_dir)
            sys.exit(1)
        logger.info("Loaded %d windows", len(windows))

        logger.info("Building peak signal schedule (this may take a minute)...")
        schedule = build_window_schedule(
            windows,
            args.peak_parquet,
            args.peak_model_dir,
            n_jobs=min(12, args.max_workers),
            batch_size=200_000,
        )
        logger.info("Schedule ready for %d windows", len(schedule))

        configs = None
        if args.refine_from:
            with open(args.refine_from, "r", encoding="utf-8") as f:
                report = json.load(f)
            top_params = report.get("top_20", [])
            if not isinstance(top_params, list):
                top_params = [top_params]
            base_configs = [r.get("params", r) if isinstance(r, dict) else r for r in top_params[: args.refine_top_n]]
            if not base_configs:
                best_params = report.get("best_params")
                if best_params:
                    base_configs = [best_params]
            configs = perturb_configs(
                base_configs,
                args.refine_per_base,
                args.refine_perturb,
                seed=args.seed,
            )
            logger.info(
                "Refining from top %d configs with %d perturbations each => %d configs",
                len(base_configs),
                args.refine_per_base,
                len(configs),
            )

        run_search(
            windows,
            schedule,
            max_workers=args.max_workers,
            output_dir=args.output_dir,
            data_dir=args.data_dir,
            n_samples=(args.n_samples if configs is None else None),
            configs=configs,
            seed=args.seed,
            max_pending=args.max_pending,
            max_cycles_per_sim=args.max_cycles_per_sim,
            initial_usdt=args.initial_usdt,
        )

    results = load_existing_results(os.path.join(args.output_dir, "tuning_results.jsonl"))
    ranked, best = aggregate_results(results)
    save_report(ranked, best, args.output_dir)

    if best:
        logger.info("Best fingerprint: %s", best.get("fingerprint"))
        logger.info("Best mean pnl/day: %.2f", best.get("pnl_per_day_usdt_mean"))
        logger.info("Best mean pnl%%: %.2f", best.get("pnl_pct_mean"))
        logger.info("Best mean max DD%%: %.2f", best.get("max_drawdown_pct_mean"))


if __name__ == "__main__":
    main()
