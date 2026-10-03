import json
from types import SimpleNamespace

import run_trades
import staged_entry
import sr_pool_scanner


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
        "bollinger_bands": {"lower": 95.0, "middle": 99.0, "upper": 103.0},
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
    meta.pop("bollinger_bands")

    prediction = staged_entry.build_range_reversion_prediction(meta, 96.0, _settings())

    assert prediction == {"valid": False, "reason": "missing_bollinger_bands"}


def test_shared_range_reversion_builder_uses_indicator_price_fallback():
    meta = _range_meta()
    meta.pop("current_price")
    meta.pop("price", None)
    meta["indicators"] = {
        "price": 96.0,
        "lower_band": 95.0,
        "middle_band": 99.0,
        "upper_band": 103.0,
        "rsi": 50.0,
        "vol_spike": 1.8,
        "trend_4h": "UP",
    }

    prediction = staged_entry.build_range_reversion_prediction(meta, None, _settings())

    assert prediction["valid"] is True
    assert prediction["target_price"] > 96.0
    assert prediction["stop_price"] < 96.0


def test_direct_filter_attaches_valid_range_reversion_before_entry_decision(monkeypatch):
    settings = _settings()
    candidate = _range_meta()
    signal_meta = {"TESTUSDT": dict(candidate)}
    monkeypatch.setattr(run_trades, "_db_client_ref", None)
    monkeypatch.setattr(run_trades, "update_signal_decision", lambda *args, **kwargs: None)
    run_trades._selection_begin_cycle(None, "RANGE_SCALP", "A")

    passed = run_trades._filter_buy_candidates(
        [candidate], signal_meta, settings, "RANGE_SCALP", predictions={}
    )

    assert passed == [candidate]
    assert candidate["range_reversion_prediction"]["valid"] is True
    assert candidate["entry_decision"]["eligible"] is True
    assert candidate["entry_decision"]["model"] == "range_reversion_v1"
    assert signal_meta["TESTUSDT"]["range_reversion_prediction"] == candidate["range_reversion_prediction"]


def test_filter_uses_prediction_price_when_signal_metadata_omits_root_price(monkeypatch):
    settings = _settings()
    candidate = _range_meta()
    candidate.pop("current_price")
    signal_meta = {"TESTUSDT": dict(candidate)}
    prediction = {
        "current_price": 96.0,
        "expected_pnl": 1.0,
        "expected_pnl_percentage": 1.0,
        "position_size_usdt": 100.0,
        "success_probability": 0.7,
    }
    monkeypatch.setattr(run_trades, "_db_client_ref", None)
    monkeypatch.setattr(run_trades, "update_signal_decision", lambda *args, **kwargs: None)
    run_trades._selection_begin_cycle(None, "RANGE_SCALP", "A")

    passed = run_trades._filter_buy_candidates(
        [candidate], signal_meta, settings, "RANGE_SCALP", predictions={"TESTUSDT": prediction}
    )

    assert passed == [candidate]
    assert candidate["current_price"] == 96.0
    assert candidate["range_reversion_prediction"]["valid"] is True


def test_filter_fetches_fresh_price_when_candidate_and_prediction_omit_it(monkeypatch):
    settings = _settings()
    candidate = _range_meta()
    candidate.pop("current_price")
    candidate.pop("price", None)
    signal_meta = {"TESTUSDT": dict(candidate)}
    prediction = {
        "expected_pnl": 1.0,
        "expected_pnl_percentage": 1.0,
        "position_size_usdt": 100.0,
        "success_probability": 0.7,
    }
    client = SimpleNamespace(get_symbol_price=lambda _symbol: 96.0)
    monkeypatch.setattr(run_trades, "_cycle_price_cache", {})
    monkeypatch.setattr(run_trades, "_db_client_ref", None)
    monkeypatch.setattr(run_trades, "update_signal_decision", lambda *args, **kwargs: None)
    run_trades._selection_begin_cycle(None, "RANGE_SCALP", "A")

    passed = run_trades._filter_buy_candidates(
        [candidate], signal_meta, settings, "RANGE_SCALP",
        predictions={"TESTUSDT": prediction}, client=client,
    )

    assert passed == [candidate]
    assert candidate["current_price"] == 96.0
    assert candidate["range_reversion_prediction"]["valid"] is True


