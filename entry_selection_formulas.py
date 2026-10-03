"""Pure offline ranking policies for a frozen universal opportunity tape."""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple


PILOT_FORMULAS = (
    "baseline_current_universal",
    "executable_contract_ev",
    "predictor_calibrated_ev",
    "robust_lower_confidence_ev",
    "qubo_calibrated",
)


@dataclass(frozen=True)
class FormulaCandidate:
    opportunity_event_id: str
    dedupe_cluster_id: str
    symbol: str
    contract: Mapping[str, Any]
    eligible: bool
    rejection_reason: Optional[str] = None
    baseline_priority: Tuple[float, ...] = ()
    contract_ev_net_usdt: Optional[float] = None
    calibrated_probability: Optional[float] = None
    probability_calibration_status: str = "pending"
    probability_calibration_id: Optional[str] = None
    gross_reward_usdt: Optional[float] = None
    gross_risk_usdt: Optional[float] = None
    total_costs_usdt: Optional[float] = None
    ev_standard_error_usdt: Optional[float] = None
    uncertainty_usdt: Optional[float] = None


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _contract_rejection(candidate: FormulaCandidate) -> Optional[str]:
    if not candidate.eligible:
        return candidate.rejection_reason or "BASELINE_INELIGIBLE"
    if not isinstance(candidate.contract, Mapping):
        return "CONTRACT_MISSING"
    if candidate.contract.get("executable") is not True:
        return "CONTRACT_NOT_EXECUTABLE"
    venue_executable = candidate.contract.get("venue_executable_at_time")
    if venue_executable is None:
        return "VENUE_EXECUTABILITY_UNKNOWN"
    if venue_executable is not True:
        return "VENUE_NOT_EXECUTABLE_AT_TIME"
    return None


def _calibrated_ev(candidate: FormulaCandidate) -> Tuple[Optional[float], Optional[str]]:
    if candidate.probability_calibration_status not in {"passed", "train_fold_base_rate"} or not candidate.probability_calibration_id:
        return None, "PROBABILITY_NOT_CALIBRATED"
    probability = _finite(candidate.calibrated_probability)
    gain = _finite(candidate.gross_reward_usdt)
    risk = _finite(candidate.gross_risk_usdt)
    costs = _finite(candidate.total_costs_usdt)
    if probability is None or not 0.0 <= probability <= 1.0:
        return None, "CALIBRATED_PROBABILITY_MISSING_OR_INVALID"
    if gain is None or risk is None or costs is None or gain < 0 or risk < 0 or costs < 0:
        return None, "CONTRACT_ECONOMICS_MISSING_OR_INVALID"
    return probability * gain - (1.0 - probability) * risk - costs, None


def _score(
    candidate: FormulaCandidate,
    formula: str,
    *,
    confidence_z: float,
) -> Tuple[Optional[Tuple[float, ...]], Optional[str]]:
    if formula == "baseline_current_universal":
        if not candidate.baseline_priority:
            return None, "BASELINE_PRIORITY_MISSING"
        priority = tuple(_finite(value) for value in candidate.baseline_priority)
        if any(value is None for value in priority):
            return None, "BASELINE_PRIORITY_INVALID"
        return tuple(float(value) for value in priority), None

    if formula in {"predictor_calibrated_ev", "qubo_calibrated"}:
        ev, reason = _calibrated_ev(candidate)
        if reason:
            return None, reason
    else:
        if formula in {"executable_contract_ev", "robust_lower_confidence_ev"}:
            probability = _finite(candidate.calibrated_probability)
            if (
                candidate.probability_calibration_status not in {"passed", "train_fold_base_rate"}
                or not candidate.probability_calibration_id
                or probability is None
                or not 0.0 <= probability <= 1.0
            ):
                return None, "PROBABILITY_NOT_CALIBRATED"
        ev = _finite(candidate.contract_ev_net_usdt)
        if ev is None:
            return None, "CONTRACT_EV_MISSING_OR_INVALID"

    if formula in {"executable_contract_ev", "predictor_calibrated_ev"}:
        return (float(ev),), None
    if formula == "robust_lower_confidence_ev":
        standard_error = _finite(candidate.ev_standard_error_usdt)
        if standard_error is None or standard_error < 0:
            return None, "EV_UNCERTAINTY_MISSING_OR_INVALID"
        return (float(ev) - confidence_z * standard_error,), None
    if formula == "qubo_calibrated":
        risk = _finite(candidate.contract.get("economic_risk_usdt"))
        uncertainty = _finite(candidate.uncertainty_usdt)
        if risk is None or risk < 0 or uncertainty is None or uncertainty < 0:
            return None, "QUBO_RISK_OR_UNCERTAINTY_MISSING"
        return (float(ev),), None
    raise ValueError(f"unknown entry-selection formula: {formula}")


def _correlation(
    correlation_matrix: Mapping[Tuple[str, str], float],
    left: FormulaCandidate,
    right: FormulaCandidate,
) -> float:
    value = correlation_matrix.get((left.symbol, right.symbol))
    if value is None:
        value = correlation_matrix.get((right.symbol, left.symbol), 0.0)
    correlation = _finite(value)
    return min(1.0, abs(correlation)) if correlation is not None else 0.0


