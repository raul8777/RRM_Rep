"""tests/test_lifecycle_sizing.py — PositionSizer and CapitalBudget tests."""

import pytest
from lifecycle.base import EntryDecision, MarketContext, Position, Side, Signal, SizingPolicy
from lifecycle.sizing import PositionSizer, size_entry


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


def _signal(symbol, category="MIDCAP_ALT", score=0.6, prob=0.55):
    return Signal(
        symbol=symbol,
        side=Side.LONG,
        category=category,
        composite_score=score,
        expected_pnl=1.0,
        success_probability=prob,
        signal_strength=0.6,
        regime_alignment=1.0,
        liquidity_usdt_24h=10_000_000,
    )


def _entry(symbol, category="MIDCAP_ALT", score=0.6, prob=0.55, price=100.0):
    return EntryDecision(
        signal=_signal(symbol, category, score, prob),
        approved=True,
        entry_price=price,
        atr=2.0,
    )


def _policy(**overrides) -> SizingPolicy:
    defaults = {
        "regime_allocation_pct": 0.8,
        "max_per_trade_usdt": 1200.0,
        "kelly_base_pct": 0.22,
        "kelly_max_pct": 0.25,
        "conviction_max_pct": 0.22,
        "category_caps": {"LARGE_CAP": 0.4, "MIDCAP_ALT": 0.4, "NARRATIVE": 0.2},
        "fng_capital_multiplier": True,
    }
    defaults.update(overrides)
    return SizingPolicy(**defaults)


class TestPositionSizer:
    def test_budget_available(self):
        sizer = PositionSizer()
        positions = [
            Position(symbol="SOLUSDT", side=Side.LONG, quantity=1.0, entry_price=100.0, entry_time=0.0, position_usdt=200.0, category="MIDCAP_ALT", regime_at_entry="SPOT_BULL", profile_at_entry="A"),
        ]
        budget = sizer.build_budget(total_liquid=5000.0, positions=positions, policy=_policy())
        # 80% of 5000 = 4000 allocated; 200 deployed => 3800 remaining
        assert budget.available_for_regime == 3800.0
        assert budget.category_caps["MIDCAP_ALT"] == 1600.0
        assert budget.already_deployed["MIDCAP_ALT"] == 200.0

    def test_basic_size(self):
        ctx = _ctx(50)
        policy = _policy()
        sizer = PositionSizer()
        budget = sizer.build_budget(total_liquid=5000.0, positions=[], policy=policy)
        entry = _entry("SOLUSDT", "MIDCAP_ALT", score=0.7, prob=0.6, price=100.0)
        trade = sizer.size(entry, budget, ctx, policy)
        # conviction = (0.7+0.6)/2 = 0.65
        # kelly_base = 4000 * 0.22 = 880
        # position = 880 * 0.65*2 = 1144
        assert 1000 < trade.position_usdt < 1200
        assert trade.quantity == pytest.approx(trade.position_usdt / 100.0, rel=1e-6)

    def test_max_per_trade_cap(self):
        ctx = _ctx(50)
        policy = _policy(max_per_trade_usdt=500.0)
        sizer = PositionSizer()
        budget = sizer.build_budget(total_liquid=5000.0, positions=[], policy=policy)
        entry = _entry("SOLUSDT", "MIDCAP_ALT", score=0.9, prob=0.8, price=100.0)
        trade = sizer.size(entry, budget, ctx, policy)
        assert trade.position_usdt == 500.0

    def test_category_cap(self):
        ctx = _ctx(50)
        policy = _policy(category_caps={"MIDCAP_ALT": 0.1})
        sizer = PositionSizer()
        positions = [
            Position(symbol="SOLUSDT", side=Side.LONG, quantity=1.0, entry_price=100.0, entry_time=0.0, position_usdt=300.0, category="MIDCAP_ALT", regime_at_entry="SPOT_BULL", profile_at_entry="A"),
        ]
        budget = sizer.build_budget(total_liquid=5000.0, positions=positions, policy=policy)
        # cap = 4000 * 0.1 = 400; deployed 300 => remaining 100
        entry = _entry("ADAUSDT", "MIDCAP_ALT", score=0.8, prob=0.7, price=1.0)
        trade = sizer.size(entry, budget, ctx, policy)
        assert trade.position_usdt <= 100.0

    def test_fng_multiplier(self):
        ctx = _ctx(15)  # extreme fear
        policy = _policy()
        sizer = PositionSizer()
        budget = sizer.build_budget(total_liquid=5000.0, positions=[], policy=policy)
        entry = _entry("SOLUSDT", "MIDCAP_ALT", score=0.7, prob=0.6, price=100.0)
        trade_fear = sizer.size(entry, budget, ctx, policy)

        ctx_greed = _ctx(80)  # extreme greed
        trade_greed = sizer.size(entry, budget, ctx_greed, policy)
        assert trade_fear.position_usdt > trade_greed.position_usdt

    def test_size_entry_one_shot(self):
        ctx = _ctx(50)
        policy = _policy()
        entry = _entry("BTCUSDT", "LARGE_CAP", score=0.8, prob=0.7, price=50000.0)
        trade = size_entry(entry, total_liquid=5000.0, positions=[], ctx=ctx, policy=policy)
        assert trade.position_usdt > 0
        assert trade.quantity > 0
