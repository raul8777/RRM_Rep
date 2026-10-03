from unittest.mock import MagicMock

import pytest

from spot_margin_client import SpotMarginClient


def _adapter():
    spot = MagicMock()
    spot.client = MagicMock()
    spot.get_symbol_price.return_value = 100.0
    spot.get_symbol_filters.return_value = {"step": 0.001}
    spot._format_amount.side_effect = lambda value: str(value)
    spot.client.get_isolated_margin_account.return_value = {
        "assets": [{"symbol": "BTCUSDT", "baseAsset": {"borrowEnabled": True}}]
    }
    spot.client.get_max_margin_loan.return_value = {"amount": "2.0"}
    return SpotMarginClient("key", "secret", spot_client=spot), spot


def test_real_margin_adapter_rejects_testnet_before_network_call():
    with pytest.raises(RuntimeError, match="does not expose /sapi Margin"):
        SpotMarginClient("key", "secret", testnet=True)


def test_preflight_rejects_unavailable_isolated_pair():
    adapter, spot = _adapter()
    spot.client.get_isolated_margin_account.return_value = {"assets": []}

    with pytest.raises(RuntimeError, match="not enabled"):
        adapter.preflight_short("BTCUSDT")


def test_open_short_borrows_sells_and_places_buy_oco():
    adapter, spot = _adapter()
    spot.client.create_margin_loan.return_value = {"tranId": 11}
    spot.client.create_margin_order.return_value = {
        "status": "FILLED",
        "orderId": 22,
        "executedQty": "1.0",
        "cummulativeQuoteQty": "100.0",
    }
    spot.client.create_margin_oco_order.return_value = {"orderListId": 33}

    position = adapter.open_short(
        "BTCUSDT",
        100.0,
        stop_loss_pct=2.0,
        take_profit_pct=4.0,
    )

    assert position.entry_price == pytest.approx(100.0)
    assert position.stop_price == pytest.approx(102.0)
    assert position.target_price == pytest.approx(96.0)
    assert spot.client.create_margin_loan.call_args.kwargs["isIsolated"] == "TRUE"
    assert spot.client.create_margin_order.call_args.kwargs["side"] == "SELL"
    assert spot.client.create_margin_oco_order.call_args.kwargs["side"] == "BUY"


def test_entry_failure_repays_borrowed_asset():
    adapter, spot = _adapter()
    spot.client.create_margin_loan.return_value = {"tranId": 11}
    spot.client.create_margin_order.side_effect = RuntimeError("sell failed")

    with pytest.raises(RuntimeError, match="sell failed"):
        adapter.open_short("BTCUSDT", 100.0, stop_loss_pct=2.0, take_profit_pct=4.0)

    spot.client.repay_margin_loan.assert_called_once()


def test_protection_failure_covers_then_repays():
    adapter, spot = _adapter()
    spot.client.create_margin_loan.return_value = {"tranId": 11}
    spot.client.create_margin_order.return_value = {
        "status": "FILLED",
        "orderId": 22,
        "executedQty": "1.0",
        "cummulativeQuoteQty": "100.0",
    }
    spot.client.create_margin_oco_order.side_effect = RuntimeError("oco failed")

    with pytest.raises(RuntimeError, match="oco failed"):
        adapter.open_short("BTCUSDT", 100.0, stop_loss_pct=2.0, take_profit_pct=4.0)

    assert spot.client.create_margin_order.call_count == 2
    assert spot.client.create_margin_order.call_args.kwargs["side"] == "BUY"
    spot.client.repay_margin_loan.assert_called_once()


