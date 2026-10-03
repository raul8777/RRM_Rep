import hashlib
import json
from pathlib import Path

import pytest

from Dev.scripts.fit_bilateral_replay_calibration import fit_replay_calibration


def _write_inputs(tmp_path):
    predictions_path = tmp_path / "predictions.jsonl"
    outcomes_path = tmp_path / "outcomes.jsonl"
    scaffold_path = tmp_path / "bilateral_contract_scaffold.jsonl"
    scaffold_path.write_text("{}\n", encoding="utf-8")
    scaffold_report = scaffold_path.with_suffix(".report.json")
    source_manifests = {"spot:BTCUSDT": "manifest-hash", "um:BTCUSDT": "manifest-hash-um"}
    source_parquets = {"spot:BTCUSDT:1m": "parquet-hash", "um:BTCUSDT:1m": "parquet-hash-um"}
    scaffold_report.write_text(json.dumps({
        "source_market_manifest_hashes": source_manifests,
        "source_market_parquet_hashes": source_parquets,
    }), encoding="utf-8")
    predictions = []
    outcomes = []
    for index in range(100):
        event_id = f"event-{index}"
        cluster_id = f"cluster-{index}"
        decision_time = 1_700_000_000 + index * 3600
        for side, thesis_role in (("LONG", "observed"), ("SHORT", "opposite")):
            thesis_id = f"{event_id}:{side}"
            probability = 0.25 + (index % 5) * 0.1 + (0.05 if side == "SHORT" else 0.0)
            predictions.append({
                "bilateral_thesis_id": thesis_id,
                "opportunity_event_id": event_id,
                "dedupe_cluster_id": cluster_id,
                "decision_time": decision_time,
                "side": side,
                "thesis_role": thesis_role,
                "prediction": {"success_probability": probability},
            })
            modes = ("SPOT_LONG", "FUTURES_LONG") if side == "LONG" else ("FUTURES_SHORT",)
            for mode in modes:
                label = int((index + (mode == "FUTURES_LONG") + (side == "SHORT")) % 3 == 0)
                outcomes.append({
                    "bilateral_thesis_id": thesis_id,
                    "opportunity_event_id": event_id,
                    "dedupe_cluster_id": cluster_id,
                    "decision_time": decision_time,
                    "symbol": "BTCUSDT",
                    "thesis_role": thesis_role,
                    "thesis_type": f"{side}_TEST",
                    "mode": mode,
                    "side": side,
                    "executable_at_decision": False,
                    "label_status": "experimental_scaffold_outcome_not_executable_fill",
                    "outcome": {"label_valid": True, "outcome": "TP" if label else "SL"},
                })
    predictions_path.write_text("".join(json.dumps(row) + "\n" for row in predictions), encoding="utf-8")
    outcomes_path.write_text("".join(json.dumps(row) + "\n" for row in outcomes), encoding="utf-8")
    outcomes_path.with_suffix(".report.json").write_text(json.dumps({
        "input_path": str(scaffold_path),
        "input_sha256": hashlib.sha256(scaffold_path.read_bytes()).hexdigest(),
        "output_sha256": hashlib.sha256(outcomes_path.read_bytes()).hexdigest(),
        "scaffold_source_market_manifest_hashes": source_manifests,
        "scaffold_source_market_parquet_hashes": source_parquets,
        "label_is_executable": False,
    }), encoding="utf-8")
    return predictions_path, outcomes_path


def test_replay_calibration_exact_join_and_chronological_cluster_holdout(tmp_path):
    predictions, outcomes = _write_inputs(tmp_path)

    report = fit_replay_calibration(predictions, outcomes, tmp_path / "calibration")

    assert report["joined_rows"] == 300
    assert report["train_independent_clusters"] == 70
    assert report["validation_independent_clusters"] == 30
    assert report["clusters_crossing_folds"] == 0
    assert set(report["models_by_venue"]) == {"SPOT_LONG", "FUTURES_LONG", "FUTURES_SHORT"}
    assert all(model["validation_independent_clusters"] >= 30 for model in report["models_by_venue"].values())
    assert report["simulated_replay_calibration_valid"] is True
    assert report["usable_for_formula_scoring"] is False
    assert report["usable_for_live_execution"] is False
    rows = [json.loads(line) for line in (tmp_path / "calibration" / Path(report["output_predictions_path"]).name).read_text(encoding="utf-8").splitlines()]
    assert all(row["label_is_executable"] is False for row in rows)
    assert all(row["fold"] == "train" or row["fold"] == "validation" for row in rows)


def test_replay_calibration_rejects_changed_outcome_file(tmp_path):
    predictions, outcomes = _write_inputs(tmp_path)
    outcomes.write_text(outcomes.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="outcomes hash differs"):
        fit_replay_calibration(predictions, outcomes, tmp_path / "calibration")