from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from loss_focus import build_loss_focus_snapshot, evaluate_loss_focus


def test_loss_focus_triggers_once_and_tracks_new_minimum():
    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    state, event = evaluate_loss_focus({}, -9.0, 9.0, 6.0, now)

    assert event == "loss_focus_triggered"
    assert state["active"] is True
    assert state["episodes"] == 1

    metadata = {"loss_focus_state": state}
    state, event = evaluate_loss_focus(metadata, -9.0, 9.0, 6.0, now)
    assert event is None

    metadata = {"loss_focus_state": state}
    state, event = evaluate_loss_focus(metadata, -11.5, 9.0, 6.0, now)
    assert event == "loss_focus_drawdown_update"
    assert state["minimum_pnl_usdt"] == -11.5


def test_loss_focus_rearms_only_after_recovery():
    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    state, _ = evaluate_loss_focus({}, -9.0, 9.0, 6.0, now)

    state, event = evaluate_loss_focus({"loss_focus_state": state}, -5.99, 9.0, 6.0, now)
    assert event == "loss_focus_recovered"
    assert state["active"] is False

    state, event = evaluate_loss_focus({"loss_focus_state": state}, -9.1, 9.0, 6.0, now)
    assert event == "loss_focus_triggered"
    assert state["episodes"] == 2


def test_loss_focus_snapshot_is_json_safe_and_keeps_entry_context():
    position = SimpleNamespace(
        position_id="p1",
        symbol="TESTUSDT",
        side="BUY",
        entry_price=Decimal("10.0"),
        entry_time=datetime(2026, 7, 21, tzinfo=timezone.utc),
        quantity=Decimal("3"),
        entry_commission=Decimal("0.03"),
        stop_loss=Decimal("9.5"),
        take_profit=Decimal("11.0"),
    )
    snapshot = build_loss_focus_snapshot(
        position=position,
        metadata={
            "entry_context_status": "canonical",
            "trade_type": "spot",
            "entry_order_id": "123",
            "peak_quality_profile": "A",
            "last_pred_pnl": Decimal("1.2"),
        },
        pnl_usdt_net=-9.0,
        pnl_pct_net=-0.3,
        pnl_usdt_gross=-8.94,
        current_price=9.7,
        regime_mode="SPOT_BULL",
        runtime_profile="A",
        signal_strength=0.8,
        signal_direction="SELL",
        pnl_velocity_1h=-0.01,
        peak_pnl_pct=0.02,
        fear_greed_value=45.0,
    )

    assert snapshot["position_id"] == "p1"
    assert snapshot["entry_context_status"] == "canonical"
    assert snapshot["quality_profile_at_entry"] == "A"
    assert snapshot["entry_time"] == "2026-07-21T00:00:00+00:00"
    assert snapshot["distance_to_sl_pct"] == 2.06185567
