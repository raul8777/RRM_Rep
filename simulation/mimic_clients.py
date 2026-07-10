"""MIMIC — Deterministic mocks for production clients.

These classes implement the same public interfaces as `BinanceClient` and
`FuturesClient` but execute against an in-memory market replay.
"""
from __future__ import annotations
from decimal import Decimal
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
from simulation.mimic_protocols import SimulationClock


class SimulationBinanceClient:
    """Mock spot client backed by a historical price feed and virtual balance."""

    PRICE_CACHE_TTL = 30
    FILTERS_CACHE_TTL = 3600

    class _Client:
        """Internal client shim so autonomous_signals.fetch_klines works."""

        def __init__(self, owner: "SimulationBinanceClient"):
            self._owner = owner

        def get_ticker(self, symbol: str) -> Dict[str, Any]:
            """Return a 24h ticker-like dict from the current bar."""
            try:
                price = self._owner._current_price(symbol)
                bars = self._owner._price_feed.get(symbol, [])
                now = self._owner.clock.now().timestamp()
                # Sum quote_volume of last 24h-ish of bars up to current clock
                last_24h = [b for b in bars if b.get("open_time", b.get("timestamp", 0)) <= now]
                last_24h = last_24h[-24:] if len(last_24h) >= 24 else last_24h
                quote_volume = sum(float(b.get("quote_volume", 0)) for b in last_24h)
                # Price change from first to last of the window
                price_change_pct = 0.0
                if last_24h:
                    first = float(last_24h[0].get("open", 0))
                    last = float(last_24h[-1].get("close", price))
                    if first > 0:
                        price_change_pct = (last - first) / first * 100
                return {
                    "symbol": symbol,
                    "lastPrice": str(price),
                    "priceChangePercent": str(price_change_pct),
                    "quoteVolume": str(quote_volume),
                }
            except Exception:
                return {
                    "symbol": symbol,
                    "lastPrice": "0.0",
                    "priceChangePercent": "0.0",
                    "quoteVolume": "0.0",
                }

        def get_symbol_info(self, symbol: str) -> Dict[str, Any]:
            return {
                "symbol": symbol,
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                    {"filterType": "NOTIONAL", "minNotional": "10.0"},
                ],
            }

        def get_klines(
            self, symbol: str, interval: str = "1h", limit: int = 100,
            startTime: Optional[int] = None, endTime: Optional[int] = None,
        ) -> List[List[Any]]:
            bars = self._owner._price_feed.get(symbol, [])
            if interval == "4h":
                bars = self._resample_4h(bars)
            elif interval == "1d":
                bars = self._resample_1d(bars)
            # Convert bars to raw Binance kline format [open_time, open, high, low, close, volume, close_time, ...]
            raw = []
            for b in bars:
                ot = int(b.get("open_time", b.get("timestamp", 0)) * 1000)
                if startTime and ot < startTime:
                    continue
                if endTime and ot > endTime:
                    continue
                # close time depends on interval
                if interval == "4h":
                    ct = ot + 14_400_000
                elif interval == "1d":
                    ct = ot + 86_400_000
                else:
                    ct = ot + 3_600_000
                raw.append([
                    ot,
                    str(b.get("open", 0)),
                    str(b.get("high", 0)),
                    str(b.get("low", 0)),
                    str(b.get("close", 0)),
                    str(b.get("volume", 0)),
                    ct,
                    str(b.get("quote_volume", 0)),
                    int(b.get("trades", 0)),
                    str(b.get("taker_buy_base", 0)),
                    str(b.get("taker_buy_quote", 0)),
                    "0",
                ])
            # Return the last `limit` bars up to the simulated clock
            now_ms = int(self._owner.clock.now().timestamp() * 1000)
            filtered = [r for r in raw if r[0] <= now_ms]
            if len(filtered) > limit:
                filtered = filtered[-limit:]
            return filtered

        def _resample_4h(self, bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            """Resample 1h bars into 4h bars."""
            if not bars:
                return []
            sorted_bars = sorted(bars, key=lambda x: x.get("open_time", x.get("timestamp", 0)))
            result = []
            bucket = []
            for b in sorted_bars:
                ot = b.get("open_time", b.get("timestamp", 0))
                bucket.append(b)
                if len(bucket) == 4:
                    result.append(self._aggregate_bucket(bucket, ot))
                    bucket = []
            if bucket:
                result.append(self._aggregate_bucket(bucket, bucket[-1].get("open_time", bucket[-1].get("timestamp", 0))))
            return result

        def _resample_1d(self, bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            if not bars:
                return []
            sorted_bars = sorted(bars, key=lambda x: x.get("open_time", x.get("timestamp", 0)))
            result = []
            bucket = []
            for b in sorted_bars:
                bucket.append(b)
                if len(bucket) == 24:
                    result.append(self._aggregate_bucket(bucket, b.get("open_time", b.get("timestamp", 0))))
                    bucket = []
            if bucket:
                result.append(self._aggregate_bucket(bucket, bucket[-1].get("open_time", bucket[-1].get("timestamp", 0))))
            return result

        def _aggregate_bucket(self, bucket: List[Dict[str, Any]], open_time: float) -> Dict[str, Any]:
            opens = [float(b.get("open", 0)) for b in bucket]
            highs = [float(b.get("high", 0)) for b in bucket]
            lows = [float(b.get("low", 0)) for b in bucket]
            closes = [float(b.get("close", 0)) for b in bucket]
            volumes = [float(b.get("volume", 0)) for b in bucket]
            quote_volumes = [float(b.get("quote_volume", 0)) for b in bucket]
            trades = [int(b.get("trades", 0)) for b in bucket]
            taker_buy_bases = [float(b.get("taker_buy_base", 0)) for b in bucket]
            taker_buy_quotes = [float(b.get("taker_buy_quote", 0)) for b in bucket]
            return {
                "open_time": open_time,
                "open": opens[0],
                "high": max(highs),
                "low": min(lows),
                "close": closes[-1],
                "volume": sum(volumes),
                "quote_volume": sum(quote_volumes),
                "trades": sum(trades),
                "taker_buy_base": sum(taker_buy_bases),
                "taker_buy_quote": sum(taker_buy_quotes),
            }

    def __init__(
        self,
        clock: SimulationClock,
        initial_balances: Optional[Dict[str, float]] = None,
        commission_rate: float = 0.001,
        slippage_pct: float = 0.0,
    ):
        self.clock = clock
        self.balances: Dict[str, float] = dict(initial_balances or {"USDT": 2910.0})
        self.commission_rate = commission_rate
        self.slippage_pct = slippage_pct
        self._price_feed: Dict[str, List[Dict[str, Any]]] = {}  # symbol -> [{ts, open, high, low, close, volume}]
        self._orders: List[Dict[str, Any]] = []
        self._price_cache: Dict[str, tuple] = {}
        self._filters_cache: Dict[str, tuple] = {}
        self._order_id = 0
        self.client = SimulationBinanceClient._Client(self)

    def set_price_feed(self, feed: Dict[str, List[Dict[str, Any]]]) -> None:
        self._price_feed = feed

    def _current_bar(self, symbol: str) -> Optional[Dict[str, Any]]:
        bars = self._price_feed.get(symbol, [])
        if not bars:
            return None
        now = self.clock.now().timestamp()
        # Last bar whose open time <= now
        bar = None
        for b in bars:
            if b.get("open_time", b.get("timestamp", 0)) <= now:
                bar = b
        return bar

    def _current_price(self, symbol: str) -> float:
        bar = self._current_bar(symbol)
        if bar is None:
            raise RuntimeError(f"No price data for {symbol} at {self.clock.now().isoformat()}")
        return float(bar.get("close", 0))

    def get_account_info(self) -> Dict[str, Any]:
        return {
            "balances": [
                {"asset": asset, "free": f"{qty:.8f}", "locked": "0.00000000"}
                for asset, qty in self.balances.items()
            ],
            "commission": self.commission_rate,
        }

    def get_account_balance(self) -> Dict[str, float]:
        return {asset: float(qty) for asset, qty in self.balances.items() if float(qty) > 0}

    def get_commission_rate(self) -> float:
        return self.commission_rate

    def _format_amount(self, amount: float) -> str:
        value = Decimal(str(amount))
        normalized = value.normalize()
        return format(normalized, "f")

    def create_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        quote_order_qty: Optional[float] = None,
    ) -> Dict[str, Any]:
        self._order_id += 1
        price = self._current_price(symbol)
        if self.slippage_pct:
            # slippage against the orderer: worse fill price
            sign = -1 if side.upper() == "BUY" else 1
            price = price * (1 + sign * self.slippage_pct)

        asset = symbol.replace("USDT", "")
        if side.upper() == "BUY":
            if quote_order_qty:
                qty = quote_order_qty / price
                cost = quote_order_qty
            else:
                qty = quantity
                cost = qty * price
            commission = cost * self.commission_rate
            self.balances["USDT"] = self.balances.get("USDT", 0) - cost - commission
            self.balances[asset] = self.balances.get(asset, 0) + qty
            fills_qty = qty
            fills_quote = cost
        else:  # SELL
            if quote_order_qty:
                raise ValueError("SELL with quote_order_qty not supported in simulation")
            qty = quantity
            proceeds = qty * price
            commission = proceeds * self.commission_rate
            self.balances[asset] = self.balances.get(asset, 0) - qty
            self.balances["USDT"] = self.balances.get("USDT", 0) + proceeds - commission
            fills_qty = qty
            fills_quote = proceeds

        order = {
            "orderId": self._order_id,
            "symbol": symbol,
            "side": side.upper(),
            "type": "MARKET",
            "status": "FILLED",
            "quantity": self._format_amount(qty),
            "quoteOrderQty": self._format_amount(quote_order_qty) if quote_order_qty else None,
            "fills": [
                {
                    "price": self._format_amount(price),
                    "qty": self._format_amount(fills_qty),
                    "commission": self._format_amount(commission),
                    "commissionAsset": "USDT" if side.upper() == "BUY" else asset,
                }
            ],
            "simulated": True,
        }
        self._orders.append(order)
        return order

    def get_order_status(self, symbol: str, order_id: int) -> Dict[str, Any]:
        for o in self._orders:
            if o.get("orderId") == order_id and o.get("symbol") == symbol:
                return o
        return {"status": "UNKNOWN"}

    def verify_order_filled(self, order_result: dict, label: str = "") -> bool:
        if not order_result:
            return False
        return order_result.get("status") == "FILLED"

    def get_symbol_price(self, symbol: str, use_cache: bool = True) -> float:
        now = self.clock.now().timestamp()
        if use_cache and symbol in self._price_cache:
            price, ts = self._price_cache[symbol]
            if now - ts < self.PRICE_CACHE_TTL:
                return price
        price = self._current_price(symbol)
        self._price_cache[symbol] = (price, now)
        return price

    def get_symbol_filters(self, symbol: str) -> Dict[str, float]:
        """Return symbol filters with 1-hour cache."""
        now = self.clock.now().timestamp()
        if symbol in self._filters_cache:
            val, ts = self._filters_cache[symbol]
            if now - ts < self.FILTERS_CACHE_TTL:
                return val
        info = self.client.get_symbol_info(symbol)
        filters_raw = info.get("filters", []) if info else []
        result = {"step": 0.001, "min_qty": 0.001, "min_notional": 10.0}
        for f in filters_raw:
            ft = f.get("filterType", "")
            if ft == "LOT_SIZE":
                result["step"] = float(f.get("stepSize", 0.001))
                result["min_qty"] = float(f.get("minQty", 0.001))
            elif ft in ("NOTIONAL", "MIN_NOTIONAL"):
                result["min_notional"] = float(f.get("minNotional", 10.0))
        self._filters_cache[symbol] = (result, now)
        return result

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        return self.client.get_ticker(symbol)

    def get_client(self):
        """Return the internal client shim used by legacy code."""
        return self.client


class SimulationFuturesClient:
    """Mock futures client backed by the same historical price feed."""

    def __init__(
        self,
        clock: SimulationClock,
        price_feed: Dict[str, List[Dict[str, Any]]],
        commission_rate: float = 0.0004,
        leverage: int = 2,
    ):
        self.clock = clock
        self._price_feed = price_feed
        self.commission_rate = commission_rate
        self.leverage = leverage
        self._open_positions: Dict[str, Any] = {}
        self._available_usdt = 0.0
        self._hedge_mode = False
        self.db_client = None
        self._cached_settings = None
        self._active_leverage = leverage
        self.max_shorts = 5
        self.max_longs = 2
        self.max_largecap_shorts = 3
        self.largecap_symbols = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT"}
        self._binance_client_ref = None

    def _current_price(self, symbol: str) -> float:
        bars = self._price_feed.get(symbol, [])
        if not bars:
            raise RuntimeError(f"No futures price data for {symbol}")
        now = self.clock.now().timestamp()
        bar = None
        for b in bars:
            if b.get("open_time", b.get("timestamp", 0)) <= now:
                bar = b
        if bar is None:
            raise RuntimeError(f"No futures price data for {symbol} at {self.clock.now().isoformat()}")
        return float(bar.get("close", 0))

    def enable_hedge_mode(self) -> None:
        self._hedge_mode = True

    def get_price(self, symbol: str) -> float:
        return self._current_price(symbol)

    def get_available_usdt(self) -> float:
        return self._available_usdt

    def check_positions(self, settings: Any, monitor_only: bool = False) -> List[Dict[str, Any]]:
        # Minimal simulation: close positions that hit SL or TP
        closes = []
        for key, pos in list(self._open_positions.items()):
            if pos.closed:
                continue
            price = self._current_price(pos.symbol)
            if pos.side == "SHORT":
                if price >= pos.sl_price:
                    closes.append({"symbol": pos.symbol, "reason": "SL", "side": "SHORT"})
                    pos.closed = True
                elif price <= pos.tp_price:
                    closes.append({"symbol": pos.symbol, "reason": "TP", "side": "SHORT"})
                    pos.closed = True
            else:  # LONG
                if price <= pos.sl_price:
                    closes.append({"symbol": pos.symbol, "reason": "SL", "side": "LONG"})
                    pos.closed = True
                elif price >= pos.tp_price:
                    closes.append({"symbol": pos.symbol, "reason": "TP", "side": "LONG"})
                    pos.closed = True
        return closes

    def reevaluate_open_positions(
        self, settings: Any, regime: str, profile_label: str,
        switch_id: Optional[str] = None, transition_policy: Optional[Dict] = None,
    ) -> int:
        # Minimal simulation: close shorts if regime no longer short-friendly
        if regime not in ("PRESERVATION", "FUTURES_SHORT"):
            closed = 0
            for pos in self._open_positions.values():
                if not pos.closed and pos.side == "SHORT":
                    pos.closed = True
                    closed += 1
            return closed
        return 0

    def smart_rebalance_capital(self, **kwargs: Any) -> Dict[str, Any]:
        return {"error": "capital bridge disabled in simulation"}

    def get_open_positions_summary(self) -> List[Dict[str, Any]]:
        """Stub for dashboard compatibility."""
        return []
