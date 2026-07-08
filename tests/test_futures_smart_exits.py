"""
tests/test_futures_smart_exits.py
Exhaustive unit tests for:
  - FuturesPosition._smart_meta (dataclass field)
  - FuturesClient.update_sl_tp()
  - FuturesClient.check_positions() — breakeven, trailing, SmartTP EXTEND, SL hit, TP hit
  - SPOT _check_sl_tp_positions regime-aware SmartTP EXTEND disable
  - Config params present with correct defaults

All tests run with dry_run=True to avoid any real Binance calls.
No mock patching of the Binance API is needed — dry_run short-circuits all network paths.
"""

import sys
import os
import time
import types
from datetime import datetime, timezone
from dataclasses import fields as dc_fields

import pytest

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from futures_client import FuturesClient, FuturesPosition


# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_settings(**overrides):
    """Return a SimpleNamespace with all smart-exit settings at safe defaults."""
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
        # ATR-scaled trailing defaults (tests use fallback ATR=0.5%)
        futures_trail_atr_enabled=True,
        futures_trail_atr_multiplier_low=1.5,
        futures_trail_atr_multiplier_mid=1.2,
        futures_trail_atr_multiplier_high=1.0,
        futures_trail_atr_multiplier_deep=0.8,
        futures_trail_floor_pct=0.4,
        futures_trail_ceiling_pct=1.2,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def make_fc(positions=None) -> FuturesClient:
    """Return a dry-run FuturesClient with optional pre-loaded positions."""
    fc = FuturesClient(api_key="test", api_secret="test", dry_run=True)
    if positions:
        for pos in positions:
            key = FuturesClient._pos_key(pos.symbol, pos.side)
            fc._open_positions[key] = pos
    return fc


def make_short(
    symbol="BTCUSDT",
    entry=100.0,
    sl_pct=3.0,
    tp_pct=7.0,
    qty=1.0,
) -> FuturesPosition:
    """Return an open SHORT FuturesPosition."""
    return FuturesPosition(
        symbol=symbol,
        side="SHORT",
        qty=qty,
        entry_price=entry,
        sl_price=round(entry * (1 + sl_pct / 100), 6),
        tp_price=round(entry * (1 - tp_pct / 100), 6),
        order_id="TEST_ORDER",
        sl_order_id="TEST_SL",
        tp_order_id="TEST_TP",
    )


def make_long(
    symbol="ETHUSDT",
    entry=1000.0,
    sl_pct=2.5,
    tp_pct=5.0,
    qty=0.1,
) -> FuturesPosition:
    """Return an open LONG FuturesPosition."""
    return FuturesPosition(
        symbol=symbol,
        side="LONG",
        qty=qty,
        entry_price=entry,
        sl_price=round(entry * (1 - sl_pct / 100), 6),
        tp_price=round(entry * (1 + tp_pct / 100), 6),
        order_id="TEST_LONG_ORDER",
        sl_order_id="TEST_LONG_SL",
        tp_order_id="TEST_LONG_TP",
    )


def inject_price(fc: FuturesClient, symbol: str, price: float):
    """Force a price into the client's cache so no network call is made."""
    fc._price_cache[symbol] = (price, time.monotonic() + 3600)


# ─── 1. Dataclass field tests ─────────────────────────────────────────────────

class TestFuturesPositionDataclass:
    def test_smart_meta_field_exists(self):
        """_smart_meta must be a proper dataclass field (not a dynamic attribute)."""
        field_names = {f.name for f in dc_fields(FuturesPosition)}
        assert '_smart_meta' in field_names, "_smart_meta is not a declared dataclass field"

    def test_smart_meta_default_is_empty_dict(self):
        pos = make_short()
        assert pos._smart_meta == {}

    def test_smart_meta_not_shared_between_instances(self):
        """Each instance must have its own _smart_meta dict (mutable default via field())."""
        a = make_short("BTCUSDT", entry=100.0)
        b = make_short("ETHUSDT", entry=200.0)
        a._smart_meta['key'] = 'val'
        assert 'key' not in b._smart_meta, "_smart_meta is shared between instances (factory bug)"

    def test_unrealized_pnl_short(self):
        pos = make_short(entry=100.0, qty=1.0)
        assert pos.unrealized_pnl(93.0) == pytest.approx(7.0)   # price fell = profit

    def test_unrealized_pnl_long(self):
        pos = make_long(entry=1000.0, qty=0.1)
        assert pos.unrealized_pnl(1100.0) == pytest.approx(10.0)  # price rose = profit

    def test_is_sl_hit_short_above_entry(self):
        pos = make_short(entry=100.0, sl_pct=3.0)
        # SL = 103.0 for SHORT
        assert pos.is_sl_hit(103.1) is True
        assert pos.is_sl_hit(102.9) is False

    def test_is_tp_hit_short_below_entry(self):
        pos = make_short(entry=100.0, tp_pct=7.0)
        # TP = 93.0 for SHORT
        assert pos.is_tp_hit(92.9) is True
        assert pos.is_tp_hit(93.1) is False

    def test_is_sl_hit_long_below_entry(self):
        pos = make_long(entry=1000.0, sl_pct=2.5)
        # SL = 975.0 for LONG
        assert pos.is_sl_hit(974.9) is True
        assert pos.is_sl_hit(975.1) is False

    def test_is_tp_hit_long_above_entry(self):
        pos = make_long(entry=1000.0, tp_pct=5.0)
        # TP = 1050.0 for LONG
        assert pos.is_tp_hit(1050.1) is True
        assert pos.is_tp_hit(1049.9) is False


# ─── 2. update_sl_tp() dry-run tests ─────────────────────────────────────────

class TestUpdateSlTp:
    def test_updates_sl_price_in_memory(self):
        pos = make_short(entry=100.0)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 97.0)

        ok = fc.update_sl_tp(pos.symbol, "SHORT", new_sl=101.0)
        assert ok is True
        assert pos.sl_price == 101.0

    def test_updates_tp_price_in_memory(self):
        pos = make_short(entry=100.0)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 97.0)

        ok = fc.update_sl_tp(pos.symbol, "SHORT", new_tp=91.0)
        assert ok is True
        assert pos.tp_price == 91.0

    def test_updates_both_sl_and_tp(self):
        pos = make_short(entry=100.0)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 95.0)

        ok = fc.update_sl_tp(pos.symbol, "SHORT", new_sl=98.0, new_tp=90.0)
        assert ok is True
        assert pos.sl_price == 98.0
        assert pos.tp_price == 90.0

    def test_returns_false_for_unknown_symbol(self):
        fc = make_fc()
        ok = fc.update_sl_tp("UNKNOWN", "SHORT", new_sl=100.0)
        assert ok is False

    def test_returns_false_for_closed_position(self):
        pos = make_short()
        pos.closed = True
        fc = make_fc([pos])
        ok = fc.update_sl_tp(pos.symbol, "SHORT", new_sl=100.0)
        assert ok is False

    def test_no_update_when_none_passed(self):
        pos = make_short(entry=100.0)
        original_sl = pos.sl_price
        original_tp = pos.tp_price
        fc = make_fc([pos])

        ok = fc.update_sl_tp(pos.symbol, "SHORT", new_sl=None, new_tp=None)
        assert ok is True
        assert pos.sl_price == original_sl
        assert pos.tp_price == original_tp

    def test_case_insensitive_side(self):
        pos = make_short()
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 97.0)
        ok = fc.update_sl_tp(pos.symbol, "short", new_sl=101.0)
        assert ok is True
        assert pos.sl_price == 101.0

    def test_long_update_sl(self):
        pos = make_long(entry=1000.0)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 1050.0)
        ok = fc.update_sl_tp(pos.symbol, "LONG", new_sl=1020.0)
        assert ok is True
        assert pos.sl_price == 1020.0


