"""tests/test_entry_quality_gate.py — Behavior-first entry quality gate.

Validates `_check_entry_quality` and `_filter_buy_candidates` in run_trades.py.
The gate must approve only assets whose current price behavior matches the
short-term profit objective of the active regime/profile.
"""

import logging
import sys
import types

import pytest

from run_trades import (
    _check_entry_quality,
    _filter_buy_candidates,
    _filter_by_quality,
    _quality_score_for_regime,
)


class DummySettings:
    """Minimal settings stand-in for gate tests."""

    def __getattr__(self, name):
        defaults = {
            "entry_quality_min_strength": 0.40,
            "range_scalp_min_rsi": 25.0,
            "range_scalp_max_rsi": 60.0,
            "futures_min_inject_strength": 0.40,
            "entry_decision_enabled": False,
        }
        if name in defaults:
            return defaults[name]
        raise AttributeError(name)


SETTINGS = DummySettings()


def _meta(
    strength=0.60,
    rsi=45.0,
    vol_spike=1.5,
    bb_position=0.25,
    bb_lower_touch=True,
    bb_near_lower=False,
    trend_4h="UP",
    ema200_trend="UP",
):
    return {
        "TESTUSDT": {
            "strength": strength,
            "rsi": rsi,
            "vol_spike": vol_spike,
            "bb_position": bb_position,
            "bb_lower_touch": bb_lower_touch,
            "bb_near_lower": bb_near_lower,
            "higher_tf_trend": trend_4h,
            "ema200_trend": ema200_trend,
        }
    }


def test_spot_bull_approves_ideal_behavior():
    ok, reason = _check_entry_quality(
        "TESTUSDT", _meta(), SETTINGS, "SPOT_BULL", "B"
    )
    assert ok is True
    assert reason == "OK"


def test_spot_bull_rejects_down_trend():
    ok, reason = _check_entry_quality(
        "TESTUSDT", _meta(trend_4h="DOWN"), SETTINGS, "SPOT_BULL", "B"
    )
    assert ok is False
    assert "SPOT_BULL_4H_DOWN" in reason


def test_spot_bull_rejects_low_volume():
    ok, reason = _check_entry_quality(
        "TESTUSDT", _meta(vol_spike=0.3), SETTINGS, "SPOT_BULL", "B"
    )
    assert ok is False
    assert "SPOT_BULL_VOL" in reason


def test_spot_bull_allows_moderate_volume_with_tuned_floor():
    ok, reason = _check_entry_quality(
        "TESTUSDT", _meta(vol_spike=0.90, rsi=66.0), SETTINGS, "SPOT_BULL", "B"
    )
    assert ok is True
    assert reason == "OK"


def test_spot_bull_rejects_overbought():
    # vol_spike below breakout threshold (1.20) to avoid breakout bypass
    ok, reason = _check_entry_quality(
        "TESTUSDT", _meta(rsi=75.0, vol_spike=0.90), SETTINGS, "SPOT_BULL", "B"
    )
    assert ok is False
    assert "SPOT_BULL_RSI" in reason


def test_range_scalp_approves_near_lower_band():
    ok, reason = _check_entry_quality(
        "TESTUSDT",
        _meta(rsi=35.0, vol_spike=1.2, bb_position=0.20, bb_lower_touch=True, trend_4h="FLAT"),
        SETTINGS,
        "RANGE_SCALP",
        "B",
    )
    assert ok is True
    assert reason == "OK"


def test_range_scalp_rejects_high_rsi():
    ok, reason = _check_entry_quality(
        "TESTUSDT",
        _meta(rsi=75.0, bb_lower_touch=True, trend_4h="FLAT"),
        SETTINGS,
        "RANGE_SCALP",
        "B",
    )
    assert ok is False
    assert "RANGE_RSI" in reason


def test_range_scalp_rejects_not_near_lower_band():
    ok, reason = _check_entry_quality(
        "TESTUSDT",
        _meta(bb_position=0.80, bb_lower_touch=False, bb_near_lower=False, trend_4h="FLAT"),
        SETTINGS,
        "RANGE_SCALP",
        "B",
    )
    assert ok is False
    assert "RANGE_BB" in reason


def test_range_scalp_rejects_low_volume():
    ok, reason = _check_entry_quality(
        "TESTUSDT",
        _meta(vol_spike=0.2, bb_lower_touch=True, trend_4h="FLAT"),
        SETTINGS,
        "RANGE_SCALP",
        "B",
    )
    assert ok is False
    assert "RANGE_VOL" in reason


