from types import SimpleNamespace

import position_telemetry_watcher as ptw


class _Settings(SimpleNamespace):
    pass


def _settings(**overrides):
    base = {
        "position_profit_guard_enabled": True,
        "position_profit_guard_activation_pct": 0.35,
        "position_profit_guard_min_peak_pct": 0.50,
        "position_profit_guard_min_giveback_pct": 0.25,
        "position_profit_guard_giveback_factor": 0.55,
        "position_profit_guard_soft_lock_factor": 0.25,
        "position_profit_guard_hard_lock_factor": 0.45,
        "position_profit_guard_floor_lock_pct": 0.05,
        "position_profit_guard_sl_buffer_pct": 0.20,
        "position_profit_guard_min_step_pct": 0.03,
    }
    base.update(overrides)
    return _Settings(**base)


def test_profit_guard_returns_none_below_activation():
    snap = {
        "side": "LONG",
        "entry_price": 100.0,
        "current_price": 100.2,
        "unrealized_pnl_pct": 0.2,
        "peak_pnl_pct": 0.004,
        "stop_loss": 99.0,
    }
    guard = ptw._compute_profit_guard_target(snap, _settings())
    assert guard is None


def test_profit_guard_long_hard_mode_and_tighter_sl():
    snap = {
        "side": "LONG",
        "entry_price": 100.0,
        "current_price": 101.0,
        "unrealized_pnl_pct": 1.0,
        "peak_pnl_pct": 0.03,  # 3%
        "stop_loss": 100.3,
    }
    guard = ptw._compute_profit_guard_target(snap, _settings())
    assert guard is not None
    assert guard["lock_mode"] == "hard"
    assert guard["target_sl"] > 100.3
    assert guard["target_sl"] < 101.0


def test_profit_guard_short_soft_mode_and_tighter_sl():
    snap = {
        "side": "SHORT",
        "entry_price": 100.0,
        "current_price": 98.8,
        "unrealized_pnl_pct": 1.2,
        "peak_pnl_pct": 0.015,  # 1.5%
        "stop_loss": 100.4,
    }
    guard = ptw._compute_profit_guard_target(snap, _settings())
    assert guard is not None
    assert guard["lock_mode"] == "soft"
    assert guard["target_sl"] < 100.4
    assert guard["target_sl"] > 98.8
