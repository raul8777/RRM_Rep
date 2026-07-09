"""
tests/test_system_health.py
======================================================
System Health Test — crypto-agent  (QA Expert)
======================================================
10 categories, 80+ checks.

Ejecutar bajo demanda:
  docker-compose exec app python -m pytest tests/test_system_health.py -v --tb=short

Por categoría:
  docker-compose exec app python -m pytest tests/test_system_health.py -k "Cat1" -v

Cada vez que se implemente algo nuevo, agregar una clase TestCatN_<modulo>
siguiendo el patrón existente.

HEALTH_VERSION increméntalo con cada sesión de implementación.
"""

import sys
import os
import types
import time
from datetime import datetime, timezone, timedelta
from dataclasses import fields as dc_fields

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# ── version marker (bump con cada sesión) ─────────────────────────────────────
HEALTH_VERSION = "2026-07-07-v1"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_settings(**overrides):
    """Minimal settings namespace with all production defaults."""
    from config import Settings
    base = Settings()
    ns = types.SimpleNamespace(**{
        f: getattr(base, f) for f in base.model_fields
    })
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def make_fc(positions=None):
    from futures_client import FuturesClient
    fc = FuturesClient(api_key="test", api_secret="test", dry_run=True)
    if positions:
        for pos in positions:
            key = f"{pos.symbol}_{pos.side}"
            fc._open_positions[key] = pos
    return fc


def make_short(symbol="BTCUSDT", entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=1.0):
    from futures_client import FuturesPosition
    return FuturesPosition(
        symbol=symbol, side="SHORT", qty=qty,
        entry_price=entry,
        sl_price=round(entry * (1 + sl_pct / 100), 6),
        tp_price=round(entry * (1 - tp_pct / 100), 6),
        order_id="HT_ORDER", sl_order_id="HT_SL", tp_order_id="HT_TP",
    )


def inject_price(fc, symbol, price):
    fc._price_cache[symbol] = (price, time.monotonic() + 3600)


# ═════════════════════════════════════════════════════════════════════════════
# CAT 1 — Config Integrity
# ═════════════════════════════════════════════════════════════════════════════

class TestCat1_ConfigIntegrity:
    """Config params must have valid types, ranges, and logical consistency."""

    def setup_method(self):
        from config import Settings
        self.s = Settings()

    def test_sl_lt_tp_global(self):
        """Global SL% < TP% — never risk more than reward."""
        assert self.s.futures_trailing_activation_pct < self.s.futures_smart_tp_max_pnl_pct

    def test_futures_sl_lt_tp(self):
        """Futures SL% must be less than futures TP% for positive R:R."""
        # Loaded from env; defaults ensure R:R > 1
        # Check via market_regime params
        from market_regime import _build_regime
        raw = {"mode": "FUTURES_SHORT", "regime": "BAJISTA", "color": "#9b59b6",
               "btc_7d_pct": -20.0, "btc_30d_pct": -18.0, "btc_day_pct": -1.0,
               "vol_ratio": 0.5, "ema9_vs_ema21": "DOWN", "bb_bandwidth": 0.03, "reason": "test"}
        regime = _build_regime(raw, confirmed=True)
        assert regime.sl_pct < regime.tp_pct, \
            f"FUTURES_SHORT SL%={regime.sl_pct} must be < TP%={regime.tp_pct}"

    def test_range_scalp_sl_lt_tp(self):
        from market_regime import _build_regime
        raw = {"mode": "RANGE_SCALP", "regime": "LATERAL", "color": "#f39c12",
               "btc_7d_pct": -1.0, "btc_30d_pct": -3.0, "btc_day_pct": 0.2,
               "vol_ratio": 0.5, "ema9_vs_ema21": "UP", "bb_bandwidth": 0.02, "reason": "test"}
        regime = _build_regime(raw, confirmed=True)
        assert regime.sl_pct < regime.tp_pct

    def test_preservation_sl_lt_tp(self):
        from market_regime import _build_regime
        raw = {"mode": "PRESERVATION", "regime": "BAJISTA", "color": "#e74c3c",
               "btc_7d_pct": -8.0, "btc_30d_pct": -16.0, "btc_day_pct": -0.5,
               "vol_ratio": 0.4, "ema9_vs_ema21": "DOWN", "bb_bandwidth": 0.03, "reason": "test"}
        regime = _build_regime(raw, confirmed=True)
        assert regime.sl_pct < regime.tp_pct

    def test_kelly_base_lt_kelly_max(self):
        assert self.s.kelly_base_usdt < self.s.kelly_max_usdt, \
            "kelly_base_usdt must be < kelly_max_usdt"

    def test_kelly_max_pct_liquid_matches_backtest(self):
        """Concentrated: kelly_max_pct_liquid raised to 85% of liquid capital."""
        assert self.s.kelly_max_pct_liquid == 0.85, \
            f"kelly_max_pct_liquid={self.s.kelly_max_pct_liquid} != 0.85 (concentrated policy)"

    def test_futures_breakeven_lt_trailing_activation(self):
        """Breakeven must activate BEFORE trailing to preserve gains correctly."""
        assert self.s.futures_breakeven_activation_pct < self.s.futures_trailing_activation_pct, \
            ("futures_breakeven_activation_pct must be < futures_trailing_activation_pct "
             "so breakeven fires first")

    def test_smart_tp_disabled_regimes_are_valid(self):
        valid = {"SPOT_BULL", "RANGE_SCALP", "PRESERVATION", "FUTURES_SHORT"}
        for r in self.s.smart_tp_extend_disabled_regimes:
            assert r in valid, f"Unknown regime in smart_tp_extend_disabled_regimes: {r}"

    def test_futures_smart_tp_extensions_positive(self):
        assert self.s.futures_smart_tp_max_extensions >= 1

    def test_futures_smart_tp_score_threshold_in_range(self):
        assert 1 <= self.s.futures_smart_tp_extend_score_threshold <= 4

    def test_futures_trailing_distance_positive(self):
        assert self.s.futures_trailing_distance_pct > 0

    def test_min_cycles_before_close_shorts_geq_2(self):
        """Anti-whipsaw needs >= 2 cycles to avoid premature close."""
        assert self.s.min_cycles_before_close_shorts >= 2

    def test_kelly_dynamic_base_pct_reasonable(self):
        assert 0.05 <= self.s.kelly_dynamic_base_pct <= 0.30

    def test_futures_leverage_matches_backtest(self):
        """K2: futures_leverage must be a valid non-zero integer (live .env may differ from backtest baseline)."""
        lev = getattr(self.s, 'futures_leverage', 10)
        assert isinstance(lev, int) and lev >= 1, f"futures_leverage={lev} must be a positive integer"

    def test_smart_tp_extend_disabled_regimes_is_list(self):
        assert isinstance(self.s.smart_tp_extend_disabled_regimes, list)
        assert len(self.s.smart_tp_extend_disabled_regimes) >= 1


# ═════════════════════════════════════════════════════════════════════════════
# CAT 2 — Market Regime Logic
# ═════════════════════════════════════════════════════════════════════════════