def test_preservation_rejects_without_volume():
    ok, reason = _check_entry_quality(
        "TESTUSDT", _meta(vol_spike=0.8), SETTINGS, "PRESERVATION", "B"
    )
    assert ok is False
    assert "PRESERVATION_VOL" in reason


def test_preservation_approves_with_volume():
    ok, reason = _check_entry_quality(
        "TESTUSDT", _meta(vol_spike=2.0, rsi=55.0), SETTINGS, "PRESERVATION", "B"
    )
    assert ok is True


def test_futures_short_rejects_non_downtrend():
    ok, reason = _check_entry_quality(
        "TESTUSDT", _meta(trend_4h="UP"), SETTINGS, "FUTURES_SHORT", "B"
    )
    assert ok is False
    assert "FUTURES_SHORT_TREND" in reason


def test_futures_short_rejects_oversold_rsi():
    ok, reason = _check_entry_quality(
        "TESTUSDT",
        _meta(rsi=25.0, trend_4h="DOWN", vol_spike=1.5),
        SETTINGS,
        "FUTURES_SHORT",
        "B",
    )
    assert ok is False
    assert "FUTURES_SHORT_RSI" in reason


def test_futures_short_approves_bounce_in_downtrend():
    ok, reason = _check_entry_quality(
        "TESTUSDT",
        _meta(rsi=55.0, trend_4h="DOWN", vol_spike=1.2),
        SETTINGS,
        "FUTURES_SHORT",
        "B",
    )
    assert ok is True
    assert reason == "OK"


def test_profile_a_is_more_permissive_than_c():
    meta_a = _meta(rsi=62.0, vol_spike=1.05, bb_position=0.42, bb_lower_touch=True, trend_4h="FLAT")
    ok_a, _ = _check_entry_quality("TESTUSDT", meta_a, SETTINGS, "RANGE_SCALP", "A")
    ok_c, _ = _check_entry_quality("TESTUSDT", meta_a, SETTINGS, "RANGE_SCALP", "C")
    assert ok_a is True
    assert ok_c is False


def test_filter_buy_candidates_keeps_valid_and_drops_invalid():
    meta = {
        "GOOD": {
            "strength": 0.60,
            "rsi": 45.0,
            "vol_spike": 1.5,
            "bb_position": 0.20,
            "bb_lower_touch": True,
            "higher_tf_trend": "UP",
            "ema200_trend": "UP",
        },
        "BAD": {
            "strength": 0.20,
            "rsi": 80.0,
            "vol_spike": 0.2,
            "bb_position": 0.90,
            "bb_lower_touch": False,
            "higher_tf_trend": "DOWN",
            "ema200_trend": "DOWN",
        },
    }
    candidates = ["GOOD", "BAD"]
    passed = _filter_buy_candidates(candidates, meta, SETTINGS, "SPOT_BULL", "B")
    assert passed == ["GOOD"]


def test_filter_buy_candidates_handles_dict_candidates():
    meta = {
        "GOOD": {
            "strength": 0.60,
            "rsi": 45.0,
            "vol_spike": 1.5,
            "bb_position": 0.20,
            "bb_lower_touch": True,
            "higher_tf_trend": "UP",
            "ema200_trend": "UP",
        },
    }
    candidates = [{"symbol": "GOOD", "strength": 0.60}]
    passed = _filter_buy_candidates(candidates, meta, SETTINGS, "SPOT_BULL", "B")
    assert len(passed) == 1
    assert passed[0]["symbol"] == "GOOD"
    assert passed[0]["entry_decision"]["eligible"] is True


class _FakeInnerClient:
    def __init__(self, tickers):
        self._tickers = tickers

    def get_ticker(self, symbol):
        if symbol not in self._tickers:
            raise RuntimeError("missing ticker")
        return self._tickers[symbol]


class _FakeClient:
    def __init__(self, tickers):
        self._inner = _FakeInnerClient(tickers)

    def get_client(self):
        return self._inner


