from Dev.scripts.analyze_universal_entry_early_profit_opportunities import (
    _level_distance_pct,
    _replay_threshold_exit,
    _tighter_target_price,
    measure_mfe_bounds,
)


def test_level_distance_pct_is_symmetric_for_long_and_short_contracts():
    assert _level_distance_pct(100.0, 95.0) == 5.0
    assert _level_distance_pct(100.0, 105.0) == 5.0


def test_long_stop_bar_has_conservative_and_optimistic_mfe_bounds():
    bars = [
        {"open_time": 0, "open": 100.0, "high": 100.8, "low": 99.0, "close": 100.2},
        {"open_time": 60, "open": 100.2, "high": 102.0, "low": 94.0, "close": 95.0},
    ]

    lower, upper = measure_mfe_bounds(
        bars, side="LONG", entry_time=0, exit_time=60,
        entry_price=100.0, stop_price=95.0, exit_price=95.0,
        outcome="SL", gap_through=False,
    )

    assert round(lower, 4) == 0.8
    assert round(upper, 4) == 2.0


def test_gap_stop_does_not_count_excursion_after_the_open_fill():
    bars = [
        {"open_time": 0, "open": 100.0, "high": 100.8, "low": 99.0, "close": 100.2},
        {"open_time": 60, "open": 94.0, "high": 102.0, "low": 93.0, "close": 95.0},
    ]

    lower, upper = measure_mfe_bounds(
        bars, side="LONG", entry_time=0, exit_time=60,
        entry_price=100.0, stop_price=95.0, exit_price=94.0,
        outcome="SL", gap_through=True,
    )

    assert round(lower, 4) == 0.8
    assert round(upper, 4) == 0.8


def test_short_stop_bar_uses_direction_correct_favorable_low():
    bars = [
        {"open_time": 0, "open": 100.0, "high": 101.0, "low": 99.2, "close": 100.1},
        {"open_time": 60, "open": 100.1, "high": 105.0, "low": 97.0, "close": 104.0},
    ]

    lower, upper = measure_mfe_bounds(
        bars, side="SHORT", entry_time=0, exit_time=60,
        entry_price=100.0, stop_price=105.0, exit_price=105.0,
        outcome="SL", gap_through=False,
    )

    assert round(lower, 4) == 0.8065
    assert round(upper, 4) == 3.0928


def test_tp_exit_caps_mfe_at_original_target():
    bars = [
        {"open_time": 0, "open": 100.0, "high": 105.0, "low": 99.0, "close": 103.0},
    ]

    lower, upper = measure_mfe_bounds(
        bars, side="LONG", entry_time=0, exit_time=0,
        entry_price=100.0, stop_price=95.0, exit_price=101.0,
        outcome="TP", gap_through=False,
    )

    assert round(lower, 4) == 1.0
    assert round(upper, 4) == 1.0


def test_threshold_policy_uses_first_of_threshold_or_original_tp_for_both_sides():
    assert _tighter_target_price("LONG", 100.0, 102.0, 1.0) == 101.0
    assert _tighter_target_price("LONG", 100.0, 102.0, 3.0) == 102.0
    assert _tighter_target_price("SHORT", 100.0, 98.0, 1.0) == 99.0
    assert _tighter_target_price("SHORT", 100.0, 98.0, 3.0) == 98.0


def test_threshold_exit_replay_keeps_original_sl_first_on_ambiguous_bar():
    result = _replay_threshold_exit(
        [{"open_time": 0, "open": 100.0, "high": 102.0, "low": 94.0, "close": 100.0}],
        side="LONG", entry_time=0, entry_price=100.0, stop_price=95.0,
        target_price=101.0, quantity=1.0, entry_fee_bps=0.0, slippage_bps=0.0,
    )

    assert result["outcome"] == "SL"
    assert result["ambiguous_intrabar"] is True
    assert result["net_pnl_usdt"] == -5.0