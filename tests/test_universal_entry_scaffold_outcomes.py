import json
import hashlib
from pathlib import Path

import pandas as pd

from Dev.scripts.build_universal_entry_scaffold_outcomes import build_outcomes


def test_scaffold_outcomes_replay_only_derived_geometry_and_marks_non_executable(tmp_path: Path):
    normalized = tmp_path / "normalized" / "spot"
    normalized.mkdir(parents=True)
    start = pd.Timestamp("2025-02-05T00:00:00Z")
    frame = pd.DataFrame([
        {
            "open_time": (start + pd.Timedelta(minutes=index)).timestamp(),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1.0,
        }
        for index in range(240)
    ])
    frame.to_parquet(normalized / "BTCUSDT_1m.parquet", index=False)
    scaffold = tmp_path / "scaffold.jsonl"
    scaffold.write_text(json.dumps({
        "opportunity_event_id": "event-1",
        "dedupe_cluster_id": "cluster-1",
        "window_id": "dev-1",
        "window_role": "development",
        "symbol": "BTCUSDT",
        "proposal_side": "LONG",
        "decision_time": start.timestamp(),
        "contracts": [
            {
                "mode": "SPOT_LONG",
                "side": "LONG",
                "venue": "spot",
                "geometry": {
                    "status": "derived_point_in_time_geometry",
                    "entry_price": 100.0,
                    "stop_price": 90.0,
                    "target_price": 110.0,
                },
            },
            {
                "mode": "FUTURES_LONG",
                "side": "LONG",
                "venue": "um",
                "geometry": {"status": "independent_thesis_pending"},
            },
            {
                "mode": "SPOT_MARGIN_SHORT",
                "side": "SHORT",
                "venue": "spot_margin",
                "geometry": {
                    "status": "derived_point_in_time_geometry",
                    "entry_price": 100.0,
                    "stop_price": 110.0,
                    "target_price": 90.0,
                },
            },
        ],
    }) + "\n", encoding="utf-8")

    report = build_outcomes(
        scaffold,
        tmp_path / "outcomes.jsonl",
        tmp_path / "normalized",
    )

    assert report["rows_written"] == 1
    assert report["skipped_modes"] == {"SPOT_MARGIN_SHORT": 1}
    row = json.loads((tmp_path / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["outcome"]["outcome"] == "EXPIRY"
    assert row["executable_at_decision"] is False
    assert row["label_status"] == "experimental_scaffold_outcome_not_executable_fill"


def test_outcome_replay_uses_each_venue_contract_entry_price(tmp_path: Path, monkeypatch):
    normalized = tmp_path / "normalized"
    start = pd.Timestamp("2025-02-05T00:00:00Z")
    for venue, price in (("spot", 100.0), ("um", 101.0)):
        venue_root = normalized / venue
        venue_root.mkdir(parents=True)
        pd.DataFrame([{
            "open_time": (start + pd.Timedelta(minutes=index)).timestamp(),
            "open": price,
            "high": price + 1.0,
            "low": price - 1.0,
            "close": price,
            "volume": 1.0,
        } for index in range(240)]).to_parquet(venue_root / "BTCUSDT_1m.parquet", index=False)
    scaffold = tmp_path / "scaffold.jsonl"
    scaffold.write_text(json.dumps({
        "opportunity_event_id": "event-venue-prices",
        "dedupe_cluster_id": "cluster-venue-prices",
        "window_id": "dev-1",
        "window_role": "development",
        "symbol": "BTCUSDT",
        "thesis_role": "observed",
        "thesis_type": "LONG_BREAKOUT",
        "decision_time": start.timestamp(),
        "contracts": [
            {"mode": "SPOT_LONG", "side": "LONG", "venue": "spot", "geometry": {"status": "derived_point_in_time_geometry", "entry_price": 100.0, "stop_price": 90.0, "target_price": 110.0}},
            {"mode": "FUTURES_LONG", "side": "LONG", "venue": "um", "geometry": {"status": "derived_point_in_time_geometry", "entry_price": 101.0, "stop_price": 91.0, "target_price": 111.0}},
        ],
    }) + "\n", encoding="utf-8")
    observed_prices = []

    def fake_resolve(bars, **kwargs):
        observed_prices.append(kwargs["entry_price"])
        return {"outcome": "EXPIRY", "label_valid": True}

    monkeypatch.setattr("Dev.scripts.build_universal_entry_scaffold_outcomes.resolve_contract_path", fake_resolve)

    report = build_outcomes(scaffold, tmp_path / "venue-outcomes.jsonl", normalized)

    assert report["rows_written"] == 2
    assert observed_prices == [100.0, 101.0]


def test_outcome_replay_fails_closed_if_market_manifest_changes_after_scaffold(tmp_path: Path):
    normalized = tmp_path / "normalized" / "spot"
    normalized.mkdir(parents=True)
    manifest = normalized / "BTCUSDT_1m.manifest.json"
    manifest.write_text(json.dumps({"symbol": "BTCUSDT", "venue": "spot"}), encoding="utf-8")
    scaffold = tmp_path / "scaffold.jsonl"
    scaffold.write_text("\n", encoding="utf-8")
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    scaffold.with_suffix(".report.json").write_text(json.dumps({
        "source_market_manifest_hashes": {"spot:BTCUSDT": "0" * 64},
    }), encoding="utf-8")

    try:
        build_outcomes(scaffold, tmp_path / "outcomes.jsonl", tmp_path / "normalized")
    except ValueError as exc:
        assert "manifest changed" in str(exc)
    else:
        raise AssertionError("changed market manifest must block outcome replay")
    assert manifest_hash != "0" * 64


def test_outcome_replay_fails_closed_if_parquet_changes_without_manifest_change(tmp_path: Path):
    normalized = tmp_path / "normalized" / "spot"
    normalized.mkdir(parents=True)
    manifest = normalized / "BTCUSDT_1m.manifest.json"
    manifest.write_text('{"symbol":"BTCUSDT"}', encoding="utf-8")
    parquet = normalized / "BTCUSDT_1m.parquet"
    parquet.write_bytes(b"original Parquet bytes")
    scaffold = tmp_path / "scaffold.jsonl"
    scaffold.write_text("", encoding="utf-8")
    scaffold.with_suffix(".report.json").write_text(json.dumps({
        "source_market_manifest_hashes": {"spot:BTCUSDT": hashlib.sha256(manifest.read_bytes()).hexdigest()},
        "source_market_parquet_hashes": {"spot:BTCUSDT:1m": hashlib.sha256(parquet.read_bytes()).hexdigest()},
    }), encoding="utf-8")
    parquet.write_bytes(b"modified Parquet bytes")
    output = tmp_path / "outcomes.jsonl"

    try:
        build_outcomes(scaffold, output, tmp_path / "normalized")
    except ValueError as exc:
        assert "Parquet changed" in str(exc)
    else:
        raise AssertionError("replay must reject changed market data")
    assert not output.exists()


def test_outcome_replay_rejects_partial_parquet_hash_map(tmp_path: Path):
    normalized = tmp_path / "normalized"
    manifest_hashes = {}
    for venue in ("spot", "um"):
        venue_root = normalized / venue
        venue_root.mkdir(parents=True)
        manifest = venue_root / "BTCUSDT_1m.manifest.json"
        manifest.write_text('{"symbol":"BTCUSDT"}', encoding="utf-8")
        manifest_hashes[f"{venue}:BTCUSDT"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    scaffold = tmp_path / "scaffold.jsonl"
    scaffold.write_text("", encoding="utf-8")
    scaffold.with_suffix(".report.json").write_text(json.dumps({
        "source_market_manifest_hashes": manifest_hashes,
        "source_market_parquet_hashes": {"spot:BTCUSDT:1m": "a" * 64},
    }), encoding="utf-8")
    output = tmp_path / "outcomes.jsonl"

    try:
        build_outcomes(scaffold, output, normalized)
    except ValueError as exc:
        assert "um:BTCUSDT:1m" in str(exc)
    else:
        raise AssertionError("partial market source coverage must block replay")
    assert not output.exists()