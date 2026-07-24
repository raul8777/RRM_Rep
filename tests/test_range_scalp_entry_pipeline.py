import json
from types import SimpleNamespace

import run_trades
import staged_entry


def _settings():
    return SimpleNamespace(
        entry_decision_enabled=True,
        entry_decision_policy_version="strict_v1",
        entry_decision_regime_mode={"RANGE_SCALP": "enforced"},
        entry_decision_min_expected_pnl={"RANGE_SCALP": 0.0},
        entry_decision_min_probability={"RANGE_SCALP": 0.60},
        range_reversion_predictor_mode="blocking",
        take_profit_pct=1.8,
        range_scalp_sl_pct=2.5,
    )


def _range_meta():
    return {
        "symbol": "TESTUSDT",
        "trade_type": "spot",
        "current_price": 96.0,
        "strength": 0.70,
        "rsi": 50.0,
        "vol_spike": 1.8,
        "higher_tf_trend": "UP",
        "bb_lower": 95.0,
        "bb_upper": 103.0,
        "peak_quality_profile": "A",
        "peak_quality_score": 2.0,
    }


def test_shared_range_reversion_builder_normalizes_signal_metadata():
    prediction = staged_entry.build_range_reversion_prediction(
        _range_meta(), 96.0, _settings()
    )

    assert prediction["valid"] is True
    assert prediction["model"] == "range_reversion_v1"
    assert prediction["expected_pnl"] > 0.0
    assert prediction["success_probability"] >= 0.60
    assert prediction["target_price"] > 96.0
    assert prediction["stop_price"] < 96.0


def test_shared_range_reversion_builder_records_missing_bollinger_reason():
    meta = _range_meta()
    meta.pop("bb_lower")
    meta.pop("bb_upper")

    prediction = staged_entry.build_range_reversion_prediction(meta, 96.0, _settings())

    assert prediction == {"valid": False, "reason": "missing_bollinger_bands"}


def test_direct_filter_attaches_valid_range_reversion_before_entry_decision(monkeypatch):
    settings = _settings()
    candidate = _range_meta()
    signal_meta = {"TESTUSDT": dict(candidate)}
    monkeypatch.setattr(run_trades, "_db_client_ref", None)
    monkeypatch.setattr(run_trades, "update_signal_decision", lambda *args, **kwargs: None)
    run_trades._selection_begin_cycle(None, "RANGE_SCALP", "A")

    passed = run_trades._filter_buy_candidates(
        [candidate], signal_meta, settings, "RANGE_SCALP", "A", predictions={}
    )

    assert passed == [candidate]
    assert candidate["range_reversion_prediction"]["valid"] is True
    assert candidate["entry_decision"]["eligible"] is True
    assert candidate["entry_decision"]["model"] == "range_reversion_v1"
    assert signal_meta["TESTUSDT"]["range_reversion_prediction"] == candidate["range_reversion_prediction"]


def test_direct_filter_rejects_invalid_range_reversion_with_reason(monkeypatch):
    settings = _settings()
    candidate = _range_meta()
    candidate.pop("bb_lower")
    candidate.pop("bb_upper")
    signal_meta = {"TESTUSDT": dict(candidate)}
    monkeypatch.setattr(run_trades, "_db_client_ref", None)
    monkeypatch.setattr(run_trades, "update_signal_decision", lambda *args, **kwargs: None)
    run_trades._selection_begin_cycle(None, "RANGE_SCALP", "A")

    passed = run_trades._filter_buy_candidates(
        [candidate], signal_meta, settings, "RANGE_SCALP", "A", predictions={}
    )

    assert passed == []
    assert candidate["range_reversion_prediction"]["reason"] == "missing_bollinger_bands"
    assert candidate["entry_decision"]["reason"] == "RANGE_MODEL_INVALID"


def test_autonomous_normalization_falls_back_to_indicator_bollinger_values():
    raw_signal = {
        "symbol": "TESTUSDT",
        "strength": 0.70,
        "reason": "BUY",
        "indicators": {
            "price": 96.0,
            "lower_band": 95.0,
            "middle_band": 99.0,
            "upper_band": 103.0,
            "rsi": 50.0,
        },
    }

    normalized = run_trades._normalize_autonomous_signal(raw_signal, "RANGE_SCALP", 1_000_000.0)

    assert normalized["current_price"] == 96.0
    assert normalized["bollinger_bands"] == {"lower": 95.0, "middle": 99.0, "upper": 103.0}
    assert normalized["bb_position"] == 0.125


def test_selection_capture_persists_complete_range_reversion_input():
    class FakeDb:
        def __init__(self):
            self.calls = []

        def execute_update(self, query, params):
            self.calls.append((query, params))

    db_client = FakeDb()
    candidate = _range_meta()
    candidate["bb_middle"] = 99.0
    run_trades._selection_begin_cycle(db_client, "RANGE_SCALP", "A")
    run_trades._selection_capture_candidates([candidate], {}, "predicted")

    payload = json.loads(db_client.calls[-1][1][-1])
    assert payload["range_reversion_input_complete"] is True
    assert payload["range_reversion_input"] == {
        "current_price": 96.0,
        "bb_lower": 95.0,
        "bb_middle": 99.0,
        "bb_upper": 103.0,
    }
