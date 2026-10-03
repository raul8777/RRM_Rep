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


def test_unconfirmed_resistance_rejection_remains_executable_with_audit_evidence():
    meta = {"current_price": 100.0}
    rejection = build_executable_opportunity("SHORT", "SHORT_RESISTANCE_REJECTION", _prediction(), meta, leverage=2)
    breakdown = build_executable_opportunity("SHORT", "SHORT_SUPPORT_BREAKDOWN", _prediction(), meta, leverage=2)
    assert rejection.executable
    assert rejection.rejection_evidence["explicit_rejection"] is False
    assert breakdown.executable


def test_breakout_pressure_is_audit_evidence_not_a_directional_gate():
    contract = build_executable_opportunity("SHORT", "SHORT_RESISTANCE_REJECTION", _prediction(),
        {"current_price": 100.0, "sr_rejection": True, "sr_breakout": True}, leverage=2)
    assert contract.executable
    assert contract.rejection_evidence["breakout_pressure"] is True


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


def test_atr_noise_floor_widens_stop_and_resizes_within_max_loss():
    contract = build_executable_opportunity(
        "SHORT", "SHORT_RESISTANCE_REJECTION",
        _prediction(position_size_usdt=1000.0, success_probability=0.60),
        {"current_price": 100.0, "atr_pct": 3.0},
        leverage=2, max_loss_usd=18.0, minimum_probability=0.55,
        min_stop_atr_multiple=1.5,
        entry_fee_bps=2.0, exit_fee_bps=8.0, slippage_bps=5.2,
    )

    assert contract.effective_stop_price == pytest.approx(104.5)
    assert contract.validation_evidence["stop_noise_adjusted"] is True
    assert contract.validation_evidence["stop_above_noise_floor"] is True
    assert contract.economic_risk_usdt <= 18.0 + 1e-9
    assert contract.executable is True


def test_atr_noise_floor_remains_fail_closed_when_atr_is_missing():
    contract = build_executable_opportunity(
        "SHORT", "SHORT_SUPPORT_BREAKDOWN", _prediction(),
        {"current_price": 100.0}, leverage=2, min_stop_atr_multiple=1.5,
    )

    assert contract.executable is False
    assert contract.validation_evidence["atr_available"] is False
    assert contract.validation_evidence["stop_noise_adjusted"] is False


def test_atr_noise_floor_accepts_stop_adjusted_to_exact_float_boundary():
    entry = 7.474
    contract = build_executable_opportunity(
        "SHORT", "SHORT_SUPPORT_BREAKDOWN",
        _prediction(
            position_size_usdt=1500.0,
            success_probability=0.77,
            target_price=7.17504,
            stop_price=7.62348,
        ),
        {"current_price": entry, "atr_pct": 2.2490225778379894},
        leverage=2,
        max_loss_usd=18.0,
        minimum_probability=0.55,
        min_stop_atr_multiple=1.5,
        entry_fee_bps=2.0,
        exit_fee_bps=8.0,
        slippage_bps=5.2,
    )

    assert contract.validation_evidence["stop_noise_adjusted"] is True
    assert contract.validation_evidence["stop_above_noise_floor"] is True
    assert contract.executable is True


def test_rejection_with_bullish_momentum_is_audited_without_blocking():
    contract = build_executable_opportunity(
        "SHORT", "SHORT_RESISTANCE_REJECTION", _prediction(),
        {"current_price": 100.0, "sr_rejection": True, "graph_momentum_5": 0.01},
        leverage=2,
    )
    assert contract.executable
    assert contract.rejection_evidence["confirmed"] is False


def test_contract_can_scale_down_but_never_above_locked_margin():
    contract = build_executable_opportunity("SHORT", "SHORT_SUPPORT_BREAKDOWN", _prediction(),
                                            {"current_price": 100.0}, leverage=2).to_dict()
    scaled = scale_contract(contract, 40)
    assert scaled["margin_usdt"] == 40
    assert scaled["notional_usdt"] == 80
    with pytest.raises(ValueError):
        scale_contract(contract, 100.01)


