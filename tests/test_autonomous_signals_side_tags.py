from autonomous_signals import AutonomousSignalGenerator


class _DummyClient:
    pass


def test_futures_short_candidates_are_tagged_short(monkeypatch):
    gen = AutonomousSignalGenerator(_DummyClient(), symbols=[])

    sample_signals = [
        {"symbol": "BTCUSDT", "signal": "SELL", "strength": 0.55, "rsi": 65, "vol_spike": 1.4},
        {"symbol": "ETHUSDT", "signal": "HOLD", "strength": 0.46, "rsi": 61, "vol_spike": 1.2},
    ]

    monkeypatch.setattr(gen, "generate_signals", lambda *args, **kwargs: sample_signals)

    out = gen.get_top_buy_signals(regime_mode="FUTURES_SHORT", min_strength=0.6, limit=5)
    assert out
    assert all(s.get("expected_side") == "SHORT" for s in out)
    assert all(s.get("trade_type") == "futures" for s in out)


def test_spot_candidates_are_tagged_long(monkeypatch):
    gen = AutonomousSignalGenerator(_DummyClient(), symbols=[])

    sample_signals = [
        {"symbol": "SOLUSDT", "signal": "BUY", "strength": 0.72, "rsi": 48, "vol_spike": 1.3},
        {"symbol": "BNBUSDT", "signal": "BUY", "strength": 0.68, "rsi": 51, "vol_spike": 1.1},
    ]

    monkeypatch.setattr(gen, "generate_signals", lambda *args, **kwargs: sample_signals)

    out = gen.get_top_buy_signals(regime_mode="SPOT_BULL", min_strength=0.6, limit=5)
    assert out
    assert all(s.get("expected_side") == "LONG" for s in out)
    assert all(s.get("trade_type") == "spot" for s in out)


def test_spot_bull_allows_short_side_when_resistance_is_clear(monkeypatch):
    gen = AutonomousSignalGenerator(_DummyClient(), symbols=[])
    sample_signals = [{
        "symbol": "DOGEUSDT",
        "signal": "SELL",
        "strength": 0.74,
        "rsi": 68,
        "bb_near_upper": True,
        "sr_zone_position": 0.82,
        "vol_spike": 1.7,
    }]
    monkeypatch.setattr(gen, "generate_signals", lambda *args, **kwargs: sample_signals)

    out = gen.get_top_buy_signals(regime_mode="SPOT_BULL", min_strength=0.6, limit=5)
    assert out
    assert out[0]["expected_side"] == "SHORT"
    assert out[0]["trade_type"] == "futures"


def test_futures_short_allows_long_side_when_support_is_clear(monkeypatch):
    gen = AutonomousSignalGenerator(_DummyClient(), symbols=[])
    sample_signals = [{
        "symbol": "APTUSDT",
        "signal": "BUY",
        "strength": 0.75,
        "rsi": 32,
        "bb_lower_touch": True,
        "support_bounce_buy": True,
        "sr_zone_position": 0.18,
        "vol_spike": 1.5,
    }]
    monkeypatch.setattr(gen, "generate_signals", lambda *args, **kwargs: sample_signals)

    out = gen.get_top_buy_signals(regime_mode="FUTURES_SHORT", min_strength=0.6, limit=5)
    assert out
    assert out[0]["expected_side"] == "LONG"
    assert out[0]["trade_type"] == "spot"
