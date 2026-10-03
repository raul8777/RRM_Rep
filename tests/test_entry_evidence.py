from datetime import datetime, timedelta, timezone

from entry_evidence import build_entry_evidence


class EvidenceDb:
    def __init__(self, ready_rows, closed_rows, candidate_rows):
        self.ready_rows = ready_rows
        self.closed_rows = closed_rows
        self.candidate_rows = candidate_rows

    def execute_query(self, sql, _params=None):
        if "FROM ready_candidates" in sql:
            return self.ready_rows
        if "FROM trades_closed" in sql:
            return self.closed_rows
        if "FROM entry_selection_candidates" in sql:
            return self.candidate_rows
        return []


def _snapshot():
    return {
        "expected_pnl_pct": 1.2,
        "success_probability": 0.7,
        "strength": 0.8,
        "rsi": 35,
        "rsi_4h": 45,
        "vol_spike": 1.5,
        "range_position": 0.2,
        "bb_position": 0.1,
        "atr": 0.02,
        "trend_4h": "FLAT",
    }


def test_evidence_counts_exact_link_and_grouped_partial_outcome():
    now = datetime(2026, 7, 23, tzinfo=timezone.utc)
    metadata = {
        "entry_regime": "RANGE_SCALP",
        "ready_candidate_id": 7,
        "peak_quality_profile": "A",
        "entry_order_id": "order-1",
        "entry_live_features": _snapshot(),
    }
    rows = [
        {"id": 1, "symbol": "BTCUSDT", "side": "BUY", "entry_time": now, "exit_time": now, "pnl": 2, "pnl_percentage": 1, "close_reason": "sso", "metadata": {**metadata, "partial_close": True}, "is_synthetic": False, "corrupt": False, "is_corrupted": False},
        {"id": 2, "symbol": "BTCUSDT", "side": "BUY", "entry_time": now, "exit_time": now, "pnl": -1, "pnl_percentage": -0.5, "close_reason": "take_profit", "metadata": metadata, "is_synthetic": False, "corrupt": False, "is_corrupted": False},
    ]
    db = EvidenceDb(
        [{"id": 7, "status": "opened", "regime_mode": "RANGE_SCALP", "profile_label": "A", "signal_meta": metadata, "opened_at": now, "opened_trade_id": None}],
        rows,
        [{"stage": "qubo_selected", "decision": "pending", "reason": None}],
    )

    result = build_entry_evidence(db, now=now)

    assert result["lineage"]["exact"] == 1
    assert result["outcomes"]["exact_only"]["mature_count"] == 1
    assert result["outcomes"]["exact_only"]["pnl_net_usdt"] == 1
    assert result["instrumentation"]["snapshot_coverage_pct"] == 100
    assert result["promotion"]["tier_a_deficit"] == 29


def test_evidence_counts_execution_preflight_blocks():
    now = datetime(2026, 7, 23, tzinfo=timezone.utc)
    db = EvidenceDb(
        [{"id": 7, "status": "validated", "regime_mode": "RANGE_SCALP", "profile_label": "B", "signal_meta": {}, "data": {"last_execution_gate": {"passed": False, "reason": "RANGE_BB"}}}],
        [],
        [],
    )

    result = build_entry_evidence(db, now=now)

    assert result["funnel"]["validated_blocked_by_execution_gate"] == 1
    assert result["funnel"]["execution_gate_reasons"] == {"RANGE_BB": 1}
    assert any(alert["code"] == "VALIDATED_PREFLIGHT_BLOCKED" for alert in result["alerts"])


def test_entry_evidence_endpoint_validates_filters(monkeypatch):
    import app as app_module
    from fastapi.testclient import TestClient

    db = EvidenceDb([], [], [])
    monkeypatch.setattr(app_module, "db_client", db)
    app_module.app.dependency_overrides[app_module._require_api_key] = lambda: None
    client = TestClient(app_module.app)

    invalid = client.get("/metrics/entry-evidence?profile=Z")
    assert invalid.status_code == 400
    valid = client.get("/metrics/entry-evidence?days=7&profile=A")
    assert valid.status_code == 200
    assert valid.json()["filters"]["profile"] == "A"


