import os

from fastapi.testclient import TestClient

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")

import app as app_module
from position_manager import Position


class _PositionManager:
    def __init__(self, positions):
        self._positions = positions

    def get_all_positions(self):
        return self._positions


def test_open_positions_detail_returns_each_sub_entry(monkeypatch):
    positions = {
        "BTCUSDT": [
            Position("BTCUSDT", "BUY", 0.1, 100.0, "2026-07-20T00:00:00Z"),
            Position("BTCUSDT", "BUY", 0.2, 110.0, "2026-07-20T01:00:00Z"),
        ],
        "ETHUSDT": [
            Position("ETHUSDT", "BUY", 1.0, 200.0, "2026-07-20T02:00:00Z"),
        ],
    }
    monkeypatch.setattr(app_module, "position_manager", _PositionManager(positions))
    monkeypatch.setattr(app_module, "_get_request_price", lambda cache, symbol: {"BTCUSDT": 120.0, "ETHUSDT": 210.0}[symbol])

    response = TestClient(app_module.app).get("/positions/open/detail")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 3
    assert [(p["symbol"], p["entry_number"]) for p in payload["positions"]] == [
        ("BTCUSDT", 1),
        ("BTCUSDT", 2),
        ("ETHUSDT", 1),
    ]
    assert [p["unrealized_pnl"] for p in payload["positions"]] == [1.989, 1.977, 9.795]
