from pnl_predictor import PnLPredictor


def _bearish_indicators():
    return {
        "rsi": {"value": 74.0},
        "macd": {"macd": -0.5, "signal": 0.1, "histogram": -0.2},
        "sma": {"value": 101.0},
        "ema": {"value": 99.0},
        "bollinger_bands": {"lower": 95.0, "middle": 102.0, "upper": 103.0},
        "vol_spike": {"value": 1.8},
        "atr_pct": {"value": 2.2},
        "trend_4h": {"value": -1},
        "weekly_range": {"value": 0.9},
    }


def test_short_probability_exceeds_long_on_bearish_setup():
    predictor = PnLPredictor()
    indicators = _bearish_indicators()

    p_long = predictor._calculate_success_probability(indicators, trade_side="LONG")
    p_short = predictor._calculate_success_probability(indicators, trade_side="SHORT")

    assert p_short > p_long


def test_expected_pnl_uses_direction_specific_math():
    predictor = PnLPredictor()

    current = 100.0
    # Asymmetric range to make LONG/SHORT EV differ clearly.
    price_range = {"min": 92.0, "max": 104.0}
    prob = 0.70
    size = 100.0

    ev_long = predictor._calculate_expected_pnl(current, price_range, prob, size, trade_side="LONG")
    ev_short = predictor._calculate_expected_pnl(current, price_range, prob, size, trade_side="SHORT")

    assert ev_short > ev_long
    assert ev_short > 0.0


def test_bollinger_probability_uses_current_price():
    predictor = PnLPredictor()
    indicators = _bearish_indicators()

    low_band = predictor._calculate_success_probability(indicators, trade_side="LONG", current_price=96.0)
    high_band = predictor._calculate_success_probability(indicators, trade_side="LONG", current_price=102.5)

    assert low_band > high_band


def test_range_reversion_prediction_returns_net_ev():
    predictor = PnLPredictor()
    indicators = _bearish_indicators()
    indicators["rsi"] = {"value": 50.0}
    indicators["trend_4h"] = {"value": 1}
    indicators["signal_strength"] = 0.7

    prediction = predictor.predict_range_reversion(96.0, indicators, 100.0)

    assert prediction["valid"] is True
    assert prediction["model"] == "range_reversion_v1"
    assert prediction["target_price"] > 96.0
    assert prediction["stop_price"] < 96.0


def test_prediction_payload_includes_trade_side():
    predictor = PnLPredictor()
    prediction = predictor.predict_pnl(
        symbol="BTCUSDT",
        current_price=100.0,
        indicators=_bearish_indicators(),
        cycle_duration_minutes=60,
        position_size_usdt=100.0,
        trade_type="futures",
        trade_side="SHORT",
    )

    assert prediction.get("trade_side") == "SHORT"