def test_filter_retries_missing_predictor_for_candidate_added_after_prediction_pass(monkeypatch):
    settings = _settings()
    candidate = _range_meta()
    predictions = {}
    calls = []

    def _predict(*args, **kwargs):
        calls.append((args[0], kwargs["trade_side"], kwargs["current_price"]))
        return ({
            "expected_pnl": 1.0,
            "expected_pnl_percentage": 1.0,
            "position_size_usdt": 100.0,
            "success_probability": 0.7,
            "current_price": 96.0,
            "target_price": 104.0,
            "stop_price": 98.0,
        }, {})

    monkeypatch.setattr(run_trades, "_db_client_ref", None)
    monkeypatch.setattr(run_trades, "update_signal_decision", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_trades.pnl_service, "predict_for_symbol", _predict)
    run_trades._selection_begin_cycle(None, "RANGE_SCALP", "A")

    passed = run_trades._filter_buy_candidates(
        [candidate], {"TESTUSDT": candidate}, settings, "RANGE_SCALP",
        predictions=predictions, client=SimpleNamespace(get_symbol_price=lambda _symbol: 96.0),
    )

    assert passed == [candidate]
    assert calls == [("TESTUSDT", "LONG", 96.0)]
    assert predictions["TESTUSDT"]["expected_pnl"] == 1.0


def test_pre_arbitration_filter_rejects_missing_generic_prediction(monkeypatch):
    settings = _settings()
    candidate = _range_meta()
    candidate.pop("bb_lower")
    candidate.pop("bb_upper")
    candidate.pop("bollinger_bands")
    signal_meta = {"TESTUSDT": dict(candidate)}
    monkeypatch.setattr(run_trades, "_db_client_ref", None)
    monkeypatch.setattr(run_trades, "update_signal_decision", lambda *args, **kwargs: None)
    run_trades._selection_begin_cycle(None, "RANGE_SCALP", "A")

    passed = run_trades._filter_buy_candidates(
        [candidate], signal_meta, settings, "RANGE_SCALP", predictions={}
    )

    assert passed == []
    assert candidate["range_reversion_prediction"]["reason"] == "missing_bollinger_bands"
    assert candidate["entry_decision"]["reason"] == "MISSING_PREDICTION"


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


def test_autonomous_normalization_keeps_root_signal_price():
    raw_signal = {
        "symbol": "TESTUSDT",
        "price": 96.0,
        "strength": 0.70,
        "reason": "BUY",
        "indicators": {
            "lower_band": 95.0,
            "middle_band": 99.0,
            "upper_band": 103.0,
            "rsi": 50.0,
        },
    }

    normalized = run_trades._normalize_autonomous_signal(raw_signal, "RANGE_SCALP", 1_000_000.0)

    assert normalized["current_price"] == 96.0
    assert normalized["bb_position"] == 0.125
    assert normalized["interval"] is None


def test_autonomous_normalization_preserves_source_timeframe():
    raw_signal = {
        "symbol": "TESTUSDT",
        "interval": "15m",
        "strength": 0.70,
        "reason": "BUY",
        "indicators": {"lower_band": 95.0, "middle_band": 99.0, "upper_band": 103.0},
    }

    normalized = run_trades._normalize_autonomous_signal(raw_signal, "RANGE_SCALP", 1_000_000.0)

    assert normalized["interval"] == "15m"
    assert normalized["signal_interval"] == "15m"


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


