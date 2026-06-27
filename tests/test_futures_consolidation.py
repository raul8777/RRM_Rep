import pytest
import json
from datetime import datetime, timezone
from futures_client import FuturesClient, FuturesPosition


class FakeDBClient:
    def __init__(self, rows):
        self.rows = rows
        self.deleted_ids = []
        self.inserted = []

    def execute_query(self, sql, params=None):
        if "SELECT id, symbol" in sql:
            return self.rows
        return []

    def execute_update(self, sql, params):
        if "DELETE" in sql:
            if isinstance(params, (list, tuple)) and len(params) > 1:
                self.deleted_ids.extend(params)
            else:
                self.deleted_ids.append(params[0] if isinstance(params, (list, tuple)) else params)
        elif "INSERT" in sql:
            self.inserted.append(params)


class FakeFuturesClient(FuturesClient):
    def __init__(self, positions, rows):
        self._open_positions = positions
        self.db_client = FakeDBClient(rows)
        self.api_key = ""
        self.api_secret = ""

    def _pos_key(self, symbol, side):
        return f"{symbol}_{side}"


def test_consolidate_merges_stale_escalonamiento_rows():
    rows = [
        {
            "id": 1, "symbol": "TAOUSDT", "side": "SELL", "quantity": 1.15, "price": 212.82,
            "stop_loss": 216.39, "take_profit": 196.48,
            "metadata": json.dumps({"trade_type": "futures", "escalonamiento_index": 1}),
            "pnl_prediction": None, "indicators_at_entry": None,
            "entry_time": datetime.now(timezone.utc),
        },
        {
            "id": 2, "symbol": "TAOUSDT", "side": "SELL", "quantity": 1.089, "price": 209.42,
            "stop_loss": 216.39, "take_profit": 196.48,
            "metadata": json.dumps({"trade_type": "futures", "escalonamiento_index": 2, "es_escalonamiento": True}),
            "pnl_prediction": None, "indicators_at_entry": None,
            "entry_time": datetime.now(timezone.utc),
        },
    ]
    positions = {
        "TAOUSDT_SHORT": FuturesPosition(
            symbol="TAOUSDT", side="SHORT", qty=1.089,
            entry_price=209.44, sl_price=216.39, tp_price=196.48,
            order_id="sync", sl_order_id=None, tp_order_id=None,
        )
    }
    fc = FakeFuturesClient(positions, rows)
    fc._consolidate_futures_open_records()
    assert len(fc.db_client.inserted) == 1
    inserted = fc.db_client.inserted[0]
    assert inserted[0] == "TAOUSDT"
    assert inserted[2] == 1.089  # qty
    assert inserted[3] == 209.44  # price from Binance
    assert set(fc.db_client.deleted_ids) == {1, 2}
