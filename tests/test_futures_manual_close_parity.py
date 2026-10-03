"""Tests for futures manual-close PnL/parity fixes."""
import sys
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, sys.path[0] + "/..")

from futures_client import FuturesClient, FuturesPosition


def test_gen_client_order_id_structure_and_length():
    """S105g: structured clientOrderId must fit Binance's 36-char limit and
    round-trip cleanly for the 'x-<reason>-<uuid8>' close-order shape."""
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=True)

    entry_cid = fc._gen_client_order_id("e")
    assert entry_cid.startswith("e-")
    assert len(entry_cid) <= 36

    sl_cid = fc._gen_client_order_id("sl")
    assert sl_cid.startswith("sl-")

    close_cid = fc._gen_client_order_id("x", "regime_change")
    assert close_cid.startswith("x-regime_change-")
    assert len(close_cid) <= 36
    assert fc._decode_close_reason_from_client_order_id(close_cid) == "regime_change"

    # Long reason gets truncated but still fits and still round-trips to *some*
    # stable, readable slug (never raises, never exceeds the Binance limit).
    long_reason = "unified_governance_near_sl_hold_extra_long_reason"
    long_cid = fc._gen_client_order_id("x", long_reason)
    assert len(long_cid) <= 36
    decoded = fc._decode_close_reason_from_client_order_id(long_cid)
    assert decoded and long_reason.startswith(decoded)

    # Not one of our tags → None (falls through to other classification steps)
    assert fc._decode_close_reason_from_client_order_id("web_ABC123XYZ") is None
    assert fc._decode_close_reason_from_client_order_id(None) is None


def test_classify_close_reason_prefers_client_order_id_over_order_type():
    """S105g: our own structured clientOrderId is the most authoritative signal —
    it must win even when the order type alone would suggest something else
    (e.g. a MARKET order that used to default to "manual_close")."""
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    fc.db_client = MagicMock()
    fc.db_client.execute_query.return_value = []  # no manual audit match
    fc._get = MagicMock(return_value={
        "type": "MARKET",
        "clientOrderId": fc._gen_client_order_id("x", "regime_change"),
    })

    reason = fc._classify_close_reason("ESPUSDT", "999", -30.98)
    assert reason == "regime_change"


def test_close_short_tags_market_order_with_reason(monkeypatch=None):
    """S105g: close_short() must stamp newClientOrderId with the real reason on
    its MARKET close order, so Binance's own record carries our intent."""
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    pos = FuturesPosition(
        symbol="DIAUSDT", side="SHORT", qty=100.0,
        entry_price=0.12, sl_price=0.13, tp_price=0.11,
        order_id="open-1", sl_order_id="sl-1", tp_order_id="tp-1",
    )
    pos.opened_at = datetime.now(timezone.utc)
    fc._open_positions["DIAUSDT_SHORT"] = pos

    captured = {}

    def fake_post(path, params):
        if path == "/fapi/v1/order":
            captured["params"] = params
        return {"orderId": "1", "avgPrice": "0.115", "executedQty": "100"}

    fc._post = MagicMock(side_effect=fake_post)
    fc._delete = MagicMock(return_value={})
    fc._get_step_size = MagicMock(return_value=1.0)
    fc._get_position_qty_real = MagicMock(return_value=0.0)
    fc._get_realized_pnl_from_trades = MagicMock(return_value={
        "exit_price": 0.115, "pnl": 0.5, "pnl_accounting_mode": "binance_user_trade_verified",
    })
    fc._save_futures_closed = MagicMock(return_value=True)

    fc.close_short("DIAUSDT", reason="regime_change")

    cid = captured["params"].get("newClientOrderId", "")
    assert cid.startswith("x-regime_change-")
    assert fc._decode_close_reason_from_client_order_id(cid) == "regime_change"


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
    ])

    assert fc._classify_close_reason("X", "1", -5.0) == "stop_loss_binance"
    assert fc._classify_close_reason("X", "2", 5.0) == "take_profit_binance"
    assert fc._classify_close_reason("X", "3", 5.0) == "trailing_sl_binance"