def test_shadow_readiness_endpoint_reports_component_states(monkeypatch):
    import types
    import app as app_module
    from fastapi.testclient import TestClient
    from config import Settings

    class ShadowDb(EvidenceDb):
        def execute_query(self, sql, params=None):
            if "lifecycle_runner_profit_floor_shadow" in sql:
                assert params == (5.5, 5.5, 5.5)
                return [{
                    "sso_closes": 12,
                    "would_defer": 4,
                    "truncated_profit_usdt": 6.25,
                    "winners_at_or_above_floor": 3,
                    "loss_exits_preserved": 5,
                    "last_at": "2026-08-15T00:00:00+00:00",
                }]
            if "FROM trades_audit" in sql and "lifecycle_structure_decision" in sql:
                return [{"total": 30, "available": 30, "actionable": 8}]
            if "FROM trades_audit" in sql:
                return [{"pool_events": 0, "entry_rejections": 0}]
            if "COUNT(*) AS total" in sql and "ready_candidates" in sql:
                return [{"total": 30, "timing": 30, "reversion_valid": 30, "schema_v1": 30}]
            return super().execute_query(sql, params)

    settings = Settings(_env_file=None)
    db = ShadowDb([], [], [])
    monkeypatch.setattr(app_module, "db_client", db)
    monkeypatch.setattr(app_module, "get_components", lambda: types.SimpleNamespace(settings=settings))
    monkeypatch.setattr(app_module, "get_hard_risk_shadow_metrics", lambda: {
        "name": "hard_risk_backstop",
        "mode": "shadow",
        "enabled": True,
        "spot_cycles": 12,
        "spot_positions": 18,
        "futures_cycles": 4,
        "futures_positions": 6,
        "breakeven_updates": 2,
        "stop_closes": 1,
        "last_at": "2026-08-06T00:00:00+00:00",
    })
    app_module.app.dependency_overrides[app_module._require_api_key] = lambda: None

    response = TestClient(app_module.app).get("/metrics/shadow-readiness")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    states = {item["id"]: item["state"] for item in payload["components"]}
    assert states["directional_v2"] == "RETIRED"
    assert states["ug_loss_v2"] == "ACTIVE"
    assert states["lifecycle_structure"] == "ACTIVE"
    assert states["staging_runner_profit_floor"] == "COLLECTING"
    assert states["range_reversion"] == "COLLECTING"
    assert states["position_telemetry_watcher"] == "ACTIVE"
    assert states["hard_risk_backstop"] == "ACTIVE"
    runner = next(item for item in payload["components"] if item["id"] == "staging_runner_profit_floor")
    assert runner["mode"] == "shadow"
    assert "challenger STG=$5.50" in runner["summary"]
    assert "sizing=$150, SSO-3=off, PRS futures=off" in runner["summary"]
    assert "4/12 cierres SSO would-defer" in runner["summary"]
    assert "$6.25 de ganancia truncada" in runner["summary"]


def test_evidence_does_not_promote_legacy_or_missing_snapshot():
    now = datetime(2026, 7, 23, tzinfo=timezone.utc)
    db = EvidenceDb(
        [],
        [{"id": 1, "symbol": "ETHUSDT", "side": "BUY", "entry_time": now, "exit_time": now, "pnl": 10, "pnl_percentage": 1, "close_reason": "take_profit", "metadata": {"entry_regime": "RANGE_SCALP"}, "is_synthetic": False, "corrupt": False, "is_corrupted": False}],
        [],
    )

    result = build_entry_evidence(db, include_legacy=True, now=now)

    assert result["lineage"]["legacy"] == 1
    assert result["outcomes"]["selected"]["mature_count"] == 1
    assert result["outcomes"]["exact_only"]["mature_count"] == 0
    assert result["promotion"]["overall_status"] == "blocked"
    assert result["alerts"][0]["code"] == "SHADOW_SNAPSHOT_ABSENT"
