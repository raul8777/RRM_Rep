"""lifecycle/regime.py — RegimeKernel with probabilistic confidence.

This module replaces the binary regime detection of market_regime.py with a
state object that exposes confidence for every mode. The existing MarketRegime
class is still used internally for data acquisition; the new kernel converts it
to RegimeState and adds a native FUTURES_SHORT confidence score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Callable
import time

import market_regime
from lifecycle.base import MarketContext


@dataclass(frozen=True)
class RegimeState:
    """Probabilistic view of the current market regime."""
    dominant: str
    confidence: Dict[str, float]
    confirmed: bool
    features: Dict[str, float]
    reason: str
    color: str

    def is_confident(self, threshold: float = 0.60) -> bool:
        return self.confidence.get(self.dominant, 0.0) >= threshold


class RegimeKernel:
    """Kernel that detects regime and returns a RegimeState.

    For now it wraps the proven market_regime.get_regime() logic and enriches it
    with a per-mode confidence distribution. Later the raw detection can be moved
    here without changing callers.
    """

    # Confidence floor for the dominant mode to avoid spurious regime flips.
    _MIN_CONFIDENCE = 0.35

    def __init__(self, binance_client, settings=None):
        self.binance_client = binance_client
        self.settings = settings

    def detect(self, force_refresh: bool = False) -> RegimeState:
        """Detect current regime and return probabilistic state."""
        regime = market_regime.get_regime(
            self.binance_client,
            force_refresh=force_refresh,
            settings=self.settings,
        )
        confidence = self._compute_confidence_distribution(regime)

        # Severe structural bear can override a weak PRESERVATION confidence into
        # FUTURES_SHORT when futures are enabled.
        futures_enabled = self.settings is not None and getattr(self.settings, "futures_enabled", True)
        if futures_enabled and regime.btc_30d_pct <= -15.0 and confidence.get("FUTURES_SHORT", 0) < 0.5:
            confidence["FUTURES_SHORT"] = max(confidence.get("PRESERVATION", 0.0), 0.55)

        dominant = max(confidence, key=confidence.get)
        if confidence[dominant] < self._MIN_CONFIDENCE:
            dominant = regime.mode  # fall back to legacy mode if no mode is confident enough

        features = {
            "btc_7d_pct": regime.btc_7d_pct,
            "btc_30d_pct": regime.btc_30d_pct,
            "btc_day_pct": regime.btc_day_pct,
            "vol_ratio": regime.vol_ratio,
            "bb_bandwidth": regime.bb_bandwidth,
            "ema9_vs_ema21": regime.ema9_vs_ema21,
        }

        return RegimeState(
            dominant=dominant,
            confidence=confidence,
            confirmed=regime.confirmed,
            features=features,
            reason=regime.reason,
            color=regime.color,
        )

    def _compute_confidence_distribution(self, regime: market_regime.MarketRegime) -> Dict[str, float]:
        """Build a soft probability distribution over the four modes.

        Uses the same indicator checks as market_regime._compute_confidence but
        produces scores for all modes simultaneously, then normalizes.
        """
        raw = {
            "btc_30d_pct": regime.btc_30d_pct,
            "btc_7d_pct": regime.btc_7d_pct,
            "btc_day_pct": regime.btc_day_pct,
            "vol_ratio": regime.vol_ratio,
            "bb_bandwidth": regime.bb_bandwidth,
            "ema9_vs_ema21": regime.ema9_vs_ema21,
        }

        def score_preservation() -> float:
            s = 0.0
            if raw["btc_30d_pct"] <= -15.0: s += 1.0
            elif raw["btc_30d_pct"] <= -7.0: s += 0.5
            if raw["btc_7d_pct"] <= -7.0: s += 1.0
            elif raw["btc_7d_pct"] <= -3.0: s += 0.5
            if raw["ema9_vs_ema21"] == "DOWN": s += 1.0
            elif raw["ema9_vs_ema21"] == "FLAT": s += 0.3
            if raw["vol_ratio"] < 0.8: s += 1.0
            elif raw["vol_ratio"] < 1.0: s += 0.5
            if raw["btc_day_pct"] < -0.5: s += 1.0
            elif raw["btc_day_pct"] < 0.0: s += 0.5
            return s / 5.0

        def score_futures_short() -> float:
            # Same inputs as preservation but only when structural bear is severe
            # and momentum is down. This is the active-short variant.
            s = 0.0
            if raw["btc_30d_pct"] <= -15.0: s += 1.0
            elif raw["btc_30d_pct"] <= -7.0: s += 0.5
            if raw["btc_7d_pct"] <= -7.0: s += 1.0
            elif raw["btc_7d_pct"] <= -3.0: s += 0.5
            if raw["ema9_vs_ema21"] == "DOWN": s += 1.0
            if raw["vol_ratio"] < 1.0: s += 0.5
            if raw["btc_day_pct"] < -0.5: s += 1.0
            return s / 5.0

        def score_spot_bull() -> float:
            s = 0.0
            if raw["btc_7d_pct"] > 0.0: s += 1.0
            elif raw["btc_7d_pct"] > -3.0: s += 0.5
            if raw["btc_30d_pct"] > -5.0: s += 1.0
            elif raw["btc_30d_pct"] > -10.0: s += 0.5
            if raw["ema9_vs_ema21"] == "UP": s += 1.0
            elif raw["ema9_vs_ema21"] == "FLAT": s += 0.3
            if raw["vol_ratio"] >= 1.0: s += 1.0
            elif raw["vol_ratio"] >= 0.8: s += 0.5
            if raw["btc_day_pct"] > 0.5: s += 1.0
            elif raw["btc_day_pct"] > 0.0: s += 0.5
            return s / 5.0

        def score_range_scalp() -> float:
            s = 0.0
            bw = raw["bb_bandwidth"]
            if bw < 0.06: s += 1.0
            elif bw < 0.10: s += 0.5
            if abs(raw["btc_day_pct"]) < 1.2: s += 1.0
            elif abs(raw["btc_day_pct"]) < 2.5: s += 0.5
            if raw["vol_ratio"] < 0.7: s += 1.0
            elif raw["vol_ratio"] < 1.0: s += 0.5
            if raw["ema9_vs_ema21"] != "UP": s += 1.0
            elif raw["ema9_vs_ema21"] == "FLAT": s += 0.5
            if raw["btc_7d_pct"] > -5.0: s += 1.0
            elif raw["btc_7d_pct"] > -10.0: s += 0.5
            return s / 5.0

        scores = {
            "SPOT_BULL": score_spot_bull(),
            "RANGE_SCALP": score_range_scalp(),
            "PRESERVATION": score_preservation(),
            "FUTURES_SHORT": score_futures_short(),
        }

        total = sum(scores.values())
        if total <= 0.0:
            distribution = {k: 0.25 for k in scores}
        else:
            distribution = {k: round(v / total, 3) for k, v in scores.items()}

        # Cap unconfirmed regimes to avoid trading on a weak flip.
        if not regime.confirmed:
            distribution = {k: min(v, 0.65) for k, v in distribution.items()}
            # Renormalize
            t = sum(distribution.values())
            if t > 0:
                distribution = {k: round(v / t, 3) for k, v in distribution.items()}

        return distribution


def get_regime_v2(binance_client, settings=None, force_refresh: bool = False) -> RegimeState:
    """Convenience function matching the old market_regime.get_regime signature."""
    kernel = RegimeKernel(binance_client, settings=settings)
    return kernel.detect(force_refresh=force_refresh)


def build_market_context(regime: RegimeState, fear_greed: float, btc_dominance: float,
                         funding_rate: Optional[float] = None,
                         altcoin_breadth: Optional[float] = None) -> MarketContext:
    """Build a MarketContext from the regime state and external market data."""
    return MarketContext(
        timestamp=time.time(),
        regime=regime.dominant,
        regime_confidence=regime.confidence,
        btc_7d_pct=regime.features["btc_7d_pct"],
        btc_30d_pct=regime.features["btc_30d_pct"],
        btc_day_pct=regime.features["btc_day_pct"],
        fear_greed_index=fear_greed,
        btc_dominance=btc_dominance,
        funding_rate=funding_rate,
        altcoin_breadth=altcoin_breadth,
    )
