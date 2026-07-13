"""lifecycle/entry.py — EntryGate: unifies all entry gates into a single pass.

The EntryGate takes ranked Signals from the SignalPipeline and decides which ones
are allowed to enter. It enforces:

- Composite score, expected PnL, and success probability thresholds
- Liquidity and BTC-dominance gates
- Funding-rate gate for LONG/SHORT entries
- Global and per-category position limits
- VolSpike override (special entry path for high-volume signals)
- Regime-specific rules injected via the policy

The gate is stateless: callers pass the current position snapshot and the entry
price / ATR for each signal.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Set, Tuple

from lifecycle.base import EntryDecision, EntryPolicy, MarketContext, Position, Side, Signal


class EntryGate:
    """Unified entry gate."""

    def __init__(
        self,
        atr_resolver: Optional[Callable[[str], Optional[float]]] = None,
        price_resolver: Optional[Callable[[str], Optional[float]]] = None,
        category_resolver: Optional[Callable[[str], str]] = None,
    ):
        self.atr_resolver = atr_resolver or (lambda _s: None)
        self.price_resolver = price_resolver or (lambda _s: None)
        self.category_resolver = category_resolver or (lambda s: _default_category(s))

    def evaluate(
        self,
        signals: List[Signal],
        ctx: MarketContext,
        policy: EntryPolicy,
        positions: List[Position],
    ) -> List[EntryDecision]:
        """Return approved EntryDecisions for the given signals."""
        # Build current category counts from positions
        category_counts: Dict[str, int] = {}
        symbol_count: Dict[str, int] = {}
        for p in positions:
            cat = p.category or self.category_resolver(p.symbol)
            category_counts[cat] = category_counts.get(cat, 0) + 1
            symbol_count[p.symbol] = symbol_count.get(p.symbol, 0) + 1

        total_positions = len(positions)
        approved: List[EntryDecision] = []

        for signal in signals:
            decision = self._evaluate_one(signal, ctx, policy, positions, total_positions, category_counts, symbol_count)
            approved.append(decision)
            if decision.approved:
                total_positions += 1
                category_counts[signal.category] = category_counts.get(signal.category, 0) + 1
                symbol_count[signal.symbol] = symbol_count.get(signal.symbol, 0) + 1

        return [d for d in approved if d.approved]

    def _evaluate_one(
        self,
        signal: Signal,
        ctx: MarketContext,
        policy: EntryPolicy,
        positions: List[Position],
        total_positions: int,
        category_counts: Dict[str, int],
        symbol_count: Dict[str, int],
    ) -> EntryDecision:
        reasons: List[str] = []
        entry_price = self.price_resolver(signal.symbol)
        atr = self.atr_resolver(signal.symbol)

        # Hard numeric thresholds
        if signal.composite_score < policy.min_composite_score:
            reasons.append(f"score {signal.composite_score:.3f} < {policy.min_composite_score}")
        if signal.expected_pnl < policy.min_expected_pnl:
            reasons.append(f"exp_pnl {signal.expected_pnl:.2f} < {policy.min_expected_pnl}")
        if signal.success_probability < policy.min_success_probability:
            reasons.append(f"prob {signal.success_probability:.3f} < {policy.min_success_probability}")
        if signal.liquidity_usdt_24h < policy.min_liquidity_usdt:
            reasons.append(f"liquidity {signal.liquidity_usdt_24h:.0f} < {policy.min_liquidity_usdt}")

        # BTC dominance gate
        if policy.btc_dominance_min is not None and ctx.btc_dominance < policy.btc_dominance_min:
            reasons.append(f"btc_dominance {ctx.btc_dominance:.2f} < {policy.btc_dominance_min}")
        if policy.btc_dominance_max is not None and ctx.btc_dominance > policy.btc_dominance_max:
            reasons.append(f"btc_dominance {ctx.btc_dominance:.2f} > {policy.btc_dominance_max}")

        # Funding-rate gate (reject longs when funding is very high, shorts when very negative)
        if ctx.funding_rate is not None and policy.funding_rate_max is not None:
            if signal.side == Side.LONG and ctx.funding_rate > policy.funding_rate_max:
                reasons.append(f"funding_rate {ctx.funding_rate:.4f} > {policy.funding_rate_max}")
            if signal.side == Side.SHORT and ctx.funding_rate < -policy.funding_rate_max:
                reasons.append(f"funding_rate {ctx.funding_rate:.4f} < -{policy.funding_rate_max}")

        # Position limit gates
        if total_positions >= policy.max_total_positions:
            reasons.append(f"total_positions {total_positions} >= {policy.max_total_positions}")
        cat_cap = policy.max_category_positions.get(signal.category)
        if cat_cap is not None and category_counts.get(signal.category, 0) >= cat_cap:
            reasons.append(f"category {signal.category} full ({cat_cap})")
        # Max positions per symbol (default 1 per side per symbol — same as existing system)
        if symbol_count.get(signal.symbol, 0) >= 1:
            reasons.append(f"symbol {signal.symbol} already in portfolio")

        # VolSpike override: if enabled and the signal has a strong volume spike but
        # fails a minor threshold, we can grant a conditional approval. This is a gate,
        # not a guarantee — it still must pass liquidity and position limits.
        if reasons and policy.volspike_override_enabled:
            vol_spike = signal.features.get("vol_spike", 0.0)
            if self._volspike_override(signal, vol_spike, reasons):
                # Remove only score/probability/exp_pnl reasons, keep structural ones
                reasons = [r for r in reasons if not any(k in r for k in ("score", "exp_pnl", "prob"))]

        # Regime-specific rules (injected by adapter / policy)
        for rule_name, rule_fn in policy.regime_specific_rules.items():
            try:
                if not rule_fn(signal, ctx, positions):
                    reasons.append(f"regime_rule:{rule_name}")
            except Exception as e:
                reasons.append(f"regime_rule:{rule_name} error {e}")

        if reasons:
            return EntryDecision(
                signal=signal,
                approved=False,
                reason="; ".join(reasons),
                entry_price=entry_price,
                atr=atr,
            )

        return EntryDecision(
            signal=signal,
            approved=True,
            reason="approved",
            entry_price=entry_price,
            atr=atr,
        )

    def _volspike_override(self, signal: Signal, vol_spike: float, reasons: List[str]) -> bool:
        """Conditional override for high-conviction volume signals.

        Only overrides score / expected_pnl / probability shortfalls. It does NOT
        override structural limits (liquidity, category full, etc.).
        """
        if signal.side != Side.LONG:
            return False
        if vol_spike < 2.0:
            return False
        if signal.signal_strength < 0.55:
            return False
        # Must have failed only soft thresholds, not hard ones.
        hard_reasons = {"liquidity", "total_positions", "category", "symbol", "funding_rate", "btc_dominance"}
        for reason in reasons:
            if any(h in reason for h in hard_reasons):
                return False
        return True


def _default_category(symbol: str) -> str:
    from lifecycle.signal import _DEFAULT_CATEGORIES
    return _DEFAULT_CATEGORIES.get(symbol, "MIDCAP_ALT")


def count_positions_by_category(positions: List[Position], category_resolver: Optional[Callable[[str], str]] = None) -> Dict[str, int]:
    """Utility: count open positions per category."""
    resolver = category_resolver or _default_category
    counts: Dict[str, int] = {}
    for p in positions:
        cat = p.category or resolver(p.symbol)
        counts[cat] = counts.get(cat, 0) + 1
    return counts
