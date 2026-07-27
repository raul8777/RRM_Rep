from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class DirectionalArbitration:
    symbol: str
    selected_mode: str
    reason: str
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


def evaluate_range_scalp(
    symbol: str,
    metadata: Dict[str, Any],
    long_prediction: Optional[Dict[str, Any]],
    futures_long_prediction: Optional[Dict[str, Any]],
    short_prediction: Optional[Dict[str, Any]],
    futures_executable: bool,
    settings: Any,
) -> DirectionalArbitration:
    minimum_probability = _number(getattr(settings, "range_scalp_direction_min_probability", 0.55), 0.55)
    short_minimum_probability = _number(
        getattr(settings, "range_scalp_short_min_probability", 0.40),
        0.40,
    )
    short_bonus = _number(getattr(settings, "range_scalp_short_priority_bonus", 0.05), 0.05)
    long_delta = _number(getattr(settings, "range_scalp_long_superiority_delta", 0.10), 0.10)
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

    short_preferred = bool(
        getattr(settings, "range_scalp_short_first_enabled", True)
        and getattr(settings, "range_scalp_short_preferred", True)
    )
    long_entries_enabled = bool(getattr(settings, "range_scalp_long_entries_enabled", True))
    short_resistance_threshold = _number(
        getattr(settings, "range_scalp_short_resistance_threshold", 0.65),
        0.65,
    )

    zone_raw = metadata.get("sr_zone_position")
    zone = _number(zone_raw, 0.5) if zone_raw is not None else 0.5
    short_breakdown = bool(metadata.get("support_breakdown_sell") or metadata.get("sr_breakdown"))
    short_resistance_setup = bool(short_breakdown or zone >= short_resistance_threshold)
    support_bounce = bool(metadata.get("support_bounce_buy") or metadata.get("sr_bounce"))
    support_breakout = bool(metadata.get("support_breakout_buy") or metadata.get("sr_breakout"))
    long_structure = bool((support_bounce or support_breakout) and not short_breakdown)
    short_structure = not long_structure
    if short_breakdown:
        short_score += 0.20
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

    short_setup_eligible = bool(short_eligible and short_structure)
    if not futures_executable:
        selected_mode = "ABSTAIN"
        reason = "TRIPLE_UNIVERSE_REQUIRED"
    elif short_preferred and short_setup_eligible:
        selected_mode = "FUTURES_SHORT"
        reason = "SHORT_RESISTANCE_PRIORITY" if short_resistance_setup else "SHORT_GEOMETRY_PRIORITY"
    elif not long_entries_enabled:
        selected_mode = "ABSTAIN"
        reason = "RANGE_SCALP_LONG_DISABLED"
    elif long_structure and long_eligible:
        selected_mode = long_mode
        reason = "LONG_SUPPORT_OR_BREAKOUT"
    elif long_structure:
        selected_mode = "ABSTAIN"
        reason = "LONG_STRUCTURE_SHORT_BLOCKED"
    else:
        selected_mode = "ABSTAIN"
        reason = "SHORT_GEOMETRY_NOT_ELIGIBLE" if not short_eligible else "SHORT_PRIORITY_DISABLED"

    return DirectionalArbitration(
        symbol=symbol,
        selected_mode=selected_mode,
        reason=reason,
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
