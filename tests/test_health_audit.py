import os
from datetime import datetime, timezone

from fastapi.testclient import TestClient

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("TRADINGVIEW_WEBHOOK_SECRET", "supersecret")
os.environ.setdefault("BINANCE_USE_TESTNET", "true")


def test_health_includes_audit_consistency(monkeypatch):
    from app import app

    class _Comp:
        def __init__(self):
            self.db_client = None

    monkeypatch.setattr("app.get_components", lambda: _Comp())
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert "audit_consistency" in payload
    assert payload["audit_consistency"]["available"] is False


def test_trade_diagnostics_helper_splits_scope_and_reason(monkeypatch):
    import app as app_module

    class _DB:
        def execute_query(self, sql, params=None):
            return [
                {
                    "trade_scope": "spot",
                    "close_reason": "stop_loss",
                    "pnl": -5.0,
                    "pnl_percentage": -2.1,
                    "entry_commission": 0.2,
                    "exit_commission": 0.1,
                    "hold_seconds": 7200,
                },
                {
                    "trade_scope": "spot",
                    "close_reason": "smart_tp_close",
                    "pnl": 8.0,
                    "pnl_percentage": 3.4,
                    "entry_commission": 0.2,
                    "exit_commission": 0.2,
                    "hold_seconds": 10800,
                },
                {
                    "trade_scope": "futures",
                    "close_reason": "stop_loss",
                    "pnl": -3.0,
                    "pnl_percentage": -1.4,
                    "entry_commission": 0.1,
                    "exit_commission": 0.1,
                    "hold_seconds": 3600,
                },
            ]

    monkeypatch.setattr(app_module, "db_client", _DB())
    payload = app_module._trade_diagnostics_range(
        datetime(2026, 7, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 2, tzinfo=timezone.utc),
    )

    assert payload["summary"]["n_trades"] == 3
    assert payload["by_scope"]["spot"]["n_trades"] == 2
    assert payload["by_scope"]["spot"]["win_rate_pct"] == 50.0
    assert payload["by_scope"]["spot"]["by_reason"][0]["close_reason"] == "stop_loss"
    assert payload["by_scope"]["futures"]["stop_loss_share_pct"] == 100.0


