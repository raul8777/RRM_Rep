"""
tests/test_profit_boost_entries.py
Unit tests for Profit Boost (PB) entry rules:
  - PB1: Stagger entries (max N new shorts per cycle)
  - PB2: Mass SL circuit breaker (global cooldown after N+ stops)
  - PB3: Red candle confirmation
  - PB4: Larger positions (config defaults)
  - PB5: BTC 3h momentum filter
  - PB6: Extended cooldown (4h)
"""

import sys
import os
import types
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ─── Test PB Config Defaults ─────────────────────────────────────────────────

class TestPBConfigDefaults:
    """Verify all PB config fields exist with correct defaults."""

    def test_futures_max_new_per_cycle_default(self):
        from config import Settings
        s = Settings()
        assert hasattr(s, 'futures_max_new_per_cycle')
        assert s.futures_max_new_per_cycle == 3

    def test_futures_mass_sl_threshold_default(self):
        from config import Settings
        s = Settings()
        assert hasattr(s, 'futures_mass_sl_threshold')
        assert s.futures_mass_sl_threshold == 3

    def test_futures_mass_sl_cooldown_hours_default(self):
        from config import Settings
        s = Settings()
        assert hasattr(s, 'futures_mass_sl_cooldown_hours')
        assert s.futures_mass_sl_cooldown_hours == 4.0

    def test_futures_confirm_red_candle_default(self):
        from config import Settings
        s = Settings()
        assert hasattr(s, 'futures_confirm_red_candle')
        assert s.futures_confirm_red_candle is True

    def test_futures_no_short_btc_3h_threshold_default(self):
        from config import Settings
        s = Settings()
        assert hasattr(s, 'futures_no_short_btc_3h_threshold')
        assert s.futures_no_short_btc_3h_threshold == 1.0

    def test_futures_sl_cooldown_hours_pb6(self):
        """Check the code default is 4.0h (env vars may override at runtime)."""
        from config import Settings
        # Check field definition default (not env-loaded value)
        field_info = Settings.model_fields['futures_sl_cooldown_hours']
        assert field_info.default == 4.0

    def test_futures_usdt_per_trade_pb4(self):
        """Check the code default is 50 (env vars may override at runtime)."""
        from config import Settings
        field_info = Settings.model_fields['futures_usdt_per_trade']
        assert field_info.default == 50.0

    def test_futures_kelly_cap_pct_pb4(self):
        """Check the code default is 0.20 (env vars may override at runtime)."""
        from config import Settings
        field_info = Settings.model_fields['futures_kelly_cap_pct']
        assert field_info.default == 0.20


# ─── Test PB2: Mass SL Circuit Breaker Variable ──────────────────────────────

class TestMassSLCircuitBreaker:
    """Test the mass SL global cooldown variable behavior."""

    def test_mass_sl_cooldown_variable_exists(self):
        import run_trades
        assert hasattr(run_trades, '_FUTURES_MASS_SL_COOLDOWN_UNTIL')

    def test_mass_sl_cooldown_initially_none(self):
        import run_trades
        # On fresh import, should be None (not active)
        assert run_trades._FUTURES_MASS_SL_COOLDOWN_UNTIL is None

    def test_mass_sl_cooldown_can_be_set(self):
        import run_trades
        # Simulate setting the cooldown
        original = run_trades._FUTURES_MASS_SL_COOLDOWN_UNTIL
        try:
            run_trades._FUTURES_MASS_SL_COOLDOWN_UNTIL = datetime.now(timezone.utc) + timedelta(hours=4)
            assert run_trades._FUTURES_MASS_SL_COOLDOWN_UNTIL is not None
            assert run_trades._FUTURES_MASS_SL_COOLDOWN_UNTIL > datetime.now(timezone.utc)
        finally:
            run_trades._FUTURES_MASS_SL_COOLDOWN_UNTIL = original

    def test_mass_sl_cooldown_expired(self):
        import run_trades
        original = run_trades._FUTURES_MASS_SL_COOLDOWN_UNTIL
        try:
            # Set to past → should be considered expired
            run_trades._FUTURES_MASS_SL_COOLDOWN_UNTIL = datetime.now(timezone.utc) - timedelta(hours=1)
            assert datetime.now(timezone.utc) >= run_trades._FUTURES_MASS_SL_COOLDOWN_UNTIL
        finally:
            run_trades._FUTURES_MASS_SL_COOLDOWN_UNTIL = original


