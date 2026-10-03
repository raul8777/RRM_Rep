import os
import asyncio

import pytest
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
    # Approximate; exact value depends on the default commission model used by mark_to_market_spot.
    for p, expected in zip(payload["positions"], [1.989, 1.977, 9.795]):
        assert p["unrealized_pnl"] == pytest.approx(expected, rel=0.05)


def test_open_positions_exposes_base_quantity_and_usdt_values(monkeypatch):
    position = Position(
        "NEARUSDT", "BUY", 13.9, 5.209, "2026-09-27T17:24:41Z",
        metadata={
            "trade_type": "spot",
            "lineage_id": "near-lineage-1",
            "source": "balance_sync",
            "entry_context_status": "unknown_reconciled",
            "cost_basis_status": "unknown",
            "entry_price_source": "balance_sync_mark_not_acquisition_cost",
            "balance_free_qty": 0.0,
            "balance_locked_qty": 13.9,
            "balance_total_qty": 13.9,
        },
    )
    monkeypatch.setattr(app_module, "position_manager", _PositionManager({"NEARUSDT": [position]}))
    monkeypatch.setattr(app_module, "_get_request_price", lambda cache, symbol: 5.3)
    monkeypatch.setattr(app_module, "_get_dashboard_context_reference", lambda: {})

    response = TestClient(app_module.app).get("/positions/open")

    assert response.status_code == 200
    item = response.json()["positions"][0]
    assert item["base_asset"] == "NEAR"
    assert item["base_quantity"] == pytest.approx(13.9)
    assert item["invested_usdt"] == pytest.approx(13.9 * 5.209)
    assert item["current_value_usdt"] == pytest.approx(13.9 * 5.3)
    assert item["cost_basis_status"] == "unknown"
    assert item["entry_price_source"] == "balance_sync_mark_not_acquisition_cost"
    assert item["lineage_id"] == "near-lineage-1"
    assert item["position_source"] == "balance_sync"
    assert item["balance_free_qty"] == 0.0
    assert item["balance_locked_qty"] == pytest.approx(13.9)
    assert item["balance_total_qty"] == pytest.approx(13.9)


def test_close_all_preserves_position_when_sell_fill_is_unconfirmed(monkeypatch):
    class PositionManager:
        def __init__(self):
            self.close_calls = []

        def get_all_positions(self):
            return {"NEARUSDT": [Position("NEARUSDT", "BUY", 13.9, 5.209, "2026-09-27T17:24:41Z")]}

        def close_all_positions_for_symbol(self, *args, **kwargs):
            self.close_calls.append((args, kwargs))

    class BinanceClient:
        def get_account_balance_details(self):
            return {"NEAR": {"free": 13.9, "locked": 0.0, "total": 13.9}}

        def get_symbol_filters(self, symbol):
            return {"step": 0.1, "min_qty": 0.1, "min_notional": 5.0}

        def create_market_order(self, **kwargs):
            return {"symbol": "NEARUSDT", "orderId": 1}

        def verify_and_fetch_fills(self, order, label):
            return None

    pm = PositionManager()
    monkeypatch.setattr(app_module, "position_manager", pm)
    monkeypatch.setattr(app_module, "binance_client", BinanceClient())
    monkeypatch.setattr(app_module, "_get_request_price", lambda cache, symbol: 5.3)

    result = asyncio.run(app_module.close_all_positions())

    assert result["closed"][0]["status"] == "fill_unconfirmed"
    assert result["closed"][0]["position_preserved"] is True
    assert pm.close_calls == []
