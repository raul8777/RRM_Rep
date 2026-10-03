import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from Dev.scripts.build_universal_entry_contract_scaffold import build_scaffold


def _write_normalized_market(root: Path, symbol: str, decision_time: float, *, spot_entry: float, um_entry: float, um_contract: str | None = None):
    for venue, entry_price in (("spot", spot_entry), ("um", um_entry)):
        venue_root = root / venue
        venue_root.mkdir(parents=True, exist_ok=True)
        scale = entry_price / spot_entry
        start = decision_time - 26 * 3600
        hourly = []
        for index in range(26):
            close = (200.0 - index) * scale
            hourly.append({
                "open_time": start + index * 3600,
                "open": close,
                "high": close + 2.0 * scale,
                "low": close - 2.0 * scale,
                "close": close,
                "volume": 1.0,
            })
        pd.DataFrame(hourly).to_parquet(venue_root / f"{symbol}_1h.parquet", index=False)
        pd.DataFrame([{
            "open_time": decision_time,
            "open": entry_price,
            "high": entry_price * 1.01,
            "low": entry_price * 0.99,
            "close": entry_price,
            "volume": 1.0,
        }]).to_parquet(venue_root / f"{symbol}_1m.parquet", index=False)
        (venue_root / f"{symbol}_1m.manifest.json").write_text(json.dumps({
            "symbol": symbol,
            "venue": venue,
            "interval": "1m",
            "contract_symbol": um_contract if venue == "um" and um_contract else symbol,
        }), encoding="utf-8")


def test_contract_scaffold_derives_original_side_geometry_but_blocks_execution(tmp_path: Path):
    source = tmp_path / "proposals.jsonl"
    output = tmp_path / "scaffold.jsonl"
    normalized = tmp_path / "normalized"
    decision_time = datetime(2025, 2, 6, tzinfo=timezone.utc).timestamp()
    _write_normalized_market(normalized, "BTCUSDT", decision_time, spot_entry=174.0, um_entry=175.5)
    source.write_text(json.dumps({
        "opportunity_event_id": "event-1",
        "dedupe_cluster_id": "cluster-1",
        "window_id": "dev-1",
        "window_role": "development",
        "symbol": "BTCUSDT",
        "philosophy_family": "mean_reversion",
        "philosophy_variant": "zscore_24h_2",
        "thesis_type": "LONG_MEAN_REVERSION",
        "side": "LONG",
        "decision_time": decision_time,
        "entry_time": decision_time,
        "entry_price": 100.0,
        "feature_snapshot": {
            "atr_24h": 2.0,
            "rolling_mean_24h": 101.0,
            "prior_24h_high": 105.0,
            "prior_24h_low": 95.0,
        },
    }) + "\n", encoding="utf-8")

    report = build_scaffold(source, output, normalized_root=normalized)
    row = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    by_mode = {contract["mode"]: contract for contract in row["contracts"]}

    assert report["proposal_count"] == 1
    assert report["contract_count"] == 4
    assert set(report["source_market_parquet_hashes"]) == {
        "spot:BTCUSDT:1m", "spot:BTCUSDT:1h", "um:BTCUSDT:1m", "um:BTCUSDT:1h",
    }
    assert by_mode["SPOT_LONG"]["geometry"]["status"] == "derived_point_in_time_geometry"
    assert by_mode["SPOT_LONG"]["geometry"]["entry_price"] == 174.0
    assert by_mode["FUTURES_LONG"]["geometry"]["entry_price"] == 175.5
    assert by_mode["FUTURES_LONG"]["contract_symbol"] == "BTCUSDT"
    assert by_mode["FUTURES_LONG"]["source_market_manifest_sha256"]
    assert by_mode["FUTURES_LONG"]["selection_eligible"] is False
    assert by_mode["FUTURES_SHORT"]["geometry"]["status"] == "independent_thesis_pending"
    assert by_mode["SPOT_MARGIN_SHORT"]["geometry"]["status"] == "venue_policy_disabled"
    assert all(contract["venue_executable_at_time"] is None for contract in row["contracts"])
    assert row["tape_eligible"] is False


def test_contract_scaffold_rejects_duplicate_event_ids(tmp_path: Path):
    source = tmp_path / "proposals.jsonl"
    output = tmp_path / "scaffold.jsonl"
    row = {"opportunity_event_id": "duplicate", "side": "LONG", "entry_price": 100.0, "feature_snapshot": {"atr_24h": 1.0}}
    source.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")

    try:
        build_scaffold(source, output)
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate IDs must fail closed")


def test_bilateral_scaffold_uses_contract_symbol_and_venue_native_price(tmp_path: Path):
    normalized = tmp_path / "normalized"
    decision_time = datetime(2025, 2, 6, tzinfo=timezone.utc).timestamp()
    _write_normalized_market(
        normalized, "PEPEUSDT", decision_time,
        spot_entry=0.00001, um_entry=0.01, um_contract="1000PEPEUSDT",
    )
    source = tmp_path / "bilateral.jsonl"
    output = tmp_path / "bilateral_scaffold.jsonl"
    source.write_text(json.dumps({
        "bilateral_thesis_id": "event-1:opposite",
        "opportunity_event_id": "event-1",
        "dedupe_cluster_id": "cluster-1",
        "window_id": "dev-1",
        "window_role": "development",
        "symbol": "PEPEUSDT",
        "thesis_role": "opposite",
        "thesis_type": "SHORT_RESISTANCE_REJECTION",
        "philosophy_family": "breakout_expansion",
        "philosophy_variant": "prior_24h_high_low_break",
        "side": "SHORT",
        "decision_time": decision_time,
        "entry_time": decision_time,
        "entry_price": 0.00001,
        "feature_available_at": decision_time,
    }) + "\n", encoding="utf-8")

    build_scaffold(source, output, normalized_root=normalized)
    row = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    contracts = {contract["mode"]: contract for contract in row["contracts"]}

    assert row["bilateral_thesis_id"] == "event-1:opposite"
    assert row["thesis_type"] == "SHORT_RESISTANCE_REJECTION"
    assert contracts["FUTURES_SHORT"]["contract_symbol"] == "1000PEPEUSDT"
    assert contracts["FUTURES_SHORT"]["source_market_manifest_sha256"]
    assert contracts["FUTURES_SHORT"]["geometry"]["entry_price"] == 0.01
    assert contracts["FUTURES_SHORT"]["geometry"]["entry_price_source"] == "venue_1m_open_at_entry_time"
    assert contracts["FUTURES_SHORT"]["geometry"]["status"] == "derived_point_in_time_geometry"
    assert contracts["SPOT_MARGIN_SHORT"]["selection_eligible"] is False
    assert contracts["SPOT_MARGIN_SHORT"]["geometry"]["status"] == "venue_policy_disabled"
