"""Adaptive full-position close planning for Staging.

This module is deliberately exchange-agnostic: callers provide a normalized
order book and remain responsible for submitting and reconciling the order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple


HARD_RISK_URGENCIES = {"HARD_RISK", "EMERGENCY"}
SOFT_URGENCIES = {"STRUCTURAL_INVALIDATION", "SOFT_PROFIT", "MANUAL"}


@dataclass(frozen=True)
class BookSimulation:
    side: str
    quantity: float
    filled_quantity: float
    expected_vwap: Optional[float]
    reference_price: Optional[float]
    spread_bps: Optional[float]
    impact_bps: Optional[float]
    sufficient_depth: bool
    levels_consumed: int
    depth_timestamp: Optional[str] = None

    @property
    def slippage_bps(self) -> Optional[float]:
        if self.expected_vwap is None or not self.reference_price:
            return None
        return abs(self.expected_vwap - self.reference_price) / self.reference_price * 10000.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "side": self.side,
            "quantity": self.quantity,
            "filled_quantity": self.filled_quantity,
            "expected_vwap": self.expected_vwap,
            "reference_price": self.reference_price,
            "spread_bps": self.spread_bps,
            "impact_bps": self.impact_bps,
            "slippage_bps": self.slippage_bps,
            "sufficient_depth": self.sufficient_depth,
            "levels_consumed": self.levels_consumed,
            "depth_timestamp": self.depth_timestamp,
        }


@dataclass(frozen=True)
class CloseExecutionPlan:
    symbol: str
    position_side: str
    order_side: str
    full_quantity: float
    urgency: str
    order_type: str
    expected_vwap: Optional[float]
    max_slippage_bps: float
    estimated_slippage_bps: Optional[float]
    estimated_slippage_usdt: Optional[float]
    sufficient_depth: bool
    reason: str
    decision_at: str
    book: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "position_side": self.position_side,
            "order_side": self.order_side,
            "full_quantity": self.full_quantity,
            "urgency": self.urgency,
            "order_type": self.order_type,
            "expected_vwap": self.expected_vwap,
            "max_slippage_bps": self.max_slippage_bps,
            "estimated_slippage_bps": self.estimated_slippage_bps,
            "estimated_slippage_usdt": self.estimated_slippage_usdt,
            "sufficient_depth": self.sufficient_depth,
            "reason": self.reason,
            "decision_at": self.decision_at,
            "book": self.book,
        }


def _levels(raw_levels: Iterable[Any]) -> List[Tuple[float, float]]:
    levels: List[Tuple[float, float]] = []
    for raw in raw_levels or []:
        try:
            if isinstance(raw, dict):
                price = float(raw.get("price"))
                quantity = float(raw.get("qty", raw.get("quantity")))
            else:
                price = float(raw[0])
                quantity = float(raw[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price > 0 and quantity > 0:
            levels.append((price, quantity))
    return levels


def simulate_close_book(
    position_side: str,
    quantity: float,
    *,
    bids: Iterable[Any] = (),
    asks: Iterable[Any] = (),
    depth_timestamp: Optional[str] = None,
) -> BookSimulation:
    """Simulate consuming the complete close quantity from the correct book side."""
    normalized_position_side = str(position_side or "").strip().upper()
    if normalized_position_side not in {"LONG", "SHORT"}:
        raise ValueError("position_side must be LONG or SHORT")
    if quantity <= 0:
        raise ValueError("quantity must be positive")

    bid_levels = _levels(bids)
    ask_levels = _levels(asks)
    sell_to_bid = normalized_position_side == "LONG"
    close_side = "SELL" if sell_to_bid else "BUY"
    executable_levels = bid_levels if sell_to_bid else ask_levels
    reference = executable_levels[0][0] if executable_levels else None
    opposite = ask_levels[0][0] if sell_to_bid and ask_levels else bid_levels[0][0] if bid_levels else None
    spread_bps = abs(opposite - reference) / reference * 10000.0 if reference and opposite else None

    remaining = float(quantity)
    filled = 0.0
    notional = 0.0
    levels_consumed = 0
    for price, available in executable_levels:
        if remaining <= 0:
            break
        taken = min(remaining, available)
        notional += taken * price
        filled += taken
        remaining -= taken
        levels_consumed += 1

    vwap = notional / filled if filled > 0 else None
    impact_bps = abs(vwap - reference) / reference * 10000.0 if vwap and reference else None
    return BookSimulation(
        side=close_side,
        quantity=float(quantity),
        filled_quantity=filled,
        expected_vwap=vwap,
        reference_price=reference,
        spread_bps=spread_bps,
        impact_bps=impact_bps,
        sufficient_depth=remaining <= 1e-12,
        levels_consumed=levels_consumed,
        depth_timestamp=depth_timestamp,
    )


def build_close_plan(
    symbol: str,
    position_side: str,
    full_quantity: float,
    urgency: str,
    *,
    bids: Iterable[Any] = (),
    asks: Iterable[Any] = (),
    max_slippage_bps: float = 10.0,
    notional_usdt: Optional[float] = None,
    depth_timestamp: Optional[str] = None,
    decision_at: Optional[str] = None,
) -> CloseExecutionPlan:
    """Choose a full-size close order while preserving hard-risk priority."""
    if full_quantity <= 0:
        raise ValueError("full_quantity must be positive")
    normalized_urgency = str(urgency or "").strip().upper()
    if normalized_urgency not in HARD_RISK_URGENCIES | SOFT_URGENCIES:
        raise ValueError(f"unsupported close urgency: {urgency}")
    simulation = simulate_close_book(
        position_side,
        full_quantity,
        bids=bids,
        asks=asks,
        depth_timestamp=depth_timestamp,
    )
    if normalized_urgency in HARD_RISK_URGENCIES:
        order_type = "MARKET"
        reason = "hard_risk_immediate" if normalized_urgency == "HARD_RISK" else "emergency_immediate"
    elif not simulation.sufficient_depth:
        order_type = "MARKET"
        reason = "insufficient_full_close_depth_fallback_market"
    elif simulation.slippage_bps is not None and simulation.slippage_bps <= max(0.0, max_slippage_bps):
        order_type = "LIMIT_FOK"
        reason = "full_size_fok_within_slippage_limit"
    else:
        order_type = "MARKET"
        reason = "estimated_slippage_over_limit_fallback_market"

    slippage_usdt = None
    if simulation.slippage_bps is not None and notional_usdt is not None:
        slippage_usdt = float(notional_usdt) * simulation.slippage_bps / 10000.0
    return CloseExecutionPlan(
        symbol=str(symbol),
        position_side=str(position_side).strip().upper(),
        order_side=simulation.side,
        full_quantity=float(full_quantity),
        urgency=normalized_urgency,
        order_type=order_type,
        expected_vwap=simulation.expected_vwap,
        max_slippage_bps=float(max_slippage_bps),
        estimated_slippage_bps=simulation.slippage_bps,
        estimated_slippage_usdt=slippage_usdt,
        sufficient_depth=simulation.sufficient_depth,
        reason=reason,
        decision_at=decision_at or datetime.now(timezone.utc).isoformat(),
        book=simulation.to_dict(),
    )
