"""Expand proposal observations into explicit bilateral thesis alternatives."""
from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "pilot_development" / "philosophy_proposals.jsonl"
DEFAULT_OUTPUT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "pilot_development" / "bilateral_thesis_matrix.jsonl"


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


def _rows(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid proposal JSON at line {line_number}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"proposal at line {line_number} is not an object")
            yield row


def _opposite_thesis(thesis: str, side: str) -> tuple[str, str]:
    thesis = str(thesis or "")
    side = str(side or "").upper()
    if "RESISTANCE" in thesis:
        return ("SHORT_RESISTANCE_REJECTION", "SHORT") if side == "LONG" else ("LONG_RESISTANCE_BREAKOUT", "LONG")
    if "SUPPORT" in thesis:
        return ("SHORT_SUPPORT_BREAKDOWN", "SHORT") if side == "LONG" else ("LONG_SUPPORT_BOUNCE", "LONG")
    if "MEAN_REVERSION" in thesis:
        return ("SHORT_MEAN_REVERSION", "SHORT") if side == "LONG" else ("LONG_MEAN_REVERSION", "LONG")
    if "MOMENTUM" in thesis or "CONTINUATION" in thesis:
        return ("SHORT_MOMENTUM_CONTINUATION", "SHORT") if side == "LONG" else ("LONG_MOMENTUM_CONTINUATION", "LONG")
    if "VOLATILITY" in thesis:
        return ("SHORT_VOLATILITY_EXPANSION", "SHORT") if side == "LONG" else ("LONG_VOLATILITY_EXPANSION", "LONG")
    return ("SHORT_DIRECTIONAL_EDGE", "SHORT") if side == "LONG" else ("LONG_DIRECTIONAL_EDGE", "LONG")


def build_matrix(input_path: Path = DEFAULT_INPUT, output_path: Path = DEFAULT_OUTPUT, *, replace: bool = False) -> Dict[str, Any]:
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    report_path = output_path.with_suffix(".report.json")
    if not replace and (output_path.exists() or report_path.exists()):
        raise FileExistsError(f"bilateral matrix exists; pass --replace: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    seen = set()
    rows_written = 0
    side_counts: Counter[str] = Counter()
    thesis_counts: Counter[str] = Counter()
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as target:
            for proposal in _rows(input_path):
                event_id = str(proposal.get("opportunity_event_id") or "")
                if not event_id or event_id in seen:
                    raise ValueError("proposal tape has duplicate or missing opportunity_event_id")
                seen.add(event_id)
                original_side = str(proposal.get("side") or "").upper()
                original_thesis = str(proposal.get("thesis_type") or "")
                opposite_thesis, opposite_side = _opposite_thesis(original_thesis, original_side)
                alternatives = [
                    ("observed", original_thesis, original_side, "source_philosophy_proposal"),
                    ("opposite", opposite_thesis, opposite_side, "independent_thesis_required"),
                ]
                for role, thesis, side, source in alternatives:
                    matrix_id = f"{event_id}:{role}"
                    side_counts[side] += 1
                    thesis_counts[thesis] += 1
                    target.write(json.dumps({
                        "bilateral_thesis_id": matrix_id,
                        "opportunity_event_id": event_id,
                        "dedupe_cluster_id": proposal.get("dedupe_cluster_id"),
                        "window_id": proposal.get("window_id"),
                        "window_role": proposal.get("window_role"),
                        "symbol": proposal.get("symbol"),
                        "decision_time": proposal.get("decision_time"),
                        "philosophy_family": proposal.get("philosophy_family"),
                        "philosophy_variant": proposal.get("philosophy_variant"),
                        "thesis_role": role,
                        "thesis_type": thesis,
                        "side": side,
                        "source": source,
                        "entry_time": proposal.get("entry_time"),
                        "entry_price": proposal.get("entry_price"),
                        "feature_available_at": proposal.get("feature_available_at"),
                        "ev_horizon_minutes": proposal.get("ev_horizon_minutes"),
                        "feature_snapshot": proposal.get("feature_snapshot"),
                        "prediction_status": "pending_independent_prediction_and_train_calibration",
                        "venue_contract_status": "pending_four_venue_contract_evidence",
                        "selection_eligible": False,
                    }, ensure_ascii=False, allow_nan=False) + "\n")
                    rows_written += 1
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)
    report = {
        "artifact_version": "universal_entry_bilateral_thesis_matrix_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_path": _display_path(input_path),
        "input_sha256": _sha256(input_path),
        "output_path": _display_path(output_path),
        "output_sha256": _sha256(output_path),
        "source_event_count": len(seen),
        "matrix_rows": rows_written,
        "expected_rows": len(seen) * 2,
        "side_counts": dict(sorted(side_counts.items())),
        "thesis_counts": dict(sorted(thesis_counts.items())),
        "all_rows_non_executable": True,
        "limitations": [
            "Opposite thesis identity is explicit but its prediction is not fabricated.",
            "No row is eligible for formula scoring until prediction, calibration and four-venue evidence exist.",
        ],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    try:
        report = build_matrix(args.input, args.output, replace=args.replace)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())