def _qubo_select(
    candidates: Sequence[FormulaCandidate],
    linear_scores: Sequence[float],
    capacity: int,
    correlation_matrix: Mapping[Tuple[str, str], float],
    lambda_correlation: float,
) -> Tuple[set[int], str, float]:
    limit = min(capacity, len(candidates))

    def objective(indices: Sequence[int]) -> float:
        score = sum(linear_scores[index] for index in indices)
        for left_pos, left_index in enumerate(indices):
            for right_index in indices[left_pos + 1:]:
                left = candidates[left_index]
                right = candidates[right_index]
                left_risk = float(left.contract["economic_risk_usdt"])
                right_risk = float(right.contract["economic_risk_usdt"])
                score -= lambda_correlation * _correlation(correlation_matrix, left, right) * math.sqrt(left_risk * right_risk)
        return score

    if len(candidates) <= 15:
        best_indices: Tuple[int, ...] = ()
        best_score = 0.0
        for size in range(1, limit + 1):
            for indices in itertools.combinations(range(len(candidates)), size):
                score = objective(indices)
                if score > best_score + 1e-12:
                    best_indices = indices
                    best_score = score
        return set(best_indices), "exact_bruteforce", best_score

    selected: list[int] = []
    remaining = set(range(len(candidates)))
    while remaining and len(selected) < limit:
        marginal_scores = []
        for index in remaining:
            marginal_scores.append((objective([*selected, index]) - objective(selected), index))
        marginal_score, best_index = max(marginal_scores, key=lambda item: (item[0], -item[1]))
        if marginal_score <= 0:
            break
        selected.append(best_index)
        remaining.remove(best_index)
    return set(selected), "deterministic_greedy", objective(selected)


def select_candidates(
    candidates: Sequence[FormulaCandidate],
    formula: str,
    capacity: int,
    *,
    confidence_z: float = 1.645,
    correlation_matrix: Optional[Mapping[Tuple[str, str], float]] = None,
    lambda_correlation: float = 0.0,
) -> Dict[str, Any]:
    """Rank/select fixed event winners without changing their contracts."""
    if formula not in PILOT_FORMULAS:
        raise ValueError(f"unknown entry-selection formula: {formula}")
    if capacity < 0:
        raise ValueError("capacity must be non-negative")
    if confidence_z < 0 or lambda_correlation < 0:
        raise ValueError("confidence and correlation penalties must be non-negative")
    event_ids = [candidate.opportunity_event_id for candidate in candidates]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("candidate tape contains duplicate opportunity_event_id values")

    correlation_matrix = correlation_matrix or {}
    scored = []
    rejected = []
    for candidate in candidates:
        reason = _contract_rejection(candidate)
        if reason:
            rejected.append((candidate, reason))
            continue
        value, reason = _score(candidate, formula, confidence_z=confidence_z)
        if reason:
            rejected.append((candidate, reason))
            continue
        if formula != "baseline_current_universal" and value[0] <= 0:
            rejected.append((candidate, "NON_POSITIVE_FORMULA_SCORE"))
            continue
        scored.append((candidate, value))

    solver = "ranked"
    objective_score = None
    selected_events: set[str]
    if formula == "qubo_calibrated":
        selected_indices, solver, objective_score = _qubo_select(
            [candidate for candidate, _ in scored],
            [value[0] for _, value in scored],
            capacity,
            correlation_matrix,
            lambda_correlation,
        )
        selected_events = {scored[index][0].opportunity_event_id for index in selected_indices}
    else:
        ordered = sorted(scored, key=lambda item: item[1], reverse=True)
        selected_events = {
            candidate.opportunity_event_id
            for candidate, _ in ordered[:capacity]
        }

    ranked = []
    for candidate, score in sorted(scored, key=lambda item: item[1], reverse=True):
        ranked.append({
            "opportunity_event_id": candidate.opportunity_event_id,
            "dedupe_cluster_id": candidate.dedupe_cluster_id,
            "symbol": candidate.symbol,
            "score": list(score),
            "selected": candidate.opportunity_event_id in selected_events,
            "rejection_reason": None if candidate.opportunity_event_id in selected_events else "CAPACITY_OR_PORTFOLIO_OBJECTIVE",
            "contract": dict(candidate.contract) if isinstance(candidate.contract, Mapping) else {},
        })
    for candidate, reason in rejected:
        ranked.append({
            "opportunity_event_id": candidate.opportunity_event_id,
            "dedupe_cluster_id": candidate.dedupe_cluster_id,
            "symbol": candidate.symbol,
            "score": None,
            "selected": False,
            "rejection_reason": reason,
            "contract": dict(candidate.contract) if isinstance(candidate.contract, Mapping) else {},
        })
    return {
        "formula": formula,
        "capacity": capacity,
        "selected_event_ids": sorted(selected_events),
        "ranked": ranked,
        "solver": solver,
        "objective_score": objective_score,
    }