"""lifecycle/sizing.py — PositionSizer and CapitalBudget.

Separates two concerns:
  1. CapitalBudget: how much capital is allocated to this regime right now,
     and how much is already deployed per category.
  2. PositionSizer: given an approved entry and a budget, decide the dollar
     size and quantity of the trade using a Kelly-inspired formula.

The sizer is intentionally simple and interpretable. It scales size with:
- regime allocation (capital available for this regime)
- signal conviction (composite score + success probability)
- Fear & Greed multiplier (more aggressive in fear, less in greed)
- per-category caps and global max_per_trade_usdt
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from lifecycle.base import CapitalBudget, EntryDecision, MarketContext, Position, SizingPolicy, SizedTrade


class PositionSizer:
    """Kelly-inspired position sizing for approved entries."""

    def __init__(self, category_resolver: Optional[Callable[[str], str]] = None):
        self.category_resolver = category_resolver or _default_category

    def build_budget(
        self,
        total_liquid: float,
        positions: List[Position],
        policy: SizingPolicy,
    ) -> CapitalBudget:
        """Construct a CapitalBudget from available capital and current positions."""
        available_for_regime = total_liquid * policy.regime_allocation_pct
        already_deployed: Dict[str, float] = {}
        total_deployed = 0.0

        for p in positions:
            cat = p.category or self.category_resolver(p.symbol)
            already_deployed[cat] = already_deployed.get(cat, 0.0) + p.position_usdt
            total_deployed += p.position_usdt

        # Reduce regime allocation if the regime is already heavily deployed.
        remaining_for_regime = max(0.0, available_for_regime - total_deployed)

        # Category caps are expressed as a fraction of the regime allocation.
        category_caps = {
            cat: available_for_regime * pct
            for cat, pct in policy.category_caps.items()
        }

        return CapitalBudget(
            total_liquid=total_liquid,
            regime_allocation_pct=policy.regime_allocation_pct,
            available_for_regime=remaining_for_regime,
            max_per_trade_usdt=policy.max_per_trade_usdt,
            category_caps=category_caps,
            already_deployed=already_deployed,
        )

    def size(
        self,
        entry: EntryDecision,
        budget: CapitalBudget,
        ctx: MarketContext,
        policy: SizingPolicy,
    ) -> SizedTrade:
        """Return a SizedTrade for the approved entry."""
        signal = entry.signal

        # Conviction = average of composite score and success probability.
        conviction = (signal.composite_score + signal.success_probability) / 2.0
        conviction = max(0.0, min(1.0, conviction))

        # Kelly base grows with the regime's available capital.
        kelly_base = budget.available_for_regime * policy.kelly_base_pct

        # Size scales linearly with conviction; at conviction 0.5 we get exactly kelly_base,
        # at conviction 1.0 we get 2*kelly_base (capped below).
        position_usdt = kelly_base * (conviction * 2.0)

        # Apply F&G multiplier.
        fng_mult = self._fng_multiplier(ctx.fear_greed_index, policy)
        position_usdt *= fng_mult

        # Hard caps.
        position_usdt = min(position_usdt, policy.max_per_trade_usdt)
        position_usdt = min(position_usdt, policy.kelly_max_pct * budget.total_liquid)
        position_usdt = min(position_usdt, policy.conviction_max_pct * budget.total_liquid)

        # Category cap.
        category_remaining = self._category_remaining(signal.category, budget)
        position_usdt = min(position_usdt, category_remaining)

        # Floor: never trade below a meaningful size.
        if position_usdt < 10.0:
            position_usdt = 0.0

        # Quantity from entry price (fallback to 0 if price unavailable).
        price = entry.entry_price or 0.0
        quantity = (position_usdt / price) if price > 0 else 0.0

        multipliers = {
            "conviction": conviction,
            "fng_multiplier": fng_mult,
            "category_remaining": category_remaining,
        }

        return SizedTrade(
            entry_decision=entry,
            position_usdt=round(position_usdt, 2),
            quantity=round(quantity, 8),
            budget=budget,
            multipliers=multipliers,
        )

    def _fng_multiplier(self, fng: float, policy: SizingPolicy) -> float:
        """Return a capital multiplier based on Fear & Greed Index.

        Extreme fear (<25) → +20% size (opportunity).
        Extreme greed (>75) → -20% size (risk).
        """
        if not policy.fng_capital_multiplier:
            return 1.0
        if fng <= 20:
            return 1.25
        if fng <= 35:
            return 1.10
        if fng >= 75:
            return 0.75
        if fng >= 60:
            return 0.90
        return 1.0

    def _category_remaining(self, category: str, budget: CapitalBudget) -> float:
        cap = budget.category_caps.get(category, budget.available_for_regime)
        deployed = budget.already_deployed.get(category, 0.0)
        return max(0.0, cap - deployed)


def _default_category(symbol: str) -> str:
    from lifecycle.signal import _DEFAULT_CATEGORIES
    return _DEFAULT_CATEGORIES.get(symbol, "MIDCAP_ALT")


def size_entry(
    entry: EntryDecision,
    total_liquid: float,
    positions: List[Position],
    ctx: MarketContext,
    policy: SizingPolicy,
    category_resolver: Optional[Callable[[str], str]] = None,
) -> SizedTrade:
    """One-shot helper: build budget + size a single entry."""
    sizer = PositionSizer(category_resolver=category_resolver)
    budget = sizer.build_budget(total_liquid, positions, policy)
    return sizer.size(entry, budget, ctx, policy)