def test_classify_close_reason_market_order_without_audit_is_not_manual():
    """S105f: a MARKET order alone is NOT proof of a manual close — every automated
    exit (regime_change, unified_governance_*, basket_reset_auto, etc.) also sends
    plain MARKET orders. Without a manual audit match, this must never silently
    become "manual_close"."""
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    fc.db_client = MagicMock()
    fc.db_client.execute_query.return_value = []  # no manual_close audit match
    fc._get = MagicMock(return_value={"type": "MARKET"})

    # No fallback_reason known (e.g. ghost cleanup with no caller context) — neutral label.
    assert fc._classify_close_reason("X", "4", 5.0) == "binance_unidentified_close"

    # Caller knew why it was trying to close (e.g. regime_change) — preserve real reason.
    fc._get = MagicMock(return_value={"type": "MARKET"})
    assert fc._classify_close_reason("X", "4", 5.0, fallback_reason="regime_change") == "regime_change"


def test_classify_close_reason_sign_fallback_prefers_caller_reason():
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    fc.db_client = MagicMock()
    fc.db_client.execute_query.return_value = []
    fc._get = MagicMock(side_effect=Exception("order lookup unavailable"))

    # No fallback: keeps the old sign-based heuristic.
    assert fc._classify_close_reason("X", "5", 5.0) == "trailing_sl_binance"
    assert fc._classify_close_reason("X", "5", -5.0) == "stop_loss_binance"

    # With fallback: prefer the caller's real intended reason over the guess.
    assert fc._classify_close_reason("X", "5", -5.0, fallback_reason="unified_governance_near_sl_hold") == \
        "unified_governance_near_sl_hold"


def test_close_short_race_lost_to_binance_keeps_original_reason_not_manual():
    """S105f regression: when close_short() loses a race (-2022/ReduceOnly) because the
    position was already closed by another automated path (also a MARKET order), the
    persisted close_reason must stay the caller's original intent — never "manual_close"."""
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)

    pos = FuturesPosition(
        symbol="ESPUSDT", side="SHORT", qty=29616.0,
        entry_price=0.06753, sl_price=0.0713, tp_price=0.0630,
        order_id="open-1", sl_order_id="sl-1", tp_order_id="tp-1",
    )
    pos.opened_at = datetime.now(timezone.utc)
    fc._open_positions["ESPUSDT_SHORT"] = pos

    fc._post = MagicMock(side_effect=Exception("APIError(code=-2022): ReduceOnly Order is rejected."))
    fc.get_price = MagicMock(return_value=0.0685)

    saved = {}

    def fake_save(p, reason, **kwargs):
        saved["reason"] = reason
        return True

    fc._save_futures_closed = MagicMock(side_effect=fake_save)
    # Simulate reconciliation finding the real closing order was our own regime_change
    # MARKET close (no manual audit match) — _classify_close_reason would have said
    # "binance_unidentified_close" without the fallback; with it, it must return it.
    fc._get_realized_pnl_from_trades = MagicMock(return_value={
        "exit_price": 0.06853684967585087,
        "pnl": -30.98,
        "reason": "regime_change",
    })

    result = fc.close_short("ESPUSDT", pos.qty, reason="regime_change")

    assert result is not None
    assert saved["reason"] == "regime_change"
    assert saved["reason"] != "manual_close"
    fc._get_realized_pnl_from_trades.assert_called_once()
    _, kwargs = fc._get_realized_pnl_from_trades.call_args
    assert kwargs.get("fallback_reason") == "regime_change"


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
        "exit_price": 0.1172504,
        "pnl": 119.86,
        "pnl_accounting_mode": "binance_user_trade_verified",
        "realized_pnl": 128.41,
        "commission": 2.03,
        "funding": 0.0,
    })
    fc._save_futures_closed = MagicMock(side_effect=fake_save)

    result = fc.close_short("DIAUSDT", reason="manual_close")

    assert result is not None
    assert saved["exit_price"] == 0.1172504
    assert saved["pnl"] == 119.86
    assert saved["reason"] == "manual_close"
    assert pos.closed is True
