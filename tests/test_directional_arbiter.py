from types import SimpleNamespace
from unittest.mock import patch

from directional_arbiter import evaluate_range_scalp
from run_trades import _prepare_range_scalp_staging_candidates


def _settings(**overrides):
    values = {
        "range_scalp_direction_mode": "short_priority",
        "range_scalp_direction_min_probability": 0.55,
        "range_scalp_short_min_probability": 0.40,
        "range_scalp_short_priority_bonus": 0.05,
        "range_scalp_long_superiority_delta": 0.10,
        "range_scalp_long_min_probability": 0.55,
        "range_scalp_long_min_expected_pnl": 0.0,
        "range_scalp_long_support_zone_max": 0.55,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _prediction(expected_pnl=5.0, probability=0.65, position_size=100.0):
    return {
        "expected_pnl": expected_pnl,
        "success_probability": probability,
        "position_size_usdt": position_size,
    }


def test_short_at_resistance_wins_even_when_long_has_higher_ev():
    result = evaluate_range_scalp(
        "BTCUSDT",
        {"sr_zone_position": 0.65},
        _prediction(50.0, 0.90),
        _prediction(40.0, 0.85),
        _prediction(5.0, 0.65),
        True,
        _settings(),
    )
    assert result.selected_mode == "FUTURES_SHORT"
    assert result.reason == "SHORT_RESISTANCE_PRIORITY"


def test_short_uses_dedicated_lower_probability_threshold():
    result = evaluate_range_scalp(
        "BTCUSDT",
        {"sr_zone_position": 0.65},
        _prediction(5.0, 0.54),
        _prediction(5.0, 0.54),
        _prediction(5.0, 0.45),
        True,
        _settings(),
    )
    assert result.selected_mode == "FUTURES_SHORT"
    assert result.reason == "SHORT_RESISTANCE_PRIORITY"
    assert result.short_eligible is True
    assert result.long_eligible is False


def test_long_requires_confirmed_support_or_breakout():
    result = evaluate_range_scalp(
        "BTCUSDT",
        {"sr_zone_position": 0.1, "support_bounce_buy": True, "trend_4h": "UP"},
        _prediction(20.0, 0.80),
        _prediction(18.0, 0.78),
        _prediction(1.0, 0.56),
        True,
        _settings(),
    )
    assert result.selected_mode == "SPOT_LONG"
    assert result.reason == "LONG_SUPPORT_OR_BREAKOUT_SUPERIOR"


def test_long_structure_abstains_when_long_entries_are_disabled():
    result = evaluate_range_scalp(
        "BTCUSDT",
        {"sr_zone_position": 0.1, "support_bounce_buy": True, "trend_4h": "UP"},
        _prediction(20.0, 0.80),
        _prediction(18.0, 0.78),
        _prediction(1.0, 0.56),
        True,
        _settings(range_scalp_long_entries_enabled=False),
    )
    assert result.selected_mode == "ABSTAIN"
    assert result.reason == "RANGE_SCALP_LONG_DISABLED"


def test_futures_long_analysis_intent_never_routes_to_spot_long():
    result = evaluate_range_scalp(
        "BTCUSDT",
        {
            "sr_zone_position": 0.1,
            "support_bounce_buy": True,
            "routing_intent": "FUTURES_LONG_ANALYSIS",
        },
        _prediction(20.0, 0.80),
        _prediction(18.0, 0.78),
        _prediction(-1.0, 0.20),
        True,
        _settings(range_scalp_long_entries_enabled=True),
    )
    assert result.selected_mode == "FUTURES_LONG"
    assert result.reason == "LONG_SUPPORT_OR_BREAKOUT_SUPERIOR"


def test_long_structure_abstains_when_not_superior_to_short():
    result = evaluate_range_scalp(
        "BTCUSDT",
        {"sr_zone_position": 0.20, "support_bounce_buy": True},
        _prediction(5.0, 0.60),
        _prediction(4.5, 0.60),
        _prediction(8.0, 0.70),
        True,
        _settings(range_scalp_long_superiority_delta=0.10),
    )
    assert result.selected_mode == "ABSTAIN"
    assert result.reason == "LONG_NOT_SUPERIOR_TO_SHORT"


def test_long_structure_abstains_when_above_support_zone_limit():
    result = evaluate_range_scalp(
        "BTCUSDT",
        {"sr_zone_position": 0.72, "support_bounce_buy": True, "trend_4h": "UP"},
        _prediction(20.0, 0.85),
        _prediction(18.0, 0.82),
        _prediction(2.0, 0.45),
        True,
        _settings(range_scalp_short_first_enabled=False),
    )
    assert result.selected_mode == "ABSTAIN"
    assert result.reason == "LONG_STRUCTURE_SHORT_BLOCKED"


def test_short_wins_in_middle_range_even_when_long_has_higher_ev():
    result = evaluate_range_scalp(
        "BTCUSDT",
        {"sr_zone_position": 0.5},
        _prediction(50.0, 0.90),
        _prediction(40.0, 0.85),
        _prediction(1.0, 0.56),
        True,
        _settings(),
    )
    assert result.selected_mode == "FUTURES_SHORT"
    assert result.reason == "SHORT_GEOMETRY_PRIORITY"


def test_long_structure_abstains_when_long_is_ineligible():
    result = evaluate_range_scalp(
        "BTCUSDT",
        {"sr_zone_position": 0.1, "support_bounce_buy": True},
        _prediction(-1.0, 0.40),
        _prediction(-1.0, 0.40),
        _prediction(5.0, 0.65),
        True,
        _settings(),
    )
    assert result.selected_mode == "ABSTAIN"
    assert result.reason == "LONG_STRUCTURE_SHORT_BLOCKED"


def test_symbol_outside_triple_universe_abstains_even_with_long_edge():
    result = evaluate_range_scalp(
        "QIUSDT",
        {"sr_zone_position": 0.05, "support_bounce_buy": True},
        _prediction(20.0, 0.90),
        None,
        None,
        False,
        _settings(),
    )
    assert result.selected_mode == "ABSTAIN"
    assert result.reason == "TRIPLE_UNIVERSE_REQUIRED"


def test_short_breakdown_receives_priority():
    result = evaluate_range_scalp(
        "ETHUSDT",
        {"sr_zone_position": 0.3, "support_breakdown_sell": True, "trend_4h": "DOWN"},
        _prediction(8.0, 0.68),
        _prediction(8.0, 0.68),
        _prediction(5.0, 0.65),
        True,
        _settings(),
    )
    assert result.selected_mode == "FUTURES_SHORT"
    assert result.reason == "SHORT_RESISTANCE_PRIORITY"
    assert result.short_structure is True
    assert result.long_structure is False


def test_resistance_rejection_receives_priority_even_below_zone_threshold():
    """resistance_bounce_sell/sr_rejection (confirmed reversal candle near
    resistance) must qualify as a real resistance setup on its own, the same
    way support_breakdown_sell already does — not just the static zone
    threshold. Zone here (0.5) is below short_resistance_threshold (0.65),
    so without the rejection event this would fall through to
    SHORT_GEOMETRY_PRIORITY instead."""
    result = evaluate_range_scalp(
        "BTCUSDT",
        {"sr_zone_position": 0.5, "resistance_bounce_sell": True, "trend_4h": "DOWN"},
        _prediction(8.0, 0.68),
        _prediction(8.0, 0.68),
        _prediction(5.0, 0.65),
        True,
        _settings(),
    )
    assert result.selected_mode == "FUTURES_SHORT"
    assert result.reason == "SHORT_RESISTANCE_PRIORITY"


def test_both_ineligible_abstains():
    result = evaluate_range_scalp(
        "SOLUSDT",
        {},
        _prediction(-1.0, 0.40),
        _prediction(-1.0, 0.40),
        _prediction(-1.0, 0.40),
        True,
        _settings(),
    )
    assert result.selected_mode == "ABSTAIN"
    assert result.reason == "SHORT_GEOMETRY_NOT_ELIGIBLE"


def test_staging_uses_arbitrated_side_and_prediction():
    decision = {
        "selected_mode": "FUTURES_SHORT",
        "reason": "SHORT_PRIORITY",
        "short_prediction": _prediction(7.0, 0.72),
        "long_prediction": _prediction(5.0, 0.65),
        "futures_long_prediction": _prediction(4.0, 0.62),
    }
    candidate = {"symbol": "BTCUSDT", "signal_id": "s1", "strength": 0.8}
    with patch("run_trades._range_scalp_direction_decision", return_value=decision):
        signals, predictions, metadata = _prepare_range_scalp_staging_candidates(
            [candidate],
            {"BTCUSDT": _prediction(5.0, 0.65)},
            {"BTCUSDT": candidate},
            "RANGE_SCALP",
            object(),
            _settings(range_scalp_short_first_enabled=True),
            {"futures": ["BTCUSDT"]},
        )
    assert signals[0]["expected_side"] == "SHORT"
    assert signals[0]["trade_type"] == "futures"
    assert metadata["BTCUSDT"]["directional_arbitration"]["selected_mode"] == "FUTURES_SHORT"
    assert predictions["BTCUSDT"] == decision["short_prediction"]


def test_staging_drops_arbitrated_abstain():
    decision = {"selected_mode": "ABSTAIN", "reason": "NO_ELIGIBLE_DIRECTION"}
    candidate = {"symbol": "BTCUSDT", "signal_id": "s1"}
    with patch("run_trades._range_scalp_direction_decision", return_value=decision), patch(
        "run_trades.update_signal_decision"
    ) as update:
        signals, predictions, metadata = _prepare_range_scalp_staging_candidates(
            [candidate],
            {"BTCUSDT": _prediction()},
            {"BTCUSDT": candidate},
            "RANGE_SCALP",
            object(),
            _settings(range_scalp_short_first_enabled=True),
            {"futures": ["BTCUSDT"]},
        )
    assert signals == []
    assert predictions == {}
    assert metadata == {}
    update.assert_called_once()


# ── s116: symmetric LONG/SHORT mode tests ──────────────────────────────────

def _symmetric_settings(**overrides):
    return _settings(
        range_scalp_direction_mode="ev_symmetric",
        range_scalp_short_priority_bonus=0.0,
        range_scalp_long_superiority_delta=0.0,
        range_scalp_long_min_probability=0.50,
        range_scalp_short_min_probability=0.50,
        **overrides,
    )


def test_symmetric_long_wins_near_support():
    result = evaluate_range_scalp(
        "BTCUSDT",
        {"sr_zone_position": 0.15, "support_bounce_buy": True, "trend_4h": "UP"},
        _prediction(15.0, 0.70),  # spot long
        _prediction(16.0, 0.72),  # futures long (preferred when score >= spot)
        _prediction(5.0, 0.60),  # short
        True,
        _symmetric_settings(),
    )
    assert result.selected_mode == "FUTURES_LONG"
    assert result.reason == "SYMMETRIC_LONG_SETUP"


def test_symmetric_short_wins_near_resistance():
    result = evaluate_range_scalp(
        "BTCUSDT",
        {"sr_zone_position": 0.85, "resistance_bounce_sell": True, "trend_4h": "DOWN"},
        _prediction(5.0, 0.60),
        _prediction(5.0, 0.60),
        _prediction(15.0, 0.70),
        True,
        _symmetric_settings(),
    )
    assert result.selected_mode == "FUTURES_SHORT"
    assert result.reason == "SYMMETRIC_SHORT_SETUP"


def test_symmetric_abstains_in_middle_zone():
    result = evaluate_range_scalp(
        "BTCUSDT",
        {"sr_zone_position": 0.6},  # outside support (0.55) and resistance (0.65) zones
        _prediction(15.0, 0.70),
        _prediction(14.0, 0.68),
        _prediction(15.0, 0.70),
        True,
        _symmetric_settings(),
    )
    assert result.selected_mode == "ABSTAIN"


def test_symmetric_tie_prefers_higher_ev():
    result = evaluate_range_scalp(
        "BTCUSDT",
        {
            "sr_zone_position": 0.5,
            "support_bounce_buy": True,
            "resistance_bounce_sell": True,
        },
        _prediction(15.0, 0.80),
        _prediction(14.0, 0.78),
        _prediction(5.0, 0.70),
        True,
        _symmetric_settings(),
    )
    assert result.selected_mode in ("FUTURES_LONG", "SPOT_LONG")
    assert result.reason == "SYMMETRIC_LONG_WINS_TIE"


def test_symmetric_long_uses_spot_when_futures_not_executable():
    result = evaluate_range_scalp(
        "QIUSDT",
        {"sr_zone_position": 0.10, "support_bounce_buy": True},
        _prediction(15.0, 0.70),
        _prediction(14.0, 0.68),
        _prediction(5.0, 0.70),
        False,
        _symmetric_settings(),
    )
    assert result.selected_mode == "ABSTAIN"
    assert result.reason == "TRIPLE_UNIVERSE_REQUIRED"


def test_symmetric_defaults_to_ev_symmetric_in_code():
    # Guardrail: the factory default in config.py/config_helpers.py is ev_symmetric.
    from config_helpers import _PARAM_DEFAULTS
    assert _PARAM_DEFAULTS.get("range_scalp_direction_mode") == "ev_symmetric"
