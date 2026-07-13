"""tests/test_lifecycle_adapters.py — SPOT/FUTURES adapter skeleton tests."""

import pytest
from lifecycle import (
    LifecycleConfig,
    SignalPolicy,
    EntryPolicy,
    SizingPolicy,
    ExitPolicy,
    PartialTake,
    MomentumExitRule,
    StagnationRule,
    BinanceSpotAdapter,
    BinanceFuturesAdapter,
    MarketContext,
)
from lifecycle.base import Side


def _config() -> LifecycleConfig:
    return LifecycleConfig(
        regime="SPOT_BULL",
        profile="A",
        signal_policy=SignalPolicy(
            min_signal_strength=0.0,
            min_success_probability=0.0,
            min_expected_pnl=-9999.0,
            min_liquidity_usdt=0.0,
            categories_enabled=("LARGE_CAP",),
            max_candidates=1,
            diversification_mode="none",
        ),
        entry_policy=EntryPolicy(
            min_composite_score=0.0,
            min_expected_pnl=-9999.0,
            min_success_probability=0.0,
            min_liquidity_usdt=0.0,
            max_total_positions=1,
            max_category_positions={"LARGE_CAP": 1},
        ),
        sizing_policy=SizingPolicy(
            regime_allocation_pct=0.8,
            max_per_trade_usdt=100.0,
            kelly_base_pct=0.1,
            kelly_max_pct=0.2,
            conviction_max_pct=0.2,
            category_caps={"LARGE_CAP": 0.5},
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


class FakeBinanceClient:
    def __init__(self):
        self.orders = []

    def get_symbol_price(self, symbol):
        return 100.0

    def create_market_order(self, **kwargs):
        self.orders.append(kwargs)
        return {"orderId": 1, "status": "FILLED", "fills": [{"qty": 1.0, "price": 100.0, "commission": 0.1, "commissionAsset": "USDT"}]}

    def verify_and_fetch_fills(self, result, label=""):
        return {"qty": 1.0, "avg_price": 100.0, "commission": 0.1}


class FakePositionManager:
    def __init__(self):
        self.positions = []
        self.closed = []

    def get_open_positions_summary(self):
        return self.positions

    def add_position(self, pos):
        self.positions.append({"symbol": pos.symbol, "side": pos.side, "quantity": pos.quantity, "entry_price": pos.entry_price, "metadata": pos.metadata})

    def close_all_positions_for_symbol(self, symbol, price, reason, commission_rate):
        self.closed.append({"symbol": symbol, "price": price, "reason": reason})


class FakeFuturesClient:
    def __init__(self):
        self.calls = []

    def get_price(self, symbol):
        return 100.0

    def open_short(self, **kwargs):
        self.calls.append(("open_short", kwargs))
        from futures_client import FuturesPosition
        return FuturesPosition(
            symbol=kwargs["symbol"], side="SHORT", qty=1.0, entry_price=100.0,
            sl_price=110.0, tp_price=90.0, order_id="1", sl_order_id="1", tp_order_id="1"
        )

    def close_short(self, symbol, qty=None, reason=""):
        self.calls.append(("close_short", {"symbol": symbol, "qty": qty, "reason": reason}))
        return {"symbol": symbol, "qty": qty}

    def get_open_positions_summary(self):
        return []


def test_spot_adapter_context():
    adapter = BinanceSpotAdapter(
        binance_client=FakeBinanceClient(),
        position_manager=FakePositionManager(),
        db_client=None,
        fng_resolver=lambda: 55,
        btc_dom_resolver=lambda: 60.0,
    )
    ctx = adapter.build_context(_config())
    assert isinstance(ctx, MarketContext)
    assert ctx.regime == "SPOT_BULL"
    assert ctx.fear_greed_index == 55
    assert ctx.btc_dominance == 60.0


def test_spot_adapter_entry():
    from lifecycle.base import Signal, EntryDecision, SizedTrade
    from lifecycle.sizing import PositionSizer
    pm = FakePositionManager()
    adapter = BinanceSpotAdapter(
        binance_client=FakeBinanceClient(),
        position_manager=pm,
        db_client=None,
    )
    signal = Signal(symbol="BTCUSDT", side=Side.LONG, category="LARGE_CAP", composite_score=0.7,
                    expected_pnl=1.0, success_probability=0.6, signal_strength=0.7,
                    regime_alignment=1.0, liquidity_usdt_24h=1_000_000_000)
    entry = EntryDecision(signal=signal, approved=True, entry_price=100.0, atr=2.0)
    sizer = PositionSizer()
    ctx = adapter.build_context(_config())
    budget = sizer.build_budget(total_liquid=1000.0, positions=[], policy=_config().sizing_policy)
    trade = sizer.size(entry, budget, ctx, _config().sizing_policy)
    result = adapter.execute_entry(trade, _config())
    assert result["filled"]
    assert len(pm.positions) == 1


def test_spot_adapter_exit():
    from lifecycle.base import Position, ExitDecision
    from lifecycle.base import Action
    pm = FakePositionManager()
    adapter = BinanceSpotAdapter(
        binance_client=FakeBinanceClient(),
        position_manager=pm,
        db_client=None,
    )
    pos = Position(symbol="BTCUSDT", side=Side.LONG, quantity=1.0, entry_price=100.0,
                   entry_time=0.0, position_usdt=100.0, category="LARGE_CAP",
                   regime_at_entry="SPOT_BULL", profile_at_entry="A")
    decision = ExitDecision(symbol="BTCUSDT", action=Action.CLOSE_ALL, reason="hard TP", priority=70, pct=1.0)
    result = adapter.execute_exit(decision, pos, _config())
    assert result["filled"]
    assert len(pm.closed) == 1


def test_futures_adapter_entry_short():
    from lifecycle.base import Signal, EntryDecision, SizedTrade
    from lifecycle.sizing import PositionSizer
    fc = FakeFuturesClient()
    adapter = BinanceFuturesAdapter(futures_client=fc, db_client=None)
    signal = Signal(symbol="BTCUSDT", side=Side.SHORT, category="LARGE_CAP", composite_score=0.7,
                    expected_pnl=1.0, success_probability=0.6, signal_strength=0.7,
                    regime_alignment=1.0, liquidity_usdt_24h=1_000_000_000)
    entry = EntryDecision(signal=signal, approved=True, entry_price=100.0, atr=2.0)
    sizer = PositionSizer()
    ctx = adapter.build_context(_config())
    budget = sizer.build_budget(total_liquid=1000.0, positions=[], policy=_config().sizing_policy)
    trade = sizer.size(entry, budget, ctx, _config().sizing_policy)
    result = adapter.execute_entry(trade, _config())
    assert result["filled"]
    assert fc.calls[0][0] == "open_short"


def test_futures_adapter_exit_short():
    from lifecycle.base import Position, ExitDecision, Action
    fc = FakeFuturesClient()
    adapter = BinanceFuturesAdapter(futures_client=fc, db_client=None)
    pos = Position(symbol="BTCUSDT", side=Side.SHORT, quantity=1.0, entry_price=100.0,
                   entry_time=0.0, position_usdt=100.0, category="LARGE_CAP",
                   regime_at_entry="FUTURES_SHORT", profile_at_entry="A")
    decision = ExitDecision(symbol="BTCUSDT", action=Action.CLOSE_ALL, reason="hard TP", priority=70, pct=1.0)
    result = adapter.execute_exit(decision, pos, _config())
    assert result["filled"]
    assert fc.calls[0][0] == "close_short"
