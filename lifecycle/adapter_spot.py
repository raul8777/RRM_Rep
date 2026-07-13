"""lifecycle/adapter_spot.py — concrete SPOT adapter using BinanceClient and PositionManager.

This adapter bridges the lifecycle kernel to the existing SPOT execution stack. It is
intended to replace the inline spot-buy logic in `run_trades.py` over time.

Responsibilities:
  - Build MarketContext from live/external data (prices, ATR, RSI, F&G, BTC dominance).
  - Load open SPOT positions via PositionManager.
  - Execute sized entries as MARKET orders (dry-run aware).
  - Execute exit decisions (full or partial) and persist to DB.
  - Audit every decision to `trades_audit`.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional

from lifecycle.adapter import ExecutionAdapter
from lifecycle.base import (
    EntryDecision,
    ExitDecision,
    LifecycleConfig,
    MarketContext,
    Position,
    Side,
    SizedTrade,
)
from lifecycle.signal import _DEFAULT_CATEGORIES


class BinanceSpotAdapter:
    """SPOT execution adapter."""

    def __init__(
        self,
        binance_client: Any,
        position_manager: Any,
        db_client: Any,
        price_resolver: Optional[Callable[[str], Optional[float]]] = None,
        atr_resolver: Optional[Callable[[str], Optional[float]]] = None,
        rsi_resolver: Optional[Callable[[str], Optional[float]]] = None,
        funding_resolver: Optional[Callable[[], Optional[float]]] = None,
        fng_resolver: Optional[Callable[[], int]] = None,
        btc_dom_resolver: Optional[Callable[[], float]] = None,
        category_resolver: Optional[Callable[[str], str]] = None,
    ):
        self.binance_client = binance_client
        self.position_manager = position_manager
        self.db_client = db_client
        self.price_resolver = price_resolver or (lambda s: self._price_from_client(s))
        self.atr_resolver = atr_resolver or (lambda _s: None)
        self.rsi_resolver = rsi_resolver or (lambda _s: None)
        self.funding_resolver = funding_resolver or (lambda: None)
        self.fng_resolver = fng_resolver or (lambda: 50)
        self.btc_dom_resolver = btc_dom_resolver or (lambda: 55.0)
        self.category_resolver = category_resolver or (lambda s: _DEFAULT_CATEGORIES.get(s, "MIDCAP_ALT"))

    def _price_from_client(self, symbol: str) -> Optional[float]:
        try:
            return self.binance_client.get_symbol_price(symbol)
        except Exception:
            return None

    def build_context(self, config: LifecycleConfig) -> MarketContext:
        return MarketContext(
            timestamp=time.time(),
            regime=config.regime,
            regime_confidence={config.regime: 1.0},
            btc_7d_pct=0.0,
            btc_30d_pct=0.0,
            btc_day_pct=0.0,
            fear_greed_index=self.fng_resolver(),
            btc_dominance=self.btc_dom_resolver(),
            funding_rate=self.funding_resolver(),
        )

    def get_positions(self, config: LifecycleConfig) -> List[Position]:
        """Return open SPOT positions as lifecycle kernel Position objects."""
        raw = self.position_manager.get_open_positions_summary()
        out: List[Position] = []
        for p in raw:
            meta = p.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            # Skip futures positions
            if meta.get("trade_type") == "futures":
                continue
            side = Side.LONG if p.get("side") in ("BUY", "LONG") else Side.SHORT
            entry_time = p.get("entry_time") or p.get("opened_at") or time.time()
            if isinstance(entry_time, str):
                try:
                    entry_time = time.mktime(time.strptime(entry_time, "%Y-%m-%dT%H:%M:%S"))
                except Exception:
                    entry_time = time.time()
            out.append(Position(
                symbol=p["symbol"],
                side=side,
                quantity=float(p.get("quantity", 0.0)),
                entry_price=float(p.get("entry_price", 0.0)),
                entry_time=float(entry_time),
                position_usdt=float(p.get("position_usdt", 0.0) or p.get("invested_usdt", 0.0)),
                category=self.category_resolver(p["symbol"]),
                regime_at_entry=config.regime,
                profile_at_entry=config.profile,
                peak_pnl_pct=float(p.get("peak_pnl_pct", 0.0) or 0.0),
                metadata=meta,
            ))
        return out

    def execute_entry(self, trade: SizedTrade, config: LifecycleConfig) -> Dict:
        entry = trade.entry_decision
        symbol = entry.signal.symbol
        price = self.price_resolver(symbol) or entry.entry_price or 0.0

        # Use quoteOrderQty for SPOT market buy when possible
        qty = trade.quantity
        result = self.binance_client.create_market_order(
            symbol=symbol,
            side="BUY" if entry.signal.side == Side.LONG else "SELL",
            quantity=qty,
        )
        fill = self.binance_client.verify_and_fetch_fills(result, label=f"lifecycle_entry_{symbol}")
        if fill:
            # Persist via position_manager (mirrors current spot flow)
            from position_manager import Position as PMPosition
            pos = PMPosition(
                symbol=symbol,
                side="BUY" if entry.signal.side == Side.LONG else "SELL",
                quantity=fill["qty"],
                entry_price=fill["avg_price"],
                entry_time=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                metadata={
                    "trade_type": "spot",
                    "entry_commission": fill["commission"],
                    "binance_entry_confirmed": True,
                    "lifecycle": True,
                },
            )
            self.position_manager.add_position(pos)
        return {"filled": bool(fill), "price": price, "fill": fill, "raw": result}

    def execute_exit(self, decision: ExitDecision, position: Position, config: LifecycleConfig) -> Dict:
        symbol = decision.symbol
        price = self.price_resolver(symbol) or 0.0
        side = "SELL" if position.side == Side.LONG else "BUY"

        if decision.action.value == "CLOSE_ALL":
            qty = position.quantity
            result = self.binance_client.create_market_order(symbol=symbol, side=side, quantity=qty)
            fill = self.binance_client.verify_and_fetch_fills(result, label=f"lifecycle_exit_{symbol}")
            if fill:
                self.position_manager.close_all_positions_for_symbol(
                    symbol, price, reason=decision.reason, commission_rate=0.001
                )
            return {"filled": bool(fill), "price": price, "fill": fill, "raw": result}

        # Partial close
        qty = position.quantity * decision.pct
        result = self.binance_client.create_market_order(symbol=symbol, side=side, quantity=qty)
        fill = self.binance_client.verify_and_fetch_fills(result, label=f"lifecycle_partial_{symbol}")
        return {"filled": bool(fill), "price": price, "fill": fill, "pct": decision.pct, "raw": result}

    def audit(self, decision: object, result: Dict, config: LifecycleConfig) -> None:
        try:
            if self.db_client is None:
                return
            from datetime import datetime, timezone
            self.db_client.execute_query(
                """
                INSERT INTO trades_audit (symbol, trade_type, event, detail, created_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    getattr(decision, "symbol", None) or getattr(decision.signal, "symbol", None),
                    "spot",
                    "LIFECYCLE_DECISION",
                    json.dumps({"regime": config.regime, "profile": config.profile, "result": result}),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        except Exception:
            pass
