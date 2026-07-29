"""Tests for futures manual-close PnL/parity fixes."""
import sys
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, sys.path[0] + "/..")

from futures_client import FuturesClient, FuturesPosition


def test_average_fill_price_prefers_fills_then_avgprice():
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=True)

    # fills array (spot-like) — weighted average of (10*2 + 11*3) / 5 = 10.6
    assert fc._average_fill_price({
        "fills": [{"price": "10", "qty": "2"}, {"price": "11", "qty": "3"}]
    }, 99.0) == 10.6

    # futures market order — no fills, avgPrice present
    assert fc._average_fill_price({
        "avgPrice": "0.1167902",
    }, 0.12) == 0.1167902

    # fallback
    assert fc._average_fill_price({}, 0.12) == 0.12


def test_classify_close_reason_prefers_manual_audit():
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    fc.db_client = MagicMock()
    fc.db_client.execute_query.return_value = [
        {"detail": json.dumps({"order_id": "manual-123", "side": "SHORT", "source": "cli"})}
    ]

    reason = fc._classify_close_reason("DIAUSDT", "manual-123", 100.0)
    assert reason == "manual_close"


def test_classify_close_reason_uses_order_type():
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    fc.db_client = MagicMock()
    fc.db_client.execute_query.return_value = []

    fc._get = MagicMock(side_effect=[
        {"type": "STOP_MARKET"},
        {"type": "TAKE_PROFIT_MARKET"},
        {"type": "TRAILING_STOP_MARKET"},
        {"type": "MARKET"},
    ])

    assert fc._classify_close_reason("X", "1", -5.0) == "stop_loss_binance"
    assert fc._classify_close_reason("X", "2", 5.0) == "take_profit_binance"
    assert fc._classify_close_reason("X", "3", 5.0) == "trailing_sl_binance"
    assert fc._classify_close_reason("X", "4", 5.0) == "manual_close"


def test_sync_manual_closes_from_audit_marks_position_closed():
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    fc.db_client = MagicMock()
    fc.db_client.execute_query.return_value = [
        {
            "symbol": "DIAUSDT",
            "detail": json.dumps({"side": "SHORT", "order_id": "123"}),
        }
    ]

    pos = FuturesPosition(
        symbol="DIAUSDT", side="SHORT", qty=100.0,
        entry_price=0.12, sl_price=0.13, tp_price=0.11,
        order_id="open-1", sl_order_id="sl-1", tp_order_id="tp-1",
    )
    fc._open_positions["DIAUSDT_SHORT"] = pos

    fc.sync_manual_closes_from_audit()

    assert pos.closed is True
    assert pos.closed_at is not None


def test_close_short_persists_real_fill_price_and_pnl():
    """After a manual market close, exit_price must come from the order fill, not entry_price."""
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)

    pos = FuturesPosition(
        symbol="DIAUSDT", side="SHORT", qty=38665.0,
        entry_price=0.12011130, sl_price=0.1180, tp_price=0.1157,
        order_id="open-1", sl_order_id="sl-1", tp_order_id="tp-1",
    )
    pos.opened_at = datetime.now(timezone.utc)
    fc._open_positions["DIAUSDT_SHORT"] = pos

    saved = {}

    def fake_save(p, reason, **kwargs):
        saved["exit_price"] = p.exit_price
        saved["pnl"] = p.pnl
        saved["reason"] = reason
        return True

    fc._post = MagicMock(return_value={
        "orderId": "1320456188",
        "avgPrice": "0.1167902",
        "executedQty": "38665",
        "cumQuote": "4515.6949000",
        "side": "BUY",
        "positionSide": "SHORT",
    })
    fc._delete = MagicMock(return_value={})
    fc._get_step_size = MagicMock(return_value=1.0)
    fc._get_position_qty_real = MagicMock(return_value=0.0)
    fc._get_realized_pnl_from_trades = MagicMock(return_value={
        "exit_price": 0.1167902,
        "pnl": 119.86,
        "pnl_accounting_mode": "binance_user_trade_verified",
        "realized_pnl": 128.41,
        "commission": 2.03,
        "funding": 0.0,
    })
    fc._save_futures_closed = MagicMock(side_effect=fake_save)

    result = fc.close_short("DIAUSDT", reason="manual_close")

    assert result is not None
    assert saved["exit_price"] == 0.1167902
    assert saved["pnl"] == 119.86
    assert saved["reason"] == "manual_close"
    assert pos.closed is True