def test_trade_diagnostics_endpoint(monkeypatch):
    import app as app_module
    from fastapi.testclient import TestClient

    class _DB:
        def execute_query(self, sql, params=None):
            return [
                {
                    "trade_scope": "spot",
                    "close_reason": "take_profit",
                    "pnl": 4.0,
                    "pnl_percentage": 1.8,
                    "entry_commission": 0.1,
                    "exit_commission": 0.1,
                    "hold_seconds": 5400,
                }
            ]

    monkeypatch.setattr(app_module, "db_client", _DB())
    client = TestClient(app_module.app)
    headers = {}
    if os.environ.get("DASHBOARD_API_KEY"):
        headers["X-API-Key"] = os.environ["DASHBOARD_API_KEY"]
    response = client.get("/metrics/trade-diagnostics?hours=24", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["by_scope"]["spot"]["n_trades"] == 1
    assert payload["by_scope"]["spot"]["by_reason"][0]["close_reason"] == "take_profit"


def test_smart_backtest_validation_helper_pass(monkeypatch):
    import app as app_module

    def _fake_baseline(run_id):
        return {
            "id": run_id,
            "label": f"baseline-{run_id}",
            "pnl_total": 100.0,
            "return_pct": 20.0,
            "win_rate": 45.0,
            "profit_factor": 1.4,
            "max_drawdown": 10.0,
            "n_trades": 20,
        }

    def _fake_candidate(session_id):
        return {
            "id": 999,
            "label": f"candidate-{session_id}",
            "pnl_total": 104.0,
            "return_pct": 21.0,
            "win_rate": 46.0,
            "profit_factor": 1.45,
            "max_drawdown": 10.5,
            "n_trades": 18,
        }

    monkeypatch.setattr(app_module, "_fetch_backtest_run_metrics", _fake_baseline)
    monkeypatch.setattr(app_module, "_fetch_best_backtest_run_for_session", _fake_candidate)

    payload = app_module._evaluate_smart_backtest_validation("root-session")
    assert payload["overall"] == "pass"
    assert all(item["status"] == "pass" for item in payload["scenarios"])


def test_smart_backtest_validation_helper_fail(monkeypatch):
    import app as app_module

    def _fake_baseline(run_id):
        return {
            "id": run_id,
            "label": f"baseline-{run_id}",
            "pnl_total": 100.0,
            "return_pct": 20.0,
            "win_rate": 45.0,
            "profit_factor": 1.4,
            "max_drawdown": 10.0,
            "n_trades": 20,
        }

    def _fake_candidate(session_id):
        return {
            "id": 999,
            "label": f"candidate-{session_id}",
            "pnl_total": 80.0,
            "return_pct": 15.0,
            "win_rate": 35.0,
            "profit_factor": 1.1,
            "max_drawdown": 15.0,
            "n_trades": 18,
        }

    monkeypatch.setattr(app_module, "_fetch_backtest_run_metrics", _fake_baseline)
    monkeypatch.setattr(app_module, "_fetch_best_backtest_run_for_session", _fake_candidate)

    payload = app_module._evaluate_smart_backtest_validation("root-session")
    assert payload["overall"] == "fail"
    assert all(item["status"] == "fail" for item in payload["scenarios"])


def test_smart_backtest_validation_endpoint(monkeypatch):
    import app as app_module
    from fastapi.testclient import TestClient

    monkeypatch.setattr(
        app_module,
        "_evaluate_smart_backtest_validation",
        lambda root_session_id, min_pnl_delta_pct=-3.0, min_win_rate_delta_pct=-5.0, max_drawdown_delta_pct=15.0: {
            "ok": True,
            "root_session_id": root_session_id,
            "overall": "pass",
            "thresholds": {
                "min_pnl_delta_pct": min_pnl_delta_pct,
                "min_win_rate_delta_pct": min_win_rate_delta_pct,
                "max_drawdown_delta_pct": max_drawdown_delta_pct,
            },
            "scenarios": [],
        },
    )

    client = TestClient(app_module.app)
    headers = {}
    if os.environ.get("DASHBOARD_API_KEY"):
        headers["X-API-Key"] = os.environ["DASHBOARD_API_KEY"]
    response = client.get("/smart-backtest/validation/test-root", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["overall"] == "pass"
    assert payload["root_session_id"] == "test-root"


def test_start_smart_backtest_validation_uses_validation_context(monkeypatch):
    import app as app_module
    from fastapi.testclient import TestClient

    monkeypatch.setattr(
        app_module,
        "_start_smart_backtest_task",
        lambda background_tasks, body, validation_context=None: {
            "ok": True,
            "run_id": "run-123",
            "status": "running",
            "validation_context": validation_context,
        },
    )

    client = TestClient(app_module.app)
    headers = {}
    if os.environ.get("DASHBOARD_API_KEY"):
        headers["X-API-Key"] = os.environ["DASHBOARD_API_KEY"]
    response = client.post(
        "/smart-backtest/validation/start",
        headers=headers,
        json={
            "period": "1y",
            "simulations": 12000,
            "candidate_label": "spot-bull-quality-v1",
            "candidate_params": {"min_signal_strength": 0.45},
            "notes": "validation run",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["validation_context"]["candidate_label"] == "spot-bull-quality-v1"
    assert payload["validation_context"]["candidate_params"]["min_signal_strength"] == 0.45


def test_selector_config_normalization_supports_smart_total_mode_fields():
    import app as app_module

    cfg = app_module._normalize_selector_config_payload({
        "smart_total_enabled": True,
        "mode_selector_enabled": True,
        "mode_require_confirmed": False,
        "mode_min_hold_cycles": "4",
        "mode_min_confidence_delta": "0.12",
        "profile_min_hold_cycles": "5",
        "profile_min_score_delta": "0.07",
    })

    assert cfg["smart_total_enabled"] is True
    assert cfg["mode_selector_enabled"] is True
    assert cfg["mode_require_confirmed"] is False
    assert cfg["mode_min_hold_cycles"] == 4
    assert cfg["mode_min_confidence_delta"] == 0.12
    assert cfg["profile_min_hold_cycles"] == 5
    assert cfg["profile_min_score_delta"] == 0.07


def test_trade_throughput_exposure_helper(monkeypatch):
    import app as app_module

    class _DB:
        def execute_query(self, sql, params=None):
            return [
                {
                    "trade_scope": "spot",
                    "symbol": "BTCUSDT",
                    "entry_time": datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc),
                    "exit_time": datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc),
                    "quantity": 0.01,
                    "entry_price": 100000.0,
                    "pnl": 10.0,
                    "close_reason": "take_profit",
                },
                {
                    "trade_scope": "futures",
                    "symbol": "ETHUSDT",
                    "entry_time": datetime(2026, 7, 1, 2, 0, tzinfo=timezone.utc),
                    "exit_time": datetime(2026, 7, 1, 3, 0, tzinfo=timezone.utc),
                    "quantity": 0.5,
                    "entry_price": 2000.0,
                    "pnl": -5.0,
                    "close_reason": "stop_loss",
                },
            ]

    monkeypatch.setattr(app_module, "db_client", _DB())
    payload = app_module._trade_throughput_exposure_range(
        datetime(2026, 7, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 2, tzinfo=timezone.utc),
        cycle_minutes=5,
    )

    assert payload["summary"]["n_trades"] == 2
    assert payload["by_scope"]["spot"]["n_trades"] == 1
    assert payload["by_scope"]["spot"]["total_notional_usdt"] == 1000.0
    assert payload["by_scope"]["futures"]["n_trades"] == 1


def test_spot_bull_root_cause_helper(monkeypatch):
    import app as app_module

    monkeypatch.setattr(
        app_module,
        "_trade_diagnostics_range",
        lambda start_ts, end_ts=None: {
            "by_scope": {
                "spot": {
                    "n_trades": 10,
                    "stop_loss_share_pct": 60.0,
                    "win_rate_pct": 35.0,
                    "avg_win": 5.0,
                    "avg_loss": -4.0,
                    "by_reason": [
                        {"close_reason": "stop_loss", "n_trades": 6},
                        {"close_reason": "prediction_exit_deteriorated", "n_trades": 2},
                    ],
                },
                "futures": {
                    "n_trades": 5,
                    "stop_loss_share_pct": 40.0,
                    "win_rate_pct": 45.0,
                    "avg_win": 7.0,
                    "avg_loss": -6.0,
                    "by_reason": [
                        {"close_reason": "stop_loss", "n_trades": 2},
                        {"close_reason": "portfolio_rebalancing", "n_trades": 1},
                    ],
                },
            }
        },
    )
    monkeypatch.setattr(
        app_module,
        "_trade_throughput_exposure_range",
        lambda start_ts, end_ts=None, cycle_minutes=5: {
            "by_scope": {
                "spot": {"fragmentation_index_pct": 70.0, "slot_utilization_pct": 20.0},
                "futures": {"fragmentation_index_pct": 40.0, "slot_utilization_pct": 35.0},
            }
        },
    )

    payload = app_module._quantify_spot_bull_root_causes(hours=24, cycle_minutes=5)
    assert payload["ok"] is True
    assert payload["scopes"]["spot"]["n_trades"] == 10
    assert payload["overall_weighted_contributions_pct"]["entry_quality_pct"] > 0


def test_spot_bull_candidate_endpoint(monkeypatch):
    import app as app_module
    from fastapi.testclient import TestClient

    class _DB:
        def execute_query(self, sql, params=None):
            return [{"value": {"snapshot_id": "snap-1"}, "updated_at": datetime(2026, 7, 3, tzinfo=timezone.utc)}]

    monkeypatch.setattr(app_module, "db_client", _DB())
    monkeypatch.setattr(app_module, "dump_active_config", lambda settings: {
        "min_signal_strength": 0.389,
        "min_success_probability": 0.499,
        "futures_min_prob": 0.499,
        "futures_vol_spike_min": 0.91,
        "max_positions_spot_bull": 20,
        "max_positions_futures_short": 12,
        "replace_ev_min_ratio": 1.1,
        "overflow_min_strength": 0.8,
        "overflow_min_prob": 0.65,
        "kelly_conviction_max_pct": 0.10,
        "futures_max_new_per_cycle": 5,
        "futures_overflow_min_strength": 0.55,
        "futures_overflow_min_vol_spike": 2.0,
    })

    client = TestClient(app_module.app)
    headers = {}
    if os.environ.get("DASHBOARD_API_KEY"):
        headers["X-API-Key"] = os.environ["DASHBOARD_API_KEY"]
    response = client.get("/tuning/candidates/spot-bull-v1", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["candidate"]["candidate_label"] == "spot_bull_quality_v1"
    assert "futures_mirror_changes" in payload["candidate"]
    assert payload["latest_futures_backup"]["value"]["snapshot_id"] == "snap-1"


def test_start_spot_bull_candidate_validation(monkeypatch):
    import app as app_module
    from fastapi.testclient import TestClient

    monkeypatch.setattr(
        app_module,
        "_start_smart_backtest_task",
        lambda background_tasks, body, validation_context=None: {
            "ok": True,
            "body": body,
            "validation_context": validation_context,
        },
    )
    monkeypatch.setattr(app_module, "dump_active_config", lambda settings: {
        "min_signal_strength": 0.389,
        "min_success_probability": 0.499,
        "futures_min_prob": 0.499,
        "futures_vol_spike_min": 0.91,
        "max_positions_spot_bull": 20,
        "max_positions_futures_short": 12,
        "replace_ev_min_ratio": 1.1,
        "overflow_min_strength": 0.8,
        "overflow_min_prob": 0.65,
        "kelly_conviction_max_pct": 0.10,
        "futures_max_new_per_cycle": 5,
        "futures_overflow_min_strength": 0.55,
        "futures_overflow_min_vol_spike": 2.0,
    })

    client = TestClient(app_module.app)
    headers = {}
    if os.environ.get("DASHBOARD_API_KEY"):
        headers["X-API-Key"] = os.environ["DASHBOARD_API_KEY"]
    response = client.post(
        "/tuning/candidates/spot-bull-v1/validate-start",
        headers=headers,
        json={"period": "1y", "simulations": 1200},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["validation_context"]["candidate_label"] == "spot_bull_quality_v1"
    assert payload["body"]["simulations"] == 1200


def test_spot_bull_candidate_decision_endpoint(monkeypatch):
    import app as app_module
    from fastapi.testclient import TestClient

    monkeypatch.setattr(
        app_module,
        "_evaluate_smart_backtest_validation",
        lambda root_session_id, min_pnl_delta_pct=-3.0, min_win_rate_delta_pct=-5.0, max_drawdown_delta_pct=15.0: {
            "ok": True,
            "overall": "pass",
            "root_session_id": root_session_id,
            "scenarios": [],
        },
    )

    client = TestClient(app_module.app)
    headers = {}
    if os.environ.get("DASHBOARD_API_KEY"):
        headers["X-API-Key"] = os.environ["DASHBOARD_API_KEY"]
    response = client.get("/tuning/candidates/spot-bull-v1/decision/root-abc", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["go_no_go"] == "GO"
    assert payload["candidate_label"] == "spot_bull_quality_v1"
