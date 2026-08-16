"""Regression tests for Smart Scale-Out (SSO)."""
from datetime import datetime, timedelta, timezone

import pytest

from smart_scale_out import SmartScaleOut, SSOPositionData


@pytest.fixture
def sso():
    return SmartScaleOut(settings=None)


def _make_pos(
    *,
    trade_type="spot",
    regime_mode="RANGE_SCALP",
    profile_label="A",
    pnl_pct=0.001,
    pnl_usdt=0.25,
    age_hours=3.5,
    position_size_usdt=150.0,
):
    now = datetime.now(timezone.utc)
    return SSOPositionData(
        symbol="SOLUSDT",
        side="BUY",
        pnl_pct=pnl_pct,
        pnl_usdt=pnl_usdt,
        position_size_usdt=position_size_usdt,
        entry_price=78.0,
        current_price=78.0 * (1 + pnl_pct),
        opened_at=now - timedelta(hours=age_hours),
        peak_pnl_pct=pnl_pct,
        trade_type=trade_type,
        regime_mode=regime_mode,
        profile_label=profile_label,
        pnl_velocity_1h=0.0,
    )


def test_flat_stagnation_timeout_fires_spot_range_scalp(sso):
    pos = _make_pos(pnl_pct=0.001, pnl_usdt=0.15, age_hours=3.5)
    action = sso.evaluate(pos)
    assert action.should_close is True
    assert action.close_pct == 1.0
    assert action.reason == "sso_flat_stagnation_timeout"


def test_flat_stagnation_timeout_fires_small_loss(sso):
    pos = _make_pos(pnl_pct=-0.002, pnl_usdt=-0.30, age_hours=3.5)
    action = sso.evaluate(pos)
    assert action.should_close is True
    assert action.reason == "sso_flat_stagnation_timeout"


def test_flat_stagnation_timeout_not_fires_too_young(sso):
    pos = _make_pos(pnl_pct=0.001, pnl_usdt=0.15, age_hours=2.0)
    action = sso.evaluate(pos)
    assert action.should_close is False


def test_flat_stagnation_timeout_not_fires_pnl_too_large(sso):
    # PnL above flat threshold but too small to trigger other SSO exits.
    pos = _make_pos(pnl_pct=0.005, pnl_usdt=0.75, age_hours=1.0)
    action = sso.evaluate(pos)
    assert action.should_close is False


def test_flat_stagnation_timeout_not_fires_spot_bull(sso):
    pos = _make_pos(pnl_pct=0.001, pnl_usdt=0.15, age_hours=4.0, regime_mode="SPOT_BULL")
    action = sso.evaluate(pos)
    assert action.should_close is False


def test_flat_stagnation_timeout_not_fires_futures(sso):
    pos = _make_pos(pnl_pct=0.001, pnl_usdt=0.15, age_hours=4.0, trade_type="futures")
    action = sso.evaluate(pos)
    assert action.should_close is False


def test_flat_stagnation_timeout_disabled(sso):
    class _DisabledSettings:
        sso_flat_stagnation_timeout_enabled = False

    sso_disabled = SmartScaleOut(settings=_DisabledSettings())
    pos = _make_pos(pnl_pct=0.001, pnl_usdt=0.15, age_hours=3.5)
    action = sso_disabled.evaluate(pos)
    assert action.should_close is False


def test_range_scalp_sso4_guard_shadow_keeps_close_but_marks_detail():
    class _Settings:
        sso_target_contribution_enabled = True
        sso_target_contribution_pct = 0.15
        sso_target_contribution_pct_range_scalp = 0.10
        sso_target_daily_usd = 40.0
        sso_target_min_hours = 1.0
        sso_target_range_scalp_guard_mode = "shadow"
        sso_target_range_scalp_guard_notional_min_usdt = 700.0
        sso_target_range_scalp_guard_min_pnl_pct = 1.40
        sso_target_range_scalp_guard_min_abs_gain_usdt = 9.0
        sso_target_range_scalp_guard_min_velocity_pct_h = 0.08
        sso_target_range_scalp_guard_min_vol_spike = 1.40

    sso = SmartScaleOut(settings=_Settings())
    pos = SSOPositionData(
        symbol="SOLUSDT",
        side="BUY",
        pnl_pct=0.009,
        pnl_usdt=6.5,
        position_size_usdt=1200.0,
        entry_price=100.0,
        current_price=100.9,
        opened_at=datetime.now(timezone.utc) - timedelta(hours=1.4),
        peak_pnl_pct=0.009,
        pnl_velocity_1h=0.0012,
        vol_spike=1.6,
        trend_4h="UP",
        daily_target=40.0,
        today_realized_pnl=2.0,
        trade_type="futures",
        regime_mode="RANGE_SCALP",
        profile_label="B",
    )

    action = sso._check_sso4_target_contribution(pos)
    assert action is not None
    assert action.should_close is True
    assert action.reason == "sso_target_contribution"
    assert "shadow_hold candidate" in action.detail


