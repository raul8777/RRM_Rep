from typing import Dict, List, Optional

from binance_client import BinanceClient
from config import Settings
from news_client import NewsClient
from strategy import build_order_params, parse_signal


class TradingAgent:
    def __init__(
        self,
        binance_client: BinanceClient,
        news_client: NewsClient,
        settings: Settings,
    ):
        self.binance_client = binance_client
        self.news_client = news_client
        self.settings = settings

    def process_signal(self, payload: Dict) -> Dict:
        try:
            signal = parse_signal(payload)
        except ValueError as exc:
            return {"error": str(exc), "payload": payload}

        news = self._gather_news(signal["symbol"])
        result = {
            "symbol": signal["symbol"],
            "action": signal["action"],
            "quantity": signal["quantity"] or self.settings.default_trade_size,
            "mode": self.settings.agent_mode,
            "news": news,
        }

        if self.settings.agent_mode == "auto":
            order_params = build_order_params(signal, self.settings.default_trade_size)
            try:
                order = self.binance_client.create_market_order(**order_params)
                result["order"] = order
            except Exception as exc:
                result["error"] = f"Order failed: {exc}"
        else:
            result["suggestion"] = {
                "type": "market",
                "symbol": signal["symbol"],
                "side": signal["action"],
                "quantity": signal["quantity"] or self.settings.default_trade_size,
            }

        return result

    def _gather_news(self, symbol: str) -> List[Dict[str, Optional[str]]]:
        return self.news_client.get_recent_news([symbol], self.settings.news_lookback_hours)
