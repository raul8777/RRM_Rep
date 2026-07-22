"""
Wolf Hunter Peak Capture Engine

Single-responsibility module that decides whether to take current profits or keep
hunting a larger peak for an open position. Uses PredExit in profit mode,
position-size-aware capture margins, momentum exhaustion, and regime/profile context.

All decisions are audit-trail friendly and restart-safe via metadata persistence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from config_helpers import get_param

logger = logging.getLogger(__name__)


@dataclass
class PeakCaptureContext:
    """All data required to evaluate a profit-capture decision."""

    symbol: str
    trade_type: str  # 'spot' or 'futures'
    side: str
    position_size_usdt: float
    portfolio_total_usdt: float
    current_price: float
    entry_price: float
    current_pnl_usdt: float
    current_pnl_pct: float
    peak_pnl_pct: float
    regime_mode: str
    profile_label: Optional[str]
    signal_strength: float
    signal_direction: str
    pnl_velocity_1h: Optional[float]
    indicators: Dict[str, Any]
    fear_greed_value: float
    metadata: Dict[str, Any]
    leverage: float = 1.0
    prediction_1h: Optional[Dict[str, Any]] = None
    prediction_4h: Optional[Dict[str, Any]] = None
    momentum_exhausted: bool = False
    trend_reversal: bool = False
    current_sl_price: Optional[float] = None
    current_tp_price: Optional[float] = None

    def success_probability(self) -> float:
        for pred in (self.prediction_1h, self.prediction_4h):
            if pred and "success_probability" in pred:
                return float(pred.get("success_probability", 0.5) or 0.5)
        return 0.5


@dataclass
class PeakCaptureDecision:
    """Result of a profit-capture evaluation."""

    should_close: bool = False
    close_pct: float = 0.0
    reason: str = ""
    detail: str = ""
    priority: int = 99
    current_pnl_usdt: float = 0.0
    expected_future_pnl: float = 0.0
    prob_higher_peak: float = 0.0
    capture_margin: float = 1.0
    metrics: Dict[str, Any] = field(default_factory=dict)


def select_peak_capture_advisory(
    early_advisory: Optional[PeakCaptureDecision],
    normal_advisory: Optional[PeakCaptureDecision],
) -> Tuple[Optional[PeakCaptureDecision], str]:
    if early_advisory and early_advisory.should_close:
        if (
            normal_advisory
            and normal_advisory.should_close
            and normal_advisory.priority > early_advisory.priority
        ):
            return normal_advisory, "normal"
        return early_advisory, "early"
    if normal_advisory:
        return normal_advisory, "normal"
    return None, "none"


class PeakCaptureEngine:
    """Decides whether to capture current profit or wait for a larger peak."""

    # In-memory cache for reconstructed peak prices (symbol -> (price, ts))
    _peak_cache: Dict[str, Tuple[float, datetime]] = {}

    def __init__(self, settings: Any, db_client: Any = None):
        self.settings = settings
        self.db_client = db_client

    # ── Public API ────────────────────────────────────────────────────────────

    def evaluate(self, ctx: PeakCaptureContext) -> PeakCaptureDecision:
        """
        Evaluate a single position and return a capture decision.

        The engine never overrides hard SL/TP; it produces an advisory for UG.
        """
        min_profit = self._min_profit_threshold(ctx)
        if ctx.current_pnl_usdt < min_profit:
            return PeakCaptureDecision(
                should_close=False,
                reason="profit_predexit_below_min",
                detail=(
                    f"current_pnl={ctx.current_pnl_usdt:.2f} < min={min_profit:.2f}"
                ),
                current_pnl_usdt=ctx.current_pnl_usdt,
            )

        margin = self._compute_capture_margin(ctx)
        ev_wait, prob_higher_peak = self._compute_ev_wait(ctx)
        giveback_penalty = self._compute_giveback_penalty(ctx)
        risk_adjusted_wait = ev_wait * margin - giveback_penalty

        decision = PeakCaptureDecision(
            current_pnl_usdt=ctx.current_pnl_usdt,
            expected_future_pnl=ev_wait,
            prob_higher_peak=prob_higher_peak,
            capture_margin=margin,
            metrics={
                "min_profit": round(min_profit, 4),
                "ev_wait": round(ev_wait, 4),
                "risk_adjusted_wait": round(risk_adjusted_wait, 4),
                "giveback_penalty": round(giveback_penalty, 4),
            },
        )

        # Priority ordering:
        # 95 PRS peak protection
        # 93 Profit PredExit momentum exhausted
        # 92 Profit PredExit regime fear / trend reversal
        # 85 Profit PredExit value-based capture
        if ctx.momentum_exhausted:
            decision.should_close = True
            decision.close_pct = 1.0
            decision.reason = "profit_predexit_momentum_exhausted"
            decision.detail = (
                f"momentum exhausted in profit; pnl={ctx.current_pnl_usdt:.2f}"
            )
            decision.priority = 93
        elif ctx.trend_reversal or ctx.fear_greed_value <= 15:
            decision.should_close = True
            decision.close_pct = 1.0
            decision.reason = "profit_predexit_regime_fear"
            decision.detail = (
                f"trend_reversal={ctx.trend_reversal}, F&G={ctx.fear_greed_value}; "
                f"pnl={ctx.current_pnl_usdt:.2f}"
            )
            decision.priority = 92
        elif ctx.current_pnl_usdt > risk_adjusted_wait:
            decision.should_close = True
            decision.close_pct = self._dynamic_close_pct(prob_higher_peak)
            decision.reason = "profit_predexit_capture"
            decision.detail = (
                f"current_pnl {ctx.current_pnl_usdt:.2f} > "
                f"risk_adjusted_wait {risk_adjusted_wait:.2f} "
                f"(margin={margin:.2f}, prob_higher={prob_higher_peak:.2f})"
            )
            decision.priority = 85
        else:
            decision.should_close = False
            decision.reason = "profit_predexit_hold"
            decision.detail = (
                f"holding: current {ctx.current_pnl_usdt:.2f} <= "
                f"risk_adjusted_wait {risk_adjusted_wait:.2f}"
            )

        return decision

    def evaluate_early_peak_protection(self, ctx: PeakCaptureContext) -> PeakCaptureDecision:
        configured_mode = str(get_param(self.settings, "profit_predexit_early_protection_mode", "shadow") or "shadow").strip().lower()
        active_regimes = get_param(self.settings, "profit_predexit_early_active_regimes", ["SPOT_BULL"])
        if isinstance(active_regimes, str):
            active_regimes = [item.strip() for item in active_regimes.split(",") if item.strip()]
        active_regimes = {str(item).strip().upper() for item in (active_regimes or [])}
        regime_allowed = str(ctx.regime_mode or "").strip().upper() in active_regimes
        mode = "shadow" if configured_mode == "active" and not regime_allowed else configured_mode
        min_peak = float(get_param(self.settings, "profit_predexit_early_min_peak_pct", 1.5)) / 100.0
        min_giveback = float(get_param(self.settings, "profit_predexit_early_min_giveback_pct", 0.5)) / 100.0
        giveback = max(0.0, float(ctx.peak_pnl_pct) - float(ctx.current_pnl_pct))
        min_profit = self._min_profit_threshold(ctx)
        behavior_confirmed = bool(
            (ctx.pnl_velocity_1h is not None and ctx.pnl_velocity_1h < 0)
            or ctx.momentum_exhausted
            or ctx.trend_reversal
        )
        metrics = {
            "mode": mode,
            "configured_mode": configured_mode,
            "regime_mode": str(ctx.regime_mode or "").strip().upper(),
            "regime_allowed": regime_allowed,
            "min_peak_pct": round(min_peak, 6),
            "min_giveback_pct": round(min_giveback, 6),
            "peak_pnl_pct": round(float(ctx.peak_pnl_pct), 6),
            "giveback_pct": round(giveback, 6),
            "min_profit": round(min_profit, 4),
            "behavior_confirmed": behavior_confirmed,
        }
        if mode == "disabled":
            return PeakCaptureDecision(reason="profit_predexit_early_disabled", metrics=metrics)
        if str((ctx.metadata.get("early_peak_protection") or {}).get("execution_status", "")) == "partial_executed":
            return PeakCaptureDecision(reason="profit_predexit_early_already_executed", metrics=metrics)
        if ctx.current_pnl_usdt < min_profit or ctx.current_pnl_pct <= 0:
            return PeakCaptureDecision(reason="profit_predexit_early_below_net_profit", metrics=metrics)
        if ctx.peak_pnl_pct < min_peak:
            return PeakCaptureDecision(reason="profit_predexit_early_peak_below_threshold", metrics=metrics)
        if giveback < min_giveback:
            return PeakCaptureDecision(reason="profit_predexit_early_giveback_below_threshold", metrics=metrics)
        if not behavior_confirmed:
            return PeakCaptureDecision(reason="profit_predexit_early_behavior_unconfirmed", metrics=metrics)
        decision = PeakCaptureDecision(
            should_close=mode == "active",
            close_pct=0.5 if mode == "active" else 0.0,
            reason="profit_predexit_early_peak_protection",
            detail=(
                f"peak={ctx.peak_pnl_pct * 100:.2f}% giveback={giveback * 100:.2f}% "
                f"velocity={ctx.pnl_velocity_1h if ctx.pnl_velocity_1h is not None else 0.0:.6f}"
            ),
            priority=84,
            current_pnl_usdt=ctx.current_pnl_usdt,
            metrics=metrics,
        )
        return decision

    def reconstruct_peak_pnl_pct(
        self,
        client: Any,
        symbol: str,
        pos: Any,
        current_price: float,
        metadata: Dict[str, Any],
    ) -> float:
        """
        Reconstruct the highest PnL% reached by this position.

        Uses metadata['trailing_peak'] and recent 1m candles. Updates metadata
        in place when a new peak is found so the change survives restarts.
        """
        if not get_param(self.settings, "peak_reconstruction_enabled", True):
            return 0.0

        entry_price = float(getattr(pos, "entry_price", 0) or 0)
        if entry_price <= 0:
            return 0.0

        # Start from persisted trailing_peak
        peak_price = entry_price
        trailing_peak = metadata.get("trailing_peak")
        if trailing_peak and float(trailing_peak) > 0:
            peak_price = max(peak_price, float(trailing_peak))

        # Merge with cached value
        cached = self._peak_cache.get(symbol)
        if cached:
            cached_price, cached_ts = cached
            if (datetime.now(timezone.utc) - cached_ts).total_seconds() < 60:
                peak_price = max(peak_price, cached_price)

        # Reconstruct from recent 1m candles
        try:
            lookback = int(
                get_param(self.settings, "peak_reconstruction_lookback_candles", 30)
            )
            klines = client.client.get_klines(
                symbol=symbol, interval="1m", limit=lookback
            )
            if klines:
                highs = [float(k[2]) for k in klines]
                max_high = max(highs) if highs else peak_price
                peak_price = max(peak_price, max_high)
        except Exception:
            pass

        peak_pct = (peak_price - entry_price) / entry_price
        peak_pct = max(0.0, peak_pct)

        # Persist if new peak
        prev_peak_pct = float(metadata.get("peak_pnl_pct_real", 0) or 0)
        if peak_pct > prev_peak_pct:
            metadata["peak_pnl_pct_real"] = round(peak_pct, 6)
            metadata["last_peak_price"] = round(peak_price, 8)
            metadata["peak_reconstruction_ts"] = datetime.now(timezone.utc).isoformat()
            self._peak_cache[symbol] = (peak_price, datetime.now(timezone.utc))

        return peak_pct

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _min_profit_threshold(self, ctx: PeakCaptureContext) -> float:
        """
        Minimum profit (post-commission) before the engine is allowed to close.

        For leveraged futures positions the effective notional is smaller, so we
        divide by leverage when applying the relative threshold.
        """
        min_pct = get_param(
            self.settings, "profit_predexit_min_profit_pct", 0.005
        )
        absolute_min = get_param(
            self.settings, "profit_predexit_absolute_min_usd", 1.50
        )

        position_size = ctx.position_size_usdt
        if ctx.trade_type == "futures":
            position_size = ctx.position_size_usdt / max(ctx.leverage, 1.0)

        relative_min = position_size * min_pct
        return max(absolute_min, relative_min)

    def _compute_capture_margin(self, ctx: PeakCaptureContext) -> float:
        """
        Compute the capture margin for the optimal-stopping decision.

        Lower margin => more eager to take profit.
        Higher margin => more patient waiting for a larger peak.
        """
        profile = str(ctx.profile_label or "B").strip().upper()
        base_margins = {
            "A": get_param(
                self.settings, "profit_predexit_capture_margin_A", 0.80
            ),
            "B": get_param(
                self.settings, "profit_predexit_capture_margin_B", 1.00
            ),
            "C": get_param(
                self.settings, "profit_predexit_capture_margin_C", 1.25
            ),
        }
        base = base_margins.get(profile, 1.00)

        # Position-size adjustment: larger position => protect faster (lower margin)
        size_sens = get_param(
            self.settings, "profit_predexit_size_sensitivity", 1.5
        )
        max_positions = max(get_param(self.settings, "max_positions", 4), 1)
        avg_pct = 1.0 / max_positions
        position_size_pct = ctx.position_size_usdt / max(
            ctx.portfolio_total_usdt, 1.0
        )
        size_factor = 1.0 - (position_size_pct - avg_pct) * size_sens

        # Conviction adjustment: higher conviction => more patient
        conv_sens = get_param(
            self.settings, "profit_predexit_conviction_sensitivity", 0.4
        )
        confidence = min(max(ctx.signal_strength * ctx.success_probability(), 0.0), 1.0)
        conviction_factor = 1.0 - (confidence - 0.5) * conv_sens

        # Fear & Greed adjustment
        fng = ctx.fear_greed_value
        fng_factor = 1.0
        if fng <= 20:
            fng_factor = 1.15  # extreme fear: take profits quicker
        elif fng >= 75:
            fng_factor = 0.90  # extreme greed: more patient

        # Regime adjustment
        regime_factor = 1.0
        regime = str(ctx.regime_mode or "").strip().upper()
        if regime == "RANGE_SCALP":
            regime_factor = 1.05  # slightly more eager in range scalps
        elif regime == "SPOT_BULL":
            regime_factor = 0.92  # more patient in runners
        elif regime == "PRESERVATION":
            regime_factor = 1.20  # very eager to take any profit
        elif regime == "FUTURES_SHORT":
            regime_factor = 1.10  # leveraged: protect faster

        margin = base * size_factor * conviction_factor * fng_factor * regime_factor
        return min(max(margin, 0.55), 1.50)

    def _compute_ev_wait(self, ctx: PeakCaptureContext) -> Tuple[float, float]:
        """
        Estimate the expected value of waiting for a larger peak.

        Returns (expected_pnl_usdt, probability_of_higher_peak).
        """
        pred = ctx.prediction_1h or ctx.prediction_4h
        if not pred:
            return 0.0, 0.0

        ev = float(pred.get("expected_pnl", 0.0) or 0.0)
        prob = float(pred.get("success_probability", 0.5) or 0.5)
        price_range = pred.get("price_range", {}) or {}
        max_price = float(price_range.get("max", ctx.current_price) or ctx.current_price)
        min_price = float(price_range.get("min", ctx.current_price) or ctx.current_price)

        leverage = ctx.leverage if ctx.trade_type == "futures" else 1.0
        ev = ev * leverage

        current_pct = ctx.current_pnl_pct
        entry_price = ctx.entry_price
        if entry_price <= 0:
            return 0.0, 0.0

        expected_max_pct = (max_price - entry_price) / entry_price
        expected_min_pct = (min_price - entry_price) / entry_price

        if expected_max_pct > current_pct:
            distance = (expected_max_pct - current_pct) / max(expected_max_pct, 1e-9)
            prob_higher_peak = prob * max(0.0, min(1.0, distance))
        else:
            prob_higher_peak = 0.0

        # Momentum decay adjustments
        if ctx.momentum_exhausted:
            prob_higher_peak *= 0.3
        if ctx.pnl_velocity_1h is not None and ctx.pnl_velocity_1h < 0:
            prob_higher_peak *= 0.7
        if ctx.trend_reversal:
            prob_higher_peak *= 0.5

        prob_higher_peak = min(max(prob_higher_peak, 0.0), 1.0)

        upside = max(0.0, expected_max_pct - current_pct) * ctx.position_size_usdt
        downside = max(0.0, current_pct - expected_min_pct) * ctx.position_size_usdt
        ev_wait = prob_higher_peak * upside - (1.0 - prob_higher_peak) * downside + ev

        return ev_wait, prob_higher_peak

    def _compute_giveback_penalty(self, ctx: PeakCaptureContext) -> float:
        """Penalty proportional to how much profit has already been given back."""
        if ctx.peak_pnl_pct <= 0 or ctx.peak_pnl_pct <= ctx.current_pnl_pct:
            return 0.0

        weight = get_param(
            self.settings, "profit_predexit_giveback_penalty_weight", 0.5
        )
        giveback_pct = ctx.peak_pnl_pct - ctx.current_pnl_pct
        leverage = ctx.leverage if ctx.trade_type == "futures" else 1.0
        return giveback_pct * ctx.position_size_usdt * weight * leverage

    def _dynamic_close_pct(self, prob_higher_peak: float) -> float:
        """
        Dynamic partial/full close based on probability of a larger peak.

        - < 35% chance of larger peak -> take everything.
        - 35-60% -> take half, let the rest run with trailing.
        - >= 60% -> hold.
        """
        if prob_higher_peak < 0.35:
            return 1.0
        if prob_higher_peak < 0.60:
            return 0.5
        return 0.0
