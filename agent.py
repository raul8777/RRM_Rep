from typing import Dict, List, Optional
from datetime import datetime

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
        portfolio_manager=None,
        position_manager=None,
    ):
        self.binance_client = binance_client
        self.news_client = news_client
        self.settings = settings
        self.portfolio_manager = portfolio_manager
        self.position_manager = position_manager

    def process_signal(self, payload: Dict) -> Dict:
        try:
            signal = parse_signal(payload)
        except ValueError as exc:
            return {"error": str(exc), "payload": payload}

        news = self._gather_news(signal["symbol"])
        
        # Use Portfolio Manager for sizing if available
        quantity = signal["quantity"]
        if quantity is None:
            if self.portfolio_manager:
                # Try to get risk-adjusted sizing from Portfolio Manager
                try:
                    balance = self.portfolio_manager.get_total_balance()
                    allocation_usdt = balance["allocation_usdt"]
                    # For now, use a simple allocation logic
                    # In production, this would use the full allocation logic
                    quantity = allocation_usdt / 10  # Default to 10% of allocation per trade
                except Exception:
                    quantity = self.settings.default_trade_size
            else:
                quantity = self.settings.default_trade_size
        
        result = {
            "symbol": signal["symbol"],
            "action": signal["action"],
            "quantity": quantity,
            "mode": self.settings.agent_mode,
            "news": news,
        }

        if self.settings.agent_mode == "auto":
            order_params = build_order_params(signal, quantity)
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
                    if notional is not None and notional > getattr(self.settings, "max_per_trade_usdt", 500.0):
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
                            
                            # Track position with Position Manager if available
                            if self.position_manager and order:
                                from position_manager import Position
                                position = Position(
                                    symbol=signal["symbol"],
                                    side=side,
                                    quantity=qty,
                                    entry_price=price or 0.0,
                                    entry_time=datetime.utcnow().isoformat(),
                                    metadata={"order_id": order.get("orderId")}
                                )
                                self.position_manager.add_position(position)
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
