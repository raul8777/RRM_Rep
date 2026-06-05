import os

from fastapi.testclient import TestClient

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("TRADINGVIEW_WEBHOOK_SECRET", "supersecret")
os.environ.setdefault("BINANCE_USE_TESTNET", "true")

from app import app  # noqa: E402

client = TestClient(app)


def test_status_endpoint():
    response = client.get("/status")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_webhook_unauthorized():
    response = client.post("/webhook", json={"symbol": "BTCUSDT", "action": "buy"})
    assert response.status_code == 401


def test_webhook_suggest_mode():
    response = client.post(
        "/webhook",
        headers={"X-Webhook-Secret": "supersecret"},
        json={"symbol": "BTCUSDT", "action": "buy", "quantity": 0.001},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["symbol"] == "BTCUSDT"
    assert data["action"] == "BUY"
    assert "suggestion" in data