def test_quality_score_for_regime_changes_with_mode():
    up = _quality_score_for_regime("SPOT_BULL", 0.03, 0.03, 0.50)
    down = _quality_score_for_regime("SPOT_BULL", 0.03, -0.03, 0.50)
    assert up > down

    rs_small = _quality_score_for_regime("RANGE_SCALP", 0.02, 0.02, 0.50)
    rs_big = _quality_score_for_regime("RANGE_SCALP", 0.06, -0.06, 0.50)
    assert rs_big > rs_small

    fs_down = _quality_score_for_regime("FUTURES_SHORT", 0.05, -0.05, 0.50)
    fs_up = _quality_score_for_regime("FUTURES_SHORT", 0.05, 0.05, 0.50)
    # FUTURES_SHORT prefers overbought bounces (positive signed) as short targets
    assert fs_up > fs_down


def test_filter_by_quality_is_mode_aware_across_four_modes():
    tickers = {
        "UPTREND": {"priceChangePercent": "6.0", "lastPrice": "10", "quoteVolume": "100000000"},
        "DOWNTREND": {"priceChangePercent": "-6.0", "lastPrice": "10", "quoteVolume": "100000000"},
        "STABLE": {"priceChangePercent": "0.4", "lastPrice": "10", "quoteVolume": "100000000"},
        "MEDIUM": {"priceChangePercent": "2.0", "lastPrice": "10", "quoteVolume": "100000000"},
    }
    symbols = ["UPTREND", "DOWNTREND", "STABLE", "MEDIUM"]
    client = _FakeClient(tickers)

    bull = _filter_by_quality(symbols, client, SETTINGS, target_count=2, regime_mode="SPOT_BULL")
    assert bull[0] == "UPTREND"

    range_scalp = _filter_by_quality(symbols, client, SETTINGS, target_count=2, regime_mode="RANGE_SCALP")
    assert range_scalp[0] in {"UPTREND", "DOWNTREND"}
    assert "DOWNTREND" in range_scalp

    preservation = _filter_by_quality(symbols, client, SETTINGS, target_count=2, regime_mode="PRESERVATION")
    assert preservation[0] == "STABLE"

    futures_short = _filter_by_quality(symbols, client, SETTINGS, target_count=2, regime_mode="FUTURES_SHORT")
    assert futures_short[0] == "UPTREND"


def test_quality_model_load_failure_is_cached(monkeypatch, tmp_path, caplog):
    import entry_profile_gate

    model_path = tmp_path / "quality-score.pkl"
    model_path.touch()
    calls = []

    def _fail_load(path):
        calls.append(path)
        raise RuntimeError("unavailable model")

    monkeypatch.setattr(entry_profile_gate, "_QUALITY_MODEL_PATH", model_path)
    monkeypatch.setattr(entry_profile_gate, "_model_cache", None)
    monkeypatch.setattr(entry_profile_gate, "_model_load_attempted", False)
    monkeypatch.setitem(sys.modules, "joblib", types.SimpleNamespace(load=_fail_load))

    with caplog.at_level(logging.WARNING, logger="entry_profile_gate"):
        assert entry_profile_gate._load_quality_score_model() is None
        assert entry_profile_gate._load_quality_score_model() is None

    assert len(calls) == 1
    assert caplog.messages.count("Could not load quality score model: unavailable model") == 1


def test_quality_model_artifact_loads():
    import entry_profile_gate

    entry_profile_gate._model_cache = None
    entry_profile_gate._model_load_attempted = False
    model = entry_profile_gate._load_quality_score_model()

    assert model is not None
    assert hasattr(model, "predict")


def test_entry_profile_gate_dynamic_fg_adjustment():
    """Extreme fear lowers C threshold; extreme greed raises it, keeping A/B fixed."""
    import entry_profile_gate

    # A score that is exactly the C threshold should benefit from fear and be hurt by greed.
    base_c = entry_profile_gate.load_thresholds()["thresholds"]["SPOT_BULL"]["C"]
    base_a = entry_profile_gate.load_thresholds()["thresholds"]["SPOT_BULL"]["A"]
    meta = {"expected_pnl_percentage": base_c, "position_size_usdt": 100.0}

    profile_fear, info_fear = entry_profile_gate.classify(
        meta, "SPOT_BULL", symbol="TESTUSDT", fear_greed_value=15
    )
    profile_greed, info_greed = entry_profile_gate.classify(
        meta, "SPOT_BULL", symbol="TESTUSDT", fear_greed_value=80
    )

    assert profile_fear == "C"
    assert info_fear["thresholds"]["C"] < base_c
    assert info_fear["thresholds"]["A"] == base_a
    assert profile_greed is None
    assert info_greed["thresholds"]["C"] > base_c
