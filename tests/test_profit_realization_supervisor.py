from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from profit_realization_supervisor import ProfitRealizationContext, evaluate


def _settings(**overrides):
    base = {
        "profit_realization_enabled": True,
        "profit_realization_spot_enabled": True,
        "profit_realization_sr_accel_enabled": True,
        "profit_realization_sr_accel_partial_multiplier": 0.80,
        "profit_realization_sr_accel_full_multiplier": 0.85,
        "profit_realization_sr_accel_drawback_multiplier": 0.85,
        "profit_realization_sr_resistance_zone_min": 0.68,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_sr_accel_triggers_partial_exit_earlier_in_range_scalp_long():
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    ctx = ProfitRealizationContext(
        symbol="BTCUSDT",
        trade_type="spot",
        side="BUY",
        pnl_pct=0.010,
        pnl_usdt=10.0,
        entry_price=100.0,
        current_price=101.0,
        opened_at=now - timedelta(minutes=16),
        profit_started_at=now - timedelta(minutes=16),
        peak_pnl_pct=0.013,
        pnl_velocity_1h=0.0,
        current_signal_strength=0.40,
        regime_mode="RANGE_SCALP",
        profile_label="B",
        metadata={
            "sr_zone_position": 0.73,
            "resistance_rejection": True,
            "profit_predexit_sr_resistance": True,
        },
    )

    decision = evaluate(ctx, _settings(), now=now)

    assert decision.should_close is True
    assert decision.close_pct > 0.0
    assert decision.reason == "profit_realization_supervisor_partial_sr_accel"


def test_sr_accel_disabled_does_not_force_early_partial_exit():
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    ctx = ProfitRealizationContext(
        symbol="BTCUSDT",
        trade_type="spot",
        side="BUY",
        pnl_pct=0.010,
        pnl_usdt=10.0,
        entry_price=100.0,
        current_price=101.0,
        opened_at=now - timedelta(minutes=16),
        profit_started_at=now - timedelta(minutes=16),
        peak_pnl_pct=0.013,
        pnl_velocity_1h=0.0,
        current_signal_strength=0.40,
        regime_mode="RANGE_SCALP",
        profile_label="B",
        metadata={
            "sr_zone_position": 0.73,
            "resistance_rejection": True,
            "profit_predexit_sr_resistance": True,
        },
    )

    decision = evaluate(ctx, _settings(profit_realization_sr_accel_enabled=False), now=now)

    assert decision.should_close is False


def test_sr_accel_triggers_partial_exit_earlier_in_range_scalp_short():
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    ctx = ProfitRealizationContext(
        symbol="ETHUSDT",
        trade_type="futures",
        side="SELL",
        pnl_pct=0.010,
        pnl_usdt=10.0,
        entry_price=100.0,
        current_price=99.0,
        opened_at=now - timedelta(minutes=16),
        profit_started_at=now - timedelta(minutes=16),
        peak_pnl_pct=0.013,
        pnl_velocity_1h=0.0,
        current_signal_strength=0.40,
        regime_mode="RANGE_SCALP",
        profile_label="B",
        metadata={
            "sr_zone_position": 0.14,
            "support_hold": True,
            "profit_predexit_sr_support": True,
        },
    )

    decision = evaluate(
        ctx,
        _settings(profit_realization_sr_support_zone_max=0.32),
        now=now,
    )

    assert decision.should_close is True
    assert decision.close_pct > 0.0
    assert decision.reason == "profit_realization_supervisor_partial_sr_accel"


def test_partial_is_deferred_when_profit_is_too_low_without_protective_signals():
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    ctx = ProfitRealizationContext(
        symbol="XPLUSDT",
        trade_type="futures",
        side="SELL",
        pnl_pct=0.0056,
        pnl_usdt=5.6,
        entry_price=1.0,
        current_price=0.9944,
        opened_at=now - timedelta(minutes=70),
        profit_started_at=now - timedelta(minutes=70),
        peak_pnl_pct=0.0080,
        pnl_velocity_1h=0.0008,
        current_signal_strength=0.55,
        regime_mode="RANGE_SCALP",
        profile_label="B",
        metadata={},
    )

    decision = evaluate(ctx, _settings(), now=now)

    assert decision.should_close is False
    assert decision.reason == "profit_realization_partial_deferred_low_pnl"


def test_partial_still_allowed_at_low_profit_when_momentum_is_low():
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    ctx = ProfitRealizationContext(
        symbol="XPLUSDT",
        trade_type="futures",
        side="SELL",
        pnl_pct=0.0056,
        pnl_usdt=5.6,
        entry_price=1.0,
        current_price=0.9944,
        opened_at=now - timedelta(minutes=70),
        profit_started_at=now - timedelta(minutes=70),
        peak_pnl_pct=0.0080,
        pnl_velocity_1h=-0.0009,
        current_signal_strength=0.10,
        regime_mode="RANGE_SCALP",
        profile_label="B",
        metadata={},
    )

    decision = evaluate(ctx, _settings(), now=now)

    assert decision.should_close is True
    assert decision.reason in {
        "profit_realization_supervisor_partial",
        "profit_realization_supervisor_full",
    }


def test_spot_uses_same_low_profit_partial_deferral_logic():
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    ctx = ProfitRealizationContext(
        symbol="BTCUSDT",
        trade_type="spot",
        side="BUY",
        pnl_pct=0.0050,
        pnl_usdt=5.0,
        entry_price=100.0,
        current_price=100.5,
        opened_at=now - timedelta(minutes=120),
        profit_started_at=now - timedelta(minutes=120),
        peak_pnl_pct=0.0075,
        pnl_velocity_1h=0.0010,
        current_signal_strength=0.60,
        regime_mode="SPOT_BULL",
        profile_label="B",
        metadata={},
    )

    decision = evaluate(ctx, _settings(), now=now)

    assert decision.should_close is False
    assert decision.reason == "profit_realization_partial_deferred_low_pnl"
