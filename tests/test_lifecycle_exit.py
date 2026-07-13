"""tests/test_lifecycle_exit.py — ExitEngine tests."""

import pytest
from lifecycle.base import ExitPolicy, MarketContext, MomentumExitRule, PartialTake, Position, Side, StagnationRule
from lifecycle.exit import ExitEngine, update_peak_pnl


def _ctx(fng=50) -> MarketContext:
    return MarketContext(
        timestamp=0.0,
        regime="SPOT_BULL",
        regime_confidence={"SPOT_BULL": 0.8},
        btc_7d_pct=0.0,
        btc_30d_pct=0.0,
        btc_day_pct=0.0,
        fear_greed_index=fng,
        btc_dominance=55.0,
    )


def _policy(**overrides) -> ExitPolicy:
    defaults = {
        "regime": "SPOT_BULL",
        "profile": "A",
        "hard_sl_atr_mult": 2.0,
        "hard_tp_atr_mult": 3.5,
        "trailing_activation_pct": 0.02,
        "trailing_atr_mult": 1.5,
        "breakeven_activation_pct": 0.015,
        "partial_takes": (PartialTake(0.25, 1.5, "tp1"), PartialTake(0.25, 2.5, "tp2")),
        "momentum_exit": MomentumExitRule(70, 30, 80, 20),
        "stagnation_rule": StagnationRule(6, 0.05, 0.5),
    }
    defaults.update(overrides)
    return ExitPolicy(**defaults)


def _pos(symbol, side, entry, qty=1.0, peak=0.0, cat="MIDCAP_ALT", entry_time=0.0):
    return Position(symbol=symbol, side=side, quantity=qty, entry_price=entry, entry_time=entry_time, position_usdt=entry*qty, category=cat, regime_at_entry="SPOT_BULL", profile_at_entry="A", peak_pnl_pct=peak)


class TestExitEngine:
    def test_hard_sl_long(self):
        engine = ExitEngine(price_resolver=lambda s: 96.0, atr_resolver=lambda s: 2.0)
        pos = _pos("SOLUSDT", Side.LONG, 100.0)
        decisions = engine.evaluate([pos], _ctx(), _policy(), now=3600)
        assert len(decisions) == 1
        assert decisions[0].symbol == "SOLUSDT"
        assert decisions[0].action.value == "CLOSE_ALL"
        assert "hard SL" in decisions[0].reason

    def test_hard_tp_long(self):
        engine = ExitEngine(price_resolver=lambda s: 107.0, atr_resolver=lambda s: 2.0)
        pos = _pos("SOLUSDT", Side.LONG, 100.0)
        decisions = engine.evaluate([pos], _ctx(), _policy(), now=3600)
        assert len(decisions) == 1
        assert decisions[0].action.value == "CLOSE_ALL"
        assert "hard TP" in decisions[0].reason

    def test_hard_sl_short(self):
        engine = ExitEngine(price_resolver=lambda s: 104.0, atr_resolver=lambda s: 2.0)
        pos = _pos("SOLUSDT", Side.SHORT, 100.0)
        decisions = engine.evaluate([pos], _ctx(), _policy(), now=3600)
        assert len(decisions) == 1
        assert decisions[0].action.value == "CLOSE_ALL"
        assert "hard SL" in decisions[0].reason

    def test_trailing_stop_long(self):
        engine = ExitEngine(price_resolver=lambda s: 102.5, atr_resolver=lambda s: 2.0)
        # Peak 4%, now 2.5% up, drawdown 1.5% < trail distance 3%. Partial take at 3% not met.
        pos = _pos("SOLUSDT", Side.LONG, 100.0, peak=0.04)
        decisions = engine.evaluate([pos], _ctx(), _policy(), now=3600)
        assert decisions[0].action.value == "HOLD"

    def test_trailing_stop_long_fires(self):
        engine = ExitEngine(price_resolver=lambda s: 102.0, atr_resolver=lambda s: 2.0)
        # Peak 5%, now 2% up, drawdown 3% >= trail distance 3% => trailing stop fires.
        pos = _pos("SOLUSDT", Side.LONG, 100.0, peak=0.05)
        decisions = engine.evaluate([pos], _ctx(), _policy(), now=3600)
        assert decisions[0].action.value == "CLOSE_ALL"
        assert "trailing" in decisions[0].reason

    def test_partial_take(self):
        engine = ExitEngine(price_resolver=lambda s: 103.0, atr_resolver=lambda s: 2.0)
        # pnl = 3%, atr distance for tp1 = 1.5*2/100 = 3% => fires.
        pos = _pos("SOLUSDT", Side.LONG, 100.0)
        decisions = engine.evaluate([pos], _ctx(), _policy(), now=3600)
        assert decisions[0].action.value == "CLOSE_PCT"
        assert decisions[0].pct == 0.25

    def test_momentum_exit_overbought(self):
        engine = ExitEngine(price_resolver=lambda s: 102.0, atr_resolver=lambda s: 2.0, rsi_resolver=lambda s: 75.0)
        pos = _pos("SOLUSDT", Side.LONG, 100.0)
        decisions = engine.evaluate([pos], _ctx(), _policy(), now=3600)
        assert decisions[0].action.value == "CLOSE_ALL"
        assert "RSI overbought" in decisions[0].reason

    def test_stagnation(self):
        engine = ExitEngine(price_resolver=lambda s: 99.5, atr_resolver=lambda s: 2.0)
        # Peak 3%, now -0.5%, drawdown 3.5% >= 5%? No. Use a 6% drawdown.
        pos = _pos("SOLUSDT", Side.LONG, 100.0, peak=0.06)
        decisions = engine.evaluate([pos], _ctx(), _policy(), now=3600*7)
        assert decisions[0].action.value == "CLOSE_ALL"
        assert "stagnation" in decisions[0].reason

    def test_highest_priority_wins(self):
        # SL and TP both at distance 3.5%? Hard to hit both; make SL fire by going below.
        engine = ExitEngine(price_resolver=lambda s: 96.0, atr_resolver=lambda s: 2.0)
        pos = _pos("SOLUSDT", Side.LONG, 100.0)
        decisions = engine.evaluate([pos], _ctx(), _policy(), now=3600)
        # SL priority 90 should beat partial take priority 60.
        assert decisions[0].action.value == "CLOSE_ALL"
        assert "hard SL" in decisions[0].reason

    def test_update_peak_pnl(self):
        positions = [_pos("SOLUSDT", Side.LONG, 100.0, peak=0.0)]
        updated = update_peak_pnl(positions, lambda s: 105.0)
        assert updated[0].peak_pnl_pct == 0.05
