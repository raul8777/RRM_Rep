from range_scalp_data_contract import REQUIRED_FEATURES, SCHEMA_VERSION, attach_snapshot, build_snapshot


def test_build_snapshot_uses_predecision_values_and_reports_missing_fields():
    meta = {
        "expected_pnl": 2.0,
        "position_size_usdt": 100.0,
        "success_probability": 0.6,
        "strength": 0.7,
        "rsi": 42.0,
        "rsi_4h": 48.0,
        "vol_spike": 1.2,
        "range_position": 0.25,
        "bb_position": 0.2,
        "atr": 0.8,
        "trend_4h": "FLAT",
    }

    snapshot, missing, completeness = build_snapshot(meta)

    assert snapshot["expected_pnl_pct"] == 2.0
    assert missing == []
    assert completeness == 1.0


def test_attach_snapshot_preserves_missingness_without_zero_imputation():
    meta = {"strength": 0.7, "rsi": 42.0}

    attach_snapshot(meta)

    assert meta["entry_feature_schema_version"] == SCHEMA_VERSION
    assert "expected_pnl_pct" in meta["entry_live_feature_missing"]
    assert meta["entry_live_features"]["expected_pnl_pct"] is None
    assert meta["entry_live_feature_completeness"] == 2 / len(REQUIRED_FEATURES)
