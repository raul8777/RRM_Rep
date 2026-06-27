import pytest
from datetime import datetime, timezone, timedelta
from state_persistence import save_state, load_state


class FakeDBClient:
    def __init__(self):
        self.store = {}

    def execute_update(self, sql, params):
        # params[0] is key, params[1] is value json string
        key = params[0]
        value = params[1]
        self.store[key] = value

    def execute_query(self, sql, params=None):
        key = params[0] if params else None
        if key in self.store:
            return [{"value": self.store[key]}]
        return []


def test_save_and_load_state():
    db = FakeDBClient()
    now = datetime.now(timezone.utc)
    save_state(
        db,
        futures_sl_cooldown={"BTCUSDT": now},
        futures_mass_sl_cooldown_until=now + timedelta(hours=4),
        volspike_cooldown={"ETHUSDT": now},
        volspike_positions={"SOLUSDT", "ADAUSDT"},
        override_daily_count={"2026-06-27": 2},
    )
    loaded = load_state(db)
    assert loaded["futures_sl_cooldown"]["BTCUSDT"].replace(tzinfo=timezone.utc) == now
    assert loaded["futures_mass_sl_cooldown_until"].replace(tzinfo=timezone.utc) == now + timedelta(hours=4)
    assert loaded["volspike_cooldown"]["ETHUSDT"].replace(tzinfo=timezone.utc) == now
    assert set(loaded["volspike_positions"]) == {"SOLUSDT", "ADAUSDT"}
    assert loaded["override_daily_count"]["2026-06-27"] == 2


def test_load_state_empty():
    db = FakeDBClient()
    assert load_state(db) == {}
