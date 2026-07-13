"""lifecycle/super_mimic_integration.py — bridge between lifecycle configs and Super MIMIC.

This module converts a `LifecycleConfig` into a flat, Super-MIMIC-compatible
parameter dict and fingerprint, and provides an adapter to run a lifecycle
config through a MIMIC backtest window.

The goal is to let Super MIMIC sweep lifecycle parameters (signal, entry, sizing,
exit) as points in the quantum landscape, then evaluate the deploy gate with the
resulting metrics.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from lifecycle.base import LifecycleConfig
from lifecycle.adapter_mimic import MimicExecutionAdapter
from lifecycle.runner import LifecycleRunner
from lifecycle.signal import SignalPipeline
from lifecycle.entry import EntryGate
from lifecycle.sizing import PositionSizer
from lifecycle.exit import ExitEngine


def lifecycle_config_to_dict(config: LifecycleConfig) -> Dict[str, Any]:
    """Flatten a LifecycleConfig into a JSON-serializable parameter dict."""
    return {
        "regime": config.regime,
        "profile": config.profile,
        # Signal policy
        "signal_min_strength": config.signal_policy.min_signal_strength,
        "signal_min_prob": config.signal_policy.min_success_probability,
        "signal_min_pnl": config.signal_policy.min_expected_pnl,
        "signal_min_liquidity": config.signal_policy.min_liquidity_usdt,
        "signal_max_candidates": config.signal_policy.max_candidates,
        "signal_diversification": config.signal_policy.diversification_mode,
        # Entry policy
        "entry_min_score": config.entry_policy.min_composite_score,
        "entry_min_prob": config.entry_policy.min_success_probability,
        "entry_min_pnl": config.entry_policy.min_expected_pnl,
        "entry_max_positions": config.entry_policy.max_total_positions,
        # Sizing policy
        "sizing_regime_pct": config.sizing_policy.regime_allocation_pct,
        "sizing_max_per_trade": config.sizing_policy.max_per_trade_usdt,
        "sizing_kelly_base_pct": config.sizing_policy.kelly_base_pct,
        "sizing_kelly_max_pct": config.sizing_policy.kelly_max_pct,
        "sizing_conviction_max_pct": config.sizing_policy.conviction_max_pct,
        # Exit policy
        "exit_sl_atr_mult": config.exit_policy.hard_sl_atr_mult,
        "exit_tp_atr_mult": config.exit_policy.hard_tp_atr_mult,
        "exit_trailing_activation_pct": config.exit_policy.trailing_activation_pct,
        "exit_trailing_atr_mult": config.exit_policy.trailing_atr_mult,
        "exit_breakeven_pct": config.exit_policy.breakeven_activation_pct,
    }


def fingerprint(config: LifecycleConfig) -> str:
    """Return the stable lifecycle fingerprint (used by Super MIMIC meta-learner)."""
    return config.fingerprint()


def build_mimic_experiment_design(
    config: LifecycleConfig,
    window: str,
    mode: str = "mimic",
) -> Dict[str, Any]:
    """Build a Super MIMIC experiment design dict for a lifecycle config."""
    return {
        "engine": mode,
        "mode": config.regime,
        "profile": config.profile,
        "windows": [window],
        "config": lifecycle_config_to_dict(config),
        "fingerprint": {
            "lifecycle_fp": fingerprint(config),
        },
    }


def run_mimic_backtest(
    analyses: List[Dict[str, Any]],
    config: LifecycleConfig,
    prices: Dict[str, float],
    atrs: Dict[str, float],
    rsis: Dict[str, float],
    total_liquid: float = 5000.0,
) -> Tuple[MimicExecutionAdapter, Dict[str, Any]]:
    """Run a single MIMIC backtest with the lifecycle kernel.

    Returns the populated adapter and the runner summary. Metrics like
    PnL/day, drawdown, peak-capture ratio, and CDR can be derived from the
    adapter's audit log and positions.
    """
    adapter = MimicExecutionAdapter(
        initial_capital=total_liquid,
        prices=prices,
        atrs=atrs,
        rsis=rsis,
    )
    runner = LifecycleRunner(
        adapter=adapter,
        signal_pipeline=SignalPipeline(),
        entry_gate=EntryGate(),
        position_sizer=PositionSizer(),
        exit_engine=ExitEngine(
            price_resolver=adapter.price_resolver,
            atr_resolver=adapter.atr_resolver,
            rsi_resolver=adapter.rsi_resolver,
        ),
    )
    summary = runner.run_cycle(analyses, config, total_liquid=total_liquid)
    return adapter, summary


def compute_metrics(adapter: MimicExecutionAdapter) -> Dict[str, float]:
    """Compute Super MIMIC metrics from a MIMIC adapter after a backtest.

    This is a minimal version; full MIMIC integration will compute these from
    the historical simulator.
    """
    total_entries = len(adapter.audit_log)
    total_exits = len([o for o in adapter.orders if o.get("action") in ("CLOSE_ALL", "CLOSE_PCT")])
    peak_capture_ratio = 0.0
    # Simple heuristic: if we exited any positions, assume we captured at least the hard TP.
    if adapter.positions:
        # Still open positions count as not captured yet.
        peak_capture_ratio = total_exits / max(1, total_entries) if total_entries > 0 else 0.0
    return {
        "pnl_per_day_usdt": 0.0,  # placeholder: requires daily PnL aggregation
        "drawdown_pct": 0.0,      # placeholder: requires equity curve
        "trades": total_exits,
        "win_rate": 0.0,          # placeholder
        "peak_capture_ratio": peak_capture_ratio,
        "capital_deployment_ratio": 0.0,  # placeholder
    }
