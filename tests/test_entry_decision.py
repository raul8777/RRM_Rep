from types import SimpleNamespace

from entry_decision import evaluate


def _settings(**overrides):
    values = {
        "entry_decision_enabled": True,
        "entry_decision_policy_version": "strict_v1",
        "entry_decision_regime_mode": {
            "SPOT_BULL": "enforced",
            "RANGE_SCALP": "enforced",
            "FUTURES_SHORT": "blocked",
            "PRESERVATION": "blocked",
        },
        "entry_decision_min_expected_pnl": {
            "SPOT_BULL": 0.0,
            "RANGE_SCALP": 0.0,
            "FUTURES_SHORT": 0.0,
            "PRESERVATION": 0.0,
        },
        "entry_decision_min_probability": {
            "SPOT_BULL": 0.60,
            "RANGE_SCALP": 0.60,
            "FUTURES_SHORT": 0.60,
            "PRESERVATION": 0.60,
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_blocks_regime_without_explicit_promotion():
    decision = evaluate(
        {"expected_pnl": 1.0, "success_probability": 0.8},
        "FUTURES_SHORT", "A", 1.0, _settings(),
    )
    assert decision.eligible is False
    assert decision.reason == "REGIME_NOT_ENABLED"


def test_requires_positive_ev_and_probability():
    settings = _settings(entry_decision_regime_mode={"SPOT_BULL": "enforced"})
    negative = evaluate(
        {"expected_pnl": -0.01, "success_probability": 0.8},
        "SPOT_BULL", "A", 1.0, settings,
    )
    weak = evaluate(
        {"expected_pnl": 1.0, "success_probability": 0.59},
        "SPOT_BULL", "A", 1.0, settings,
    )
    assert negative.reason == "EV_NON_POSITIVE"
    assert weak.reason == "PROBABILITY_BELOW_THRESHOLD"


def test_range_scalp_requires_valid_reversion_payoff_when_blocking():
    settings = _settings(
        entry_decision_regime_mode={"RANGE_SCALP": "enforced"},
        range_reversion_predictor_mode="blocking",
    )
    invalid = evaluate(
        {"thesis_type": "LONG_SUPPORT_BOUNCE", "expected_pnl": 2.0, "success_probability": 0.8},
        "RANGE_SCALP", "A", 1.0, settings,
    )
    valid = evaluate(
        {
            "range_reversion_prediction": {
                "valid": True,
                "model": "range_reversion_v1",
                "expected_pnl": 1.0,
                "expected_pnl_percentage": 1.0,
                "success_probability": 0.7,
                "target_price": 102.0,
                "stop_price": 99.0,
            }
        },
        "RANGE_SCALP", "A", 1.0, settings,
    )
    assert invalid.reason == "RANGE_MODEL_INVALID"
    assert valid.eligible is True
    assert valid.model == "range_reversion_v1"


def test_range_pre_arbitration_candidate_is_not_vetoed_by_reversion_model():
    settings = _settings(
        entry_decision_regime_mode={"RANGE_SCALP": "enforced"},
        range_reversion_predictor_mode="enforced",
    )
    decision = evaluate(
        {
            "entry_decision_prediction": {
                "expected_pnl": 2.0,
                "success_probability": 0.7,
                "price_range": {"min": 99.0, "max": 102.0},
            }
        },
        "RANGE_SCALP", "A", 1.0, settings, current_price=100.0,
    )
    assert decision.eligible is True
    assert decision.model == "pre_arbitration_pending"


def test_range_continuation_uses_directional_model_without_reversion_veto():
    settings = _settings(
        entry_decision_regime_mode={"RANGE_SCALP": "enforced"},
        range_reversion_predictor_mode="enforced",
    )
    for thesis_type, side, target, stop in (
        ("LONG_RESISTANCE_BREAKOUT", "LONG", 102.0, 99.0),
        ("SHORT_SUPPORT_BREAKDOWN", "SHORT", 98.0, 101.0),
    ):
        decision = evaluate(
            {
                "thesis_type": thesis_type,
                "expected_side": side,
                "entry_decision_prediction": {
                    "model": "directional_continuation_v1",
                    "expected_pnl": 2.0,
                    "expected_pnl_percentage": 1.0,
                    "success_probability": 0.7,
                    "target_price": target,
                    "stop_price": stop,
                },
                "range_reversion_prediction": {"valid": False, "reason": "invalid_reversion_payoff"},
            },
            "RANGE_SCALP", "A", 1.0, settings, current_price=100.0,
        )
        assert decision.eligible is True
        assert decision.reason == "ENTRY_DECISION_ACCEPTED"
        assert decision.model == "directional_continuation_v1"


def test_range_continuation_requires_oriented_target_and_stop():
    settings = _settings(
        entry_decision_regime_mode={"RANGE_SCALP": "enforced"},
        range_reversion_predictor_mode="enforced",
    )
    decision = evaluate(
        {
            "thesis_type": "LONG_RESISTANCE_BREAKOUT",
            "expected_side": "LONG",
            "entry_decision_prediction": {
                "expected_pnl": 2.0,
                "success_probability": 0.7,
                "target_price": 99.0,
                "stop_price": 101.0,
            },
        },
        "RANGE_SCALP", "A", 1.0, settings, current_price=100.0,
    )
    assert decision.reason == "CONTINUATION_TARGET_STOP_INVALID"


def test_range_reversal_still_requires_valid_reversion_model():
    settings = _settings(
        entry_decision_regime_mode={"RANGE_SCALP": "enforced"},
        range_reversion_predictor_mode="enforced",
    )
    decision = evaluate(
        {
            "thesis_type": "LONG_SUPPORT_BOUNCE",
            "expected_pnl": 2.0,
            "success_probability": 0.7,
            "range_reversion_prediction": {"valid": False},
        },
        "RANGE_SCALP", "A", 1.0, settings,
    )
    assert decision.reason == "RANGE_MODEL_INVALID"


def test_range_scalp_directional_edge_uses_selected_contract_without_reversion_model():
    settings = _settings(
        entry_decision_regime_mode={"RANGE_SCALP": "enforced"},
        entry_decision_min_expected_pnl={"RANGE_SCALP": 0.0},
        entry_decision_min_probability={"RANGE_SCALP": 0.5},
    )
    decision = evaluate(
        {
            "expected_side": "LONG",
            "thesis_type": "LONG_DIRECTIONAL_EDGE",
            "entry_decision_prediction": {
                "expected_pnl": 1.5,
                "success_probability": 0.7,
                "current_price": 100.0,
                "target_price": 104.0,
                "stop_price": 98.0,
            },
            "range_reversion_prediction": {"valid": False, "reason": "shadow_skipped"},
        },
        "RANGE_SCALP", "A", 0.8, settings, current_price=100.0,
    )

    assert decision.eligible is True
    assert decision.reason == "ENTRY_DECISION_ACCEPTED"
    assert decision.target_price == 104.0
    assert decision.stop_price == 98.0


def test_rejects_degenerate_and_missing_predictions():
    settings = _settings(entry_decision_regime_mode={"SPOT_BULL": "enforced"})
    degenerate = evaluate(
        {"degenerate_output": True, "expected_pnl": 1.0, "success_probability": 0.8},
        "SPOT_BULL", "A", 1.0, settings,
    )
    missing = evaluate({}, "SPOT_BULL", "A", 1.0, settings)
    assert degenerate.reason == "PREDICTOR_INVALID"
    assert missing.reason == "MISSING_PREDICTION"