def test_contract_uses_injected_exchange_filters_before_execution():
    contract = build_executable_opportunity(
        "LONG",
        "LONG_RESISTANCE_BREAKOUT",
        _prediction(position_size_usdt=15.0, target_price=105.0, stop_price=98.0),
        {"current_price": 100.0, "atr_pct": 2.0},
        trade_type="spot",
        exchange_filters={
            "min_qty": 0.001,
            "max_qty": 100.0,
            "market_step": 0.01,
            "market_min_qty": 0.001,
            "market_max_qty": 100.0,
            "min_notional": 20.0,
            "tick_size": 0.01,
            "exchange_filters_validated": True,
            "filter_snapshot": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"}],
            "filter_snapshot_sha256": "a" * 64,
            "filter_snapshot_retrieved_at": "2026-09-30T12:00:00+00:00",
            "filter_snapshot_venue": "spot_demo",
            "filter_snapshot_symbol": "BTCUSDT",
            "filter_snapshot_source": "BinanceClient.get_symbol_filters",
        },
    )

    assert contract.executable is False
    assert contract.validation_evidence["exchange_filters_available"] is True
    assert contract.validation_evidence["exchange_filters_validated"] is True
    assert contract.validation_evidence["exchange_min_notional"] == 20.0
    assert contract.validation_evidence["exchange_filter_snapshot_sha256"] == "a" * 64
    assert contract.validation_evidence["exchange_filter_snapshot_retrieved_at"] == "2026-09-30T12:00:00+00:00"
    assert contract.validation_evidence["exchange_filter_snapshot_venue"] == "spot_demo"
    assert contract.validation_evidence["exchange_filter_snapshot_symbol"] == "BTCUSDT"


def test_contract_rounds_market_quantity_down_before_estimating_risk():
    contract = build_executable_opportunity(
        "LONG",
        "LONG_RESISTANCE_BREAKOUT",
        _prediction(position_size_usdt=100.09, success_probability=0.90, target_price=105.0, stop_price=98.0),
        {"current_price": 100.0, "atr_pct": 2.0},
        trade_type="spot",
        exchange_filters={
            "min_qty": 0.03,
            "max_qty": 100.0,
            "market_step": 0.03,
            "market_min_qty": 0.03,
            "market_max_qty": 100.0,
            "min_notional": 5.0,
            "tick_size": 0.01,
            "exchange_filters_validated": True,
        },
    )

    assert contract.executable is True
    assert contract.qty == pytest.approx(0.99)
    assert contract.notional_usdt == pytest.approx(99.0)
    assert contract.margin_usdt == pytest.approx(99.0)
    assert contract.validation_evidence["quantity_rounded_down_to_market_step"] is True
    assert contract.economic_risk_usdt <= (100.09 * 0.02 + 2.0)


@pytest.mark.parametrize("exchange_filters", [{}, {"min_qty": 0.001}, {"min_notional": 5.0, "exchange_filters_validated": False}])
def test_contract_fails_closed_when_explicit_exchange_filters_are_incomplete(exchange_filters):
    contract = build_executable_opportunity(
        "LONG",
        "LONG_RESISTANCE_BREAKOUT",
        _prediction(position_size_usdt=100.0, target_price=105.0, stop_price=98.0),
        {"current_price": 100.0, "atr_pct": 2.0},
        trade_type="spot",
        exchange_filters=exchange_filters,
    )

    assert contract.executable is False
    assert contract.validation_evidence["exchange_filters_validated"] is False
    assert contract.validation_evidence["exchange_min_notional"] == float("inf")


def test_contract_distance_multipliers_scale_registered_geometry():
    contract = build_executable_opportunity(
        "SHORT",
        "SHORT_SUPPORT_BREAKDOWN",
        _prediction(target_price=96.0, stop_price=102.0),
        {"current_price": 100.0},
        sl_multiplier=1.5,
        tp_multiplier=0.5,
    )

    assert contract.effective_target_price == pytest.approx(98.0)
    assert contract.effective_stop_price == pytest.approx(103.0)