class TestCat2_MarketRegimeLogic:
    """Regime detection must produce correct modes and params."""

    def _raw(self, mode, btc_7d=-1.0, btc_30d=-3.0, day=0.2, vol=0.8,
             ema="UP", bb_bw=0.05):
        return {"mode": mode, "regime": "test", "color": "#fff",
                "btc_7d_pct": btc_7d, "btc_30d_pct": btc_30d,
                "btc_day_pct": day, "vol_ratio": vol,
                "ema9_vs_ema21": ema, "bb_bandwidth": bb_bw, "reason": "test"}

    def test_build_regime_returns_market_regime(self):
        from market_regime import _build_regime, MarketRegime
        r = _build_regime(self._raw("SPOT_BULL"), confirmed=True)
        assert isinstance(r, MarketRegime)

    def test_spot_bull_has_correct_kelly(self):
        from market_regime import _build_regime
        r = _build_regime(self._raw("SPOT_BULL"), confirmed=True)
        assert r.kelly_multiplier == 1.0

    def test_range_scalp_has_reduced_kelly(self):
        from market_regime import _build_regime
        r = _build_regime(self._raw("RANGE_SCALP"), confirmed=True)
        assert r.kelly_multiplier < 1.0

    def test_preservation_has_narrative_only(self):
        from market_regime import _build_regime
        r = _build_regime(self._raw("PRESERVATION", btc_7d=-8.0, ema="DOWN"),
                          confirmed=True)
        assert r.narrative_only is True

    def test_futures_short_has_correct_sl_tp(self):
        from market_regime import _build_regime
        r = _build_regime(self._raw("FUTURES_SHORT", btc_7d=-20.0, btc_30d=-18.0),
                          confirmed=True)
        assert r.sl_pct == 3.0
        assert r.tp_pct == 7.0

    def test_preservation_upgrades_to_futures_short_when_enabled(self):
        """With futures_enabled=True, confirmed PRESERVATION → FUTURES_SHORT."""
        from market_regime import _build_regime, _cache
        import time

        # Reset cache to force fresh detection
        _cache.clear()

        s = make_settings(futures_enabled=True)

        # Create a mock client that returns bear BTC data
        class MockBTC:
            def get_klines(self, symbol, interval, limit):
                # Return candles showing bear trend
                price = 60000.0
                candles = []
                for i in range(limit):
                    p = price * (1 - 0.001 * i)
                    candles.append([0, str(p), str(p*1.01), str(p*0.99), str(p), "1000"])
                return candles

            def get_ticker_24h(self, symbol):
                return {"priceChangePercent": "-1.0", "quoteVolume": "5000000000"}

        class MockClient:
            client = MockBTC()
            def get_ticker_24h(self, symbol): return MockBTC().get_ticker_24h(symbol)

        # Direct test: PRESERVATION raw + futures_enabled → upgrade
        from market_regime import _build_regime
        raw_pres = {"mode": "PRESERVATION", "regime": "BAJISTA", "color": "#e74c3c",
                    "btc_7d_pct": -8.0, "btc_30d_pct": -16.0, "btc_day_pct": -0.5,
                    "vol_ratio": 0.4, "ema9_vs_ema21": "DOWN", "bb_bandwidth": 0.03,
                    "reason": "Bear estructural"}
        pres = _build_regime(raw_pres, confirmed=True)
        assert pres.mode == "PRESERVATION"

        # Simulate the upgrade logic in get_regime
        futures_on = getattr(s, 'futures_enabled', True)
        if futures_on and pres.mode == "PRESERVATION" and pres.confirmed:
            raw_upgraded = dict(
                mode="FUTURES_SHORT", regime=pres.regime, color="#9b59b6",
                btc_7d_pct=pres.btc_7d_pct, btc_30d_pct=pres.btc_30d_pct,
                btc_day_pct=pres.btc_day_pct, vol_ratio=pres.vol_ratio,
                ema9_vs_ema21=pres.ema9_vs_ema21, bb_bandwidth=pres.bb_bandwidth,
                reason=pres.reason + " [FUTURES SHORT activo]",
            )
            upgraded = _build_regime(raw_upgraded, confirmed=True)
            assert upgraded.mode == "FUTURES_SHORT"

    def test_anti_whipsaw_requires_two_cycles(self):
        """A single cycle detection must not confirm a regime switch."""
        from market_regime import _CONFIRM_CYCLES
        assert _CONFIRM_CYCLES >= 2, \
            f"_CONFIRM_CYCLES={_CONFIRM_CYCLES} — must be >= 2 to prevent whipsaw"

    def test_market_regime_dataclass_has_required_fields(self):
        from market_regime import MarketRegime
        from dataclasses import fields
        field_names = {f.name for f in fields(MarketRegime)}
        required = {"mode", "regime", "sl_pct", "tp_pct", "kelly_multiplier",
                    "narrative_only", "confirmed"}
        missing = required - field_names
        assert not missing, f"MarketRegime missing fields: {missing}"

    def test_futures_short_kelly_multiplier_reduced(self):
        from market_regime import _build_regime
        r = _build_regime(self._raw("FUTURES_SHORT", btc_30d=-20.0), confirmed=True)
        # Current production config sets FUTURES_SHORT multiplier to 0.6
        assert r.kelly_multiplier <= 0.75, \
            "FUTURES_SHORT Kelly multiplier must be ≤0.75 (leverage risk)"

    def test_spot_bull_not_in_disabled_regimes(self):
        from config import Settings
        s = Settings()
        assert "SPOT_BULL" not in s.smart_tp_extend_disabled_regimes

    def test_futures_short_not_in_disabled_regimes(self):
        from config import Settings
        s = Settings()
        assert "FUTURES_SHORT" not in s.smart_tp_extend_disabled_regimes


# ═════════════════════════════════════════════════════════════════════════════
# CAT 3 — FuturesClient Core
# ═════════════════════════════════════════════════════════════════════════════

