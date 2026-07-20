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


def test_range_scalp_requires_valid_reversion_payoff():
    settings = _settings(entry_decision_regime_mode={"RANGE_SCALP": "enforced"})
    invalid = evaluate(
        {"expected_pnl": 2.0, "success_probability": 0.8},
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


def test_rejects_degenerate_and_missing_predictions():
    settings = _settings(entry_decision_regime_mode={"SPOT_BULL": "enforced"})
    degenerate = evaluate(
        {"degenerate_output": True, "expected_pnl": 1.0, "success_probability": 0.8},
        "SPOT_BULL", "A", 1.0, settings,
    )
    missing = evaluate({}, "SPOT_BULL", "A", 1.0, settings)
    assert degenerate.reason == "PREDICTOR_INVALID"
    assert missing.reason == "MISSING_PREDICTION"