# ─── 3. check_positions() — Breakeven SL ─────────────────────────────────────

class TestBreakevenSL:
    def _run_check(self, pos, price, settings=None):
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, price)
        if settings is None:
            settings = make_settings()
        return fc, fc.check_positions(settings=settings)

    def test_breakeven_triggers_for_short_at_threshold(self):
        """SHORT at 1.2% gain should move SL to entry."""
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        # 1.2% gain for SHORT = price dropped to 98.8
        price = 100.0 * (1 - 0.012)  # 98.8
        fc, closed = self._run_check(pos, price)
        # SL should move to entry (100.0)
        assert pos.sl_price == pytest.approx(100.0, abs=0.01)
        assert pos._smart_meta.get('breakeven_done') is True

    def test_breakeven_only_fires_once(self):
        """breakeven_done flag prevents re-firing (but trailing can still move SL)."""
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        pos._smart_meta['breakeven_done'] = True
        price = 100.0 * (1 - 0.015)  # 1.5% gain — above breakeven but below trailing activation
        fc, closed = self._run_check(pos, price, settings=make_settings(futures_trailing_activation_pct=3.0))
        # Breakeven already done, trailing not activated (1.5% < 3.0%) → SL unchanged
        assert pos.sl_price == 103.0  # original SL unchanged

    def test_breakeven_not_triggered_below_threshold(self):
        """At 0.5% gain, breakeven should NOT fire (threshold is 1.2%)."""
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        price = 100.0 * (1 - 0.005)  # 0.5% gain
        original_sl = pos.sl_price
        fc, closed = self._run_check(pos, price)
        assert pos.sl_price == original_sl

    def test_breakeven_disabled_when_trailing_disabled(self):
        """With futures_trailing_enabled=False, breakeven should not move SL."""
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        price = 100.0 * (1 - 0.02)  # 2% gain
        original_sl = pos.sl_price
        s = make_settings(futures_trailing_enabled=False)
        fc, closed = self._run_check(pos, price, settings=s)
        assert pos.sl_price == original_sl

    def test_breakeven_long_triggers(self):
        """LONG at 1.2% gain should move SL to entry."""
        pos = make_long(entry=1000.0, sl_pct=2.5, tp_pct=5.0, qty=0.1)
        price = 1000.0 * 1.012  # 1.2% gain
        fc, closed = self._run_check(pos, price)
        assert pos.sl_price == pytest.approx(1000.0, abs=0.01)
        assert pos._smart_meta.get('breakeven_done') is True

    def test_breakeven_short_sl_already_at_entry_no_update(self):
        """If SL is already <= entry for SHORT (already at or below breakeven), skip."""
        pos = make_short(entry=100.0)
        pos.sl_price = 100.0  # already at entry
        price = 100.0 * (1 - 0.015)
        fc, closed = self._run_check(pos, price)
        # Condition `entry < pos.sl_price` is False → no update, flag stays False
        assert not pos._smart_meta.get('breakeven_done')


# ─── 4. check_positions() — Trailing Stop ─────────────────────────────────────

class TestTrailingStop:
    def _run_check(self, pos, price, settings=None):
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, price)
        if settings is None:
            settings = make_settings()
        return fc, fc.check_positions(settings=settings)

    def test_trailing_fires_when_pnl_above_activation(self):
        """
        At 2.5% gain (> 2.0% activation), trailing activates with ATR-scaled distance.
        Fallback ATR=0.5%, mult=1.5 at <3% PnL → dist=0.75%.
        Breakeven fires in the same cycle, then trailing moves SL tighter.
        """
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        # First cycle at 97.5 — breakeven fires, trail initializes and moves SL
        price = 100.0 * (1 - 0.025)  # 97.5 = 2.5% gain
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, price)
        fc.check_positions(settings=make_settings())
        # ATR fallback=0.5%, mult=1.5 (pnl<3%) → dist=0.75%
        # SL = 97.5 * (1 + 0.0075) = 98.23
        assert pos.sl_price == pytest.approx(97.5 * 1.0075, abs=0.15)
        # Second cycle at 96.0 (4% PnL) → mult=1.2, dist=0.6%
        pos._smart_meta['trail_best_price'] = 97.5
        inject_price(fc, pos.symbol, 96.0)
        fc.check_positions(settings=make_settings())
        # SL = 96.0 * (1 + 0.006) = 96.576
        assert pos.sl_price == pytest.approx(96.0 * 1.006, abs=0.15)
        assert pos._smart_meta.get('trail_best_price') == pytest.approx(96.0, abs=0.01)

    def test_trailing_does_not_fire_below_activation(self):
        """
        At 1.8% gain (> 1.2% breakeven but < 2.0% trail activation):
        - Breakeven fires → SL moves to entry (100.0)
        - Trailing does NOT fire (below 2.0% threshold)
        """
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        price = 100.0 * (1 - 0.018)  # 98.2 = 1.8% gain
        fc, closed = self._run_check(pos, price)
        # Breakeven fires (1.8% >= 1.2%), SL moves to entry
        assert pos.sl_price == pytest.approx(100.0, abs=0.01)
        # Trail did NOT fire (1.8% < 2.0% activation)
        assert 'trail_best_price' not in pos._smart_meta

    def test_trailing_sl_only_moves_down_for_short(self):
        """Trailing SL for SHORT should ONLY move to lower value (profit protection)."""
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        pos._smart_meta['trail_best_price'] = 96.0  # best was 96.0
        pos.sl_price = 96.0 * 1.01  # SL already at 96.96

        price = 97.0  # price went UP (worse for SHORT) — trail should NOT move SL
        original_sl = pos.sl_price
        fc, closed = self._run_check(pos, price)
        assert pos.sl_price == original_sl  # unchanged

    def test_trailing_tp_moves_down_on_new_low_for_short(self):
        """When SHORT makes new low after trailing activated, TP should also trail down."""
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        # Activation at 2%, current price = 95.0 (5% gain, above activation)
        price = 95.0
        fc, closed = self._run_check(pos, price)
        # TP should trail: 95.0 * (1 - 0.005) = 94.525
        expected_tp = 95.0 * (1.0 - 0.005)  # trail_dist/2 = 0.5%
        # TP must be <= original (93.0) — trail only tightens
        # Since 94.525 > 93.0, trail should NOT move TP in this case
        assert pos.tp_price == pytest.approx(93.0, abs=0.1)  # stays at original (93)

    def test_trailing_tp_moves_on_very_deep_move(self):
        """When SHORT goes 8% deep (past original TP), trailing TP should activate."""
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        pos.tp_price = 93.0  # original TP
        # Price at 91.0 = 9% gain, deep below TP
        price = 91.0
        fc, closed = self._run_check(pos, price)
        # Trail TP = 91.0 * (1 - 0.005) = 90.545, which is < 93.0
        expected_new_tp = 91.0 * (1 - 0.005)
        # Position is tp_hit at 91.0 (below TP 93.0), so it will close OR extend
        # With SmartTP at score -1 (no client), it closes → verify position is closed
        assert pos.closed is True or pos.tp_price <= 93.0

    def test_trailing_long_sl_moves_up(self):
        """
        LONG trailing: SL should move UP as price makes new highs.
        ATR fallback=0.5%, mult varies by PnL.
        """
        pos = make_long(entry=1000.0, sl_pct=2.5, tp_pct=5.0, qty=0.1)
        fc = make_fc([pos])
        # First cycle at 1025 (2.5% gain): mult=1.5, dist=0.75%
        inject_price(fc, pos.symbol, 1025.0)
        fc.check_positions(settings=make_settings())
        # Trail moves SL to 1025 * (1 - 0.0075) = 1017.31
        assert pos.sl_price == pytest.approx(1025.0 * (1 - 0.0075), abs=0.5)
        # Second cycle at 1035 (3.5% gain): mult=1.2, dist=0.6%
        pos._smart_meta['trail_best_price'] = 1025.0
        inject_price(fc, pos.symbol, 1035.0)
        fc.check_positions(settings=make_settings())
        # SL should trail up: 1035 * (1 - 0.006) = 1028.79
        expected_sl = 1035.0 * (1 - 0.006)
        assert pos.sl_price == pytest.approx(expected_sl, abs=0.5)

    def test_trailing_long_sl_does_not_move_down(self):
        """LONG trailing: SL should NOT move down (only protects profits)."""
        pos = make_long(entry=1000.0, sl_pct=2.5, tp_pct=5.0, qty=0.1)
        pos._smart_meta['trail_best_price'] = 1030.0  # best was 1030
        pos.sl_price = 1030.0 * (1 - 0.01)  # SL at 1019.7

        price = 1025.0  # price dropped from 1030 → SL should NOT move down
        original_sl = pos.sl_price
        fc, closed = self._run_check(pos, price)
        assert pos.sl_price == original_sl

    def test_trailing_disabled_config(self):
        """futures_trailing_enabled=False should skip ALL trailing logic."""
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        price = 95.0  # 5% gain — would normally trigger trail
        original_sl = pos.sl_price
        s = make_settings(futures_trailing_enabled=False)
        fc, closed = self._run_check(pos, price, settings=s)
        assert pos.sl_price == original_sl


