"""Fit train-only base-rate probability calibration for experimental scaffold labels."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "pilot_development" / "scaffold_outcomes.jsonl"
DEFAULT_OUTPUT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "pilot_development" / "scaffold_probability_calibration.json"


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


def _rows(path: Path) -> List[Dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            if row.get("label_status") != "experimental_scaffold_outcome_not_executable_fill":
                continue
            outcome = (row.get("outcome") or {}).get("outcome")
            if outcome not in {"TP", "SL", "EXPIRY"}:
                continue
            row["label"] = 1 if outcome == "TP" else 0
            rows.append(row)
    return sorted(rows, key=lambda row: (float(row.get("decision_time") or 0), str(row.get("opportunity_event_id"))))


def _brier(rows: List[Dict[str, Any]], probabilities: Dict[str, float]) -> float | None:
    if not rows:
        return None
    return sum((probabilities[row["group"]] - row["label"]) ** 2 for row in rows) / len(rows)


def calibrate(input_path: Path = DEFAULT_INPUT, output_path: Path = DEFAULT_OUTPUT, *, train_fraction: float = 0.7) -> Dict[str, Any]:
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    if not 0.5 <= train_fraction < 1.0:
        raise ValueError("train_fraction must be between 0.5 and 1.0")
    rows = _rows(input_path)
    if len(rows) < 2:
        raise ValueError("not enough labeled scaffold outcomes")
    split_index = max(1, min(len(rows) - 1, int(len(rows) * train_fraction)))
    train = rows[:split_index]
    validation = rows[split_index:]

    def group(row: Dict[str, Any]) -> str:
        return f"{row.get('mode')}|{row.get('side')}"

    train_counts: Dict[str, Counter[str]] = defaultdict(Counter)
    for row in train:
        train_counts[group(row)]["n"] += 1
        train_counts[group(row)]["wins"] += int(row["label"])
    global_wins = sum(row["label"] for row in train)
    global_probability = (global_wins + 1.0) / (len(train) + 2.0)
    probabilities = {
        key: (counts["wins"] + 1.0) / (counts["n"] + 2.0)
        for key, counts in train_counts.items()
    }
    for row in validation:
        row["group"] = group(row)
    for row in train:
        row["group"] = group(row)

    train_probabilities = {row["group"]: probabilities.get(row["group"], global_probability) for row in train}
    validation_probabilities = {row["group"]: probabilities.get(row["group"], global_probability) for row in validation}
    train_brier = _brier(train, train_probabilities)
    validation_brier = _brier(validation, validation_probabilities)
    report = {
        "artifact_version": "universal_entry_scaffold_probability_calibration_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_path": _display_path(input_path),
        "input_sha256": _sha256(input_path),
        "label_definition": "TP=1; SL/EXPIRY=0",
        "calibration_method": "Laplace-smoothed train-fold base rate by venue/side",
        "train_fraction": train_fraction,
        "train_rows": len(train),
        "validation_rows": len(validation),
        "global_train_probability": global_probability,
        "train_group_counts": {key: dict(value) for key, value in sorted(train_counts.items())},
        "train_group_probabilities": dict(sorted(probabilities.items())),
        "train_brier": train_brier,
        "validation_brier": validation_brier,
        "calibration_status": "experimental_non_executable_pending_contract_and_fill_evidence",
        "usable_for_formula_scoring": False,
        "limitations": [
            "Labels come from non-executable scaffold geometries, not exchange-confirmed fills.",
            "This is a train-only calibration pipeline smoke result, not a promoted predictor calibration.",
            "No formula or threshold may be selected from this report.",
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
    parser.add_argument("--train-fraction", type=float, default=0.7)
    args = parser.parse_args()
    try:
        report = calibrate(args.input, args.output, train_fraction=args.train_fraction)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())