"""position_belief.py — belief-driven exit arbiter.

STAGING s130 Belief-Driven Exit Lifecycle, Fases 2-4 (2026-08-16), implemented
active (no shadow stage) per explicit user directive: "no queremos que nada
quede en shadow, implementa de una vez y se verificara en staging su
comportamiento".

Answers one question for an open position: "do we believe it will keep
moving in our favor, or has it exhausted its move?" — replacing time-based
holds/closes (PRS timers, SSO stagnation clocks, target-diario) as the
dominant closing criterion.

belief_state ∈ {"running_strong", "exhausted", "uncertain"}

Design principles (see plan C:\\Users\\raul8\\.windsurf\\plans\\sr-exit-lifecycle-redesign-741a4c.md):
  - The hard limit on belief is STRUCTURAL, not a dollar amount or a clock:
    the entry thesis's own stop level (support for LONG, resistance for
    SHORT, or an explicit predictor stop_price). Crossing it forces
    `exhausted` immediately, regardless of momentum.
  - Confidence required to call a position "running_strong" is ASYMMETRIC:
    a losing position needs more corroborating evidence than a winning one,
    and the bar rises further as price approaches the structural stop.
  - Every input is optional; missing evidence never manufactures a decision
    — it just narrows how many signals are available to agree.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from position_thesis import structural_stop_price, structural_target_price

RUNNING_STRONG = "running_strong"
EXHAUSTED = "exhausted"
UNCERTAIN = "uncertain"

_FAVOR_MOMENTUM_VELOCITY = 0.00005   # per-second, already side-normalized by _analyze_drawdown_trend
_ADVERSE_MOMENTUM_VELOCITY = -0.00005
_SIGNAL_STRENGTH_MIN = 0.55
_NEAR_STOP_THRESHOLD_PCT = 0.005     # 0.5% — inside this band the bar rises


def _is_long(side: Optional[str]) -> bool:
    return str(side or "").strip().upper() in ("LONG", "BUY")


def _distance_to_stop_pct(current_price: float, stop_price: Optional[float], side: Optional[str]) -> Optional[float]:
    if not stop_price or not current_price or current_price <= 0:
        return None
    if _is_long(side):
        return max(0.0, (current_price - stop_price) / current_price)
    return max(0.0, (stop_price - current_price) / current_price)


def _structural_stop_breached(current_price: float, stop_price: Optional[float], side: Optional[str]) -> bool:
    if not stop_price or not current_price or current_price <= 0:
        return False
    if _is_long(side):
        return current_price <= stop_price
    return current_price >= stop_price


def evaluate_belief(
    *,
    side: str,
    pnl_usdt: float,
    current_price: float,
    thesis_snapshot: Optional[Dict[str, Any]] = None,
    momentum: Optional[Dict[str, Any]] = None,
    current_success_probability: Optional[float] = None,
    signal_direction: Optional[str] = None,
    signal_strength: Optional[float] = None,
    peak_capture_prob_higher_peak: Optional[float] = None,
    sr_path_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Evaluate the belief state for an open position.

    Args:
        side: "LONG"/"SHORT" (or "BUY"/"SELL", normalized).
        pnl_usdt: current unrealized PnL — drives the asymmetric confidence bar.
        current_price: current mark/last price.
        thesis_snapshot: output of `position_thesis.build_thesis_snapshot()`.
        momentum: output of `position_telemetry_watcher._analyze_drawdown_trend()`
            (velocity_per_sec/acceleration/recovery_signal, already side-normalized
            so positive = favorable regardless of LONG/SHORT).
        current_success_probability: predictor's success_probability right now,
            compared against `thesis_snapshot['entry_success_probability']`.
        signal_direction/signal_strength: current autonomous signal.
        peak_capture_prob_higher_peak: from `peak_capture_engine` EV model.

    Returns:
        dict with belief_state, reasons, structural_stop_price,
        structural_target_price, distance_to_structural_stop_pct,
        structural_stop_breached.
    """
    stop_price = structural_stop_price(thesis_snapshot)
    target_price = structural_target_price(thesis_snapshot)
    distance_pct = _distance_to_stop_pct(current_price, stop_price, side)
    breached = _structural_stop_breached(current_price, stop_price, side)
    thesis_side = str((thesis_snapshot or {}).get("side") or "").strip().upper()
    normalized_side = "LONG" if _is_long(side) else "SHORT"
    thesis_complete = bool(
        stop_price
        and target_price
        and thesis_side in ({"LONG", "BUY"} if normalized_side == "LONG" else {"SHORT", "SELL"})
    )

    result: Dict[str, Any] = {
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "structural_stop_price": stop_price,
        "structural_target_price": target_price,
        "distance_to_structural_stop_pct": distance_pct,
        "structural_stop_breached": breached,
        "thesis_complete": thesis_complete,
    }

    if breached:
        result["belief_state"] = EXHAUSTED
        result["reasons"] = ["structural_stop_breached"]
        return result

    if not thesis_complete:
        result["belief_state"] = UNCERTAIN
        result["reasons"] = ["thesis_incomplete"]
        return result

    reasons: List[str] = []
    favor = 0
    adverse = 0
    available = 0
    recovery_signal = bool((momentum or {}).get("recovery_signal"))

    velocity = (momentum or {}).get("velocity_per_sec")
    if velocity is not None:
        available += 1
        if recovery_signal or velocity >= _FAVOR_MOMENTUM_VELOCITY:
            favor += 1
            reasons.append("momentum_favorable")
        elif velocity <= _ADVERSE_MOMENTUM_VELOCITY:
            adverse += 1
            reasons.append("momentum_adverse")

    entry_prob = (thesis_snapshot or {}).get("entry_success_probability")
    if entry_prob is not None and current_success_probability is not None:
        available += 1
        if current_success_probability > entry_prob:
            favor += 1
            reasons.append("probability_rising")
        elif current_success_probability < entry_prob:
            adverse += 1
            reasons.append("probability_falling")

    if signal_direction:
        sig = str(signal_direction).strip().upper()
        strength = float(signal_strength or 0.0)
        is_long = _is_long(side)
        aligned = (is_long and sig == "BUY") or (not is_long and sig == "SELL")
        opposite = (is_long and sig == "SELL") or (not is_long and sig == "BUY")
        if strength >= _SIGNAL_STRENGTH_MIN:
            available += 1
            if aligned:
                favor += 1
                reasons.append("signal_aligned")
            elif opposite:
                adverse += 1
                reasons.append("signal_against")

    if peak_capture_prob_higher_peak is not None:
        available += 1
        if peak_capture_prob_higher_peak >= 0.55:
            favor += 1
            reasons.append("ev_favors_continuation")
        elif peak_capture_prob_higher_peak <= 0.35:
            adverse += 1
            reasons.append("ev_favors_capture")

    # sr_path_v1: fresh structural evidence from the thesis-anchored path
    # evaluator (candle-close gated). Only consumed when its mode is "active" —
    # in "shadow" the states are persisted/audited but never influence belief.
    path_state = str((sr_path_state or {}).get("state") or "").strip().lower()
    path_mode = str((sr_path_state or {}).get("mode") or "shadow").strip().lower()
    if path_state and path_mode == "active":
        available += 1
        if path_state == "invalidated":
            # Structural invalidation at primary candle close is decisive
            # evidence against the thesis — same weight class as a stop breach.
            adverse += 2
            reasons.append("sr_path_invalidated")
        elif path_state == "stalled":
            adverse += 1
            reasons.append("sr_path_stalled")
        elif path_state in ("on_track", "target_reached", "extended_to_next_level"):
            favor += 1
            reasons.append(f"sr_path_{path_state}")

    is_loser = pnl_usdt < 0
    near_stop = distance_pct is not None and distance_pct <= _NEAR_STOP_THRESHOLD_PCT

    # Asymmetric confidence bar: a losing position needs more corroborating
    # evidence than a winner before we believe it will keep going our way.
    min_favor_for_strong = 2 if is_loser else 1

    running_strong = (
        favor >= min_favor_for_strong
        and adverse == 0
        and not (near_stop and not recovery_signal)
    )

    if running_strong:
        result["belief_state"] = RUNNING_STRONG
        if not reasons:
            reasons.append("no_adverse_evidence")
        result["reasons"] = reasons
        return result

    exhausted = adverse >= 1 and favor == 0 and available >= 2
    if exhausted:
        result["belief_state"] = EXHAUSTED
        result["reasons"] = reasons or ["adverse_evidence_no_favor"]
        return result

    result["belief_state"] = UNCERTAIN
    result["reasons"] = reasons or ["insufficient_evidence"]
    return result
