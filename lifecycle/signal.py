"""lifecycle/signal.py — SignalPipeline: declarative signal generation and ranking.

The pipeline converts raw symbol analysis dicts (from AutonomousSignalGenerator,
MIMIC historical screener, or tests) into `Signal` objects, filters them by a
`SignalPolicy`, and diversifies by category before returning ranked candidates.

Design goals:
- Stateless (no cache here — callers may cache analysis dicts).
- Category-aware (replaces the QUBO portfolio selector with a simpler, faster
  greedy diversification by orthogonal narratives).
- Regime-aware (uses MarketContext to adjust score thresholds when confidence
  is low and to reject signals that conflict with the dominant regime).
- No Binance / DB dependencies inside the kernel.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Callable, Dict, List, Optional, Set, Tuple

from lifecycle.base import MarketContext, Side, Signal, SignalPolicy


# Symbol → market_behavior category resolver. Can be replaced by any callable.
_DEFAULT_CATEGORIES: Dict[str, str] = {
    "BTCUSDT": "LARGE_CAP", "ETHUSDT": "LARGE_CAP", "BNBUSDT": "LARGE_CAP",
    "SOLUSDT": "MIDCAP_ALT", "AVAXUSDT": "MIDCAP_ALT", "DOTUSDT": "MIDCAP_ALT",
    "LINKUSDT": "MIDCAP_ALT", "SUIUSDT": "MIDCAP_ALT", "ADAUSDT": "MIDCAP_ALT",
    "XRPUSDT": "MIDCAP_ALT", "UNIUSDT": "MIDCAP_ALT", "AAVEUSDT": "MIDCAP_ALT",
    "BCHUSDT": "MIDCAP_ALT", "ZECUSDT": "MIDCAP_ALT", "LTCUSDT": "MIDCAP_ALT",
    "XLMUSDT": "MIDCAP_ALT", "ZROUSDT": "MIDCAP_ALT", "HBARUSDT": "MIDCAP_ALT",
    "ARBUSDT": "MIDCAP_ALT", "MATICUSDT": "MIDCAP_ALT", "ATOMUSDT": "MIDCAP_ALT",
    "SEIUSDT": "MIDCAP_ALT", "SUSHIUSDT": "MIDCAP_ALT", "TRXUSDT": "MIDCAP_ALT",
    "JUPUSDT": "NARRATIVE", "OPUSDT": "MIDCAP_ALT", "UUSDT": "MIDCAP_ALT",
    "FILUSDT": "MIDCAP_ALT", "FETUSDT": "NARRATIVE", "TAOUSDT": "NARRATIVE",
    "RENDERUSDT": "NARRATIVE", "NEARUSDT": "NARRATIVE", "WLDUSDT": "NARRATIVE",
    "AGIXUSDT": "NARRATIVE", "OCEANUSDT": "NARRATIVE", "INJUSDT": "NARRATIVE",
    "GRTUSDT": "NARRATIVE", "AIOZUSDT": "NARRATIVE", "JTOUSDT": "NARRATIVE",
    "ONDOUSDT": "NARRATIVE", "DOGEUSDT": "MEME", "PEPEUSDT": "MEME",
    "WIFUSDT": "MEME", "TRUMPUSDT": "MEME", "MEGAUSDT": "MEME",
}


class SignalPipeline:
    """Convert raw analyses into filtered, diversified, ranked Signals."""

    def __init__(
        self,
        category_resolver: Optional[Callable[[str], str]] = None,
        short_eligible: Optional[Set[str]] = None,
    ):
        self.category_resolver = category_resolver or self._default_category
        self.short_eligible = short_eligible or set()

    def _default_category(self, symbol: str) -> str:
        return _DEFAULT_CATEGORIES.get(symbol, "MIDCAP_ALT")

    def evaluate(
        self,
        analyses: List[Dict],
        ctx: MarketContext,
        policy: SignalPolicy,
    ) -> List[Signal]:
        """Run the full signal pipeline and return ranked Signals."""
        candidates = [self._to_signal(a, ctx) for a in analyses]
        candidates = [s for s in candidates if s is not None and self._passes_filter(s, ctx, policy)]
        candidates = self._diversify(candidates, ctx, policy)
        candidates.sort(key=lambda s: s.composite_score, reverse=True)
        return candidates[: policy.max_candidates]

    def _to_signal(self, analysis: Dict, ctx: MarketContext) -> Optional[Signal]:
        """Convert a raw analysis dict into a Signal object.

        The dict is expected to come from AutonomousSignalGenerator.analyze_symbol()
        or from a MIMIC historical screener. Missing fields are defaulted safely.
        """
        symbol = analysis.get("symbol")
        if not symbol:
            return None

        raw_signal = analysis.get("signal", "HOLD")
        side = Side.LONG if raw_signal == "BUY" else Side.SHORT if raw_signal == "SELL" else None
        if side is None:
            return None
        if side == Side.SHORT and symbol not in self.short_eligible:
            return None

        category = self.category_resolver(symbol)
        strength = float(analysis.get("strength", 0.0))

        # Expected PnL and success probability may come from pnl_service or learning_engine.
        expected_pnl = float(analysis.get("expected_pnl", analysis.get("pnl_prediction", 0.0)))
        success_prob = float(analysis.get("success_probability", analysis.get("pnl_prob", 0.5)))

        # Composite score: blend strength, EV, and regime alignment.
        ev_score = max(0.0, min(1.0, (expected_pnl + 2.0) / 4.0)) if expected_pnl is not None else 0.5
        prob_score = max(0.0, min(1.0, success_prob)) if success_prob is not None else 0.5
        regime_alignment = self._regime_alignment(side, ctx)
        composite_score = round(
            0.40 * strength + 0.25 * ev_score + 0.25 * prob_score + 0.10 * regime_alignment, 4
        )

        liquidity = float(analysis.get("liquidity_usdt_24h", analysis.get("quote_volume", 0.0)))

        return Signal(
            symbol=symbol,
            side=side,
            category=category,
            composite_score=composite_score,
            expected_pnl=expected_pnl,
            success_probability=success_prob,
            signal_strength=strength,
            regime_alignment=regime_alignment,
            liquidity_usdt_24h=liquidity,
            features={k: float(v) for k, v in analysis.items() if isinstance(v, (int, float, bool))},
            source=analysis.get("source", "autonomous"),
        )

    def _regime_alignment(self, side: Side, ctx: MarketContext) -> float:
        """Return 0..1 score indicating how well the signal side aligns with the regime."""
        dominant = ctx.regime
        if dominant == "SPOT_BULL":
            return 1.0 if side == Side.LONG else 0.0
        if dominant == "FUTURES_SHORT":
            return 1.0 if side == Side.SHORT else 0.0
        if dominant == "RANGE_SCALP":
            return 0.8 if side == Side.LONG else 0.0
        if dominant == "PRESERVATION":
            return 0.1  # discourage entries in preservation
        return 0.5

    def _passes_filter(self, signal: Signal, ctx: MarketContext, policy: SignalPolicy) -> bool:
        if signal.signal_strength < policy.min_signal_strength:
            return False
        if signal.success_probability < policy.min_success_probability:
            return False
        if signal.expected_pnl < policy.min_expected_pnl:
            return False
        if signal.liquidity_usdt_24h < policy.min_liquidity_usdt:
            return False
        if signal.category not in policy.categories_enabled:
            return False
        # Reject signals that fight the dominant regime unless confidence is very low.
        if signal.regime_alignment < 0.3 and ctx.regime_confidence.get(ctx.regime, 0.0) > 0.6:
            return False
        return True

    def _diversify(
        self,
        signals: List[Signal],
        ctx: MarketContext,
        policy: SignalPolicy,
    ) -> List[Signal]:
        """Greedy category diversification: pick the best signal from each
        enabled category first, then fill remaining slots with the next-best
        non-conflicting signal. This replaces the QUBO portfolio selector with
        a deterministic, interpretable, and fast alternative.
        """
        if policy.diversification_mode == "none":
            return signals
        if policy.diversification_mode != "category":
            # Reserved for future QUBO / correlation implementations.
            return signals

        by_category: Dict[str, List[Signal]] = defaultdict(list)
        for s in signals:
            by_category[s.category].append(s)
        for cat in by_category:
            by_category[cat].sort(key=lambda s: s.composite_score, reverse=True)

        selected: List[Signal] = []
        used_symbols: Set[str] = set()

        # Round 1: one slot per enabled category, respecting category-specific caps
        # if they are provided in the policy.
        # Round 1: one slot per category unless the policy explicitly defines a higher cap.
        category_slots = getattr(policy, "max_category_positions", None) or {
            cat: 1 for cat in policy.categories_enabled
        }
        category_counts: Dict[str, int] = {cat: 0 for cat in policy.categories_enabled}

        for cat in policy.categories_enabled:
            for s in by_category.get(cat, []):
                if s.symbol in used_symbols:
                    continue
                if category_counts[cat] >= category_slots.get(cat, 1):
                    break
                selected.append(s)
                used_symbols.add(s.symbol)
                category_counts[cat] += 1

        # Round 2: fill remaining slots with the highest remaining score,
        # but never exceed the category slot cap.
        remaining = [s for s in signals if s.symbol not in used_symbols]
        remaining.sort(key=lambda s: s.composite_score, reverse=True)
        for s in remaining:
            if len(selected) >= policy.max_candidates:
                break
            if category_counts.get(s.category, 0) >= category_slots.get(s.category, 1):
                continue
            selected.append(s)
            used_symbols.add(s.symbol)
            category_counts[s.category] = category_counts.get(s.category, 0) + 1

        return selected


def analysis_to_signal(
    analysis: Dict,
    ctx: MarketContext,
    category_resolver: Optional[Callable[[str], str]] = None,
    short_eligible: Optional[Set[str]] = None,
) -> Optional[Signal]:
    """One-shot conversion of a single analysis dict to a Signal."""
    pipeline = SignalPipeline(category_resolver=category_resolver, short_eligible=short_eligible)
    signals = pipeline.evaluate([analysis], ctx, SignalPolicy(
        min_signal_strength=0.0,
        min_success_probability=0.0,
        min_expected_pnl=-9999.0,
        min_liquidity_usdt=0.0,
        categories_enabled=("LARGE_CAP", "MIDCAP_ALT", "NARRATIVE", "MEME"),
        max_candidates=1,
        diversification_mode="none",
    ))
    return signals[0] if signals else None
