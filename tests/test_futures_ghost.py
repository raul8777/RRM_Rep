"""Tests for futures ghost-prevention verification logic."""
import sys
sys.path.insert(0, sys.path[0] + "/..")

from datetime import datetime, timezone
from unittest.mock import ANY, MagicMock
from futures_client import FuturesClient


def test_verify_entry_filled_false_when_no_position():
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=True)
    # In dry_run, verification returns True
    assert fc._verify_entry_filled("BTCUSDT", "LONG") is True


def test_verify_position_closed_false_when_position_exists():
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    fc._get = MagicMock(return_value=[{"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "0.01"}])
    assert fc._verify_position_closed("BTCUSDT", "LONG") is False
    assert fc._get_position_qty_real("BTCUSDT", "LONG") == 0.01


def test_verify_position_closed_true_when_position_zero():
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    fc._get = MagicMock(return_value=[{"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "0.0"}])
    assert fc._verify_position_closed("BTCUSDT", "LONG") is True


def test_entry_filled_true_when_position_exists():
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    fc._get = MagicMock(return_value=[{"symbol": "BTCUSDT", "positionSide": "SHORT", "positionAmt": "-0.05"}])
    assert fc._verify_entry_filled("BTCUSDT", "SHORT") is True


def test_entry_filled_false_when_api_error():
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    fc._get = MagicMock(side_effect=Exception("API down"))
    # Should NOT assume filled on API error
    assert fc._verify_entry_filled("BTCUSDT", "LONG") is False


def test_external_close_reconciliation_aggregates_partial_fills():
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    closing_fills = [
        {"time": 2000, "positionSide": "SHORT", "qty": "4", "price": "12", "realizedPnl": "-4", "commission": "0.1"},
        {"time": 2000, "positionSide": "SHORT", "qty": "6", "price": "12", "realizedPnl": "-6", "commission": "0.2"},
        {"time": 1000, "positionSide": "SHORT", "qty": "10", "price": "10", "realizedPnl": "0", "commission": "0.3"},
    ]
    fc._get = MagicMock(side_effect=[closing_fills, []])

    result = fc._get_realized_pnl_from_trades("BTCUSDT", "SHORT")

    assert result["exit_price"] == 12.0
    assert result["realized_pnl"] == -10.0
    assert result["commission"] == 0.6
    assert result["pnl"] == -10.6
    assert result["reason"] == "stop_loss_binance"


def test_external_close_reconciliation_groups_partial_fills_by_order_id():
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    closing_fills = [
        {"time": 2001, "orderId": "close-1", "positionSide": "SHORT", "qty": "4", "price": "12", "realizedPnl": "-4", "commission": "0.1"},
        {"time": 2002, "orderId": "close-1", "positionSide": "SHORT", "qty": "6", "price": "12", "realizedPnl": "-6", "commission": "0.2"},
        {"time": 1000, "orderId": "open-1", "positionSide": "SHORT", "qty": "10", "price": "10", "realizedPnl": "0", "commission": "0.3"},
    ]
    fc._get = MagicMock(side_effect=[closing_fills, []])

    result = fc._get_realized_pnl_from_trades("BTCUSDT", "SHORT")

    assert result["exit_price"] == 12.0
    assert result["realized_pnl"] == -10.0
    assert result["commission"] == 0.6
    assert result["pnl"] == -10.6


def test_external_close_reconciliation_converts_bnb_commission_to_usdt():
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    fills = [
        {"time": 2000, "orderId": "close-1", "positionSide": "SHORT", "qty": "10", "price": "12", "realizedPnl": "10", "commission": "0.01", "commissionAsset": "BNB"},
        {"time": 1000, "orderId": "open-1", "positionSide": "SHORT", "qty": "10", "price": "10", "realizedPnl": "0", "commission": "0.01", "commissionAsset": "BNB"},
    ]
    fc._get = MagicMock(side_effect=[fills, []])
    fc.get_price = MagicMock(return_value=500.0)

    result = fc._get_realized_pnl_from_trades("BTCUSDT", "SHORT")

    assert result["commission"] == 10.0
    assert result["commission_by_asset"] == {"BNB": 0.02}
    assert result["pnl"] == 0.0


def _make_synced_client(ghost_row):
    """FuturesClient with no live Binance positions and a single stale
    trades_open row, ready to exercise the FIX-S4 ghost cleanup block in
    _sync_open_positions()."""
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    fc.get_positions = MagicMock(return_value=[])
    fc._get = MagicMock(return_value=[])
    fc._consolidate_futures_open_records = MagicMock()
    fc.db_client = MagicMock()
    fc.db_client.execute_query = MagicMock(return_value=[ghost_row])
    fc.db_client.execute_update = MagicMock(return_value=1)
    return fc


def test_ghost_cleanup_reconciles_real_close_before_deleting():
    """s105d: a stale trades_open row for a position Binance already closed
    must be reconciled into trades_closed (real exit price/PnL from Binance)
    BEFORE being deleted — previously the row (and its real PnL, win or loss)
    was silently discarded."""
    ghost_row = {
        "id": 1395,
        "symbol": "ALLOUSDT",
        "side": "SELL",
        "quantity": 7034.0,
        "price": 0.28432,
        "entry_time": datetime(2026, 7, 31, 1, 26, 11, tzinfo=timezone.utc),
        "metadata": {"trade_type": "futures"},
        "entry_commission": 0.85,
        "pnl_prediction": None,
        "indicators_at_entry": None,
    }
    fc = _make_synced_client(ghost_row)
    fc._get_realized_pnl_from_trades = MagicMock(return_value={
        "exit_price": 0.2732374011941997,
        "pnl": 74.10997449679999,
        "realized_pnl": 77.64919972,
        "commission": 3.5392252232,
        "reason": "manual_close",
        "pnl_accounting_mode": "binance_user_trade_verified",
    })

    fc._sync_open_positions()

    insert_call = next(
        c for c in fc.db_client.execute_update.call_args_list
        if "INSERT INTO trades_closed" in c.args[0]
    )
    params = insert_call.args[1]
    assert params[0] == "ALLOUSDT"
    assert params[1] == "SELL"
    assert params[3] == 0.28432  # entry_price
    assert params[4] == 0.2732374011941997  # exit_price (real, from Binance)
    assert round(params[5], 2) == 74.11  # pnl (net, already includes commission)
    assert params[7] == "manual_close"  # close_reason

    delete_call = next(
        c for c in fc.db_client.execute_update.call_args_list
        if "DELETE FROM trades_open" in c.args[0]
    )
    assert delete_call.args[1] == (1395,)


def test_ghost_cleanup_deletes_without_reconciling_when_no_binance_history():
    """When Binance has no trade history to reconcile from (e.g. a truly
    synthetic/duplicate row), fall back to the previous delete-only behavior
    instead of inserting a bogus trades_closed row."""
    ghost_row = {
        "id": 1390,
        "symbol": "LAUSDT",
        "side": "SELL",
        "quantity": 100.0,
        "price": 0.05,
        "entry_time": datetime(2026, 7, 31, 0, 0, 0, tzinfo=timezone.utc),
        "metadata": {"trade_type": "futures"},
        "entry_commission": 0.1,
        "pnl_prediction": None,
        "indicators_at_entry": None,
    }
    fc = _make_synced_client(ghost_row)
    fc._get_realized_pnl_from_trades = MagicMock(return_value={
        "exit_price": 0, "pnl": 0, "reason": "binance_safety_close",
    })

    fc._sync_open_positions()

    assert not any(
        "INSERT INTO trades_closed" in c.args[0]
        for c in fc.db_client.execute_update.call_args_list
    )
    delete_call = next(
        c for c in fc.db_client.execute_update.call_args_list
        if "DELETE FROM trades_open" in c.args[0]
    )
    assert delete_call.args[1] == (1390,)


def test_align_qty_preserves_step_sized_float_residual():
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=True)

    assert fc._align_qty(0.09999999999990905, 0.1) == 0.1
    assert fc._align_qty(0.099, 0.1) == 0.0