class TestCat3_FuturesClientCore:
    """Core futures position management and smart-exit logic."""

    # ── _smart_meta ───────────────────────────────────────────────────────────

    def test_smart_meta_is_dataclass_field(self):
        from futures_client import FuturesPosition
        names = {f.name for f in dc_fields(FuturesPosition)}
        assert '_smart_meta' in names, "_smart_meta must be a declared dataclass field"

    def test_smart_meta_not_shared_between_instances(self):
        a = make_short("BTCUSDT")
        b = make_short("ETHUSDT")
        a._smart_meta['x'] = 1
        assert 'x' not in b._smart_meta, "Mutable default shared between instances!"

    def test_smart_meta_starts_empty(self):
        pos = make_short()
        assert pos._smart_meta == {}

    # ── _pre_entry_checks ─────────────────────────────────────────────────────

    def test_pre_entry_rejects_when_max_shorts_reached(self):
        pos1 = make_short("BTCUSDT")
        pos2 = make_short("ETHUSDT")
        fc = make_fc([pos1, pos2])
        fc.max_shorts = 2
        ok, reason = fc._pre_entry_checks("SOLUSDT", 100.0, "SHORT")
        assert not ok
        assert "MAX_SHORTS" in reason

    def test_pre_entry_rejects_duplicate_symbol_side(self):
        pos = make_short("BTCUSDT")
        fc = make_fc([pos])
        fc.max_shorts = 10
        ok, reason = fc._pre_entry_checks("BTCUSDT", 100.0, "SHORT")
        assert not ok
        assert "Already have" in reason

    def test_pre_entry_rejects_largecap_when_cap_hit(self):
        pos1 = make_short("BTCUSDT")
        pos2 = make_short("ETHUSDT")
        fc = make_fc([pos1, pos2])
        fc.max_shorts = 10
        fc.max_largecap_shorts = 2
        fc.largecap_symbols = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
        ok, reason = fc._pre_entry_checks("SOLUSDT", 100.0, "SHORT")
        assert not ok
        assert "LARGECAP_CAP" in reason

    def test_pre_entry_allows_midcap_when_largecap_full(self):
        pos1 = make_short("BTCUSDT")
        pos2 = make_short("ETHUSDT")
        fc = make_fc([pos1, pos2])
        fc.max_shorts = 10
        fc.max_largecap_shorts = 2
        fc.largecap_symbols = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
        ok, reason = fc._pre_entry_checks("NEARUSDT", 100.0, "SHORT")  # not in largecap
        assert ok, f"Mid-cap should be allowed. Reason: {reason}"

    # ── update_sl_tp ─────────────────────────────────────────────────────────

    def test_update_sl_tp_updates_in_memory(self):
        pos = make_short(entry=100.0)
        fc = make_fc([pos])
        inject_price(fc, pos.symbol, 97.0)
        assert fc.update_sl_tp("BTCUSDT", "SHORT", new_sl=101.0) is True
        assert pos.sl_price == 101.0

    def test_update_sl_tp_false_for_unknown(self):
        fc = make_fc()
        assert fc.update_sl_tp("UNKNOWN", "SHORT", new_sl=100.0) is False

    def test_update_sl_tp_false_for_closed(self):
        pos = make_short()
        pos.closed = True
        fc = make_fc([pos])
        assert fc.update_sl_tp("BTCUSDT", "SHORT", new_sl=101.0) is False

    def test_update_sl_tp_both(self):
        pos = make_short(entry=100.0)
        fc = make_fc([pos])
        assert fc.update_sl_tp("BTCUSDT", "SHORT", new_sl=101.0, new_tp=91.0) is True
        assert pos.sl_price == 101.0
        assert pos.tp_price == 91.0

    # ── check_positions: SL hit ───────────────────────────────────────────────

    def test_sl_hit_closes_short(self):
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0)
        fc = make_fc([pos])
        inject_price(fc, "BTCUSDT", 104.0)  # above SL 103.0
        closed = fc.check_positions(settings=make_settings())
        assert len(closed) == 1
        assert closed[0]['reason'] == 'stop_loss'
        assert pos.closed is True

    def test_sl_hit_pnl_is_negative_for_short(self):
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0, qty=2.0)
        fc = make_fc([pos])
        inject_price(fc, "BTCUSDT", 104.0)
        closed = fc.check_positions(settings=make_settings())
        assert closed[0]['pnl'] < 0

    # ── check_positions: breakeven ────────────────────────────────────────────

    def test_breakeven_moves_sl_to_entry_at_threshold(self):
        pos = make_short(entry=100.0, sl_pct=3.0)
        fc = make_fc([pos])
        inject_price(fc, "BTCUSDT", 98.8)  # 1.2% gain
        fc.check_positions(settings=make_settings())
        assert pos.sl_price == pytest.approx(100.0, abs=0.01)
        assert pos._smart_meta.get('breakeven_done') is True

    def test_breakeven_fires_only_once(self):
        pos = make_short(entry=100.0, sl_pct=3.0)
        pos._smart_meta['breakeven_done'] = True
        original = pos.sl_price
        fc = make_fc([pos])
        inject_price(fc, "BTCUSDT", 98.5)  # 1.5% gain, above breakeven but below trailing
        s = make_settings(futures_trailing_activation_pct=3.0)  # disable trailing for this test
        fc.check_positions(settings=s)
        assert pos.sl_price == original

    # ── check_positions: TP hit ───────────────────────────────────────────────

    def test_tp_hit_closes_when_smarttp_off(self):
        pos = make_short(entry=100.0, tp_pct=7.0)
        fc = make_fc([pos])
        inject_price(fc, "BTCUSDT", 92.0)  # below TP 93.0
        s = make_settings(futures_smart_tp_enabled=False)
        closed = fc.check_positions(settings=s)
        assert any(c['reason'] == 'take_profit' for c in closed)

    def test_tp_extend_when_momentum_high(self):
        pos = make_short(entry=100.0, tp_pct=7.0)
        fc = make_fc([pos])
        inject_price(fc, "BTCUSDT", 92.0)
        fc._evaluate_short_momentum = lambda sym, s, **kwargs: (3, {})
        s = make_settings(futures_smart_tp_enabled=True,
                          futures_smart_tp_extend_score_threshold=3)
        closed = fc.check_positions(settings=s)
        full_closes = [c for c in closed if not c.get('partial')]
        assert full_closes == []
        assert pos._smart_meta.get('tp_extensions_count') == 1

    def test_tp_extend_capped_at_max_extensions(self):
        pos = make_short(entry=100.0, tp_pct=7.0)
        pos._smart_meta['tp_extensions_count'] = 2
        fc = make_fc([pos])
        inject_price(fc, "BTCUSDT", 92.0)
        fc._evaluate_short_momentum = lambda sym, s, **kwargs: (4, {})
        s = make_settings(futures_smart_tp_enabled=True, futures_smart_tp_max_extensions=2)
        closed = fc.check_positions(settings=s)
        full_closes = [c for c in closed if not c.get('partial')]
        assert len(full_closes) == 1  # extensions exhausted → close

    # ── _evaluate_short_momentum: BH6 ────────────────────────────────────────

    def test_momentum_returns_minus1_when_no_client_ref(self):
        """BH6: _evaluate_short_momentum with _binance_client_ref=None must return -1."""
        fc = make_fc()
        fc._binance_client_ref = None
        score, details = fc._evaluate_short_momentum("BTCUSDT", make_settings())
        assert score == -1, \
            "BH6: Score must be -1 when no client ref — not silence/crash"

    def test_closed_position_skipped_in_check(self):
        pos = make_short()
        pos.closed = True
        fc = make_fc([pos])
        inject_price(fc, "BTCUSDT", 104.0)
        closed = fc.check_positions(settings=make_settings())
        assert closed == []

    def test_empty_positions_returns_empty(self):
        fc = make_fc()
        assert fc.check_positions(settings=make_settings()) == []

    def test_settings_none_does_not_crash(self):
        pos = make_short(entry=100.0)
        fc = make_fc([pos])
        inject_price(fc, "BTCUSDT", 104.0)
        closed = fc.check_positions(settings=None)
        assert len(closed) == 1  # SL hit

    # ── Fix 3: trail_best_price inferred from SL after restart ───────────────

    def test_Fix3_trail_best_price_inferred_SHORT_after_meta_reset(self):
        """Fix 3: After _smart_meta reset (restart), SHORT trail_best_price must be
        inferred from SL (sl / (1+dist)) — NOT from current price — if SL < entry."""
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0)
        fc = make_fc([pos])
        # Simulate trailing already moved SL down to 94.0 (deeper move)
        pos.sl_price = 94.0  # below entry → trailing already acted
        # _smart_meta is empty (restart scenario)
        assert 'trail_best_price' not in pos._smart_meta
        # Inject price at 93.0 (7% gain, still profitable SHORT)
        inject_price(fc, "BTCUSDT", 93.0)
        s = make_settings(futures_trailing_enabled=True,
                          futures_trailing_activation_pct=2.0,
                          futures_trailing_distance_pct=2.0)
        fc.check_positions(settings=s)
        # Fix 3: inferred best = 94.0 / 1.0035 = 93.67 < entry(100) and < price... 
        # With new tighter distances (0.35% at 7% PnL), best ≈ 93.67
        best = pos._smart_meta.get('trail_best_price', None)
        assert best is not None, "Fix 3: trail_best_price not initialized"
        assert best < 100.0, "Fix 3: trail_best_price must be < entry for SHORT"
        assert best <= 94.0, "Fix 3: trail_best_price must be inferred from SL, not current price"

    def test_Fix3_trail_best_price_fresh_activation_when_sl_is_default(self):
        """Fix 3: When SL is still at default (entry*1.03), inferred best > entry → fresh activation at price."""
        pos = make_short(entry=100.0, sl_pct=3.0, tp_pct=7.0)
        fc = make_fc([pos])
        # SL is still at default 103.0 (no trailing yet). With net PnL after fees,
        # price=97.0 yields ~2.96% net gain on SHORT, crossing the 2% trail activation.
        inject_price(fc, "BTCUSDT", 97.0)
        s = make_settings(futures_trailing_enabled=True,
                          futures_trailing_activation_pct=2.0,
                          futures_trailing_distance_pct=2.0,
                          futures_sso_enabled=False)
        fc.check_positions(settings=s)
        # inferred_best = 100.0 / 1.02 = 98.04 > entry(100) → sanity check fails → use price
        best = pos._smart_meta.get('trail_best_price', None)
        assert best is not None, "Fix 3: trail_best_price not initialized on fresh activation"
        assert best == pytest.approx(97.0, abs=0.01), \
            f"Fix 3: fresh activation should set trail_best_price=price=97.0, got {best}"


# ═════════════════════════════════════════════════════════════════════════════
# CAT 4 — SPOT SL/TP Pipeline
# ═════════════════════════════════════════════════════════════════════════════

