from copy import deepcopy

import pytest

from entry_selection_formulas import FormulaCandidate, select_candidates


def _candidate(event_id, *, ev=10.0, priority=(1.0, 1.0), **overrides):
    values = {
        "opportunity_event_id": event_id,
        "dedupe_cluster_id": f"cluster:{event_id}",
        "symbol": event_id,
        "contract": {
            "side": "LONG",
            "mode": "SPOT_LONG",
            "executable": True,
            "venue_executable_at_time": True,
            "economic_risk_usdt": 5.0,
        },
        "eligible": True,
        "baseline_priority": priority,
        "contract_ev_net_usdt": ev,
        "calibrated_probability": 0.7,
        "probability_calibration_status": "passed",
        "probability_calibration_id": "train-fold-1-isotonic-v1",
        "gross_reward_usdt": 20.0,
        "gross_risk_usdt": 5.0,
        "total_costs_usdt": 1.0,
        "ev_standard_error_usdt": 1.0,
        "uncertainty_usdt": 1.0,
    }
    values.update(overrides)
    return FormulaCandidate(**values)


def test_formula_ranking_preserves_contract_and_limits_capacity():
    candidates = [_candidate("event-a", ev=4.0), _candidate("event-b", ev=9.0)]
    original_contracts = [deepcopy(candidate.contract) for candidate in candidates]

    result = select_candidates(candidates, "executable_contract_ev", capacity=1)

    assert result["selected_event_ids"] == ["event-b"]
    assert [row["contract"] for row in result["ranked"]] == original_contracts[::-1]
    assert {row["contract"]["side"] for row in result["ranked"]} == {"LONG"}
    assert {row["contract"]["mode"] for row in result["ranked"]} == {"SPOT_LONG"}


def test_baseline_uses_frozen_priority_not_formula_ev():
    candidates = [
        _candidate("high-ev", ev=100.0, priority=(0.2, 4.0)),
        _candidate("baseline-first", ev=1.0, priority=(0.8, 1.0)),
    ]

    result = select_candidates(candidates, "baseline_current_universal", capacity=1)

    assert result["selected_event_ids"] == ["baseline-first"]


def test_predictor_ev_fails_closed_without_fold_calibration():
    candidates = [
        _candidate("calibrated"),
        _candidate("raw", probability_calibration_status="pending", probability_calibration_id=None),
    ]

    result = select_candidates(candidates, "predictor_calibrated_ev", capacity=2)

    assert result["selected_event_ids"] == ["calibrated"]
    rejected = next(row for row in result["ranked"] if row["opportunity_event_id"] == "raw")
    assert rejected["rejection_reason"] == "PROBABILITY_NOT_CALIBRATED"


@pytest.mark.parametrize("formula", ["executable_contract_ev", "robust_lower_confidence_ev", "qubo_calibrated"])
def test_all_ev_families_fail_closed_without_fold_calibration(formula):
    result = select_candidates(
        [_candidate("raw", probability_calibration_status="pending", probability_calibration_id=None)],
        formula,
        capacity=1,
    )

    assert result["selected_event_ids"] == []
    assert result["ranked"][0]["rejection_reason"] == "PROBABILITY_NOT_CALIBRATED"


def test_malformed_contract_is_audited_as_rejected():
    candidate = _candidate("malformed", contract=None)

    result = select_candidates([candidate], "executable_contract_ev", capacity=1)

    assert result["selected_event_ids"] == []
    assert result["ranked"][0]["rejection_reason"] == "CONTRACT_MISSING"
    assert result["ranked"][0]["contract"] == {}


@pytest.mark.parametrize(
    "venue_status,expected_reason",
    [(None, "VENUE_EXECUTABILITY_UNKNOWN"), (False, "VENUE_NOT_EXECUTABLE_AT_TIME")],
)
def test_formula_selector_fails_closed_on_missing_or_unavailable_historical_venue(venue_status, expected_reason):
    candidate = _candidate("historical-venue", contract={
        "side": "LONG",
        "mode": "SPOT_LONG",
        "executable": True,
        "venue_executable_at_time": venue_status,
        "economic_risk_usdt": 5.0,
    })

    result = select_candidates([candidate], "baseline_current_universal", capacity=1)

    assert result["selected_event_ids"] == []
    assert result["ranked"][0]["rejection_reason"] == expected_reason


def test_robust_ev_uses_lower_confidence_score():
    candidates = [
        _candidate("mean-high", ev=10.0, ev_standard_error_usdt=5.0),
        _candidate("robust-high", ev=8.0, ev_standard_error_usdt=0.1),
    ]

    result = select_candidates(candidates, "robust_lower_confidence_ev", capacity=1, confidence_z=1.0)

    assert result["selected_event_ids"] == ["robust-high"]


def test_qubo_uses_exact_small_set_and_pairwise_correlation_penalty():
    candidates = [
        _candidate("a", ev=10.0, symbol="A"),
        _candidate("b", ev=9.0, symbol="B"),
        _candidate("c", ev=8.0, symbol="C"),
    ]

    result = select_candidates(
        candidates,
        "qubo_calibrated",
        capacity=2,
        correlation_matrix={("A", "B"): 1.0},
        lambda_correlation=2.0,
    )

    assert result["solver"] == "exact_bruteforce"
    assert result["selected_event_ids"] == ["a", "c"]


def test_formula_selector_rejects_duplicate_event_identity():
    with pytest.raises(ValueError, match="duplicate opportunity_event_id"):
        select_candidates([_candidate("same"), _candidate("same")], "executable_contract_ev", capacity=1)