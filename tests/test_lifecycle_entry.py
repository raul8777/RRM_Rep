"""tests/test_lifecycle_entry.py — EntryGate tests."""

import pytest
from lifecycle.base import EntryPolicy, MarketContext, Side, Signal, Position
from lifecycle.entry import EntryGate


def _ctx(regime="SPOT_BULL", btc_dom=55.0, funding=None) -> MarketContext:
    return MarketContext(
        timestamp=0.0,
        regime=regime,
        regime_confidence={regime: 0.8},
        btc_7d_pct=0.0,
        btc_30d_pct=0.0,
        btc_day_pct=0.0,
        fear_greed_index=50,
        btc_dominance=btc_dom,
        funding_rate=funding,
    )


def _signal(symbol, side=Side.LONG, score=0.6, pnl=1.0, prob=0.55, liquidity=10_000_000, category="MIDCAP_ALT", strength=0.6, vol_spike=1.0):
    return Signal(
        symbol=symbol,
        side=side,
        category=category,
        composite_score=score,
        expected_pnl=pnl,
        success_probability=prob,
        signal_strength=strength,
        regime_alignment=1.0,
        liquidity_usdt_24h=liquidity,
        features={"vol_spike": vol_spike},
    )


def _policy(**overrides) -> EntryPolicy:
    defaults = {
        "min_composite_score": 0.5,
        "min_expected_pnl": 0.0,
        "min_success_probability": 0.5,
        "min_liquidity_usdt": 3_000_000,
        "max_total_positions": 4,
        "max_category_positions": {"LARGE_CAP": 1, "MIDCAP_ALT": 2, "NARRATIVE": 1},
    }
    defaults.update(overrides)
    return EntryPolicy(**defaults)


class TestEntryGate:
    def test_approves_strong_signal(self):
        ctx = _ctx("SPOT_BULL")
        policy = _policy()
        gate = EntryGate()
        signals = [_signal("SOLUSDT", score=0.7, pnl=1.5, prob=0.6, category="MIDCAP_ALT")]
        decisions = gate.evaluate(signals, ctx, policy, [])
        assert len(decisions) == 1
        assert decisions[0].approved

    def test_rejects_weak_score(self):
        ctx = _ctx("SPOT_BULL")
        policy = _policy()
        gate = EntryGate()
        signals = [_signal("SOLUSDT", score=0.3, pnl=1.5, prob=0.6, category="MIDCAP_ALT")]
        decisions = gate.evaluate(signals, ctx, policy, [])
        assert len(decisions) == 0

    def test_respects_total_position_cap(self):
        ctx = _ctx("SPOT_BULL")
        policy = _policy(max_total_positions=1)
        gate = EntryGate()
        existing = [Position(symbol="BTCUSDT", side=Side.LONG, quantity=1.0, entry_price=100.0, entry_time=0.0, position_usdt=100.0, category="LARGE_CAP", regime_at_entry="SPOT_BULL", profile_at_entry="A")]
        signals = [_signal("SOLUSDT", score=0.7, pnl=1.5, prob=0.6, category="MIDCAP_ALT")]
        decisions = gate.evaluate(signals, ctx, policy, existing)
        assert len(decisions) == 0

    def test_respects_category_cap(self):
        ctx = _ctx("SPOT_BULL")
        policy = _policy(max_category_positions={"MIDCAP_ALT": 1})
        gate = EntryGate()
        existing = [Position(symbol="SOLUSDT", side=Side.LONG, quantity=1.0, entry_price=100.0, entry_time=0.0, position_usdt=100.0, category="MIDCAP_ALT", regime_at_entry="SPOT_BULL", profile_at_entry="A")]
        signals = [_signal("ADAUSDT", score=0.7, pnl=1.5, prob=0.6, category="MIDCAP_ALT")]
        decisions = gate.evaluate(signals, ctx, policy, existing)
        assert len(decisions) == 0

    def test_btc_dominance_gate(self):
        ctx = _ctx("SPOT_BULL", btc_dom=66.0)
        policy = _policy(btc_dominance_max=64.0)
        gate = EntryGate()
        signals = [_signal("SOLUSDT", score=0.7, pnl=1.5, prob=0.6, category="MIDCAP_ALT")]
        decisions = gate.evaluate(signals, ctx, policy, [])
        assert len(decisions) == 0

    def test_funding_rate_gate(self):
        ctx = _ctx("SPOT_BULL", funding=0.0015)
        policy = _policy(funding_rate_max=0.0010)
        gate = EntryGate()
        signals = [_signal("SOLUSDT", score=0.7, pnl=1.5, prob=0.6, category="MIDCAP_ALT")]
        decisions = gate.evaluate(signals, ctx, policy, [])
        assert len(decisions) == 0

    def test_volspike_override_soft_limits(self):
        ctx = _ctx("SPOT_BULL")
        policy = _policy(min_composite_score=0.7, volspike_override_enabled=True)
        gate = EntryGate()
        # Score is below 0.7 but high volume spike and strong signal strength should trigger override.
        signals = [_signal("SOLUSDT", score=0.65, pnl=1.5, prob=0.6, category="MIDCAP_ALT", strength=0.80, vol_spike=2.5)]
        decisions = gate.evaluate(signals, ctx, policy, [])
        assert len(decisions) == 1
        assert decisions[0].approved

    def test_volspike_override_does_not_override_hard_limits(self):
        ctx = _ctx("SPOT_BULL")
        policy = _policy(min_composite_score=0.7, max_total_positions=0, volspike_override_enabled=True)
        gate = EntryGate()
        signals = [_signal("SOLUSDT", score=0.65, pnl=1.5, prob=0.6, category="MIDCAP_ALT", strength=0.80, vol_spike=2.5)]
        decisions = gate.evaluate(signals, ctx, policy, [])
        assert len(decisions) == 0

    def test_entry_price_and_atr_resolved(self):
        ctx = _ctx("SPOT_BULL")
        policy = _policy()
        gate = EntryGate(price_resolver=lambda s: 150.0, atr_resolver=lambda s: 2.5)
        signals = [_signal("SOLUSDT", score=0.7, pnl=1.5, prob=0.6, category="MIDCAP_ALT")]
        decisions = gate.evaluate(signals, ctx, policy, [])
        assert decisions[0].entry_price == 150.0
        assert decisions[0].atr == 2.5