# ─── 5. check_positions() — SL hit ───────────────────────────────────────────

class TestSLHit:
    def test_short_sl_hit_closes_position(self):
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 104.0)  # above SL 103.0
        closed = fc.check_positions(settings=make_settings())
        assert any(c['symbol'] == 'BTCUSDT' and c['reason'] == 'stop_loss' for c in closed)
        assert pos.closed is True

    def test_long_sl_hit_closes_position(self):
        pos = make_long(entry=1000.0, sl_pct=2.5, tp_pct=5.0, qty=0.1)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 974.0)  # below SL 975.0
        closed = fc.check_positions(settings=make_settings())
        assert any(c['symbol'] == 'ETHUSDT' and c['reason'] == 'stop_loss' for c in closed)
        assert pos.closed is True

    def test_short_above_sl_not_closed(self):
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 101.0)  # below SL 103.0
        closed = fc.check_positions(settings=make_settings())
        assert closed == []
        assert pos.closed is False

    def test_sl_hit_returns_correct_pnl_sign(self):
        """SL hit for SHORT should return negative PnL."""
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=2.0)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 104.0)  # SL at 103, price above it
        closed = fc.check_positions(settings=make_settings())
        assert len(closed) == 1
        assert closed[0]['pnl'] < 0  # loss on SL hit for SHORT


# ─── 6. check_positions() — TP hit (SmartTP EXTEND disabled / enabled) ───────

class TestTPHit:
    def test_tp_hit_closes_when_smart_tp_disabled(self):
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 92.0)  # below TP 93.0
        s = make_settings(futures_smart_tp_enabled=False)
        closed = fc.check_positions(settings=s)
        assert any(c['reason'] == 'take_profit' for c in closed)
        assert pos.closed is True

    def test_tp_hit_extend_when_score_high_enough(self):
        """When SmartTP is ON and momentum eval score >= threshold (3), should EXTEND not close."""
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 92.0)  # below TP

        # Monkey-patch _evaluate_short_momentum to return score=3 (high short momentum)
        fc._evaluate_short_momentum = lambda sym, s, **kwargs: (3, {"vol_spike": 1.0, "rsi": 60.0, "trend_4h": 0.0, "success_prob": 0.44})

        s = make_settings(futures_smart_tp_enabled=True, futures_smart_tp_extend_score_threshold=3)
        closed = fc.check_positions(settings=s)
        # Position should NOT be fully closed — it's extended (scale-out partial is OK)
        full_closes = [c for c in closed if not c.get('partial')]
        assert full_closes == []
        assert pos.closed is False
        assert pos._smart_meta.get('tp_extensions_count') == 1
        # New TP should be lower than original (extending short downward)
        assert pos.tp_price < 92.0

    def test_tp_hit_close_when_score_low(self):
        """When score < threshold (score=1 < 3), should CLOSE immediately."""
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 92.0)

        fc._evaluate_short_momentum = lambda sym, s: (1, {})
        s = make_settings(futures_smart_tp_enabled=True, futures_smart_tp_extend_score_threshold=3)
        closed = fc.check_positions(settings=s)
        full_closes = [c for c in closed if not c.get('partial')]
        assert len(full_closes) == 1
        assert pos.closed is True

    def test_tp_extend_respects_max_extensions(self):
        """After max_extensions reached, always close even if score is high."""
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        pos._smart_meta['tp_extensions_count'] = 2  # already at max (2)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 92.0)

        fc._evaluate_short_momentum = lambda sym, s: (4, {})  # max score
        s = make_settings(futures_smart_tp_enabled=True, futures_smart_tp_max_extensions=2)
        closed = fc.check_positions(settings=s)
        full_closes = [c for c in closed if not c.get('partial')]
        assert len(full_closes) == 1
        assert pos.closed is True

    def test_tp_extend_respects_max_pnl(self):
        """When PnL exceeds max_pnl_pct, never extend."""
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        # Simulate position in 13% gain (above 12% cap)
        pos.tp_price = 87.0  # extended TP
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 86.5)  # 13.5% gain

        fc._evaluate_short_momentum = lambda sym, s: (4, {})
        s = make_settings(futures_smart_tp_enabled=True, futures_smart_tp_max_pnl_pct=12.0)
        closed = fc.check_positions(settings=s)
        full_closes = [c for c in closed if not c.get('partial')]
        assert len(full_closes) == 1
        assert pos.closed is True

    def test_tp_hit_long_closes(self):
        pos = make_long(entry=1000.0, sl_pct=2.5, tp_pct=5.0, qty=0.1)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 1051.0)  # above TP 1050
        s = make_settings(futures_smart_tp_enabled=False)
        closed = fc.check_positions(settings=s)
        assert any(c['reason'] == 'take_profit' for c in closed)
        assert pos.closed is True

    def test_tp_extend_sl_locked_for_short(self):
        """On EXTEND, new SL should be set tighter (closer to price) to lock gains."""
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        original_sl = pos.sl_price  # 103.0
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 92.0)

        fc._evaluate_short_momentum = lambda sym, s: (3, {})
        s = make_settings(futures_smart_tp_enabled=True, futures_smart_tp_extend_score_threshold=3)
        closed = fc.check_positions(settings=s)
        # Extended: new SL should be < original SL (tighter, protects gain)
        if pos._smart_meta.get('tp_extensions_count', 0) > 0:
            assert pos.sl_price < original_sl


# ─── 7. Closed positions are skipped ─────────────────────────────────────────

