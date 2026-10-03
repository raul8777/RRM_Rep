import json
from pathlib import Path

from Dev.scripts.audit_universal_entry_contract_gaps import build_contract_gap_audit


def test_contract_gap_audit_emits_four_non_executable_records_without_fabrication(tmp_path: Path):
    source = tmp_path / "proposals.jsonl"
    output = tmp_path / "gaps.jsonl"
    source.write_text(json.dumps({
        "opportunity_event_id": "event-1",
        "dedupe_cluster_id": "cluster-1",
        "window_id": "dev-1",
        "window_role": "development",
        "symbol": "BTCUSDT",
        "philosophy_family": "breakout_expansion",
        "philosophy_variant": "prior_high_break",
        "thesis_type": "LONG_RESISTANCE_BREAKOUT",
        "side": "LONG",
        "decision_time": 100.0,
    }) + "\n", encoding="utf-8")

    report = build_contract_gap_audit(source, output)
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

    assert report["proposal_count"] == 1
    assert report["contract_alternative_count"] == 4
    assert {contract["mode"] for contract in rows[0]["contracts"]} == {
        "SPOT_LONG", "FUTURES_LONG", "FUTURES_SHORT", "SPOT_MARGIN_SHORT",
    }
    assert all(contract["venue_executable_at_time"] is None for contract in rows[0]["contracts"])
    assert all(contract["selection_eligible"] is False for contract in rows[0]["contracts"])
    assert rows[0]["tape_eligible"] is False


def test_contract_gap_audit_refuses_duplicate_opportunity_ids(tmp_path: Path):
    source = tmp_path / "proposals.jsonl"
    output = tmp_path / "gaps.jsonl"
    row = {"opportunity_event_id": "duplicate", "symbol": "BTCUSDT"}
    source.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")

    try:
        build_contract_gap_audit(source, output)
    except ValueError as exc:
        assert "duplicate opportunity_event_id" in str(exc)
    else:
        raise AssertionError("duplicate opportunity IDs must fail closed")