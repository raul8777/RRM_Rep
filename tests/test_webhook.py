import os
from unittest.mock import patch

from fastapi.testclient import TestClient

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("TRADINGVIEW_WEBHOOK_SECRET", "supersecret")
os.environ.setdefault("BINANCE_USE_TESTNET", "true")

_DASHBOARD_API_KEY = os.environ.get("DASHBOARD_API_KEY", "")

from app import app  # noqa: E402
import app as _app_module

client = TestClient(app)


def test_status_endpoint():
    response = client.get("/status")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_webhook_unauthorized():
    response = client.post("/webhook", json={"symbol": "BTCUSDT", "action": "buy"})
    assert response.status_code == 401


def test_webhook_suggest_mode():
    _headers = {"X-Webhook-Secret": "supersecret"}
    if _DASHBOARD_API_KEY:
        _headers["X-API-Key"] = _DASHBOARD_API_KEY
    with patch.object(_app_module.settings, "tradingview_webhook_secret", "supersecret"):
        response = client.post(
            "/webhook",
            headers=_headers,
            json={"symbol": "BTCUSDT", "action": "buy", "quantity": 0.001},
        )
    assert response.status_code == 200
    data = response.json()
    assert data.get("signal_registered") is True
    signal = data.get("signal", {})
    assert signal.get("symbol") == "BTCUSDT"
    assert signal.get("action", "").upper() == "BUY"
