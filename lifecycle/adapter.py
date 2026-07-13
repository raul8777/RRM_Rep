"""lifecycle/adapter.py — execution adapter protocol for SPOT / FUTURES / MIMIC.

An adapter bridges the lifecycle kernel to a concrete execution environment. It
must provide:

  - market context construction (prices, ATR, RSI, etc.)
  - position loading
  - entry execution and persistence
  - exit execution and persistence
  - audit logging

The kernel is intentionally blind to the adapter; the adapter implements the
protocol and translates kernel decisions into Binance orders, DB rows, or MIMIC
simulation events.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Protocol

from lifecycle.base import (
    EntryDecision,
    ExitDecision,
    LifecycleConfig,
    MarketContext,
    Position,
    SizedTrade,
)


class ExecutionAdapter(Protocol):
    """Protocol for lifecycle execution adapters."""

    def build_context(self, config: LifecycleConfig) -> MarketContext:
        """Build the current MarketContext for the kernel."""
        ...

    def get_positions(self, config: LifecycleConfig) -> List[Position]:
        """Return open positions relevant to this regime/config."""
        ...

    def execute_entry(self, trade: SizedTrade, config: LifecycleConfig) -> Dict:
        """Execute a sized entry and return a result dict (with order_id, filled, etc.)."""
        ...

    def execute_exit(self, decision: ExitDecision, position: Position, config: LifecycleConfig) -> Dict:
        """Execute an exit decision and return a result dict."""
        ...

    def audit(self, decision: object, result: Dict, config: LifecycleConfig) -> None:
        """Persist an audit record for every lifecycle decision."""
        ...
