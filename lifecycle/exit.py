"""lifecycle/exit.py — ExitEngine: unified, policy-driven exit engine.

Replaces the fragmented SSO / PRS / PeakCapture / UG logic with a single,
declarative engine. For each open position it evaluates:

  1. Hard SL/TP (ATR-based)
  2. Trailing stop
  3. Breakeven stop
  4. Partial takes (TP1, TP2, ...)
  5. Momentum exit (RSI / Fear & Greed extremes)
  6. Stagnation / decay rule

The highest-priority action per position wins. If multiple partial-close
actions have the same priority they are merged into a single CLOSE_PCT with
the largest requested percentage.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Dict, List, Optional, Tuple

from lifecycle.base import (
    Action,
    ExitDecision,
    ExitPolicy,
    MarketContext,
    Position,
    Side,
)


class ExitEngine:
    """Evaluate open positions against an ExitPolicy and return ExitDecisions."""

    def __init__(
        self,
        price_resolver: Optional[Callable[[str], Optional[float]]] = None,
        atr_resolver: Optional[Callable[[str], Optional[float]]] = None,
        rsi_resolver: Optional[Callable[[str], Optional[float]]] = None,
    ):
        self.price_resolver = price_resolver or (lambda _s: None)
        self.atr_resolver = atr_resolver or (lambda _s: None)
        self.rsi_resolver = rsi_resolver or (lambda _s: None)

    def evaluate(
        self,
        positions: List[Position],
        ctx: MarketContext,
        policy: ExitPolicy,
        now: float,
    ) -> List[ExitDecision]:
        """Return a list of ExitDecisions, one per position (or HOLD)."""
        decisions: List[ExitDecision] = []
        for pos in positions:
            price = self.price_resolver(pos.symbol)
            atr = self.atr_resolver(pos.symbol)
            rsi = self.rsi_resolver(pos.symbol)
            if price is None:
                decisions.append(ExitDecision.hold(pos.symbol, "no price"))
                continue

            candidates = []
            candidates.extend(self._hard_sl_tp(pos, price, atr, policy, now))
            candidates.extend(self._expected_gain_tp(pos, price, policy, now))
            candidates.extend(self._trailing_stop(pos, price, atr, policy, now))
            candidates.extend(self._breakeven_stop(pos, price, atr, policy, now))
            candidates.extend(self._partial_takes(pos, price, atr, policy, now))
            candidates.extend(self._momentum_exit(pos, price, rsi, ctx, policy, now))
            candidates.extend(self._stagnation(pos, price, policy, now))
            candidates.extend(self._time_stop(pos, policy, now))

            if not candidates:
                decisions.append(ExitDecision.hold(pos.symbol))
                continue

            # Highest priority wins. If multiple partials share the top priority,
            # merge them by taking the largest percentage.
            candidates.sort(key=lambda d: (-d.priority, d.action.value))
            top_priority = candidates[0].priority
            top_candidates = [d for d in candidates if d.priority == top_priority]

            if top_candidates[0].action == Action.CLOSE_ALL:
                decisions.append(top_candidates[0])
            elif top_candidates[0].action == Action.CLOSE_PCT and not policy.allow_partial_exits:
                decisions.append(ExitDecision.hold(pos.symbol, "partial_exit_blocked"))
            elif top_candidates[0].action == Action.CLOSE_PCT:
                merged_pct = max(d.pct for d in top_candidates if d.action == Action.CLOSE_PCT)
                merged_reason = " + ".join(d.reason for d in top_candidates if d.action == Action.CLOSE_PCT)
                decisions.append(ExitDecision.close_pct(
                    pos.symbol,
                    merged_pct,
                    merged_reason,
                    top_priority,
                ))
            else:
                decisions.append(top_candidates[0])

        return decisions

    def _pnl_pct(self, pos: Position, price: float) -> float:
        """Return unrealized PnL % for the position."""
        if pos.side == Side.LONG:
            return (price - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0.0
        return (pos.entry_price - price) / pos.entry_price if pos.entry_price > 0 else 0.0

    def _hours_held(self, pos: Position, now: float) -> float:
        return (now - pos.entry_time) / 3600.0

    def _expected_gain_tp(
        self,
        pos: Position,
        price: float,
        policy: ExitPolicy,
        now: float,
    ) -> List[ExitDecision]:
        """Close when the position reaches the predicted peak return."""
        if not policy.use_expected_gain_for_tp:
            return []
        expected = pos.metadata.get("expected_peak_return")
        if expected is None or float(expected) <= 0:
            return []
        target = float(expected) * float(policy.expected_gain_mult)
        pnl_pct = self._pnl_pct(pos, price)
        if pnl_pct >= target:
            return [ExitDecision.close_all(pos.symbol, "expected gain TP", 92)]
        return []

    def _time_stop(
        self,
        pos: Position,
        policy: ExitPolicy,
        now: float,
    ) -> List[ExitDecision]:
        """Hard maximum holding-time stop."""
        if policy.time_stop_hours is None or policy.time_stop_hours <= 0:
            return []
        if self._hours_held(pos, now) >= policy.time_stop_hours:
            return [ExitDecision.close_all(pos.symbol, "time stop", 94)]
        return []

    def _hard_sl_tp(
        self,
        pos: Position,
        price: float,
        atr: Optional[float],
        policy: ExitPolicy,
        now: float,
    ) -> List[ExitDecision]:
        """Hard stop-loss and take-profit based on ATR multiples."""
        if atr is None or atr <= 0:
            return []
        out: List[ExitDecision] = []
        pnl_pct = self._pnl_pct(pos, price)
        sl_distance = atr * policy.hard_sl_atr_mult / pos.entry_price
        tp_distance = atr * policy.hard_tp_atr_mult / pos.entry_price

        if pos.side == Side.LONG:
            if pnl_pct <= -sl_distance:
                out.append(ExitDecision.close_all(pos.symbol, "hard SL", 90))
            elif pnl_pct >= tp_distance:
                out.append(ExitDecision.close_all(pos.symbol, "hard TP", 70))
        else:  # SHORT
            if pnl_pct <= -sl_distance:
                out.append(ExitDecision.close_all(pos.symbol, "hard SL (short)", 90))
            elif pnl_pct >= tp_distance:
                out.append(ExitDecision.close_all(pos.symbol, "hard TP (short)", 70))
        return out

    def _trailing_stop(
        self,
        pos: Position,
        price: float,
        atr: Optional[float],
        policy: ExitPolicy,
        now: float,
    ) -> List[ExitDecision]:
        """Trailing stop once activation PnL is reached."""
        if atr is None or atr <= 0:
            return []
        pnl_pct = self._pnl_pct(pos, price)
        activation = policy.trailing_activation_pct
        trail_distance = atr * policy.trailing_atr_mult / pos.entry_price
        peak = pos.peak_pnl_pct

        if pos.side == Side.LONG:
            if pnl_pct >= activation:
                drawback = peak - pnl_pct
                if drawback >= trail_distance:
                    return [ExitDecision.close_all(pos.symbol, "trailing stop", 85)]
        else:  # SHORT
            if pnl_pct >= activation:
                drawback = peak - pnl_pct
                if drawback >= trail_distance:
                    return [ExitDecision.close_all(pos.symbol, "trailing stop (short)", 85)]
        return []

    def _breakeven_stop(
        self,
        pos: Position,
        price: float,
        atr: Optional[float],
        policy: ExitPolicy,
        now: float,
    ) -> List[ExitDecision]:
        """Move stop to breakeven once activation PnL is reached."""
        pnl_pct = self._pnl_pct(pos, price)
        activation = policy.breakeven_activation_pct
        if pnl_pct >= activation:
            # If price has retreated back to entry or slightly below, close at breakeven.
            if pos.side == Side.LONG and price <= pos.entry_price * (1 + 0.001):
                return [ExitDecision.close_all(pos.symbol, "breakeven stop", 80)]
            if pos.side == Side.SHORT and price >= pos.entry_price * (1 - 0.001):
                return [ExitDecision.close_all(pos.symbol, "breakeven stop (short)", 80)]
        return []

    def _partial_takes(
        self,
        pos: Position,
        price: float,
        atr: Optional[float],
        policy: ExitPolicy,
        now: float,
    ) -> List[ExitDecision]:
        """Partial take-profits at AR multiples."""
        if atr is None or atr <= 0:
            return []
        out: List[ExitDecision] = []
        pnl_pct = self._pnl_pct(pos, price)
        for pt in policy.partial_takes:
            target = atr * pt.trigger_atr_mult / pos.entry_price
            if pnl_pct >= target:
                out.append(ExitDecision.close_pct(pos.symbol, pt.pct, f"partial {pt.label}", 60))
        return out

    def _momentum_exit(
        self,
        pos: Position,
        price: float,
        rsi: Optional[float],
        ctx: MarketContext,
        policy: ExitPolicy,
        now: float,
    ) -> List[ExitDecision]:
        """Close on momentum extremes (RSI + Fear & Greed)."""
        out: List[ExitDecision] = []
        pnl_pct = self._pnl_pct(pos, price)
        rule = policy.momentum_exit

        if pos.side == Side.LONG:
            if rsi is not None and rsi >= rule.rsi_overbought and pnl_pct > 0.0:
                out.append(ExitDecision.close_all(pos.symbol, "RSI overbought", 75))
            if ctx.fear_greed_index >= rule.fng_extreme_greed and pnl_pct > 0.0:
                out.append(ExitDecision.close_all(pos.symbol, "F&G extreme greed", 75))
        else:  # SHORT
            if rsi is not None and rsi <= rule.rsi_oversold and pnl_pct > 0.0:
                out.append(ExitDecision.close_all(pos.symbol, "RSI oversold (short)", 75))
            if ctx.fear_greed_index <= rule.fng_extreme_fear and pnl_pct > 0.0:
                out.append(ExitDecision.close_all(pos.symbol, "F&G extreme fear (short)", 75))
        return out

    def _stagnation(
        self,
        pos: Position,
        price: float,
        policy: ExitPolicy,
        now: float,
    ) -> List[ExitDecision]:
        """Close positions that have stagnated or decayed."""
        hours = self._hours_held(pos, now)
        if hours < policy.stagnation_rule.min_hours:
            return []
        pnl_pct = self._pnl_pct(pos, price)
        peak = pos.peak_pnl_pct
        drawdown_from_peak = peak - pnl_pct

        if drawdown_from_peak >= policy.stagnation_rule.max_drawdown_from_peak_pct:
            return [ExitDecision.close_all(pos.symbol, "stagnation/decay", 65)]
        return []


def update_peak_pnl(positions: List[Position], price_resolver: Callable[[str], Optional[float]]) -> List[Position]:
    """Return a copy of positions with peak_pnl_pct updated to the current price."""
    updated = []
    for pos in positions:
        price = price_resolver(pos.symbol)
        if price is None:
            updated.append(pos)
            continue
        pnl_pct = (price - pos.entry_price) / pos.entry_price if pos.side == Side.LONG and pos.entry_price > 0 else 0.0
        if pos.side == Side.SHORT and pos.entry_price > 0:
            pnl_pct = (pos.entry_price - price) / pos.entry_price
        new_peak = max(pos.peak_pnl_pct, pnl_pct)
        updated.append(replace(pos, peak_pnl_pct=new_peak))
    return updated
