"""Tests for the S105g real-time Binance User Data Stream consumer.

These are pure unit tests of the event-handling/idempotency logic — they never
open a real socket. Connection behavior (ThreadedWebsocketManager wiring) is
exercised only through the graceful-no-op paths (disabled/dry_run/no creds),
since a live websocket test is out of scope for this environment.
"""
import sys
import os
import time
import types
from datetime import datetime, timezone
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from binance_event_stream import BinanceEventStream
from futures_client import FuturesClient, FuturesPosition


def _make_settings(**overrides):
    defaults = dict(
        dry_run=True,
        binance_event_stream_enabled=True,
        binance_event_stream_futures_enabled=True,
        binance_event_stream_spot_enabled=True,
        binance_api_key=None,
        binance_api_secret=None,
        binance_use_testnet=False,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


# ── start()/stop() graceful no-ops ──────────────────────────────────────────

def test_start_noop_when_dry_run():
    stream = BinanceEventStream(settings=_make_settings(dry_run=True))
    assert stream.start() is False
    assert stream.running is False


def test_start_noop_when_disabled_via_config():
    stream = BinanceEventStream(settings=_make_settings(dry_run=False, binance_event_stream_enabled=False))
    assert stream.start() is False


def test_start_noop_when_no_credentials():
    stream = BinanceEventStream(settings=_make_settings(dry_run=False, binance_api_key=None, binance_api_secret=None))
    assert stream.start() is False
    assert stream.last_error is None  # missing creds is an expected no-op, not an error


def test_stop_without_start_does_not_raise():
    stream = BinanceEventStream(settings=_make_settings())
    stream.stop()  # must not raise even if never started


def test_health_shape():
    stream = BinanceEventStream(settings=_make_settings())
    h = stream.health()
    assert set(h.keys()) == {
        "event_stream_running", "event_stream_connected", "event_stream_last_event_at",
        "event_stream_last_futures_event_at", "event_stream_last_spot_event_at", "event_stream_last_error",
        "event_stream_mark_price_subscriptions", "event_stream_reconnect_count",
        "event_stream_last_reconnect_at", "event_stream_pending_reconnect",
    }
    assert h["event_stream_running"] is False


# ── futures ORDER_TRADE_UPDATE dispatch ─────────────────────────────────────

def test_on_futures_message_dispatches_to_futures_client():
    fc = MagicMock()
    stream = BinanceEventStream(settings=_make_settings(), futures_client_getter=lambda: fc)
    order_data = {"s": "ESPUSDT", "X": "FILLED", "ps": "SHORT", "rp": "-30.98"}
    stream._on_futures_message({"e": "ORDER_TRADE_UPDATE", "o": order_data})
    fc.handle_order_fill_event.assert_called_once_with(order_data)
    assert stream.last_futures_event_at is not None
    assert stream.last_event_at is not None


def test_on_futures_message_ignores_non_order_events():
    fc = MagicMock()
    stream = BinanceEventStream(settings=_make_settings(), futures_client_getter=lambda: fc)
    stream._on_futures_message({"e": "ACCOUNT_UPDATE"})
    fc.handle_order_fill_event.assert_not_called()
    # health tracking still updates so a stalled stream can be detected
    assert stream.last_futures_event_at is not None


def test_on_futures_message_never_raises_on_malformed_payload():
    stream = BinanceEventStream(settings=_make_settings(), futures_client_getter=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    # Should not raise even though the getter itself blows up.
    stream._on_futures_message({"e": "ORDER_TRADE_UPDATE", "o": {}})
    assert stream.last_error is not None


# ── spot executionReport audit ──────────────────────────────────────────────

def test_on_spot_message_audits_filled_execution():
    db = MagicMock()
    stream = BinanceEventStream(settings=_make_settings(), db_client=db)
    stream._on_spot_message({
        "e": "executionReport", "X": "FILLED", "s": "BTCUSDT", "S": "SELL",
        "z": "0.01", "L": "65000", "c": "web_123", "i": 999,
    })
    db.execute_update.assert_called_once()
    args = db.execute_update.call_args[0]
    assert "event_stream_spot_execution" in args[1]


def test_on_spot_message_ignores_non_filled():
    db = MagicMock()
    stream = BinanceEventStream(settings=_make_settings(), db_client=db)
    stream._on_spot_message({"e": "executionReport", "X": "NEW", "s": "BTCUSDT"})
    db.execute_update.assert_not_called()


# ── FuturesClient.handle_order_fill_event (the futures-side of the pipeline) ─

def _make_pos(symbol="ESPUSDT", side="SHORT"):
    return FuturesPosition(
        symbol=symbol, side=side, qty=100.0,
        entry_price=0.06753, sl_price=0.0713, tp_price=0.0630,
        order_id="open-1", sl_order_id="sl-1", tp_order_id="tp-1",
        opened_at=datetime.now(timezone.utc),
    )


def test_handle_order_fill_event_closes_tracked_position():
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    pos = _make_pos()
    fc._open_positions["ESPUSDT_SHORT"] = pos
    fc._get_realized_pnl_from_trades = MagicMock(return_value={
        "exit_price": 0.0685, "pnl": -30.98, "reason": "regime_change",
    })
    fc._save_futures_closed = MagicMock(return_value=True)

    result = fc.handle_order_fill_event({
        "s": "ESPUSDT", "X": "FILLED", "ps": "SHORT", "rp": "-30.98",
        "i": "12345", "c": fc._gen_client_order_id("x", "regime_change"),
        "ap": "0.0685", "ot": "MARKET",
    })

    assert result is not None
    assert result["reason"] == "regime_change"
    assert pos.closed is True
    fc._save_futures_closed.assert_called_once()
    _, saved_reason = fc._save_futures_closed.call_args[0]
    assert saved_reason == "regime_change"


def test_handle_order_fill_event_idempotent_when_already_closed():
    """S105g: if the polling path already closed the position, the event must
    be a silent no-op — never double-persist or raise."""
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    pos = _make_pos()
    pos.closed = True
    fc._open_positions["ESPUSDT_SHORT"] = pos
    fc._save_futures_closed = MagicMock(return_value=True)

    result = fc.handle_order_fill_event({
        "s": "ESPUSDT", "X": "FILLED", "ps": "SHORT", "rp": "-30.98",
    })

    assert result is None
    fc._save_futures_closed.assert_not_called()


def test_handle_order_fill_event_ignores_non_closing_fill():
    """An entry fill (FILLED, realized profit 0) must not be treated as a close."""
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    pos = _make_pos()
    fc._open_positions["ESPUSDT_SHORT"] = pos
    fc._save_futures_closed = MagicMock(return_value=True)

    result = fc.handle_order_fill_event({
        "s": "ESPUSDT", "X": "FILLED", "ps": "SHORT", "rp": "0",
    })

    assert result is None
    assert pos.closed is False
    fc._save_futures_closed.assert_not_called()


def test_handle_order_fill_event_never_raises_on_malformed_event():
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    # Missing/garbage fields must not raise.
    assert fc.handle_order_fill_event({}) is None
    assert fc.handle_order_fill_event({"X": "FILLED", "rp": "not_a_number"}) is None


# ── S105h: websocket error detection + reconnect scheduling ─────────────────

def test_socket_error_payload_detection():
    stream = BinanceEventStream(settings=_make_settings())
    assert stream._is_socket_error_payload("Read loop has been closed, please reset the websocket connection and listen to the message error")
    assert stream._is_socket_error_payload({"e": "error", "m": "Read loop has been closed"})
    assert stream._is_socket_error_payload({"message": "websocket error"})
    assert not stream._is_socket_error_payload({"e": "ORDER_TRADE_UPDATE", "o": {}})
    assert not stream._is_socket_error_payload("random benign text")


def test_on_futures_message_error_triggers_reconnect():
    stream = BinanceEventStream(settings=_make_settings())
    reconnects = []
    stream._schedule_reconnect = lambda: reconnects.append(True)
    stream._on_futures_message({"e": "error", "m": "Read loop has been closed, please reset the websocket connection"})
    assert len(reconnects) == 1
    assert stream.last_error is not None


def test_on_spot_message_error_triggers_reconnect():
    stream = BinanceEventStream(settings=_make_settings())
    reconnects = []
    stream._schedule_reconnect = lambda: reconnects.append(True)
    stream._on_spot_message("Read loop has been closed, please reset the websocket connection and listen to the message error")
    assert len(reconnects) == 1
    assert stream.last_error is not None


def test_force_reconnect_schedules_reconnect():
    stream = BinanceEventStream(settings=_make_settings())
    reconnects = []
    stream._schedule_reconnect = lambda: reconnects.append(True)
    assert stream.force_reconnect() is True
    assert len(reconnects) == 1


def test_watchdog_detects_stale_connection():
    stream = BinanceEventStream(settings=_make_settings())
    stream.running = True
    stream.last_event_at = datetime.now(timezone.utc).replace(year=2000)
    stream.WATCHDOG_INTERVAL_SECONDS = 0.05
    stream.EVENT_TIMEOUT_SECONDS = 0.01
    reconnects = []
    stream._schedule_reconnect = lambda: reconnects.append(True)
    stream._start_watchdog()
    time.sleep(0.2)
    stream.stop()
    assert len(reconnects) >= 1
