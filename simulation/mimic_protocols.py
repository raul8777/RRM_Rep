"""MIMIC — Protocol definitions for simulation components.

These protocols mirror the public interfaces of the production clients so that
any change in production is caught by `tests/test_simulation_regression.py`.
"""
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable
from datetime import datetime


@runtime_checkable
class IBinanceClient(Protocol):
    """Production interface implemented by `binance_client.BinanceClient`."""

    def get_account_info(self) -> Dict[str, Any]: ...
    def get_account_balance(self) -> Dict[str, float]: ...
    def get_commission_rate(self) -> float: ...
    def create_market_order(
        self, symbol: str, side: str, quantity: float,
        quote_order_qty: Optional[float] = None,
    ) -> Dict[str, Any]: ...
    def get_order_status(self, symbol: str, order_id: int) -> Dict[str, Any]: ...
    def verify_order_filled(self, order_result: dict, label: str = "") -> bool: ...
    def get_symbol_price(self, symbol: str, use_cache: bool = True) -> float: ...


@runtime_checkable
class IFuturesClient(Protocol):
    """Production interface implemented by `futures_client.FuturesClient`."""

    def check_positions(self, settings: Any, monitor_only: bool = False) -> List[Dict[str, Any]]: ...
    def reevaluate_open_positions(
        self, settings: Any, regime: str, profile_label: str,
        switch_id: Optional[str] = None, transition_policy: Optional[Dict] = None,
    ) -> int: ...
    def smart_rebalance_capital(self, **kwargs: Any) -> Dict[str, Any]: ...
    def get_price(self, symbol: str) -> float: ...
    def get_available_usdt(self) -> float: ...
    def enable_hedge_mode(self) -> None: ...


@runtime_checkable
class INewsClient(Protocol):
    """Production interface implemented by `news_client.NewsClient`."""

    def get_fear_greed(self) -> Dict[str, Any]: ...
    def get_fear_greed_modifier(self) -> float: ...
    def get_trending_symbols(self) -> List[str]: ...


@runtime_checkable
class IPositionManager(Protocol):
    """Production interface implemented by `position_manager.PositionManager`."""

    def sync_with_exchange_balance(self, client: Any, settings: Any, min_value_usdt: float = 5.0) -> Optional[List]: ...
    def get_open_positions(self) -> List[Dict[str, Any]]: ...
    def get_all_positions(self) -> List[Dict[str, Any]]: ...
    def close_all_positions_for_symbol(
        self, symbol: str, exit_price: float, reason: str, exit_context: Optional[Dict] = None,
    ) -> Any: ...


@runtime_checkable
class IPortfolioManager(Protocol):
    """Production interface implemented by `portfolio_manager.PortfolioManager`."""

    def get_unified_balance(self, futures_client: Any) -> Dict[str, Any]: ...


@runtime_checkable
class IDatabaseClient(Protocol):
    """Minimal interface for DB operations used by run_trades."""

    def execute_query(self, sql: str, params: Optional[tuple] = None) -> List[Dict[str, Any]]: ...
    def execute_update(self, sql: str, params: Optional[tuple] = None) -> int: ...


class SimulationClock:
    """Deterministic clock used by MIMIC. Replaces `datetime.now(timezone.utc)` calls."""

    def __init__(self, start: datetime):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta_seconds: float) -> None:
        self._now = self._now + __import__('datetime').timedelta(seconds=delta_seconds)

    def isoformat(self) -> str:
        return self._now.isoformat()
