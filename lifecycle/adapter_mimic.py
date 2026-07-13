"""lifecycle/adapter_mimic.py — MIMIC-compatible adapter for backtesting lifecycle configs.

This adapter is intentionally lightweight. It runs the lifecycle kernel over
historical MIMIC-style data without touching Binance or the live DB. In a real
Super MIMIC experiment this adapter will be fed by the simulation engine
(mimic_historical_screener / mimic_db) and its results will be evaluated by the
deploy gate.

For now it provides an in-memory execution sandbox used for unit tests and
sweep prototyping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

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


@dataclass
class MimicExecutionAdapter:
    """In-memory adapter for MIMIC / dry-run testing."""

    initial_capital: float = 5000.0
    prices: Dict[str, float] = field(default_factory=dict)
    atrs: Dict[str, float] = field(default_factory=dict)
    rsis: Dict[str, float] = field(default_factory=dict)
    funding_rate: Optional[float] = None
    fear_greed: float = 50.0
    btc_dominance: float = 55.0
    timestamp: float = 0.0

    # Internal state
    positions: List[Position] = field(default_factory=list)
    orders: List[Dict] = field(default_factory=list)
    audit_log: List[Dict] = field(default_factory=list)

    def build_context(self, config: LifecycleConfig) -> MarketContext:
        return MarketContext(
            timestamp=self.timestamp,
            regime=config.regime,
            regime_confidence={config.regime: 1.0},
            btc_7d_pct=0.0,
            btc_30d_pct=0.0,
            btc_day_pct=0.0,
            fear_greed_index=self.fear_greed,
            btc_dominance=self.btc_dominance,
            funding_rate=self.funding_rate,
        )

    def get_positions(self, config: LifecycleConfig) -> List[Position]:
        return [p for p in self.positions if p.regime_at_entry == config.regime]

    def execute_entry(self, trade: SizedTrade, config: LifecycleConfig) -> Dict:
        entry = trade.entry_decision
        price = self.prices.get(entry.signal.symbol, entry.entry_price or 0.0)
        pos = Position(
            symbol=entry.signal.symbol,
            side=entry.signal.side,
            quantity=trade.quantity,
            entry_price=price,
            entry_time=self.timestamp,
            position_usdt=trade.position_usdt,
            category=entry.signal.category,
            regime_at_entry=config.regime,
            profile_at_entry=config.profile,
            metadata={"result": "entry"},
        )
        self.positions.append(pos)
        order = {"symbol": pos.symbol, "side": pos.side.value, "qty": pos.quantity, "price": price}
        self.orders.append(order)
        return {"filled": True, "price": price, "order": order}

    def execute_exit(self, decision: ExitDecision, position: Position, config: LifecycleConfig) -> Dict:
        price = self.prices.get(decision.symbol, 0.0)
        order = {
            "symbol": decision.symbol,
            "action": decision.action.value,
            "pct": decision.pct,
            "reason": decision.reason,
            "price": price,
        }
        self.orders.append(order)
        if decision.action.value == "CLOSE_ALL":
            self.positions = [p for p in self.positions if p.symbol != decision.symbol]
        else:
            # Reduce quantity proportionally for partial close
            for p in self.positions:
                if p.symbol == decision.symbol:
                    p.quantity *= (1 - decision.pct)
                    p.position_usdt *= (1 - decision.pct)
        return {"filled": True, "price": price, "order": order}

    def audit(self, decision: object, result: Dict, config: LifecycleConfig) -> None:
        self.audit_log.append({
            "timestamp": self.timestamp,
            "regime": config.regime,
            "profile": config.profile,
            "decision": decision,
            "result": result,
        })

    def price_resolver(self, symbol: str) -> Optional[float]:
        return self.prices.get(symbol)

    def atr_resolver(self, symbol: str) -> Optional[float]:
        return self.atrs.get(symbol)

    def rsi_resolver(self, symbol: str) -> Optional[float]:
        return self.rsis.get(symbol)
