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
