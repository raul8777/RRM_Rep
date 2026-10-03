import json
from pathlib import Path

from Dev.scripts.calibrate_bilateral_raw_predictions import audit_calibration


def test_raw_calibration_audit_joins_observed_predictions_temporally_but_stays_non_executable(tmp_path: Path):
    predictions = tmp_path / "predictions.jsonl"
    outcomes = tmp_path / "outcomes.jsonl"
    prediction_rows = []
    outcome_rows = []
    for index in range(10):
        prediction_rows.append({
            "opportunity_event_id": f"event-{index}",
            "dedupe_cluster_id": f"cluster-{index}",
            "thesis_role": "observed",
            "side": "LONG",
            "thesis_type": "LONG_MEAN_REVERSION",
            "decision_time": float(index),
            "prediction": {"success_probability": 0.5},
        })
        prediction_rows.append({
            "opportunity_event_id": f"event-{index}",
            "dedupe_cluster_id": f"cluster-{index}",
            "thesis_role": "opposite",
            "side": "SHORT",
            "decision_time": float(index),
            "prediction": {"success_probability": 0.5},
        })
        outcome_rows.append({
            "opportunity_event_id": f"event-{index}",
            "mode": "SPOT_LONG",
            "outcome": {"label_valid": True, "outcome": "TP" if index % 2 == 0 else "SL"},
        })
    predictions.write_text("\n".join(json.dumps(row) for row in prediction_rows) + "\n", encoding="utf-8")
    outcomes.write_text("\n".join(json.dumps(row) for row in outcome_rows) + "\n", encoding="utf-8")

    report = audit_calibration(predictions, outcomes, tmp_path / "calibration.json", train_fraction=0.7)

    assert report["joined_rows"] == 10
    assert report["train_rows"] == 7
    assert report["validation_rows"] == 3
    assert report["calibration_status"] == "experimental_raw_calibration_not_executable"
    assert report["usable_for_formula_scoring"] is False
    assert report["validation_brier_raw"] is not None


def test_raw_calibration_can_include_both_theses_and_available_venues_without_cluster_leakage(tmp_path: Path):
    predictions = tmp_path / "predictions.jsonl"
    outcomes = tmp_path / "outcomes.jsonl"
    prediction_rows = []
    outcome_rows = []
    for index in range(4):
        for role, side, thesis in (
            ("observed", "LONG", "LONG_BREAKOUT"),
            ("opposite", "SHORT", "SHORT_REJECTION"),
        ):
            event_id = f"event-{index}-{role}"
            prediction_rows.append({
                "opportunity_event_id": event_id,
                "dedupe_cluster_id": f"cluster-{index}",
                "thesis_role": role,
                "side": side,
                "thesis_type": thesis,
                "decision_time": float(index),
                "prediction": {"success_probability": 0.6},
            })
            modes = ("SPOT_LONG", "FUTURES_LONG") if side == "LONG" else ("FUTURES_SHORT",)
            for mode in modes:
                outcome_rows.append({
                    "opportunity_event_id": event_id,
                    "mode": mode,
                    "outcome": {"label_valid": True, "outcome": "TP" if index % 2 == 0 else "SL"},
                })
    predictions.write_text("\n".join(json.dumps(row) for row in prediction_rows) + "\n", encoding="utf-8")
    outcomes.write_text("\n".join(json.dumps(row) for row in outcome_rows) + "\n", encoding="utf-8")

    report = audit_calibration(
        predictions, outcomes, tmp_path / "both-sides-calibration.json",
        train_fraction=0.5, include_opposite=True, all_venues=True,
    )

    assert report["joined_rows"] == 12
    assert report["train_independent_clusters"] == 2
    assert report["validation_independent_clusters"] == 2
    assert report["clusters_split_across_folds"] == 0
    opposite_short = report["metrics_by_thesis_mode_side"]["opposite|FUTURES_SHORT|SHORT"]
    assert opposite_short["n"] == 4
    assert opposite_short["train_n"] == 2
    assert opposite_short["validation_n"] == 2
    assert report["usable_for_formula_scoring"] is False
