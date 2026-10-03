import json
from pathlib import Path

from Dev.scripts.build_universal_entry_raw_predictions import build_predictions


def test_raw_predictions_run_both_sides_and_remain_uncalibrated(tmp_path: Path):
    source = tmp_path / "matrix.jsonl"
    rows = []
    for side, role in (("LONG", "observed"), ("SHORT", "opposite")):
        rows.append({
            "bilateral_thesis_id": f"event-1:{role}",
            "opportunity_event_id": "event-1",
            "dedupe_cluster_id": "cluster-1",
            "symbol": "BTCUSDT",
            "thesis_role": role,
            "thesis_type": "LONG_MEAN_REVERSION" if side == "LONG" else "SHORT_MEAN_REVERSION",
            "side": side,
            "decision_time": 100.0,
            "entry_time": 101.0,
            "entry_price": 100.0,
            "feature_snapshot": {"atr_24h": 2.0, "rolling_mean_24h": 101.0},
        })
    source.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    report = build_predictions(source, tmp_path / "predictions.jsonl")

    assert report["rows"] == 2
    assert report["calibration_status"] == "raw_uncalibrated"
    assert report["formula_scoring_allowed"] is False
    output_rows = [json.loads(line) for line in (tmp_path / "predictions.jsonl").read_text().splitlines()]
    assert {row["side"] for row in output_rows} == {"LONG", "SHORT"}
    assert all(row["calibration_status"] == "raw_uncalibrated" for row in output_rows)
