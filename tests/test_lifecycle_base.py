"""tests/test_lifecycle_base.py — unit tests for lifecycle contracts and regime kernel."""

import pytest
from lifecycle.base import (
    LifecycleConfig,
    SignalPolicy,
    EntryPolicy,
    SizingPolicy,
    ExitPolicy,
    PartialTake,
    MomentumExitRule,
    StagnationRule,
    MarketContext,
    Side,
    ExitDecision,
    Action,
)
from lifecycle.regime import RegimeState, build_market_context
from market_structure import evaluate


class TestLifecycleConfig:
    def test_fingerprint_stable(self):
        cfg = LifecycleConfig(
            regime="SPOT_BULL",
            profile="A",
            signal_policy=SignalPolicy(
                min_signal_strength=0.4,
                min_success_probability=0.45,
                min_expected_pnl=-0.5,
                min_liquidity_usdt=3_000_000,
                categories_enabled=("LARGE_CAP", "MIDCAP_ALT", "NARRATIVE"),
                max_candidates=10,
            ),
            entry_policy=EntryPolicy(
                min_composite_score=0.5,
                min_expected_pnl=-0.5,
                min_success_probability=0.45,
                min_liquidity_usdt=3_000_000,
                max_total_positions=8,
                max_category_positions={"LARGE_CAP": 2, "MIDCAP_ALT": 3, "NARRATIVE": 2},
            ),
            sizing_policy=SizingPolicy(
                regime_allocation_pct=0.8,
                max_per_trade_usdt=1200,
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
                momentum_exit=MomentumExitRule(70, 30, 80, 15),
                stagnation_rule=StagnationRule(6, 0.05, 0.5),
            ),
        )
        fp1 = cfg.fingerprint()
        fp2 = cfg.fingerprint()
        assert fp1 == fp2
        assert len(fp1) == 16


class TestExitDecision:
    def test_hold(self):
        d = ExitDecision.hold("BTCUSDT")
        assert d.action == Action.HOLD
        assert d.pct == 0.0

    def test_close_pct(self):
        d = ExitDecision.close_pct("ETHUSDT", 0.25, "tp1", 80)
        assert d.action == Action.CLOSE_PCT
        assert d.pct == 0.25

    def test_close_all(self):
        d = ExitDecision.close_all("SOLUSDT", "sl", 95)
        assert d.action == Action.CLOSE_ALL
        assert d.pct == 1.0


class TestRegimeState:
    def test_confident(self):
        state = RegimeState(
            dominant="FUTURES_SHORT",
            confidence={"FUTURES_SHORT": 0.75, "PRESERVATION": 0.15, "SPOT_BULL": 0.05, "RANGE_SCALP": 0.05},
            confirmed=True,
            features={"btc_30d_pct": -18.5},
            reason="bear",
            color="#9b59b6",
        )
        assert state.is_confident(0.60)
        assert not state.is_confident(0.80)

    def test_build_market_context(self):
        state = RegimeState(
            dominant="RANGE_SCALP",
            confidence={"RANGE_SCALP": 0.6},
            confirmed=True,
            features={"btc_7d_pct": -2.0, "btc_30d_pct": -5.0, "btc_day_pct": 0.5},
            reason="lateral",
            color="#f39c12",
        )
        ctx = build_market_context(state, 55, 58.0, -0.01, 0.45)
        assert ctx.regime == "RANGE_SCALP"
        assert ctx.fear_greed_index == 55
        assert ctx.btc_dominance == 58.0


def test_structure_decision_contract_has_traceability_fields():
    snapshot = {
        "state": "neutral",
        "reason": "no_zone_event",
        "structure_1h": {"sr_zone_position": 0.10},
        "confirmation_5m": {"sr_bounce": True},
        "timing_1m": {"sr_bounce": True},
    }
    decision = evaluate(snapshot, "LONG")
    payload = decision.to_dict()

    assert payload["state"] in {"neutral", "structural_confirmed", "invalidated", "unavailable"}
    assert payload["decision_id"]
    assert payload["phase"] in {"monitor", "protect", "capture", "finalize", "close"}
    assert payload["evidence"]
    assert payload["reason_code"]