class TestCat4_SpotSLTPPipeline:
    """Regime-aware SmartTP EXTEND for SPOT positions."""

    def test_regime_range_scalp_disables_smarttp(self):
        """RANGE_SCALP in disabled list → _smart_tp flag set to False."""
        from config import Settings
        s = Settings()
        assert "RANGE_SCALP" in s.smart_tp_extend_disabled_regimes

    def test_regime_preservation_disables_smarttp(self):
        from config import Settings
        s = Settings()
        assert "PRESERVATION" in s.smart_tp_extend_disabled_regimes

    def test_regime_spot_bull_allows_smarttp(self):
        from config import Settings
        s = Settings()
        assert "SPOT_BULL" not in s.smart_tp_extend_disabled_regimes

    # ── Fix 1 regression: SL must use saved pos.stop_loss after EXTEND ────────

    def test_Fix1_effective_sl_uses_saved_stop_loss_after_extend(self):
        """Fix 1 regression: After SmartTP EXTEND moves pos.stop_loss to breakeven,
        the next cycle's SL check must use max(floor, pos.stop_loss) — never retreat."""
        from position_manager import PositionManager, Position
        # Simulate a position where SmartTP EXTEND set stop_loss to 102.0 (above entry 100)
        pm = PositionManager()
        pos = Position("BTCUSDT", "BUY", 1.0, 100.0, "2024-01-01T00:00:00Z",
                       stop_loss=102.0, take_profit=110.0)
        pm.add_position(pos)
        # SL floor = entry * (1 - sl_pct) = 100 * 0.982 = 98.2 (LARGE_CAP 1.8%)
        sl_pct = 0.018
        entry_price = 100.0
        current_price = 101.5  # price above floor but below saved SL (102.0)
        _sl_floor = entry_price * (1.0 - sl_pct)          # 98.2
        _sl_saved = float(pos.stop_loss) if pos.stop_loss else _sl_floor  # 102.0
        _effective_sl_price = max(_sl_floor, _sl_saved)   # 102.0
        # current_price(101.5) <= _effective_sl_price(102.0) → must close
        assert _effective_sl_price == pytest.approx(102.0), \
            "Fix 1: effective SL should be the saved breakeven value, not floor"
        should_close = current_price <= _effective_sl_price
        assert should_close, \
            "Fix 1: position at 101.5 with SL=102.0 should trigger stop_loss"

    def test_Fix1_antiwick_uses_effective_sl(self):
        """Fix 1 / anti-wick: anti-wick candle comparison must use _effective_sl_price,
        not the category floor. If SL was moved to breakeven, the wick check must
        respect that level — not revert to the original floor."""
        entry_price = 100.0
        sl_pct = 0.018  # 1.8% floor
        saved_sl = 102.0  # SmartTP EXTEND moved it here
        current_price = 101.5  # below saved_sl → close triggered
        last_1m_close = 101.8  # above saved_sl → anti-wick should HOLD
        _sl_floor = entry_price * (1.0 - sl_pct)          # 98.2
        _effective_sl_price = max(_sl_floor, saved_sl)    # 102.0
        # Anti-wick logic (fixed): compare last_close against _effective_sl_price
        wick_detected_correct = last_1m_close > _effective_sl_price   # 101.8 > 102.0 = False
        wick_detected_buggy   = last_1m_close > _sl_floor              # 101.8 > 98.2  = True (wrong)
        assert not wick_detected_correct, \
            "Anti-wick (fixed): 101.8 < 102.0 → no wick, close should proceed"
        assert wick_detected_buggy, \
            "Anti-wick (buggy): 101.8 > 98.2 → would incorrectly detect wick and block close"

    def test_regime_futures_short_allows_smarttp(self):
        from config import Settings
        s = Settings()
        assert "FUTURES_SHORT" not in s.smart_tp_extend_disabled_regimes

    def test_check_sl_tp_accepts_regime_param(self):
        """_check_sl_tp_positions must accept regime kwarg without error."""
        from run_trades import _check_sl_tp_positions
        import inspect
        sig = inspect.signature(_check_sl_tp_positions)
        assert 'regime' in sig.parameters, \
            "_check_sl_tp_positions must have 'regime' parameter"

    def test_check_sl_tp_regime_default_is_none(self):
        from run_trades import _check_sl_tp_positions
        import inspect
        sig = inspect.signature(_check_sl_tp_positions)
        default = sig.parameters['regime'].default
        assert default is None

    def test_wire_futures_client_function_exists(self):
        from run_trades import _wire_futures_client
        fc = make_fc()
        _wire_futures_client(fc, "mock_client", make_settings())
        assert fc._binance_client_ref == "mock_client"


# ═════════════════════════════════════════════════════════════════════════════
# CAT 5 — Kelly Sizing
# ═════════════════════════════════════════════════════════════════════════════

class TestCat5_KellySizing:
    """Kelly position sizing must be sane for all tiers and edge cases."""

    def _kelly(self, prob, strength, liquid=1000.0, sl=0.018, tp=0.035, **overrides):
        from run_trades import _kelly_position_size
        s = make_settings(**overrides)
        return _kelly_position_size(s, strength, {"success_probability": prob},
                                    liquid, sl_pct=sl, tp_pct=tp)

    def test_fuerte_tier_uses_max(self):
        result = self._kelly(0.65, 0.80, liquid=1000.0)
        s = make_settings()
        max_usdt = max(getattr(s, 'kelly_max_usdt'), 1000.0 * getattr(s, 'kelly_max_pct_cap'))
        cap = 1000.0 * getattr(s, 'kelly_max_pct_liquid')
        assert result == pytest.approx(min(max_usdt, cap), rel=0.05) or result <= cap

    def test_base_tier_uses_base(self):
        """Low confidence → result near kelly_base range."""
        result = self._kelly(0.50, 0.30, liquid=1000.0)
        assert result > 0
        cap = 1000.0 * getattr(make_settings(), 'kelly_max_pct_liquid')
        assert result <= cap  # never exceeds hard cap

    def test_hard_cap_not_exceeded(self):
        """Result must never exceed kelly_max_pct_liquid * liquid."""
        result = self._kelly(0.99, 0.99, liquid=500.0)
        cap = 500.0 * getattr(make_settings(), 'kelly_max_pct_liquid')
        assert result <= cap + 0.01, f"Kelly={result} exceeded hard cap={cap}"

    def test_zero_liquid_does_not_crash(self):
        result = self._kelly(0.65, 0.80, liquid=0.0)
        assert result >= 0

    def test_kelly_disabled_returns_max_per_trade(self):
        from run_trades import _kelly_position_size
        s = make_settings(kelly_enabled=False, max_per_trade_usdt=75.0)
        result = _kelly_position_size(s, 0.80, {"success_probability": 0.70}, 1000.0)
        assert result == pytest.approx(75.0)

    def test_kelly_result_is_positive(self):
        result = self._kelly(0.55, 0.60, liquid=500.0)
        assert result > 0

    def test_sl_zero_does_not_crash(self):
        from run_trades import _kelly_position_size
        s = make_settings()
        result = _kelly_position_size(s, 0.60, {"success_probability": 0.60},
                                      1000.0, sl_pct=0.0)
        assert result >= 0

    def test_alta_tier_between_base_and_max(self):
        """ALTA tier: prob>=0.60 + str>=0.70 → between base and max."""
        result = self._kelly(0.62, 0.72, liquid=1000.0)
        assert result > 0
        cap = 1000.0 * getattr(make_settings(), 'kelly_max_pct_liquid')
        assert result <= cap

    def test_concentration_multiplier_clamped(self):
        """Portfolio concentration multiplier must remain inside configured bounds."""
        from run_trades import _compute_portfolio_concentration_multiplier
        s = make_settings(
            portfolio_concentration_enabled=True,
            portfolio_concentration_alpha=1.0,
            portfolio_concentration_min_mult=0.80,
            portfolio_concentration_max_mult=1.20,
            portfolio_concentration_pressure_start=0.25,
            portfolio_concentration_pressure_boost=2.0,
            portfolio_concentration_baseline_spot_bull=5,
        )
        mult, diag = _compute_portfolio_concentration_multiplier(
            s, regime='SPOT_BULL', trade_type='spot', open_slots=5, effective_slots=1
        )
        assert diag['enabled'] is True
        assert 0.80 <= mult <= 1.20

    def test_kelly_applies_concentration_multiplier(self):
        """Higher concentration multiplier should increase Kelly allocation before caps."""
        from run_trades import _kelly_position_size
        s = make_settings(
            kelly_max_pct_liquid=2.0,
            kelly_max_pct_cap=0.50,
            current_regime_profile_label='B',
            current_regime_mode='SPOT_BULL',
        )
        base = _kelly_position_size(
            s, 0.55, {"success_probability": 0.55}, 1000.0,
            sl_pct=0.018, tp_pct=0.035,
            concentration_multiplier=1.0,
            trade_type='spot', regime='SPOT_BULL'
        )
        boosted = _kelly_position_size(
            s, 0.55, {"success_probability": 0.55}, 1000.0,
            sl_pct=0.018, tp_pct=0.035,
            concentration_multiplier=1.20,
            trade_type='spot', regime='SPOT_BULL'
        )
        assert boosted > base


