"""
binance_event_stream.py — S105g: real-time Binance User Data Stream consumer.

Anticipates Binance's own order/position lifecycle instead of discovering it
after the fact via polling. Wraps python-binance's ThreadedWebsocketManager
(already a project dependency, no new package needed) for both futures and
spot user data streams. Binance remains the ultimate source of truth; this
module just lets our system react to it in seconds instead of up to
`_trade_cycle_interval_minutes` (default 5 min).

Design notes:
- Never a hard dependency: if websocket setup fails (bad credentials, no
  network, disabled via config, dry_run), the system keeps working exactly
  as before via the existing polling/reconciliation paths (FIX-S4, FIX-T4-L1/L2
  in futures_client.py). This is a latency improvement layered on top, not a
  replacement of the safety net.
- Idempotent by construction: futures fills are handled by
  FuturesClient.handle_order_fill_event(), which no-ops if the position is
  already closed (e.g. the polling path won the race).
- listenKey creation/keepalive is handled internally by ThreadedWebsocketManager
  — no hand-rolled listenKey management needed.
- SPOT has no exchange-side SL/TP (see canonical/calibrar-ciclo-vida-52fe5d.md);
  closes are always local MARKET orders issued by the trade cycle. The spot
  stream's job is early visibility (same-second audit trail of every real fill)
  rather than autonomous state mutation, to keep the blast radius of this
  change limited to the (already well-tested) futures reconciliation pipeline.
"""
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class BinanceEventStream:
    """Owns the websocket connection(s) and dispatches events to the right handler."""

    def __init__(self, settings, futures_client_getter: Optional[Callable[[], Any]] = None, db_client=None):
        """
        Args:
            settings: app Settings (binance_api_key/secret, testnet, dry_run, feature flags).
            futures_client_getter: zero-arg callable returning the live FuturesClient
                (lazy — avoids import cycles / stale-client issues, mirrors the
                pattern of run_trades._get_futures_client()).
            db_client: optional, used for lightweight spot-side audit logging.
        """
        self.settings = settings
        self._futures_client_getter = futures_client_getter
        self.db_client = db_client
        self._twm = None
        self._futures_conn = None
        self._spot_conn = None
        self.running = False
        self.connected = False
        self.last_event_at: Optional[datetime] = None
        self.last_futures_event_at: Optional[datetime] = None
        self.last_spot_event_at: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self._lock = threading.Lock()
        self._reconnect_lock = threading.Lock()
        self._pending_reconnect = False
        self._reconnect_count = 0
        self._last_reconnect_at: Optional[datetime] = None
        self._watcher_thread: Optional[threading.Thread] = None
        self._watcher_stop = threading.Event()
        # PIS: mark price streams for open positions (zero API weight)
        self._mark_price_cache: dict = {}  # symbol -> last mark price (float)
        self._mark_price_conns: dict = {}  # symbol -> conn_key
        self._mark_price_lock = threading.Lock()
        # snapshot of currently active symbols so we can re-subscribe after reconnect
        self._mark_price_symbols: set = set()
        # S105h: reconnect tuning
        self.WATCHDOG_INTERVAL_SECONDS = 30.0
        # User data streams can be legitimately silent for minutes when there is
        # no position/order activity; the read-loop error is normally delivered to
        # the callback (handled immediately). This long timeout is only a safety
        # net for a completely frozen socket (e.g. network partition).
        self.EVENT_TIMEOUT_SECONDS = 1800.0  # 30 min
        self.RECONNECT_COOLDOWN_SECONDS = 60.0

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> bool:
        """Start the websocket connection(s). Returns True if at least one
        stream connected. Never raises — any failure just leaves the system
        on its existing polling behavior."""
        with self._lock:
            if self.running:
                return True
            if getattr(self.settings, 'dry_run', True):
                print("[EventStream] dry_run — websocket disabled, using polling only")
                return False
            if not getattr(self.settings, 'binance_event_stream_enabled', True):
                print("[EventStream] disabled via config (binance_event_stream_enabled=False)")
                return False
            api_key = getattr(self.settings, 'binance_api_key', None)
            api_secret = getattr(self.settings, 'binance_api_secret', None)
            if not api_key or not api_secret:
                print("[EventStream] no API credentials — websocket disabled")
                return False
            try:
                from binance import ThreadedWebsocketManager
            except Exception as e:
                print(f"[EventStream] python-binance websocket support unavailable: {e}")
                self.last_error = f"import_failed: {e}"
                return False
            try:
                testnet = bool(getattr(self.settings, 'binance_use_testnet', True))
                self._twm = ThreadedWebsocketManager(api_key=api_key, api_secret=api_secret, testnet=testnet)
                self._twm.start()

                # NOTE: start_futures_user_socket()/start_user_socket() return a
                # connection-key string identifying the socket, NOT a truthiness
                # signal — some python-binance versions can return an empty
                # string on success, which would be falsy. Track success via an
                # explicit flag set only when the call didn't raise, never via
                # bool(return_value).
                futures_ok = False
                spot_ok = False

                if getattr(self.settings, 'binance_event_stream_futures_enabled', True):
                    try:
                        self._futures_conn = self._twm.start_futures_user_socket(callback=self._on_futures_message)
                        futures_ok = True
                        print("[EventStream] futures user data stream started")
                    except Exception as e:
                        self.last_error = f"futures_socket_start_failed: {e}"
                        print(f"[EventStream] {self.last_error}")

                if getattr(self.settings, 'binance_event_stream_spot_enabled', True):
                    try:
                        self._spot_conn = self._twm.start_user_socket(callback=self._on_spot_message)
                        spot_ok = True
                        print("[EventStream] spot user data stream started")
                    except Exception as e:
                        self.last_error = f"spot_socket_start_failed: {e}"
                        print(f"[EventStream] {self.last_error}")

                self.running = futures_ok or spot_ok
                self.connected = self.running
                self._futures_ok = futures_ok
                self._spot_ok = spot_ok
                print(f"[EventStream] start() done: running={self.running} futures={futures_ok} spot={spot_ok}")
                self._start_watchdog()
                return self.running
            except Exception as e:
                self.last_error = str(e)
                print(f"[EventStream] start failed: {e}")
                self.running = False
                self.connected = False
                return False

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the websocket connection(s).

        S105g hardening: earlier `python-binance==1.0.16` hung indefinitely
        during async teardown against a newer `websockets` version
        (AttributeError inside its __aexit__ path) — fixed by upgrading to
        python-binance==1.0.37 (verified in production: stop() now completes
        in a few seconds). This timeout wrapper is kept as defensive-in-depth:
        a live trading system must ALWAYS be able to pause/stop, so even a
        future/unknown teardown hang can never block the caller — the real
        stop runs in a daemon thread with a hard timeout, and we move on
        (leaving the background thread to die with the process if it never
        unblocks) rather than freezing `orchestrator.stop_all_cycles()`.
        """
        with self._lock:
            twm = self._twm
            if not self.running and twm is None and self._watcher_thread is None:
                return
            self.running = False
            self.connected = False
            self._twm = None
            self._futures_conn = None
            self._spot_conn = None
            self._pending_reconnect = False

        self._watcher_stop.set()
        if self._watcher_thread is not None:
            try:
                self._watcher_thread.join(timeout=timeout)
            except Exception:
                pass
            self._watcher_thread = None
        self._watcher_stop.clear()

        if twm is None:
            return

        def _do_stop():
            try:
                twm.stop()
            except Exception as e:
                print(f"[EventStream] stop error: {e}")

        stopper = threading.Thread(target=_do_stop, daemon=True)
        stopper.start()
        stopper.join(timeout=timeout)
        if stopper.is_alive():
            self.last_error = "stop_timed_out"
            print(f"[EventStream] stop() did not complete within {timeout}s — abandoning teardown thread")
        else:
            print("[EventStream] stopped")

    def health(self) -> dict:
        return {
            "event_stream_running": self.running,
            "event_stream_connected": self.connected,
            "event_stream_last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
            "event_stream_last_futures_event_at": self.last_futures_event_at.isoformat() if self.last_futures_event_at else None,
            "event_stream_last_spot_event_at": self.last_spot_event_at.isoformat() if self.last_spot_event_at else None,
            "event_stream_last_error": self.last_error,
            "event_stream_mark_price_subscriptions": len(self._mark_price_conns),
            "event_stream_reconnect_count": self._reconnect_count,
            "event_stream_last_reconnect_at": self._last_reconnect_at.isoformat() if self._last_reconnect_at else None,
            "event_stream_pending_reconnect": self._pending_reconnect,
        }

    # ── callbacks (never raise — a bad event must not kill the socket thread) ─
    def _is_socket_error_payload(self, msg) -> bool:
        """Detect python-binance/websockets error payloads that indicate the
        read loop died (e.g. 'Read loop has been closed ...')."""
        text = ""
        if isinstance(msg, str):
            text = msg
        elif isinstance(msg, dict):
            text = str(msg.get("m", msg.get("message", msg.get("e", ""))))
        return any(needle in text.lower() for needle in (
            "read loop has been closed",
            "please reset the websocket connection",
            "error receiving message",
            "connection closed",
            "websocket error",
        ))

    def _handle_socket_error(self, stream_name: str, msg) -> None:
        self.last_error = f"{stream_name}_read_loop_closed: {msg!r}"
        print(f"[EventStream] {self.last_error}")
        self._schedule_reconnect()

    def _on_futures_message(self, msg: Any) -> None:
        try:
            if self._is_socket_error_payload(msg):
                self._handle_socket_error("futures", msg)
                return
            _now = datetime.now(timezone.utc)
            self.last_event_at = _now
            self.last_futures_event_at = _now
            if not isinstance(msg, dict):
                return
            if msg.get('e') == 'ORDER_TRADE_UPDATE':
                order_data = msg.get('o') or {}
                fc = self._futures_client_getter() if self._futures_client_getter else None
                if fc is not None:
                    fc.handle_order_fill_event(order_data)
            # ACCOUNT_UPDATE / MARGIN_CALL / listenKeyExpired: observed via
            # last_event_at for health, not acted on yet — polling reconciliation
            # (FIX-T4-L1/L2) remains the safety net for those cases.
        except Exception as e:
            self.last_error = str(e)
            print(f"[EventStream] futures message handling error: {e}")

    def _on_spot_message(self, msg: Any) -> None:
        try:
            if self._is_socket_error_payload(msg):
                self._handle_socket_error("spot", msg)
                return
            _now = datetime.now(timezone.utc)
            self.last_event_at = _now
            self.last_spot_event_at = _now
            if not isinstance(msg, dict):
                return
            if msg.get('e') == 'executionReport' and str(msg.get('X', '')).upper() == 'FILLED':
                self._audit_spot_execution(msg)
        except Exception as e:
            self.last_error = str(e)
            print(f"[EventStream] spot message handling error: {e}")

    def _audit_spot_execution(self, msg: dict) -> None:
        """SPOT has no exchange-side SL/TP — closes are always local MARKET
        orders issued by the trade cycle itself. Here we just give
        `binance_truth_guard.evaluate_spot_parity()` (still the authority for
        state mutation) a same-second audit trail of every real fill, instead
        of discovering a balance change up to one cycle later."""
        if not self.db_client:
            return
        try:
            detail = {
                'side': msg.get('S'),
                'qty': msg.get('z'),
                'price': msg.get('L') or msg.get('p'),
                'client_order_id': msg.get('c'),
                'order_id': msg.get('i'),
            }
            self.db_client.execute_update(
                "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed)"
                " VALUES (%s, %s, %s, %s, %s)",
                (msg.get('s'), "spot", "event_stream_spot_execution", json.dumps(detail), True),
            )
        except Exception as e:
            print(f"[EventStream] spot audit failed: {e}")

    # ── S105h: reconnect / watchdog ──────────────────────────────────────────

    def _start_watchdog(self) -> None:
        """Start a background thread that verifies the websocket is still alive
        and triggers a reconnect if no event has arrived within the timeout."""
        if self._watcher_thread is not None and self._watcher_thread.is_alive():
            return
        self._watcher_stop.clear()
        self._watcher_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watcher_thread.start()

    def _watchdog_loop(self) -> None:
        """Daemon loop: every WATCHDOG_INTERVAL_SECONDS check whether any event has
        arrived recently. If not, schedule a reconnect."""
        while not self._watcher_stop.wait(self.WATCHDOG_INTERVAL_SECONDS):
            try:
                with self._lock:
                    running = self.running
                    last = self.last_event_at
                if not running:
                    continue
                if last is None:
                    # started but no event yet; give it the full timeout before panic
                    continue
                elapsed = (datetime.now(timezone.utc) - last).total_seconds()
                if elapsed > self.EVENT_TIMEOUT_SECONDS:
                    self.last_error = f"watchdog_timeout: no event for {elapsed:.0f}s"
                    print(f"[EventStream] {self.last_error} — scheduling reconnect")
                    self._schedule_reconnect()
            except Exception as exc:
                print(f"[EventStream] watchdog error: {exc}")

    def _schedule_reconnect(self) -> None:
        """Enqueue a reconnect from a background thread. Serialization and a
        cooldown avoid overlapping or runaway reconnect attempts."""
        with self._reconnect_lock:
            if self._pending_reconnect:
                return
            if self._last_reconnect_at is not None:
                elapsed = (datetime.now(timezone.utc) - self._last_reconnect_at).total_seconds()
                if elapsed < self.RECONNECT_COOLDOWN_SECONDS:
                    return
            self._pending_reconnect = True
        threading.Thread(target=self._reconnect, daemon=True).start()

    def _reconnect(self) -> None:
        """Tear down the existing ThreadedWebsocketManager, build a fresh one,
        and restart futures/spot/mark-price streams. Never raises."""
        try:
            print("[EventStream] reconnect started")
            with self._lock:
                self._last_reconnect_at = datetime.now(timezone.utc)
                self.connected = False
                self.running = False
                old_twm = self._twm
                old_futures = getattr(self, '_futures_ok', False)
                old_spot = getattr(self, '_spot_ok', False)
                self._twm = None
                self._futures_conn = None
                self._spot_conn = None

            def _stop_old():
                try:
                    if old_twm is not None:
                        old_twm.stop()
                except Exception as exc:
                    print(f"[EventStream] reconnect: old twm stop error: {exc}")
            threading.Thread(target=_stop_old, daemon=True).start()

            # clear stale mark-price connection state (will be re-created)
            with self._mark_price_lock:
                self._mark_price_conns.clear()

            time.sleep(1.0)

            try:
                from binance import ThreadedWebsocketManager
            except Exception as exc:
                self.last_error = f"reconnect_import_failed: {exc}"
                print(f"[EventStream] {self.last_error}")
                return

            api_key = getattr(self.settings, 'binance_api_key', None)
            api_secret = getattr(self.settings, 'binance_api_secret', None)
            testnet = bool(getattr(self.settings, 'binance_use_testnet', True))
            new_twm = ThreadedWebsocketManager(api_key=api_key, api_secret=api_secret, testnet=testnet)
            new_twm.start()

            futures_ok = False
            if old_futures and getattr(self.settings, 'binance_event_stream_futures_enabled', True):
                try:
                    self._futures_conn = new_twm.start_futures_user_socket(callback=self._on_futures_message)
                    futures_ok = True
                    print("[EventStream] reconnect: futures user data stream started")
                except Exception as exc:
                    self.last_error = f"reconnect_futures_socket_failed: {exc}"
                    print(f"[EventStream] {self.last_error}")

            spot_ok = False
            if old_spot and getattr(self.settings, 'binance_event_stream_spot_enabled', True):
                try:
                    self._spot_conn = new_twm.start_user_socket(callback=self._on_spot_message)
                    spot_ok = True
                    print("[EventStream] reconnect: spot user data stream started")
                except Exception as exc:
                    self.last_error = f"reconnect_spot_socket_failed: {exc}"
                    print(f"[EventStream] {self.last_error}")

            with self._lock:
                self._twm = new_twm
                self._futures_ok = futures_ok
                self._spot_ok = spot_ok
                self.running = futures_ok or spot_ok
                self.connected = self.running
                self._reconnect_count += 1
                self._last_reconnect_at = datetime.now(timezone.utc)
                if self.running:
                    self.last_error = None
            print(f"[EventStream] reconnect done: running={self.running} futures={futures_ok} spot={spot_ok}")

            # re-subscribe mark-price streams for currently tracked symbols
            symbols_to_resub = set()
            with self._mark_price_lock:
                symbols_to_resub = set(self._mark_price_symbols)
            for symbol in symbols_to_resub:
                self.subscribe_mark_price(symbol)
        except Exception as exc:
            self.last_error = f"reconnect_fatal: {exc}"
            print(f"[EventStream] reconnect fatal error: {exc}")
        finally:
            with self._reconnect_lock:
                self._pending_reconnect = False

    def force_reconnect(self) -> bool:
        """Public API to force a reconnect from orchestrator/health endpoints."""
        self._schedule_reconnect()
        return True

    # ── PIS: Mark Price Streams (zero API weight) ────────────────────────────

    def subscribe_mark_price(self, symbol: str) -> bool:
        """Subscribe to @markPrice stream for a futures position (1s updates).

        Uses the already-running ThreadedWebsocketManager. Returns True if
        subscription succeeded or was already active. Never raises.
        """
        with self._mark_price_lock:
            if symbol in self._mark_price_conns:
                return True
        if not self.running or self._twm is None:
            return False
        try:
            conn_key = self._twm.start_symbol_mark_price_socket(
                callback=self._on_mark_price,
                symbol=symbol.lower(),
                fast=True,  # @1s instead of @3s
            )
            with self._mark_price_lock:
                self._mark_price_conns[symbol] = conn_key
                self._mark_price_symbols.add(symbol)
            return True
        except Exception as e:
            print(f"[EventStream] mark price subscribe failed for {symbol}: {e}")
            return False

    def unsubscribe_mark_price(self, symbol: str) -> None:
        """Unsubscribe from mark price stream when a position closes. Never raises."""
        with self._mark_price_lock:
            conn_key = self._mark_price_conns.pop(symbol, None)
            self._mark_price_cache.pop(symbol, None)
            self._mark_price_symbols.discard(symbol)
        if conn_key and self._twm:
            try:
                self._twm.stop_socket(conn_key)
            except Exception:
                pass

    def get_mark_price(self, symbol: str) -> Optional[float]:
        """Get last mark price from websocket cache. Returns None if not subscribed."""
        return self._mark_price_cache.get(symbol)

    def get_all_mark_prices(self) -> dict:
        """Return snapshot of all cached mark prices. Thread-safe read."""
        with self._mark_price_lock:
            return dict(self._mark_price_cache)

    def _on_mark_price(self, msg: Any) -> None:
        """Callback for mark price update events. Never raises."""
        try:
            if not isinstance(msg, dict):
                return
            event_type = msg.get('e', '')
            if event_type == 'markPriceUpdate':
                symbol = msg.get('s', '')
                price = float(msg.get('p', 0))
                if symbol and price > 0:
                    self._mark_price_cache[symbol] = price
        except Exception:
            pass
