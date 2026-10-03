from position_thesis import build_thesis_snapshot, structural_stop_price, structural_target_price


def test_build_thesis_snapshot_from_entry_decision_prediction():
    metadata = {
        "entry_decision_prediction": {
            "target_price": 110.0,
            "stop_price": 95.0,
            "success_probability": 0.62,
        },
        "discovery_source": "sr_discovery",
    }
    snap = build_thesis_snapshot(metadata=metadata, side="LONG", regime_mode="RANGE_SCALP")
    assert snap["target_price"] == 110.0
    assert snap["stop_price"] == 95.0
    assert snap["entry_success_probability"] == 0.62
    assert snap["discovery_source"] == "sr_discovery"
    assert snap["side"] == "LONG"
    assert snap["regime_at_entry"] == "RANGE_SCALP"


def test_build_thesis_snapshot_from_pnl_prediction_fallback():
    pnl_prediction = {"target_price": 50.0, "stop_price": 48.0, "success_probability": 0.55}
    snap = build_thesis_snapshot(metadata={}, pnl_prediction=pnl_prediction, side="SHORT")
    assert snap["target_price"] == 50.0
    assert snap["stop_price"] == 48.0


def test_build_thesis_snapshot_sr_entry_context_futures():
    metadata = {
        "sr_entry_context": {
            "support_at_entry": 100.0,
            "resistance_at_entry": 110.0,
            "zone_at_entry": 0.3,
        }
    }
    snap = build_thesis_snapshot(metadata=metadata, side="LONG", regime_mode="FUTURES_SHORT")
    assert snap["support_level"] == 100.0
    assert snap["resistance_level"] == 110.0
    assert snap["entry_range_position"] == 0.3


def test_build_thesis_snapshot_indicators_at_entry_fallback_spot():
    indicators = {"graph_range_position": 0.25, "support_level": 20.0, "resistance_level": 25.0}
    snap = build_thesis_snapshot(indicators_at_entry=indicators, side="LONG", regime_mode="SPOT_BULL")
    assert snap["entry_range_position"] == 0.25
    assert snap["support_level"] == 20.0
    assert snap["resistance_level"] == 25.0


def test_build_thesis_snapshot_returns_none_when_no_evidence():
    assert build_thesis_snapshot(metadata={}, side="LONG") is None


def test_structural_stop_price_prefers_explicit_stop():
    snap = {"stop_price": 90.0, "support_level": 80.0, "side": "LONG"}
    assert structural_stop_price(snap) == 90.0


def test_structural_stop_price_falls_back_to_support_for_long():
    snap = {"stop_price": None, "support_level": 80.0, "side": "LONG"}
    assert structural_stop_price(snap) == 80.0


def test_structural_stop_price_falls_back_to_resistance_for_short():
    snap = {"stop_price": None, "resistance_level": 120.0, "side": "SHORT"}
    assert structural_stop_price(snap) == 120.0


def test_structural_target_price_prefers_explicit_target():
    snap = {"target_price": 130.0, "resistance_level": 125.0, "side": "LONG"}
    assert structural_target_price(snap) == 130.0


def test_structural_target_price_falls_back_to_opposite_level():
    snap_long = {"target_price": None, "resistance_level": 125.0, "side": "LONG"}
    assert structural_target_price(snap_long) == 125.0
    snap_short = {"target_price": None, "support_level": 90.0, "side": "SHORT"}
    assert structural_target_price(snap_short) == 90.0


def test_structural_helpers_handle_missing_snapshot():
    assert structural_stop_price(None) is None
    assert structural_target_price(None) is None
