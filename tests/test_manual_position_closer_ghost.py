from types import SimpleNamespace
from datetime import datetime, timezone

from manual_position_closer import close_ghost_db


class _PositionManager:
    def __init__(self):
        self.position = SimpleNamespace(quantity=13.9, metadata={})
        self.close_calls = []
        self.audit_events = []

    def get_all_positions(self):
        return {"NEARUSDT": [self.position]}

    def close_all_positions_for_symbol(self, **kwargs):
        self.close_calls.append(kwargs)
        return [self.position]

    def _audit(self, symbol, event, detail, binance_confirmed=False):
        self.audit_events.append((symbol, event, detail, binance_confirmed))


class _Client:
    dry_run = False

    def __init__(self, balances=None, error=None, trades=None):
        self.balances = balances or {}
        self.error = error
        self.trades = trades or []
        self.reads = 0

    def get_account_balance_details(self):
        self.reads += 1
        if self.error:
            raise self.error
        return self.balances

    def get_symbol_price(self, symbol, use_cache=False):
        return 5.2

    def get_my_trades(self, symbol, limit=50):
        return self.trades[:limit]



def _components(client):
    return SimpleNamespace(
        position_manager=_PositionManager(),
        binance_client=client,
        db_client=None,
        settings=SimpleNamespace(binance_commission_rate=0.001),
    )


def test_manual_ghost_close_refuses_when_asset_is_still_on_exchange():
    client = _Client({"NEAR": {"free": 0.0, "locked": 13.9, "total": 13.9}})
    components = _components(client)

    result = close_ghost_db("NEARUSDT", components)

    assert result["ok"] is False
    assert result["reason"] == "exchange_balance_present_keep_position"
    assert components.position_manager.close_calls == []
    assert client.reads == 2


def test_manual_ghost_close_refuses_when_balance_api_fails():
    client = _Client(error=RuntimeError("temporary timeout"))
    components = _components(client)

    result = close_ghost_db("NEARUSDT", components)

    assert result["ok"] is False
    assert result["reason"] == "balance_unavailable_keep_position"
    assert components.position_manager.close_calls == []


def test_manual_ghost_close_requires_two_valid_zero_snapshots():
    client = _Client({}, trades=[{
        "time": int(datetime.now(timezone.utc).timestamp() * 1000),
        "isBuyer": False,
        "qty": "13.9",
    }])
    components = _components(client)

    result = close_ghost_db("NEARUSDT", components)

    assert result["ok"] is True
    assert result["entries_closed"] == 1
    assert client.reads == 2
    assert len(components.position_manager.close_calls) == 1
