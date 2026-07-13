"""MIMIC Mode × Profile sweep over continuous market windows.

This sweep:
- Reads simulation windows built by mimic_build_window.py.
- Keeps regime detection dynamic (production-like), but forces the profile A/B/C.
- Can vary per-mode/per-profile cycle durations and entry parameters.
- Writes incremental results so it can be paused/resumed and monitored from the dashboard.
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import sys
import traceback
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo_root)

from config import get_settings
from simulation.mimic_runner import SimulationRunner
from simulation.mimic_lifecycle_adapter import run_lifecycle_simulation
from simulation.mimic_cache import get_cache as _get_sweep_cache


# Default paths (preserved for CLI usage)
_DEFAULT_LOG_PATH = os.path.join(repo_root, "Dev", "benchmarks", "mimic_sweep.log")
_DEFAULT_WINDOWS_DIR = os.path.join(repo_root, "Dev", "benchmarks", "windows")
_DEFAULT_STATUS_PATH = os.path.join(repo_root, "Dev", "benchmarks", "mimic_sweep_status.json")
_DEFAULT_REPORT_PATH = os.path.join(repo_root, "Dev", "benchmarks", "mimic_sweep_report.json")
_DEFAULT_RESULTS_PATH = os.path.join(repo_root, "Dev", "benchmarks", "mimic_sweep_results.jsonl")

_logger: Optional[logging.Logger] = None


def _configure_logging(log_path: str, mode: str = "w") -> logging.Logger:
    """Configure (or reconfigure) sweep logging to a specific path."""
    global _logger
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    logger = logging.getLogger("mimic_sweep")
    logger.setLevel(logging.INFO)
    # Avoid duplicate handlers if already configured
    if not logger.handlers:
        logger.addHandler(logging.FileHandler(log_path, mode=mode))
        logger.addHandler(logging.StreamHandler(sys.stdout))
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    for handler in logger.handlers:
        handler.setFormatter(fmt)
    _logger = logger
    return logger


WINDOWS_DIR = _DEFAULT_WINDOWS_DIR
STATUS_PATH = _DEFAULT_STATUS_PATH
REPORT_PATH = _DEFAULT_REPORT_PATH
RESULTS_PATH = _DEFAULT_RESULTS_PATH

PROFILES = ["A", "B", "C"]

# Grid applied on top of base settings. Per-mode/per-profile cycle overrides
# can be injected via profile_overrides (see _build_profile_overrides).
PARAM_GRID = {
    "min_signal_strength": [0.45, 0.50, 0.55, 0.60],
    "min_success_probability": [0.45, 0.50, 0.55, 0.60],
    "stop_loss_pct": [2.5, 3.36, 4.5],
    "kelly_max_usdt": [800.0, 1000.0, 1200.0],
    "kelly_dynamic_base_pct": [0.18, 0.22, 0.26],
    "max_positions_spot_bull": [5, 8],
}

# Cycle variations explored per profile. Production already has cycle_spot_bull etc.
# Here we add a profile multiplier to explore faster/slower cycles.
CYCLE_PROFILE_MULTIPLIERS = {
    "A": 0.80,   # aggressive: shorter cycles, more opportunities, more fees
    "B": 1.00,   # baseline
    "C": 1.25,   # conservative: longer cycles, fewer fees
}


def _load_windows(window_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Load window manifests from Dev/benchmarks/windows."""
    if not os.path.isdir(WINDOWS_DIR):
        return []
    windows = []
    for fname in sorted(os.listdir(WINDOWS_DIR)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(WINDOWS_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                w = json.load(f)
            if window_ids is None or w.get("window_id") in window_ids:
                windows.append(w)
        except Exception as e:
            _logger.warning("Could not load window %s: %s", path, e)
    return windows


def _clear_global_caches() -> None:
    try:
        from autonomous_signals import AutonomousSignalGenerator
        AutonomousSignalGenerator._signal_cache.clear()
    except Exception:
        pass
    try:
        from analysis_cache import analysis_cache
        analysis_cache.clear()
    except Exception:
        pass
    try:
        from pair_screener import invalidate_cache
        invalidate_cache()
    except Exception:
        pass


def _build_profile_overrides(
    profile: str,
    base_cycles: Optional[Dict[str, int]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Build per-mode/per-profile overrides including cycle durations."""
    mult = CYCLE_PROFILE_MULTIPLIERS.get(profile, 1.0)
    cycles = base_cycles or {
        "SPOT_BULL": 10,
        "RANGE_SCALP": 3,
        "PRESERVATION": 45,
        "FUTURES_SHORT": 4,
    }
    overrides: Dict[str, Dict[str, Any]] = {}
    for mode in cycles:
        overrides[f"{mode}_{profile}"] = {
            "cycle_spot_bull": max(1, int(round(cycles["SPOT_BULL"] * mult))),
            "cycle_range_scalp": max(1, int(round(cycles["RANGE_SCALP"] * mult))),
            "cycle_preservation": max(1, int(round(cycles["PRESERVATION"] * mult))),
            "cycle_futures_short": max(1, int(round(cycles["FUTURES_SHORT"] * mult))),
        }
    return overrides


def _base_settings(profile: str, symbols: List[str]) -> Any:
    settings = get_settings()
    settings.dry_run = True
    settings.allow_real_trading = False
    settings.binance_use_testnet = True
    settings.agent_mode = "auto"
    settings.market_regime_enabled = True
    settings.spot_require_confirmed_regime = True
    settings.futures_enabled = True
    settings.screener_enabled = False
    settings.screener_top_n_enabled = False
    settings.category_max_positions_enabled = True
    settings.current_regime_profile_label = profile
    settings.min_signal_strength = 0.55
    settings.min_success_probability = 0.55
    settings.stop_loss_pct = 3.36
    settings.kelly_max_usdt = 1000.0
    settings.kelly_base_usdt = 100.0
    # Restrict all symbol lists to the historical universe so the simulation only
    # analyzes symbols for which we have parquet data.
    settings.analysis_symbols = list(symbols)
    settings.futures_symbols = [s for s in symbols if s in ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT")] or list(symbols)
    settings.narrative_tokens = [s for s in symbols if s in settings.narrative_tokens] or list(symbols)
    return settings


def _derive_metrics(results: List[Dict[str, Any]], initial_usdt: float) -> Dict[str, Any]:
    """Compute full risk/return metrics from SimulationRunner cycle results."""
    import math

    _empty = {
        "pnl_usdt": 0.0, "pnl_pct": 0.0, "max_drawdown_pct": 0.0,
        "trades": 0, "buys": 0, "sells": 0, "sl_tp": 0, "cycles": 0,
        "pnl_per_day_usdt": 0.0, "regime_distribution": {},
        "sharpe": None, "sortino": None, "calmar": None,
        "profit_factor": None, "win_rate": None,
        "capital_deployed_ratio": None, "pnl_by_regime": {},
        "transition_cost_pct": None,
    }
    if not results:
        return _empty

    equity = [r.get("total_value", initial_usdt) for r in results]
    usdt_bals = [r.get("usdt_balance", None) for r in results]
    final_value = equity[-1]
    pnl_usdt = final_value - initial_usdt
    pnl_pct = pnl_usdt / initial_usdt * 100.0

    # Max drawdown
    peak = equity[0]
    max_dd = 0.0
    for val in equity:
        if val > peak:
            peak = val
        dd = (peak - val) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    # Per-cycle returns for Sharpe/Sortino
    returns = []
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        if prev > 0:
            returns.append((equity[i] - prev) / prev)
    sharpe = sortino = None
    if len(returns) >= 5:
        avg_r = sum(returns) / len(returns)
        std_r = math.sqrt(sum((r - avg_r) ** 2 for r in returns) / len(returns))
        # Annualise: assume each cycle is avg_cycle_minutes long
        avg_cycle_minutes = 60.0  # default
        ncm = [r.get("next_cycle_minutes") for r in results if r.get("next_cycle_minutes")]
        if ncm:
            avg_cycle_minutes = sum(ncm) / len(ncm)
        cycles_per_year = 525600.0 / avg_cycle_minutes  # minutes in year
        ann_factor = math.sqrt(cycles_per_year)
        if std_r > 0:
            sharpe = round(avg_r / std_r * ann_factor, 4)
        downside = [r for r in returns if r < 0]
        if downside:
            down_std = math.sqrt(sum(r ** 2 for r in downside) / len(downside))
            if down_std > 0:
                sortino = round(avg_r / down_std * ann_factor, 4)

    # Calmar
    calmar = None
    if max_dd > 0:
        calmar = round(pnl_pct / (max_dd * 100.0), 4)

    # Trade counts + profit factor + win rate
    buys = sells = sl_tp = 0
    gross_wins = gross_losses = 0.0
    winning_trades = total_pnl_trades = 0
    for r in results:
        res = r.get("result", {})
        bo = res.get("buy_orders", []) or []
        so = res.get("sell_orders", []) or []
        st = res.get("sl_tp_closes", []) or []
        buys += len(bo)
        sells += len(so)
        sl_tp += len(st)
        for order in so + st:
            pnl = float(order.get("pnl_usdt", order.get("pnl", 0.0)) or 0.0)
            total_pnl_trades += 1
            if pnl > 0:
                gross_wins += pnl
                winning_trades += 1
            else:
                gross_losses += abs(pnl)

    profit_factor = round(gross_wins / gross_losses, 4) if gross_losses > 0 else None
    win_rate = round(winning_trades / total_pnl_trades, 4) if total_pnl_trades > 0 else None

    # Capital deployed ratio (avg fraction of portfolio not in USDT)
    capital_deployed_ratio = None
    valid_bals = [(eq, ub) for eq, ub in zip(equity, usdt_bals) if ub is not None and eq > 0]
    if valid_bals:
        ratios = [(eq - ub) / eq for eq, ub in valid_bals]
        capital_deployed_ratio = round(sum(ratios) / len(ratios), 4)

    # PnL by regime (cumulative equity change while in each regime)
    pnl_by_regime: Dict[str, Dict[str, Any]] = {}
    regime_dist: Dict[str, int] = {}
    for i, r in enumerate(results):
        mode = r.get("regime_mode", "UNKNOWN")
        regime_dist[mode] = regime_dist.get(mode, 0) + 1
        if i == 0:
            continue
        cycle_pnl = equity[i] - equity[i - 1]
        if mode not in pnl_by_regime:
            pnl_by_regime[mode] = {"pnl_usdt": 0.0, "cycles": 0}
        pnl_by_regime[mode]["pnl_usdt"] += cycle_pnl
        pnl_by_regime[mode]["cycles"] += 1
    for mode in pnl_by_regime:
        pnl_by_regime[mode]["pnl_usdt"] = round(pnl_by_regime[mode]["pnl_usdt"], 2)

    # Transition cost: avg equity change in cycle immediately following a regime change
    transition_pnls = []
    for i in range(1, len(results)):
        prev_mode = results[i - 1].get("regime_mode")
        curr_mode = results[i].get("regime_mode")
        if prev_mode and curr_mode and prev_mode != curr_mode and i + 1 < len(equity):
            transition_pnls.append((equity[i + 1] - equity[i]) / equity[i] * 100.0)
    transition_cost_pct = round(sum(transition_pnls) / len(transition_pnls), 4) if transition_pnls else None

    window_days = max(1, len(results) / 24)
    return {
        "pnl_usdt": round(pnl_usdt, 2),
        "pnl_pct": round(pnl_pct, 4),
        "max_drawdown_pct": round(max_dd * 100, 4),
        "trades": buys + sells + sl_tp,
        "buys": buys,
        "sells": sells,
        "sl_tp": sl_tp,
        "cycles": len(results),
        "pnl_per_day_usdt": round(pnl_usdt / window_days, 2),
        "regime_distribution": regime_dist,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "profit_factor": profit_factor,
        "win_rate": win_rate,
        "capital_deployed_ratio": capital_deployed_ratio,
        "pnl_by_regime": pnl_by_regime,
        "transition_cost_pct": transition_cost_pct,
    }


def _check_early_stop(
    cycle_results: List[Dict[str, Any]],
    initial_usdt: float,
    max_drawdown_stop_pct: float = 15.0,
    min_cycles_before_stop: int = 10,
) -> bool:
    """D15: Return True if accumulated drawdown exceeds the stop threshold.

    Only evaluated after ``min_cycles_before_stop`` cycles have elapsed so
    short windows are not killed prematurely by noise.
    """
    if len(cycle_results) < min_cycles_before_stop:
        return False
    equity = [r.get("total_value", initial_usdt) for r in cycle_results]
    peak = equity[0]
    for val in equity:
        if val > peak:
            peak = val
        dd_pct = (peak - val) / peak * 100.0 if peak > 0 else 0.0
        if dd_pct >= max_drawdown_stop_pct:
            return True
    return False


def _run_single_config(
    window: Dict[str, Any],
    profile: str,
    params: Dict[str, Any],
    profile_overrides: Dict[str, Dict[str, Any]],
    max_cycles: Optional[int] = None,
    max_drawdown_stop_pct: float = 15.0,
    lifecycle: bool = False,
) -> Dict[str, Any]:
    if _logger is None:
        _configure_logging(_DEFAULT_LOG_PATH)
    _logger.info("Running window=%s profile=%s lifecycle=%s params=%s", window.get("window_id"), profile, lifecycle, params)
    _clear_global_caches()

    _all_symbols = window.get("symbols", []) or window.get("baseline_symbols", [])
    _data_dir = os.path.join(repo_root, "historical_data")
    symbols = [s for s in _all_symbols if os.path.exists(os.path.join(_data_dir, f"{s}_1h.parquet"))]
    if not symbols:
        symbols = _all_symbols
    settings = _base_settings(profile, symbols)
    for key, value in params.items():
        setattr(settings, key, value)

    start = datetime.fromisoformat(window["start"])
    end = datetime.fromisoformat(window["end"])

    data_dir = os.path.join(repo_root, "historical_data")
    initial_usdt_val = 2910.0

    if lifecycle:
        raw_result = run_lifecycle_simulation(
            settings=settings,
            symbols=symbols,
            interval="1h",
            start=start,
            end=end,
            initial_usdt=initial_usdt_val,
            fear_greed_value=50.0,
            data_dir=data_dir,
            forced_profile=profile,
            profile_overrides=profile_overrides,
            max_cycles=max_cycles,
        )
        metrics = _derive_metrics(raw_result.get("results", []), initial_usdt_val)
        return {
            "window_id": window.get("window_id"),
            "window_start": window["start"],
            "window_end": window["end"],
            "profile": profile,
            "params": params,
            "profile_overrides": profile_overrides,
            "early_stopped": False,
            "lifecycle": True,
            **metrics,
        }

    runner = SimulationRunner(
        settings=settings,
        symbols=symbols,
        interval="1h",
        start=start,
        end=end,
        initial_usdt=initial_usdt_val,
        fear_greed_value=50.0,
        data_dir=data_dir,
        forced_profile=profile,
        profile_overrides=profile_overrides,
    )

    # D15: Run with early-stop monitoring instead of runner.run() in one shot
    components = runner.build_components()
    cycle = 0
    current = start
    next_cycle_minutes: Optional[int] = None
    last_successful_step: Optional[int] = None
    early_stopped = False
    while current <= end:
        runner.clock._now = current
        runner._invalidate_caches()
        runner._inject_historical_screener()
        runner.news_client.update_for_date(current)
        try:
            result = runner.run_cycle(components=components)
            _balance = runner.spot_client.get_account_balance()
            _total = float(_balance.get("USDT", 0.0))
            for _asset, _qty in _balance.items():
                if _asset == "USDT":
                    continue
                _price = runner.spot_client.get_symbol_price(f"{_asset}USDT", use_cache=False)
                _total += float(_qty) * _price
            _mode = str(result.get("regime_mode", "SPOT_BULL")).upper()
            if runner._last_mode is not None and _mode != runner._last_mode:
                runner._transition_log.append({
                    "cycle": cycle,
                    "timestamp": runner.clock.now().isoformat(),
                    "from_mode": runner._last_mode,
                    "to_mode": _mode,
                })
            runner._last_mode = _mode
            _usdt_balance = float(_balance.get("USDT", 0.0))
            runner.results.append({
                "cycle": cycle,
                "timestamp": runner.clock.now().isoformat(),
                "result": result,
                "total_value": _total,
                "usdt_balance": _usdt_balance,
                "regime_mode": _mode,
                "next_cycle_minutes": result.get("next_cycle_minutes"),
            })
            next_cycle_minutes = result.get("next_cycle_minutes")
            if next_cycle_minutes is not None:
                last_successful_step = next_cycle_minutes
        except Exception as e:
            _logger.debug("Cycle %d failed: %s", cycle, e)
            next_cycle_minutes = None

        cycle += 1
        step_minutes = next_cycle_minutes or last_successful_step
        if step_minutes is None:
            step_minutes = 60
        current = current + timedelta(minutes=step_minutes)
        if max_cycles is not None and cycle >= max_cycles:
            break

        # D15: Early stopping check every 5 cycles
        if cycle % 5 == 0 and _check_early_stop(runner.results, initial_usdt_val, max_drawdown_stop_pct):
            _logger.info("Early stop at cycle %d: drawdown exceeded %.1f%%", cycle, max_drawdown_stop_pct)
            early_stopped = True
            break

    raw_result = {
        "cycles": cycle,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "final_balance": runner.spot_client.get_account_balance(),
        "results": runner.results,
        "transitions": runner._transition_log,
    }
    metrics = _derive_metrics(raw_result.get("results", []), initial_usdt_val)
    return {
        "window_id": window.get("window_id"),
        "window_start": window["start"],
        "window_end": window["end"],
        "profile": profile,
        "params": params,
        "profile_overrides": profile_overrides,
        "early_stopped": early_stopped,
        **metrics,
        "transitions": raw_result.get("transitions", []),
    }


def _save_status(status: Dict[str, Any], status_path: str) -> None:
    os.makedirs(os.path.dirname(status_path), exist_ok=True)
    with open(status_path, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2, ensure_ascii=False)


def _append_result(result: Dict[str, Any], results_path: str) -> None:
    os.makedirs(os.path.dirname(results_path), exist_ok=True)
    with open(results_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")


def _load_existing_results(results_path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(results_path):
        return []
    results = []
    with open(results_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                results.append(json.loads(line))
    return results


def _already_ran(
    existing: List[Dict[str, Any]],
    window_id: str,
    profile: str,
    params: Dict[str, Any],
) -> bool:
    key = (window_id, profile, json.dumps(params, sort_keys=True))
    for r in existing:
        if (
            r.get("window_id") == window_id
            and r.get("profile") == profile
            and json.dumps(r.get("params", {}), sort_keys=True) == key[2]
        ):
            return True
    return False


@dataclass
class SweepPaths:
    """Output paths for an isolated sweep experiment."""
    status_path: str
    results_path: str
    report_path: str
    log_path: str

    @classmethod
    def from_output_dir(cls, output_dir: str) -> "SweepPaths":
        os.makedirs(output_dir, exist_ok=True)
        return cls(
            status_path=os.path.join(output_dir, "mimic_sweep_status.json"),
            results_path=os.path.join(output_dir, "mimic_sweep_results.jsonl"),
            report_path=os.path.join(output_dir, "mimic_sweep_report.json"),
            log_path=os.path.join(output_dir, "mimic_sweep.log"),
        )


def run_sweep(
    window_ids: Optional[List[str]] = None,
    strategy: str = "full",
    max_cycles_per_sim: Optional[int] = None,
    max_workers: int = 1,
    max_drawdown_stop_pct: float = 15.0,
    use_cache: bool = True,
    output_dir: Optional[str] = None,
    status_path: Optional[str] = None,
    results_path: Optional[str] = None,
    report_path: Optional[str] = None,
    log_path: Optional[str] = None,
    lifecycle: bool = False,
) -> Dict[str, Any]:
    """Run the profile sweep across all configured windows.

    Paths can be supplied individually or via ``output_dir`` for isolated
    experiments. When omitted, the historical global defaults are used so the
    CLI keeps working exactly as before.
    """
    # Resolve output paths
    if output_dir:
        paths = SweepPaths.from_output_dir(output_dir)
    else:
        paths = SweepPaths(
            status_path=status_path or _DEFAULT_STATUS_PATH,
            results_path=results_path or _DEFAULT_RESULTS_PATH,
            report_path=report_path or _DEFAULT_REPORT_PATH,
            log_path=log_path or _DEFAULT_LOG_PATH,
        )

    logger = _configure_logging(paths.log_path)

    windows = _load_windows(window_ids)
    if not windows:
        raise RuntimeError(f"No windows found in {WINDOWS_DIR}")

    if strategy == "quick":
        grid = {k: [v[0]] for k, v in PARAM_GRID.items()}
    elif strategy == "coarse":
        grid = {
            k: [v[0], v[-1]] if len(v) > 1 else v
            for k, v in PARAM_GRID.items()
        }
    elif strategy == "exhaustive":
        grid = PARAM_GRID
        grid = {**grid, "min_signal_strength": [0.45, 0.50, 0.55, 0.60]}
    elif strategy == "lifecycle-quick":
        # Focused grid for lifecycle kernel baseline: best SPOT_BULL-A params + neighbors
        grid = {
            "min_signal_strength": [0.50, 0.55, 0.60],
            "min_success_probability": [0.50, 0.55, 0.60],
            "stop_loss_pct": [2.5, 4.5],
            "kelly_max_usdt": [800.0, 1000.0, 1200.0],
            "kelly_dynamic_base_pct": [0.18, 0.22],
            "max_positions_spot_bull": [5, 8],
        }
    else:  # full
        grid = PARAM_GRID

    param_keys = list(grid.keys())
    param_values = [grid[k] for k in param_keys]
    param_combos = [dict(zip(param_keys, combo)) for combo in itertools.product(*param_values)]

    jobs = []
    for window in windows:
        for profile in PROFILES:
            for params in param_combos:
                jobs.append({
                    "window": window,
                    "profile": profile,
                    "params": params,
                })

    _cache = _get_sweep_cache() if use_cache else None
    existing = _load_existing_results(paths.results_path)
    total = len(jobs)
    completed_initial = len(existing)
    status = {
        "state": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "total_jobs": total,
        "completed_jobs": completed_initial,
        "failed_jobs": 0,
        "errors": [],
        "eta_seconds": None,
        "current_job": None,
    }
    _save_status(status, paths.status_path)

    results: List[Dict[str, Any]] = list(existing)
    started = datetime.now(timezone.utc)

    def _update_eta(submitted: int):
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        ran = max(1, submitted)
        avg = elapsed / ran
        remaining = avg * (total - submitted)
        status["eta_seconds"] = int(remaining)
        _save_status(status, paths.status_path)

    if max_workers == 1:
        for i, job in enumerate(jobs, 1):
            window = job["window"]
            profile = job["profile"]
            params = job["params"]
            window_id = window.get("window_id")

            if _already_ran(existing, window_id, profile, params):
                _logger.info("Skipping already-run job %s/%s: %s %s %s", i, total, window_id, profile, params)
                continue

            status["current_job"] = {
                "index": i,
                "window_id": window_id,
                "profile": profile,
                "params": params,
            }
            _save_status(status, paths.status_path)

            try:
                profile_overrides = _build_profile_overrides(profile)
                # Warm-start: check SQLite cache before running simulation
                _cache_key = _cache.make_key(window_id, profile, params) if _cache else None
                cached_res = _cache.get(_cache_key) if _cache and _cache_key else None
                if cached_res is not None:
                    _logger.info("Cache hit for job %s/%s: %s %s", i, total, window_id, profile)
                    res = cached_res
                else:
                    res = _run_single_config(
                        window=window,
                        profile=profile,
                        params=params,
                        profile_overrides=profile_overrides,
                        max_cycles=max_cycles_per_sim,
                        max_drawdown_stop_pct=max_drawdown_stop_pct,
                        lifecycle=lifecycle,
                    )
                    if _cache and _cache_key:
                        _cache.put(_cache_key, res)
                results.append(res)
                _append_result(res, paths.results_path)
                status["completed_jobs"] = len(results)
            except Exception as e:
                status["failed_jobs"] += 1
                err = {
                    "job_index": i,
                    "window_id": window_id,
                    "profile": profile,
                    "params": params,
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                }
                status["errors"].append(err)
                _logger.exception("Job failed: %s", err)

            _update_eta(i)
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed

        futures_to_job = {}
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            for i, job in enumerate(jobs, 1):
                window = job["window"]
                profile = job["profile"]
                params = job["params"]
                window_id = window.get("window_id")

                if _already_ran(existing, window_id, profile, params):
                    _logger.info("Skipping already-run job %s/%s: %s %s %s", i, total, window_id, profile, params)
                    continue

                profile_overrides = _build_profile_overrides(profile)
                fut = executor.submit(
                    _run_single_config,
                    window,
                    profile,
                    params,
                    profile_overrides,
                    max_cycles_per_sim,
                    max_drawdown_stop_pct,
                    lifecycle,
                )
                futures_to_job[fut] = (i, window_id, profile, params)

            total = len(futures_to_job)
            status["total_jobs"] = total
            _save_status(status, paths.status_path)

            for fut in as_completed(futures_to_job):
                i, window_id, profile, params = futures_to_job[fut]
                try:
                    res = fut.result()
                    results.append(res)
                    _append_result(res, paths.results_path)
                    status["completed_jobs"] = len(results)
                except Exception as e:
                    status["failed_jobs"] += 1
                    err = {
                        "job_index": i,
                        "window_id": window_id,
                        "profile": profile,
                        "params": params,
                        "error": str(e),
                        "traceback": traceback.format_exc(),
                    }
                    status["errors"].append(err)
                    _logger.exception("Job failed: %s", err)

                status["current_job"] = {
                    "index": i,
                    "window_id": window_id,
                    "profile": profile,
                    "params": params,
                }
                _update_eta(status["completed_jobs"] + status["failed_jobs"])

    report = _build_report(results)
    with open(paths.report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    status["state"] = "completed"
    status["completed_at"] = datetime.now(timezone.utc).isoformat()
    status["current_job"] = None
    status["eta_seconds"] = 0
    _save_status(status, paths.status_path)

    return report


def _btc_benchmark_pct(valid: List[Dict[str, Any]]) -> Optional[float]:
    """Estimate BTC buy-and-hold return for the most common window in valid results."""
    try:
        import os
        import pandas as pd
        data_dir = os.path.join(repo_root, "historical_data")
        btc_file = None
        for fname in os.listdir(data_dir):
            if fname.startswith("BTCUSDT_1h") and fname.endswith(".parquet"):
                btc_file = os.path.join(data_dir, fname)
                break
        if not btc_file:
            return None
        df = pd.read_parquet(btc_file)
        if df.empty or "timestamp" not in df.columns:
            return None
        # Use window dates from first result
        sample = valid[0]
        w_start = sample.get("window_start")
        w_end = sample.get("window_end")
        if not w_start or not w_end:
            return None
        ts_start = pd.Timestamp(w_start, tz="UTC")
        ts_end = pd.Timestamp(w_end, tz="UTC")
        mask = (df["timestamp"] >= ts_start) & (df["timestamp"] <= ts_end)
        sub = df[mask]
        if len(sub) < 2:
            return None
        p_start = float(sub.iloc[0]["open"])
        p_end = float(sub.iloc[-1]["close"])
        if p_start <= 0:
            return None
        return round((p_end - p_start) / p_start * 100.0, 4)
    except Exception:
        return None


def _safe_sharpe(r: Dict[str, Any]) -> Optional[float]:
    """Return a sane Sharpe ratio, capping anomalies from near-zero std."""
    s = r.get("sharpe")
    if s is None:
        return None
    try:
        s = float(s)
        if abs(s) > 100:
            return None
        return s
    except Exception:
        return None


def _best_result(group: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pick the best result by total real PnL with sensible fallbacks.

    Prefer configs with positive total PnL (pnl_usdt) and at least 3 trades,
    the same thresholds enforced by the Super MIMIC deploy gate. Fall back to
    >= 1 trade, then any result, so we never return empty.
    """
    if not group:
        return None
    candidates = [
        r for r in group
        if r.get("trades", 0) >= 3 and r.get("pnl_usdt", 0.0) > 0.0
    ]
    if not candidates:
        candidates = [r for r in group if r.get("trades", 0) >= 1]
    if not candidates:
        candidates = group
    # Primary: total real PnL over the backtest window.
    # Secondary: safe sharpe as a tie-breaker.
    candidates.sort(
        key=lambda x: (
            x.get("pnl_usdt", -1e9),
            _safe_sharpe(x) or -1e9,
        ),
        reverse=True,
    )
    return candidates[0]


def _build_report(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    valid = [r for r in results if "pnl_usdt" in r]

    # BTC benchmark for alpha calculation
    btc_bm = _btc_benchmark_pct(valid) if valid else None

    # Summary stats (cap anomalous sharpes before summarising)
    pnl_pcts = [r.get("pnl_pct", 0.0) for r in valid]
    sharpes = [s for s in (_safe_sharpe(r) for r in valid) if s is not None]
    pfs = [r.get("profit_factor") for r in valid if r.get("profit_factor") is not None]
    summary = {
        "total_runs": len(valid),
        "best_pnl_usdt": max((r.get("pnl_usdt", -1e9) for r in valid), default=None),
        "best_pnl_pct": max(pnl_pcts, default=None),
        "worst_pnl_pct": min(pnl_pcts, default=None),
        "avg_pnl_pct": round(sum(pnl_pcts) / len(pnl_pcts), 4) if pnl_pcts else None,
        "best_sharpe": max(sharpes, default=None),
        "avg_sharpe": round(sum(sharpes) / len(sharpes), 4) if sharpes else None,
        "best_profit_factor": max(pfs, default=None),
        "avg_profit_factor": round(sum(pfs) / len(pfs), 4) if pfs else None,
        "btc_benchmark_pct": btc_bm,
        "alpha_vs_btc_avg": round(sum(pnl_pcts) / len(pnl_pcts) - btc_bm, 4) if pnl_pcts and btc_bm is not None else None,
    }

    by_profile: Dict[str, List[Dict[str, Any]]] = {p: [] for p in PROFILES}
    for r in valid:
        by_profile[r.get("profile", "B")].append(r)

    recommendations = []
    for profile in PROFILES:
        group = by_profile[profile]
        if not group:
            continue
        # Best by PnL/day, preferring configs that survive the Super MIMIC deploy gate
        best_pnl = _best_result(group)
        if best_pnl is None:
            continue

        # Also find best by Sharpe (with min 3 trades and sane sharpe)
        sharpe_group = [
            r for r in group
            if _safe_sharpe(r) is not None and r.get("trades", 0) >= 3
        ]
        sharpe_group.sort(key=lambda x: _safe_sharpe(x) or -1e9, reverse=True)
        best_sharpe_r = sharpe_group[0] if sharpe_group else best_pnl

        # Also find best by profit factor
        pf_group = [
            r for r in group
            if r.get("profit_factor") is not None and r.get("trades", 0) >= 3
        ]
        pf_group.sort(key=lambda x: x.get("profit_factor") or -1e9, reverse=True)
        best_pf_r = pf_group[0] if pf_group else best_pnl

        recommendations.append({
            "profile": profile,
            # Best by PnL/day
            "best_window": best_pnl.get("window_id"),
            "best_params": best_pnl.get("params"),
            "best_profile_overrides": best_pnl.get("profile_overrides"),
            "best_pnl_usdt": best_pnl.get("pnl_usdt"),
            "best_pnl_per_day_usdt": best_pnl.get("pnl_per_day_usdt"),
            "best_drawdown_pct": best_pnl.get("max_drawdown_pct"),
            "best_trades": best_pnl.get("trades"),
            "best_win_rate": best_pnl.get("win_rate"),
            "best_profit_factor": best_pnl.get("profit_factor"),
            "best_sharpe": _safe_sharpe(best_pnl),
            "best_capital_deployed": best_pnl.get("capital_deployed_ratio"),
            "best_pnl_by_regime": best_pnl.get("pnl_by_regime"),
            "alpha_vs_btc": round(best_pnl.get("pnl_pct", 0.0) - btc_bm, 4) if btc_bm is not None else None,
            "regime_distribution": best_pnl.get("regime_distribution"),
            # Best by Sharpe
            "sharpe_best_params": best_sharpe_r.get("params"),
            "sharpe_best_value": _safe_sharpe(best_sharpe_r),
            "sharpe_best_pnl_pct": best_sharpe_r.get("pnl_pct"),
            "sharpe_best_drawdown_pct": best_sharpe_r.get("max_drawdown_pct"),
            # Best by profit factor
            "pf_best_params": best_pf_r.get("params"),
            "pf_best_value": best_pf_r.get("profit_factor"),
            "pf_best_pnl_pct": best_pf_r.get("pnl_pct"),
        })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "recommendations": recommendations,
        "results": valid,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="MIMIC continuous-window profile sweep")
    parser.add_argument("--windows", default=None, help="Comma-separated window IDs")
    parser.add_argument("--strategy", choices=["quick", "coarse", "full", "exhaustive", "lifecycle-quick"], default="full")
    parser.add_argument("--max-cycles", type=int, default=None)
    parser.add_argument("--max-workers", type=int, default=1, help="Parallel workers for simulations (default 1)")
    parser.add_argument("--max-dd-stop", type=float, default=15.0, help="Early-stop drawdown threshold %% (default 15.0, 0=disable)")
    parser.add_argument("--lifecycle", action="store_true", help="Use the new lifecycle kernel instead of run_trades.execute_once")
    parser.add_argument("--clear-results", action="store_true", help="Ignore previous results.jsonl")
    args = parser.parse_args()

    if args.clear_results and os.path.exists(_DEFAULT_RESULTS_PATH):
        os.remove(_DEFAULT_RESULTS_PATH)

    window_ids = [w.strip() for w in args.windows.split(",") if w.strip()] if args.windows else None
    logger = _configure_logging(_DEFAULT_LOG_PATH)
    logger.info("Starting continuous-window sweep (strategy=%s)...", args.strategy)
    report = run_sweep(
        window_ids=window_ids,
        strategy=args.strategy,
        max_cycles_per_sim=args.max_cycles,
        max_workers=args.max_workers,
        max_drawdown_stop_pct=args.max_dd_stop,
        lifecycle=args.lifecycle,
    )
    logger.info("Sweep complete. Report saved to %s", _DEFAULT_REPORT_PATH)
    print(json.dumps(report["summary"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