def test_selection_capture_persists_bilateral_contracts_and_original_side():
    class FakeDb:
        def __init__(self):
            self.calls = []

        def execute_update(self, query, params):
            self.calls.append((query, params))

    db_client = FakeDb()
    arbitration = {
        "decision_id": "arb-decision-42",
        "selected_mode": "SPOT_LONG",
        "selected_thesis_type": "LONG_RESISTANCE_BREAKOUT",
        "selected_contract": {"side": "LONG", "effective_target_price": 104.0},
        "alternative_contract": {"side": "SHORT", "effective_target_price": 96.0},
        "long_venue_contracts": [{"mode": "SPOT_LONG", "side": "LONG"}],
        "short_venue_contracts": [{"mode": "FUTURES_SHORT", "side": "SHORT"}],
        "all_venue_contracts": [{"mode": "SPOT_LONG", "side": "LONG"}],
    }
    candidate = {
        "symbol": "TESTUSDT",
        "entry_signal_side": "SHORT",
        "expected_side": "LONG",
        "trade_type": "spot",
        "current_price": 100.0,
        "directional_arbitration": arbitration,
    }
    run_trades._selection_begin_cycle(db_client, "RANGE_SCALP", "A")
    run_trades._selection_capture_candidates([candidate], {}, "bilateral_prepared")

    payload = json.loads(db_client.calls[-1][1][-1])
    assert payload["entry_signal_side"] == "SHORT"
    assert payload["expected_side"] == "LONG"
    assert payload["directional_arbitration"] == arbitration
    assert payload["decision_id"] == "arb-decision-42"
    assert payload["selected_mode"] == "SPOT_LONG"
    assert payload["selected_thesis_type"] == "LONG_RESISTANCE_BREAKOUT"
    assert payload["selected_contract"]["side"] == "LONG"
    assert payload["alternative_contract"]["side"] == "SHORT"
    assert payload["long_venue_contracts"] == [{"mode": "SPOT_LONG", "side": "LONG"}]
    assert payload["short_venue_contracts"] == [{"mode": "FUTURES_SHORT", "side": "SHORT"}]
    assert payload["all_venue_contracts"] == [{"mode": "SPOT_LONG", "side": "LONG"}]


def test_selection_capture_reads_range_inputs_and_atr_from_nested_indicators():
    class FakeDb:
        def __init__(self):
            self.calls = []

        def execute_update(self, query, params):
            self.calls.append((query, params))

    db_client = FakeDb()
    candidate = {
        "symbol": "TESTUSDT",
        "indicators": {
            "price": 96.0,
            "interval": "5m",
            "atr": 1.25,
            "lower_band": 95.0,
            "middle_band": 99.0,
            "upper_band": 103.0,
        },
    }
    run_trades._selection_begin_cycle(db_client, "RANGE_SCALP", "A")
    run_trades._selection_capture_candidates([candidate], {}, "predicted")

    payload = json.loads(db_client.calls[-1][1][-1])
    assert payload["range_reversion_input_complete"] is True
    assert payload["entry_model_features"]["atr"] == 1.25
    assert payload["interval"] == "5m"


def test_selection_capture_persists_lineage_horizon_and_predictor_identity():
    class FakeDb:
        def __init__(self):
            self.calls = []

        def execute_update(self, query, params):
            self.calls.append((query, params))

    db_client = FakeDb()
    candidate = {
        "symbol": "TESTUSDT",
        "signal_id": "signal-7",
        "ready_candidate_id": 41,
        "qubo_cycle_id": "qubo-cycle-7",
        "qubo_symbols_hash": "b" * 64,
        "thesis_type": "LONG_SUPPORT_BOUNCE",
        "cycle_duration_minutes": 30,
    }
    run_trades._selection_begin_cycle(db_client, "RANGE_SCALP", "A")
    run_trades._selection_capture_candidates(
        [candidate],
        {"TESTUSDT": {
            "expected_pnl": 1.5,
            "success_probability": 0.65,
            "cycle_duration_minutes": 45,
            "weights_hash": "a" * 64,
            "weights_source": "learned",
            "weights_regime": "RANGE_SCALP",
        }},
        "bilateral_prepared",
    )

    payload = json.loads(db_client.calls[-1][1][-1])
    assert payload["opportunity_event_id"] == "signal:signal-7"
    assert payload["dedupe_cluster_id"] == "signal:signal-7"
    assert payload["dedupe_cluster_source"] == "signal_id"
    assert payload["ready_candidate_id"] == 41
    assert payload["qubo_cycle_id"] == "qubo-cycle-7"
    assert payload["qubo_symbols_hash"] == "b" * 64
    assert payload["thesis_type"] == "LONG_SUPPORT_BOUNCE"
    assert payload["ev_horizon_minutes"] == 45
    assert payload["predictor_weights_hash"] == "a" * 64
    assert payload["predictor_weights_source"] == "learned"
    assert payload["predictor_weights_regime"] == "RANGE_SCALP"