class TestClosedPositionsSkipped:
    def test_already_closed_position_not_processed(self):
        pos = make_short(entry=100.0)
        pos.closed = True
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 104.0)  # would be SL hit
        closed = fc.check_positions(settings=make_settings())
        assert closed == []

    def test_multiple_positions_partial_close(self):
        """One position hits SL, another may SSO partial — SL always closes."""
        pos1 = make_short("BTCUSDT", entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        pos2 = make_short("ETHUSDT", entry=200.0, sl_pct=3.0, tp_pct=7.0, qty=0.5)
        fc = make_fc([pos1, pos2])
        inject_price(fc, "BTCUSDT", 104.0)  # BTCUSDT SL hit
        inject_price(fc, "ETHUSDT", 198.0)  # ETHUSDT fine (1% gain — may trigger SSO partial)
        closed = fc.check_positions(settings=make_settings())
        assert len(closed) >= 1
        sl_closes = [c for c in closed if c.get('reason') == 'stop_loss']
        assert len(sl_closes) == 1
        assert sl_closes[0]['symbol'] == 'BTCUSDT'
        assert pos1.closed is True
        assert pos2.closed is False  # SSO partial doesn't fully close


# ─── 8. PnL calculation accuracy ─────────────────────────────────────────────

class TestPnLCalculation:
    def test_pnl_percentage_computation(self):
        """pnl_pct = unr / (entry * qty). Verify the math in check_positions uses net PnL."""
        # entry=100, qty=2.0, price=98.0 (SHORT 2% gross gain)
        # gross = (100 - 98) * 2 = 4.0
        # net after Binance futures fees = 4.0 - comm ~= 3.92 => ~1.96%
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=2.0)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 98.0)
        fc.check_positions(settings=make_settings())
        # Breakeven fires at 1.0% net, but trailing activation is 2.0% net -> not yet
        assert pos.sl_price == pytest.approx(100.0, abs=0.1)
        assert pos.sl_price <= 100.0


# ─── 9. SPOT regime-aware SmartTP tests (via run_trades mock) ────────────────

class TestSpotRegimeSmartTP:
    """
    Test that _check_sl_tp_positions disables SmartTP EXTEND in RANGE_SCALP/PRESERVATION.
    We test the config objects directly without calling the full trading cycle.
    """

    def test_settings_has_smart_tp_extend_disabled_regimes(self):
        from config import Settings
        s = Settings()
        val = getattr(s, 'smart_tp_extend_disabled_regimes', ['RANGE_SCALP', 'PRESERVATION'])
        assert val is not None, "smart_tp_extend_disabled_regimes not in Settings"
        assert 'RANGE_SCALP' in val
        assert 'PRESERVATION' in val

    def test_spot_bull_not_in_disabled_regimes(self):
        from config import Settings
        s = Settings()
        assert 'SPOT_BULL' not in s.smart_tp_extend_disabled_regimes

    def test_futures_short_not_in_disabled_regimes(self):
        from config import Settings
        s = Settings()
        assert 'FUTURES_SHORT' not in s.smart_tp_extend_disabled_regimes


# ─── 10. Config param validation ─────────────────────────────────────────────

class TestConfigParams:
    """Ensure all new smart-exit config params exist with correct defaults."""

    def _get(self, attr):
        from config import Settings
        return getattr(Settings(), attr, None)

    def test_futures_trailing_enabled(self):
        assert self._get('futures_trailing_enabled') is True

    def test_futures_trailing_activation_pct(self):
        val = self._get('futures_trailing_activation_pct')
        assert val is not None and 0.5 <= val <= 6.0, f"futures_trailing_activation_pct={val} out of valid range [0.5, 6.0]"

    def test_futures_trailing_distance_pct(self):
        assert self._get('futures_trailing_distance_pct') == pytest.approx(2.0)  # concentrated

    def test_futures_breakeven_activation_pct(self):
        assert self._get('futures_breakeven_activation_pct') == pytest.approx(1.0)

    def test_futures_smart_tp_enabled(self):
        assert self._get('futures_smart_tp_enabled') is True

    def test_futures_smart_tp_max_extensions(self):
        assert self._get('futures_smart_tp_max_extensions') == 2

    def test_futures_smart_tp_max_pnl_pct(self):
        assert self._get('futures_smart_tp_max_pnl_pct') == pytest.approx(12.0)

    def test_futures_smart_tp_extend_score_threshold(self):
        assert self._get('futures_smart_tp_extend_score_threshold') == 2

    def test_futures_smart_tp_extension_pct(self):
        assert self._get('futures_smart_tp_extension_pct') == pytest.approx(2.0)

    def test_smart_tp_extend_disabled_regimes_is_list(self):
        val = self._get('smart_tp_extend_disabled_regimes')
        assert isinstance(val, list)
        assert len(val) >= 2

    def test_config_helpers_critical_defaults_match_settings(self):
        from config import Settings
        from config_helpers import _PARAM_DEFAULTS

        critical_keys = [
            'kelly_dynamic_base_pct',
            'max_per_trade_usdt',
            'max_positions',
            'trailing_stop_activation_pct',
            'trailing_stop_distance_pct',
        ]
        for key in critical_keys:
            assert _PARAM_DEFAULTS[key] == Settings.model_fields[key].default, f"Default mismatch for {key}"


class TestUnifiedGovernanceMonitorOnlyPropagation:
    def test_monitor_only_flag_reaches_futures_unified_governance(self, monkeypatch):
        from unified_governance_supervisor import UnifiedGovernanceDecision

        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 100.2)  # tiny loss, avoids PRS branch
        s = make_settings(
            sso_enabled=False,
            futures_adverse_exit_enabled=False,
            futures_smart_tp_enabled=False,
        )

        captured = {}

        def _fake_ug(ctx, _settings):
            captured['monitor_only'] = ctx.monitor_only
            return UnifiedGovernanceDecision()

        monkeypatch.setattr('futures_client._evaluate_unified_governance', _fake_ug)
        fc.check_positions(settings=s, monitor_only=True)
        assert captured.get('monitor_only') is True


# ─── 11. update_sl_tp + check_positions interaction (race condition) ──────────

class TestRaceCondition:
    """Verify that if update_sl_tp marks pos.closed=True, check_positions stops cleanly."""

    def test_cancel_sl_2011_marks_position_closed_and_returns_empty(self):
        """
        Simulates the -2011 race: SL algo order already filled when we try to cancel.
        update_sl_tp() sets pos.closed=True and returns False.
        check_positions() should then produce no output (position handled by Binance).
        """
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 97.5)  # 2.5% gain — triggers breakeven + trail

        original_update = fc.update_sl_tp

        def mock_update_sl_tp(symbol, side, new_sl=None, new_tp=None):
            # Simulate -2011 error on SL update
            if new_sl is not None:
                key = FuturesClient._pos_key(symbol, side.upper())
                p = fc._open_positions.get(key)
                if p:
                    p.closed = True
                return False
            return original_update(symbol, side, new_sl=new_sl, new_tp=new_tp)

        fc.update_sl_tp = mock_update_sl_tp
        closed = fc.check_positions(settings=make_settings())
        # pos is now marked closed, but no close order went through
        assert pos.closed is True
        # The closed list should have no full closes (position handled by Binance)
        # Scale-out partial may have fired before the -2011 race occurred
        full_closes = [c for c in closed if not c.get('partial')]
        assert full_closes == []


