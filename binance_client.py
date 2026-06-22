import os
import time
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple


class BinanceClient:
    PRICE_CACHE_TTL = 30        # seconds
    FILTERS_CACHE_TTL = 3600    # 1 hour

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True, dry_run: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.dry_run = dry_run
        self.client = Client(self.api_key, self.api_secret)
        if self.testnet:
            self.client.API_URL = "https://testnet.binance.vision/api"
        # In-process caches: {symbol: (value, timestamp)}
        self._price_cache: Dict[str, Tuple[float, float]] = {}
        self._filters_cache: Dict[str, Tuple[Dict, float]] = {}

    def get_account_balance(self) -> Dict[str, float]:
        try:
            account_info = self.client.get_account()
            balances = {
                item["asset"]: float(item["free"])
                for item in account_info.get("balances", [])
                if float(item.get("free", 0)) > 0
            }
            return balances
        except (BinanceAPIException, BinanceRequestException) as exc:
            raise RuntimeError(f"Binance balance error: {exc}") from exc

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

    def get_order_status(self, symbol: str, order_id: int) -> Dict[str, Any]:
        try:
            return self.client.get_order(symbol=symbol, orderId=order_id)
        except (BinanceAPIException, BinanceRequestException) as exc:
            raise RuntimeError(f"Binance order status error: {exc}") from exc

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
            result: Dict = {"step": 0.001, "min_qty": 0.001, "min_notional": 10.0}
            for f in filters_raw:
                ft = f.get("filterType", "")
                if ft == "LOT_SIZE":
                    result["step"] = float(f.get("stepSize", 0.001))
                    result["min_qty"] = float(f.get("minQty", 0.001))
                elif ft == "NOTIONAL":
                    result["min_notional"] = float(f.get("minNotional", 10.0))
                elif ft == "MIN_NOTIONAL":
                    result["min_notional"] = float(f.get("minNotional", 10.0))
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
