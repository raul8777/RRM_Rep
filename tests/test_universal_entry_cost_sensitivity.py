import hashlib
import json
from pathlib import Path

import pandas as pd

from Dev.scripts import build_universal_entry_cost_sensitivity as costs


def test_cost_sensitivity_uses_current_fees_and_public_funding_as_labelled_scenarios(tmp_path, monkeypatch):
    pilot = tmp_path / "pilot"
    pilot.mkdir()
    funding_dir = tmp_path / "external"
    funding_dir.mkdir()
    funding = funding_dir / "funding.parquet"
    pd.DataFrame([{
        "symbol": "BTCUSDT", "funding_time": pd.Timestamp("2025-02-05T00:30:00Z"), "funding_rate": 0.0001,
    }]).to_parquet(funding, index=False)
    funding_manifest = funding_dir / "manifest.json"
    funding_manifest.write_text(json.dumps({"files": [{"file": funding.name, "sha256": hashlib.sha256(funding.read_bytes()).hexdigest()}]}), encoding="utf-8")
    raw_funding = pilot / "public_production_funding_1000PEPEUSDT_20250205_20250220.json"
    raw_funding.write_text(json.dumps([{"symbol": "1000PEPEUSDT", "fundingTime": 1738713600000, "fundingRate": "0.0001"}]), encoding="utf-8")
    recovery = pilot / "public_production_funding_coverage.json"
    recovery.write_text(json.dumps({"recovered_raw_path": str(raw_funding), "recovered_raw_sha256": hashlib.sha256(raw_funding.read_bytes()).hexdigest()}), encoding="utf-8")
    outcomes = pilot / "outcomes.jsonl"
    common = {"label_valid": True, "entry_time": 1738713600, "exit_time": 1738717200,
              "entry_price": 100.0, "exit_price": 102.0, "quantity": 1.0, "gross_pnl_usdt": 2.0}
    rows = [
        {"opportunity_event_id": "spot", "symbol": "BTCUSDT", "contract_symbol": "BTCUSDT", "mode": "SPOT_LONG", "side": "LONG", "outcome": common},
        {"opportunity_event_id": "future-long", "symbol": "BTCUSDT", "contract_symbol": "BTCUSDT", "mode": "FUTURES_LONG", "side": "LONG", "outcome": common},
        {"opportunity_event_id": "future-short", "symbol": "BTCUSDT", "contract_symbol": "BTCUSDT", "mode": "FUTURES_SHORT", "side": "SHORT", "outcome": common},
    ]
    outcomes.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    fees = pilot / "fees.json"
    fees.write_text(json.dumps({
        "capture_type": "current_account_specific_fees_not_historical",
        "captured_at": "2026-10-01T20:00:00+00:00",
        "orders_submitted": False,
        "venues": {
            "spot_demo": {"records": [{"symbol": f"SYM{i}", "status": "verified", "rates": {"standardCommission": {"taker": "0.001"}}} for i in range(25)] + [{"symbol": "BTCUSDT", "status": "verified", "rates": {"standardCommission": {"taker": "0.001"}}}], "verified_symbols": 26, "expected_symbols": 26},
            "usd_m_futures_testnet": {"records": [{"symbol": f"SYM{i}", "status": "verified", "rates": {"takerCommissionRate": "0.0004"}} for i in range(25)] + [{"symbol": "BTCUSDT", "status": "verified", "rates": {"takerCommissionRate": "0.0004"}}], "verified_symbols": 26, "expected_symbols": 26},
        },
    }), encoding="utf-8")
    monkeypatch.setattr(costs, "FUNDING_PARQUET", funding)
    monkeypatch.setattr(costs, "FUNDING_MANIFEST", funding_manifest)
    monkeypatch.setattr(costs, "PUBLIC_FUNDING_RECOVERY", recovery)

    report = costs.build_cost_sensitivity(outcomes_path=outcomes, fee_path=fees, output_root=pilot / "costs")

    assert report["rows_written"] == 12
    assert report["is_executable_replay"] is False
    assert report["formula_scoring_allowed"] is False
    assert report["funding_provenance"]["source"].endswith("not_testnet")
    assert len(report["summaries"]) == 12
    rows_path = next((pilot / "costs").glob("bilateral_cost_sensitivity_*.jsonl"))
    rows_out = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines()]
    assert hashlib.sha256(rows_path.read_bytes()).hexdigest() == report["rows_sha256"]
    base_scenario = next(row for row in rows_out if row["mode"] == "FUTURES_LONG" and row["slippage_bps_per_fill_assumed"] == 0)
    assert base_scenario["entry_taker_fee_rate_current"] == 0.0004
    assert base_scenario["public_production_funding_events"] == 1
    assert base_scenario["public_production_funding_cost_pct_approx"] == 0.0001
    assert base_scenario["costs_are_historical_account_fills"] is False


def test_cost_sensitivity_rejects_incomplete_fee_snapshot(tmp_path):
    fee = tmp_path / "fees.json"
    fee.write_text(json.dumps({"capture_type": "current_account_specific_fees_not_historical", "orders_submitted": False,
                               "venues": {"spot_demo": {"verified_symbols": 1, "expected_symbols": 26}}}), encoding="utf-8")
    try:
        costs.build_cost_sensitivity(outcomes_path=tmp_path / "missing.jsonl", fee_path=fee, output_root=tmp_path / "out")
    except ValueError as exc:
        assert "coverage incomplete" in str(exc)
    else:
        raise AssertionError("incomplete fee evidence must block sensitivity build")