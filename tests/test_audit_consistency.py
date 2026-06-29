import json
import os

from fastapi.testclient import TestClient

from audit_tools import check_signal_audit_consistency


class FakeDBClient:
    def __init__(self, signals, audits):
        self._signals = signals
        self._audits = audits

    def execute_query(self, sql, params=None):
        if "FROM market_data" in sql:
            return [{"data": s, "timestamp": "2026-06-29T00:00:00Z"} for s in self._signals]
        if "FROM trades_audit" in sql:
            return [
                {
                    "symbol": a["symbol"],
                    "trade_type": a.get("trade_type", "spot"),
                    "event": "signal_decision",
                    "detail": json.dumps(a["detail"]),
                    "binance_confirmed": True,
                    "created_at": "2026-06-29T00:00:00Z",
                }
                for a in self._audits
            ]
        return []


def test_audit_consistency_helper_matches_signal_id():
    signals = [
        {"symbol": "BTCUSDT", "signal_id": "sig-1", "decision": "executed", "decision_reason": "EJECUTADO"},
        {"symbol": "ETHUSDT", "signal_id": "sig-2", "decision": "rejected", "decision_reason": "SIN_SALDO"},
    ]
    audits = [
        {"symbol": "BTCUSDT", "detail": {"symbol": "BTCUSDT", "signal_id": "sig-1", "decision": "executed", "reason": "EJECUTADO"}},
        {"symbol": "ETHUSDT", "detail": {"symbol": "ETHUSDT", "signal_id": "sig-2", "decision": "rejected", "reason": "SIN_SALDO"}},
    ]
    result = check_signal_audit_consistency(FakeDBClient(signals, audits), limit=10)
    assert result["available"] is True
    assert result["signals_missing_audit"] == 0
    assert result["coverage_pct"] == 100.0


def test_audit_consistency_helper_detects_missing_audit():
    signals = [
        {"symbol": "BTCUSDT", "signal_id": "sig-1", "decision": "executed", "decision_reason": "EJECUTADO"},
    ]
    audits = []
    result = check_signal_audit_consistency(FakeDBClient(signals, audits), limit=10)
    assert result["signals_missing_audit"] == 1
    assert result["coverage_pct"] == 0.0


def test_audit_consistency_endpoint(monkeypatch):
    from app import app

    class _Comp:
        def __init__(self):
            self.db_client = FakeDBClient(
                signals=[
                    {"symbol": "BTCUSDT", "signal_id": "sig-1", "decision": "executed", "decision_reason": "EJECUTADO"}
                ],
                audits=[
                    {"symbol": "BTCUSDT", "detail": {"symbol": "BTCUSDT", "signal_id": "sig-1", "decision": "executed", "reason": "EJECUTADO"}}
                ],
            )

    monkeypatch.setattr("app.get_components", lambda: _Comp())
    client = TestClient(app)
    headers = {}
    if os.environ.get("DASHBOARD_API_KEY"):
        headers["X-API-Key"] = os.environ["DASHBOARD_API_KEY"]
    response = client.get("/audit/consistency", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["signals_missing_audit"] == 0