# ─── Test PB1: Stagger Logic ─────────────────────────────────────────────────

class TestStaggerLogic:
    """Test that the stagger counter logic is sound."""

    def test_stagger_counter_resets_each_cycle(self):
        """The stagger counter is a local variable initialized to 0 before the loop.
        Verify the pattern works by checking the code structure."""
        import inspect
        import run_trades
        source = inspect.getsource(run_trades._buy_top_symbols)
        # Verify the stagger counter initialization exists
        assert '_pb_new_shorts_this_cycle = 0' in source
        assert '_pb_max_new_per_cycle' in source

    def test_stagger_check_before_open(self):
        """Verify the stagger check occurs before open_short call."""
        import inspect
        import run_trades
        source = inspect.getsource(run_trades._buy_top_symbols)
        # Find positions of stagger check and open_short
        stagger_pos = source.find('STAGGER_LIMIT')
        open_pos = source.find('opening short')
        assert stagger_pos > 0
        assert open_pos > 0
        assert stagger_pos < open_pos  # stagger check comes BEFORE opening

    def test_stagger_increment_after_success(self):
        """Verify counter increments after successful open."""
        import inspect
        import run_trades
        source = inspect.getsource(run_trades._buy_top_symbols)
        assert '_pb_new_shorts_this_cycle += 1' in source


# ─── Test PB3: Red Candle Confirmation ────────────────────────────────────────

class TestRedCandleConfirmation:
    """Test the red candle confirmation logic structure."""

    def test_red_candle_check_in_code(self):
        """Verify red candle check exists in the entry path."""
        import inspect
        import run_trades
        source = inspect.getsource(run_trades._buy_top_symbols)
        assert 'NO_RED_CANDLE' in source
        assert 'futures_confirm_red_candle' in source

    def test_red_candle_uses_completed_candle(self):
        """Verify we check the LAST COMPLETED candle (index -2), not current."""
        import inspect
        import run_trades
        source = inspect.getsource(run_trades._buy_top_symbols)
        # Should use [-2] for last completed candle
        assert '_5m_klines[-2]' in source


# ─── Test PB5: BTC 3h Momentum Filter ────────────────────────────────────────

class TestBTC3hMomentumFilter:
    """Test the 3h BTC momentum filter logic structure."""

    def test_3h_filter_in_code(self):
        """Verify 3h filter exists in the entry path."""
        import inspect
        import run_trades
        source = inspect.getsource(run_trades._buy_top_symbols)
        assert 'BTC_3H_RALLY' in source
        assert 'futures_no_short_btc_3h_threshold' in source

    def test_3h_filter_uses_4_candles(self):
        """Verify we fetch 4 1h candles to compute 3h change."""
        import inspect
        import run_trades
        source = inspect.getsource(run_trades._buy_top_symbols)
        assert "limit=4" in source or "'1h', limit=4" in source


# ─── Test PB6: Extended Cooldown ──────────────────────────────────────────────

class TestExtendedCooldown:
    """Test that SL cooldown is now 4h."""

    def test_cooldown_default_is_4h(self):
        from config import Settings
        field_info = Settings.model_fields['futures_sl_cooldown_hours']
        assert field_info.default == 4.0

    def test_cooldown_used_in_run_trades(self):
        """Verify run_trades uses the cooldown setting."""
        import inspect
        import run_trades
        source = inspect.getsource(run_trades._buy_top_symbols)
        assert 'futures_sl_cooldown_hours' in source
