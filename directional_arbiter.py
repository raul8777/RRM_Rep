from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class DirectionalArbitration:
    symbol: str
    selected_mode: str
    reason: str
    capital_intent: str
    locked_position_size_usdt: float
    short_score: float
    long_score: float
    short_eligible: bool
    long_eligible: bool
    long_structure: bool
    short_structure: bool
    short_resistance_setup: bool
    futures_executable: bool
    short_prediction: Dict[str, Any]
    long_prediction: Dict[str, Any]
    futures_long_prediction: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _prediction_score(prediction: Optional[Dict[str, Any]], minimum_probability: float) -> tuple[bool, float]:
    pred = prediction if isinstance(prediction, dict) else {}
    if not pred or bool(pred.get("degenerate_output")):
        return False, 0.0
    expected_pnl = _number(pred.get("expected_pnl"))
    probability = _number(pred.get("success_probability"))
    position_size = max(1.0, _number(pred.get("position_size_usdt"), 100.0))
    expected_pct = pred.get("expected_pnl_percentage")
    edge = _number(expected_pct) / 100.0 if expected_pct is not None else expected_pnl / position_size
    eligible = expected_pnl > 0.0 and probability >= minimum_probability
    return eligible, probability + max(-0.25, min(0.25, edge))


def _sr_distance_features(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Compute support/resistance distance features used by symmetric mode.

    If explicit support/resistance levels are present, use them; otherwise fall
    back to the legacy ``sr_zone_position`` scalar (0 = support, 1 = resistance).
    """
    price = _number(metadata.get("current_price"), 0.0)
    support = _number(metadata.get("support_level"), 0.0)
    resistance = _number(metadata.get("resistance_level"), 0.0)

    if price > 0 and support > 0 and resistance > support:
        range_height = resistance - support
        dist_to_support_pct = (price - support) / range_height
        dist_to_resistance_pct = (resistance - price) / range_height
    else:
        zone = _number(metadata.get("sr_zone_position"), 0.5)
        dist_to_support_pct = zone
        dist_to_resistance_pct = 1.0 - zone

    return {
        "dist_to_support_pct": dist_to_support_pct,
        "dist_to_resistance_pct": dist_to_resistance_pct,
    }


def evaluate_range_scalp(
    symbol: str,
    metadata: Dict[str, Any],
    long_prediction: Optional[Dict[str, Any]],
    futures_long_prediction: Optional[Dict[str, Any]],
    short_prediction: Optional[Dict[str, Any]],
    futures_executable: bool,
    settings: Any,
) -> DirectionalArbitration:
    direction_mode = str(getattr(settings, "range_scalp_direction_mode", "ev_symmetric")).strip().lower()
    if direction_mode == "short_priority":
        return _evaluate_range_scalp_legacy(
            symbol, metadata, long_prediction, futures_long_prediction,
            short_prediction, futures_executable, settings,
        )
    return _evaluate_range_scalp_symmetric(
        symbol, metadata, long_prediction, futures_long_prediction,
        short_prediction, futures_executable, settings,
    )


def _evaluate_range_scalp_legacy(
    symbol: str,
    metadata: Dict[str, Any],
    long_prediction: Optional[Dict[str, Any]],
    futures_long_prediction: Optional[Dict[str, Any]],
    short_prediction: Optional[Dict[str, Any]],
    futures_executable: bool,
    settings: Any,
) -> DirectionalArbitration:
    """Original short-priority arbiter; kept for rollback compatibility."""
    minimum_probability = _number(getattr(settings, "range_scalp_direction_min_probability", 0.55), 0.55)
    short_minimum_probability = _number(
        getattr(settings, "range_scalp_short_min_probability", 0.50),
        0.50,
    )
    short_bonus = _number(getattr(settings, "range_scalp_short_priority_bonus", 0.0), 0.0)
    long_delta = _number(getattr(settings, "range_scalp_long_superiority_delta", 0.0), 0.0)
    long_min_probability = _number(
        getattr(settings, "range_scalp_long_min_probability", 0.50),
        0.50,
    )
    long_min_expected_pnl = _number(
        getattr(settings, "range_scalp_long_min_expected_pnl", 0.0),
        0.0,
    )
    long_support_zone_max = _number(
        getattr(settings, "range_scalp_long_support_zone_max", 0.55),
        0.55,
    )
    short_eligible, short_score = _prediction_score(short_prediction, short_minimum_probability)
    spot_long_eligible, spot_long_score = _prediction_score(long_prediction, minimum_probability)
    futures_long_eligible, futures_long_score = _prediction_score(futures_long_prediction, minimum_probability)
    short_eligible = bool(short_eligible and futures_executable)
    futures_long_eligible = bool(futures_long_eligible and futures_executable)
    routing_intent = str(metadata.get("routing_intent") or "").strip().upper()
    if routing_intent == "FUTURES_LONG_ANALYSIS":
        long_eligible = bool(futures_long_eligible)
        long_score = futures_long_score
        long_mode = "FUTURES_LONG"
    else:
        long_eligible = bool(spot_long_eligible or futures_long_eligible)
        long_score = max(spot_long_score, futures_long_score)
        long_mode = "FUTURES_LONG" if futures_long_eligible and futures_long_score >= spot_long_score else "SPOT_LONG"

    long_entries_enabled = bool(getattr(settings, "range_scalp_long_entries_enabled", True))
    short_resistance_threshold = _number(
        getattr(settings, "range_scalp_short_resistance_threshold", 0.65),
        0.65,
    )

    zone_raw = metadata.get("sr_zone_position")
    zone = _number(zone_raw, 0.5) if zone_raw is not None else 0.5
    short_breakdown = bool(metadata.get("support_breakdown_sell") or metadata.get("sr_breakdown"))
    resistance_rejection = bool(metadata.get("resistance_bounce_sell") or metadata.get("sr_rejection"))
    short_resistance_setup = bool(short_breakdown or resistance_rejection or zone >= short_resistance_threshold)
    support_bounce = bool(metadata.get("support_bounce_buy") or metadata.get("sr_bounce"))
    support_breakout = bool(metadata.get("support_breakout_buy") or metadata.get("sr_breakout"))
    long_structure = bool((support_bounce or support_breakout) and not short_breakdown)
    short_structure = not long_structure
    if short_breakdown:
        short_score += 0.20
    if resistance_rejection:
        short_score += 0.15
    if zone >= short_resistance_threshold:
        short_score += min(0.20, (zone - short_resistance_threshold) * 0.50)
    if support_bounce:
        long_score += 0.15
    if support_breakout:
        long_score += 0.15
    if zone <= 0.35:
        long_score += min(0.15, (0.35 - zone) * 0.40)
    trend = str(metadata.get("trend_4h") or metadata.get("higher_tf_trend") or "").upper()
    if trend in {"DOWN", "BEARISH", "-1", "-1.0"}:
        short_score += 0.10
    elif trend in {"UP", "BULLISH", "1", "1.0"}:
        long_score += 0.10
    if short_eligible:
        short_score += short_bonus

    long_pred_ref = futures_long_prediction if long_mode == "FUTURES_LONG" else long_prediction
    long_pred_ref = long_pred_ref if isinstance(long_pred_ref, dict) else {}
    long_prob = _number(long_pred_ref.get("success_probability"), 0.0)
    long_expected_pnl = _number(long_pred_ref.get("expected_pnl"), 0.0)
    long_zone_ok = bool(zone <= long_support_zone_max)
    long_prob_ok = bool(long_prob >= long_min_probability)
    long_pnl_ok = bool(long_expected_pnl >= long_min_expected_pnl)
    long_eligible = bool(long_eligible and long_zone_ok and long_prob_ok and long_pnl_ok)
    long_superior_vs_short = bool(long_score >= (short_score + long_delta))

    short_setup_eligible = bool(short_eligible and short_structure)
    if not futures_executable:
        selected_mode = "ABSTAIN"
        reason = "TRIPLE_UNIVERSE_REQUIRED"
    elif short_setup_eligible:
        selected_mode = "FUTURES_SHORT"
        reason = "SHORT_RESISTANCE_PRIORITY" if short_resistance_setup else "SHORT_GEOMETRY_PRIORITY"
    elif not long_entries_enabled:
        selected_mode = "ABSTAIN"
        reason = "RANGE_SCALP_LONG_DISABLED"
    elif long_structure and long_eligible and long_superior_vs_short:
        selected_mode = long_mode
        reason = "LONG_SUPPORT_OR_BREAKOUT_SUPERIOR"
    elif long_structure and long_eligible and not long_superior_vs_short:
        selected_mode = "ABSTAIN"
        reason = "LONG_NOT_SUPERIOR_TO_SHORT"
    elif long_structure:
        selected_mode = "ABSTAIN"
        reason = "LONG_STRUCTURE_SHORT_BLOCKED"
    else:
        selected_mode = "ABSTAIN"
        reason = "SHORT_GEOMETRY_NOT_ELIGIBLE" if not short_eligible else "SHORT_PRIORITY_DISABLED"

    capital_intent = "NONE"
    locked_position_size_usdt = 0.0
    if selected_mode == "FUTURES_SHORT":
        capital_intent = "FUTURES"
        locked_position_size_usdt = _number((short_prediction or {}).get("position_size_usdt"), 0.0)
    elif selected_mode == "FUTURES_LONG":
        capital_intent = "FUTURES"
        locked_position_size_usdt = _number((futures_long_prediction or {}).get("position_size_usdt"), 0.0)
    elif selected_mode == "SPOT_LONG":
        capital_intent = "SPOT"
        locked_position_size_usdt = _number((long_prediction or {}).get("position_size_usdt"), 0.0)

    return DirectionalArbitration(
        symbol=symbol,
        selected_mode=selected_mode,
        reason=reason,
        capital_intent=capital_intent,
        locked_position_size_usdt=round(locked_position_size_usdt, 6),
        short_score=round(short_score, 6),
        long_score=round(long_score, 6),
        short_eligible=short_eligible,
        long_eligible=long_eligible,
        long_structure=long_structure,
        short_structure=short_structure,
        short_resistance_setup=short_resistance_setup,
        futures_executable=bool(futures_executable),
        short_prediction=dict(short_prediction or {}),
        long_prediction=dict(long_prediction or {}),
        futures_long_prediction=dict(futures_long_prediction or {}),
    )


def _evaluate_range_scalp_symmetric(
    symbol: str,
    metadata: Dict[str, Any],
    long_prediction: Optional[Dict[str, Any]],
    futures_long_prediction: Optional[Dict[str, Any]],
    short_prediction: Optional[Dict[str, Any]],
    futures_executable: bool,
    settings: Any,
) -> DirectionalArbitration:
    """Symmetric LONG/SHORT selector.

    LONG is selected only when price is near support / a LONG S/R event is
    present and the LONG expected value is the best available side.  SHORT is
    selected only when price is near resistance / a SHORT S/R event is present
    and the SHORT expected value is the best available side.  No side wins by
    default.
    """
    minimum_probability = _number(getattr(settings, "range_scalp_direction_min_probability", 0.55), 0.55)
    long_min_probability = _number(
        getattr(settings, "range_scalp_long_min_probability", 0.50),
        0.50,
    )
    short_min_probability = _number(
        getattr(settings, "range_scalp_short_min_probability", 0.50),
        0.50,
    )
    long_min_expected_pnl = _number(
        getattr(settings, "range_scalp_long_min_expected_pnl", 0.0),
        0.0,
    )
    short_min_expected_pnl = _number(
        getattr(settings, "range_scalp_short_min_expected_pnl", 0.0),
        0.0,
    )
    long_support_zone_max = _number(
        getattr(settings, "range_scalp_long_support_zone_max", 0.55),
        0.55,
    )
    short_resistance_threshold = _number(
        getattr(settings, "range_scalp_short_resistance_threshold", 0.65),
        0.65,
    )
    long_entries_enabled = bool(getattr(settings, "range_scalp_long_entries_enabled", True))

    sr = _sr_distance_features(metadata)
    dist_to_support_pct = sr["dist_to_support_pct"]
    dist_to_resistance_pct = sr["dist_to_resistance_pct"]

    # Explicit S/R events from support_resistance.py (preferred when available)
    short_breakdown = bool(metadata.get("support_breakdown_sell") or metadata.get("sr_breakdown"))
    resistance_rejection = bool(metadata.get("resistance_bounce_sell") or metadata.get("sr_rejection"))
    support_bounce = bool(metadata.get("support_bounce_buy") or metadata.get("sr_bounce"))
    support_breakout = bool(metadata.get("support_breakout_buy") or metadata.get("sr_breakout"))

    near_support = dist_to_support_pct <= long_support_zone_max
    near_resistance = dist_to_resistance_pct <= (1.0 - short_resistance_threshold)

    long_setup = bool(support_bounce or support_breakout or near_support)
    short_setup = bool(short_breakdown or resistance_rejection or near_resistance)

    # Prediction eligibility
    spot_long_eligible, spot_long_score = _prediction_score(long_prediction, minimum_probability)
    futures_long_eligible, futures_long_score = _prediction_score(futures_long_prediction, minimum_probability)
    short_eligible, short_score = _prediction_score(short_prediction, minimum_probability)

    # Side-specific probability floors (symmetric defaults)
    futures_long_eligible = bool(
        futures_long_eligible
        and futures_executable
        and futures_long_prediction is not None
        and _number(futures_long_prediction.get("success_probability")) >= long_min_probability
        and _number(futures_long_prediction.get("expected_pnl")) >= long_min_expected_pnl
    )
    spot_long_eligible = bool(
        spot_long_eligible
        and long_prediction is not None
        and _number(long_prediction.get("success_probability")) >= long_min_probability
        and _number(long_prediction.get("expected_pnl")) >= long_min_expected_pnl
    )
    short_eligible = bool(
        short_eligible
        and futures_executable
        and short_prediction is not None
        and _number(short_prediction.get("success_probability")) >= short_min_probability
        and _number(short_prediction.get("expected_pnl")) >= short_min_expected_pnl
    )

    # Routing intent can force FUTURES_LONG analysis. Otherwise prefer FUTURES_LONG
    # when it is eligible and its score is at least as good as spot; this keeps the
    # hedge/symmetric LONG capacity in the futures book rather than silently leaking
    # to spot whenever the spot predictor is slightly higher.
    routing_intent = str(metadata.get("routing_intent") or "").strip().upper()
    if routing_intent == "FUTURES_LONG_ANALYSIS":
        long_mode = "FUTURES_LONG"
        long_score = futures_long_score
    elif futures_long_eligible and futures_long_score >= spot_long_score:
        long_mode = "FUTURES_LONG"
        long_score = futures_long_score
    elif spot_long_eligible:
        long_mode = "SPOT_LONG"
        long_score = spot_long_score
    else:
        long_mode = "SPOT_LONG"
        long_score = max(spot_long_score, futures_long_score)

    long_structure = bool(long_setup)
    short_structure = bool(short_setup)
    short_resistance_setup = bool(short_setup)

    # Trend applied symmetrically as a small quality bonus, not a tie-break
    trend = str(metadata.get("trend_4h") or metadata.get("higher_tf_trend") or "").upper()
    if trend in {"DOWN", "BEARISH", "-1", "-1.0"}:
        short_score += 0.05
    elif trend in {"UP", "BULLISH", "1", "1.0"}:
        long_score += 0.05

    selected_mode = "ABSTAIN"
    reason = "NO_CLEAR_SR_SETUP"
    capital_intent = "NONE"
    locked_position_size_usdt = 0.0

    if not futures_executable:
        selected_mode = "ABSTAIN"
        reason = "TRIPLE_UNIVERSE_REQUIRED"
    elif long_setup and short_setup:
        # Both setups present: pick the side with the higher score, if eligible.
        if long_entries_enabled and (futures_long_eligible or spot_long_eligible) and short_eligible:
            if long_score >= short_score:
                selected_mode = long_mode
                reason = "SYMMETRIC_LONG_WINS_TIE"
                capital_intent = "FUTURES" if long_mode == "FUTURES_LONG" else "SPOT"
                locked_position_size_usdt = _number(
                    (futures_long_prediction if long_mode == "FUTURES_LONG" else long_prediction or {}).get("position_size_usdt"),
                    0.0,
                )
            else:
                selected_mode = "FUTURES_SHORT"
                reason = "SYMMETRIC_SHORT_WINS_TIE"
                capital_intent = "FUTURES"
                locked_position_size_usdt = _number((short_prediction or {}).get("position_size_usdt"), 0.0)
        elif long_entries_enabled and (futures_long_eligible or spot_long_eligible):
            selected_mode = long_mode
            reason = "SYMMETRIC_LONG_ONLY_ELIGIBLE"
            capital_intent = "FUTURES" if long_mode == "FUTURES_LONG" else "SPOT"
            locked_position_size_usdt = _number(
                (futures_long_prediction if long_mode == "FUTURES_LONG" else long_prediction or {}).get("position_size_usdt"),
                0.0,
            )
        elif short_eligible:
            selected_mode = "FUTURES_SHORT"
            reason = "SYMMETRIC_SHORT_ONLY_ELIGIBLE"
            capital_intent = "FUTURES"
            locked_position_size_usdt = _number((short_prediction or {}).get("position_size_usdt"), 0.0)
        else:
            reason = "SYMMETRIC_BOTH_SETUPS_NOT_ELIGIBLE"
    elif long_setup:
        if not long_entries_enabled:
            selected_mode = "ABSTAIN"
            reason = "RANGE_SCALP_LONG_DISABLED"
        elif futures_long_eligible or spot_long_eligible:
            selected_mode = long_mode
            reason = "SYMMETRIC_LONG_SETUP"
            capital_intent = "FUTURES" if long_mode == "FUTURES_LONG" else "SPOT"
            locked_position_size_usdt = _number(
                (futures_long_prediction if long_mode == "FUTURES_LONG" else long_prediction or {}).get("position_size_usdt"),
                0.0,
            )
        else:
            reason = "SYMMETRIC_LONG_SETUP_NOT_ELIGIBLE"
    elif short_setup:
        if short_eligible:
            selected_mode = "FUTURES_SHORT"
            reason = "SYMMETRIC_SHORT_SETUP"
            capital_intent = "FUTURES"
            locked_position_size_usdt = _number((short_prediction or {}).get("position_size_usdt"), 0.0)
        else:
            reason = "SYMMETRIC_SHORT_SETUP_NOT_ELIGIBLE"
    else:
        reason = "NO_CLEAR_SR_SETUP"

    return DirectionalArbitration(
        symbol=symbol,
        selected_mode=selected_mode,
        reason=reason,
        capital_intent=capital_intent,
        locked_position_size_usdt=round(locked_position_size_usdt, 6),
        short_score=round(short_score, 6),
        long_score=round(long_score, 6),
        short_eligible=short_eligible,
        long_eligible=bool(futures_long_eligible or spot_long_eligible),
        long_structure=long_structure,
        short_structure=short_structure,
        short_resistance_setup=short_resistance_setup,
        futures_executable=bool(futures_executable),
        short_prediction=dict(short_prediction or {}),
        long_prediction=dict(long_prediction or {}),
        futures_long_prediction=dict(futures_long_prediction or {}),
    )
