"""
tests/test_futures_usd_loss_guard.py
Regression tests for USD-denominated loss guardrail in futures.
"""

import sys
import os
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from futures_client import FuturesClient, FuturesPosition


def _make_settings(**overrides):
    defaults = dict(
        futures_trailing_enabled=True,
        futures_trailing_activation_pct=2.0,
        futures_trailing_distance_pct=1.0,
        futures_breakeven_activation_pct=1.0,
        futures_smart_tp_enabled=True,
        futures_smart_tp_max_extensions=2,
        futures_smart_tp_max_pnl_pct=12.0,
        futures_smart_tp_extend_score_threshold=3,
        futures_smart_tp_extension_pct=2.0,
        smart_tp_extend_disabled_regimes=["RANGE_SCALP", "PRESERVATION"],
        futures_trail_atr_enabled=False,
        futures_trail_atr_multiplier_low=1.5,
        futures_trail_atr_multiplier_mid=1.2,
        futures_trail_atr_multiplier_high=1.0,
        futures_trail_atr_multiplier_deep=0.8,
        futures_trail_floor_pct=0.4,
        futures_trail_ceiling_pct=1.2,
        max_loss_usd_per_trade=18.0,
        emergency_gap_loss_multiplier=1.5,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _make_fc(positions=None):
    fc = FuturesClient(api_key="test", api_secret="test", dry_run=True)
    if positions:
        for pos in positions:
            fc._open_positions[FuturesClient._pos_key(pos.symbol, pos.side)] = pos
    return fc


def _inject_price(fc, symbol, price):
    fc._price_cache[symbol] = (price, time.monotonic() + 3600)


def _make_short(symbol="BTCUSDT", entry=1000.0, qty=0.1):
    return FuturesPosition(
        symbol=symbol,
        side="SHORT",
        qty=qty,
        entry_price=entry,
        sl_price=entry * 1.03,
        tp_price=entry * 0.97,
        order_id="TEST_ORDER",
        sl_order_id="TEST_SL",
        tp_order_id="TEST_TP",
    )


def _make_long(symbol="ETHUSDT", entry=1000.0, qty=0.1):
    return FuturesPosition(
        symbol=symbol,
        side="LONG",
        qty=qty,
        entry_price=entry,
        sl_price=entry * 0.975,
        tp_price=entry * 1.05,
        order_id="TEST_LONG_ORDER",
        sl_order_id="TEST_LONG_SL",
        tp_order_id="TEST_LONG_TP",
    )


class TestFuturesUSDEmergencyGapStop:
    """Validate emergency USD gap stop closes positions beyond 1.5x the USD cap."""

    def test_short_emergency_gap_stop_fires(self):
        pos = _make_short(entry=1000.0, qty=0.1)  # notional $100
        fc = _make_fc([pos])
        # price $1320 => gross loss = (1000 - 1320) * 0.1 = -$32, net > -$27 threshold
        _inject_price(fc, "BTCUSDT", 1320.0)
        closed = fc.check_positions(settings=_make_settings())
        assert len(closed) == 1
        assert "emergency_gap_stop" in closed[0]["reason"]
        assert closed[0]["side"] == "SHORT"

    def test_long_emergency_gap_stop_fires(self):
        pos = _make_long(entry=1000.0, qty=0.1)  # notional $100
        fc = _make_fc([pos])
        # price $680 => gross loss = (680 - 1000) * 0.1 = -$32
        _inject_price(fc, "ETHUSDT", 680.0)
        closed = fc.check_positions(settings=_make_settings())
        assert len(closed) == 1
        assert "emergency_gap_stop" in closed[0]["reason"]
        assert closed[0]["side"] == "LONG"

    def test_gap_stop_does_not_fire_within_threshold(self):
        pos = _make_long(entry=1000.0, qty=0.1)
        fc = _make_fc([pos])
        # price $960 => gross loss = -$4, well below $27 threshold
        _inject_price(fc, "ETHUSDT", 960.0)
        closed = fc.check_positions(settings=_make_settings())
        assert not any("emergency_gap_stop" in c.get("reason", "") for c in closed)

    def test_gap_stop_disabled_when_max_loss_zero(self):
        pos = _make_long(entry=1000.0, qty=0.1)
        fc = _make_fc([pos])
        _inject_price(fc, "ETHUSDT", 500.0)
        closed = fc.check_positions(
            settings=_make_settings(max_loss_usd_per_trade=0.0, emergency_gap_loss_multiplier=1.5)
        )
        assert not any("emergency_gap_stop" in c.get("reason", "") for c in closed)