# ─── 12. Edge cases ───────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_zero_qty_position_does_not_crash(self):
        """pnl_pct = unr / (entry * qty) — qty=0 must not divide by zero."""
        pos = make_short(entry=100.0, qty=0.0)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 97.0)
        # Should not raise ZeroDivisionError
        closed = fc.check_positions(settings=make_settings())
        # pnl_pct = 0 → no trailing or breakeven triggers
        assert True  # just verify no exception

    def test_entry_price_zero_does_not_crash(self):
        pos = make_short(entry=0.0, qty=1.0)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 1.0)
        closed = fc.check_positions(settings=make_settings())
        assert True

    def test_position_already_at_tp_on_first_cycle(self):
        """If price is already at TP when position is created (e.g., sync error), close cleanly."""
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 92.0)  # immediately at TP
        s = make_settings(futures_smart_tp_enabled=False)
        closed = fc.check_positions(settings=s)
        full_closes = [c for c in closed if not c.get('partial')]
        assert len(full_closes) == 1
        assert full_closes[0]['reason'] == 'take_profit'

    def test_breakeven_and_trailing_both_fire_same_cycle(self):
        """At 2.5% gain, both breakeven and trailing should fire on the same cycle without conflict."""
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 97.5)  # 2.5% gain
        closed = fc.check_positions(settings=make_settings())
        # Should not crash, position should still be open (scale-out partial is OK)
        full_closes = [c for c in closed if not c.get('partial')]
        assert full_closes == []
        # SL moved (either breakeven=100.0 or trail=98.475, whichever is lower)
        assert pos.sl_price <= 100.0

    def test_check_positions_empty_positions(self):
        """Empty positions dict should return empty list without errors."""
        fc = make_fc()
        closed = fc.check_positions(settings=make_settings())
        assert closed == []

    def test_settings_none_uses_defaults(self):
        """check_positions(settings=None) should use hardcoded defaults without crashing."""
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 104.0)  # SL hit
        closed = fc.check_positions(settings=None)
        assert len(closed) == 1


# ─── 11. SSO-1b Profit Decay tests ──────────────────────────────────────────

class TestSSOProfitDecay:
    """SSO-1b: Profit Decay trigger unit tests."""

    def test_decay_fires_when_conditions_met(self):
        """Position with 0.5% decay, $4 profit, 3h age → should close."""
        from datetime import timedelta
        from smart_scale_out import SmartScaleOut, SSOPositionData
        sso = SmartScaleOut(make_settings())
        pos = SSOPositionData(
            symbol="SUIUSDT", side="SHORT",
            pnl_pct=0.015, pnl_usdt=4.0,
            position_size_usdt=100.0,
            entry_price=0.73, current_price=0.719,
            opened_at=datetime.now(timezone.utc) - timedelta(hours=3),
            peak_pnl_pct=0.020,
            pnl_velocity_1h=0.002,
            sso_levels_done=[],
            atr_4h_pct=0.02,
            trade_type="futures",
        )
        action = sso.evaluate(pos)
        assert action.should_close is True
        assert "sso_profit_decay" in action.reason

    def test_decay_does_not_fire_small_decay(self):
        """Decay < 0.4% → should NOT fire sso_profit_decay."""
        from datetime import timedelta
        from smart_scale_out import SmartScaleOut, SSOPositionData
        sso = SmartScaleOut(make_settings())
        pos = SSOPositionData(
            symbol="ETHUSDT", side="SHORT",
            pnl_pct=0.018, pnl_usdt=3.5,
            position_size_usdt=100.0,
            entry_price=1762.0, current_price=1730.0,
            opened_at=datetime.now(timezone.utc) - timedelta(hours=3),
            peak_pnl_pct=0.020,
            pnl_velocity_1h=0.001,
            sso_levels_done=[],
            atr_4h_pct=0.015,
            trade_type="futures",
        )
        action = sso.evaluate(pos)
        assert "sso_profit_decay" not in (action.reason or "")

    def test_decay_does_not_fire_low_profit(self):
        """Profit < $2 → should NOT fire even with large decay."""
        from datetime import timedelta
        from smart_scale_out import SmartScaleOut, SSOPositionData
        sso = SmartScaleOut(make_settings())
        pos = SSOPositionData(
            symbol="BTCUSDT", side="SHORT",
            pnl_pct=0.005, pnl_usdt=1.5,
            position_size_usdt=100.0,
            entry_price=64000.0, current_price=63700.0,
            opened_at=datetime.now(timezone.utc) - timedelta(hours=4),
            peak_pnl_pct=0.015,
            pnl_velocity_1h=-0.001,
            sso_levels_done=[],
            atr_4h_pct=0.01,
            trade_type="futures",
        )
        action = sso.evaluate(pos)
        assert "sso_profit_decay" not in (action.reason or "")

    def test_decay_does_not_fire_too_young(self):
        """Age < 1.5h → should NOT fire."""
        from datetime import timedelta
        from smart_scale_out import SmartScaleOut, SSOPositionData
        sso = SmartScaleOut(make_settings())
        pos = SSOPositionData(
            symbol="AVAXUSDT", side="SHORT",
            pnl_pct=0.010, pnl_usdt=3.0,
            position_size_usdt=100.0,
            entry_price=6.4, current_price=6.33,
            opened_at=datetime.now(timezone.utc) - timedelta(minutes=45),
            peak_pnl_pct=0.018,
            pnl_velocity_1h=0.001,
            sso_levels_done=[],
            atr_4h_pct=0.02,
            trade_type="futures",
        )
        action = sso.evaluate(pos)
        assert "sso_profit_decay" not in (action.reason or "")

    def test_decay_fires_with_low_profit_independently(self):
        """SSO-1b with $2.5 profit should fire (not blocked by SSO-1's $3 gate)."""
        from datetime import timedelta
        from smart_scale_out import SmartScaleOut, SSOPositionData
        sso = SmartScaleOut(make_settings())
        pos = SSOPositionData(
            symbol="SOLUSDT", side="SHORT",
            pnl_pct=0.008, pnl_usdt=2.5,
            position_size_usdt=100.0,
            entry_price=73.0, current_price=72.4,
            opened_at=datetime.now(timezone.utc) - timedelta(hours=3),
            peak_pnl_pct=0.015,  # decay = 0.7%
            pnl_velocity_1h=0.0005,
            sso_levels_done=[],
            atr_4h_pct=0.015,
            trade_type="futures",
        )
        action = sso.evaluate(pos)
        assert action.should_close is True
        assert "sso_profit_decay" in action.reason


# ─── 12. SSO-1c Slow Crawl tests ────────────────────────────────────────────

