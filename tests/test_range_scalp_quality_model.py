from types import SimpleNamespace

import entry_profile_gate
import staged_entry


def _meta():
    return {
        "symbol": "TESTUSDT",
        "current_price": 96.0,
        "expected_pnl": 1.0,
        "position_size_usdt": 100.0,
        "success_probability": 0.7,
        "strength": 0.7,
        "rsi": 50.0,
        "rsi_4h": 50.0,
        "vol_spike": 1.8,
        "bb_position": 0.3,
        "higher_tf_trend": "UP",
        "range_position": 0.3,
        "atr": 1.0,
        "composite_score": 0.7,
    }


def _thresholds():
    return {"thresholds": {"RANGE_SCALP": {"A": 2.0, "B": 1.0, "C": 0.5}}}


def test_range_scalp_model_is_shadow_only_until_enforced(monkeypatch):
    monkeypatch.setattr(entry_profile_gate, "load_thresholds", lambda path=None: _thresholds())
    monkeypatch.setattr(entry_profile_gate, "_model_quality_score", lambda *args, **kwargs: (2.5, [], "model-v2"))
    monkeypatch.setattr(entry_profile_gate, "_fallback_score", lambda *args, **kwargs: 0.6)

    shadow_profile, shadow_info = entry_profile_gate.classify(
        _meta(), "RANGE_SCALP", range_scalp_model_mode="shadow"
    )
    enforced_profile, enforced_info = entry_profile_gate.classify(
        _meta(), "RANGE_SCALP", range_scalp_model_mode="enforced"
    )

    assert shadow_profile == "C"
    assert shadow_info["score_source"] == "fallback"
    assert shadow_info["shadow_profile"] == "A"
    assert enforced_profile == "A"
    assert enforced_info["score_source"] == "model"
    assert enforced_info["model_version"] == "model-v2"


def test_range_scalp_model_missing_features_falls_back(monkeypatch):
    monkeypatch.setattr(entry_profile_gate, "load_thresholds", lambda path=None: _thresholds())
    monkeypatch.setattr(entry_profile_gate, "_model_quality_score", lambda *args, **kwargs: (None, ["atr"], "model-v2"))
    monkeypatch.setattr(entry_profile_gate, "_fallback_score", lambda *args, **kwargs: 1.1)

    profile, info = entry_profile_gate.classify(
        _meta(), "RANGE_SCALP", range_scalp_model_mode="enforced"
    )

    assert profile == "B"
    assert info["score_source"] == "fallback"
    assert info["missing_features"] == ["atr"]


def test_staged_entry_passes_range_model_settings(monkeypatch):
    captured = {}

    def classify(*args, **kwargs):
        captured.update(kwargs)
        return "A", {"score": 2.0}

    monkeypatch.setattr(staged_entry.entry_profile_gate, "classify", classify)
    settings = SimpleNamespace(
        entry_profile_gate_thresholds_path="thresholds.json",
        range_scalp_quality_model_mode="shadow",
        range_scalp_quality_model_path="range-model.pkl",
    )

    profile, score, _ = staged_entry._classify_with_gate(_meta(), "RANGE_SCALP", "TESTUSDT", settings, None)

    assert profile == "A"
    assert score == 2.0
    assert captured["range_scalp_model_mode"] == "shadow"
    assert captured["range_scalp_model_path"] == "range-model.pkl"
