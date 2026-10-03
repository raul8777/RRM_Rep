"""Fit train-only Platt calibration against simulated bilateral replay labels."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
PILOT_ROOT = REPO_ROOT / "Dev/benchmarks/universal_entry_selection/pilot_development"
DEFAULT_PREDICTIONS = PILOT_ROOT / "bilateral_raw_predictions.jsonl"
DEFAULT_OUTCOMES = PILOT_ROOT / "bilateral_scaffold_outcomes.jsonl"
DEFAULT_OUTPUT = PILOT_ROOT / "bilateral_replay_probability_calibration"
MODES = ("SPOT_LONG", "FUTURES_LONG", "FUTURES_SHORT")
MIN_FOLD_CLUSTERS_PER_MODE = 30


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path.resolve())


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"non-object JSON at {path.name}:{line_number}")
            rows.append(row)
    return rows


def _logit(probability: float) -> float:
    clipped = min(1.0 - 1e-9, max(1e-9, probability))
    return math.log(clipped / (1.0 - clipped))


def _sigmoid(value: float) -> float:
    value = max(-40.0, min(40.0, value))
    return 1.0 / (1.0 + math.exp(-value))


def _fit_platt(rows: List[Dict[str, Any]], *, max_iterations: int = 200) -> Tuple[float, float, bool]:
    if not rows or len({row["label"] for row in rows}) < 2:
        raise ValueError("Platt fit requires both positive and negative train labels")
    slope, intercept = 1.0, 0.0
    slope_penalty = 0.1
    intercept_penalty = 0.001

    def objective(a: float, b: float) -> float:
        loss = 0.0
        for row in rows:
            x = _logit(row["raw_probability"])
            z = max(-40.0, min(40.0, a * x + b))
            loss += math.log1p(math.exp(z)) - row["label"] * z
        n = len(rows)
        return loss / n + 0.5 * slope_penalty * a * a + 0.5 * intercept_penalty * b * b

    converged = False
    for _ in range(max_iterations):
        n = len(rows)
        g_slope = slope_penalty * slope
        g_intercept = intercept_penalty * intercept
        h_ss = slope_penalty
        h_si = 0.0
        h_ii = intercept_penalty
        for row in rows:
            x = _logit(row["raw_probability"])
            y = row["label"]
            p = _sigmoid(slope * x + intercept)
            weight = max(1e-12, p * (1.0 - p)) / n
            error = (p - y) / n
            g_slope += error * x
            g_intercept += error
            h_ss += weight * x * x
            h_si += weight * x
            h_ii += weight
        determinant = h_ss * h_ii - h_si * h_si
        if determinant <= 1e-18:
            raise ValueError("singular Hessian during regularized Platt fit")
        delta_slope = (h_ii * g_slope - h_si * g_intercept) / determinant
        delta_intercept = (-h_si * g_slope + h_ss * g_intercept) / determinant
        current_loss = objective(slope, intercept)
        step = 1.0
        while step > 1e-8:
            candidate_slope = max(-20.0, min(20.0, slope - step * delta_slope))
            candidate_intercept = max(-20.0, min(20.0, intercept - step * delta_intercept))
            if objective(candidate_slope, candidate_intercept) <= current_loss + 1e-12:
                break
            step *= 0.5
        if step <= 1e-8:
            converged = True
            break
        slope_change = candidate_slope - slope
        intercept_change = candidate_intercept - intercept
        slope, intercept = candidate_slope, candidate_intercept
        if max(abs(slope_change), abs(intercept_change)) < 1e-7:
            converged = True
            break
    if not math.isfinite(slope) or not math.isfinite(intercept):
        raise ValueError("non-finite Platt parameters")
    return slope, intercept, converged


def _brier(rows: List[Dict[str, Any]], probability_key: str) -> float | None:
    if not rows:
        return None
    return sum((row[probability_key] - row["label"]) ** 2 for row in rows) / len(rows)


def _log_loss(rows: List[Dict[str, Any]], probability_key: str) -> float | None:
    if not rows:
        return None
    eps = 1e-12
    return -sum(
        row["label"] * math.log(max(eps, row[probability_key]))
        + (1 - row["label"]) * math.log(max(eps, 1.0 - row[probability_key]))
        for row in rows
    ) / len(rows)


def _join_by_thesis(
    predictions: List[Dict[str, Any]], outcomes: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    prediction_by_thesis = {}
    for prediction in predictions:
        thesis_id = str(prediction.get("bilateral_thesis_id") or "")
        if not thesis_id or thesis_id in prediction_by_thesis:
            raise ValueError("prediction thesis identities are missing or duplicated")
        prediction_by_thesis[thesis_id] = prediction
    joined = []
    counts = Counter()
    seen = set()
    for outcome in outcomes:
        result = outcome.get("outcome") if isinstance(outcome.get("outcome"), dict) else {}
        if result.get("label_valid") is not True:
            counts["invalid_or_censored_outcomes"] += 1
            continue
        if outcome.get("label_status") != "experimental_scaffold_outcome_not_executable_fill" or outcome.get("executable_at_decision") is not False:
            raise ValueError("outcome row is not explicitly marked as a non-executable scaffold label")
        thesis_id = str(outcome.get("bilateral_thesis_id") or "")
        mode = str(outcome.get("mode") or "")
        key = (thesis_id, mode)
        if key in seen:
            raise ValueError(f"duplicate thesis/venue outcome: {key}")
        seen.add(key)
        prediction = prediction_by_thesis.get(thesis_id)
        if prediction is None:
            counts["missing_exact_thesis_prediction"] += 1
            continue
        if str(prediction.get("opportunity_event_id")) != str(outcome.get("opportunity_event_id")):
            raise ValueError("event identity mismatch for exact thesis join")
        if str(prediction.get("side") or "").upper() != str(outcome.get("side") or "").upper():
            raise ValueError("side mismatch for exact thesis join")
        if str(prediction.get("dedupe_cluster_id")) != str(outcome.get("dedupe_cluster_id")):
            raise ValueError("cluster mismatch for exact thesis join")
        probability = (prediction.get("prediction") or {}).get("success_probability")
        if not isinstance(probability, (int, float)) or not math.isfinite(float(probability)) or not 0.0 <= float(probability) <= 1.0:
            counts["invalid_raw_probability"] += 1
            continue
        if mode not in MODES:
            counts["excluded_mode"] += 1
            continue
        decision_time = float(outcome.get("decision_time") or 0.0)
        if decision_time <= 0:
            raise ValueError("missing decision timestamp")
        joined.append({
            "bilateral_thesis_id": thesis_id,
            "opportunity_event_id": outcome.get("opportunity_event_id"),
            "dedupe_cluster_id": outcome.get("dedupe_cluster_id"),
            "decision_time": decision_time,
            "symbol": outcome.get("symbol"),
            "thesis_role": outcome.get("thesis_role"),
            "thesis_type": outcome.get("thesis_type"),
            "mode": mode,
            "side": str(outcome.get("side")).upper(),
            "raw_probability": float(probability),
            "label": int(result.get("outcome") == "TP"),
            "outcome": result.get("outcome"),
        })
    return joined, dict(counts)


def fit_replay_calibration(
    predictions_path: Path = DEFAULT_PREDICTIONS,
    outcomes_path: Path = DEFAULT_OUTCOMES,
    output_root: Path = PILOT_ROOT,
    *, train_fraction: float = 0.70,
) -> Dict[str, Any]:
    if not 0.5 <= train_fraction < 1.0:
        raise ValueError("train_fraction must be in [0.5, 1.0)")
    scaffold_report_path = outcomes_path.with_suffix(".report.json")
    if not predictions_path.is_file() or not outcomes_path.is_file() or not scaffold_report_path.is_file():
        raise FileNotFoundError("predictions, outcomes, or outcome report missing")
    scaffold_report = json.loads(scaffold_report_path.read_text(encoding="utf-8"))
    if _sha256(outcomes_path) != scaffold_report.get("output_sha256"):
        raise ValueError("outcomes hash differs from replay report")
    if scaffold_report.get("label_is_executable") is not False:
        raise ValueError("this calibrator accepts only explicitly non-executable replay labels")
    source_manifest_hashes = scaffold_report.get("scaffold_source_market_manifest_hashes", {})
    source_parquet_hashes = scaffold_report.get("scaffold_source_market_parquet_hashes", {})
    if not source_manifest_hashes or not source_parquet_hashes:
        raise ValueError("outcomes lack bound manifest/Parquet provenance")
    scaffold_path = Path(str(scaffold_report.get("input_path", "")))
    if not scaffold_path.is_absolute():
        scaffold_path = REPO_ROOT / scaffold_path
    if not scaffold_path.is_file() or _sha256(scaffold_path) != scaffold_report.get("input_sha256"):
        raise ValueError("scaffold hash differs from replay report")
    scaffold = json.loads(scaffold_path.with_suffix(".report.json").read_text(encoding="utf-8"))
    if scaffold.get("source_market_manifest_hashes") != source_manifest_hashes or scaffold.get("source_market_parquet_hashes") != source_parquet_hashes:
        raise ValueError("outcome replay source hashes differ from scaffold source hashes")

    joined, join_counts = _join_by_thesis(_read_jsonl(predictions_path), _read_jsonl(outcomes_path))
    if not joined:
        raise ValueError("no exact prediction/outcome rows joined")

    cluster_times: Dict[str, float] = {}
    for row in joined:
        cluster = str(row["dedupe_cluster_id"])
        cluster_times[cluster] = min(cluster_times.get(cluster, row["decision_time"]), row["decision_time"])
    ordered_clusters = sorted(cluster_times, key=lambda cluster: (cluster_times[cluster], cluster))
    if len(ordered_clusters) < 2:
        raise ValueError("need at least two independent clusters")
    split_index = max(1, min(len(ordered_clusters) - 1, int(len(ordered_clusters) * train_fraction)))
    train_clusters = set(ordered_clusters[:split_index])
    train = [row for row in joined if row["dedupe_cluster_id"] in train_clusters]
    validation = [row for row in joined if row["dedupe_cluster_id"] not in train_clusters]
    cluster_split: Dict[str, Dict[str, int]] = {}
    models = {}
    for mode in MODES:
        train_mode = [row for row in train if row["mode"] == mode]
        validation_mode = [row for row in validation if row["mode"] == mode]
        train_mode_clusters = len({row["dedupe_cluster_id"] for row in train_mode})
        validation_mode_clusters = len({row["dedupe_cluster_id"] for row in validation_mode})
        if train_mode_clusters < MIN_FOLD_CLUSTERS_PER_MODE or validation_mode_clusters < MIN_FOLD_CLUSTERS_PER_MODE:
            raise ValueError(f"insufficient independent clusters for {mode}: train={train_mode_clusters}, validation={validation_mode_clusters}")
        slope, intercept, converged = _fit_platt(train_mode)
        if len({row["label"] for row in train_mode}) < 2:
            raise ValueError(f"single-class train labels for {mode}")
        for row in train_mode + validation_mode:
            row["calibrated_probability"] = _sigmoid(slope * _logit(row["raw_probability"]) + intercept)
        models[mode] = {
            "method": "Platt_logistic_on_raw_probability_logit",
            "slope": slope,
            "intercept": intercept,
            "converged": converged,
            "slope_l2_penalty": 0.1,
            "intercept_l2_penalty": 0.001,
            "train_rows": len(train_mode),
            "validation_rows": len(validation_mode),
            "train_independent_clusters": train_mode_clusters,
            "validation_independent_clusters": validation_mode_clusters,
            "train_label_counts": dict(Counter(row["label"] for row in train_mode)),
            "validation_label_counts": dict(Counter(row["label"] for row in validation_mode)),
            "raw_train_brier": _brier(train_mode, "raw_probability"),
            "calibrated_train_brier": _brier(train_mode, "calibrated_probability"),
            "raw_validation_brier": _brier(validation_mode, "raw_probability"),
            "calibrated_validation_brier": _brier(validation_mode, "calibrated_probability"),
            "raw_validation_log_loss": _log_loss(validation_mode, "raw_probability"),
            "calibrated_validation_log_loss": _log_loss(validation_mode, "calibrated_probability"),
        }
        cluster_split[mode] = {
            "train_clusters": train_mode_clusters,
            "validation_clusters": validation_mode_clusters,
            "clusters_crossing_folds": 0,
        }

    output_root.mkdir(parents=True, exist_ok=True)
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}_{uuid.uuid4().hex[:8]}"
    predictions_output = output_root / f"bilateral_replay_calibrated_predictions_{run_id}.jsonl"
    report_output = output_root / f"bilateral_replay_calibration_{run_id}.report.json"
    temporary = predictions_output.with_suffix(".jsonl.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as target:
            for row in joined:
                target.write(json.dumps({
                    **row,
                    "fold": "train" if row["dedupe_cluster_id"] in train_clusters else "validation",
                    "label_is_executable": False,
                    "calibration_target": "simulated_scaffold_TP_label_only",
                }, ensure_ascii=False, allow_nan=False) + "\n")
        temporary.replace(predictions_output)
    finally:
        temporary.unlink(missing_ok=True)

    validation_passed = all(
        model["converged"]
        and model["calibrated_validation_brier"] <= model["raw_validation_brier"]
        and model["calibrated_validation_log_loss"] <= model["raw_validation_log_loss"]
        for model in models.values()
    )
    report = {
        "artifact_version": "universal_entry_bilateral_replay_calibration_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "predictions_input_path": _display_path(predictions_path),
        "predictions_input_sha256": _sha256(predictions_path),
        "outcomes_input_path": _display_path(outcomes_path),
        "outcomes_input_sha256": _sha256(outcomes_path),
        "outcomes_source_market_manifest_hashes": source_manifest_hashes,
        "outcomes_source_market_parquet_hashes": source_parquet_hashes,
        "joined_rows": len(joined),
        "train_fraction": train_fraction,
        "train_rows": len(train),
        "validation_rows": len(validation),
        "independent_clusters": len(ordered_clusters),
        "train_independent_clusters": len(train_clusters),
        "validation_independent_clusters": len(ordered_clusters) - len(train_clusters),
        "clusters_crossing_folds": 0,
        "join_rejections": join_counts,
        "models_by_venue": models,
        "output_predictions_path": _display_path(predictions_output),
        "output_predictions_sha256": _sha256(predictions_output),
        "calibration_status": "simulated_replay_holdout_passed" if validation_passed else "simulated_replay_holdout_failed",
        "simulated_replay_calibration_valid": True,
        "simulated_replay_validation_passed": validation_passed,
        "usable_for_formula_scoring": False,
        "usable_for_live_execution": False,
        "limitations": [
            "Platt models are fitted on development replay outcomes only, split chronologically by dedupe_cluster_id, with all venues for a cluster kept in one fold.",
            "Probability target is TP versus SL/EXPIRY in the fixed 240-minute scaffold replay; labels are not exchange fills and contain no observed historical account execution.",
            "This calibration is valid only as a diagnostic for this simulated replay label definition; it is not a production/live or executable-contract calibrator.",
            "No formulas, entry thresholds, live configuration, or orders are authorized by this artifact.",
        ],
    }
    report_output.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    report["report_path"] = _display_path(report_output)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, default=DEFAULT_PREDICTIONS)
    parser.add_argument("--outcomes", type=Path, default=DEFAULT_OUTCOMES)
    parser.add_argument("--output-root", type=Path, default=PILOT_ROOT)
    parser.add_argument("--train-fraction", type=float, default=0.70)
    args = parser.parse_args()
    try:
        report = fit_replay_calibration(args.predictions, args.outcomes, args.output_root, train_fraction=args.train_fraction)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps({
        "report_path": report["report_path"],
        "joined_rows": report["joined_rows"],
        "train_clusters": report["train_independent_clusters"],
        "validation_clusters": report["validation_independent_clusters"],
        "simulated_replay_calibration_valid": report["simulated_replay_calibration_valid"],
        "usable_for_formula_scoring": report["usable_for_formula_scoring"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())