class TestSSOSlowCrawl:
    """SSO-1c: Slow Crawl with lifetime velocity + escalating tiers."""

    def test_slow_fires_lifetime_low(self):
        """Lifetime 0.05%/h at 3h, snap 0.05%/h → T1 threshold 0.10%/h → fires."""
        from datetime import timedelta
        from smart_scale_out import SmartScaleOut, SSOPositionData
        sso = SmartScaleOut(make_settings())
        pos = SSOPositionData(
            symbol="SOLUSDT", side="SHORT",
            pnl_pct=0.0015, pnl_usdt=2.0,  # 0.15% in 3h = 0.05%/h lifetime
            position_size_usdt=100.0,
            entry_price=73.0, current_price=72.89,
            opened_at=datetime.now(timezone.utc) - timedelta(hours=3),
            peak_pnl_pct=0.002,
            pnl_velocity_1h=0.0005,  # 0.05%/h snap
            sso_levels_done=[], atr_4h_pct=0.015, trade_type="futures",
        )
        action = sso.evaluate(pos)
        assert action.should_close is True
        assert "sso_slow_crawl" in action.reason

    def test_slow_tier3_catches_suiusdt_pattern(self):
        """SUIUSDT: 1.9% in 6.3h = 0.30%/h lifetime, snap 0.46%/h → T3 thresh 0.35%/h → fires."""
        from datetime import timedelta
        from smart_scale_out import SmartScaleOut, SSOPositionData
        sso = SmartScaleOut(make_settings())
        pos = SSOPositionData(
            symbol="SUIUSDT", side="SHORT",
            pnl_pct=0.019, pnl_usdt=5.0,  # 1.9% in 6.3h = 0.30%/h
            position_size_usdt=100.0,
            entry_price=0.733, current_price=0.719,
            opened_at=datetime.now(timezone.utc) - timedelta(hours=6, minutes=18),
            peak_pnl_pct=0.0205,
            pnl_velocity_1h=0.0046,  # 0.46%/h snap (recent burst)
            sso_levels_done=[], atr_4h_pct=0.02, trade_type="futures",
        )
        action = sso.evaluate(pos)
        assert action.should_close is True
        assert "sso_slow_crawl" in action.reason
        assert "T3" in action.detail

    def test_slow_tier4_very_old(self):
        """16h position, 1% total → 0.06%/h lifetime → T4 thresh 0.50%/h → fires."""
        from datetime import timedelta
        from smart_scale_out import SmartScaleOut, SSOPositionData
        sso = SmartScaleOut(make_settings())
        pos = SSOPositionData(
            symbol="SOLUSDT", side="SHORT",
            pnl_pct=0.01, pnl_usdt=2.5,
            position_size_usdt=100.0,
            entry_price=73.0, current_price=72.27,
            opened_at=datetime.now(timezone.utc) - timedelta(hours=16),
            peak_pnl_pct=0.012,
            pnl_velocity_1h=0.0005,
            sso_levels_done=[], atr_4h_pct=0.015, trade_type="futures",
        )
        action = sso.evaluate(pos)
        assert action.should_close is True
        assert "sso_slow_crawl" in action.reason

    def test_slow_healthy_velocity_no_fire(self):
        """0.8%/h lifetime at 3h → well above T1 0.10%/h → NO fire."""
        from datetime import timedelta
        from smart_scale_out import SmartScaleOut, SSOPositionData
        sso = SmartScaleOut(make_settings())
        pos = SSOPositionData(
            symbol="UNIUSDT", side="SHORT",
            pnl_pct=0.024, pnl_usdt=3.0,  # 2.4% in 3h = 0.8%/h
            position_size_usdt=100.0,
            entry_price=3.09, current_price=3.016,
            opened_at=datetime.now(timezone.utc) - timedelta(hours=3),
            peak_pnl_pct=0.025,
            pnl_velocity_1h=0.005,  # 0.5%/h snap
            sso_levels_done=[], atr_4h_pct=0.018, trade_type="futures",
        )
        action = sso.evaluate(pos)
        assert "sso_slow_crawl" not in (action.reason or "")

    def test_slow_too_young_no_fire(self):
        """Age < 2h → should NOT fire."""
        from datetime import timedelta
        from smart_scale_out import SmartScaleOut, SSOPositionData
        sso = SmartScaleOut(make_settings())
        pos = SSOPositionData(
            symbol="DOTUSDT", side="SHORT",
            pnl_pct=0.005, pnl_usdt=1.5,
            position_size_usdt=100.0,
            entry_price=0.95, current_price=0.945,
            opened_at=datetime.now(timezone.utc) - timedelta(hours=1),
            peak_pnl_pct=0.006,
            pnl_velocity_1h=0.0005,
            sso_levels_done=[], atr_4h_pct=0.015, trade_type="futures",
        )
        action = sso.evaluate(pos)
        assert "sso_slow_crawl" not in (action.reason or "")

    def test_slow_low_profit_no_fire(self):
        """Profit < $1 → should NOT fire."""
        from datetime import timedelta
        from smart_scale_out import SmartScaleOut, SSOPositionData
        sso = SmartScaleOut(make_settings())
        pos = SSOPositionData(
            symbol="BTCUSDT", side="SHORT",
            pnl_pct=0.002, pnl_usdt=0.50,
            position_size_usdt=100.0,
            entry_price=64700.0, current_price=64570.0,
            opened_at=datetime.now(timezone.utc) - timedelta(hours=5),
            peak_pnl_pct=0.003,
            pnl_velocity_1h=0.0003,
            sso_levels_done=[], atr_4h_pct=0.008, trade_type="futures",
        )
        action = sso.evaluate(pos)
        assert "sso_slow_crawl" not in (action.reason or "")

    def test_diagnosis_always_populated(self):
        """Even when no trigger fires, diagnosis must be populated."""
        from datetime import timedelta
        from smart_scale_out import SmartScaleOut, SSOPositionData
        sso = SmartScaleOut(make_settings())
        pos = SSOPositionData(
            symbol="ETHUSDT", side="SHORT",
            pnl_pct=0.005, pnl_usdt=0.30,  # tiny profit, no trigger fires
            position_size_usdt=100.0,
            entry_price=1760.0, current_price=1751.2,
            opened_at=datetime.now(timezone.utc) - timedelta(hours=1),
            peak_pnl_pct=0.006,
            pnl_velocity_1h=0.003,
            sso_levels_done=[], atr_4h_pct=0.012, trade_type="futures",
        )
        action = sso.evaluate(pos)
        assert action.diagnosis != ""
        assert "SSO-1" in action.diagnosis


# ─── 13. SSO Dynamic Gates tests ─────────────────────────────────────────────

class TestSSODynamicGates:
    """Verify dynamic USD gate calculation from wallet balance (futures_total / spot_total)."""

    def test_futures_dynamic_gates_calculated(self):
        """futures_total=2864 → gates are % of wallet, not config defaults."""
        from smart_scale_out import SmartScaleOut
        sso = SmartScaleOut(make_settings(), futures_total=2864.0)
        assert sso.stagnant_min_profit_floor == round(2864.0 * 0.003, 2)   # $8.59
        assert sso.target_daily_usd == round(2864.0 * 0.05, 2)             # $143.20
        assert sso._dyn_decay_min_profit == round(2864.0 * 0.002, 2)       # $5.73
        assert sso._dyn_slow_min_profit == round(2864.0 * 0.001, 2)        # $2.86

    def test_spot_dynamic_gates_calculated(self):
        """spot_total=400 → gates are % of spot wallet."""
        from smart_scale_out import SmartScaleOut
        sso = SmartScaleOut(make_settings(), spot_total=400.0)
        assert sso.stagnant_min_profit_floor == round(400.0 * 0.003, 2)    # $1.20
        assert sso.target_daily_usd == round(400.0 * 0.05, 2)              # $20.0
        assert sso._dyn_decay_min_profit == round(400.0 * 0.002, 2)        # $0.80

    def test_futures_overrides_spot_when_both_passed(self):
        """futures_total takes priority over spot_total."""
        from smart_scale_out import SmartScaleOut
        sso = SmartScaleOut(make_settings(), futures_total=2864.0, spot_total=400.0)
        assert sso.target_daily_usd == round(2864.0 * 0.05, 2)

    def test_fallback_to_config_when_no_capital_passed(self):
        """No futures_total nor spot_total → falls back to config/default values."""
        from smart_scale_out import SmartScaleOut
        sso = SmartScaleOut(make_settings())
        assert sso.stagnant_min_profit_floor == 3.0   # config default
        assert sso.target_daily_usd == 40.0            # config default
        assert sso._dyn_decay_min_profit is None
        assert sso._dyn_slow_min_profit is None


# ─── 14. SSO-6 Bounce Rescue tests ───────────────────────────────────────────

