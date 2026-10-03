import os
import time
try:
    from binance.client import Client
    from binance.exceptions import BinanceAPIException, BinanceRequestException
except Exception:
    Client = None

    class BinanceAPIException(Exception):
        pass

    class BinanceRequestException(Exception):
        pass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from exchange_filter_snapshot import describe_filter_snapshot


class BinanceClient:
    PRICE_CACHE_TTL = 30        # seconds
    FILTERS_CACHE_TTL = 3600    # 1 hour

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True, dry_run: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.dry_run = dry_run
        if Client is None and not self.dry_run:
            raise RuntimeError("python-binance is required for live BinanceClient usage")
        self.client = Client(self.api_key, self.api_secret)
        if self.testnet:
            self.client.API_URL = "https://demo-api.binance.com/api"
        # In-process caches: {symbol: (value, timestamp)}
        self._price_cache: Dict[str, Tuple[float, float]] = {}
        self._filters_cache: Dict[str, Tuple[Dict, float]] = {}

    def get_account_info(self) -> Dict[str, Any]:
        try:
            return self.client.get_account()
        except (BinanceAPIException, BinanceRequestException) as exc:
            raise RuntimeError(f"Binance account info error: {exc}") from exc

    def get_account_balance(self) -> Dict[str, float]:
        try:
            account_info = self.get_account_info()
            balances = {
                item["asset"]: float(item["free"])
                for item in account_info.get("balances", [])
                if float(item.get("free", 0)) > 0
            }
            return balances
        except (BinanceAPIException, BinanceRequestException) as exc:
            raise RuntimeError(f"Binance balance error: {exc}") from exc

    def get_account_balance_details(self) -> Dict[str, Dict[str, float]]:
        """Return free, locked, and total Spot quantities per base asset."""
        try:
            account_info = self.get_account_info()
            details: Dict[str, Dict[str, float]] = {}
            for item in account_info.get("balances", []):
                asset = str(item.get("asset") or "").strip()
                if not asset:
                    continue
                free = float(item.get("free", 0.0) or 0.0)
                locked = float(item.get("locked", 0.0) or 0.0)
                if free < 0 or locked < 0:
                    raise ValueError(f"Negative Spot balance for {asset}")
                total = free + locked
                details[asset] = {"free": free, "locked": locked, "total": total}
            return details
        except (BinanceAPIException, BinanceRequestException) as exc:
            raise RuntimeError(f"Binance balance details error: {exc}") from exc

    def get_wallet_balance(self) -> Dict[str, float]:
        """Return total Spot wallet balances (free + locked).

        This endpoint covers the Binance Spot account. Funding/Earn wallets that
        are not present in the Spot account must be reconciled through their own
        transfer/account APIs; they are not fabricated from missing fields.
        """
        return {
            asset: row["total"]
            for asset, row in self.get_account_balance_details().items()
            if row["total"] > 0
        }

    def get_commission_rate(self) -> float:
        """Return the live spot taker commission rate from Binance account info.

        Falls back to the documented default (0.001) if the API response does not
        contain the field or if the call fails.
        """
        try:
            info = self.get_account_info()
            rate = info.get("commission")
            if rate is not None:
                return float(rate)
        except Exception as exc:
            print(f"[BinanceClient] commission rate fetch failed, using default: {exc}")
        return 0.001  # Binance default spot taker rate

    def _format_amount(self, amount: float) -> str:
        value = Decimal(str(amount))
        normalized = value.normalize()
        # prevent scientific notation in API parameters
        return format(normalized, 'f')

    def create_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        quote_order_qty: Optional[float] = None,
    ) -> Dict[str, Any]:
        # If DRY_RUN is enabled, return a simulated order result.
        if self.dry_run or os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes"):
            simulated = {
                "simulated": True,
                "symbol": symbol,
                "side": side.upper(),
                "type": "MARKET",
                "quantity": self._format_amount(quantity),
                "quoteOrderQty": self._format_amount(quote_order_qty) if quote_order_qty else None,
            }
            return simulated

        try:
            order_params = {
                "symbol": symbol,
                "side": side.upper(),
                "type": "MARKET",
            }
            if quote_order_qty:
                order_params["quoteOrderQty"] = self._format_amount(quote_order_qty)
            else:
                order_params["quantity"] = self._format_amount(quantity)
            result = self.client.create_order(**order_params)
            return result
        except (BinanceAPIException, BinanceRequestException) as exc:
            raise RuntimeError(f"Binance order error: {exc}") from exc

    def create_oco_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        above_type: str,
        below_type: str,
        above_price: Optional[float] = None,
        above_stop_price: Optional[float] = None,
        below_price: Optional[float] = None,
        below_stop_price: Optional[float] = None,
        list_client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Place a spot OCO (One-Cancels-the-Other) order: a LIMIT_MAKER leg
        plus a STOP_LOSS_LIMIT leg submitted together, used to place TP+SL
        simultaneously on a spot position (real or synthetic-short).

        For a SELL OCO (closing a LONG): the LIMIT_MAKER leg sits above the
        current price (take-profit) and the STOP_LOSS_LIMIT leg sits below it
        (stop-loss). For a BUY OCO (closing a synthetic SHORT): the
        STOP_LOSS_LIMIT leg sits above the current price (stop-loss) and the
        LIMIT_MAKER leg sits below it (take-profit).

        Maps the `above_*`/`below_*` price-position naming used by callers to
        the Binance OCO API's `price`/`stopPrice`/`stopLimitPrice` fields,
        which are always expressed relative to order type, not price position.
        """
        if above_type == "LIMIT_MAKER":
            limit_price = above_price
            stop_price = below_stop_price
            stop_limit_price = below_price
        else:
            limit_price = below_price
            stop_price = above_stop_price
            stop_limit_price = above_price

        if self.dry_run or os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes"):
            sim_id = f"SIM-{int(time.time() * 1000)}"
            return {
                "simulated": True,
                "symbol": symbol,
                "side": side.upper(),
                "orderListId": sim_id,
                "orders": [{"orderId": f"{sim_id}-1"}, {"orderId": f"{sim_id}-2"}],
            }

        try:
            params: Dict[str, Any] = {
                "symbol": symbol,
                "side": side.upper(),
                "quantity": self._format_amount(quantity),
                "price": self._format_amount(limit_price),
                "stopPrice": self._format_amount(stop_price),
                "stopLimitPrice": self._format_amount(stop_limit_price),
                "stopLimitTimeInForce": "GTC",
            }
            if list_client_order_id:
                params["listClientOrderId"] = list_client_order_id
            return self.client.create_oco_order(**params)
        except (BinanceAPIException, BinanceRequestException) as exc:
            raise RuntimeError(f"Binance OCO order error: {exc}") from exc

    def get_order_status(self, symbol: str, order_id: int) -> Dict[str, Any]:
        try:
            return self.client.get_order(symbol=symbol, orderId=order_id)
        except (BinanceAPIException, BinanceRequestException) as exc:
            raise RuntimeError(f"Binance order status error: {exc}") from exc

    def get_order_book(self, symbol: str, limit: int = 20) -> Dict[str, Any]:
        """Return a fresh public order-book snapshot for full-size close planning."""
        try:
            return self.client.get_order_book(symbol=symbol, limit=int(limit))
        except (BinanceAPIException, BinanceRequestException) as exc:
            raise RuntimeError(f"Binance order book error: {exc}") from exc

    def create_limit_fok_order(
        self, symbol: str, side: str, quantity: float, price: float,
    ) -> Dict[str, Any]:
        """Submit a fill-or-kill order; it cannot intentionally leave a residual."""
        if self.dry_run or os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes"):
            return {
                "simulated": True,
                "symbol": symbol,
                "side": side.upper(),
                "type": "LIMIT",
                "timeInForce": "FOK",
                "quantity": self._format_amount(quantity),
                "price": self._format_amount(price),
            }
        try:
            return self.client.create_order(
                symbol=symbol,
                side=side.upper(),
                type="LIMIT",
                timeInForce="FOK",
                quantity=self._format_amount(quantity),
                price=self._format_amount(price),
            )
        except (BinanceAPIException, BinanceRequestException) as exc:
            raise RuntimeError(f"Binance FOK order error: {exc}") from exc

    def verify_order_filled(self, order_result: dict, label: str = "") -> bool:
        """Verify a market order was actually filled by Binance.
        Returns True if confirmed filled, False otherwise."""
        if not order_result:
            print(f"[Verify] {label}: order_result is None/empty — NOT filled")
            return False
        if order_result.get("simulated"):
            return True
        status = order_result.get("status", "")
        sym = order_result.get("symbol", label)
        if status == "FILLED":
            return True
        fills = order_result.get("fills", [])
        if fills and len(fills) > 0:
            return True
        oid = order_result.get("orderId")
        if oid and sym:
            try:
                real = self.get_order_status(sym, int(oid))
                if real.get("status") == "FILLED":
                    return True
                print(f"[Verify] {sym} {label}: order {oid} status={real.get('status')} — NOT filled")
                return False
            except Exception as e:
                print(f"[Verify] {sym} {label}: status check failed: {e}")
        print(f"[Verify] {sym} {label}: could not confirm fill (status={status})")
        return False

    def get_symbol_price(self, symbol: str, use_cache: bool = True) -> float:
        now = time.monotonic()
        if use_cache and symbol in self._price_cache:
            val, ts = self._price_cache[symbol]
            if now - ts < self.PRICE_CACHE_TTL:
                return val
        try:
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            price = float(ticker.get("price", 0))
            self._price_cache[symbol] = (price, now)
            return price
        except (BinanceAPIException, BinanceRequestException) as exc:
            raise RuntimeError(f"Binance price error: {exc}") from exc

    def get_symbol_avg_price(self, symbol: str, use_cache: bool = True) -> float:
        """Return 5-minute rolling average price (VWAP) for a symbol.

        This is more robust than lastPrice for dashboard unrealized PnL because
        it smooths single-candle wicks and illiquid prints.
        """
        now = time.monotonic()
        cache_key = f"{symbol}:avg"
        if use_cache and cache_key in self._price_cache:
            val, ts = self._price_cache[cache_key]
            if now - ts < self.PRICE_CACHE_TTL:
                return val
        try:
            avg = self.client.get_avg_price(symbol=symbol)
            price = float(avg.get("price", 0))
            self._price_cache[cache_key] = (price, now)
            return price
        except (BinanceAPIException, BinanceRequestException) as exc:
            raise RuntimeError(f"Binance avg price error: {exc}") from exc

    def get_symbol_filters(self, symbol: str) -> Dict:
        """Return symbol filters with 1-hour cache to minimize API calls."""
        now = time.monotonic()
        if symbol in self._filters_cache:
            val, ts = self._filters_cache[symbol]
            if now - ts < self.FILTERS_CACHE_TTL:
                return val
        try:
            info = self.client.get_symbol_info(symbol)
            filters_raw = info.get("filters", []) if info else []
            filter_types = {item.get("filterType") for item in filters_raw if isinstance(item, dict)}
            result: Dict = {
                "step": 0.001,
                "min_qty": 0.001,
                "max_qty": 0.0,
                "market_step": 0.001,
                "market_min_qty": 0.001,
                "market_max_qty": 0.0,
                "min_notional": 10.0,
                "tick_size": 0.0,
                "filter_snapshot": filters_raw,
                **describe_filter_snapshot(
                    filters_raw,
                    venue="spot_demo" if self.testnet else "spot_mainnet",
                    symbol=symbol,
                    source="BinanceClient.get_symbol_filters",
                ),
            }
            for f in filters_raw:
                ft = f.get("filterType", "")
                if ft == "LOT_SIZE":
                    result["step"] = float(f.get("stepSize", 0.001))
                    result["min_qty"] = float(f.get("minQty", 0.001))
                    result["max_qty"] = float(f.get("maxQty", 0.0))
                elif ft == "MARKET_LOT_SIZE":
                    result["market_step"] = float(f.get("stepSize", result["step"]))
                    result["market_min_qty"] = float(f.get("minQty", result["min_qty"]))
                    result["market_max_qty"] = float(f.get("maxQty", result["max_qty"]))
                elif ft == "PRICE_FILTER":
                    result["tick_size"] = float(f.get("tickSize", 0.0))
                elif ft == "NOTIONAL":
                    result["min_notional"] = float(f.get("minNotional", 10.0))
                elif ft == "MIN_NOTIONAL":
                    result["min_notional"] = float(f.get("minNotional", 10.0))
            result["exchange_filters_validated"] = bool(
                info
                and {"LOT_SIZE", "MARKET_LOT_SIZE", "PRICE_FILTER"}.issubset(filter_types)
                and bool(filter_types.intersection({"MIN_NOTIONAL", "NOTIONAL"}))
                and result["step"] > 0
                and result["max_qty"] > 0
                and result["market_step"] > 0
                and result["market_max_qty"] > 0
                and result["tick_size"] > 0
                and result["min_notional"] > 0
            )
            self._filters_cache[symbol] = (result, now)
            return result
        except (BinanceAPIException, BinanceRequestException) as exc:
            raise RuntimeError(f"Binance filters error: {exc}") from exc

    def get_daily_open_price(self, symbol: str, open_hour_utc: int = 14) -> float:
        """Return the open price of the 1h candle at open_hour_utc today (8AM Mexico = 14:00 UTC).
        Falls back to current price if klines are unavailable."""
        import calendar
        from datetime import datetime, timezone, timedelta
        now_utc = datetime.now(timezone.utc)
        # Build timestamp for today's open_hour_utc; if we haven't reached it yet use yesterday's
        target = now_utc.replace(hour=open_hour_utc, minute=0, second=0, microsecond=0)
        if now_utc < target:
            target -= timedelta(days=1)
        start_ms = int(calendar.timegm(target.timetuple()) * 1000)
        try:
            klines = self.client.get_klines(
                symbol=symbol, interval="1h", startTime=start_ms, limit=1
            )
            if klines:
                return float(klines[0][1])  # index 1 = open
        except Exception:
            pass
        return self.get_symbol_price(symbol)

    def get_my_trades(self, symbol: str, order_id: Optional[int] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Fetch recent SPOT trades for a symbol from Binance.

        If order_id is provided, filters trades to those matching the order.
        Returns a list of raw trade dicts (qty, price, commission, etc.).
        """
        try:
            params = {"symbol": symbol, "limit": limit}
            if order_id is not None:
                params["orderId"] = int(order_id)
            trades = self.client.get_my_trades(**params)
            return trades if isinstance(trades, list) else []
        except (BinanceAPIException, BinanceRequestException) as exc:
            print(f"[BinanceClient] get_my_trades error for {symbol}: {exc}")
            return []
        except Exception as exc:
            print(f"[BinanceClient] get_my_trades unexpected error for {symbol}: {exc}")
            return []

    def verify_and_fetch_fills(self, order_result: dict, label: str = "") -> Optional[Dict[str, Any]]:
        """Verify a SPOT market order and return aggregated fill data.

        Returns None if not confirmed. When confirmed, returns dict with:
          - qty (total executed base asset)
          - quote_qty (total quote asset)
          - commission (total commission in quote terms)
          - avg_price (weighted average price)
          - binance_confirmed: True
        """
        if not self.verify_order_filled(order_result, label=label):
            return None
        sym = order_result.get("symbol", "")
        oid = order_result.get("orderId")
        fills = order_result.get("fills") or []
        if not fills and oid and sym:
            fills = self.get_my_trades(sym, order_id=int(oid))
        if not fills:
            return None
        from pnl_accounting import normalize_spot_fills
        payload = normalize_spot_fills(
            fills,
            sym,
            price_lookup=lambda commission_symbol: self.get_symbol_price(commission_symbol, use_cache=False),
        )
        payload.update({
            "order_id": oid,
            "commission": payload["commission_usdt"],
            "binance_confirmed": True,
        })
        return payload

    def calculate_commission(self, amount: float, price: float, commission_rate: float = 0.001) -> float:
        """
        Calculate commission for a trade.

        Args:
            amount: Quantity of the asset
            price: Price per unit
            commission_rate: Commission rate (default 0.1% = 0.001)

        Returns:
            Commission amount in quote currency (USDT)
        """
        return amount * price * commission_rate
