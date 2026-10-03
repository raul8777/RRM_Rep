"""Audit raw bilateral probability calibration against non-executable scaffold labels."""
from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PREDICTIONS = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "pilot_development" / "bilateral_raw_predictions.jsonl"
DEFAULT_OUTCOMES = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "pilot_development" / "scaffold_outcomes.jsonl"
DEFAULT_OUTPUT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "pilot_development" / "bilateral_raw_calibration_audit.json"


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


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def _brier(rows: List[Dict[str, Any]]) -> float | None:
    if not rows:
        return None
    return sum((float(row["probability"]) - int(row["label"])) ** 2 for row in rows) / len(rows)


def _split_by_cluster(rows: List[Dict[str, Any]], train_fraction: float) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("dedupe_cluster_id") or row.get("opportunity_event_id"))].append(row)
    ordered_clusters = sorted(
        grouped,
        key=lambda cluster_id: (
            min(float(row["decision_time"]) for row in grouped[cluster_id]),
            cluster_id,
        ),
    )
    if len(ordered_clusters) < 2:
        return [row for cluster_id in ordered_clusters for row in grouped[cluster_id]], []
    split = max(1, min(len(ordered_clusters) - 1, int(len(ordered_clusters) * train_fraction)))
    train_ids = set(ordered_clusters[:split])
    train = [row for cluster_id in ordered_clusters if cluster_id in train_ids for row in grouped[cluster_id]]
    validation = [row for cluster_id in ordered_clusters if cluster_id not in train_ids for row in grouped[cluster_id]]
    return train, validation