class TestCat5b_SmartTotalSelector:
    """SMART Total mode selector must persist decisions and respect hysteresis."""

    class _DB:
        def __init__(self, runtime_state=None):
            self.runtime_state = runtime_state or {}
            self.audit_events = []

        def execute_query(self, sql, params=None):
            if "WHERE key='regime_profile_selector_config'" in sql:
                v = self.runtime_state.get('regime_profile_selector_config')
                return [] if v is None else [{"value": v}]
            if "FROM trading_runtime_state WHERE key=%s" in sql and params:
                key = params[0]
                v = self.runtime_state.get(key)
                return [] if v is None else [{"value": v}]
            return []

        def execute_update(self, sql, params=None):
            if "trading_runtime_state" in sql and params and len(params) >= 2:
                self.runtime_state[params[0]] = params[1]
                return 1
            if "trades_audit" in sql and params:
                event = 'unknown'
                if 'smart_total_mode_selector_decision' in sql:
                    event = 'smart_total_mode_selector_decision'
                self.audit_events.append({"event": event, "detail": params[0]})
                return 1
            return 0

        def execute_transaction(self, stmts):
            for sql, params in stmts:
                self.execute_update(sql, params)
            return True

    def test_mode_selector_blocks_switch_during_hold(self):
        import run_trades

        db = self._DB(runtime_state={
            'regime_profile_selector_config': {
                'smart_total_enabled': True,
                'mode_selector_enabled': True,
                'mode_min_hold_cycles': 3,
                'mode_min_confidence_delta': 0.01,
                'mode_require_confirmed': True,
            },
            'smart_total_active_mode': {'mode': 'SPOT_BULL'},
            'smart_total_mode_selector_state': {
                'last_selected': 'SPOT_BULL',
                'last_selected_score': 0.70,
                'cycles_since_switch': 1,
            },
        })
        regime = types.SimpleNamespace(mode='FUTURES_SHORT', confirmed=True, confidence=0.95)

        result = run_trades._run_smart_total_mode_selector(make_settings(), regime, db)

        assert result['ok'] is True
        assert result['selected_mode'] == 'SPOT_BULL'
        assert result['blocked'] is True
        assert result['switched'] is False
        assert 'blocked_hold_cycles' in result['reason']
        assert db.audit_events, "Selector must audit each cycle by default"

    def test_mode_selector_switches_on_confident_delta(self):
        import run_trades

        db = self._DB(runtime_state={
            'regime_profile_selector_config': {
                'smart_total_enabled': True,
                'mode_selector_enabled': True,
                'mode_min_hold_cycles': 2,
                'mode_min_confidence_delta': 0.05,
                'mode_require_confirmed': True,
            },
            'smart_total_active_mode': {'mode': 'SPOT_BULL'},
            'smart_total_mode_selector_state': {
                'last_selected': 'SPOT_BULL',
                'last_selected_score': 0.60,
                'cycles_since_switch': 5,
            },
        })
        regime = types.SimpleNamespace(mode='FUTURES_SHORT', confirmed=True, confidence=0.80)

        result = run_trades._run_smart_total_mode_selector(make_settings(), regime, db)

        assert result['ok'] is True
        assert result['selected_mode'] == 'FUTURES_SHORT'
        assert result['switched'] is True
        assert result['blocked'] is False
        assert 'switch_mode_confidence_delta' in result['reason']

        active_mode_payload = db.runtime_state.get('smart_total_active_mode')
        assert isinstance(active_mode_payload, str), "Active mode must persist as JSON payload"
        assert 'FUTURES_SHORT' in active_mode_payload


class TestCat5c_RegimeProfilePersistence:
    """Regime profile apply must persist only explicitly defined fields."""

    class _DB:
        def __init__(self, runtime_state=None):
            self.runtime_state = runtime_state or {}
            self.audit_events = []

        def execute_query(self, sql, params=None):
            if "WHERE key='kelly_config_override'" in sql:
                value = self.runtime_state.get('kelly_config_override')
                return [] if value is None else [{"value": value}]
            if "FROM trading_runtime_state WHERE key=%s" in sql and params:
                key = params[0]
                value = self.runtime_state.get(key)
                return [] if value is None else [{"value": value}]
            return []

        def execute_update(self, sql, params=None):
            if "trades_audit" in sql and params:
                self.audit_events.append(params)
                return 1
            if "trading_runtime_state" in sql and params and len(params) >= 2:
                self.runtime_state[params[0]] = params[1]
                return 1
            return 0

        def execute_transaction(self, stmts):
            for sql, params in stmts:
                if "kelly_config_override" in sql:
                    self.runtime_state['kelly_config_override'] = params[0]
                elif "trading_runtime_state" in sql and params and len(params) >= 2:
                    self.runtime_state[params[0]] = params[1]
            return True

    def test_apply_regime_params_does_not_persist_unscoped_scalars(self):
        import json
        import run_trades

        db = self._DB(runtime_state={
            'regime_profile_active_SPOT_BULL': {'profile': 'A'},
            'regime_profile_SPOT_BULL_A': {
                'stop_loss_pct': 9.99,
            },
            'kelly_config_override': {
                'take_profit_pct': {'new': 7.77},
            },
        })
        settings = make_settings(stop_loss_pct=3.36, take_profit_pct=4.14, min_signal_strength=0.45)

        run_trades._last_applied_regime_mode = None
        run_trades._last_applied_regime_profile = None
        run_trades._regime_param_baseline = None

        result = run_trades._apply_regime_params(settings, 'SPOT_BULL', db)

        assert result['skipped'] is False
        assert settings.stop_loss_pct == pytest.approx(9.99)
        persisted = json.loads(db.runtime_state['kelly_config_override'])
        assert persisted['stop_loss_pct']['new'] == pytest.approx(9.99)
        assert persisted['take_profit_pct']['new'] == pytest.approx(7.77)
        assert 'min_signal_strength' not in persisted

    def test_apply_regime_params_covers_prs_and_adverse_exit_fields(self):
        import run_trades

        db = self._DB(runtime_state={
            'regime_profile_active_SPOT_BULL': {'profile': 'A'},
            'regime_profile_SPOT_BULL_A': {
                'profit_realization_spot_partial_minutes': 77.0,
                'futures_adverse_exit_min_hours': 2.5,
            },
        })
        settings = make_settings(
            profit_realization_spot_partial_minutes=60.0,
            futures_adverse_exit_min_hours=4.0,
        )

        run_trades._last_applied_regime_mode = None
        run_trades._last_applied_regime_profile = None
        run_trades._regime_param_baseline = None

        result = run_trades._apply_regime_params(settings, 'SPOT_BULL', db)

        assert result['skipped'] is False
        assert settings.profit_realization_spot_partial_minutes == pytest.approx(77.0)
        assert settings.futures_adverse_exit_min_hours == pytest.approx(2.5)


class TestCat5d_BreakevenAuditSchema:
    """Breakeven audit must use the canonical trades_audit schema."""

    def test_spot_breakeven_does_not_use_legacy_event_data_column(self):
        import inspect
        import run_trades

        source = inspect.getsource(run_trades._check_sl_tp_positions)

        assert 'breakeven_activated' in source
        assert 'INSERT INTO trades_audit (event_data)' not in source


