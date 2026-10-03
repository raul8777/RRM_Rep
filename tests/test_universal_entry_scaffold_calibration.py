import json
from pathlib import Path

from Dev.scripts.calibrate_universal_entry_scaffold_probabilities import calibrate


def test_calibration_uses_temporal_train_split_and_is_not_marked_executable(tmp_path: Path):
    source = tmp_path / "outcomes.jsonl"
    rows = []
    for index in range(10):
        rows.append({
            "opportunity_event_id": f"event-{index}",
            "mode": "SPOT_LONG",
            "side": "LONG",
            "decision_time": index,
            "label_status": "experimental_scaffold_outcome_not_executable_fill",
            "outcome": {"outcome": "TP" if index % 2 == 0 else "SL"},
        })
    source.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    report = calibrate(source, tmp_path / "calibration.json", train_fraction=0.7)

    assert report["train_rows"] == 7
    assert report["validation_rows"] == 3
    assert report["calibration_status"] == "experimental_non_executable_pending_contract_and_fill_evidence"
    assert report["usable_for_formula_scoring"] is False
    assert report["validation_brier"] is not None