import json
from pathlib import Path

from Dev.scripts.build_universal_entry_bilateral_theses import build_matrix


def test_bilateral_matrix_adds_independent_opposite_thesis_without_eligibility(tmp_path: Path):
    source = tmp_path / "proposals.jsonl"
    output = tmp_path / "matrix.jsonl"
    source.write_text(json.dumps({
        "opportunity_event_id": "event-1",
        "dedupe_cluster_id": "cluster-1",
        "window_id": "dev-1",
        "window_role": "development",
        "symbol": "BTCUSDT",
        "philosophy_family": "breakout_expansion",
        "philosophy_variant": "break",
        "thesis_type": "LONG_RESISTANCE_BREAKOUT",
        "side": "LONG",
        "decision_time": 100.0,
        "entry_time": 101.0,
        "entry_price": 100.5,
        "feature_available_at": 100.0,
        "ev_horizon_minutes": 240,
        "feature_snapshot": {"atr_24h": 2.0},
    }) + "\n", encoding="utf-8")

    report = build_matrix(source, output)
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

    assert report["source_event_count"] == 1
    assert report["matrix_rows"] == 2
    assert {row["thesis_role"] for row in rows} == {"observed", "opposite"}
    assert {row["thesis_type"] for row in rows} == {"LONG_RESISTANCE_BREAKOUT", "SHORT_RESISTANCE_REJECTION"}
    assert all(row["selection_eligible"] is False for row in rows)
    assert all(row["prediction_status"].startswith("pending_") for row in rows)
    assert all(row["feature_snapshot"] == {"atr_24h": 2.0} for row in rows)
    assert all(row["entry_price"] == 100.5 for row in rows)


def test_bilateral_matrix_rejects_duplicate_source_events(tmp_path: Path):
    source = tmp_path / "proposals.jsonl"
    output = tmp_path / "matrix.jsonl"
    row = {"opportunity_event_id": "duplicate", "side": "LONG", "thesis_type": "LONG_DIRECTIONAL_EDGE"}
    source.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")

    try:
        build_matrix(source, output)
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate source events must fail closed")