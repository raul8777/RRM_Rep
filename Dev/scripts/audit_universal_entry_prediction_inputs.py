"""Audit bilateral prediction inputs for symmetry and point-in-time completeness."""
from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "pilot_development" / "bilateral_thesis_matrix.jsonl"
DEFAULT_OUTPUT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "pilot_development" / "bilateral_prediction_input_audit.json"


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


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def audit(input_path: Path = DEFAULT_INPUT, output_path: Path = DEFAULT_OUTPUT) -> Dict[str, Any]:
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    grouped = defaultdict(list)
    rows = 0
    invalid = 0
    for line in input_path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            invalid += 1
            continue
        grouped[str(row.get("opportunity_event_id"))].append(row)
        rows += 1

    complete_pairs = 0
    asymmetric_pairs = 0
    missing_feature_counts: Counter[str] = Counter()
    prediction_status_counts: Counter[str] = Counter()
    examples = []
    for event_id, members in grouped.items():
        roles = {str(row.get("thesis_role")): row for row in members}
        observed = roles.get("observed")
        opposite = roles.get("opposite")
        if not observed or not opposite:
            asymmetric_pairs += 1
            continue
        shared_keys = ("symbol", "decision_time", "entry_time", "entry_price", "feature_available_at", "ev_horizon_minutes")
        mismatches = [key for key in shared_keys if observed.get(key) != opposite.get(key)]
        observed_features = observed.get("feature_snapshot") if isinstance(observed.get("feature_snapshot"), dict) else {}
        opposite_features = opposite.get("feature_snapshot") if isinstance(opposite.get("feature_snapshot"), dict) else {}
        if _canonical(observed_features) != _canonical(opposite_features):
            mismatches.append("feature_snapshot")
        prediction_status_counts[str(observed.get("prediction_status"))] += 1
        prediction_status_counts[str(opposite.get("prediction_status"))] += 1
        if mismatches:
            asymmetric_pairs += 1
            if len(examples) < 20:
                examples.append({"event_id": event_id, "mismatches": mismatches})
            continue
        complete_pairs += 1
        for key in ("atr_24h", "rolling_mean_24h", "rolling_std_24h", "prior_24h_high", "prior_24h_low"):
            if observed_features.get(key) is None:
                missing_feature_counts[key] += 1

    report = {
        "artifact_version": "universal_entry_prediction_input_audit_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_path": _display_path(input_path),
        "input_sha256": _sha256(input_path),
        "rows": rows,
        "invalid_rows": invalid,
        "source_events": len(grouped),
        "complete_bilateral_pairs": complete_pairs,
        "asymmetric_or_missing_pairs": asymmetric_pairs,
        "missing_feature_counts": dict(missing_feature_counts),
        "prediction_status_counts": dict(prediction_status_counts),
        "point_in_time_symmetry_passed": invalid == 0 and asymmetric_pairs == 0 and complete_pairs == len(grouped),
        "predictions_available": False,
        "calibration_available": False,
        "formula_scoring_allowed": False,
        "examples": examples,
        "limitations": [
            "This audits shared inputs only; it does not run or calibrate a predictor.",
            "Both thesis rows remain non-executable until predictions, contracts and venue evidence exist.",
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output_path)
    report["output"] = _display_path(output_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        report = audit(args.input, args.output)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
