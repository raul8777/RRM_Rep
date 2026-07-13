"""tests/test_lifecycle_runner.py — LifecycleRunner + MIMIC adapter tests."""

import pytest
from lifecycle import (
    LifecycleConfig,
    LifecycleRunner,
    MimicExecutionAdapter,
    SignalPipeline,
    EntryGate,
    PositionSizer,
    ExitEngine,
    SignalPolicy,
    EntryPolicy,
    SizingPolicy,
    ExitPolicy,
    PartialTake,
    MomentumExitRule,
    StagnationRule,
)


def _config() -> LifecycleConfig:
    return LifecycleConfig(
        regime="SPOT_BULL",
        profile="A",
        signal_policy=SignalPolicy(
            min_signal_strength=0.0,
            min_success_probability=0.0,
            min_expected_pnl=-9999.0,
            min_liquidity_usdt=0.0,
            categories_enabled=("LARGE_CAP", "MIDCAP_ALT", "NARRATIVE"),
            max_candidates=3,
            diversification_mode="category",
        ),
        entry_policy=EntryPolicy(
            min_composite_score=0.0,
            min_expected_pnl=-9999.0,
            min_success_probability=0.0,
            min_liquidity_usdt=0.0,
            max_total_positions=3,
            max_category_positions={"LARGE_CAP": 1, "MIDCAP_ALT": 2, "NARRATIVE": 1},
        ),
        sizing_policy=SizingPolicy(
            regime_allocation_pct=0.8,
            max_per_trade_usdt=500.0,
            kelly_base_pct=0.22,
            kelly_max_pct=0.25,
            conviction_max_pct=0.22,
            category_caps={"LARGE_CAP": 0.4, "MIDCAP_ALT": 0.4, "NARRATIVE": 0.2},
        ),
        exit_policy=ExitPolicy(
            regime="SPOT_BULL",
            profile="A",
            hard_sl_atr_mult=2.0,
            hard_tp_atr_mult=3.5,
            trailing_activation_pct=0.02,
            trailing_atr_mult=1.5,
            breakeven_activation_pct=0.015,
            partial_takes=(PartialTake(0.25, 1.5, "tp1"),),
            momentum_exit=MomentumExitRule(70, 30, 80, 20),
            stagnation_rule=StagnationRule(6, 0.05, 0.5),
        ),
    )


class TestLifecycleRunner:
    def test_full_cycle_entries(self):
        adapter = MimicExecutionAdapter(
            initial_capital=5000.0,
            prices={"BTCUSDT": 50000.0, "SOLUSDT": 100.0, "FETUSDT": 2.0},
            atrs={"BTCUSDT": 500.0, "SOLUSDT": 2.0, "FETUSDT": 0.05},
            timestamp=0.0,
        )
        analyses = [
            {"symbol": "BTCUSDT", "signal": "BUY", "strength": 0.8, "expected_pnl": 2.0, "success_probability": 0.6, "liquidity_usdt_24h": 1_000_000_000},
            {"symbol": "SOLUSDT", "signal": "BUY", "strength": 0.7, "expected_pnl": 1.5, "success_probability": 0.55, "liquidity_usdt_24h": 50_000_000},
            {"symbol": "FETUSDT", "signal": "BUY", "strength": 0.65, "expected_pnl": 1.0, "success_probability": 0.52, "liquidity_usdt_24h": 10_000_000},
        ]
        runner = LifecycleRunner(
            adapter=adapter,
            signal_pipeline=SignalPipeline(),
            entry_gate=EntryGate(),
            position_sizer=PositionSizer(),
            exit_engine=ExitEngine(price_resolver=adapter.price_resolver, atr_resolver=adapter.atr_resolver),
        )
        result = runner.run_cycle(analyses, _config(), total_liquid=5000.0)
        assert result["regime"] == "SPOT_BULL"
        assert result["signals"] == 3
        assert len(result["entries"]) == 3
        assert len(result["exits"]) == 0  # no positions before cycle, no exits
        assert len(adapter.positions) == 3

    def test_exit_side(self):
        adapter = MimicExecutionAdapter(
            initial_capital=5000.0,
            prices={"SOLUSDT": 107.0},  # 7% up, hits hard TP
            atrs={"SOLUSDT": 2.0},
            timestamp=3600,
        )
        # Pre-seed a position
        from lifecycle.base import Position, Side
        adapter.positions.append(Position(
            symbol="SOLUSDT", side=Side.LONG, quantity=1.0, entry_price=100.0,
            entry_time=0.0, position_usdt=100.0, category="MIDCAP_ALT",
            regime_at_entry="SPOT_BULL", profile_at_entry="A",
        ))
        runner = LifecycleRunner(
            adapter=adapter,
            signal_pipeline=SignalPipeline(),
            entry_gate=EntryGate(),
            position_sizer=PositionSizer(),
            exit_engine=ExitEngine(price_resolver=adapter.price_resolver, atr_resolver=adapter.atr_resolver),
        )
        result = runner.run_cycle([], _config(), total_liquid=5000.0)
        assert len(result["exits"]) == 1
        assert result["exits"][0]["action"] == "CLOSE_ALL"
        assert len(adapter.positions) == 0
