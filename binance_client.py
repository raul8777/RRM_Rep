from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from typing import Any, Dict, Optional


class BinanceClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.client = Client(self.api_key, self.api_secret)
        if self.testnet:
            self.client.API_URL = "https://testnet.binance.vision/api"

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

    def create_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        quote_order_qty: Optional[float] = None,
    ) -> Dict[str, Any]:
        try:
            order_params = {
                "symbol": symbol,
                "side": side.upper(),
                "type": "MARKET",
            }
            if quote_order_qty:
                order_params["quoteOrderQty"] = quote_order_qty
            else:
                order_params["quantity"] = quantity
            result = self.client.create_order(**order_params)
            return result
        except (BinanceAPIException, BinanceRequestException) as exc:
            raise RuntimeError(f"Binance order error: {exc}") from exc

    def get_order_status(self, symbol: str, order_id: int) -> Dict[str, Any]:
        try:
            return self.client.get_order(symbol=symbol, orderId=order_id)
        except (BinanceAPIException, BinanceRequestException) as exc:
            raise RuntimeError(f"Binance order status error: {exc}") from exc

    def get_symbol_price(self, symbol: str) -> float:
        try:
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            return float(ticker.get("price", 0))
        except (BinanceAPIException, BinanceRequestException) as exc:
            raise RuntimeError(f"Binance price error: {exc}") from exc
