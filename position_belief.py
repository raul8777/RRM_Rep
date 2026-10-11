"""position_belief.py — belief-driven exit arbiter.

STAGING s130 Belief-Driven Exit Lifecycle, Fases 2-4 (2026-08-16), implemented
active (no shadow stage) per explicit user directive: "no queremos que nada
quede en shadow, implementa de una vez y se verificara en staging su
comportamiento".

Answers one question for an open position: "do we believe it will keep
moving in our favor, or has it exhausted its move?" — replacing time-based
holds/closes (PRS timers, SSO stagnation clocks, target-diario) as the
dominant closing criterion.

belief_state ∈ {"running_strong", "exhausted", "uncertain", "adverse_watch"}

Design principles (see plan .windsurf/plans/sr-exit-lifecycle-redesign-741a4c.md):
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
import math
from typing import Any, Dict, List, Optional

from position_thesis import structural_stop_price, structural_target_price

RUNNING_STRONG = "running_strong"
EXHAUSTED = "exhausted"
UNCERTAIN = "uncertain"
ADVERSE_WATCH = "adverse_watch"
STRUCTURALLY_INVALIDATED = "structurally_invalidated"
EXHAUSTED_CONFIRMED = "exhausted_confirmed"

_FAVOR_MOMENTUM_VELOCITY = 0.00005   # per-second, already side-normalized by _analyze_drawdown_trend
_ADVERSE_MOMENTUM_VELOCITY = -0.00005
_SIGNAL_STRENGTH_MIN = 0.55
_NEAR_STOP_THRESHOLD_PCT = 0.005     # 0.5% — inside this band the bar rises

# Economic recovery threshold: a hold is only justified if the estimated
# probability of recovery before the hard stop exceeds the geometric break-even.
# This is used as an *evidence source*, not a hardcoded timer.
_RECOVERY_ECONOMICS_SAFETY_MARGIN = 1.2


def _finite_number(value: Any) -> Optional[float]:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _probability(value: Any) -> Optional[float]:
    result = _finite_number(value)
    return result if result is not None and 0.0 <= result <= 1.0 else None


def _distance_to_target_pct(current_price: float, target_price: Optional[float], side: Optional[str]) -> Optional[float]:
    if not target_price or not current_price or current_price <= 0:
        return None
    if _is_long(side):
        return max(0.0, (target_price - current_price) / current_price)
    return max(0.0, (current_price - target_price) / current_price)


def _compute_required_recovery_probability(distance_to_stop_pct: Optional[float], distance_to_target_pct: Optional[float]) -> float:
    """Return the minimum P(recovery before stop) that makes holding break-even.

    If we stand to lose L (distance to stop) to gain G (distance to target),
    the break-even probability is L / (G + L).  When G >> L, a small
    probability of recovery is enough to justify holding.
    """
    l = distance_to_stop_pct if distance_to_stop_pct is not None else 0.5
    g = distance_to_target_pct if distance_to_target_pct is not None else 0.5
    total = g + l
    if total <= 0:
        return 0.5
    return l / total


def _estimate_recovery_probability(
    *,
    favorable_sources: set,
    adverse_sources: set,
    recovery_signal: bool,
    near_stop: bool,
) -> float:
    """Point-in-time heuristic estimator for P(recovery before hard stop).

    This is intentionally conservative: it starts at 0.5 and only moves when
    there is concrete evidence.  It will be replaced by a trained recovery model
    once we have enough labeled telemetry.
    """
    p = 0.50
    if "signal" in favorable_sources:
        p += 0.15
    if "momentum" in favorable_sources:
        p += 0.10
    if "sr_structure" in favorable_sources:
        p += 0.20
    if "peak_capture" in favorable_sources:
        p += 0.05
    if "probability" in favorable_sources:
        p += 0.05

    if "signal" in adverse_sources:
        p -= 0.25
    if "momentum" in adverse_sources:
        p -= 0.15
    if "sr_structure" in adverse_sources:
        p -= 0.30
    if "peak_capture" in adverse_sources:
        p -= 0.10
    if "probability" in adverse_sources:
        p -= 0.10

    if recovery_signal and "momentum" not in adverse_sources:
        p += 0.10
    if near_stop and adverse_sources:
        # Near the stop, holding is geometrically worse, so discount optimism.
        p -= 0.15

    return max(0.05, min(0.95, p))


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
    entry_price: Optional[float] = None,
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
    invalid_reason = None
    raw_side = str(side or "").strip().upper()
    if raw_side not in ("LONG", "BUY", "SHORT", "SELL"):
        invalid_reason = "side_unavailable"
    normalized_side = "LONG" if _is_long(raw_side) else "SHORT"
    thesis_side = str((thesis_snapshot or {}).get("side") or "").strip().upper()
    if thesis_side and thesis_side not in ({"LONG", "BUY"} if normalized_side == "LONG" else {"SHORT", "SELL"}):
        invalid_reason = "thesis_side_mismatch"
    try:
        current_price = float(current_price)
        if not math.isfinite(current_price) or current_price <= 0:
            invalid_reason = "current_price_unavailable"
    except (TypeError, ValueError, OverflowError):
        invalid_reason = "current_price_unavailable"
    if invalid_reason:
        return {
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "belief_state": UNCERTAIN,
            "reasons": [invalid_reason],
            "favorable_sources": [], "adverse_sources": [], "decisive_evidence": [],
            "structural_stop_price": None, "structural_target_price": None,
            "distance_to_structural_stop_pct": None,
            "structural_stop_breached": False, "thesis_complete": False,
        }
    resolved_entry_price = _finite_number(entry_price)
    if resolved_entry_price is None:
        resolved_entry_price = _finite_number((thesis_snapshot or {}).get("entry_price"))
    stop_price = structural_stop_price(thesis_snapshot, resolved_entry_price)
    target_price = structural_target_price(thesis_snapshot, resolved_entry_price)
    distance_pct = _distance_to_stop_pct(current_price, stop_price, side)
    breached = _structural_stop_breached(current_price, stop_price, side)
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
        "entry_price": resolved_entry_price,
        "thesis_complete": thesis_complete,
    }

    if breached:
        result["belief_state"] = EXHAUSTED
        result["reasons"] = ["structural_stop_breached"]
        result["favorable_sources"] = []
        result["adverse_sources"] = ["structure"]
        result["decisive_evidence"] = ["structural_stop_breached"]
        return result

    path = sr_path_state or {}
    path_state = str(path.get("state") or "").strip().lower()
    path_mode = str(path.get("mode") or "shadow").strip().lower()
    sr_invalidated = path_mode == "active" and path_state == "invalidated"
    if sr_invalidated:
        result["belief_state"] = EXHAUSTED
        result["reasons"] = ["sr_path_invalidated"]
        result["favorable_sources"] = []
        result["adverse_sources"] = ["sr_structure"]
        result["decisive_evidence"] = ["sr_path_invalidated"]
        return result

    if not thesis_complete:
        result["belief_state"] = UNCERTAIN
        raw_stop = _finite_number((thesis_snapshot or {}).get("stop_price"))
        raw_target = _finite_number((thesis_snapshot or {}).get("target_price"))
        invalid_geometry = resolved_entry_price is not None and (
            (raw_stop is not None and stop_price is None)
            or (raw_target is not None and target_price is None)
        )
        result["reasons"] = ["thesis_geometry_invalid"] if invalid_geometry else ["thesis_incomplete"]
        result["favorable_sources"] = []
        result["adverse_sources"] = []
        result["decisive_evidence"] = []
        return result

    target_distance_pct = _distance_to_target_pct(current_price, target_price, side)
    if not all(value is not None and math.isfinite(value) for value in (distance_pct, target_distance_pct)):
        result["distance_to_structural_stop_pct"] = None
        path = sr_path_state or {}
        invalidated = str(path.get("mode") or "shadow").lower() == "active" and str(path.get("state") or "").lower() == "invalidated"
        result["belief_state"] = EXHAUSTED if invalidated else UNCERTAIN
        result["reasons"] = ["sr_path_invalidated" if invalidated else "structural_geometry_unavailable"]
        result["favorable_sources"] = []
        result["adverse_sources"] = ["sr_structure"] if invalidated else []
        result["decisive_evidence"] = ["sr_path_invalidated"] if invalidated else []
        return result

    reasons: List[str] = []
    favorable_sources: set = set()
    adverse_sources: set = set()
    recovery_signal = bool((momentum or {}).get("recovery_signal"))

    velocity = _finite_number((momentum or {}).get("velocity_per_sec"))
    if velocity is not None:
        if recovery_signal or velocity >= _FAVOR_MOMENTUM_VELOCITY:
            favorable_sources.add("momentum")
            reasons.append("momentum_favorable")
        elif velocity <= _ADVERSE_MOMENTUM_VELOCITY:
            adverse_sources.add("momentum")
            reasons.append("momentum_adverse")

    entry_prob = _probability((thesis_snapshot or {}).get("entry_success_probability"))
    current_success_probability = _probability(current_success_probability)
    if entry_prob is not None and current_success_probability is not None:
        if current_success_probability > entry_prob:
            favorable_sources.add("probability")
            reasons.append("probability_rising")
        elif current_success_probability < entry_prob:
            adverse_sources.add("probability")
            reasons.append("probability_falling")

    if signal_direction:
        sig = str(signal_direction).strip().upper()
        strength = _finite_number(signal_strength)
        is_long = _is_long(side)
        aligned = (is_long and sig == "BUY") or (not is_long and sig == "SELL")
        opposite = (is_long and sig == "SELL") or (not is_long and sig == "BUY")
        if strength is not None and strength >= _SIGNAL_STRENGTH_MIN:
            if aligned:
                favorable_sources.add("signal")
                reasons.append("signal_aligned")
            elif opposite:
                adverse_sources.add("signal")
                reasons.append("signal_against")

    peak_capture_prob_higher_peak = _probability(peak_capture_prob_higher_peak)
    if peak_capture_prob_higher_peak is not None:
        if peak_capture_prob_higher_peak >= 0.55:
            favorable_sources.add("peak_capture")
            reasons.append("ev_favors_continuation")
        elif peak_capture_prob_higher_peak <= 0.35:
            adverse_sources.add("peak_capture")
            reasons.append("ev_favors_capture")

    # sr_path_v1: fresh structural evidence from the thesis-anchored path
    # evaluator (candle-close gated). Only consumed when its mode is "active" —
    # in "shadow" the states are persisted/audited but never influence belief.
    sr_active = path_mode == "active"
    sr_confirms_thesis = sr_active and path_state in (
        "on_track", "target_reached", "extended_to_next_level", "structural_confirmed"
    )
    sr_invalidated = sr_active and path_state == "invalidated"
    sr_stalled = sr_active and path_state == "stalled"
    if sr_invalidated:
        # Structural invalidation at primary candle close is decisive
        # evidence against the thesis — same weight class as a stop breach.
        adverse_sources.add("sr_structure")
        reasons.append("sr_path_invalidated")
    elif sr_stalled:
        adverse_sources.add("sr_structure")
        reasons.append("sr_path_stalled")
    elif sr_confirms_thesis:
        favorable_sources.add("sr_structure")
        reasons.append(f"sr_path_{path_state}")

    pnl_usdt = _finite_number(pnl_usdt)
    is_loser = pnl_usdt is not None and pnl_usdt < 0
    near_stop = distance_pct is not None and distance_pct <= _NEAR_STOP_THRESHOLD_PCT
    required_recovery_probability = _compute_required_recovery_probability(distance_pct, target_distance_pct)
    estimated_recovery_probability = _estimate_recovery_probability(
        favorable_sources=favorable_sources,
        adverse_sources=adverse_sources,
        recovery_signal=recovery_signal,
        near_stop=near_stop,
    )
    recovery_economics_unfavorable = (
        estimated_recovery_probability < required_recovery_probability * _RECOVERY_ECONOMICS_SAFETY_MARGIN
    )

    # Structural invalidation is decisive and closes immediately, regardless of
    # other evidence or economics.  It is the candle-close sibling of a stop breach.
    if sr_invalidated:
        result["belief_state"] = EXHAUSTED
        result["reasons"] = reasons or ["sr_path_invalidated"]
        result["favorable_sources"] = sorted(favorable_sources)
        result["adverse_sources"] = sorted(adverse_sources)
        result["decisive_evidence"] = ["sr_path_invalidated"]
        result["recovery_economics"] = {
            "required_recovery_probability": round(required_recovery_probability, 4),
            "estimated_recovery_probability": round(estimated_recovery_probability, 4),
            "unfavorable": recovery_economics_unfavorable,
        }
        return result

    # Asymmetric confidence bar: a losing position needs more corroborating
    # evidence than a winner before we believe it will keep going our way.
    if pnl_usdt is None:
        result["belief_state"] = UNCERTAIN
        result["reasons"] = reasons + ["pnl_unavailable"]
        result["favorable_sources"] = sorted(favorable_sources)
        result["adverse_sources"] = sorted(adverse_sources)
        result["decisive_evidence"] = []
        return result
    min_favor_for_strong = 2 if is_loser else 1

    running_strong = (
        len(favorable_sources) >= min_favor_for_strong
        and not adverse_sources
        and not (near_stop and not recovery_signal)
    )

    if running_strong:
        result["belief_state"] = RUNNING_STRONG
        if not reasons:
            reasons.append("no_adverse_evidence")
        result["reasons"] = reasons
        result["favorable_sources"] = sorted(favorable_sources)
        result["adverse_sources"] = sorted(adverse_sources)
        result["decisive_evidence"] = []
        result["recovery_economics"] = {
            "required_recovery_probability": round(required_recovery_probability, 4),
            "estimated_recovery_probability": round(estimated_recovery_probability, 4),
            "unfavorable": recovery_economics_unfavorable,
        }
        return result

    # Confirmed exhaustion requires decisive evidence that continued holding is
    # very likely to produce additional loss.  We do NOT close on a single
    # transient adverse source (e.g. one signal_against tick), and we do NOT treat
    # neutral observations as corroborating evidence.
    #
    # A close is justified by any of:
    #   1. two or more independent adverse sources without favorable sources;
    #   2. near the structural stop + at least one adverse source (geometry makes
    #      holding asymmetrically dangerous);
    #   3. the economic recovery estimate is worse than the geometric break-even
    #      probability and there is at least one adverse source.
    decisive_evidence: List[str] = []
    independent_adverse = len(adverse_sources)
    if independent_adverse >= 2 and not favorable_sources:
        decisive_evidence.append("multiple_independent_adverse_sources")
    if near_stop and adverse_sources and not favorable_sources:
        decisive_evidence.append("near_stop_with_adverse_evidence")
    # Recovery economics is persisted for audit/model training, but it only
    # corroborates existing decisive evidence — it never authorizes a close by
    # itself until a trained recovery model replaces the heuristic below.
    if recovery_economics_unfavorable and adverse_sources and not favorable_sources and (near_stop or independent_adverse >= 2):
        decisive_evidence.append("recovery_economics_unfavorable")

    if decisive_evidence:
        result["belief_state"] = EXHAUSTED
        result["reasons"] = reasons or ["adverse_evidence_no_favor"]
        result["favorable_sources"] = sorted(favorable_sources)
        result["adverse_sources"] = sorted(adverse_sources)
        result["decisive_evidence"] = decisive_evidence
        result["recovery_economics"] = {
            "required_recovery_probability": round(required_recovery_probability, 4),
            "estimated_recovery_probability": round(estimated_recovery_probability, 4),
            "unfavorable": recovery_economics_unfavorable,
        }
        return result

    # A single adverse source is not enough to be "sure" of further loss.  It
    # puts the position under watch, allowing the next telemetry cycles to
    # either corroborate (→ exhausted) or clear (→ running/uncertain).
    if adverse_sources:
        result["belief_state"] = ADVERSE_WATCH
        result["reasons"] = reasons or ["single_adverse_source"]
        result["favorable_sources"] = sorted(favorable_sources)
        result["adverse_sources"] = sorted(adverse_sources)
        result["decisive_evidence"] = []
        result["recovery_economics"] = {
            "required_recovery_probability": round(required_recovery_probability, 4),
            "estimated_recovery_probability": round(estimated_recovery_probability, 4),
            "unfavorable": recovery_economics_unfavorable,
        }
        return result

    result["belief_state"] = UNCERTAIN
    result["reasons"] = reasons or ["insufficient_evidence"]
    result["favorable_sources"] = sorted(favorable_sources)
    result["adverse_sources"] = sorted(adverse_sources)
    result["decisive_evidence"] = []
    result["recovery_economics"] = {
        "required_recovery_probability": round(required_recovery_probability, 4),
        "estimated_recovery_probability": round(estimated_recovery_probability, 4),
        "unfavorable": recovery_economics_unfavorable,
    }
    return result
