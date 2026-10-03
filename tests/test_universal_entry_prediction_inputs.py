import json
from pathlib import Path

from Dev.scripts.audit_universal_entry_prediction_inputs import audit


def test_prediction_input_audit_requires_identical_point_in_time_snapshot(tmp_path: Path):
    source = tmp_path / "matrix.jsonl"
    common = {
        "opportunity_event_id": "event-1",
        "symbol": "BTCUSDT",
        "decision_time": 100.0,
        "entry_time": 101.0,
        "entry_price": 100.5,
        "feature_available_at": 100.0,
        "ev_horizon_minutes": 240,
        "feature_snapshot": {"atr_24h": 2.0},
        "prediction_status": "pending_independent_prediction_and_train_calibration",
    }
    source.write_text("\n".join(json.dumps({**common, "thesis_role": role}) for role in ("observed", "opposite")) + "\n", encoding="utf-8")

    report = audit(source, tmp_path / "audit.json")

    assert report["rows"] == 2
    assert report["complete_bilateral_pairs"] == 1
    assert report["asymmetric_or_missing_pairs"] == 0
    assert report["point_in_time_symmetry_passed"] is True
    assert report["predictions_available"] is False
    assert report["formula_scoring_allowed"] is False