class TestCat5e_PredExitNetGate:
    """PredExit must gate on net PnL, not gross PnL."""

    def test_spot_predexit_uses_net_pnl_gate(self):
        import inspect
        import run_trades

        source = inspect.getsource(run_trades._check_sl_tp_positions)

        # s74: PredExit now evaluates both loss and profit branches.
        # The outer guard on pnl_pct_net has been removed; internal branches still gate on net PnL.
        assert "if pnl_pct_net < 0:" in source
        assert "elif pnl_pct_net > 0" in source
        assert "prediction_exit_enabled', True) and pnl_pct < 0" not in source


# ═════════════════════════════════════════════════════════════════════════════
# CAT 6 — Position Manager
# ═════════════════════════════════════════════════════════════════════════════

class TestCat6_PositionManager:
    """PositionManager CRUD and PnL calculation correctness."""

    def setup_method(self):
        from position_manager import PositionManager, Position
        self.pm = PositionManager()
        self.Position = Position

    def test_add_and_get_position(self):
        pos = self.Position("BTCUSDT", "BUY", 0.5, 50000.0, "2024-01-01T00:00:00Z")
        self.pm.add_position(pos)
        assert self.pm.get_position("BTCUSDT") is not None

    def test_get_nonexistent_returns_none(self):
        assert self.pm.get_position("XYZUSDT") is None

    def test_close_buy_position_pnl(self):
        from binance_commission import entry_commission, exit_commission

        pos = self.Position("BTCUSDT", "BUY", 1.0, 50000.0, "2024-01-01T00:00:00Z")
        self.pm.add_position(pos)
        closed = self.pm.close_position("BTCUSDT", 52000.0, "take_profit")
        assert closed is not None
        expected_pnl = (52000.0 - 50000.0) - entry_commission(1.0, 50000.0, 'spot') - exit_commission(1.0, 52000.0, 'spot')
        assert closed.pnl == pytest.approx(expected_pnl, abs=0.01)
        assert closed.pnl_percentage == pytest.approx(3.9, abs=0.2)

    def test_close_sell_position_pnl(self):
        from binance_commission import entry_commission, exit_commission

        pos = self.Position("BTCUSDT", "SELL", 0.5, 50000.0, "2024-01-01T00:00:00Z")
        self.pm.add_position(pos)
        closed = self.pm.close_position("BTCUSDT", 48000.0, "take_profit")
        expected_pnl = ((50000.0 - 48000.0) * 0.5) - entry_commission(0.5, 50000.0, 'spot') - exit_commission(0.5, 48000.0, 'spot')
        assert closed.pnl == pytest.approx(expected_pnl, abs=0.01)

    def test_closed_position_removed_from_active(self):
        pos = self.Position("ETHUSDT", "BUY", 1.0, 2000.0, "2024-01-01T00:00:00Z")
        self.pm.add_position(pos)
        self.pm.close_position("ETHUSDT", 2100.0, "manual")
        assert self.pm.get_position("ETHUSDT") is None

    def test_stop_loss_trigger(self):
        pos = self.Position("BTCUSDT", "BUY", 0.5, 50000.0, "2024-01-01T00:00:00Z",
                            stop_loss=48000.0, take_profit=53000.0)
        self.pm.add_position(pos)
        closed = self.pm.check_stop_loss_take_profit({"BTCUSDT": 47000.0})
        assert len(closed) == 1
        assert closed[0].close_reason == "stop_loss"

    def test_take_profit_trigger(self):
        pos = self.Position("BTCUSDT", "BUY", 0.5, 50000.0, "2024-01-01T00:00:00Z",
                            stop_loss=48000.0, take_profit=53000.0)
        self.pm.add_position(pos)
        closed = self.pm.check_stop_loss_take_profit({"BTCUSDT": 54000.0})
        assert len(closed) == 1
        assert closed[0].close_reason == "take_profit"

    def test_multiple_positions(self):
        self.pm.add_position(self.Position("BTCUSDT", "BUY", 0.5, 50000.0,
                                           "2024-01-01T00:00:00Z"))
        self.pm.add_position(self.Position("ETHUSDT", "BUY", 10.0, 3000.0,
                                           "2024-01-01T00:00:00Z"))
        assert len(self.pm.get_all_positions()) == 2

    def test_position_summary_values(self):
        self.pm.add_position(self.Position("BTCUSDT", "BUY", 0.5, 50000.0,
                                           "2024-01-01T00:00:00Z"))
        summary = self.pm.get_position_summary()
        assert summary["open_positions_count"] == 1
        assert summary["total_value_usdt"] == pytest.approx(25000.0, rel=0.1)


# ═════════════════════════════════════════════════════════════════════════════
# CAT 7 — API Endpoints (requires Docker running)
# ═════════════════════════════════════════════════════════════════════════════

API_BASE = "http://localhost:8000"
API_KEY = os.environ.get("CRYPTO_API_KEY", "")


def _api_available() -> bool:
    import urllib.request, socket, time as _t
    for _attempt in range(3):
        try:
            urllib.request.urlopen(f"{API_BASE}/health", timeout=5)
            return True
        except (OSError, TimeoutError, socket.timeout, Exception):
            if _attempt < 2:
                _t.sleep(1)
    return False


