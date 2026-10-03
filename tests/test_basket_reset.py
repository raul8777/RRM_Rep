import pytest

from datetime import datetime, timedelta, timezone

from binance_truth_guard import basket_reset_trigger_is_actionable, compute_basket_reset_decision


def _position(pnl, *, hours=6.0, pnl_pct=-0.01, momentum=0.0):
    return {
        "symbol": "BTCUSDT",
        "unrealized_pnl": pnl,
        "hours_held": hours,
        "pnl_pct": pnl_pct,
        "momentum_score": momentum,
    }


def test_profitable_basket_never_matches_loss_threshold():
    decision = compute_basket_reset_decision([_position(5.0)])

    assert decision["trigger"] is False
    assert decision["reason"] == "basket_hold"


def test_losing_mature_basket_triggers_reset():
    decision = compute_basket_reset_decision([_position(-11.0)])

    assert decision["trigger"] is True
    assert decision["reason"] == "basket_underperforming_reset"


def test_positive_loss_threshold_is_rejected():
    with pytest.raises(ValueError, match="must be negative"):
        compute_basket_reset_decision([_position(-11.0)], pnl_threshold_usd=10.0)


def test_repeated_trigger_is_suppressed_during_cooldown():
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    decision = compute_basket_reset_decision([_position(-11.0)])
    previous = {
        "trigger": True,
        "actioned_at": (now - timedelta(minutes=5)).isoformat(),
    }

    assert basket_reset_trigger_is_actionable(
        decision,
        previous,
        cooldown_seconds=3600,
        now=now,
    ) is False


def test_trigger_becomes_actionable_after_cooldown():
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    decision = compute_basket_reset_decision([_position(-11.0)])
    previous = {
        "trigger": True,
        "actioned_at": (now - timedelta(hours=2)).isoformat(),
    }

    assert basket_reset_trigger_is_actionable(
        decision,
        previous,
        cooldown_seconds=3600,
        now=now,
    ) is True