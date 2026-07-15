from autonomous_signals import AutonomousSignalGenerator


class _DummyClient:
    client = None


def _generator_with_signals(signals):
    gen = AutonomousSignalGenerator(_DummyClient(), symbols=[s.get("symbol", "X") for s in signals])

    def _fake_generate_signals(interval='1h', use_multi_timeframe=True, news_client=None):
        return list(signals)

    gen.generate_signals = _fake_generate_signals
    return gen


def test_spot_bull_prefers_strength_and_uptrend():
    signals = [
        {"symbol": "A", "signal": "BUY", "strength": 0.70, "higher_tf_trend": "FLAT", "vol_spike": 1.0},
        {"symbol": "B", "signal": "BUY", "strength": 0.66, "higher_tf_trend": "UP", "vol_spike": 1.3},
    ]
    gen = _generator_with_signals(signals)
    top = gen.get_top_buy_signals(regime_mode="SPOT_BULL", min_strength=0.5, limit=2)
    assert top[0]["symbol"] == "A"


def test_range_scalp_prioritizes_bb_touch_then_low_rsi():
    signals = [
        {"symbol": "LOW_RSI_NO_TOUCH", "signal": "BUY", "strength": 0.61, "rsi": 28, "bb_lower_touch": False, "bb_near_lower": False, "vol_spike": 1.1},
        {"symbol": "TOUCH_MED_RSI", "signal": "BUY", "strength": 0.55, "rsi": 45, "bb_lower_touch": True, "bb_near_lower": False, "vol_spike": 1.0},
    ]
    gen = _generator_with_signals(signals)
    top = gen.get_top_buy_signals(regime_mode="RANGE_SCALP", min_strength=0.5, limit=2)
    assert top[0]["symbol"] == "TOUCH_MED_RSI"


def test_preservation_prefers_confirmed_and_balanced_signal():
    signals = [
        {"symbol": "HIGH_BUT_WEAK_QUALITY", "signal": "BUY", "strength": 0.70, "rsi": 80, "higher_tf_trend": "DOWN", "volume_confirmed": False, "vol_spike": 1.0},
        {"symbol": "BALANCED_CONFIRMED", "signal": "BUY", "strength": 0.64, "rsi": 52, "higher_tf_trend": "UP", "volume_confirmed": True, "vol_spike": 1.2},
    ]
    gen = _generator_with_signals(signals)
    top = gen.get_top_buy_signals(regime_mode="PRESERVATION", min_strength=0.5, limit=2)
    assert top[0]["symbol"] == "BALANCED_CONFIRMED"


def test_futures_short_includes_sell_and_prioritizes_overbought():
    signals = [
        {"symbol": "BUY_MID", "signal": "BUY", "strength": 0.58, "rsi": 58, "vol_spike": 1.0},
        {"symbol": "SELL_OB", "signal": "SELL", "strength": 0.45, "rsi": 70, "vol_spike": 1.3},
    ]
    gen = _generator_with_signals(signals)
    top = gen.get_top_buy_signals(regime_mode="FUTURES_SHORT", min_strength=0.55, limit=2)
    assert top[0]["symbol"] == "SELL_OB"