def test_selection_capture_clusters_rechecks_by_source_candle_not_cycle():
    class FakeDb:
        def __init__(self):
            self.calls = []

        def execute_update(self, query, params):
            self.calls.append((query, params))

    payloads = []
    for cycle_id in ("cycle-a", "cycle-b"):
        db_client = FakeDb()
        run_trades._selection_begin_cycle(db_client, "RANGE_SCALP", "A")
        run_trades._selection_context["cycle_id"] = cycle_id
        run_trades._selection_capture_candidates([{
            "symbol": "TESTUSDT",
            "timestamp": 1_790_000_000_000,
            "interval": "1h",
            "thesis_type": "LONG_SUPPORT_BOUNCE",
        }], {}, "bilateral_prepared")
        payloads.append(json.loads(db_client.calls[-1][1][-1]))

    assert payloads[0]["cycle_id"] == "cycle-a"
    assert payloads[1]["cycle_id"] == "cycle-b"
    assert payloads[0]["dedupe_cluster_id"] == payloads[1]["dedupe_cluster_id"]
    assert payloads[0]["opportunity_event_id"] == payloads[1]["opportunity_event_id"]
    assert payloads[0]["dedupe_cluster_source"] == "symbol_source_timestamp_interval"


def test_sr_pool_signal_contains_price_bollinger_atr_and_timeframe(monkeypatch):
    monkeypatch.setattr(sr_pool_scanner, "_resolve_sr_mode_config", lambda *_: {
        "lookback": 40,
        "pivot_order": 3,
        "near_band_atr_mult": 0.25,
        "near_band_price_pct": 0.0025,
        "pool_zone_support_max": 0.20,
        "pool_zone_resistance_min": 0.80,
        "min_strength": 0.0,
        "interval": "5m",
        "limit": 40,
    })
    monkeypatch.setattr(sr_pool_scanner, "compute_sr_features", lambda *args, **kwargs: {
        "sr_zone_position": 0.1,
        "support_level": 95.0,
        "resistance_level": 105.0,
        "support_strength": 0.8,
        "resistance_strength": 0.3,
        "support_bounce_buy": True,
        "sr_path_long": {"anchor": 95.0, "next_levels": [100.0, 105.0]},
    })
    rows = [[i, 99.0 + i, 100.0 + i, 98.0 + i, 99.5 + i, 1000.0 + i] for i in range(40)]
    client = SimpleNamespace(client=SimpleNamespace(get_klines=lambda **kwargs: rows))

    signals = sr_pool_scanner.detect_sr_pool_breaks(client, ["TESTUSDT"], "RANGE_SCALP", None)

    assert len(signals) == 1
    signal = signals[0]
    assert signal["current_price"] == 138.5
    assert signal["interval"] == signal["sr_primary_timeframe"] == "5m"
    assert signal["atr"] > 0.0
    assert signal["bb_lower"] < signal["bb_middle"] < signal["bb_upper"]
    assert signal["indicators"]["atr"] == signal["atr"]
    assert signal["sr_path_long"]["anchor"] == 95.0


def test_indicators_at_entry_uses_known_sr_timeframe():
    indicators = run_trades._build_indicators_at_entry({
        "sr_primary_timeframe": "5m",
        "atr": 1.25,
        "sr_path_long": {"anchor": 95.0},
    })

    assert indicators["interval"] == "5m"
    assert indicators["atr"] == 1.25
    assert indicators["sr_path_long"]["anchor"] == 95.0
