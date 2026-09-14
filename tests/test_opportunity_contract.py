import pytest

from opportunity_contract import build_executable_opportunity, scale_contract


def _prediction(**extra):
    result = {"position_size_usdt": 100.0, "success_probability": .7,
              "target_price": 96.0, "stop_price": 102.0}
    result.update(extra)
    return result


def test_contract_contains_homogeneous_economic_fields_and_fixed_2x():
    contract = build_executable_opportunity("SHORT", "SHORT_SUPPORT_BREAKDOWN", _prediction(),
                                            {"current_price": 100.0, "fee_bps": 4,
                                             "slippage_bps": 2, "funding_bps": 1}, leverage=10)
    data = contract.to_dict()
    assert contract.executable
    assert data["leverage"] == 2
    assert data["qty"] == 2
    assert data["notional_usdt"] == 200
    assert data["economic_risk_usdt"] > 4
    assert data["projected_entry_fee_usdt"] > 0
    assert data["projected_exit_fee_usdt"] > 0
    assert data["expected_return_on_risk"] > 0


def test_unconfirmed_resistance_rejection_is_immediately_ineligible_only_for_that_thesis():
    meta = {"current_price": 100.0}
    rejection = build_executable_opportunity("SHORT", "SHORT_RESISTANCE_REJECTION", _prediction(), meta, leverage=2)
    breakdown = build_executable_opportunity("SHORT", "SHORT_SUPPORT_BREAKDOWN", _prediction(), meta, leverage=2)
    assert not rejection.executable
    assert rejection.rejection_evidence["explicit_rejection"] is False
    assert breakdown.executable


def test_breakout_pressure_invalidates_explicit_rejection():
    contract = build_executable_opportunity("SHORT", "SHORT_RESISTANCE_REJECTION", _prediction(),
        {"current_price": 100.0, "sr_rejection": True, "sr_breakout": True}, leverage=2)
    assert not contract.executable


def test_generic_directional_prediction_builds_geometry_from_risk_metrics():
    prediction = {
        "position_size_usdt": 100.0,
        "success_probability": 0.70,
        "risk_metrics": {"percentage_reward": 4.0, "percentage_risk": 2.0},
    }
    contract = build_executable_opportunity(
        "SHORT", "SHORT_SUPPORT_BREAKDOWN", prediction,
        {"current_price": 100.0}, leverage=2, minimum_probability=0.55,
    )
    assert contract.executable
    assert contract.effective_target_price == pytest.approx(96.0)
    assert contract.effective_stop_price == pytest.approx(102.0)


def test_max_loss_contract_reserves_exit_costs_before_price_risk():
    contract = build_executable_opportunity(
        "SHORT", "SHORT_SUPPORT_BREAKDOWN", _prediction(stop_price=102.0),
        {"current_price": 100.0}, leverage=2, max_loss_usd=1.0,
        entry_fee_bps=2.0, exit_fee_bps=8.0, slippage_bps=5.2,
    )
    assert contract.economic_risk_usdt <= 1.0 + 1e-9
    assert contract.effective_stop_price == pytest.approx(102.0)
    # Max-loss scaled notional down, not the stop.
    assert contract.notional_usdt < 200.0
    assert contract.margin_usdt < 100.0


def test_rejection_with_bullish_momentum_is_not_confirmed():
    contract = build_executable_opportunity(
        "SHORT", "SHORT_RESISTANCE_REJECTION", _prediction(),
        {"current_price": 100.0, "sr_rejection": True, "graph_momentum_5": 0.01},
        leverage=2,
    )
    assert not contract.executable


def test_contract_can_scale_down_but_never_above_locked_margin():
    contract = build_executable_opportunity("SHORT", "SHORT_SUPPORT_BREAKDOWN", _prediction(),
                                            {"current_price": 100.0}, leverage=2).to_dict()
    scaled = scale_contract(contract, 40)
    assert scaled["margin_usdt"] == 40
    assert scaled["notional_usdt"] == 80
    with pytest.raises(ValueError):
        scale_contract(contract, 100.01)
