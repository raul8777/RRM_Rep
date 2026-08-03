import pytest
import json
from datetime import datetime, timezone
from futures_client import FuturesClient, FuturesPosition


class FakeDBClient:
    def __init__(self, rows, existing_order_ids=None):
        self.rows = rows
        self.deleted_ids = []
        self.inserted = []
        self.existing_order_ids = existing_order_ids or {}

    def execute_query(self, sql, params=None):
        if "SELECT id, symbol" in sql:
            return self.rows
        if "metadata->>'order_id'" in sql and params:
            _order_id = params[0]
            if _order_id in self.existing_order_ids:
                return [{"id": self.existing_order_ids[_order_id]}]
            return []
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
    def __init__(self, positions, rows, existing_order_ids=None):
        self._open_positions = positions
        self.db_client = FakeDBClient(rows, existing_order_ids=existing_order_ids)
        self.api_key = ""
        self.api_secret = ""
        self._cached_settings = None

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


def test_save_futures_open_skips_when_order_id_already_persisted():
    """s109: _save_futures_open must be idempotent on order_id — this is what
    prevents `_consolidate_futures_open_records`'s "ensure DB record" step
    from creating a genuine duplicate trades_open row (same order_id, same
    entry_time) when its existence check races with a very recent insert for
    the same real order (the BABYUSDT incident)."""
    pos = FuturesPosition(
        symbol="BABYUSDT", side="SHORT", qty=168918.0,
        entry_price=0.01184, sl_price=0.012, tp_price=0.0113,
        order_id="1509418114", sl_order_id=None, tp_order_id=None,
    )
    fc = FakeFuturesClient({}, [], existing_order_ids={"1509418114": 1380})
    fc._save_futures_open(pos)
    assert fc.db_client.inserted == []


def test_save_futures_open_inserts_when_order_id_is_new():
    pos = FuturesPosition(
        symbol="BABYUSDT", side="SHORT", qty=168918.0,
        entry_price=0.01184, sl_price=0.012, tp_price=0.0113,
        order_id="1509481502", sl_order_id=None, tp_order_id=None,
    )
    fc = FakeFuturesClient({}, [], existing_order_ids={"1509418114": 1380})
    fc._save_futures_open(pos)
    assert len(fc.db_client.inserted) >= 1


def test_save_futures_open_does_not_dedup_sync_placeholder_order_ids():
    """SYNC_/DRY_/RECONCILE_ order_ids are synthetic placeholders reused
    across many real trades over time for the same symbol — they must never
    be treated as a real order_id duplicate guard."""
    pos = FuturesPosition(
        symbol="BABYUSDT", side="SHORT", qty=168918.0,
        entry_price=0.01184, sl_price=0.012, tp_price=0.0113,
        order_id="SYNC_BABYUSDT_SHORT", sl_order_id=None, tp_order_id=None,
    )
    fc = FakeFuturesClient({}, [], existing_order_ids={"SYNC_BABYUSDT_SHORT": 999})
    fc._save_futures_open(pos)
    assert len(fc.db_client.inserted) >= 1
