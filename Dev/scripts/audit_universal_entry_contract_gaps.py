"""Audit missing evidence for four venue contracts per philosophy proposal."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_INPUT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "pilot_development" / "philosophy_proposals.jsonl"
DEFAULT_OUTPUT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "pilot_development" / "contract_evidence_gaps.jsonl"
CONTRACT_MODES = (
    ("SPOT_LONG", "LONG", "spot"),
    ("FUTURES_LONG", "LONG", "um"),
    ("FUTURES_SHORT", "SHORT", "um"),
    ("SPOT_MARGIN_SHORT", "SHORT", "spot_margin"),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(resolved)


def _proposal_rows(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid proposal JSON at line {line_number}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"proposal at line {line_number} is not an object")
            yield row


def _contract_gap(proposal: Dict[str, Any], mode: str, contract_side: str, venue: str) -> Dict[str, Any]:
    proposal_side = str(proposal.get("side") or "").upper()
    if mode == "SPOT_MARGIN_SHORT":
        status = "venue_policy_disabled_current_historical_availability_unknown"
    elif proposal_side != contract_side:
        status = "opposite_side_requires_independent_bilateral_prediction"
    else:
        status = "pending_contract_geometry_probability_and_point_in_time_executability"
    return {
        "mode": mode,
        "side": contract_side,
        "venue": venue,
        "status": status,
        "venue_executable_at_time": None,
        "selection_eligible": False,
        "evidence_missing": [
            "prediction_or_train_fold_probability",
            "target_and_stop_geometry_from_declared_thesis",
            "historical_exchange_filter_snapshot",
            "historical_account_balance_or_borrow_state",
            "venue_cost_snapshot_for_entry_time",
        ],
        "note": "This is an evidence-gap record, not a contract, order, or outcome.",
    }


def build_contract_gap_audit(
    input_path: Path = DEFAULT_INPUT,
    output_path: Path = DEFAULT_OUTPUT,
    *,
    replace: bool = False,
) -> Dict[str, Any]:
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    report_path = output_path.with_suffix(".report.json")
    if not replace and (output_path.exists() or report_path.exists()):
        raise FileExistsError(f"contract gap audit exists; pass --replace: {output_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    proposal_count = 0
    modes: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    seen_ids = set()
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as target:
            for proposal in _proposal_rows(input_path):
                event_id = str(proposal.get("opportunity_event_id") or "")
                if not event_id or event_id in seen_ids:
                    raise ValueError("proposal tape has missing or duplicate opportunity_event_id")
                seen_ids.add(event_id)
                contracts = []
                for mode, side, venue in CONTRACT_MODES:
                    contract = _contract_gap(proposal, mode, side, venue)
                    modes[mode] += 1
                    statuses[contract["status"]] += 1
                    contracts.append(contract)
                row = {
                    "opportunity_event_id": event_id,
                    "dedupe_cluster_id": proposal.get("dedupe_cluster_id"),
                    "window_id": proposal.get("window_id"),
                    "window_role": proposal.get("window_role"),
                    "symbol": proposal.get("symbol"),
                    "philosophy_family": proposal.get("philosophy_family"),
                    "philosophy_variant": proposal.get("philosophy_variant"),
                    "thesis_type": proposal.get("thesis_type"),
                    "proposal_side": proposal.get("side"),
                    "decision_time": proposal.get("decision_time"),
                    "contracts": contracts,
                    "tape_eligible": False,
                }
                target.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                proposal_count += 1
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)

    report = {
        "artifact_version": "universal_entry_contract_evidence_gaps_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_path": _display_path(input_path),
        "input_sha256": _sha256(input_path),
        "proposal_count": proposal_count,
        "contract_alternative_count": proposal_count * len(CONTRACT_MODES),
        "counts_by_mode": dict(sorted(modes.items())),
        "counts_by_status": dict(sorted(statuses.items())),
        "all_alternatives_fail_closed": True,
        "output_path": _display_path(output_path),
        "output_sha256": _sha256(output_path),
        "limitations": [
            "No contract geometry, probability, size, fees, exchange filters, account state, or outcome is fabricated.",
            "The output audits evidence gaps and is not a candidate tape or formula benchmark input.",
        ],
    }
    report_temp = report_path.with_name(f".{report_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        report_temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report_temp.replace(report_path)
    finally:
        report_temp.unlink(missing_ok=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    try:
        report = build_contract_gap_audit(args.input, args.output, replace=args.replace)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())