def test_close_short_covers_total_debt_and_repays_interest():
    adapter, spot = _adapter()
    spot.client.create_margin_loan.return_value = {"tranId": 11}
    spot.client.create_margin_order.return_value = {
        "status": "FILLED",
        "orderId": 22,
        "executedQty": "1.0",
        "cummulativeQuoteQty": "100.0",
        "fills": [{"price": "100", "commission": "0.1", "commissionAsset": "USDT"}],
    }
    spot.client.create_margin_oco_order.return_value = {"orderListId": 33}
    position = adapter.open_short("BTCUSDT", 100.0, stop_loss_pct=2.0, take_profit_pct=4.0)
    spot.client.get_isolated_margin_account.return_value = {
        "assets": [{
            "symbol": "BTCUSDT",
            "baseAsset": {"borrowed": "1.0", "interest": "0.001", "free": "0"},
        }]
    }
    spot.client.create_margin_order.return_value = {
        "status": "FILLED",
        "orderId": 44,
        "executedQty": "1.001",
        "cummulativeQuoteQty": "95.095",
        "fills": [{"price": "95", "commission": "0.095", "commissionAsset": "USDT"}],
    }
    spot.client.repay_margin_loan.return_value = {"tranId": 55}

    result = adapter.close_short(position)

    assert spot.client.cancel_margin_oco_order.call_args.kwargs["orderListId"] == "33"
    assert spot.client.create_margin_order.call_args.kwargs["quantity"] == "1.001"
    assert spot.client.repay_margin_loan.call_args.kwargs["amount"] == "1.001"
    assert result["interest_base"] == pytest.approx(0.001)
    assert result["net_pnl_usdt"] == pytest.approx(4.71)
    assert result["repay_transaction_id"] == "55"


def test_reconcile_debt_exposes_restart_state():
    adapter, spot = _adapter()
    spot.client.get_isolated_margin_account.return_value = {
        "assets": [{
            "symbol": "BTCUSDT",
            "baseAsset": {"borrowed": "0.5", "interest": "0.002", "free": "0.1"},
        }]
    }

    debt = adapter.reconcile_debt("BTCUSDT")

    assert debt["total_debt"] == pytest.approx(0.502)
    assert debt["asset"] == "BTC"


def test_open_short_persists_identity_and_thesis_after_protection():
    adapter, spot = _adapter()
    adapter.db_client = MagicMock()
    spot.client.create_margin_loan.return_value = {"tranId": 11}
    spot.client.create_margin_order.return_value = {
        "status": "FILLED",
        "orderId": 22,
        "executedQty": "1.0",
        "cummulativeQuoteQty": "100.0",
    }
    spot.client.create_margin_oco_order.return_value = {"orderListId": 33}

    position = adapter.open_short(
        "BTCUSDT",
        100.0,
        stop_loss_pct=2.0,
        take_profit_pct=4.0,
        metadata={"thesis_snapshot_v1": {"side": "SHORT", "stop_price": 102.0, "target_price": 96.0}},
    )

    assert position.position_id
    assert position.thesis_id
    assert position.close_group_id
    assert position.metadata["thesis_snapshot_v1"]["side"] == "SHORT"
    params = adapter.db_client.execute_update.call_args.args[1]
    persisted = params[5]
    assert "thesis_snapshot_v1" in persisted


def test_close_short_persists_same_identity_after_repay():
    adapter, spot = _adapter()
    adapter.db_client = MagicMock()
    spot.client.create_margin_loan.return_value = {"tranId": 11}
    spot.client.create_margin_order.return_value = {
        "status": "FILLED",
        "orderId": 22,
        "executedQty": "1.0",
        "cummulativeQuoteQty": "100.0",
    }
    spot.client.create_margin_oco_order.return_value = {"orderListId": 33}
    position = adapter.open_short("BTCUSDT", 100.0, stop_loss_pct=2.0, take_profit_pct=4.0)
    adapter.db_client.reset_mock()
    spot.client.get_isolated_margin_account.return_value = {
        "assets": [{
            "symbol": "BTCUSDT",
            "baseAsset": {"borrowed": "1.0", "interest": "0", "free": "0"},
        }]
    }
    spot.client.create_margin_order.return_value = {
        "status": "FILLED",
        "orderId": 44,
        "executedQty": "1.0",
        "cummulativeQuoteQty": "95.0",
    }
    spot.client.repay_margin_loan.return_value = {"tranId": 55}

    adapter.close_short(position, reason="take_profit")

    insert_params = adapter.db_client.execute_update.call_args.args[1]
    assert insert_params[0] == position.position_id
    assert insert_params[-1] == position.position_id
    assert f"{position.position_id}:close:0" in insert_params
    assert adapter.db_client.execute_update.call_count == 1


