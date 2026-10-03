import pytest

from close_execution import build_close_plan, simulate_close_book


def test_long_close_consumes_bid_and_calculates_vwap():
    simulation = simulate_close_book(
        "LONG", 10.0,
        bids=[[100.0, 4.0], [99.0, 10.0]],
        asks=[[100.1, 20.0]],
    )

    assert simulation.side == "SELL"
    assert simulation.sufficient_depth is True
    assert simulation.expected_vwap == pytest.approx(99.4)
    assert simulation.levels_consumed == 2


def test_short_close_consumes_ask_and_calculates_vwap():
    simulation = simulate_close_book(
        "SHORT", 10.0,
        bids=[[99.9, 20.0]],
        asks=[[100.0, 4.0], [101.0, 10.0]],
    )

    assert simulation.side == "BUY"
    assert simulation.sufficient_depth is True
    assert simulation.expected_vwap == pytest.approx(100.6)
    assert simulation.levels_consumed == 2


def test_soft_close_uses_full_size_fok_within_limit():
    plan = build_close_plan(
        "BTCUSDT", "SHORT", 10.0, "SOFT_PROFIT",
        bids=[[99.9, 20.0]], asks=[[100.0, 20.0]],
        max_slippage_bps=1.0,
    )

    assert plan.order_type == "LIMIT_FOK"
    assert plan.full_quantity == 10.0
    assert plan.order_side == "BUY"


def test_hard_risk_uses_market_even_with_good_book():
    plan = build_close_plan(
        "BTCUSDT", "LONG", 10.0, "HARD_RISK",
        bids=[[100.0, 20.0]], asks=[[100.1, 20.0]],
        max_slippage_bps=1.0,
    )

    assert plan.order_type == "MARKET"
    assert plan.reason == "hard_risk_immediate"


def test_insufficient_depth_falls_back_to_market_without_partial_quantity():
    plan = build_close_plan(
        "OPUSDT", "SHORT", 100.0, "SOFT_PROFIT",
        bids=[[0.096, 10.0]], asks=[[0.0962, 10.0]],
    )

    assert plan.order_type == "MARKET"
    assert plan.sufficient_depth is False
    assert plan.full_quantity == 100.0
    assert plan.book["filled_quantity"] == 10.0


def test_invalid_inputs_are_rejected():
    with pytest.raises(ValueError):
        simulate_close_book("SIDEWAYS", 1.0)
    with pytest.raises(ValueError):
        build_close_plan("BTCUSDT", "LONG", 0.0, "SOFT_PROFIT")
    with pytest.raises(ValueError):
        build_close_plan("BTCUSDT", "LONG", 1.0, "UNKNOWN")
