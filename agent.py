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
            qty = order_params.get("quantity")
            side = order_params.get("side")

            # Estimate notional amount (price * qty)
            try:
                price = self.binance_client.get_symbol_price(signal["symbol"])
            except Exception:
                price = None

            notional = price * qty if price else None

            # Dry-run: do not execute, only return what would be done
            if getattr(self.settings, "dry_run", True):
                result["dry_run"] = True
                result["order_estimate"] = {
                    "symbol": signal["symbol"],
                    "side": side,
                    "quantity": qty,
                    "price": price,
                    "notional": notional,
                }
            else:
                # Prevent accidental real trading unless explicitly allowed
                if not self.settings.binance_use_testnet and not getattr(self.settings, "allow_real_trading", False):
                    result["error"] = "Real trading is disabled (set ALLOW_REAL_TRADING=true to enable)."
                else:
                    # Enforce per-trade USDT limit if we have a notional estimate
                    if notional is not None and notional > getattr(self.settings, "max_per_trade_usdt", 0):
                        result["error"] = f"Order not placed: notional {notional} exceeds max per-trade {self.settings.max_per_trade_usdt} USDT"
                    else:
                        # Check balances for buy/sell
                        try:
                            balances = self.binance_client.get_account_balance()
                        except Exception:
                            balances = {}

                        base_asset = signal["symbol"].replace("USDT", "")
                        if side and side.upper() == "SELL":
                            available = float(balances.get(base_asset, 0))
                            if available < qty:
                                result["error"] = f"Insufficient {base_asset} balance: {available} < {qty}"
                                return result
                        elif side and side.upper() == "BUY" and notional is not None:
                            available_quote = float(balances.get("USDT", 0))
                            if available_quote < notional:
                                result["error"] = f"Insufficient USDT balance: {available_quote} < {notional}"
                                return result

                        # All checks passed: execute order
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
