"""
futures_client.py — Binance USDM Futures wrapper (fapi)

Provides SHORT position management with conservative risk controls:
  - Fixed 2x leverage (never higher)
  - Mandatory stop-loss on every position
  - Max 2 simultaneous short positions
  - Funding rate check before entry (skip if funding > 0.05%)
  - Dry-run support (mirrors binance_client.py dry_run flag)

API base: https://fapi.binance.com
Auth: same API key/secret as spot, requires Futures trading enabled on account.

Usage:
    fc = FuturesClient(api_key, api_secret, dry_run=False)
    pos = fc.open_short("BTCUSDT", usdt_amount=50.0, sl_pct=3.0, tp_pct=5.0)
    fc.close_short("BTCUSDT", pos["qty"])
"""

import json
import time
import hashlib
import hmac
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone


# ── Constants ─────────────────────────────────────────────────────────────────
FAPI_BASE     = "https://fapi.binance.com"
LEVERAGE_MAX  = 2          # Hard cap — never allow higher
MAX_SHORTS    = 2          # Max simultaneous short positions
FUNDING_LIMIT = 0.0005     # Skip entry if funding rate > 0.05% (overleveraged longs → risky to short)
_PRICE_CACHE_TTL = 15      # seconds


@dataclass
class FuturesPosition:
    symbol: str
    side: str                    # "SHORT"
    qty: float
    entry_price: float
    sl_price: float
    tp_price: float
    order_id: str
    sl_order_id: Optional[str]
    tp_order_id: Optional[str]
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed: bool = False
    closed_at: Optional[datetime] = None
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    _smart_meta: Dict = field(default_factory=dict, repr=False, compare=False)

    def unrealized_pnl(self, current_price: float) -> float:
        if self.side == "LONG":
            return (current_price - self.entry_price) * self.qty
        return (self.entry_price - current_price) * self.qty

    def is_sl_hit(self, current_price: float) -> bool:
        if self.side == "LONG":
            return current_price <= self.sl_price   # LONG SL is below entry
        return current_price >= self.sl_price       # SHORT SL is above entry

    def is_tp_hit(self, current_price: float) -> bool:
        if self.side == "LONG":
            return current_price >= self.tp_price   # LONG TP is above entry
        return current_price <= self.tp_price       # SHORT TP is below entry


