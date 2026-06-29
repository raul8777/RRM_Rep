import os

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
