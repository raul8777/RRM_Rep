"""lifecycle/adapter_futures.py — concrete FUTURES adapter using FuturesClient.

Bridges the lifecycle kernel to the existing USDM Futures execution stack.
Currently supports SHORT entries (the dominant FUTURES_SHORT regime) and full/partial
closes. LONG futures can be added later with the same pattern.
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


class BinanceFuturesAdapter:
    """FUTURES execution adapter (USDM)."""

    def __init__(
        self,
        futures_client: Any,
        db_client: Any,
        price_resolver: Optional[Callable[[str], Optional[float]]] = None,
        atr_resolver: Optional[Callable[[str], Optional[float]]] = None,
        rsi_resolver: Optional[Callable[[str], Optional[float]]] = None,
        funding_resolver: Optional[Callable[[], Optional[float]]] = None,
        fng_resolver: Optional[Callable[[], int]] = None,
        btc_dom_resolver: Optional[Callable[[], float]] = None,
        category_resolver: Optional[Callable[[str], str]] = None,
    ):
        self.futures_client = futures_client
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
            return self.futures_client.get_price(symbol)
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
        """Return tracked open futures positions as lifecycle kernel Positions."""
        raw = self.futures_client.get_open_positions_summary()
        out: List[Position] = []
        for p in raw:
            meta = p.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            side = Side.SHORT if p.get("side") == "SHORT" else Side.LONG
            opened_at = p.get("opened_at") or time.time()
            if isinstance(opened_at, str):
                try:
                    opened_at = time.mktime(time.strptime(opened_at, "%Y-%m-%dT%H:%M:%S"))
                except Exception:
                    opened_at = time.time()
            out.append(Position(
                symbol=p["symbol"],
                side=side,
                quantity=abs(float(p.get("qty", 0.0))),
                entry_price=float(p.get("entry_price", 0.0)),
                entry_time=float(opened_at),
                position_usdt=float(p.get("position_usdt", 0.0) or p.get("margin", 0.0)),
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

        if entry.signal.side == Side.SHORT:
            # Map lifecycle exit policy SL/TP pct to futures_client args
            sl_pct = config.exit_policy.hard_sl_atr_mult * 1.0  # placeholder mapping
            tp_pct = config.exit_policy.hard_tp_atr_mult * 1.0
            pos = self.futures_client.open_short(
                symbol=symbol,
                usdt_amount=trade.position_usdt,
                sl_pct=sl_pct,
                tp_pct=tp_pct,
                leverage=getattr(self.futures_client, "_active_leverage", 10),
            )
            return {
                "filled": pos is not None,
                "price": price,
                "position": pos,
            }

        # LONG futures support can be added here
        return {"filled": False, "reason": "LONG futures not yet supported by adapter"}

    def execute_exit(self, decision: ExitDecision, position: Position, config: LifecycleConfig) -> Dict:
        symbol = decision.symbol
        price = self.price_resolver(symbol) or 0.0

        if decision.action.value == "CLOSE_ALL":
            result = self.futures_client.close_short(symbol, qty=position.quantity, reason=decision.reason)
            return {"filled": result is not None, "price": price, "raw": result}

        # Partial close
        qty = position.quantity * decision.pct
        result = self.futures_client.close_short(symbol, qty=qty, reason=decision.reason)
        return {"filled": result is not None, "price": price, "pct": decision.pct, "raw": result}

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
                    "futures",
                    "LIFECYCLE_DECISION",
                    json.dumps({"regime": config.regime, "profile": config.profile, "result": result}),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        except Exception:
            pass
