from types import SimpleNamespace

import position_telemetry_watcher as ptw
from futures_client import FuturesPosition


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


def test_futures_snapshot_uses_contract_qty_and_stable_position_id():
    position = FuturesPosition(
        symbol="ALTUSDT",
        side="SHORT",
        qty=1000.0,
        entry_price=1.0,
        sl_price=1.03,
        tp_price=0.95,
        order_id="entry-1",
        sl_order_id="sl-1",
        tp_order_id="tp-1",
    )
    client = SimpleNamespace(_active_leverage=10)

    snapshot = ptw._snapshot_futures_position(position, 1.01, client)

    assert snapshot["position_id"] == "ALTUSDT_SHORT"
    assert snapshot["quantity"] == 1000.0
    assert snapshot["unrealized_pnl_usdt"] == -10.0


def test_belief_prefers_persisted_thesis_snapshot(monkeypatch):
    persisted = {
        "side": "LONG",
        "stop_price": 95.0,
        "target_price": 112.0,
        "entry_success_probability": 0.68,
    }
    captured = {}

    def fake_evaluate_belief(**kwargs):
        captured.update(kwargs)
        return {"belief_state": "intact"}

    monkeypatch.setattr(ptw, "evaluate_belief", fake_evaluate_belief)
    monkeypatch.setattr(ptw, "_update_drawdown_buffer", lambda *args: [])
    monkeypatch.setattr(ptw, "_analyze_drawdown_trend", lambda *args: {})
    monkeypatch.setattr(ptw, "_should_persist_belief", lambda *args: False)

    result, should_persist = ptw._evaluate_position_belief(
        position_id="position-1",
        symbol="ALTUSDT",
        side="LONG",
        pnl_usdt=-1.0,
        current_price=99.0,
        metadata={
            "thesis_snapshot_v1": persisted,
            "entry_decision_prediction": {"stop_price": 80.0, "target_price": 130.0},
        },
        pnl_prediction={"stop_price": 70.0, "target_price": 140.0},
        settings=_Settings(position_belief_enabled=True),
    )

    assert result == {"belief_state": "intact"}
    assert should_persist is False
    assert captured["thesis_snapshot"] == persisted


def test_drawdown_velocity_is_price_normalized():
    buffer = [
        {"ts": float(second), "price": 0.01 * (1.0 + second * 0.001), "pnl_usdt": -float(second)}
        for second in range(20)
    ]

    trend = ptw._analyze_drawdown_trend(buffer, "SHORT")

    assert trend["velocity_per_sec"] < -0.0009
    assert trend["velocity_per_sec"] > -0.0011


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


def test_shadow_metrics_expose_shared_lifecycle_surfaces():
    original = dict(ptw._shadow_metrics)
    try:
        ptw._shadow_metrics["profit_guard_actions"] = 3
        ptw._shadow_metrics["profit_guard_hard"] = 2
        ptw._shadow_metrics["profit_guard_soft"] = 1
        ptw._shadow_metrics["drawdown_actions"] = 4
        ptw._shadow_metrics["drawdown_cuts"] = 1
        ptw._shadow_metrics["drawdown_tightens"] = 3
        ptw._shadow_metrics["drawdown_recoverable_windows"] = 6
        ptw._shadow_metrics["drawdown_failed_recovery_windows"] = 2
        ptw._shadow_metrics["drawdown_shock_events"] = 1
        ptw._shadow_metrics["drawdown_deferred_cuts"] = 3
        ptw._shadow_metrics["profit_positions_evaluated"] = 12
        ptw._shadow_metrics["loss_positions_evaluated"] = 9
        ptw._shadow_metrics["profit_guard_candidates"] = 7
        ptw._shadow_metrics["last_summary"] = {"profit_guard_actions": 3, "drawdown_monitor_applied": 4}

        metrics = ptw.get_sr_invalidation_shadow_metrics()

        assert metrics["profit_guard_actions"] == 3
        assert metrics["profit_guard_hard"] == 2
        assert metrics["profit_guard_soft"] == 1
        assert metrics["drawdown_actions"] == 4
        assert metrics["drawdown_cuts"] == 1
        assert metrics["drawdown_tightens"] == 3
        assert metrics["drawdown_recoverable_windows"] == 6
        assert metrics["drawdown_failed_recovery_windows"] == 2
        assert metrics["drawdown_shock_events"] == 1
        assert metrics["drawdown_deferred_cuts"] == 3
        assert metrics["profit_positions_evaluated"] == 12
        assert metrics["loss_positions_evaluated"] == 9
        assert metrics["profit_guard_candidates"] == 7
        assert metrics["downstream_surfaces"] == [
            "smart_scale_out",
            "profit_realization_supervisor",
            "unified_governance_supervisor",
        ]
        assert metrics["last_summary"]["drawdown_monitor_applied"] == 4
    finally:
        ptw._shadow_metrics.clear()
        ptw._shadow_metrics.update(original)


def test_drawdown_recovery_window_defers_cut_when_rescue_is_alive():
    snap = {
        "unrealized_pnl_usdt": -11.2,
    }
    trend = {
        "recent_velocity": 0.0002,
        "acceleration": 0.00003,
        "recovery_signal": True,
    }
    prior = {
        "threshold_cross_pnl": -15.0,
        "rescue_best_unrealized": -8.0,
        "rescue_observed": True,
    }
    settings = _Settings(
        drawdown_monitor_threshold_usdt=-10.0,
        drawdown_recovery_score_min_usdt=3.0,
        drawdown_failed_recovery_giveback_usdt=4.0,
        drawdown_shock_recent_velocity_threshold=-0.0012,
    )

    out = ptw._assess_drawdown_recovery_window(snap, trend, prior, settings)

    assert out["defer_cut"] is True
    assert out["failed_recovery"] is False
    assert out["phase"] == "recoverable"


def test_drawdown_recovery_window_reenables_cut_after_failed_recovery():
    snap = {
        "unrealized_pnl_usdt": -13.0,
    }
    trend = {
        "recent_velocity": -0.002,
        "acceleration": -0.0002,
        "recovery_signal": False,
    }
    prior = {
        "threshold_cross_pnl": -15.0,
        "rescue_best_unrealized": -6.0,
        "rescue_observed": True,
    }
    settings = _Settings(
        drawdown_monitor_threshold_usdt=-10.0,
        drawdown_recovery_score_min_usdt=3.0,
        drawdown_failed_recovery_giveback_usdt=4.0,
        drawdown_shock_recent_velocity_threshold=-0.0012,
    )

    out = ptw._assess_drawdown_recovery_window(snap, trend, prior, settings)

    assert out["defer_cut"] is False
    assert out["failed_recovery"] is True
    assert out["phase"] == "failed_recovery"
