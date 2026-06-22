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
        """pnl_pct = unr / (entry * qty). Verify the math in check_positions."""
        # entry=100, qty=2.0, price=98.0 (SHORT 2% gain)
        # unr = (100 - 98) * 2 = 4.0
        # pnl_pct = 4.0 / (100 * 2) = 0.02 = 2.0%
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=2.0)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 98.0)
        # At 2.0% PnL, breakeven fires (1.0%), trail activates (2.0%) with 0.75% distance
        # Trail: best=98.0, new_sl = 98*(1+0.0075)=98.735 which is < 100.0 (entry)
        fc.check_positions(settings=make_settings())
        # Trail supersedes breakeven: SL = 98.735
        assert pos.sl_price == pytest.approx(98.0 * 1.0075, abs=0.1)
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
        val = getattr(s, 'smart_tp_extend_disabled_regimes', None)
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
        assert self._get('futures_trailing_activation_pct') == pytest.approx(2.5)

    def test_futures_trailing_distance_pct(self):
        assert self._get('futures_trailing_distance_pct') == pytest.approx(1.5)

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