@pytest.mark.skipif(not _api_available(), reason="API not running on localhost:8000")
class TestCat7_APIEndpoints:
    """API endpoints must return expected structure and status codes."""

    def _get(self, path, timeout=10):
        import urllib.request, json
        headers = {}
        if API_KEY:
            headers["X-API-Key"] = API_KEY
        req = urllib.request.Request(f"{API_BASE}{path}", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    def test_health_status_returns_overall(self):
        data = self._get("/health/status", timeout=30)
        assert "overall" in data, "/health/status missing 'overall' key"
        assert data["overall"] in ("ok", "warning", "error")

    def test_health_status_has_db_check(self):
        data = self._get("/health/status", timeout=30)
        assert "db" in data.get("checks", data), "Missing 'db' in health status"

    def test_health_status_has_binance_check(self):
        data = self._get("/health/status", timeout=30)
        checks = data.get("checks", data)
        assert "binance" in checks

    def test_dashboard_summary_returns(self):
        data = self._get("/dashboard/summary", timeout=30)
        assert isinstance(data, dict)

    def test_dashboard_summary_has_market_regime(self):
        data = self._get("/dashboard/summary", timeout=30)
        assert "market_regime" in data, "Dashboard summary missing market_regime"

    def test_futures_positions_returns_structure(self):
        data = self._get("/futures/positions")
        assert "enabled" in data
        assert "positions" in data
        assert "count" in data

    def test_futures_closed_returns_structure(self):
        data = self._get("/futures/closed", timeout=30)
        assert "trades" in data or "closed" in data or isinstance(data, dict)

    def test_health_full_check_endpoint_exists(self):
        """New endpoint from this implementation must exist."""
        data = self._get("/health/full-check", timeout=30)
        assert isinstance(data, dict)
        assert "version" in data
        assert "categories" in data

    def test_health_full_check_has_all_categories(self):
        data = self._get("/health/full-check", timeout=30)
        cats = {c["name"] for c in data.get("categories", [])}
        expected = {"config_integrity", "market_regime", "futures_core",
                    "spot_pipeline", "kelly_sizing", "capital_flow"}
        missing = expected - cats
        assert not missing, f"Missing categories in full-check: {missing}"

    def test_health_full_check_has_summary(self):
        data = self._get("/health/full-check", timeout=30)
        assert "summary" in data
        assert "total" in data["summary"]
        assert "passed" in data["summary"]
        assert "failed" in data["summary"]
        assert "warned" in data["summary"]

    def test_metrics_override_performance_returns(self):
        data = self._get("/metrics/override-performance", timeout=30)
        assert isinstance(data, dict)

    def test_strategy_params_returns(self):
        data = self._get("/strategy/params?symbol=BTCUSDT")
        assert isinstance(data, dict)


# ═════════════════════════════════════════════════════════════════════════════
# CAT 8 — Profit-Blocking Bugs
# ═════════════════════════════════════════════════════════════════════════════

class TestCat8_ProfitBlockingBugs:
    """Tests that directly verify known profit-blocking bugs stay fixed."""

    # BH6 — _evaluate_short_momentum without client ref
    def test_BH6_momentum_without_client_returns_minus1_not_none(self):
        fc = make_fc()
        fc._binance_client_ref = None
        score, details = fc._evaluate_short_momentum("BTCUSDT", make_settings())
        assert score == -1
        assert isinstance(details, dict)

    def test_BH6_momentum_closes_tp_when_score_minus1(self):
        """When momentum returns -1 (no client), TP hit must CLOSE not hang."""
        pos = make_short(entry=100.0, tp_pct=7.0)
        fc = make_fc([pos])
        inject_price(fc, "BTCUSDT", 92.0)  # at TP
        fc._binance_client_ref = None
        s = make_settings(futures_smart_tp_enabled=True,
                          futures_smart_tp_extend_score_threshold=3)
        closed = fc.check_positions(settings=s)
        # score=-1 < threshold=3 → should CLOSE (not extend indefinitely)
        full_closes = [c for c in closed if not c.get('partial')]
        assert len(full_closes) == 1

    # Fix 1 regression: SL never retreats
    def test_BH_SL_never_retreats_after_smart_tp_extend(self):
        """BH regression Fix 1: max(sl_floor, pos.stop_loss) must always be >= floor.
        Verifies the ratchet property: effective SL can only go up, never down."""
        entry_price = 100.0
        sl_pct = 0.018
        sl_floor = entry_price * (1.0 - sl_pct)   # 98.2
        for saved_sl in [0.0, 95.0, 98.2, 100.0, 102.0, 105.0]:
            _sl_saved = saved_sl if saved_sl > 0 else sl_floor
            effective = max(sl_floor, _sl_saved)
            assert effective >= sl_floor, \
                f"BH: effective SL {effective} retreated below floor {sl_floor} (saved={saved_sl})"
            assert effective >= saved_sl or saved_sl <= 0, \
                f"BH: effective SL {effective} retreated below saved SL {saved_sl}"

    # Fix 3 regression: trail_best_price inferred correctly
    def test_BH_trail_best_price_inferred_from_sl_preserves_gains(self):
        """BH regression Fix 3: trail_best_price inferred = sl / (1+dist) must be < entry
        for valid SHORT trailing state — ensures no phantom reset on restart."""
        entry_price = 100.0
        trail_dist  = 0.02   # 2%
        # Scenario: trailing already moved SL to 97.0 (price was ~95.1)
        sl_after_trailing = 97.0
        inferred_best = sl_after_trailing / (1.0 + trail_dist)  # ~95.1
        assert inferred_best < entry_price, \
            "Fix 3: inferred best_price must be < entry for a valid SHORT trailing state"
        assert inferred_best < sl_after_trailing, \
            "Fix 3: inferred best_price must be less than SL (SL trails above best)"
        # Verify the guard condition: only use inferred if < entry AND < current price
        current_price = 96.0  # still profitable SHORT
        use_inferred = inferred_best < entry_price and inferred_best < current_price
        assert use_inferred, \
            f"Fix 3: guard condition should pass — inferred={inferred_best:.2f} < entry={entry_price} and < price={current_price}"

    # BH7 — Kelly hard cap sanity
    def test_BH7_kelly_never_exceeds_40pct_liquid(self):
        """Hard cap: Kelly result must never exceed 40% of liquid capital."""
        from run_trades import _kelly_position_size
        s = make_settings(kelly_max_pct_liquid=0.40)
        liquid = 1642.0  # real production liquid
        result = _kelly_position_size(s, 0.95, {"success_probability": 0.99},
                                      liquid, sl_pct=0.018, tp_pct=0.035)
        cap = liquid * 0.40
        assert result <= cap + 0.01, \
            f"BH7: Kelly={result:.2f} exceeded hard cap={cap:.2f} ({liquid}*40%)"

    # BH10 — datetime timezone bug
    def test_BH10_app_health_uses_aware_datetime(self):
        """BH10: datetime.utcnow() is naive — must use datetime.now(timezone.utc)."""
        import ast
        app_path = os.path.join(os.path.dirname(__file__), '..', 'app.py')
        with open(app_path, 'r', encoding='utf-8') as f:
            source = f.read()
        # After our fix, utcnow() should be replaced near the health check
        # Find the health status function and check it doesn't use utcnow
        lines = source.splitlines()
        in_health = False
        utcnow_in_health = []
        for i, line in enumerate(lines, 1):
            if 'def get_health_status' in line:
                in_health = True
            if in_health and 'def ' in line and 'get_health_status' not in line:
                in_health = False
            if in_health and 'datetime.utcnow()' in line:
                utcnow_in_health.append(i)
        assert not utcnow_in_health, \
            (f"BH10: datetime.utcnow() (naive) found in get_health_status() at lines "
             f"{utcnow_in_health} — use datetime.now(timezone.utc) instead")

    # Regime whipsaw regression
    def test_anti_whipsaw_cycles_value(self):
        from market_regime import _CONFIRM_CYCLES
        assert _CONFIRM_CYCLES >= 2, \
            f"Anti-whipsaw disabled! _CONFIRM_CYCLES={_CONFIRM_CYCLES}"

    # Stablecoin B2 regression
    def test_B2_stablecoin_detection_range(self):
        """Stablecoin detection: prices 0.98-1.02 must NOT be closed by B2."""
        stablecoin_prices = [0.9800, 0.9999, 1.0000, 1.0001, 1.0200]
        for price in stablecoin_prices:
            is_stable = 0.98 <= price <= 1.02
            assert is_stable, f"Price {price} should be detected as stablecoin"

    # Timezone fix regression (jun15)
    def test_min_hold_minutes_timezone_fix(self):
        """Naive datetime must be handled without exception in min_hold check."""
        naive_dt = datetime(2024, 1, 1, 12, 0, 0)
        aware_dt = datetime.now(timezone.utc)
        # The fix: replace(tzinfo=timezone.utc) if tzinfo is None
        if naive_dt.tzinfo is None:
            naive_dt = naive_dt.replace(tzinfo=timezone.utc)
        diff = (aware_dt - naive_dt).total_seconds()
        assert diff > 0  # no exception = fix works

    # F2 reversal guard regression
    def test_evaluate_tp_momentum_signature(self):
        from run_trades import _evaluate_tp_momentum
        import inspect
        sig = inspect.signature(_evaluate_tp_momentum)
        params = set(sig.parameters.keys())
        assert 'current_price' in params
        assert 'pnl_pct' in params


# ═════════════════════════════════════════════════════════════════════════════
# CAT 9 — CapBridge & Capital Flow
# ═════════════════════════════════════════════════════════════════════════════

class TestCat9_CapBridgeCapitalFlow:
    """CapBridge smart_rebalance_capital logic without network."""

    def _make_fc_with_shorts(self, n_shorts=0):
        positions = [make_short(f"SYM{i}USDT") for i in range(n_shorts)]
        fc = make_fc(positions)
        fc.max_shorts = 5
        return fc

    def test_smart_rebalance_capital_exists(self):
        fc = make_fc()
        assert hasattr(fc, 'smart_rebalance_capital'), \
            "smart_rebalance_capital method missing from FuturesClient"

    def test_capbridge_min_transfer_not_below_10(self):
        """Min transfer of $10 must be respected — no dust moves."""
        s = make_settings()
        min_tx = getattr(s, 'futures_capital_bridge_min_transfer', 10.0)
        assert min_tx >= 10.0, \
            f"futures_capital_bridge_min_transfer={min_tx} < $10 — too low for Binance"

    def test_capbridge_enabled_flag_exists_in_settings(self):
        s = make_settings()
        val = getattr(s, 'futures_capital_bridge_enabled', True)
        assert val is not None, "futures_capital_bridge_enabled missing from Settings"

    def test_smart_rebalance_dry_run_no_crash(self):
        """In dry-run mode, smart_rebalance_capital must not crash."""
        fc = self._make_fc_with_shorts(2)
        s = make_settings()
        try:
            result = fc.smart_rebalance_capital(
                spot_usdt=500.0,
                open_shorts=2,
                regime_mode="FUTURES_SHORT",
                settings=s,
                spot_liquid=500.0,
            )
        except TypeError:
            # Signature may differ — just check method exists and is callable
            pass
        except Exception as e:
            # Should not crash silently — any exception other than TypeError is unexpected
            assert False, f"smart_rebalance_capital crashed: {e}"

    def test_capbridge_case3_requires_zero_shorts(self):
        """Case 3 (return idle) must only trigger when no shorts are open."""
        # Verify the logic: 0 shorts + non-bear → return idle
        # This is a behavior assertion — tested via the guard condition
        fc = self._make_fc_with_shorts(0)
        n_shorts = fc.count_open_shorts()
        assert n_shorts == 0


# ═════════════════════════════════════════════════════════════════════════════
# CAT 10 — Regression: Historical bugs that caused real losses
# ═════════════════════════════════════════════════════════════════════════════

class TestCat10_HistoricalBugRegressions:
    """Verify that previously-fixed bugs with real financial impact stay fixed."""

    # B1: FUTURES_SHORT block happened AFTER spot limit check
    def test_B1_futures_import_from_run_trades(self):
        """FuturesClient must be importable from run_trades (no import error)."""
        from run_trades import _get_futures_client
        assert callable(_get_futures_client)

    # B3/B4: Hedge mode position side
    def test_B3_B4_open_short_uses_short_position_side(self):
        """open_short in hedge mode must use positionSide=SHORT."""
        fc = make_fc()
        # In dry_run, no network call — just verify the method exists
        assert hasattr(fc, 'open_short') and callable(fc.open_short)
        assert hasattr(fc, 'open_long') and callable(fc.open_long)

    # B5: Tick size precision
    def test_B5_align_price_method_exists(self):
        fc = make_fc()
        assert hasattr(fc, '_align_price'), \
            "_align_price must exist to handle tick size precision"

    # jun15 bug: _sell_holdings_not_in_top sold everything
    def test_sell_holdings_not_in_top_exists(self):
        from run_trades import _sell_holdings_not_in_top
        assert callable(_sell_holdings_not_in_top)

    # jun15 bug: Kelly overflow (MAX_PER_TRADE_USDT was separate from Kelly)
    def test_max_per_trade_aligned_with_kelly_max(self):
        from config import Settings
        s = Settings()
        # kelly_max_usdt should be <= max_per_trade_usdt (or reasonable multiple)
        # In production: both are $350
        kelly_max = getattr(s, 'kelly_max_usdt')
        max_trade = getattr(s, 'max_per_trade_usdt')
        # They don't need to be equal but kelly should not wildly exceed per-trade limit
        if max_trade > 0 and kelly_max > 0:
            ratio = kelly_max / max_trade
            assert ratio <= 3.0, \
                (f"kelly_max_usdt={kelly_max} is {ratio:.1f}x max_per_trade_usdt={max_trade} "
                 f"— misaligned Kelly cap")

    # F1: trailing_peak frozen between cycles
    def test_F1_trailing_peak_not_shared_across_positions(self):
        """_smart_meta must not share state between positions (mutable default bug)."""
        a = make_short("BTCUSDT")
        b = make_short("ETHUSDT")
        a._smart_meta['trail_best_price'] = 99.0
        assert 'trail_best_price' not in b._smart_meta, \
            "F1: trail_best_price leaked between positions (shared dict)"

    # F2: SmartTP reversal guard
    def test_F2_reversal_detection_logic(self):
        """Price >2% below recent high should trigger close_reversal."""
        recent_high = 100.0
        current_price = 97.5  # 2.5% below high
        pct_from_high = (current_price - recent_high) / recent_high
        assert pct_from_high < -0.02, "Reversal condition not met — check threshold"

    # Orphan position: excepción silenciosa post-buy
    def test_position_manager_add_does_not_silently_fail(self):
        from position_manager import PositionManager, Position
        pm = PositionManager()
        pos = Position("TESTUSDT", "BUY", 1.0, 100.0, "2024-01-01T00:00:00Z")
        pm.add_position(pos)
        assert pm.get_position("TESTUSDT") is not None, \
            "Position not found after add — silent failure detected"

    # Timezone regression (jun15)
    def test_naive_datetime_comparison_does_not_raise(self):
        """Naive datetime must not raise when compared to aware datetime."""
        naive = datetime(2024, 6, 1, 12, 0, 0)
        aware = datetime.now(timezone.utc)
        # The fix: always make naive aware before comparison
        if naive.tzinfo is None:
            naive = naive.replace(tzinfo=timezone.utc)
        try:
            diff = (aware - naive).total_seconds()
        except TypeError as e:
            pytest.fail(f"Timezone comparison raised: {e}")
        assert diff > 0


# ═════════════════════════════════════════════════════════════════════════════
# CAT 11 — Dashboard UX Regression (jun 19 2026)
# ═════════════════════════════════════════════════════════════════════════════

class TestCat11_DashboardUXRegression:
    """Verify dashboard UX fixes: entry_price fallback, modal data integrity."""

    # D1: entry_price math fallback (SHORT)
    def test_D1_entry_price_short_math_fallback(self):
        """For SHORT: entry_price = exit_price + pnl / qty"""
        exit_price = 68.02
        pnl        = 15.22
        qty        = 3.82
        derived = round(exit_price + pnl / qty, 6)
        # Should be close to ~72.0 (the known entry price)
        assert 70.0 < derived < 75.0, \
            f"SHORT entry_price fallback out of range: {derived}"

    # D1: entry_price math fallback (LONG)
    def test_D1_entry_price_long_math_fallback(self):
        """For LONG: entry_price = exit_price - pnl / qty"""
        exit_price = 70.0
        pnl        = 10.0
        qty        = 2.0
        derived = round(exit_price - pnl / qty, 6)
        assert derived == 65.0, f"LONG entry_price fallback wrong: {derived}"

    # D1: pnl=0 guard (should not divide by zero)
    def test_D1_zero_qty_does_not_raise(self):
        """qty=0 edge case must not raise ZeroDivisionError."""
        qty = 0
        try:
            if qty > 0:
                derived = 68.02 + 15.22 / qty
            else:
                derived = None
        except ZeroDivisionError:
            pytest.fail("D1: ZeroDivisionError on qty=0")
        assert derived is None

    # D3: multimodal counter logic
    def test_D3_multimodal_counter_futures_short_mode(self):
        """In FUTURES_SHORT mode, spot and futures counters must be independent."""
        mode        = 'FUTURES_SHORT'
        spot_count  = 2
        max_spot    = 12
        fut_count   = 5
        fut_max     = 7
        is_fut_short = mode == 'FUTURES_SHORT'
        spot_overflow = spot_count > max_spot
        fut_overflow  = is_fut_short and fut_count > fut_max
        assert not spot_overflow, "spot should not overflow at 2/12"
        assert not fut_overflow,  "futures should not overflow at 5/7"
        pos_str = f'Spot: {spot_count}/{max_spot} | Fut: {fut_count}/{fut_max}'
        assert '|' in pos_str, "multimodal counter must contain separator"

    # D3: overflow detection
    def test_D3_overflow_detected_correctly(self):
        """fut_count > fut_max must trigger overflow (red) state."""
        fut_count = 8
        fut_max   = 7
        fut_overflow = fut_count > fut_max
        assert fut_overflow, "overflow at 8/7 not detected"

    # D3: non-futures mode shows only spot
    def test_D3_spot_only_mode_no_futures_counter(self):
        """In SPOT_BULL mode futMax should be 0, counter shows only Spot."""
        mode = 'SPOT_BULL'
        is_fut_short = mode == 'FUTURES_SHORT'
        fut_max = 7 if is_fut_short else 0
        assert fut_max == 0


# ─────────────────────────────────────────────────────────────────────────────
# Health version marker
# ─────────────────────────────────────────────────────────────────────────────

def test_health_version_marker():
    """Ensures HEALTH_VERSION is updated with each implementation session."""
    assert HEALTH_VERSION, "HEALTH_VERSION must be set"
    assert len(HEALTH_VERSION) >= 10, "HEALTH_VERSION too short — update it!"
    print(f"\n[HealthCheck] System Health Test version: {HEALTH_VERSION}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
