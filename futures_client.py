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
    exit_price: Optional[float] = None
    pnl: Optional[float] = None

    def unrealized_pnl(self, current_price: float) -> float:
        """For SHORT: profit when price falls."""
        return (self.entry_price - current_price) * self.qty

    def is_sl_hit(self, current_price: float) -> bool:
        return current_price >= self.sl_price

    def is_tp_hit(self, current_price: float) -> bool:
        return current_price <= self.tp_price


class FuturesClient:
    def __init__(self, api_key: str, api_secret: str, dry_run: bool = False):
        self.api_key    = api_key
        self.api_secret = api_secret
        self.dry_run    = dry_run
        self._open_positions: Dict[str, FuturesPosition] = {}
        self._price_cache: Dict[str, Tuple[float, float]] = {}  # {symbol: (price, ts)}
        self._leverage_set: set = set()   # symbols with leverage already configured

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

    def count_open_shorts(self) -> int:
        try:
            positions = self.get_positions()
            return sum(1 for p in positions if float(p.get("positionAmt", 0)) < 0)
        except Exception:
            return len([p for p in self._open_positions.values() if not p.closed])

    def get_available_usdt(self) -> float:
        try:
            account = self.get_account_info()
            for asset in account.get("assets", []):
                if asset.get("asset") == "USDT":
                    return float(asset.get("availableBalance", 0))
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
            if "No need to change margin type" in err or "-4046" in err or "already" in err.lower():
                return True  # already isolated — not an error
            print(f"[Futures] Set isolated margin failed {symbol}: {e}")
            return False

    # ── Pre-entry safety checks ───────────────────────────────────────────────

    def _pre_entry_checks(self, symbol: str, usdt_amount: float) -> Tuple[bool, str]:
        """Run all safety checks before opening a short. Returns (ok, reason)."""
        # 1. Max open shorts
        n_shorts = self.count_open_shorts()
        if n_shorts >= MAX_SHORTS:
            return False, f"MAX_SHORTS reached ({n_shorts}/{MAX_SHORTS})"

        # 2. Already have short on this symbol
        if symbol in self._open_positions and not self._open_positions[symbol].closed:
            return False, f"Already have open SHORT on {symbol}"

        # 3. Funding rate check
        funding = self.get_funding_rate(symbol)
        if funding > FUNDING_LIMIT:
            return False, f"Funding rate {funding:.4%} > {FUNDING_LIMIT:.4%} — skip (overleveraged longs)"

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
        tp_pct: float = 5.0,   # % below entry — take profit
        leverage: int = LEVERAGE_MAX,
    ) -> Optional[FuturesPosition]:
        """
        Open a SHORT position with mandatory SL and TP orders.
        Returns FuturesPosition or None if safety checks fail.

        Risk math at 2x leverage:
          SL 3% move → 6% loss on margin
          TP 5% move → 10% gain on margin
        """
        ok, reason = self._pre_entry_checks(symbol, usdt_amount)
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

        sl_price = round(price * (1 + sl_pct / 100), 6)
        tp_price = round(price * (1 - tp_pct / 100), 6)

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
            self._open_positions[symbol] = pos
            return pos

        # ── Live order execution ──────────────────────────────────────────────
        try:
            # 1. Market SHORT order
            entry_order = self._post("/fapi/v1/order", {
                "symbol": symbol, "side": "SELL", "type": "MARKET",
                "quantity": str(qty), "reduceOnly": "false",
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
                    _sl_trigger = round(self.get_price(symbol) * (1 + sl_pct / 100), 6) if _sl_attempt > 0 else sl_price
                    sl_order = self._post("/fapi/v1/algoOrder", {
                        "symbol": symbol, "side": "BUY",
                        "algoType": "CONDITIONAL", "orderType": "STOP_MARKET",
                        "triggerPrice": str(_sl_trigger), "closePosition": "true",
                    })
                    sl_order_id = str(sl_order.get("algoId", sl_order.get("orderId", "")))
                    sl_price = _sl_trigger
                    print(f"[Futures] SL algo order placed: triggerPrice={sl_price:.4f} (algoId={sl_order_id})")
                    break
                except Exception as se:
                    if _sl_attempt == 0:
                        print(f"[Futures] SL order attempt 1 failed for {symbol}: {se} — retrying with refreshed price")
                        time.sleep(0.5)
                    else:
                        print(f"[Futures] SL order failed for {symbol}: {se} — position open WITHOUT SL, closing immediately")
                        self._post("/fapi/v1/order", {
                            "symbol": symbol, "side": "BUY", "type": "MARKET",
                            "quantity": str(qty), "reduceOnly": "true",
                        })
                        return None

            # 3. Take-profit order via Algo Orders API
            # POST /fapi/v1/algoOrder with algoType=CONDITIONAL + orderType=TAKE_PROFIT_MARKET
            # For SHORT: BUY TAKE_PROFIT_MARKET triggers when price falls to triggerPrice
            tp_order_id = None
            try:
                tp_order = self._post("/fapi/v1/algoOrder", {
                    "symbol": symbol, "side": "BUY",
                    "algoType": "CONDITIONAL", "orderType": "TAKE_PROFIT_MARKET",
                    "triggerPrice": str(tp_price), "closePosition": "true",
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
            self._open_positions[symbol] = pos
            print(f"[Futures] SHORT {symbol} OPEN: entry={filled_price:.4f}, "
                  f"SL={sl_price:.4f}, TP={tp_price:.4f}")
            return pos

        except Exception as e:
            print(f"[Futures] Failed to open SHORT {symbol}: {e}")
            return None

    def close_short(self, symbol: str, qty: float = None, reason: str = "manual") -> Optional[dict]:
        """
        Close a SHORT position with a BUY MARKET order.
        If qty is None, uses the tracked position qty.
        Cancels any open SL/TP orders before closing.
        """
        pos = self._open_positions.get(symbol)
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
                pos.exit_price = current_price
                pos.pnl        = pnl
            return {"simulated": True, "symbol": symbol, "pnl": pnl}

        # Cancel open SL/TP orders first
        if pos:
            for oid in [pos.sl_order_id, pos.tp_order_id]:
                if oid:
                    try:
                        self._delete("/fapi/v1/order", {"symbol": symbol, "orderId": oid})
                    except Exception:
                        pass

        try:
            step = self._get_step_size(symbol)
            from decimal import Decimal
            aligned_qty = self._align_qty(close_qty, step)
            result = self._post("/fapi/v1/order", {
                "symbol": symbol, "side": "BUY", "type": "MARKET",
                "quantity": str(aligned_qty), "reduceOnly": "true",
            })
            if pos:
                pos.closed     = True
                pos.exit_price = current_price
                pos.pnl        = pnl
            print(f"[Futures] SHORT {symbol} CLOSED: PnL~${pnl:.2f}")
            return result
        except Exception as e:
            print(f"[Futures] close_short {symbol} failed: {e}")
            return None

    def check_positions(self) -> List[dict]:
        """
        Check all tracked positions for SL/TP hits.
        Returns list of closed positions with PnL.
        Called from run_trades.py each cycle.
        """
        closed = []
        for symbol, pos in list(self._open_positions.items()):
            if pos.closed:
                continue
            try:
                price = self.get_price(symbol)
                unr   = pos.unrealized_pnl(price)
                print(f"[Futures] {symbol} SHORT check: price={price:.4f}, "
                      f"entry={pos.entry_price:.4f}, unr_pnl=${unr:.2f}")

                if pos.is_sl_hit(price):
                    result = self.close_short(symbol, reason="stop_loss")
                    if result:
                        closed.append({"symbol": symbol, "reason": "stop_loss",
                                       "pnl": pos.pnl or unr})
                elif pos.is_tp_hit(price):
                    result = self.close_short(symbol, reason="take_profit")
                    if result:
                        closed.append({"symbol": symbol, "reason": "take_profit",
                                       "pnl": pos.pnl or unr})
            except Exception as e:
                print(f"[Futures] check_positions {symbol}: {e}")
        return closed

    def get_open_positions_summary(self) -> List[dict]:
        """Summary of all tracked open short positions for dashboard."""
        result = []
        for symbol, pos in self._open_positions.items():
            if pos.closed:
                continue
            try:
                price = self.get_price(symbol)
                unr   = pos.unrealized_pnl(price)
            except Exception:
                price = pos.entry_price
                unr   = 0.0
            result.append({
                "symbol":      symbol,
                "side":        "SHORT",
                "qty":         pos.qty,
                "entry_price": pos.entry_price,
                "current_price": price,
                "sl_price":    pos.sl_price,
                "tp_price":    pos.tp_price,
                "unrealized_pnl": round(unr, 4),
                "opened_at":   pos.opened_at.isoformat(),
            })
        return result