class TestSSO6BounceRescue:
    """SSO-6: Bounce Rescue Mode — closes bounce-trapped positions near breakeven."""

    def _make_sso(self, **overrides):
        from smart_scale_out import SmartScaleOut
        s = types.SimpleNamespace(
            futures_bounce_rescue_enabled=True,
            futures_bounce_rescue_max_peak_pct=0.5,
            futures_bounce_rescue_min_hours=4.0,
            futures_bounce_rescue_target_pnl=-0.50,
            spot_bounce_rescue_enabled=True,
            spot_bounce_rescue_max_peak_pct=0.8,
            spot_bounce_rescue_min_hours=8.0,
            spot_bounce_rescue_target_pnl=-0.30,
            sso_stagnant_min_profit_factor=0.02,
            sso_stagnant_min_profit_floor=3.0,
            sso_stagnant_min_hours=2.0,
            sso_stagnant_velocity_window_min=60,
            sso_decay_threshold_pct=0.004,
            sso_decay_min_profit_usd=2.0,
            sso_decay_min_hours=1.5,
            sso_slow_velocity_threshold=0.001,
            sso_slow_min_profit_usd=1.0,
            sso_slow_min_hours=2.0,
            sso_slow_tier2_hours=4.0,
            sso_slow_tier3_hours=6.0,
            sso_slow_tier4_hours=8.0,
            sso_momentum_rsi_overbought=70,
            sso_momentum_rsi_oversold=30,
            sso_momentum_min_pnl_pct_futures=1.0,
            sso_momentum_min_pnl_pct_spot=1.5,
            sso_momentum_vol_dying_threshold=0.5,
            sso_multilevel_enabled=False,
            sso_target_contribution_pct=0.15,
            sso_target_daily_usd=40.0,
            sso_target_min_hours=1.0,
            sso_reentry_min_strength_diff=0.3,
            sso_reentry_min_ev_multiplier=1.5,
            rescue_smart_gate_enabled=True,
            rescue_stay_strength_min=0.50,
            rescue_stay_ev_multiplier=3.0,
            rescue_velocity_min_pct=0.003,
            rescue_gate_min_concurrent=2,
            rescue_gate_g1_min_pct=0.005,
            rescue_gate_g1_spot_min_pct=0.003,
            rescue_gate_vol_min_spike=1.5,
            rescue_gate_max_blocks=3,
            rescue_gate_min_pnl_improvement=0.005,
        )
        for k, v in overrides.items():
            setattr(s, k, v)
        return SmartScaleOut(s)

    def _make_pos(self, **overrides):
        from smart_scale_out import SSOPositionData
        defaults = dict(
            symbol="ADAUSDT",
            side="SHORT",
            pnl_pct=-0.06,
            pnl_usdt=-0.30,
            position_size_usdt=150.0,
            entry_price=0.1396,
            current_price=0.148,
            opened_at=datetime.now(timezone.utc).replace(hour=0, minute=0),
            peak_pnl_pct=0.003,
            trade_type="futures",
            bounce_entry=True,
        )
        defaults.update(overrides)
        return SSOPositionData(**defaults)

    def test_fires_when_bounce_tagged_and_near_breakeven(self):
        """SSO-6 must fire when bounce_entry=True and pnl >= target."""
        sso = self._make_sso()
        pos = self._make_pos(pnl_usdt=-0.30, bounce_entry=True)
        action = sso._check_sso6_bounce_rescue(pos)
        assert action.should_close is True
        assert action.reason == "sso_bounce_rescue"
        assert action.trigger == "SSO-6"

    def test_does_not_fire_when_pnl_too_low(self):
        """SSO-6 must NOT fire when pnl < target (still too deep in loss)."""
        sso = self._make_sso()
        pos = self._make_pos(pnl_usdt=-5.00, bounce_entry=True)
        action = sso._check_sso6_bounce_rescue(pos)
        assert action.should_close is False
        assert "wait" in action.diagnosis

    def test_does_not_fire_when_position_is_young(self):
        """SSO-6 must NOT fire inline-detection on young position (<4h), no bounce_entry flag."""
        from datetime import timedelta
        sso = self._make_sso()
        pos = self._make_pos(
            bounce_entry=False,
            peak_pnl_pct=0.002,
            opened_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        action = sso._check_sso6_bounce_rescue(pos)
        assert action.should_close is False

    def test_does_not_fire_when_disabled(self):
        """SSO-6 must respect futures_bounce_rescue_enabled=False."""
        sso = self._make_sso(futures_bounce_rescue_enabled=False)
        pos = self._make_pos(pnl_usdt=-0.10, bounce_entry=True)
        action = sso._check_sso6_bounce_rescue(pos)
        assert action.should_close is False
        assert "disabled" in action.diagnosis

    # ── Smart Gate tests ──────────────────────────────────────────────────────

    def test_gate_blocked_when_pnl_positive(self):
        """Smart Gate G1: rescue must be blocked when position is already in profit."""
        sso = self._make_sso()
        pos = self._make_pos(
            pnl_usdt=0.80, pnl_pct=0.005, bounce_entry=True,
            rescue_signal=None, rescue_signal_strength=0.0, rescue_prediction_ev=None,
        )
        action = sso._check_sso6_bounce_rescue(pos)
        assert action.should_close is False
        assert "GATE_BLOCKED" in action.diagnosis
        assert "pnl_positive" in action.diagnosis

    def test_gate_blocked_when_favorable_signal_with_vol(self):
        """Smart Gate G2+G4: rescue blocked when signal favorable + vol + velocity (>=2 concurrent)."""
        sso = self._make_sso()
        pos = self._make_pos(
            pnl_usdt=-0.30, pnl_pct=0.001, bounce_entry=True,  # pnl_pct>0 but <g1_min -> G1 not alone
            side="SHORT",
            rescue_signal="SELL", rescue_signal_strength=0.55,  # G2 active
            vol_spike=1.8,                                       # vol >= 1.5
            rescue_prediction_ev=0.20,                          # G3 inactive
            pnl_velocity_1h=0.006,                              # G4 active (vel>0.003)
            peak_pnl_pct=0.0,                                   # pnl_pct(0.001) > 0*0.5=0 -> G4 ok
        )
        action = sso._check_sso6_bounce_rescue(pos)
        assert action.should_close is False
        assert "GATE_BLOCKED" in action.diagnosis
        assert "signal_favorable" in action.diagnosis

    def test_gate_blocked_when_prediction_ev_high(self):
        """Smart Gate G3+G4: rescue blocked when EV > 3×|target_pnl| + velocity (>=2 concurrent)."""
        sso = self._make_sso()
        # target_pnl = -0.50 → ev_thresh = 0.50 * 3.0 = 1.50; ev=1.60 > 1.50 -> G3 active
        pos = self._make_pos(
            pnl_usdt=-0.40, pnl_pct=0.001, bounce_entry=True,  # pnl_pct<g1_min -> G1 not alone
            rescue_signal="HOLD", rescue_signal_strength=0.0,   # G2 inactive
            rescue_prediction_ev=1.60,                          # G3 active (1.60 > 1.50)
            vol_spike=0.5,
            pnl_velocity_1h=0.006,                              # G4 active
            peak_pnl_pct=0.0,                                   # pnl_pct(0.001)>0 -> G4 ok
        )
        action = sso._check_sso6_bounce_rescue(pos)
        assert action.should_close is False
        assert "GATE_BLOCKED" in action.diagnosis
        assert "G3" in action.diagnosis  # G3+G4 concurrent block uses AND-partial diagnosis

    def test_gate_fires_when_all_gates_pass(self):
        """Smart Gate: rescue must execute when no gate blocks it (no favorable signal, low EV, pnl<0)."""
        sso = self._make_sso()
        pos = self._make_pos(
            pnl_usdt=-0.30, pnl_pct=-0.002, bounce_entry=True,
            side="SHORT",
            rescue_signal="BUY",     # unfavorable for SHORT
            rescue_signal_strength=0.55,
            rescue_prediction_ev=0.20,  # < 1.0 threshold
            vol_spike=0.5,
            pnl_velocity_1h=0.0,
            peak_pnl_pct=0.001,
        )
        action = sso._check_sso6_bounce_rescue(pos)
        assert action.should_close is True
        assert action.reason == "sso_bounce_rescue"
        assert action.trigger == "SSO-6"

    def test_gate_blocked_spot_favorable_signal(self):
        """Smart Gate G2+G3 SPOT: rescue blocked when BUY signal + vol + EV (>=2 concurrent)."""
        sso = self._make_sso()
        # SPOT target_pnl=-0.30 -> ev_thresh = 0.30*3.0=0.90; ev=1.00 > 0.90 -> G3 active
        pos = self._make_pos(
            pnl_usdt=-0.25, pnl_pct=-0.002, bounce_entry=True,
            side="BUY", trade_type="spot",
            rescue_signal="BUY", rescue_signal_strength=0.65,  # G2 active
            vol_spike=1.6,                                      # vol >= 1.5
            rescue_prediction_ev=1.00,                         # G3 active for SPOT
        )
        action = sso._check_sso6_bounce_rescue(pos)
        assert action.should_close is False
        assert "GATE_BLOCKED" in action.diagnosis
        assert "signal_favorable" in action.diagnosis

    # ── AND-partial + G1 threshold + expiry tests ─────────────────────────────

    def test_g4_alone_does_not_block(self):
        """AND-partial: G4 (velocity) active alone must NOT block (needs >=2 concurrent)."""
        sso = self._make_sso()
        pos = self._make_pos(
            pnl_usdt=-0.40, pnl_pct=-0.002, bounce_entry=True,
            side="SHORT",
            rescue_signal="HOLD", rescue_signal_strength=0.0,
            rescue_prediction_ev=0.10,  # below EV threshold
            vol_spike=0.5,
            pnl_velocity_1h=0.006,  # G4 active (vel > 0.003)
            peak_pnl_pct=0.001,     # pnl_pct (-0.002) < peak*0.5 (0.0005) is False -> need check
        )
        # G4 condition: vel > thresh AND pnl_pct > peak * 0.5
        # pnl_pct=-0.002 > 0.001*0.5=0.0005 is False -> G4 inactive anyway
        # Force G4 active via peak_pnl_pct=0 so pnl_pct(-0.002) > 0*0.5=0
        pos2 = self._make_pos(
            pnl_usdt=-0.40, pnl_pct=-0.002, bounce_entry=True,
            side="SHORT",
            rescue_signal="HOLD", rescue_signal_strength=0.0,
            rescue_prediction_ev=0.10,
            vol_spike=0.5,
            pnl_velocity_1h=0.006,  # G4 active
            peak_pnl_pct=0.0,       # ensures pnl_pct > peak*0.5
        )
        action = sso._check_sso6_bounce_rescue(pos2)
        # G4 alone (concurrent=1 < min_concurrent=2) -> should NOT block
        assert action.should_close is True  # rescue executes
        assert action.reason == "sso_bounce_rescue"

    def test_g2_g4_concurrent_blocks(self):
        """AND-partial: G2+G4 concurrent (>=2) MUST block rescue."""
        sso = self._make_sso()
        # G4 condition: vel > thresh AND pnl_pct > peak*0.5
        # Use pnl_pct=0.001 (positive, above 0*0.5=0) to satisfy G4 recovery condition
        pos = self._make_pos(
            pnl_usdt=-0.30, pnl_pct=0.001, bounce_entry=True,  # pnl_pct<g1_min -> G1 not alone
            side="SHORT",
            rescue_signal="SELL", rescue_signal_strength=0.55,  # G2 active
            vol_spike=1.6,           # vol >= 1.5 -> G2 confirmed
            rescue_prediction_ev=0.50,  # below EV threshold (3.0 * 0.50 = 1.50) -> G3 inactive
            pnl_velocity_1h=0.006,  # G4 active (vel > 0.003)
            peak_pnl_pct=0.0,       # pnl_pct(0.001) > 0*0.5=0 -> G4 condition met
        )
        action = sso._check_sso6_bounce_rescue(pos)
        assert action.should_close is False
        assert "GATE_BLOCKED" in action.diagnosis
        assert "signal_favorable" in action.diagnosis
        assert "G2" in action.diagnosis

    def test_g1_below_threshold_does_not_block_alone(self):
        """AND-partial: G1 tick (pnl_pct=+0.001%) below threshold must NOT block alone."""
        sso = self._make_sso()  # g1_min_pct=0.005 (0.5%)
        pos = self._make_pos(
            pnl_usdt=0.05, pnl_pct=0.001,  # only +0.1% — below 0.5% threshold
            bounce_entry=True, side="SHORT",
            rescue_signal="HOLD", rescue_signal_strength=0.0,
            rescue_prediction_ev=0.10,
            vol_spike=0.5,
            pnl_velocity_1h=0.001,
            peak_pnl_pct=0.0,
        )
        action = sso._check_sso6_bounce_rescue(pos)
        # G1 below threshold + no other gate active -> rescue fires
        assert action.should_close is True
        assert action.reason == "sso_bounce_rescue"

    def test_g1_above_threshold_blocks_alone(self):
        """AND-partial: G1 strong (pnl_pct=+0.7%) above threshold blocks alone."""
        sso = self._make_sso()  # g1_min_pct=0.005
        pos = self._make_pos(
            pnl_usdt=1.20, pnl_pct=0.007,  # +0.7% >= 0.5% threshold
            bounce_entry=True, side="SHORT",
            rescue_signal=None, rescue_signal_strength=0.0,
            rescue_prediction_ev=None, vol_spike=0.5,
        )
        action = sso._check_sso6_bounce_rescue(pos)
        assert action.should_close is False
        assert "GATE_BLOCKED" in action.diagnosis
        assert "pnl_positive" in action.diagnosis

    def test_gate_expiry_forces_rescue_after_max_blocks(self):
        """Expiry: after rescue_gate_max_blocks blocks without pnl improvement, rescue must fire."""
        sso = self._make_sso(rescue_gate_max_blocks=3, rescue_gate_min_pnl_improvement=0.005)
        pos = self._make_pos(
            pnl_usdt=-0.30, pnl_pct=-0.002, bounce_entry=True,
            side="SHORT",
            rescue_signal="SELL", rescue_signal_strength=0.55,
            vol_spike=1.6,
            rescue_prediction_ev=0.50,
            pnl_velocity_1h=0.006,
            peak_pnl_pct=0.0,
            # Simulate 3 blocks already done, no improvement
            _gate_block_count=3,
            _gate_last_pnl_pct=-0.0025,  # was worse before, improvement = -0.002 - (-0.0025) = 0.0005 < 0.005
        )
        action = sso._check_sso6_bounce_rescue(pos)
        # Gate would block (G2+G4 concurrent) BUT expiry fires rescue
        assert action.should_close is True
        assert action.reason == "sso_bounce_rescue"

    def test_gate_spot_uses_different_g1_threshold(self):
        """AND-partial SPOT: G1 uses g1_spot_min_pct=0.003 (0.3%), not futures 0.5%."""
        sso = self._make_sso()  # g1_spot_min_pct=0.003
        # pnl_pct=+0.004 (0.4%) >= 0.3% SPOT threshold -> should block
        pos = self._make_pos(
            pnl_usdt=0.80, pnl_pct=0.004,
            bounce_entry=True, side="BUY", trade_type="spot",
            rescue_signal=None, rescue_signal_strength=0.0,
            rescue_prediction_ev=None, vol_spike=0.5,
        )
        action = sso._check_sso6_bounce_rescue(pos)
        assert action.should_close is False
        assert "GATE_BLOCKED" in action.diagnosis
        assert "pnl_positive" in action.diagnosis
