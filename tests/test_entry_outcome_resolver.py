from simulation.entry_outcome_resolver import resolve_bar_exit, resolve_contract_path
from simulation.mimic_clients import (
    SimulationClock,
    SimulationFuturesClient,
    SimulationLeveragedPosition,
)
import pandas as pd


def test_same_bar_stop_and_target_is_stop_first_and_marked_ambiguous():
    result = resolve_bar_exit(
        "LONG",
        {"open": 100.0, "high": 106.0, "low": 94.0, "close": 101.0},
        stop_price=95.0,
        target_price=105.0,
    )

    assert result.reason == "SL"
    assert result.exit_price == 95.0
    assert result.ambiguous_intrabar is True


def test_gap_through_stop_fills_at_bar_open():
    result = resolve_bar_exit(
        "LONG",
        {"open": 90.0, "high": 94.0, "low": 88.0, "close": 92.0},
        stop_price=95.0,
        target_price=105.0,
    )

    assert result.reason == "SL"
    assert result.exit_price == 90.0
    assert result.gap_through is True


def test_open_gap_to_target_precedes_later_intrabar_stop():
    result = resolve_bar_exit(
        "LONG",
        {"open": 106.0, "high": 108.0, "low": 94.0, "close": 100.0},
        stop_price=95.0,
        target_price=105.0,
    )

    assert result.reason == "TP"
    assert result.exit_price == 105.0
    assert result.gap_through is True
    assert result.ambiguous_intrabar is False


def test_fixed_horizon_expiry_applies_costs_and_funding_once():
    bars = [
        {"open_time": 60 * index, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}
        for index in range(4)
    ]
    result = resolve_contract_path(
        bars,
        side="LONG",
        entry_time=0,
        entry_price=100.0,
        stop_price=95.0,
        target_price=105.0,
        horizon_minutes=4,
        quantity=2.0,
        entry_fee_bps=10.0,
        exit_fee_bps=10.0,
        slippage_bps=5.0,
        funding_cost_usdt=0.01,
    )

    assert result["outcome"] == "EXPIRY"
    assert result["exit_time"] == 240
    assert result["gross_pnl_usdt"] == 1.0
    assert result["entry_fee_usdt"] == 0.2
    assert result["exit_fee_usdt"] == 0.201
    assert result["funding_cost_usdt"] == 0.01
    assert result["net_pnl_usdt"] < 1.0


def test_fixed_horizon_censors_missing_bars_instead_of_fabricating_expiry():
    bars = [
        {"open_time": 0, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
        {"open_time": 120, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
    ]

    result = resolve_contract_path(
        bars,
        side="SHORT",
        entry_time=0,
        entry_price=100.0,
        stop_price=105.0,
        target_price=95.0,
        horizon_minutes=3,
    )

    assert result["label_valid"] is False
    assert result["outcome"] == "DATA_GAP"
    assert result["missing_bar_time"] == 60


def test_ambiguous_sensitivity_mode_does_not_assign_a_winner():
    result = resolve_contract_path(
        [{"open_time": 0, "open": 100.0, "high": 106.0, "low": 94.0, "close": 100.0}],
        side="SHORT",
        entry_time=0,
        entry_price=100.0,
        stop_price=105.0,
        target_price=95.0,
        horizon_minutes=1,
        ambiguous_policy="mark_ambiguous",
    )

    assert result["outcome"] == "AMBIGUOUS"
    assert result["label_valid"] is False


def test_fixed_horizon_censors_duplicate_timestamps():
    duplicate_bars = [
        {"open_time": 0, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
        {"open_time": 0, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
    ]

    result = resolve_contract_path(
        duplicate_bars,
        side="LONG",
        entry_time=0,
        entry_price=100.0,
        stop_price=95.0,
        target_price=105.0,
        horizon_minutes=1,
    )

    assert result["outcome"] == "DATA_GAP"
    assert result["reason"] == "DUPLICATE_BAR_TIMESTAMP"


def test_simulation_futures_client_uses_shared_gap_through_resolution():
    now = pd.Timestamp("2026-01-01T01:00:00Z").to_pydatetime()
    bars = [{
        "open_time": now.timestamp(), "open": 90.0, "high": 94.0,
        "low": 88.0, "close": 92.0,
    }]
    client = SimulationFuturesClient(SimulationClock(now), {"BTCUSDT": bars}, commission_rate=0.0)
    position = SimulationLeveragedPosition(
        symbol="BTCUSDT",
        side="LONG",
        qty=1.0,
        entry_price=100.0,
        sl_price=95.0,
        tp_price=105.0,
        order_id="fixture-order",
        margin_usdt=50.0,
        opened_at=now.timestamp() - 60,
    )
    client._available_usdt = 500.0
    client._open_positions["BTCUSDT_LONG"] = position

    closes = client.check_positions(settings=None)

    assert closes[0]["reason"] == "SL"
    assert closes[0]["exit_price"] == 90.0
    assert closes[0]["gap_through"] is True
    assert position.closed is True