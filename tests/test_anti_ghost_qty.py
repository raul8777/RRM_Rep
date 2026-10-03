"""Tests for anti-ghost quantity helpers in trade_utils."""
import sys
import math

sys.path.insert(0, sys.path[0] + "/..")

from trade_utils import align_quantity_to_step, get_binance_sell_qty, get_min_notional_delta


class _FakeClient:
    def __init__(self, balances, filters, balance_details=None, balance_error=None):
        self._balances = balances
        self._filters = filters
        self._balance_details = balance_details
        self._balance_error = balance_error

    def get_account_balance(self):
        if self._balance_error:
            raise self._balance_error
        return self._balances

    def get_account_balance_details(self):
        if self._balance_error:
            raise self._balance_error
        if self._balance_details is None:
            raise AttributeError("details unavailable")
        return self._balance_details

    def get_symbol_filters(self, symbol):
        return self._filters.get(symbol, {"step": 0.0001, "min_qty": 0.0001, "min_notional": 5.0})

    def get_symbol_price(self, symbol):
        return 1.0


def test_get_binance_sell_qty_ok():
    client = _FakeClient({"PEPE": 1000.0}, {"PEPEUSDT": {"step": 1.0, "min_qty": 1.0, "min_notional": 5.0}})
    res = get_binance_sell_qty(client, "PEPEUSDT", 1000.0, 0.01)
    assert res["reason"] == "ok"
    assert res["qty_to_sell"] == 1000.0
    assert not res["is_ghost"]


def test_get_binance_sell_qty_ghost():
    client = _FakeClient({"PEPE": 0.1}, {"PEPEUSDT": {"step": 1.0, "min_qty": 1.0, "min_notional": 5.0}})
    res = get_binance_sell_qty(client, "PEPEUSDT", 1000.0, 0.01)
    assert res["is_ghost"] is True
    assert res["ratio"] < 0.001


def test_get_binance_sell_qty_capped():
    client = _FakeClient({"PEPE": 500.0}, {"PEPEUSDT": {"step": 1.0, "min_qty": 1.0, "min_notional": 5.0}})
    res = get_binance_sell_qty(client, "PEPEUSDT", 1000.0, 0.01)
    assert res["reason"] == "balance_capped"
    assert res["qty_to_sell"] == 500.0


def test_get_binance_sell_qty_dust():
    client = _FakeClient({"PEPE": 100.0}, {"PEPEUSDT": {"step": 1.0, "min_qty": 1.0, "min_notional": 5.0}})
    res = get_binance_sell_qty(client, "PEPEUSDT", 1000.0, 0.00001)
    assert res["is_dust"] is True
    assert res["top_up_qty"] is not None


def test_locked_balance_is_not_a_ghost_but_is_not_sellable():
    client = _FakeClient(
        {"NEAR": 0.0},
        {"NEARUSDT": {"step": 0.1, "min_qty": 0.1, "min_notional": 5.0}},
        balance_details={"NEAR": {"free": 0.0, "locked": 13.9, "total": 13.9}},
    )

    result = get_binance_sell_qty(client, "NEARUSDT", 13.9, 5.2)

    assert result["balance_status"] == "ok"
    assert result["is_ghost"] is False
    assert result["balance_qty"] == 13.9
    assert result["free_balance_qty"] == 0.0
    assert result["locked_balance_qty"] == 13.9
    assert result["qty_to_sell"] == 0.0
    assert result["reason"] == "balance_locked"


def test_balance_api_error_is_unknown_not_ghost():
    client = _FakeClient(
        {},
        {"NEARUSDT": {"step": 0.1, "min_qty": 0.1, "min_notional": 5.0}},
        balance_error=RuntimeError("temporary Binance timeout"),
    )

    result = get_binance_sell_qty(client, "NEARUSDT", 13.9, 5.2)

    assert result["balance_status"] == "api_error"
    assert result["is_ghost"] is False
    assert result["balance_qty"] is None
    assert result["qty_to_sell"] == 0.0
    assert result["reason"] == "balance_unavailable"


def test_get_min_notional_delta_needed():
    client = _FakeClient({"PEPE": 100.0}, {"PEPEUSDT": {"step": 1.0, "min_qty": 1.0, "min_notional": 5.0}})
    delta = get_min_notional_delta(client, "PEPEUSDT", 0.00001, target_notional_usdt=5.0)
    assert delta["needed"] is True
    assert delta["buy_qty"] > 0
    assert delta["sell_qty"] >= delta["buy_qty"] + 100.0


def test_align_quantity_to_step_truncate():
    assert align_quantity_to_step(123.456, 1.0) == 123.0
    assert align_quantity_to_step(123.456, 10.0) == 120.0