def test_recover_open_position_leaves_active_oco_untouched():
    adapter, spot = _adapter()
    adapter.db_client = MagicMock()
    adapter.db_client.execute_query.return_value = [{
        "symbol": "BTCUSDT",
        "quantity": 1.0,
        "price": 100.0,
        "stop_loss": 102.0,
        "take_profit": 96.0,
        "metadata": {"trade_type": "spot_margin", "entry_quote_received": 100.0},
        "entry_commission": 0.1,
        "position_id": "position-1",
        "thesis_id": "thesis-1",
        "close_group_id": "close-1",
        "entry_order_id": "22",
        "protection_order_list_id": "33",
        "margin_loan_transaction_id": "11",
    }]
    spot.client.get_margin_oco_order.return_value = {
        "listOrderStatus": "EXECUTING",
        "orderReports": [{"side": "BUY", "status": "NEW", "orderId": 44}],
    }
    spot.client.get_isolated_margin_account.return_value = {
        "assets": [{
            "symbol": "BTCUSDT",
            "baseAsset": {"borrowed": "1.0", "interest": "0.001", "free": "0"},
        }]
    }

    recovered = adapter.recover_open_positions()

    assert recovered[0]["status"] == "active"
    spot.client.create_margin_order.assert_not_called()
    spot.client.repay_margin_loan.assert_not_called()


def test_recover_filled_oco_covers_interest_repays_and_persists_once():
    adapter, spot = _adapter()
    adapter.db_client = MagicMock()
    adapter.db_client.execute_query.return_value = [{
        "symbol": "BTCUSDT",
        "quantity": 1.0,
        "price": 100.0,
        "stop_loss": 102.0,
        "take_profit": 96.0,
        "metadata": {"trade_type": "spot_margin", "entry_quote_received": 100.0},
        "entry_commission": 0.1,
        "position_id": "position-1",
        "thesis_id": "thesis-1",
        "close_group_id": "close-1",
        "entry_order_id": "22",
        "protection_order_list_id": "33",
        "margin_loan_transaction_id": "11",
    }]
    spot.client.get_margin_oco_order.return_value = {
        "listOrderStatus": "ALL_DONE",
        "orderReports": [{"side": "BUY", "status": "FILLED", "orderId": 44}],
    }
    spot.client.get_margin_order.return_value = {
        "status": "FILLED",
        "orderId": 44,
        "executedQty": "1.0",
        "cummulativeQuoteQty": "95.0",
    }
    spot.client.get_margin_trades.return_value = []
    spot.client.get_isolated_margin_account.side_effect = [
        {"assets": [{"symbol": "BTCUSDT", "baseAsset": {
            "borrowed": "1.0", "interest": "0.001", "free": "1.0",
        }}]},
        {"assets": [{"symbol": "BTCUSDT", "baseAsset": {
            "borrowed": "1.0", "interest": "0.001", "free": "1.101",
        }}]},
    ]
    spot.client.create_margin_order.return_value = {
        "status": "FILLED",
        "orderId": 45,
        "executedQty": "0.101",
        "cummulativeQuoteQty": "9.595",
    }
    spot.client.repay_margin_loan.return_value = {"tranId": 55}

    recovered = adapter.recover_open_positions()

    assert recovered[0]["status"] == "closed"
    assert spot.client.create_margin_order.call_args.kwargs["side"] == "BUY"
    assert spot.client.repay_margin_loan.call_args.kwargs["amount"] == "1.001"
    assert adapter.db_client.execute_update.call_count == 1