class FuturesClient:
    def __init__(self, api_key: str, api_secret: str, dry_run: bool = False,
                 max_shorts: int = MAX_SHORTS, max_longs: int = 3):
        self.api_key    = api_key
        self.api_secret = api_secret
        self.dry_run    = dry_run
        self.max_shorts = max_shorts       # configurable via settings.futures_max_shorts
        self.max_longs  = max_longs        # configurable via settings.futures_max_longs
        self.max_largecap_shorts: int = 3  # max LARGE_CAP shorts simultaneously — set by _get_futures_client
        self.largecap_symbols: set = {'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'XRPUSDT', 'SOLUSDT'}  # updated from settings
        # Key format: "{SYMBOL}_{SIDE}" e.g. "BTCUSDT_SHORT", "ETHUSDT_LONG"
        self._open_positions: Dict[str, FuturesPosition] = {}
        self._price_cache: Dict[str, Tuple[float, float]] = {}  # {symbol: (price, ts)}
        self._leverage_set: set = set()   # symbols with leverage already configured
        self._hedge_mode: Optional[bool] = None  # None = unknown until checked
        self._binance_client_ref = None  # set by _get_futures_client() for momentum eval
        if not dry_run:
            self._sync_open_positions()

    # ── Position key helpers ──────────────────────────────────────────────────

    @staticmethod
    def _pos_key(symbol: str, side: str) -> str:
        """Canonical key for _open_positions dict: 'BTCUSDT_SHORT' or 'BTCUSDT_LONG'."""
        return f"{symbol}_{side.upper()}"

    # ── Internal HTTP helpers ─────────────────────────────────────────────────

    def _sign(self, params: str) -> str:
        return hmac.new(
            self.api_secret.encode("utf-8"),
            params.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    def _get(self, path: str, params: dict = None, signed: bool = False, timeout: int = 5) -> dict:
        p = params or {}
        if signed:
            p["timestamp"] = int(time.time() * 1000)
            query = urllib.parse.urlencode(p)
            query += f"&signature={self._sign(query)}"
        else:
            query = urllib.parse.urlencode(p)
        url = f"{FAPI_BASE}{path}?{query}" if query else f"{FAPI_BASE}{path}"
        req = urllib.request.Request(url, headers={"X-MBX-APIKEY": self.api_key})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def _post(self, path: str, params: dict, timeout: int = 8) -> dict:
        import urllib.error as _ue
        params["timestamp"] = int(time.time() * 1000)
        query = urllib.parse.urlencode(params)
        query += f"&signature={self._sign(query)}"
        url  = f"{FAPI_BASE}{path}"
        data = query.encode("utf-8")
        req  = urllib.request.Request(
            url, data=data, method="POST",
            headers={"X-MBX-APIKEY": self.api_key, "Content-Type": "application/x-www-form-urlencoded"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except _ue.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise Exception(f"HTTP {e.code} {e.reason} — {body}") from None

    def _delete(self, path: str, params: dict, timeout: int = 8) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        query = urllib.parse.urlencode(params)
        query += f"&signature={self._sign(query)}"
        url  = f"{FAPI_BASE}{path}?{query}"
        req  = urllib.request.Request(
            url, method="DELETE",
            headers={"X-MBX-APIKEY": self.api_key}
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    # ── Market data ───────────────────────────────────────────────────────────

    def get_price(self, symbol: str) -> float:
        now = time.monotonic()
        if symbol in self._price_cache:
            val, ts = self._price_cache[symbol]
            if now - ts < _PRICE_CACHE_TTL:
                return val
        data  = self._get("/fapi/v1/ticker/price", {"symbol": symbol})
        price = float(data["price"])
        self._price_cache[symbol] = (price, now)
        return price

    def get_funding_rate(self, symbol: str) -> float:
        """Return latest funding rate for symbol. Returns 0.0 on error."""
        try:
            data = self._get("/fapi/v1/fundingRate", {"symbol": symbol, "limit": 1})
            return float(data[0]["fundingRate"]) if data else 0.0
        except Exception:
            return 0.0

    def get_open_interest(self, symbol: str) -> float:
        """Returns open interest in contracts (useful for liquidity check)."""
        try:
            data = self._get("/fapi/v1/openInterest", {"symbol": symbol})
            return float(data.get("openInterest", 0))
        except Exception:
            return 0.0

    def get_symbol_info(self, symbol: str) -> dict:
        """Get futures symbol info (step size, min qty, etc)."""
        try:
            info = self._get("/fapi/v1/exchangeInfo")
            for s in info.get("symbols", []):
                if s["symbol"] == symbol:
                    return s
        except Exception:
            pass
        return {}

    def _get_step_size(self, symbol: str) -> float:
        info = self.get_symbol_info(symbol)
        for f in info.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                return float(f.get("stepSize", 0.001))
        return 0.001

    def _get_tick_size(self, symbol: str) -> float:
        """Return PRICE_FILTER tickSize for triggerPrice alignment."""
        info = self.get_symbol_info(symbol)
        for f in info.get("filters", []):
            if f.get("filterType") == "PRICE_FILTER":
                return float(f.get("tickSize", "0.01"))
        # Fallback: derive from pricePrecision
        prec = int(info.get("pricePrecision", 2))
        return 10 ** (-prec)

    def _align_price(self, price: float, tick: float) -> float:
        """Align price to tickSize (same logic as _align_qty for stepSize)."""
        from decimal import Decimal
        if tick <= 0:
            return round(price, 2)
        p = Decimal(str(price))
        t = Decimal(str(tick))
        aligned = (p / t).to_integral_value() * t
        return float(aligned)

    def _align_qty(self, qty: float, step: float) -> float:
        from decimal import Decimal
        if step <= 0 or qty <= 0:
            return 0.0
        q    = Decimal(str(qty))
        s    = Decimal(str(step))
        aligned = (q // s) * s
        return float(aligned.normalize()) if aligned > 0 else 0.0

    # ── Account / position queries ────────────────────────────────────────────

    def get_account_info(self) -> dict:
        return self._get("/fapi/v2/account", signed=True)

    def get_positions(self) -> List[dict]:
        """All open futures positions (non-zero positionAmt)."""
        account = self.get_account_info()
        return [p for p in account.get("positions", []) if float(p.get("positionAmt", 0)) != 0]

    def get_recent_trades(self, days: int = 3, limit: int = 50,
                          extra_symbols: List[str] = None) -> List[dict]:
        """
        Return recent closing trades from Binance userTrades (realizedPnl != 0).
        Queries tracked symbols + extra_symbols (e.g. futures_symbols from config).
        Used to populate the closed positions card in the dashboard.
        """
        if self.dry_run:
            return []
        cutoff_ms = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)
        symbols_to_check = set(extra_symbols or [])
        for pos in self._open_positions.values():
            symbols_to_check.add(pos.symbol)

        # Build entry_price lookup from in-memory open positions (available after sync)
        _entry_lookup: dict = {}
        for pos in self._open_positions.values():
            _entry_lookup[(pos.symbol, pos.side)] = {
                "entry_price": pos.entry_price,
                "sl_price":    pos.sl_price,
                "tp_price":    pos.tp_price,
            }

        results = []
        for sym in symbols_to_check:
            try:
                trades = self._get("/fapi/v1/userTrades", {
                    "symbol": sym, "startTime": str(cutoff_ms), "limit": str(limit)
                }, signed=True)
                # Group consecutive trades into open/close pairs by positionSide
                for t in trades:
                    side_raw = t.get("positionSide", "BOTH")
                    is_buyer = t.get("buyer", False)
                    qty      = float(t.get("qty", 0))
                    price    = float(t.get("price", 0))
                    pnl      = float(t.get("realizedPnl", 0))
                    ts_ms    = int(t.get("time", 0))
                    # Only include trades that closed a position (realizedPnl != 0)
                    if pnl == 0.0:
                        continue
                    side = side_raw if side_raw in ("LONG", "SHORT") else ("LONG" if is_buyer else "SHORT")
                    closed_at = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()

                    # Always derive entry_price mathematically from the trade itself.
                    # Never use _open_positions lookup: if the same symbol reopened
                    # after closing (e.g. SOL), the lookup returns the new position's
                    # entry price which equals the old exit price, giving wrong data.
                    # For SHORT: pnl = (entry - exit) * qty  →  entry = exit + pnl/qty
                    # For LONG:  pnl = (exit - entry) * qty  →  entry = exit - pnl/qty
                    entry_price = None
                    if qty > 0:
                        try:
                            if side == "SHORT":
                                entry_price = round(price + pnl / qty, 6)
                            else:
                                entry_price = round(price - pnl / qty, 6)
                        except Exception:
                            entry_price = None

                    # sl/tp from memory are reference-only (may belong to new open pos)
                    mem = _entry_lookup.get((sym, side), {})
                    sl_price = mem.get("sl_price")
                    tp_price = mem.get("tp_price")

                    results.append({
                        "symbol":      sym,
                        "side":        side,
                        "qty":         qty,
                        "entry_price": entry_price,
                        "exit_price":  price,
                        "sl_price":    sl_price,
                        "tp_price":    tp_price,
                        "pnl":         round(pnl, 4),
                        "opened_at":   None,
                        "closed_at":   closed_at,
                        "trade_type":  "futures",
                    })
            except Exception:
                pass

        results.sort(key=lambda x: x.get("closed_at", ""), reverse=True)
        return results[:limit]

    def enable_hedge_mode(self) -> bool:
        """
        Activate Binance hedge mode (dualSidePosition=true).
        Must be called before opening LONG positions to avoid netting.
        Returns True if already in hedge mode or successfully activated.
        Safe to call multiple times (idempotent).
        """
        if self.dry_run:
            print("[Futures DRY] Hedge mode enabled (simulated)")
            self._hedge_mode = True
            return True
        try:
            status = self._get("/fapi/v1/positionSide/dual", signed=True)
            if status.get("dualSidePosition") is True:
                self._hedge_mode = True
                print("[Futures] Hedge mode already active")
                return True
        except Exception as e:
            print(f"[Futures] Hedge mode check failed: {e}")
        try:
            self._post("/fapi/v1/positionSide/dual", {"dualSidePosition": "true"})
            self._hedge_mode = True
            print("[Futures] Hedge mode activated (dualSidePosition=true)")
            return True
        except Exception as e:
            err = str(e)
            if "-4067" in err or "open orders" in err.lower():
                # Binance rejects mode change when algo orders (SL/TP) exist on any symbol.
                # Cancel all open algo orders first, then retry.
                print(f"[Futures] Hedge mode blocked by open orders (-4067) — attempting algo order cleanup")
                try:
                    # Cancel regular open orders per symbol
                    for _sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT"]:
                        try:
                            self._delete("/fapi/v1/allOpenOrders", {"symbol": _sym})
                        except Exception:
                            pass
                    # Cancel ALL open algo orders (SL/TP) — correct endpoint per Binance docs
                    try:
                        _algo_resp = self._get("/fapi/v1/openAlgoOrders", signed=True)
                        _algo_list = _algo_resp if isinstance(_algo_resp, list) else _algo_resp.get("orders", _algo_resp.get("algoOrders", []))
                        for _ao in _algo_list:
                            try:
                                self._delete("/fapi/v1/algoOrder", {"symbol": _ao.get("symbol"), "algoId": str(_ao.get("algoId"))})
                            except Exception:
                                pass
                        if _algo_list:
                            print(f"[Futures] Cleaned {len(_algo_list)} open algo orders")
                    except Exception as _ae:
                        print(f"[Futures] Algo order cleanup error: {_ae}")
                    self._post("/fapi/v1/positionSide/dual", {"dualSidePosition": "true"})
                    self._hedge_mode = True
                    print("[Futures] Hedge mode activated after cleanup")
                    return True
                except Exception as e2:
                    print(f"[Futures] Hedge mode activation failed after cleanup: {e2} — running in one-way mode (LONG disabled)")
                    self._hedge_mode = False
                    return False
            print(f"[Futures] Hedge mode activation failed: {e}")
            self._hedge_mode = False
            return False

    def _sync_open_positions(self) -> None:
        """Rebuild _open_positions from Binance on startup (survives restarts)."""
        try:
            positions = self.get_positions()
            # Fix 4: fetch all active algo orders once — used to restore real SL/TP per position
            _all_algo_orders: list = []
            try:
                _algo_resp = self._get("/fapi/v1/openAlgoOrders", signed=True)
                _all_algo_orders = _algo_resp if isinstance(_algo_resp, list) else _algo_resp.get("orders", _algo_resp.get("algoOrders", []))
            except Exception as _ae:
                print(f"[Futures/Sync] openAlgoOrders fetch failed ({_ae}) — SL/TP will use entry-based defaults")
            for p in positions:
                sym = p.get("symbol", "")
                amt = float(p.get("positionAmt", 0))
                entry = float(p.get("entryPrice", 0))
                if amt == 0 or entry <= 0:
                    continue
                # Determine side from positionAmt sign AND positionSide field
                ps = p.get("positionSide", "BOTH")
                if ps == "BOTH":
                    side = "SHORT" if amt < 0 else "LONG"
                else:
                    side = ps  # LONG or SHORT from hedge mode
                qty = abs(amt)
                tick = self._get_tick_size(sym)
                if side == "SHORT":
                    sl_price = self._align_price(entry * 1.03, tick)
                    tp_price = self._align_price(entry * 0.94, tick)
                else:  # LONG
                    sl_price = self._align_price(entry * 0.97, tick)
                    tp_price = self._align_price(entry * 1.06, tick)
                # Fix 4: restore real SL/TP from _all_algo_orders (fetched once above the loop)
                _sl_order_id = None
                _tp_order_id = None
                for _o in _all_algo_orders:
                    if _o.get("symbol") != sym:
                        continue
                    _o_pos_side = _o.get("positionSide", "BOTH")
                    _o_matches = (_o_pos_side == side) or (_o_pos_side == "BOTH")
                    if not _o_matches:
                        continue
                    _o_type = _o.get("type") or _o.get("orderType", "")
                    _trigger = float(_o.get("triggerPrice", 0))
                    if _trigger <= 0:
                        continue
                    if _o_type == "STOP_MARKET":
                        sl_price = _trigger
                        _sl_order_id = str(_o.get("algoId", _o.get("orderId", "")))
                    elif _o_type == "TAKE_PROFIT_MARKET":
                        tp_price = _trigger
                        _tp_order_id = str(_o.get("algoId", _o.get("orderId", "")))
                print(f"[Futures/Sync] {sym} {side}: SL={sl_price:.4f} TP={tp_price:.4f}"
                      f"{' (from algoOrders)' if _sl_order_id or _tp_order_id else ' (entry defaults)'}")
                pos = FuturesPosition(
                    symbol=sym, side=side, qty=qty,
                    entry_price=entry, sl_price=sl_price, tp_price=tp_price,
                    order_id=f"SYNC_{sym}_{side}", sl_order_id=_sl_order_id, tp_order_id=_tp_order_id,
                )
                key = self._pos_key(sym, side)
                self._open_positions[key] = pos
                print(f"[Futures] Synced position: {side} {sym} qty={qty} entry={entry:.4f} (SL={sl_price:.4f}, TP={tp_price:.4f})")
            if self._open_positions:
                print(f"[Futures] FuturesClient initialized — synced {len(self._open_positions)} open position(s)")
            else:
                print(f"[Futures] FuturesClient initialized")
        except Exception as e:
            print(f"[Futures] FuturesClient initialized (sync failed: {e})")

    def count_open_shorts(self) -> int:
        try:
            positions = self.get_positions()
            return sum(1 for p in positions if float(p.get("positionAmt", 0)) < 0)
        except Exception:
            return sum(1 for p in self._open_positions.values() if not p.closed and p.side == "SHORT")

    def count_open_longs(self) -> int:
        try:
            positions = self.get_positions()
            return sum(1 for p in positions
                       if float(p.get("positionAmt", 0)) > 0
                       and p.get("positionSide", "BOTH") in ("LONG", "BOTH"))
        except Exception:
            return sum(1 for p in self._open_positions.values() if not p.closed and p.side == "LONG")

    def get_available_usdt(self) -> float:
        try:
            account = self.get_account_info()
            for asset in account.get("assets", []):
                if asset.get("asset") == "USDT":
                    return float(asset.get("availableBalance", 0))
        except Exception:
            pass
        return 0.0

    def get_total_wallet_usdt(self) -> float:
        """Return total USDT in futures wallet: margin + available + unrealized PnL.
        Uses walletBalance which includes committed margin, unlike availableBalance."""
        try:
            account = self.get_account_info()
            for asset in account.get("assets", []):
                if asset.get("asset") == "USDT":
                    return float(asset.get("walletBalance", 0))
        except Exception:
            pass
        return 0.0

    def transfer_to_futures(self, amount: float, spot_api_key: str = None, spot_api_secret: str = None) -> bool:
        """
        Transfer USDT from Spot wallet to Futures wallet.
        Uses /sapi/v1/futures/transfer (type=1 = SPOT→USDT-M Futures).
        Requires 'Universal Transfer' permission on the API key.
        Returns True on success.
        """
        _key    = spot_api_key    or self.api_key
        _secret = spot_api_secret or self.api_secret
        if self.dry_run:
            print(f"[Futures DRY] Transfer ${amount:.2f} USDT spot→futures")
            return True
        try:
            params = {
                "asset":     "USDT",
                "amount":    str(round(amount, 4)),
                "type":      "1",           # 1 = SPOT → USDT-M Futures
                "timestamp": int(time.time() * 1000),
            }
            query = urllib.parse.urlencode(params)
            sig   = hmac.new(_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            url   = f"https://api.binance.com/sapi/v1/futures/transfer?{query}&signature={sig}"
            req   = urllib.request.Request(
                url, data=b"", method="POST",
                headers={"X-MBX-APIKEY": _key, "Content-Type": "application/x-www-form-urlencoded"}
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                result = json.loads(r.read())
            if result.get("tranId"):
                print(f"[Futures] Transferred ${amount:.2f} USDT spot→futures (tranId={result['tranId']})")
                return True
            print(f"[Futures] Transfer failed: {result}")
            return False
        except Exception as e:
            print(f"[Futures] transfer_to_futures error: {e}")
            return False

    def transfer_to_spot(self, amount: float, spot_api_key: str = None, spot_api_secret: str = None) -> bool:
        """
        Transfer USDT from Futures wallet back to Spot wallet.
        Uses /sapi/v1/futures/transfer (type=2 = USDT-M Futures→SPOT).
        Only transfers if no open positions remain.
        Returns True on success.
        """
        _key    = spot_api_key    or self.api_key
        _secret = spot_api_secret or self.api_secret
        if self.dry_run:
            print(f"[Futures DRY] Transfer ${amount:.2f} USDT futures→spot")
            return True
        try:
            params = {
                "asset":     "USDT",
                "amount":    str(round(amount, 4)),
                "type":      "2",           # 2 = USDT-M Futures → SPOT
                "timestamp": int(time.time() * 1000),
            }
            query = urllib.parse.urlencode(params)
            sig   = hmac.new(_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            url   = f"https://api.binance.com/sapi/v1/futures/transfer?{query}&signature={sig}"
            req   = urllib.request.Request(
                url, data=b"", method="POST",
                headers={"X-MBX-APIKEY": _key, "Content-Type": "application/x-www-form-urlencoded"}
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                result = json.loads(r.read())
            if result.get("tranId"):
                print(f"[Futures] Transferred ${amount:.2f} USDT futures→spot (tranId={result['tranId']})")
                return True
            print(f"[Futures] Transfer futures→spot failed: {result}")
            return False
        except Exception as e:
            print(f"[Futures] transfer_to_spot error: {e}")
            return False

    def return_idle_funds_to_spot(self, reserve_usdt: float = 5.0,
                                  spot_api_key: str = None, spot_api_secret: str = None) -> bool:
        """
        Return excess futures balance to spot when no positions are open.
        Keeps `reserve_usdt` in futures as buffer; transfers the rest.
        Returns True if transfer executed or not needed.
        """
        if self._open_positions:
            print(f"[Futures] {len(self._open_positions)} open positions — not returning funds to spot")
            return False
        available = self.get_available_usdt()
        transferable = available - reserve_usdt
        if transferable < 1.0:
            print(f"[Futures] Balance ${available:.2f} too low to return (reserve=${reserve_usdt:.2f})")
            return False
        print(f"[Futures] Returning ${transferable:.2f} USDT futures→spot (keeping ${reserve_usdt:.2f} reserve)")
        return self.transfer_to_spot(transferable, spot_api_key, spot_api_secret)

    def check_and_fund(self, required_usdt: float, spot_api_key: str = None, spot_api_secret: str = None) -> bool:
        """
        Check futures balance and auto-transfer from spot if needed.
        Only transfers if futures balance < required_usdt.
        Returns True if futures wallet has sufficient funds after check.
        """
        available = self.get_available_usdt()
        if available >= required_usdt:
            print(f"[Futures] Wallet funded: ${available:.2f} USDT available (need ${required_usdt:.2f})")
            return True
        shortfall = required_usdt - available
        # Transfer a bit more to allow multiple positions (2× required)
        transfer_amount = round(required_usdt * 2, 2)
        print(f"[Futures] Insufficient balance: ${available:.2f} < ${required_usdt:.2f} — transferring ${transfer_amount:.2f} from spot")
        ok = self.transfer_to_futures(transfer_amount, spot_api_key, spot_api_secret)
        if ok:
            # Small delay for balance to reflect
            time.sleep(1.5)
            new_balance = self.get_available_usdt()
            print(f"[Futures] Post-transfer balance: ${new_balance:.2f} USDT")
            return new_balance >= required_usdt
        return False

    def smart_rebalance_capital(
        self,
        spot_usdt: float,
        open_shorts: int,
        max_shorts: int,
        kelly_margin: float,
        open_spot_count: int,
        kelly_spot_amount: float,
        regime: str,
        spot_api_key: str = None,
        spot_api_secret: str = None,
        min_transfer: float = 10.0,
    ) -> dict:
        """
        Bidirectional autonomous capital bridge: spot ↔ futures.
        Evaluates both wallets every cycle and transfers exactly what is needed
        to avoid missing any trading opportunity in either direction.

        Returns dict with keys: direction ('spot->futures'|'futures->spot'|'none'),
        amount, reason, success (bool), error (str|None).
        """
        result = {"direction": "none", "amount": 0.0, "reason": "", "success": True, "error": None}
        try:
            futures_usdt = self.get_available_usdt()

            # ── Compute committed margin (margin locked in open shorts) ──────────
            committed_margin = 0.0
            for pos in self._open_positions.values():
                if not pos.closed and pos.side == "SHORT":
                    try:
                        committed_margin += float(pos.margin or (pos.qty * pos.entry_price / 2.0))
                    except Exception:
                        pass
            futures_min_reserve = max(committed_margin * 1.1, 5.0)

            # ── Compute spot minimum reserve (protect existing spot positions) ──
            spot_min_reserve = max(100.0, open_spot_count * kelly_spot_amount * 0.5)

            # ── Case 1: Futures needs capital (FUTURES_SHORT, open slots exist) ─
            if regime == "FUTURES_SHORT":
                open_slots = max(0, max_shorts - open_shorts)
                if open_slots > 0:
                    needed_futures = open_slots * kelly_margin
                    shortfall = needed_futures - futures_usdt
                    if shortfall >= min_transfer:
                        transferable_from_spot = max(0.0, spot_usdt - spot_min_reserve)
                        transfer_amount = round(min(shortfall, transferable_from_spot), 2)
                        if transfer_amount >= min_transfer:
                            reason = (f"FUTURES_SHORT: {open_slots} open slot(s) × ${kelly_margin:.0f}/slot "
                                      f"= need ${needed_futures:.0f}, have ${futures_usdt:.0f} "
                                      f"→ transfer ${transfer_amount:.2f} spot→futures")
                            print(f"[CapBridge] {reason}")
                            ok = self.transfer_to_futures(transfer_amount, spot_api_key, spot_api_secret)
                            result["direction"] = "spot->futures"
                            result["amount"] = transfer_amount
                            result["reason"] = reason
                            result["success"] = ok
                            if ok:
                                time.sleep(1.0)
                                new_bal = self.get_available_usdt()
                                print(f"[CapBridge] Post-transfer futures balance: ${new_bal:.2f} USDT")
                            else:
                                err_msg = "transfer_to_futures returned False"
                                result["error"] = err_msg
                                print(f"[CapBridge] ERROR: spot→futures transfer failed — {err_msg}")
                            return result
                        else:
                            print(f"[CapBridge] spot→futures: need ${shortfall:.2f} but only ${transferable_from_spot:.2f} "
                                  f"transferable (spot_min_reserve=${spot_min_reserve:.0f}) — skipping")
                    else:
                        if shortfall > 0:
                            print(f"[CapBridge] futures shortfall ${shortfall:.2f} < min_transfer ${min_transfer:.0f} — skipping")

            # ── Case 2: Spot needs capital (non-bear regime, spot has open slots) ─
            elif regime in ("SPOT_BULL", "RANGE_SCALP"):
                from config import get_settings as _gs
                try:
                    _s = _gs()
                    _max_spot = getattr(_s, 'max_positions', 10)
                except Exception:
                    _max_spot = 10
                spot_open_slots = max(0, _max_spot - open_spot_count)
                if spot_open_slots > 0:
                    needed_spot = spot_open_slots * kelly_spot_amount
                    spot_shortfall = needed_spot - spot_usdt
                    if spot_shortfall >= min_transfer:
                        futures_excess = max(0.0, futures_usdt - futures_min_reserve)
                        transfer_amount = round(min(spot_shortfall, futures_excess), 2)
                        if transfer_amount >= min_transfer:
                            reason = (f"{regime}: {spot_open_slots} spot slot(s) × ${kelly_spot_amount:.0f}/slot "
                                      f"= need ${needed_spot:.0f}, have ${spot_usdt:.0f} "
                                      f"→ transfer ${transfer_amount:.2f} futures→spot")
                            print(f"[CapBridge] {reason}")
                            ok = self.transfer_to_spot(transfer_amount, spot_api_key, spot_api_secret)
                            result["direction"] = "futures->spot"
                            result["amount"] = transfer_amount
                            result["reason"] = reason
                            result["success"] = ok
                            if ok:
                                time.sleep(1.0)
                                print(f"[CapBridge] Post-transfer: spot USDT ~${spot_usdt + transfer_amount:.2f}")
                            else:
                                err_msg = "transfer_to_spot returned False"
                                result["error"] = err_msg
                                print(f"[CapBridge] ERROR: futures→spot transfer failed — {err_msg}")
                            return result

            # ── Case 3: Idle futures capital — no open shorts, return excess ────
            if open_shorts == 0 and regime not in ("FUTURES_SHORT", "PRESERVATION"):
                excess = futures_usdt - futures_min_reserve
                if excess >= min_transfer:
                    reason = f"Idle futures capital ${excess:.2f} (no open shorts, regime={regime}) → returning to spot"
                    print(f"[CapBridge] {reason}")
                    ok = self.transfer_to_spot(round(excess, 2), spot_api_key, spot_api_secret)
                    result["direction"] = "futures->spot"
                    result["amount"] = round(excess, 2)
                    result["reason"] = reason
                    result["success"] = ok
                    if not ok:
                        err_msg = "transfer_to_spot returned False (idle return)"
                        result["error"] = err_msg
                        print(f"[CapBridge] ERROR: idle futures return failed — {err_msg}")
                    return result

            if result["direction"] == "none":
                print(f"[CapBridge] No transfer needed — futures=${futures_usdt:.2f}, spot=${spot_usdt:.2f}, "
                      f"regime={regime}, open_shorts={open_shorts}/{max_shorts}")

        except Exception as e:
            result["success"] = False
            result["error"] = str(e)
            print(f"[CapBridge] ERROR: unexpected exception in smart_rebalance_capital: {e}")

        return result

    # ── Leverage management ───────────────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int = LEVERAGE_MAX) -> bool:
        """Set leverage for symbol. Hard-capped at LEVERAGE_MAX=2."""
        leverage = min(leverage, LEVERAGE_MAX)
        if symbol in self._leverage_set:
            return True
        if self.dry_run:
            print(f"[Futures DRY] Set leverage {symbol} × {leverage}")
            self._leverage_set.add(symbol)
            return True
        try:
            self._post("/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})
            self._leverage_set.add(symbol)
            print(f"[Futures] Leverage set: {symbol} × {leverage}")
            return True
        except Exception as e:
            print(f"[Futures] Set leverage failed {symbol}: {e}")
            return False

    def set_isolated_margin(self, symbol: str) -> bool:
        """Set margin type to ISOLATED for controlled risk."""
        if self.dry_run:
            return True
        try:
            self._post("/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"})
            return True
        except Exception as e:
            err = str(e)
            if ("No need to change margin type" in err or "-4046" in err
                    or "already" in err.lower() or "-4047" in err):
                return True  # already isolated, or open orders block change — not an error
            print(f"[Futures] Set isolated margin failed {symbol}: {e}")
            return False

    # ── Pre-entry safety checks ───────────────────────────────────────────────

    def _pre_entry_checks(self, symbol: str, usdt_amount: float, side: str = "SHORT") -> Tuple[bool, str]:
        """Run all safety checks before opening a position. Returns (ok, reason)."""
        side = side.upper()

        # 1. Max open positions per side
        if side == "SHORT":
            n = self.count_open_shorts()
            if n >= self.max_shorts:
                return False, f"MAX_SHORTS reached ({n}/{self.max_shorts})"
        else:  # LONG
            n = self.count_open_longs()
            if n >= self.max_longs:
                return False, f"MAX_LONGS reached ({n}/{self.max_longs})"

        # 2. Already have same-side position on this symbol
        key = self._pos_key(symbol, side)
        if key in self._open_positions and not self._open_positions[key].closed:
            return False, f"Already have open {side} on {symbol}"

        # 2b. LARGE_CAP category cap — prevents opening >max_largecap_shorts correlated LARGE_CAPs simultaneously
        if side == "SHORT" and symbol in self.largecap_symbols:
            n_lc = sum(1 for k, p in self._open_positions.items()
                       if not p.closed and p.side == "SHORT" and p.symbol in self.largecap_symbols)
            if n_lc >= self.max_largecap_shorts:
                return False, (f"LARGECAP_CAP: {n_lc}/{self.max_largecap_shorts} large-cap shorts open "
                               f"({', '.join(p.symbol for p in self._open_positions.values() if not p.closed and p.side=='SHORT' and p.symbol in self.largecap_symbols)}) "
                               f"— prefer MID-CAP slot for {symbol}")

        # 3. Funding rate check (inverted logic for LONG vs SHORT)
        funding = self.get_funding_rate(symbol)
        if side == "SHORT" and funding > FUNDING_LIMIT:
            return False, f"Funding rate {funding:.4%} > {FUNDING_LIMIT:.4%} — skip (overleveraged longs)"
        # For LONG: very negative funding = shorts very apalancados = dangerous for longs
        if side == "LONG" and funding < -FUNDING_LIMIT:
            return False, f"Funding rate {funding:.4%} < -{FUNDING_LIMIT:.4%} — skip (overleveraged shorts)"

        # 4. Available margin
        if not self.dry_run:
            available = self.get_available_usdt()
            if available < usdt_amount:
                return False, f"Insufficient futures margin: ${available:.2f} < ${usdt_amount:.2f}"

        return True, ""

    # ── Position management ───────────────────────────────────────────────────

    def open_short(
        self,
        symbol: str,
        usdt_amount: float,
        sl_pct: float = 3.0,   # % above entry — stop loss
        tp_pct: float = 7.0,   # % below entry — take profit
        leverage: int = LEVERAGE_MAX,
    ) -> Optional[FuturesPosition]:
        """
        Open a SHORT position with mandatory SL and TP orders.
        Returns FuturesPosition or None if safety checks fail.

        Risk math at 2x leverage:
          SL 3% move → 6% loss on margin
          TP 7% move → 14% gain on margin
        """
        ok, reason = self._pre_entry_checks(symbol, usdt_amount, side="SHORT")
        if not ok:
            print(f"[Futures] SHORT {symbol} blocked: {reason}")
            return None

        # Configure leverage + isolated margin
        self.set_isolated_margin(symbol)
        self.set_leverage(symbol, min(leverage, LEVERAGE_MAX))

        price    = self.get_price(symbol)
        notional = usdt_amount * min(leverage, LEVERAGE_MAX)  # leveraged notional
        step     = self._get_step_size(symbol)
        qty      = self._align_qty(notional / price, step)

        if qty <= 0:
            print(f"[Futures] SHORT {symbol}: qty=0 after alignment (usdt={usdt_amount}, price={price})")
            return None

        tick = self._get_tick_size(symbol)
        sl_price = self._align_price(price * (1 + sl_pct / 100), tick)
        tp_price = self._align_price(price * (1 - tp_pct / 100), tick)

        print(f"[Futures] Opening SHORT {symbol}: qty={qty}, entry~{price:.4f}, "
              f"SL={sl_price:.4f} (+{sl_pct}%), TP={tp_price:.4f} (-{tp_pct}%), "
              f"leverage={leverage}×, notional=${notional:.2f}")

        if self.dry_run:
            pos = FuturesPosition(
                symbol=symbol, side="SHORT", qty=qty,
                entry_price=price, sl_price=sl_price, tp_price=tp_price,
                order_id=f"DRY_{symbol}_{int(time.time())}",
                sl_order_id=None, tp_order_id=None,
            )
            self._open_positions[self._pos_key(symbol, "SHORT")] = pos
            return pos

        # ── Live order execution ──────────────────────────────────────────────
        try:
            # 1. Market SHORT order (hedge mode requires positionSide=SHORT)
            entry_order = self._post("/fapi/v1/order", {
                "symbol": symbol, "side": "SELL", "type": "MARKET",
                "quantity": str(qty), "positionSide": "SHORT",
            })
            order_id    = str(entry_order.get("orderId", "?"))
            filled_price = float(entry_order.get("avgPrice", price)) or price

            # Recalculate SL/TP based on actual fill price
            sl_price = round(filled_price * (1 + sl_pct / 100), 6)
            tp_price = round(filled_price * (1 - tp_pct / 100), 6)

            # 2. Stop-loss order via Algo Orders API (required since Binance API change 2025-12-09)
            # POST /fapi/v1/algoOrder with algoType=CONDITIONAL + orderType=STOP_MARKET
            # For SHORT: BUY STOP_MARKET triggers when price rises to triggerPrice
            sl_order_id = None
            for _sl_attempt in range(2):
                try:
                    _raw_trigger = self.get_price(symbol) * (1 + sl_pct / 100) if _sl_attempt > 0 else filled_price * (1 + sl_pct / 100)
                    _sl_trigger = self._align_price(_raw_trigger, tick)
                    sl_order = self._post("/fapi/v1/algoOrder", {
                        "symbol": symbol, "side": "BUY",
                        "algoType": "CONDITIONAL", "type": "STOP_MARKET",
                        "quantity": str(qty), "triggerPrice": str(_sl_trigger),
                        "positionSide": "SHORT", "timeInForce": "GTC",
                    })
                    sl_order_id = str(sl_order.get("algoId", sl_order.get("orderId", "")))
                    sl_price = _sl_trigger
                    print(f"[Futures] SL algo order placed: triggerPrice={sl_price} (algoId={sl_order_id})")
                    break
                except Exception as se:
                    if _sl_attempt == 0:
                        print(f"[Futures] SL order attempt 1 failed for {symbol}: {se} — retrying with refreshed price")
                        time.sleep(0.5)
                    else:
                        print(f"[Futures] SL order failed for {symbol}: {se} — position open WITHOUT SL, closing immediately")
                        self._post("/fapi/v1/order", {
                            "symbol": symbol, "side": "BUY", "type": "MARKET",
                            "quantity": str(qty), "positionSide": "SHORT",
                        })
                        return None

            # 3. Take-profit order via Algo Orders API
            # POST /fapi/v1/algoOrder with algoType=CONDITIONAL + orderType=TAKE_PROFIT_MARKET
            # For SHORT: BUY TAKE_PROFIT_MARKET triggers when price falls to triggerPrice
            tp_order_id = None
            try:
                tp_order = self._post("/fapi/v1/algoOrder", {
                    "symbol": symbol, "side": "BUY",
                    "algoType": "CONDITIONAL", "type": "TAKE_PROFIT_MARKET",
                    "quantity": str(qty), "triggerPrice": str(self._align_price(tp_price, tick)),
                    "positionSide": "SHORT", "timeInForce": "GTC",
                })
                tp_order_id = str(tp_order.get("algoId", tp_order.get("orderId", "")))
                print(f"[Futures] TP algo order placed: triggerPrice={tp_price:.4f} (algoId={tp_order_id})")
            except Exception as te:
                print(f"[Futures] TP order failed for {symbol}: {te} — continuing with SL only")

            pos = FuturesPosition(
                symbol=symbol, side="SHORT", qty=qty,
                entry_price=filled_price, sl_price=sl_price, tp_price=tp_price,
                order_id=order_id, sl_order_id=sl_order_id, tp_order_id=tp_order_id,
            )
            self._open_positions[self._pos_key(symbol, "SHORT")] = pos
            print(f"[Futures] SHORT {symbol} OPEN: entry={filled_price:.4f}, "
                  f"SL={sl_price:.4f}, TP={tp_price:.4f}")
            return pos

        except Exception as e:
            print(f"[Futures] Failed to open SHORT {symbol}: {e}")
            return None

    def open_long(
        self,
        symbol: str,
        usdt_amount: float,
        sl_pct: float = 2.5,   # % below entry — stop loss
        tp_pct: float = 5.0,   # % above entry — take profit
        leverage: int = LEVERAGE_MAX,
    ) -> Optional[FuturesPosition]:
        """
        Open a LONG position with mandatory SL and TP orders.
        Requires hedge mode enabled (enable_hedge_mode() called first).

        Risk math at 2x leverage:
          SL 2.5% move down → 5% loss on margin
          TP 5.0% move up   → 10% gain on margin
        """
        if not self._hedge_mode:
            if not self.enable_hedge_mode():
                print(f"[Futures] LONG {symbol} blocked: hedge mode not available")
                return None

        ok, reason = self._pre_entry_checks(symbol, usdt_amount, side="LONG")
        if not ok:
            print(f"[Futures] LONG {symbol} blocked: {reason}")
            return None

        self.set_isolated_margin(symbol)
        self.set_leverage(symbol, min(leverage, LEVERAGE_MAX))

        price    = self.get_price(symbol)
        notional = usdt_amount * min(leverage, LEVERAGE_MAX)
        step     = self._get_step_size(symbol)
        qty      = self._align_qty(notional / price, step)

        if qty <= 0:
            print(f"[Futures] LONG {symbol}: qty=0 after alignment")
            return None

        tick     = self._get_tick_size(symbol)
        sl_price = self._align_price(price * (1 - sl_pct / 100), tick)  # below entry
        tp_price = self._align_price(price * (1 + tp_pct / 100), tick)  # above entry

        print(f"[Futures] Opening LONG {symbol}: qty={qty}, entry~{price:.4f}, "
              f"SL={sl_price:.4f} (-{sl_pct}%), TP={tp_price:.4f} (+{tp_pct}%), "
              f"leverage={leverage}×, notional=${notional:.2f}")

        if self.dry_run:
            pos = FuturesPosition(
                symbol=symbol, side="LONG", qty=qty,
                entry_price=price, sl_price=sl_price, tp_price=tp_price,
                order_id=f"DRY_LONG_{symbol}_{int(time.time())}",
                sl_order_id=None, tp_order_id=None,
            )
            self._open_positions[self._pos_key(symbol, "LONG")] = pos
            return pos

        try:
            # 1. Market LONG (BUY) order with positionSide=LONG for hedge mode
            entry_order = self._post("/fapi/v1/order", {
                "symbol": symbol, "side": "BUY", "type": "MARKET",
                "quantity": str(qty), "positionSide": "LONG",
            })
            order_id    = str(entry_order.get("orderId", "?"))
            filled_price = float(entry_order.get("avgPrice", price)) or price

            sl_price = self._align_price(filled_price * (1 - sl_pct / 100), tick)
            tp_price = self._align_price(filled_price * (1 + tp_pct / 100), tick)

            # 2. Stop-loss: SELL STOP_MARKET with positionSide=LONG
            sl_order_id = None
            try:
                sl_order = self._post("/fapi/v1/algoOrder", {
                    "symbol": symbol, "side": "SELL",
                    "algoType": "CONDITIONAL", "type": "STOP_MARKET",
                    "quantity": str(qty), "triggerPrice": str(sl_price),
                    "positionSide": "LONG", "timeInForce": "GTC",
                })
                sl_order_id = str(sl_order.get("algoId", sl_order.get("orderId", "")))
                print(f"[Futures] LONG SL placed: triggerPrice={sl_price:.4f} (algoId={sl_order_id})")
            except Exception as se:
                print(f"[Futures] LONG SL order failed {symbol}: {se} — closing immediately")
                self._post("/fapi/v1/order", {
                    "symbol": symbol, "side": "SELL", "type": "MARKET",
                    "quantity": str(qty), "positionSide": "LONG",
                })
                return None

            # 3. Take-profit: SELL TAKE_PROFIT_MARKET with positionSide=LONG
            tp_order_id = None
            try:
                tp_order = self._post("/fapi/v1/algoOrder", {
                    "symbol": symbol, "side": "SELL",
                    "algoType": "CONDITIONAL", "type": "TAKE_PROFIT_MARKET",
                    "quantity": str(qty), "triggerPrice": str(tp_price),
                    "positionSide": "LONG", "timeInForce": "GTC",
                })
                tp_order_id = str(tp_order.get("algoId", tp_order.get("orderId", "")))
                print(f"[Futures] LONG TP placed: triggerPrice={tp_price:.4f} (algoId={tp_order_id})")
            except Exception as te:
                print(f"[Futures] LONG TP order failed {symbol}: {te} — continuing with SL only")

            pos = FuturesPosition(
                symbol=symbol, side="LONG", qty=qty,
                entry_price=filled_price, sl_price=sl_price, tp_price=tp_price,
                order_id=order_id, sl_order_id=sl_order_id, tp_order_id=tp_order_id,
            )
            self._open_positions[self._pos_key(symbol, "LONG")] = pos
            print(f"[Futures] LONG {symbol} OPEN: entry={filled_price:.4f}, "
                  f"SL={sl_price:.4f}, TP={tp_price:.4f}")
            return pos

        except Exception as e:
            print(f"[Futures] Failed to open LONG {symbol}: {e}")
            return None

    def close_long(self, symbol: str, qty: float = None, reason: str = "manual") -> Optional[dict]:
        """Close a LONG position with a SELL MARKET order."""
        key = self._pos_key(symbol, "LONG")
        pos = self._open_positions.get(key)
        close_qty = qty or (pos.qty if pos else 0)
        if close_qty <= 0:
            return None
        current_price = self.get_price(symbol)
        pnl = (current_price - pos.entry_price) * close_qty if pos else 0.0
        print(f"[Futures] Closing LONG {symbol}: entry={pos.entry_price if pos else '?':.4f}, "
              f"current={current_price:.4f}, PnL~${pnl:.2f}, reason={reason}")
        if self.dry_run:
            if pos:
                pos.closed = True; pos.closed_at = datetime.now(timezone.utc); pos.exit_price = current_price; pos.pnl = pnl
            return {"simulated": True, "symbol": symbol, "pnl": pnl}
        if pos:
            for oid in [pos.sl_order_id, pos.tp_order_id]:
                if oid:
                    try:
                        self._delete("/fapi/v1/algoOrder", {"symbol": symbol, "algoId": oid})
                    except Exception:
                        pass
        try:
            step = self._get_step_size(symbol)
            aligned_qty = self._align_qty(close_qty, step)
            result = self._post("/fapi/v1/order", {
                "symbol": symbol, "side": "SELL", "type": "MARKET",
                "quantity": str(aligned_qty), "positionSide": "LONG",
            })
            if pos:
                pos.closed = True; pos.closed_at = datetime.now(timezone.utc); pos.exit_price = current_price; pos.pnl = pnl
            print(f"[Futures] LONG {symbol} CLOSED: PnL~${pnl:.2f}")
            return result
        except Exception as e:
            print(f"[Futures] close_long {symbol} failed: {e}")
            return None

    def close_short(self, symbol: str, qty: float = None, reason: str = "manual") -> Optional[dict]:
        """
        Close a SHORT position with a BUY MARKET order.
        If qty is None, uses the tracked position qty.
        Cancels any open SL/TP orders before closing.
        """
        key = self._pos_key(symbol, "SHORT")
        pos = self._open_positions.get(key) or self._open_positions.get(symbol)  # legacy compat
        close_qty = qty or (pos.qty if pos else 0)

        if close_qty <= 0:
            print(f"[Futures] close_short {symbol}: no qty to close")
            return None

        current_price = self.get_price(symbol)
        pnl = (pos.entry_price - current_price) * close_qty if pos else 0.0

        print(f"[Futures] Closing SHORT {symbol}: qty={close_qty}, "
              f"entry={pos.entry_price if pos else '?':.4f}, current={current_price:.4f}, "
              f"PnL~${pnl:.2f}, reason={reason}")

        if self.dry_run:
            if pos:
                pos.closed     = True
                pos.closed_at  = datetime.now(timezone.utc)
                pos.exit_price = current_price
                pos.pnl        = pnl
            return {"simulated": True, "symbol": symbol, "pnl": pnl}

        # Cancel open SL/TP algo orders first
        if pos:
            for oid in [pos.sl_order_id, pos.tp_order_id]:
                if oid:
                    try:
                        self._delete("/fapi/v1/algoOrder", {"symbol": symbol, "algoId": oid})
                    except Exception:
                        pass

        try:
            step = self._get_step_size(symbol)
            from decimal import Decimal
            aligned_qty = self._align_qty(close_qty, step)
            result = self._post("/fapi/v1/order", {
                "symbol": symbol, "side": "BUY", "type": "MARKET",
                "quantity": str(aligned_qty), "positionSide": "SHORT",
            })
            if pos:
                pos.closed     = True
                pos.closed_at  = datetime.now(timezone.utc)
                pos.exit_price = current_price
                pos.pnl        = pnl
            print(f"[Futures] SHORT {symbol} CLOSED: PnL~${pnl:.2f}")
            return result
        except Exception as e:
            print(f"[Futures] close_short {symbol} failed: {e}")
            return None

    def update_sl_tp(
        self,
        symbol: str,
        side: str,
        new_sl: Optional[float] = None,
        new_tp: Optional[float] = None,
    ) -> bool:
        """
        Move stop-loss and/or take-profit on a live futures position.

        Process:
          1. Cancel the existing algo orders for SL/TP.
          2. Place new algo orders at the updated levels.
          3. Update in-memory FuturesPosition state.

        Returns True on success, False if anything failed (position stays managed
        by the old levels in Binance — silent failure does NOT close the position).

        IMPORTANT: Binance may reject the cancel with -2011 (order already filled)
        if the SL/TP triggered between our cycles.  This is handled gracefully —
        we proceed to place the new order (it will fail with no-position error and
        check_positions() will detect the closure on the next cycle).
        """
        key = self._pos_key(symbol, side.upper())
        pos = self._open_positions.get(key)
        if not pos or pos.closed:
            return False

        if self.dry_run:
            if new_sl is not None:
                pos.sl_price = new_sl
            if new_tp is not None:
                pos.tp_price = new_tp
            print(f"[Futures/Trail] DRY update {symbol} {side}: SL={new_sl} TP={new_tp}")
            return True

        tick = self._get_tick_size(symbol)
        success = True

        # ── Cancel existing SL algo order ─────────────────────────────────────
        if new_sl is not None and pos.sl_order_id:
            try:
                self._delete("/fapi/v1/algoOrder", {"symbol": symbol, "algoId": pos.sl_order_id})
            except Exception as _e:
                _emsg = str(_e)
                if "-2011" in _emsg or "Unknown order" in _emsg:
                    print(f"[Futures/Trail] SL cancel {symbol}: order already gone (-2011), treating as closed")
                    pos.closed = True
                    return False
                print(f"[Futures/Trail] SL cancel {symbol}: {_e} — placing new order anyway")

        # ── Cancel existing TP algo order ─────────────────────────────────────
        if new_tp is not None and pos.tp_order_id:
            try:
                self._delete("/fapi/v1/algoOrder", {"symbol": symbol, "algoId": pos.tp_order_id})
            except Exception as _e:
                _emsg = str(_e)
                if "-2011" in _emsg or "Unknown order" in _emsg:
                    print(f"[Futures/Trail] TP cancel {symbol}: order already gone (-2011)")
                else:
                    print(f"[Futures/Trail] TP cancel {symbol}: {_e} — placing new order anyway")

        # ── Place new SL algo order ───────────────────────────────────────────
        if new_sl is not None:
            try:
                _sl_aligned = self._align_price(new_sl, tick)
                _sl_side   = "BUY" if side.upper() == "SHORT" else "SELL"
                _pos_side  = side.upper()
                _sl_type   = "STOP_MARKET"
                _sl_order  = self._post("/fapi/v1/algoOrder", {
                    "symbol": symbol, "side": _sl_side,
                    "algoType": "CONDITIONAL", "type": _sl_type,
                    "quantity": str(pos.qty), "triggerPrice": str(_sl_aligned),
                    "positionSide": _pos_side, "timeInForce": "GTC",
                })
                pos.sl_order_id = str(_sl_order.get("algoId", _sl_order.get("orderId", "")))
                pos.sl_price    = _sl_aligned
                print(f"[Futures/Trail] {symbol} {side}: SL moved → {_sl_aligned:.4f} (algoId={pos.sl_order_id})")
            except Exception as _e:
                print(f"[Futures/Trail] {symbol}: failed to place new SL order: {_e}")
                success = False

        # ── Place new TP algo order ───────────────────────────────────────────
        if new_tp is not None:
            try:
                _tp_aligned = self._align_price(new_tp, tick)
                _tp_side   = "BUY" if side.upper() == "SHORT" else "SELL"
                _pos_side  = side.upper()
                _tp_type   = "TAKE_PROFIT_MARKET"
                _tp_order  = self._post("/fapi/v1/algoOrder", {
                    "symbol": symbol, "side": _tp_side,
                    "algoType": "CONDITIONAL", "type": _tp_type,
                    "quantity": str(pos.qty), "triggerPrice": str(_tp_aligned),
                    "positionSide": _pos_side, "timeInForce": "GTC",
                })
                pos.tp_order_id = str(_tp_order.get("algoId", _tp_order.get("orderId", "")))
                pos.tp_price    = _tp_aligned
                print(f"[Futures/Trail] {symbol} {side}: TP moved → {_tp_aligned:.4f} (algoId={pos.tp_order_id})")
            except Exception as _e:
                print(f"[Futures/Trail] {symbol}: failed to place new TP order: {_e}")
                success = False

        return success

    def check_positions(self, settings=None) -> List[dict]:
        """
        Check all tracked positions (LONG and SHORT) for SL/TP hits.

        Per-cycle logic (executed in order):
          1. Breakeven SL — when unrealized PnL% reaches futures_breakeven_activation_pct,
             move SL to entry price (protects capital, never reduces realized gains).
          2. Trailing stop — when unrealized PnL% reaches futures_trailing_activation_pct,
             trail the SL (SHORT: move SL down toward price; LONG: move SL up toward price)
             by futures_trailing_distance_pct.  Also trails the TP proportionally for SHORTs
             (when price drops more than TP, lower TP to lock in more profit).
          3. Smart TP EXTEND — when TP is hit and futures_smart_tp_enabled, evaluate SHORT
             momentum (RSI, vol_spike, trend_4h, success_prob).  If score >= threshold:
             EXTEND (lower TP, raise SL to current price). Otherwise CLOSE.
          4. SL hit — close immediately.

        Returns list of closed positions dicts with keys: symbol, side, reason, pnl.
        """
        closed = []

        # Read settings — use safe defaults if not provided
        _trailing_enabled      = getattr(settings, 'futures_trailing_enabled', True)      if settings else True
        _trail_act_pct        = getattr(settings, 'futures_trailing_activation_pct', 2.0) if settings else 2.0
        _trail_dist_pct       = getattr(settings, 'futures_trailing_distance_pct', 1.0)   if settings else 1.0
        _breakeven_act_pct    = getattr(settings, 'futures_breakeven_activation_pct', 1.2) if settings else 1.2
        _smart_tp             = getattr(settings, 'futures_smart_tp_enabled', True)        if settings else True
        _smart_tp_max_ext     = getattr(settings, 'futures_smart_tp_max_extensions', 2)   if settings else 2
        _smart_tp_max_pnl     = getattr(settings, 'futures_smart_tp_max_pnl_pct', 12.0)  if settings else 12.0
        _smart_tp_threshold   = getattr(settings, 'futures_smart_tp_extend_score_threshold', 3) if settings else 3
        _smart_tp_ext_pct     = getattr(settings, 'futures_smart_tp_extension_pct', 2.0)  if settings else 2.0

        for key, pos in list(self._open_positions.items()):
            if pos.closed:
                continue
            symbol = pos.symbol
            try:
                price   = self.get_price(symbol)
                unr     = pos.unrealized_pnl(price)
                entry   = pos.entry_price
                pnl_pct = unr / (entry * pos.qty) if entry > 0 and pos.qty > 0 else 0.0

                print(f"[Futures] {symbol} {pos.side} check: price={price:.4f}, "
                      f"entry={entry:.4f}, unr_pnl=${unr:.2f} ({pnl_pct*100:.2f}%), "
                      f"SL={pos.sl_price:.4f}, TP={pos.tp_price:.4f}")

                # ── 1. Breakeven SL ─────────────────────────────────────────────
                if _trailing_enabled and pnl_pct >= _breakeven_act_pct / 100.0:
                    if not pos._smart_meta.get('breakeven_done'):
                        _be_ok = False
                        if pos.side == "SHORT" and entry < pos.sl_price:
                            # SHORT: SL is above entry. Move SL to entry (price must rise to entry to stop out)
                            _be_ok = self.update_sl_tp(symbol, pos.side, new_sl=entry)
                        elif pos.side == "LONG" and entry > pos.sl_price:
                            # LONG: SL is below entry. Move SL to entry
                            _be_ok = self.update_sl_tp(symbol, pos.side, new_sl=entry)
                        if _be_ok:
                            pos._smart_meta['breakeven_done'] = True
                            print(f"[Futures/Breakeven] {symbol} {pos.side}: SL → entry={entry:.4f} "
                                  f"(PnL={pnl_pct*100:.2f}% >= {_breakeven_act_pct}%)")

                # ── 2. Trailing stop ────────────────────────────────────────────
                if _trailing_enabled and pnl_pct >= _trail_act_pct / 100.0:
                    _trail_dist = _trail_dist_pct / 100.0
                    tick = self._get_tick_size(symbol)

                    if pos.side == "SHORT":
                        # SHORT profits when price falls. "Best price" = lowest seen.
                        # Trailing SL = best_price * (1 + trail_dist) — moves down as price falls.
                        # Trailing TP = best_price * (1 - trail_dist/2) — trails TP downward.
                        _best_price = pos._smart_meta.get('trail_best_price', price)

                        # Fix 3: Initialize trail_best_price from current SL (not from price).
                        # After a restart, SL may already be trailing-moved (Fix 4 restores it).
                        # Inferring best_price from SL prevents resetting progress: best = SL / (1 + dist)
                        if 'trail_best_price' not in pos._smart_meta:
                            _inferred_best = pos.sl_price / (1.0 + _trail_dist)
                            # Only use inferred value if it's lower than current price (valid for SHORT)
                            # and lower than entry (confirms trailing already happened)
                            if _inferred_best < pos.entry_price and _inferred_best < price:
                                pos._smart_meta['trail_best_price'] = _inferred_best
                                _best_price = _inferred_best
                                print(f"[Futures/Trail] {symbol} SHORT: trail_best_price inferred from SL={pos.sl_price:.4f} → best={_inferred_best:.4f} (PnL={pnl_pct*100:.2f}%)")
                            else:
                                pos._smart_meta['trail_best_price'] = price
                                _best_price = price
                                print(f"[Futures/Trail] {symbol} SHORT: Trailing activated at {price:.4f} (PnL={pnl_pct*100:.2f}%)")

                        if price < _best_price:  # new low = new best for SHORT
                            _best_price = price
                            pos._smart_meta['trail_best_price'] = _best_price
                            # New SL = best_price * (1 + trail_dist) — stays above best price
                            _new_sl = self._align_price(_best_price * (1.0 + _trail_dist), tick)
                            # Only lower SL (SHORT: lower sl_price = tighter stop = more profit protected)
                            if _new_sl < pos.sl_price:
                                self.update_sl_tp(symbol, pos.side, new_sl=_new_sl)
                                print(f"[Futures/Trail] {symbol} SHORT: SL trailed down → {_new_sl:.4f} "
                                      f"(best={_best_price:.4f}, dist={_trail_dist_pct}%)")
                            # Also trail TP downward (lock in more profit on deep moves)
                            _new_tp = self._align_price(_best_price * (1.0 - _trail_dist / 2.0), tick)
                            if _new_tp < pos.tp_price:  # only move TP toward more profit
                                self.update_sl_tp(symbol, pos.side, new_tp=_new_tp)
                                print(f"[Futures/Trail] {symbol} SHORT: TP trailed → {_new_tp:.4f}")

                    elif pos.side == "LONG":
                        # LONG profits when price rises. "Best price" = highest seen.
                        _best_price = pos._smart_meta.get('trail_best_price', price)
                        # Fix 3 (LONG): infer trail_best_price from SL on first activation after restart
                        # For LONG: SL = best_price * (1 - dist) → best = SL / (1 - dist)
                        if 'trail_best_price' not in pos._smart_meta:
                            _inferred_best = pos.sl_price / (1.0 - _trail_dist) if (1.0 - _trail_dist) > 0 else price
                            if _inferred_best > pos.entry_price and _inferred_best > price:
                                pos._smart_meta['trail_best_price'] = _inferred_best
                                _best_price = _inferred_best
                                print(f"[Futures/Trail] {symbol} LONG: trail_best_price inferred from SL={pos.sl_price:.4f} → best={_inferred_best:.4f} (PnL={pnl_pct*100:.2f}%)")
                            else:
                                pos._smart_meta['trail_best_price'] = price
                                _best_price = price
                                print(f"[Futures/Trail] {symbol} LONG: Trailing activated at {price:.4f} (PnL={pnl_pct*100:.2f}%)")
                        if price > _best_price:  # new high = new best for LONG
                            _best_price = price
                            pos._smart_meta['trail_best_price'] = _best_price
                            _new_sl = self._align_price(_best_price * (1.0 - _trail_dist), tick)
                            if _new_sl > pos.sl_price:  # only raise SL (LONG: protect more)
                                self.update_sl_tp(symbol, pos.side, new_sl=_new_sl)
                                print(f"[Futures/Trail] {symbol} LONG: SL trailed up → {_new_sl:.4f}")

                # ── Re-read pos.sl_price / pos.tp_price (may have changed above) ──
                # Note: pos is a reference — updates in update_sl_tp() are reflected here.

                # ── 3. Check SL hit ─────────────────────────────────────────────
                if pos.is_sl_hit(price):
                    result = (self.close_long(symbol, reason="stop_loss")
                              if pos.side == "LONG"
                              else self.close_short(symbol, reason="stop_loss"))
                    if result:
                        closed.append({"symbol": symbol, "side": pos.side,
                                       "reason": "stop_loss", "pnl": pos.pnl or unr})
                    continue  # done with this position

                # ── 4. Check TP hit — Smart TP EXTEND or close ───────────────
                if pos.is_tp_hit(price):
                    _ext_count = pos._smart_meta.get('tp_extensions_count', 0)
                    _can_extend = (_smart_tp
                                   and _ext_count < _smart_tp_max_ext
                                   and pnl_pct < _smart_tp_max_pnl / 100.0)

                    if _can_extend:
                        try:
                            _pos_size_real = float(pos.qty) * float(pos.entry_price)
                            _score, _details = self._evaluate_short_momentum(symbol, settings, position_size_usdt=_pos_size_real)
                        except Exception as _me:
                            print(f"[Futures/SmartTP] {symbol}: momentum eval failed: {_me} — closing")
                            _score, _details = -1, {}

                        print(f"[Futures/SmartTP] {symbol} {pos.side}: PnL={pnl_pct*100:.2f}% "
                              f"score={_score}/4 ext={_ext_count}/{_smart_tp_max_ext} "
                              f"→ {'EXTEND' if _score >= _smart_tp_threshold else 'CLOSE'}")

                        if _score >= _smart_tp_threshold:
                            tick = self._get_tick_size(symbol)
                            if pos.side == "SHORT":
                                # Extend: lower TP (SHORT profits on price drop)
                                _new_tp = self._align_price(price * (1.0 - _smart_tp_ext_pct / 100.0), tick)
                                # Raise SL to just above current price (lock in gains)
                                _new_sl = self._align_price(price * 1.005, tick)
                                # Only raise SL if new_sl is lower than current sl (SHORT: lower sl_price = tighter stop)
                                _update_sl = _new_sl < pos.sl_price
                            else:  # LONG
                                _new_tp = self._align_price(price * (1.0 + _smart_tp_ext_pct / 100.0), tick)
                                _new_sl = self._align_price(price * 0.995, tick)
                                _update_sl = _new_sl > pos.sl_price

                            pos._smart_meta['tp_extensions_count'] = _ext_count + 1

                            self.update_sl_tp(
                                symbol, pos.side,
                                new_sl=_new_sl if _update_sl else None,
                                new_tp=_new_tp
                            )
                            print(f"[Futures/SmartTP] EXTEND {symbol} {pos.side}: "
                                  f"TP → {pos.tp_price:.4f}, SL → {pos.sl_price:.4f} "
                                  f"(ext {_ext_count+1}/{_smart_tp_max_ext})")
                            continue  # do NOT close — let it run

                    # Not extending — close the position
                    _close_fn = self.close_long if pos.side == "LONG" else self.close_short
                    result = _close_fn(symbol, reason="take_profit")
                    if result:
                        _reason = (f"smart_tp_take_profit (ext={_ext_count})" if _ext_count > 0
                                   else "take_profit")
                        closed.append({"symbol": symbol, "side": pos.side,
                                       "reason": _reason, "pnl": pos.pnl or unr})

            except Exception as e:
                print(f"[Futures] check_positions {key}: {e}")
        return closed

    def _evaluate_short_momentum(self, symbol: str, settings, position_size_usdt: float = None) -> tuple:
        """
        Evaluate whether the current SHORT momentum justifies extending the TP.

        For a SHORT to be extended:
          - vol_spike < 1.0 (low buying volume — sellers still in control)
          - RSI 4h > 60 (overbought / still elevated — shorts benefit)
          - trend_4h == DOWN (confirmed bear)
          - success_prob < 0.45 (low probability of reversal)

        Score: 1 point per bearish SHORT condition (max 4).
        Returns (score: int, details: dict). -1 score = evaluation failed.
        """
        details = {"vol_spike": 1.0, "rsi": 50.0, "trend_4h": 0.0, "success_prob": 0.5}
        try:
            from autonomous_signals import AutonomousSignalGenerator
            from binance_client import BinanceClient as _BC

            # Re-use an existing BinanceClient if possible (avoid creating a new one)
            _bc = getattr(self, '_binance_client_ref', None)
            if _bc is None:
                return -1, details  # no client available, safe fallback

            sig_gen = AutonomousSignalGenerator(_bc)
            sig = sig_gen.analyze_symbol(symbol, interval='1h', use_multi_timeframe=True)
            ind = sig.get("indicators", {})

            vol_spike  = float(ind.get("vol_spike") or ind.get("volume_ratio") or 1.0)
            rsi        = float(ind.get("rsi") or 50.0)
            trend_4h   = float(ind.get("trend_4h") or 0.0)  # 1.0=UP, 0.0=DOWN

            # Run predictor to get success probability
            from pnl_service import PnlPredictionService
            _pnl_svc = PnlPredictionService()
            _default_size = float(getattr(settings, 'futures_usdt_per_trade', 20.0))
            _max_size = float(getattr(settings, 'max_per_trade_usdt', 150.0))
            _effective_size = min(position_size_usdt, _max_size) if position_size_usdt else _default_size
            prediction, _ = _pnl_svc.predict_for_symbol(
                symbol=symbol, client=_bc, settings=settings,
                position_size_usdt=_effective_size
            )
            success_prob = float((prediction or {}).get("success_probability", 0.5))

            details.update({
                "vol_spike": round(vol_spike, 3),
                "rsi": round(rsi, 2),
                "trend_4h": round(trend_4h, 2),
                "success_prob": round(success_prob, 4),
            })

            # Score for SHORT extension — each point = SHORT-friendly condition
            score = 0
            if vol_spike < 1.2:     score += 1  # low buying pressure
            if rsi > 55.0:          score += 1  # price still elevated (short is winning)
            if trend_4h < 0.5:      score += 1  # 4h trend is DOWN (0.0=DOWN, 1.0=UP)
            if success_prob < 0.48: score += 1  # predictor says low upside probability

            return score, details

        except Exception as _e:
            print(f"[Futures/SmartTP] Momentum eval error for {symbol}: {_e}")
            return -1, details

    def get_open_positions_summary(self) -> List[dict]:
        """Summary of all tracked open positions (LONG and SHORT) for dashboard."""
        result = []
        for key, pos in self._open_positions.items():
            if pos.closed:
                continue
            try:
                price = self.get_price(pos.symbol)
                unr   = pos.unrealized_pnl(price)
            except Exception:
                price = pos.entry_price
                unr   = 0.0
            result.append({
                "symbol":         pos.symbol,
                "side":           pos.side,
                "qty":            pos.qty,
                "entry_price":    pos.entry_price,
                "current_price":  price,
                "sl_price":       pos.sl_price,
                "tp_price":       pos.tp_price,
                "unrealized_pnl": round(unr, 4),
                "opened_at":      pos.opened_at.isoformat(),
            })
        return result
