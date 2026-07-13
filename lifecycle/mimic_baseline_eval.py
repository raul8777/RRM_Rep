"""lifecycle/mimic_baseline_eval.py — lightweight MIMIC baseline evaluation.

Evaluates a lifecycle config against a synthetic market window to produce a
baseline artifact comparable to Super MIMIC sweep results. This is a staging
tool; real MIMIC sweeps should use `lifecycle/super_mimic_integration.py` plugged
into `Dev/scripts/mimic_mode_profile_sweep.py`.

Inputs:
  - A LifecycleConfig (regime + profile + policies).
  - A list of symbol analysis dicts (signal, strength, expected_pnl, etc.).
  - Price/ATR/RSI maps at entry and at each exit-check step.

Output:
  - Metrics dict: pnl_per_day, drawdown_pct, trades, win_rate,
    peak_capture_ratio, capital_deployment_ratio.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from lifecycle import (
    LifecycleConfig,
    SignalPolicy,
    EntryPolicy,
    SizingPolicy,
    ExitPolicy,
    PartialTake,
    MomentumExitRule,
    StagnationRule,
)
from lifecycle.adapter_mimic import MimicExecutionAdapter
from lifecycle.runner import LifecycleRunner
from lifecycle.signal import SignalPipeline
from lifecycle.entry import EntryGate
from lifecycle.sizing import PositionSizer
from lifecycle.exit import ExitEngine
from lifecycle.super_mimic_integration import compute_metrics


def spot_bull_a_config() -> LifecycleConfig:
    """LifecycleConfig mapped from the best SPOT_BULL-A sweep result.

    Source: Dev/sweep_analysis_SPOT_BULL_20260713.md Step 7
    Params: min_signal_strength=0.55, min_success_probability=0.55,
            stop_loss_pct=4.5, kelly_max_usdt=1000, kelly_dynamic_base_pct=0.18,
            max_positions_spot_bull=5
    """
    return LifecycleConfig(
        regime="SPOT_BULL",
        profile="A",
        signal_policy=SignalPolicy(
            min_signal_strength=0.55,
            min_success_probability=0.55,
            min_expected_pnl=0.0,
            min_liquidity_usdt=3_000_000.0,
            categories_enabled=("LARGE_CAP", "MIDCAP_ALT", "NARRATIVE"),
            max_candidates=5,
            diversification_mode="category",
        ),
        entry_policy=EntryPolicy(
            min_composite_score=0.55,
            min_expected_pnl=0.0,
            min_success_probability=0.55,
            min_liquidity_usdt=3_000_000.0,
            max_total_positions=5,
            max_category_positions={"LARGE_CAP": 1, "MIDCAP_ALT": 2, "NARRATIVE": 1},
        ),
        sizing_policy=SizingPolicy(
            regime_allocation_pct=0.8,
            max_per_trade_usdt=1000.0,
            kelly_base_pct=0.18,
            kelly_max_pct=0.20,
            conviction_max_pct=0.20,
            category_caps={"LARGE_CAP": 0.30, "MIDCAP_ALT": 0.35, "NARRATIVE": 0.15},
        ),
        exit_policy=ExitPolicy(
            regime="SPOT_BULL",
            profile="A",
            hard_sl_atr_mult=2.25,  # maps ~4.5% SL for a 2% ATR symbol
            hard_tp_atr_mult=3.5,
            trailing_activation_pct=0.02,
            trailing_atr_mult=1.5,
            breakeven_activation_pct=0.015,
            partial_takes=(PartialTake(0.25, 1.5, "tp1"), PartialTake(0.25, 2.5, "tp2")),
            momentum_exit=MomentumExitRule(70, 30, 80, 20),
            stagnation_rule=StagnationRule(6, 0.05, 0.5),
        ),
    )


def run_baseline_window(
    config: LifecycleConfig,
    analyses: List[Dict[str, Any]],
    price_series: List[Dict[str, float]],
    atr_map: Dict[str, float],
    rsi_map: Dict[str, float],
    total_liquid: float = 5000.0,
) -> Tuple[MimicExecutionAdapter, Dict[str, Any], Dict[str, Any]]:
    """Run the lifecycle kernel over a synthetic price series.

    `price_series` is a list of price snapshots (one per cycle/step). At each step
    the runner evaluates entries and exits against the current prices.
    """
    adapter = MimicExecutionAdapter(
        initial_capital=total_liquid,
        prices=price_series[0],
        atrs=atr_map,
        rsis=rsi_map,
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
    summaries: List[Dict[str, Any]] = []
    for step, prices in enumerate(price_series):
        adapter.prices = prices
        adapter.timestamp = float(step * 3600)
        summary = runner.run_cycle(analyses, config, total_liquid=total_liquid)
        summaries.append(summary)
    metrics = compute_metrics(adapter)
    return adapter, metrics, {
        "steps": len(price_series),
        "summaries": summaries,
    }


def sample_spot_bull_analyses() -> List[Dict[str, Any]]:
    """Synthetic analysis dicts for a SPOT_BULL baseline smoke test."""
    return [
        {"symbol": "BTCUSDT", "signal": "BUY", "strength": 0.70, "expected_pnl": 2.0,
         "success_probability": 0.60, "liquidity_usdt_24h": 1_000_000_000, "vol_spike": 1.8},
        {"symbol": "SOLUSDT", "signal": "BUY", "strength": 0.60, "expected_pnl": 1.5,
         "success_probability": 0.55, "liquidity_usdt_24h": 100_000_000, "vol_spike": 1.6},
        {"symbol": "FETUSDT", "signal": "BUY", "strength": 0.55, "expected_pnl": 1.0,
         "success_probability": 0.52, "liquidity_usdt_24h": 20_000_000, "vol_spike": 1.4},
    ]


def sample_price_series() -> List[Dict[str, float]]:
    """A short synthetic window where BTC rallies then retraces."""
    return [
        {"BTCUSDT": 50000.0, "SOLUSDT": 100.0, "FETUSDT": 2.00},
        {"BTCUSDT": 50500.0, "SOLUSDT": 101.0, "FETUSDT": 2.05},  # +1%
        {"BTCUSDT": 51200.0, "SOLUSDT": 103.0, "FETUSDT": 2.12},  # +2.4% BTC
        {"BTCUSDT": 52000.0, "SOLUSDT": 105.0, "FETUSDT": 2.20},  # hard TP zone
        {"BTCUSDT": 51900.0, "SOLUSDT": 104.5, "FETUSDT": 2.18},  # slight retracement
        {"BTCUSDT": 51800.0, "SOLUSDT": 104.0, "FETUSDT": 2.15},  # trailing risk
    ]