def audit_calibration(
    predictions_path: Path = DEFAULT_PREDICTIONS,
    outcomes_path: Path = DEFAULT_OUTCOMES,
    output_path: Path = DEFAULT_OUTPUT,
    *,
    train_fraction: float = 0.7,
    include_opposite: bool = False,
    all_venues: bool = False,
) -> Dict[str, Any]:
    if not predictions_path.is_file() or not outcomes_path.is_file():
        raise FileNotFoundError("raw predictions or scaffold outcomes missing")
    if not 0.5 <= train_fraction < 1.0:
        raise ValueError("train_fraction must be between 0.5 and 1.0")

    predictions = _read_jsonl(predictions_path)
    outcomes = _read_jsonl(outcomes_path)
    outcome_by_key: Dict[tuple[str, str], Dict[str, Any]] = {}
    for outcome in outcomes:
        result = outcome.get("outcome") or {}
        if not result.get("label_valid"):
            continue
        key = (str(outcome.get("opportunity_event_id")), str(outcome.get("mode")))
        outcome_by_key[key] = outcome

    joined = []
    missing = Counter()
    for prediction in predictions:
        thesis_role = str(prediction.get("thesis_role") or "observed")
        if thesis_role != "observed" and not include_opposite:
            continue
        pred = prediction.get("prediction") or {}
        probability = pred.get("success_probability")
        if not isinstance(probability, (int, float)) or not 0.0 <= float(probability) <= 1.0:
            missing["invalid_probability"] += 1
            continue
        side = str(prediction.get("side") or "").upper()
        modes = (
            ("SPOT_LONG", "FUTURES_LONG") if side == "LONG" else ("FUTURES_SHORT",)
        ) if all_venues else (("FUTURES_SHORT" if side == "SHORT" else "SPOT_LONG",))
        for mode in modes:
            outcome = outcome_by_key.get((str(prediction.get("opportunity_event_id")), mode))
            if outcome is None:
                missing[f"outcome:{mode}:{thesis_role}"] += 1
                continue
            joined.append({
                "opportunity_event_id": prediction.get("opportunity_event_id"),
                "dedupe_cluster_id": prediction.get("dedupe_cluster_id") or prediction.get("opportunity_event_id"),
                "decision_time": float(prediction.get("decision_time") or 0.0),
                "side": side,
                "thesis_role": thesis_role,
                "thesis_type": prediction.get("thesis_type"),
                "mode": mode,
                "probability": float(probability),
                "label": int((outcome.get("outcome") or {}).get("outcome") == "TP"),
                "outcome": (outcome.get("outcome") or {}).get("outcome"),
            })
    train, validation = _split_by_cluster(joined, train_fraction)
    bins = defaultdict(lambda: {"n": 0, "wins": 0, "probability_sum": 0.0})
    for row in validation:
        bucket = min(9, int(float(row["probability"]) * 10))
        bins[bucket]["n"] += 1
        bins[bucket]["wins"] += row["label"]
        bins[bucket]["probability_sum"] += row["probability"]
    group_metrics: Dict[str, Any] = {}
    group_keys = sorted({(str(row["thesis_role"]), str(row["mode"]), str(row["side"])) for row in joined})
    for role, mode, side in group_keys:
        selection = [row for row in joined if row["thesis_role"] == role and row["mode"] == mode and row["side"] == side]
        train_selection = [row for row in train if row["thesis_role"] == role and row["mode"] == mode and row["side"] == side]
        validation_selection = [row for row in validation if row["thesis_role"] == role and row["mode"] == mode and row["side"] == side]
        group_metrics[f"{role}|{mode}|{side}"] = {
            "n": len(selection),
            "brier_raw": _brier(selection),
            "tp_rate": sum(row["label"] for row in selection) / len(selection),
            "train_n": len(train_selection),
            "train_brier_raw": _brier(train_selection),
            "validation_n": len(validation_selection),
            "validation_brier_raw": _brier(validation_selection),
        }
    train_cluster_count = len({row["dedupe_cluster_id"] for row in train})
    validation_cluster_count = len({row["dedupe_cluster_id"] for row in validation})
    report = {
        "artifact_version": "universal_entry_raw_calibration_audit_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "predictions_path": _display_path(predictions_path),
        "outcomes_path": _display_path(outcomes_path),
        "predictions_sha256": _sha256(predictions_path),
        "outcomes_sha256": _sha256(outcomes_path),
        "joined_rows": len(joined),
        "train_rows": len(train),
        "validation_rows": len(validation),
        "train_independent_clusters": train_cluster_count,
        "validation_independent_clusters": validation_cluster_count,
        "clusters_split_across_folds": 0,
        "include_opposite_theses": include_opposite,
        "all_venues": all_venues,
        "metrics_by_thesis_mode_side": group_metrics,
        "train_fraction": train_fraction,
        "train_brier_raw": _brier(train),
        "validation_brier_raw": _brier(validation),
        "validation_reliability_bins": {str(key): value for key, value in sorted(bins.items())},
        "missing_counts": dict(missing),
        "calibration_status": "experimental_raw_calibration_not_executable",
        "usable_for_formula_scoring": False,
        "limitations": [
            "Labels come from non-executable scaffold geometries, not exchange-confirmed fills.",
            "Train/validation are split chronologically by dedupe_cluster_id; no cluster is divided across folds.",
            "This audits raw predictor behavior; it does not fit or promote a calibrator.",
            "No formula, threshold, contract, or Staging action is authorized by this report.",
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
    parser.add_argument("--predictions", type=Path, default=DEFAULT_PREDICTIONS)
    parser.add_argument("--outcomes", type=Path, default=DEFAULT_OUTCOMES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--train-fraction", type=float, default=0.7)
    parser.add_argument("--include-opposite", action="store_true", help="include independent opposite-thesis raw predictions")
    parser.add_argument("--all-venues", action="store_true", help="include each side-matched non-Margin venue geometry")
    args = parser.parse_args()
    try:
        report = audit_calibration(
            args.predictions, args.outcomes, args.output,
            train_fraction=args.train_fraction,
            include_opposite=args.include_opposite,
            all_venues=args.all_venues,
        )
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