def test_sso4_disabled_keeps_sso3_multilevel_active():
    class _Settings:
        sso_target_contribution_enabled = False
        sso_multilevel_enabled = True

    sso = SmartScaleOut(settings=_Settings())
    pos = _make_pos(pnl_pct=0.012, pnl_usdt=6.5, age_hours=1.4)
    pos.atr_4h_pct = 0.01
    pos.daily_target = 40.0

    assert sso._check_sso4_target_contribution(pos) is None

    action = sso._check_sso3_multilevel(pos)
    assert action is not None
    assert action.should_close is True
    assert action.reason == "sso_multilevel_l1"
    assert action.close_pct == pytest.approx(0.255)


def test_winner_floor_blocks_small_profit_but_not_loss_exit():
    class _Settings:
        sso_min_winner_profit_usdt = 5.0
        sso_multilevel_enabled = False
        futures_bounce_rescue_enabled = True
        rescue_smart_gate_enabled = False
        sso_flat_stagnation_timeout_enabled = False

    sso = SmartScaleOut(settings=_Settings())
    small_winner = _make_pos(
        trade_type="futures",
        pnl_pct=0.0009,
        pnl_usdt=0.23,
        age_hours=4.0,
    )
    small_winner.bounce_entry = True

    action = sso.evaluate(small_winner)
    assert action.should_close is False
    assert "SSO-6:blocked_winner_floor($0.23<$5.00)" in action.diagnosis

    losing_position = _make_pos(
        trade_type="futures",
        pnl_pct=-0.006,
        pnl_usdt=-1.0,
        age_hours=4.0,
    )
    losing_position.rescue_signal = "SELL"
    losing_position.rescue_signal_strength = 0.80

    action = sso.evaluate(losing_position)
    assert action.should_close is True
    assert action.reason == "sso_loss_cut"


def test_range_scalp_sso4_guard_active_blocks_early_close():
    class _Settings:
        sso_target_contribution_enabled = True
        sso_target_contribution_pct = 0.15
        sso_target_contribution_pct_range_scalp = 0.10
        sso_target_daily_usd = 40.0
        sso_target_min_hours = 1.0
        sso_target_range_scalp_guard_mode = "active"
        sso_target_range_scalp_guard_notional_min_usdt = 700.0
        sso_target_range_scalp_guard_min_pnl_pct = 1.40
        sso_target_range_scalp_guard_min_abs_gain_usdt = 9.0
        sso_target_range_scalp_guard_min_velocity_pct_h = 0.08
        sso_target_range_scalp_guard_min_vol_spike = 1.40

    sso = SmartScaleOut(settings=_Settings())
    pos = SSOPositionData(
        symbol="SOLUSDT",
        side="BUY",
        pnl_pct=0.009,
        pnl_usdt=6.5,
        position_size_usdt=1200.0,
        entry_price=100.0,
        current_price=100.9,
        opened_at=datetime.now(timezone.utc) - timedelta(hours=1.4),
        peak_pnl_pct=0.009,
        pnl_velocity_1h=0.0012,
        vol_spike=1.6,
        trend_4h="UP",
        daily_target=40.0,
        today_realized_pnl=2.0,
        trade_type="futures",
        regime_mode="RANGE_SCALP",
        profile_label="B",
    )

    action = sso._check_sso4_target_contribution(pos)
    assert action is None
