"""Score canonical formula arms on identical, explicitly simulated fast tapes."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from portfolio_selector import Candidate, select_portfolio
from Dev.scripts.universal_entry_run_state import GIB, ProgressLedger, memory_snapshot, safe_worker_limit


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN_ROOT = REPO_ROOT / "Dev/benchmarks/universal_entry_selection/comparison_runs"
FORMULAS = (
    "baseline_current_universal",
    "linear_additive_utility",
    "multiplicative_quality",
    "executable_contract_ev",
    "predictor_calibrated_ev",
    "robust_lower_confidence_ev",
    "pareto_frontier",
    "constraint_outranking",
    "marginal_portfolio_utility",
    "calibrated_qubo",
)
FRICTION_BPS_PER_FILL = (0.0, 5.0, 10.0, 20.0)
SLOT_CAPACITY = 6
FUTURES_MAX_NEW_PER_CYCLE = 3
BOOTSTRAP_REPLICATES = 500
CANONICAL_FAMILY_MAP = {
    "mean_reversion": "mean_reversion_range",
    "time_series_momentum": "time_series_momentum_continuation",
    "breakout_expansion": "breakout_volatility_expansion",
    "volatility_expansion": "breakout_volatility_expansion",
    "cross_sectional_relative_strength": "cross_sectional_relative_strength",
}
PENDING_CANONICAL_FAMILIES = (
    "carry_funding_aware",
    "volatility_risk_premium_state",
    "statistical_arbitrage_relative_value",
    "microstructure_order_flow",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _stable_seed(*parts: Any) -> int:
    value = "|".join(str(part) for part in parts)
    return int(hashlib.sha256(value.encode("utf-8")).hexdigest()[:8], 16)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-40.0, min(40.0, value))))


def _logit(probability: float) -> float:
    value = min(1.0 - 1e-9, max(1e-9, probability))
    return math.log(value / (1.0 - value))


def _platt_probability(raw_probability: float, model: Dict[str, Any]) -> float:
    return _sigmoid(float(model["slope"]) * _logit(raw_probability) + float(model["intercept"]))


def _family_quality(family: str, matrix_row: Dict[str, Any], prediction: Dict[str, Any]) -> float:
    features = matrix_row.get("feature_snapshot") if isinstance(matrix_row.get("feature_snapshot"), dict) else {}
    indicators = prediction.get("indicators_used") if isinstance(prediction.get("indicators_used"), dict) else {}
    if family == "mean_reversion":
        raw = abs(_number(features.get("zscore_24h"))) / 4.0
    elif family == "time_series_momentum":
        raw = abs(_number(features.get("return_4h"))) / 0.02
    elif family == "breakout_expansion":
        close = _number(features.get("close"))
        level_key = "prior_24h_high" if str(matrix_row.get("side", "")).upper() == "LONG" else "prior_24h_low"
        distance = abs(close - _number(features.get(level_key))) if close else 0.0
        raw = distance / max(_number(features.get("atr_24h")), 1e-12)
    elif family == "volatility_expansion":
        raw = max(0.0, _number(features.get("true_range_expansion_ratio")) - 1.0) / 2.0
    elif family == "cross_sectional_relative_strength":
        relative = indicators.get("relative_strength_zscore_24h")
        if relative is None:
            relative = features.get("relative_return_24h_zscore", features.get("return_4h"))
        raw = abs(_number(relative)) / 3.0
    else:
        raw = 0.0
    return max(0.0, min(1.0, raw))


def _candidate_rows(
    *,
    role: str,
    role_root: Path,
    models: Dict[str, Dict[str, Any]],
    calibrated_rows: Dict[Tuple[str, str], Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    matrix_rows = _read_jsonl(role_root / "bilateral_thesis_matrix.jsonl")
    scaffold_rows = _read_jsonl(role_root / "bilateral_contract_scaffold.jsonl")
    outcome_rows = _read_jsonl(role_root / "bilateral_scaffold_outcomes.jsonl")
    prediction_rows = _read_jsonl(role_root / "bilateral_raw_predictions.jsonl")
    matrix = {row["bilateral_thesis_id"]: row for row in matrix_rows}
    scaffold = {row["bilateral_thesis_id"]: row for row in scaffold_rows}
    predictions = {row["bilateral_thesis_id"]: row for row in prediction_rows}
    counts: Counter[str] = Counter()
    candidates = []
    for outcome_row in outcome_rows:
        counts["outcome_rows"] += 1
        thesis_id = str(outcome_row.get("bilateral_thesis_id") or "")
        matrix_row = matrix.get(thesis_id)
        scaffold_row = scaffold.get(thesis_id)
        prediction_row = predictions.get(thesis_id)
        if not matrix_row or not scaffold_row or not prediction_row:
            counts["missing_exact_thesis_join"] += 1
            continue
        if matrix_row.get("thesis_role") != "observed":
            counts["opposite_thesis_excluded"] += 1
            continue
        identity_fields = ("opportunity_event_id", "dedupe_cluster_id", "side")
        if any(str(outcome_row.get(key)) != str(matrix_row.get(key)) for key in identity_fields):
            raise ValueError(f"identity mismatch for observed thesis {thesis_id}")
        if prediction_row.get("prediction_status") != "raw_available":
            counts["raw_prediction_unavailable"] += 1
            continue
        prediction = prediction_row.get("prediction") if isinstance(prediction_row.get("prediction"), dict) else {}
        raw_probability = _number(prediction.get("success_probability"), -1.0)
        if not 0.0 <= raw_probability <= 1.0:
            counts["raw_probability_invalid"] += 1
            continue
        mode = str(outcome_row.get("mode") or "")
        if mode not in models:
            counts["mode_without_train_calibration"] += 1
            continue
        calibration_row = calibrated_rows.get((thesis_id, mode))
        if role == "development":
            if not calibration_row:
                counts["development_without_calibration_fold"] += 1
                continue
            fold = str(calibration_row.get("fold") or "")
            calibrated_probability = _number(calibration_row.get("calibrated_probability"), -1.0)
            if fold not in {"train", "validation"} or not 0.0 <= calibrated_probability <= 1.0:
                counts["development_calibration_invalid"] += 1
                continue
        else:
            fold = "temporal_validation"
            calibrated_probability = _platt_probability(raw_probability, models[mode])
        result = outcome_row.get("outcome") if isinstance(outcome_row.get("outcome"), dict) else {}
        geometry = outcome_row.get("geometry") if isinstance(outcome_row.get("geometry"), dict) else {}
        contract = next((item for item in scaffold_row.get("contracts", []) if item.get("mode") == mode), None)
        contract_geometry = (contract or {}).get("geometry") or {}
        if result.get("label_valid") is not True:
            counts["invalid_or_censored_label"] += 1
            continue
        if outcome_row.get("label_status") != "experimental_scaffold_outcome_not_executable_fill" or outcome_row.get("executable_at_decision") is not False:
            raise ValueError(f"outcome is not explicitly non-executable for {thesis_id}:{mode}")
        if geometry.get("geometry_valid") is not True or contract_geometry.get("geometry_valid") is not True:
            counts["geometry_invalid"] += 1
            continue
        entry = _number(geometry.get("entry_price"))
        stop = _number(geometry.get("stop_price"))
        target = _number(geometry.get("target_price"))
        if entry <= 0 or stop <= 0 or target <= 0:
            counts["geometry_prices_invalid"] += 1
            continue
        side = str(outcome_row.get("side") or "").upper()
        if side == "LONG":
            reward_pct = (target - entry) / entry * 100.0
            risk_pct = (entry - stop) / entry * 100.0
        elif side == "SHORT":
            reward_pct = (entry - target) / entry * 100.0
            risk_pct = (stop - entry) / entry * 100.0
        else:
            counts["side_invalid"] += 1
            continue
        if reward_pct <= 0 or risk_pct <= 0:
            counts["geometry_orientation_invalid"] += 1
            continue
        quantity = _number(result.get("quantity"), 1.0)
        notional = entry * quantity
        if notional <= 0:
            counts["notional_invalid"] += 1
            continue
        gross_pnl_per_100 = _number(result.get("gross_pnl_usdt", result.get("net_pnl_usdt"))) / notional * 100.0
        feature_snapshot = matrix_row.get("feature_snapshot") if isinstance(matrix_row.get("feature_snapshot"), dict) else {}
        candidates.append({
            "candidate_id": f"{thesis_id}:{mode}",
            "bilateral_thesis_id": thesis_id,
            "opportunity_event_id": outcome_row.get("opportunity_event_id"),
            "dedupe_cluster_id": outcome_row.get("dedupe_cluster_id"),
            "window_id": outcome_row.get("window_id"),
            "window_role": role,
            "decision_time": float(outcome_row.get("decision_time") or 0.0),
            "entry_time": float(result.get("entry_time") or outcome_row.get("decision_time") or 0.0),
            "exit_time": float(result.get("exit_time") or outcome_row.get("decision_time") or 0.0),
            "symbol": str(outcome_row.get("symbol") or ""),
            "family": str(matrix_row.get("philosophy_family") or "unknown"),
            "variant": str(matrix_row.get("philosophy_variant") or "unknown"),
            "side": side,
            "mode": mode,
            "thesis_type": str(outcome_row.get("thesis_type") or ""),
            "fold": fold,
            "raw_probability": raw_probability,
            "calibrated_probability": calibrated_probability,
            "raw_expected_pnl_per_100": _number(prediction.get("expected_pnl")) * 100.0 / max(_number(prediction.get("position_size_usdt"), 100.0), 1.0),
            "quality": _family_quality(str(matrix_row.get("philosophy_family") or ""), matrix_row, prediction),
            "risk_pct": risk_pct,
            "reward_pct": reward_pct,
            "gross_pnl_per_100": gross_pnl_per_100,
            "simulated_outcome": str(result.get("outcome") or "unknown"),
            "feature_snapshot": feature_snapshot,
            "prediction_payload": prediction,
        })
        counts["eligible_simulated_contract_rows"] += 1
    return candidates, dict(counts)


def _mean_std(values: List[float]) -> Tuple[float, float]:
    if not values:
        return 0.0, 1.0
    mean = sum(values) / len(values)
    deviation = statistics.pstdev(values) if len(values) > 1 else 1.0
    return mean, deviation if deviation > 1e-12 else 1.0


def _training_stats(training_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_family_mode: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    by_mode: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in training_rows:
        by_family_mode[(row["family"], row["mode"])].append(row)
        by_mode[row["mode"]].append(row)
    stats: Dict[str, Any] = {}
    for key, rows in by_family_mode.items():
        selected = rows if len({r["dedupe_cluster_id"] for r in rows}) >= 30 else by_mode[key[1]]
        tp = sum(row["simulated_outcome"] == "TP" for row in selected)
        n = len(selected)
        fee_bps_per_fill = 10.0 if key[1] == "SPOT_LONG" else 5.0
        predictor_evs = [
            row["calibrated_probability"] * row["reward_pct"]
            - (1.0 - row["calibrated_probability"]) * row["risk_pct"]
            - 100.0 * (2.0 * fee_bps_per_fill) / 10000.0
            for row in selected
        ]
        values = {
            "base_probability": (tp + 1.0) / (n + 2.0),
            "base_probability_rows": n,
            "base_probability_source": "family_mode_train" if selected is rows else "mode_train_fallback",
            "return_sigma": statistics.pstdev([row["gross_pnl_per_100"] for row in selected]) if n > 1 else 0.0,
            "train_clusters": len({r["dedupe_cluster_id"] for r in selected}),
            "predictor_ev_mean": statistics.mean(predictor_evs) if predictor_evs else 0.0,
            "predictor_ev_std": statistics.pstdev(predictor_evs) if len(predictor_evs) > 1 else 1.0,
        }
        for feature in ("raw_expected_pnl_per_100", "calibrated_probability", "quality", "risk_pct"):
            values[f"{feature}_mean"], values[f"{feature}_std"] = _mean_std([_number(row.get(feature)) for row in selected])
        stats[f"{key[0]}|{key[1]}"] = values
    return stats


def _stats_for(stats: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    key = f"{row['family']}|{row['mode']}"
    return stats.get(key, {
        "base_probability": 0.5, "base_probability_rows": 0, "base_probability_source": "unavailable",
        "return_sigma": 0.0, "train_clusters": 0,
        "raw_expected_pnl_per_100_mean": 0.0, "raw_expected_pnl_per_100_std": 1.0,
        "calibrated_probability_mean": 0.5, "calibrated_probability_std": 1.0,
        "quality_mean": 0.5, "quality_std": 1.0, "risk_pct_mean": 0.0, "risk_pct_std": 1.0,
        "predictor_ev_mean": 0.0, "predictor_ev_std": 1.0,
    })


def _correlation_matrix(training_rows: List[Dict[str, Any]]) -> Dict[Tuple[str, str], float]:
    by_symbol_time: Dict[str, Dict[float, float]] = defaultdict(dict)
    for row in training_rows:
        feature = row.get("feature_snapshot") or {}
        value = feature.get("return_4h")
        if value is not None:
            by_symbol_time[row["symbol"]][row["decision_time"]] = _number(value)
    symbols = sorted(by_symbol_time)
    matrix = {}
    for left_index, left in enumerate(symbols):
        for right in symbols[left_index + 1:]:
            common = sorted(set(by_symbol_time[left]) & set(by_symbol_time[right]))
            if len(common) < 30:
                corr = 0.0
            else:
                left_values = [by_symbol_time[left][t] for t in common]
                right_values = [by_symbol_time[right][t] for t in common]
                left_mean, right_mean = statistics.mean(left_values), statistics.mean(right_values)
                left_sd, right_sd = statistics.pstdev(left_values), statistics.pstdev(right_values)
                denominator = len(common) * left_sd * right_sd
                corr = sum((a - left_mean) * (b - right_mean) for a, b in zip(left_values, right_values)) / denominator if denominator > 1e-15 else 0.0
                corr = max(-1.0, min(1.0, corr))
            matrix[(left, right)] = corr
            matrix[(right, left)] = corr
    return matrix


def score_candidate_components(
    row: Dict[str, Any], *, stats: Dict[str, Any], friction_bps_per_fill: float,
) -> Dict[str, float]:
    train = _stats_for(stats, row)
    fee_bps_per_fill = 10.0 if row["mode"] == "SPOT_LONG" else 5.0
    round_trip_cost_usdt = 100.0 * 2.0 * (fee_bps_per_fill + friction_bps_per_fill) / 10000.0
    target_usdt = row["reward_pct"]
    stop_usdt = row["risk_pct"]
    probability = row["calibrated_probability"]
    raw_ev = row["raw_expected_pnl_per_100"] - round_trip_cost_usdt
    contract_ev = train["base_probability"] * target_usdt - (1.0 - train["base_probability"]) * stop_usdt - round_trip_cost_usdt
    predictor_ev = probability * target_usdt - (1.0 - probability) * stop_usdt - round_trip_cost_usdt
    risk_z = (row["risk_pct"] - train["risk_pct_mean"]) / train["risk_pct_std"]
    quality_z = (row["quality"] - train["quality_mean"]) / train["quality_std"]
    probability_z = (probability - train["calibrated_probability_mean"]) / train["calibrated_probability_std"]
    ev_z = (predictor_ev - train.get("predictor_ev_mean", 0.0)) / max(train.get("predictor_ev_std", 1.0), 1e-12)
    return {
        "raw_predictor_ev_per_100": raw_ev,
        "training_base_rate_contract_ev_per_100": contract_ev,
        "predictor_calibrated_ev_per_100": predictor_ev,
        "robust_lcb_per_100": predictor_ev - 1.28 * train["return_sigma"],
        "linear_additive_utility": 0.55 * ev_z + 0.20 * probability_z + 0.15 * quality_z - 0.10 * risk_z,
        "multiplicative_quality": max(0.01, probability) * max(0.05, row["quality"]) * (row["reward_pct"] / max(row["risk_pct"], 1e-9)) / (1.0 + row["risk_pct"]),
        "base_probability": train["base_probability"],
        "train_return_sigma": train["return_sigma"],
        "train_clusters": float(train["train_clusters"]),
        "round_trip_cost_usdt_per_100": round_trip_cost_usdt,
    }


def _dominates(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    left_values = (left["_components"]["predictor_calibrated_ev_per_100"], left["calibrated_probability"], left["quality"], -left["risk_pct"])
    right_values = (right["_components"]["predictor_calibrated_ev_per_100"], right["calibrated_probability"], right["quality"], -right["risk_pct"])
    return all(a >= b for a, b in zip(left_values, right_values)) and any(a > b for a, b in zip(left_values, right_values))


def _pareto_order(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    remaining = list(rows)
    ordered = []
    front = 0
    while remaining:
        current = [row for row in remaining if not any(_dominates(other, row) for other in remaining if other is not row)]
        current.sort(key=lambda row: (-row["_components"]["predictor_calibrated_ev_per_100"], -row["calibrated_probability"], row["symbol"], row["candidate_id"]))
        for row in current:
            row["_pareto_front"] = front
        ordered.extend(current)
        current_ids = {id(row) for row in current}
        remaining = [row for row in remaining if id(row) not in current_ids]
        front += 1
    return ordered


def _outranking_order(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def dimensions(row: Dict[str, Any]) -> Tuple[float, ...]:
        c = row["_components"]
        return (c["predictor_calibrated_ev_per_100"], row["calibrated_probability"], row["quality"], -row["risk_pct"])
    scores = {}
    for left in rows:
        left_values = dimensions(left)
        count = 0
        for right in rows:
            if left is right:
                continue
            right_values = dimensions(right)
            differences = [a - b for a, b in zip(left_values, right_values)]
            if sum(value >= -1e-9 for value in differences) >= 3 and sum(value > 1e-9 for value in differences) >= 2:
                count += 1
        scores[left["candidate_id"]] = count
    return sorted(rows, key=lambda row: (-scores[row["candidate_id"]], -row["_components"]["predictor_calibrated_ev_per_100"], row["symbol"], row["candidate_id"]))


def _deduplicate_batch(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_symbol = {}
    for row in sorted(rows, key=lambda item: (item["symbol"], item["candidate_id"])):
        by_symbol.setdefault(row["symbol"], row)
    return list(by_symbol.values())


def _select_batch(
    formula: str,
    rows: List[Dict[str, Any]],
    *,
    slots: int,
    correlation: Dict[Tuple[str, str], float],
    seed: int,
) -> Tuple[List[Dict[str, Any]], str]:
    if slots <= 0 or not rows:
        return [], "none"
    rows = _deduplicate_batch(rows)
    if formula in {"baseline_current_universal", "calibrated_qubo"}:
        candidates = []
        for row in rows:
            components = row["_components"]
            expected_pnl = components["raw_predictor_ev_per_100"] if formula == "baseline_current_universal" else components["predictor_calibrated_ev_per_100"]
            peak_quality = _number(row["prediction_payload"].get("peak_quality_score")) if formula == "baseline_current_universal" else row["quality"] * 100.0
            candidates.append(Candidate(
                symbol=row["symbol"], expected_pnl=expected_pnl,
                success_probability=row["calibrated_probability"],
                risk_score=max(0.0, min(100.0, row["risk_pct"] * 10.0)),
                peak_quality_score=peak_quality,
                metadata={"candidate_id": row["candidate_id"]},
            ))
        result = select_portfolio(
            candidates, correlation, min(slots, len(candidates)),
            lambda_corr=1.0, lambda_size=5.0, lambda_risk=0.5,
            solver="sqa", population_size=8, iterations=12, seed=seed,
        )
        selected = sorted(
            result.selected,
            key=lambda candidate: (
                -candidate.expected_pnl / (1.0 + candidate.risk_score / 100.0),
                candidate.symbol,
            ),
        )[:slots]
        selected_ids = {candidate.metadata["candidate_id"] for candidate in selected}
        return [row for row in rows if row["candidate_id"] in selected_ids], f"{result.solver}_capped_to_{slots}"
    if formula == "marginal_portfolio_utility":
        remaining = list(rows)
        selected = []
        while remaining and len(selected) < slots:
            scored = []
            for row in remaining:
                c = row["_components"]
                corr_penalty = sum(max(0.0, correlation.get((row["symbol"], picked["symbol"]), 0.0)) for picked in selected)
                marginal = c["predictor_calibrated_ev_per_100"] - 0.15 * row["risk_pct"] - 0.20 * corr_penalty
                scored.append((marginal, row["symbol"], row["candidate_id"], row))
            chosen = max(scored, key=lambda item: (item[0], item[1], item[2]))[3]
            selected.append(chosen)
            remaining.remove(chosen)
        return selected, "deterministic_marginal_greedy"
    if formula == "pareto_frontier":
        return _pareto_order(rows)[:slots], "nondominated_fronts"
    if formula == "constraint_outranking":
        return _outranking_order(rows)[:slots], "pairwise_outranking"
    score_key = {
        "linear_additive_utility": "linear_additive_utility",
        "multiplicative_quality": "multiplicative_quality",
        "executable_contract_ev": "training_base_rate_contract_ev_per_100",
        "predictor_calibrated_ev": "predictor_calibrated_ev_per_100",
        "robust_lower_confidence_ev": "robust_lcb_per_100",
    }.get(formula)
    if score_key is None:
        raise ValueError(f"unknown formula arm: {formula}")
    return sorted(rows, key=lambda row: (-row["_components"][score_key], row["symbol"], row["candidate_id"]))[:slots], "deterministic_rank"


def _candidate_inputs(
    run_dir: Path, role: str, calibration_report: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    role_root = run_dir / "replay" / role
    matrix_rows = _read_jsonl(role_root / "bilateral_thesis_matrix.jsonl")
    scaffold_rows = _read_jsonl(role_root / "bilateral_contract_scaffold.jsonl")
    outcome_rows = _read_jsonl(role_root / "bilateral_scaffold_outcomes.jsonl")
    prediction_rows = _read_jsonl(role_root / "bilateral_raw_predictions.jsonl")
    matrix = {row["bilateral_thesis_id"]: row for row in matrix_rows}
    scaffold = {row["bilateral_thesis_id"]: row for row in scaffold_rows}
    predictions = {row["bilateral_thesis_id"]: row for row in prediction_rows}
    calibrated_rows: Dict[Tuple[str, str], Dict[str, Any]] = {}
    calibrated_path = Path(str(calibration_report["output_predictions_path"]))
    if not calibrated_path.is_absolute():
        calibrated_path = REPO_ROOT / calibrated_path
    if not calibrated_path.is_file() or _sha256(calibrated_path) != calibration_report.get("output_predictions_sha256"):
        raise ValueError("development calibrated predictions are missing or hash-mismatched")
    for row in _read_jsonl(calibrated_path):
        calibrated_rows[(str(row.get("bilateral_thesis_id") or ""), str(row.get("mode") or ""))] = row

    models = calibration_report.get("models_by_venue", {})
    counts: Counter[str] = Counter()
    candidates = []
    for outcome_row in outcome_rows:
        counts["outcome_rows"] += 1
        thesis_id = str(outcome_row.get("bilateral_thesis_id") or "")
        mode = str(outcome_row.get("mode") or "")
        matrix_row = matrix.get(thesis_id)
        scaffold_row = scaffold.get(thesis_id)
        prediction_row = predictions.get(thesis_id)
        if not matrix_row or not scaffold_row or not prediction_row:
            counts["missing_exact_thesis_join"] += 1
            continue
        if matrix_row.get("thesis_role") != "observed":
            counts["opposite_thesis_excluded"] += 1
            continue
        if any(str(outcome_row.get(key)) != str(matrix_row.get(key)) for key in ("opportunity_event_id", "dedupe_cluster_id", "side")):
            raise ValueError(f"event/cluster/side mismatch for observed thesis {thesis_id}")
        contract = next((item for item in scaffold_row.get("contracts", []) if item.get("mode") == mode), None)
        if contract is None:
            counts["contract_mode_missing"] += 1
            continue
        prediction = prediction_row.get("prediction") if isinstance(prediction_row.get("prediction"), dict) else {}
        raw_probability = _number(prediction.get("success_probability"), -1.0)
        if prediction_row.get("prediction_status") != "raw_available" or not 0.0 <= raw_probability <= 1.0:
            counts["raw_prediction_unavailable"] += 1
            continue
        model = models.get(mode)
        if model is None:
            counts["mode_without_development_calibrator"] += 1
            continue
        if role == "development":
            calibrated = calibrated_rows.get((thesis_id, mode))
            if calibrated is None:
                counts["development_calibration_row_missing"] += 1
                continue
            fold = str(calibrated.get("fold") or "")
            probability = _number(calibrated.get("calibrated_probability"), -1.0)
            if fold not in {"train", "validation"} or not 0.0 <= probability <= 1.0:
                counts["development_calibration_invalid"] += 1
                continue
        else:
            fold = "temporal_validation"
            probability = _platt_probability(raw_probability, model)

        result = outcome_row.get("outcome") if isinstance(outcome_row.get("outcome"), dict) else {}
        geometry = outcome_row.get("geometry") if isinstance(outcome_row.get("geometry"), dict) else {}
        contract_geometry = contract.get("geometry") if isinstance(contract.get("geometry"), dict) else {}
        if result.get("label_valid") is not True:
            counts["invalid_or_censored_label"] += 1
            continue
        if outcome_row.get("label_status") != "experimental_scaffold_outcome_not_executable_fill" or outcome_row.get("executable_at_decision") is not False:
            raise ValueError(f"outcome row is not explicitly non-executable: {thesis_id}:{mode}")
        if geometry.get("geometry_valid") is not True or contract_geometry.get("geometry_valid") is not True:
            counts["geometry_invalid"] += 1
            continue
        entry, stop, target = (_number(geometry.get(key)) for key in ("entry_price", "stop_price", "target_price"))
        if min(entry, stop, target) <= 0:
            counts["geometry_price_invalid"] += 1
            continue
        side = str(outcome_row.get("side") or "").upper()
        if side == "LONG":
            reward_pct, risk_pct = (target - entry) / entry * 100.0, (entry - stop) / entry * 100.0
        elif side == "SHORT":
            reward_pct, risk_pct = (entry - target) / entry * 100.0, (stop - entry) / entry * 100.0
        else:
            counts["side_invalid"] += 1
            continue
        if min(reward_pct, risk_pct) <= 0:
            counts["geometry_orientation_invalid"] += 1
            continue
        notional = entry * _number(result.get("quantity"), 1.0)
        if notional <= 0:
            counts["notional_invalid"] += 1
            continue
        features = matrix_row.get("feature_snapshot") if isinstance(matrix_row.get("feature_snapshot"), dict) else {}
        candidates.append({
            "candidate_id": f"{thesis_id}:{mode}",
            "bilateral_thesis_id": thesis_id,
            "opportunity_event_id": outcome_row.get("opportunity_event_id"),
            "dedupe_cluster_id": outcome_row.get("dedupe_cluster_id"),
            "window_role": role,
            "decision_time": float(outcome_row.get("decision_time") or 0.0),
            "exit_time": float(result.get("exit_time") or outcome_row.get("decision_time") or 0.0),
            "symbol": str(outcome_row.get("symbol") or ""),
            "family": CANONICAL_FAMILY_MAP.get(str(matrix_row.get("philosophy_family") or ""), "unknown"),
            "source_family": str(matrix_row.get("philosophy_family") or "unknown"),
            "variant": str(matrix_row.get("philosophy_variant") or "unknown"),
            "side": side,
            "mode": mode,
            "thesis_type": str(outcome_row.get("thesis_type") or ""),
            "fold": fold,
            "raw_probability": raw_probability,
            "calibrated_probability": probability,
            "raw_expected_pnl_per_100": _number(prediction.get("expected_pnl")) * 100.0 / max(_number(prediction.get("position_size_usdt"), 100.0), 1.0),
            "quality": _family_quality(str(matrix_row.get("philosophy_family") or ""), matrix_row, prediction),
            "risk_pct": risk_pct,
            "reward_pct": reward_pct,
            "gross_pnl_per_100": _number(result.get("gross_pnl_usdt", result.get("net_pnl_usdt"))) / notional * 100.0,
            "simulated_outcome": str(result.get("outcome") or "unknown"),
            "feature_snapshot": features,
            "prediction_payload": prediction,
            "label_is_executable": False,
        })
        counts["eligible_simulated_contract_rows"] += 1
    return candidates, dict(counts)


def _training_stats(training_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_family_mode: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    by_mode: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in training_rows:
        by_family_mode[(row["family"], row["mode"])].append(row)
        by_mode[row["mode"]].append(row)
    result = {}
    for (family, mode), family_rows in by_family_mode.items():
        fit_rows = family_rows if len({row["dedupe_cluster_id"] for row in family_rows}) >= 30 else by_mode[mode]
        tp_count = sum(row["simulated_outcome"] == "TP" for row in fit_rows)
        fee_bps_per_fill = 10.0 if mode == "SPOT_LONG" else 5.0
        expected_values = [
            row["calibrated_probability"] * row["reward_pct"]
            - (1.0 - row["calibrated_probability"]) * row["risk_pct"]
            - 100.0 * (2.0 * fee_bps_per_fill) / 10000.0
            for row in fit_rows
        ]
        stats = {
            "base_probability": (tp_count + 1.0) / (len(fit_rows) + 2.0),
            "base_probability_rows": len(fit_rows),
            "base_probability_source": "family_mode_train" if fit_rows is family_rows else "mode_train_fallback",
            "train_clusters": len({row["dedupe_cluster_id"] for row in fit_rows}),
            "predictor_ev_mean": statistics.mean(expected_values) if expected_values else 0.0,
            "predictor_ev_std": statistics.pstdev(expected_values) if len(expected_values) > 1 else 1.0,
            "return_sigma": statistics.pstdev([row["gross_pnl_per_100"] for row in fit_rows]) if len(fit_rows) > 1 else 0.0,
        }
        for feature in ("calibrated_probability", "quality", "risk_pct"):
            values = [_number(row[feature]) for row in fit_rows]
            mean = statistics.mean(values) if values else 0.0
            deviation = statistics.pstdev(values) if len(values) > 1 else 1.0
            stats[f"{feature}_mean"] = mean
            stats[f"{feature}_std"] = deviation if deviation > 1e-12 else 1.0
        result[f"{family}|{mode}"] = stats
    return result


def _training_correlation(training_rows: List[Dict[str, Any]]) -> Dict[Tuple[str, str], float]:
    by_symbol_time: Dict[str, Dict[float, float]] = defaultdict(dict)
    for row in training_rows:
        returns = row.get("feature_snapshot") if isinstance(row.get("feature_snapshot"), dict) else {}
        if returns.get("return_4h") is not None:
            by_symbol_time[row["symbol"]][row["decision_time"]] = _number(returns["return_4h"])
    symbols = sorted(by_symbol_time)
    matrix = {}
    for index, left in enumerate(symbols):
        for right in symbols[index + 1:]:
            common = sorted(set(by_symbol_time[left]) & set(by_symbol_time[right]))
            if len(common) < 30:
                corr = 0.0
            else:
                left_values = [by_symbol_time[left][time] for time in common]
                right_values = [by_symbol_time[right][time] for time in common]
                left_mean, right_mean = statistics.mean(left_values), statistics.mean(right_values)
                left_sd, right_sd = statistics.pstdev(left_values), statistics.pstdev(right_values)
                denominator = len(common) * left_sd * right_sd
                corr = sum((a - left_mean) * (b - right_mean) for a, b in zip(left_values, right_values)) / denominator if denominator > 1e-15 else 0.0
                corr = max(-1.0, min(1.0, corr))
            matrix[(left, right)] = corr
            matrix[(right, left)] = corr
    return matrix


def _cluster_net_returns(
    candidates: List[Dict[str, Any]], selected: List[Dict[str, Any]], net_by_candidate: Dict[str, float],
) -> Dict[str, float]:
    returns = {str(row["dedupe_cluster_id"]): 0.0 for row in candidates}
    for row in selected:
        cluster = str(row["dedupe_cluster_id"])
        returns[cluster] = returns.get(cluster, 0.0) + float(net_by_candidate[row["candidate_id"]])
    return returns


def _score_job(job: Dict[str, Any]) -> Dict[str, Any]:
    role, family = job["role"], job["family"]
    rows = job["rows"]
    if role == "development":
        rows = [row for row in rows if row["fold"] == "validation"]
    training_stats = job["training_stats"]
    correlation = job["correlation"]
    batches: Dict[Tuple[float, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        row["_components"] = score_candidate_components(row, stats=training_stats, friction_bps_per_fill=0.0)
        batches[(row["decision_time"], row["mode"])].append(row)

    summaries: Dict[str, Dict[str, Any]] = {}
    cluster_returns: Dict[str, Dict[str, float]] = {}
    selected_ids: Dict[str, set[str]] = {formula: set() for formula in FORMULAS}
    solver_counts: Counter[str] = Counter()
    for batch_number, ((decision_time, mode), batch_rows) in enumerate(sorted(batches.items())):
        slots = SLOT_CAPACITY if mode == "SPOT_LONG" else min(SLOT_CAPACITY, FUTURES_MAX_NEW_PER_CYCLE)
        for formula in FORMULAS:
            selected, solver_name = _select_batch(
                formula, batch_rows, slots=slots, correlation=correlation,
                seed=_stable_seed(role, family, decision_time, mode, formula, job["seed"]),
            )
            solver_counts[solver_name] += 1
            selected_ids[formula].update(row["candidate_id"] for row in selected)
            for slippage_bps in FRICTION_BPS_PER_FILL:
                key = f"{formula}|{mode}|slippage_{slippage_bps:g}bps_per_fill"
                fee_bps_per_fill = 10.0 if mode == "SPOT_LONG" else 5.0
                state = summaries.setdefault(key, {
                    "formula": formula, "mode": mode, "slippage_bps_per_fill": slippage_bps,
                    "fee_bps_per_fill": fee_bps_per_fill, "candidate_pool_rows": 0,
                    "selected_count": 0, "net_pnl_per_100_sum": 0.0, "gross_pnl_per_100_sum": 0.0,
                    "gross_profit_per_100": 0.0, "gross_loss_per_100": 0.0, "net_wins": 0,
                    "selected_clusters": set(), "simulated_outcomes": Counter(),
                })
                state["candidate_pool_rows"] += len(batch_rows)
                round_trip_cost = 100.0 * 2.0 * (fee_bps_per_fill + slippage_bps) / 10000.0
                net_by_candidate = {
                    row["candidate_id"]: row["gross_pnl_per_100"] - round_trip_cost
                    for row in selected
                }
                batch_cluster_returns = _cluster_net_returns(batch_rows, selected, net_by_candidate)
                target_cluster_returns = cluster_returns.setdefault(key, {})
                for cluster, net in batch_cluster_returns.items():
                    target_cluster_returns[cluster] = target_cluster_returns.get(cluster, 0.0) + net
                for row in selected:
                    net = row["gross_pnl_per_100"] - round_trip_cost
                    state["selected_count"] += 1
                    state["gross_pnl_per_100_sum"] += row["gross_pnl_per_100"]
                    state["net_pnl_per_100_sum"] += net
                    state["gross_profit_per_100"] += max(0.0, net)
                    state["gross_loss_per_100"] += abs(min(0.0, net))
                    state["net_wins"] += int(net > 0)
                    state["selected_clusters"].add(row["dedupe_cluster_id"])
                    state["simulated_outcomes"][row["simulated_outcome"]] += 1

    for state in summaries.values():
        count = state["selected_count"]
        state["mean_net_pnl_per_100"] = state["net_pnl_per_100_sum"] / count if count else None
        state["mean_gross_pnl_per_100"] = state["gross_pnl_per_100_sum"] / count if count else None
        state["net_win_rate"] = state["net_wins"] / count if count else None
        state["net_profit_factor"] = state["gross_profit_per_100"] / state["gross_loss_per_100"] if state["gross_loss_per_100"] else None
        state["unique_selected_clusters"] = len(state.pop("selected_clusters"))
        state["simulated_outcomes"] = dict(state["simulated_outcomes"])
        state["metrics_are_simulated_non_executable"] = True
    return {
        "role": role,
        "family": family,
        "source_families": sorted({row["source_family"] for row in rows}),
        "candidate_rows_scored": len(rows),
        "batches": len(batches),
        "solver_counts": dict(solver_counts),
        "arm_summaries": summaries,
        "cluster_net_pnl": cluster_returns,
        "selected_candidate_ids": {formula: sorted(ids) for formula, ids in selected_ids.items()},
    }


def _bootstrap_paired_difference(
    baseline: Dict[str, float], challenger: Dict[str, float], *, seed: int,
    replicates: int = BOOTSTRAP_REPLICATES,
) -> Dict[str, Any]:
    shared = sorted(set(baseline) & set(challenger))
    if len(shared) < 2:
        return {"clusters": len(shared), "mean_difference_net_pnl_per_100": None, "ci_95": None}
    differences = [challenger[key] - baseline[key] for key in shared]
    rng = random.Random(seed)
    samples = sorted(statistics.mean(rng.choices(differences, k=len(differences))) for _ in range(replicates))
    return {
        "clusters": len(shared),
        "mean_difference_net_pnl_per_100": statistics.mean(differences),
        "cluster_bootstrap_ci_95": [samples[int(0.025 * (replicates - 1))], samples[int(0.975 * (replicates - 1))]],
        "replicates": replicates,
    }


def _summary_for_role(role: str, shards: List[Dict[str, Any]], *, seed: int, replicates: int) -> Dict[str, Any]:
    arms: Dict[str, Dict[str, Any]] = {}
    paired: Dict[str, Any] = {}
    for shard in shards:
        for key, value in shard["arm_summaries"].items():
            formula, mode, scenario = key.split("|", 2)
            arm_key = f"{shard['family']}|{formula}|{mode}|{scenario}"
            arms[arm_key] = {
                **value,
                "family": shard["family"],
                "source_families": shard.get("source_families", []),
                "role": role,
                "labels_are_executable_fills": False,
            }
        for key, challenger in shard["cluster_net_pnl"].items():
            formula, mode, scenario = key.split("|", 2)
            if formula == "baseline_current_universal":
                continue
            baseline_key = f"baseline_current_universal|{mode}|{scenario}"
            paired[f"{shard['family']}|{key}"] = _bootstrap_paired_difference(
                shard["cluster_net_pnl"].get(baseline_key, {}),
                challenger,
                seed=_stable_seed(seed, role, shard["family"], key),
                replicates=replicates,
            )
    return {
        "artifact_version": "universal_entry_fast_formula_screen_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "role": role,
        "family_shards_completed": len(shards),
        "arms": arms,
        "paired_cluster_bootstrap_vs_baseline": paired,
        "metrics_are_simulated_non_executable": True,
        "winner_claim_allowed": False,
        "staging_touched": False,
        "orders_submitted": False,
        "limitations": [
            "All labels are experimental scaffold outcomes, not exchange fills.",
            "Historical filters/account states are not reconstructed; no formula is an executable-contract winner.",
            "Current sandbox fee schedules are proxies; slippage is scenario-based; historical funding is omitted in this fast screen.",
            "The baseline arm reconstructs runtime QUBO with raw predictor EV; peak-quality is absent and defaults to zero, so the control is a proxy.",
            "Selection is stratified by philosophy, timestamp and venue mode; it does not compare cross-venue routing.",
            "Overlapping four-hour labels are not compounded into an equity curve.",
            "Carry, volatility-risk-premium, statistical-arbitrage and microstructure families are pending in this fast tape.",
        ],
    }


def run_fast_formula_screen(
    run_dir: Path,
    *,
    screen_name: str = "formula_screen",
    requested_workers: int = 8,
    reserve_gb: float = 2.5,
    estimated_worker_gb: float = 0.75,
    resume: bool = False,
    memory_poll_seconds: float = 2.0,
    memory_wait_timeout_minutes: float = 60.0,
    bootstrap_replicates: int = BOOTSTRAP_REPLICATES,
) -> Dict[str, Any]:
    run_dir = Path(run_dir)
    calibration_paths = sorted(
        (run_dir / "replay" / "development").glob("bilateral_replay_calibration_*.report.json"),
        key=lambda path: path.stat().st_mtime,
    )
    if not calibration_paths:
        raise FileNotFoundError("development-only replay calibration report is required")
    calibration_path = calibration_paths[-1]
    calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
    if calibration.get("usable_for_formula_scoring") is not False or calibration.get("usable_for_live_execution") is not False:
        raise ValueError("calibration report must remain explicitly simulation-only")
    for mode, model in calibration.get("models_by_venue", {}).items():
        if model.get("converged") is not True or not math.isfinite(float(model.get("slope", float("nan")))):
            raise ValueError(f"development calibration model is invalid for {mode}")

    source_paths = []
    for role in ("development", "validation"):
        role_root = run_dir / "replay" / role
        source_paths.extend(role_root / f"bilateral_{name}.jsonl" for name in (
            "thesis_matrix", "contract_scaffold", "scaffold_outcomes", "raw_predictions",
        ))
        source_paths.append(run_dir / role / "philosophy_proposals.jsonl")
    input_hashes = {str(path.relative_to(REPO_ROOT)).replace("\\", "/"): _sha256(path) for path in source_paths if path.is_file()}
    if len(input_hashes) != len(source_paths):
        raise FileNotFoundError("a frozen proposal/replay input is missing")
    manifest = {
        "scope": "fast_simulated_same_tape_formula_screen",
        "source_run_id": run_dir.name,
        "input_sha256": input_hashes,
        "calibration_report_path": str(calibration_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "calibration_report_sha256": _sha256(calibration_path),
        "scorer_sha256": _sha256(Path(__file__).resolve()),
        "screen_name": screen_name,
        "formula_arms": list(FORMULAS),
        "slippage_bps_per_fill": list(FRICTION_BPS_PER_FILL),
        "spot_slot_capacity": SLOT_CAPACITY,
        "futures_new_per_cycle": FUTURES_MAX_NEW_PER_CYCLE,
        "adaptive_formula_included": False,
        "execution_enabled": False,
    }
    score_root = run_dir / screen_name
    ledger = ProgressLedger(
        score_root,
        run_id=f"{run_dir.name}-{screen_name.replace('_', '-')}",
        manifest=manifest,
        resume=resume,
    )

    source_rows: Dict[str, List[Dict[str, Any]]] = {}
    input_counts: Dict[str, Dict[str, int]] = {}
    for role in ("development", "validation"):
        source_rows[role], input_counts[role] = _candidate_inputs(run_dir, role, calibration)
    training_rows = [row for row in source_rows["development"] if row["fold"] == "train"]
    training_clusters = len({row["dedupe_cluster_id"] for row in training_rows})
    if training_clusters < 30:
        raise ValueError(f"fewer than 30 independent development training clusters: {training_clusters}")
    training_stats = _training_stats(training_rows)
    correlation = _training_correlation(training_rows)

    roles = ("development_train", "development_holdout", "validation")
    source_role = {"development_train": "development", "development_holdout": "development", "validation": "validation"}
    role_rows: Dict[str, List[Dict[str, Any]]] = {}
    for role in roles:
        rows = source_rows[source_role[role]]
        if role == "development_train":
            rows = [row for row in rows if row["fold"] == "train"]
        elif role == "development_holdout":
            rows = [row for row in rows if row["fold"] == "validation"]
        role_rows[role] = rows
    families = sorted({row["family"] for rows in role_rows.values() for row in rows})
    jobs = []
    for role in roles:
        for family in families:
            rows = [row for row in role_rows[role] if row["family"] == family]
            if rows:
                jobs.append({
                    "job_id": f"{role}:{family}", "role": role, "family": family, "rows": rows,
                    "training_stats": training_stats, "correlation": correlation,
                    "seed": 42,
                })

    ledger.start("same_tape_formula_scoring", total_jobs=len(jobs), requested_workers=requested_workers)
    ledger.state.update({
        "input_row_counts": input_counts,
        "eligible_training_rows": len(training_rows),
        "training_clusters": training_clusters,
        "roles": list(roles),
        "formula_arms": list(FORMULAS),
        "families_in_tape": families,
        "pending_canonical_families": list(PENDING_CANONICAL_FAMILIES),
        "winner_claim_allowed": False,
        "staging_touched": False,
    })
    ledger._save()

    completed = ledger.state.setdefault("completed_jobs", {})
    pending_jobs = []
    for job in jobs:
        path = score_root / job["role"] / f"{job['family']}.json"
        record = completed.get(job["job_id"], {})
        if record.get("status") == "completed" and path.is_file() and _sha256(path) == record.get("result_sha256"):
            continue
        completed.pop(job["job_id"], None)
        pending_jobs.append(job)
    ledger.start("same_tape_formula_scoring", total_jobs=len(jobs), requested_workers=requested_workers)
    ledger.state.update({
        "input_row_counts": input_counts,
        "eligible_training_rows": len(training_rows),
        "training_clusters": training_clusters,
        "roles": list(roles),
        "formula_arms": list(FORMULAS),
        "families_in_tape": families,
        "pending_canonical_families": list(PENDING_CANONICAL_FAMILIES),
        "winner_claim_allowed": False,
        "staging_touched": False,
    })
    ledger._save()

    reserve_bytes = int(reserve_gb * GIB)
    estimated_worker_bytes = int(estimated_worker_gb * GIB)
    wait_deadline = time.monotonic() + memory_wait_timeout_minutes * 60
    pending_index = 0
    active: Dict[Future, Dict[str, Any]] = {}
    failures = []
    completed_shards: Dict[str, List[Dict[str, Any]]] = {role: [] for role in roles}
    with ProcessPoolExecutor(max_workers=requested_workers) as executor:
        while pending_index < len(pending_jobs) or active:
            snapshot = memory_snapshot()
            safe_limit = safe_worker_limit(
                requested_workers,
                available_bytes=snapshot.get("available_bytes"),
                reserve_bytes=reserve_bytes,
                estimated_worker_bytes=estimated_worker_bytes,
            )
            ledger.update_runtime(active_workers=len(active), safe_worker_limit=safe_limit)
            if safe_limit == 0:
                ledger.state["state"] = "waiting_for_memory"
                ledger.state["memory_wait_seconds_remaining"] = max(0, int(wait_deadline - time.monotonic()))
                ledger._save()
                if time.monotonic() >= wait_deadline:
                    ledger.finish(state="blocked_low_memory", error="safe worker memory reserve unavailable before timeout")
                    return ledger.state
                time.sleep(memory_poll_seconds)
                continue
            ledger.state["state"] = "running"
            while pending_index < len(pending_jobs) and len(active) < safe_limit:
                job = pending_jobs[pending_index]
                pending_index += 1
                active[executor.submit(_score_job, job)] = job
                ledger.state["current_job"] = job["job_id"]
                ledger.update_runtime(active_workers=len(active), safe_worker_limit=safe_limit)
            done, _ = wait(active, timeout=memory_poll_seconds, return_when=FIRST_COMPLETED)
            for future in done:
                job = active.pop(future)
                try:
                    result = future.result()
                    output = score_root / job["role"] / f"{job['family']}.json"
                    _atomic_json(output, result)
                    ledger.complete_job(
                        job["job_id"],
                        checkpoint=str(output.relative_to(REPO_ROOT)).replace("\\", "/"),
                        result_sha256=_sha256(output),
                    )
                    completed_shards[job["role"]].append(result)
                    ledger.state["last_completed_job"] = job["job_id"]
                    ledger.state.pop("current_job", None)
                    ledger._save()
                except Exception as exc:
                    reason = f"{type(exc).__name__}: {str(exc)[:300]}"
                    failures.append(job["job_id"])
                    ledger.fail_job(job["job_id"], reason)

    for job in jobs:
        if job["job_id"] in completed:
            output = score_root / job["role"] / f"{job['family']}.json"
            if output.is_file() and _sha256(output) == completed[job["job_id"]].get("result_sha256"):
                if not any(item["family"] == job["family"] for item in completed_shards[job["role"]]):
                    completed_shards[job["role"]].append(json.loads(output.read_text(encoding="utf-8")))
    if failures:
        ledger.finish(state="partial", error=f"formula scoring shards failed: {', '.join(failures)}")
        return ledger.state

    reports = {}
    for role in roles:
        report = _summary_for_role(role, completed_shards[role], seed=42, replicates=bootstrap_replicates)
        report.update({
            "run_id": run_dir.name,
            "input_sha256": input_hashes,
            "calibration_report_path": manifest["calibration_report_path"],
            "calibration_report_sha256": manifest["calibration_report_sha256"],
            "training_clusters": training_clusters,
            "training_stats": training_stats,
            "selection_policy": {
                "family_grouping": True,
                "decision_time_grouping": True,
                "venue_mode_grouping": True,
                "spot_long_slot_capacity": SLOT_CAPACITY,
                "futures_per_mode_cycle_slot_capacity": FUTURES_MAX_NEW_PER_CYCLE,
                "identical_candidates_for_all_formula_arms": True,
            },
        })
        report_path = score_root / f"{role}_formula_comparison.report.json"
        _atomic_json(report_path, report)
        reports[role] = str(report_path.relative_to(REPO_ROOT)).replace("\\", "/")

    summary = {
        "artifact_version": "universal_entry_fast_formula_screen_v1",
        "run_id": run_dir.name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phase": "same_tape_formula_screen_complete",
        "state": "simulation_screen_complete_review_required",
        "formula_arms": list(FORMULAS),
        "roles": list(roles),
        "family_results": reports,
        "families_in_tape": families,
        "pending_canonical_families": list(PENDING_CANONICAL_FAMILIES),
        "input_counts": input_counts,
        "training_clusters": training_clusters,
        "metrics_are_simulated_non_executable": True,
        "winner_claim_allowed": False,
        "staging_touched": False,
        "orders_submitted": False,
        "limitations": [
            "All outcomes are simulated scaffold labels, not exchange-confirmed fills.",
            "Historical filters/account state are unknown; these are not executable-contract winners.",
            "Current sandbox fees are proxies, slippage is scenario-based, and funding is omitted in this fast screen.",
            "The baseline arm is a reconstruction of the current QUBO objective; missing peak-quality values default to zero.",
            "Selection is stratified by family/time/venue and does not compare cross-venue routing.",
            "Four canonical families remain pending in this fast tape: carry, volatility-risk-premium, statistical arbitrage and microstructure/order-flow.",
            "Overlapping four-hour labels are not compounded into an equity curve; adaptive selection is not evaluated.",
        ],
    }
    _atomic_json(score_root / "summary.json", summary)
    ledger.state["summary_path"] = str((score_root / "summary.json").relative_to(REPO_ROOT)).replace("\\", "/")
    ledger.state["reports"] = reports
    ledger.state["winner_claim_allowed"] = False
    ledger.state["staging_touched"] = False
    ledger.finish(state="formula_screen_complete_no_winner_claim")
    return ledger.state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--screen-name", default="formula_screen")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-workers", type=int, default=8)
    parser.add_argument("--reserve-gb", type=float, default=2.5)
    parser.add_argument("--estimated-worker-gb", type=float, default=0.75)
    parser.add_argument("--memory-poll-seconds", type=float, default=2.0)
    parser.add_argument("--memory-wait-timeout-minutes", type=float, default=60.0)
    parser.add_argument("--bootstrap-replicates", type=int, default=BOOTSTRAP_REPLICATES)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    args = parser.parse_args()
    run_dir = args.run_root / args.run_id
    if not run_dir.is_dir():
        parser.error(f"formula source run not found: {run_dir}")
    try:
        state = run_fast_formula_screen(
            run_dir,
            screen_name=args.screen_name,
            requested_workers=args.max_workers,
            reserve_gb=args.reserve_gb,
            estimated_worker_gb=args.estimated_worker_gb,
            resume=args.resume,
            memory_poll_seconds=args.memory_poll_seconds,
            memory_wait_timeout_minutes=args.memory_wait_timeout_minutes,
            bootstrap_replicates=args.bootstrap_replicates,
        )
    except Exception as exc:
        parser.error(f"fast formula screen failed: {type(exc).__name__}: {str(exc)[:400]}")
    print(json.dumps({
        "run_id": state.get("run_id"), "state": state.get("state"), "phase": state.get("phase"),
        "counts": state.get("counts"), "workers": state.get("workers"),
        "summary_path": state.get("summary_path"), "winner_claim_allowed": state.get("winner_claim_allowed"),
    }, ensure_ascii=False))
    return 0 if state.get("state") == "formula_screen_complete_no_winner_claim" else 1


if __name__ == "__main__":
    raise SystemExit(main())


def _legacy_duplicate_bootstrap_paired_difference(
    baseline: Dict[str, float], challenger: Dict[str, float], *, seed: int,
    replicates: int = BOOTSTRAP_REPLICATES,
) -> Dict[str, Any]:
    shared = sorted(set(baseline) & set(challenger))
    if len(shared) < 2:
        return {"clusters": len(shared), "mean_difference_net_pnl_per_100": None, "ci_95": None}
    differences = [challenger[key] - baseline[key] for key in shared]
    rng = random.Random(seed)
    samples = sorted(statistics.mean(rng.choices(differences, k=len(differences))) for _ in range(replicates))
    return {
        "clusters": len(shared),
        "mean_difference_net_pnl_per_100": statistics.mean(differences),
        "cluster_bootstrap_ci_95": [samples[int(0.025 * (replicates - 1))], samples[int(0.975 * (replicates - 1))]],
        "replicates": replicates,
    }


def _legacy_duplicate_paired_summaries(shard: Dict[str, Any], *, seed: int, replicates: int) -> Dict[str, Any]:
    baseline_prefix = "baseline_current_universal|"
    results = {}
    for key, candidate_clusters in shard["cluster_net_pnl"].items():
        if not key.startswith(baseline_prefix):
            formula, mode, scenario = key.split("|", 2)
            baseline_key = f"baseline_current_universal|{mode}|{scenario}"
            results[key] = _legacy_duplicate_bootstrap_paired_difference(
                shard["cluster_net_pnl"].get(baseline_key, {}), candidate_clusters,
                seed=_stable_seed(seed, shard["role"], shard["family"], key),
                replicates=replicates,
            )
    return results


def _legacy_duplicate_summary_for_role(role: str, family_shards: List[Dict[str, Any]]) -> Dict[str, Any]:
    arms: Dict[str, Dict[str, Any]] = {}
    paired: Dict[str, Any] = {}
    for shard in family_shards:
        for key, value in shard["arm_summaries"].items():
            formula, mode, scenario = key.split("|", 2)
            result_key = f"{shard['family']}|{formula}|{mode}|{scenario}"
            arms[result_key] = {
                **value,
                "family": shard["family"],
                "source_family": shard["source_family"],
                "role": role,
                "calibration_fold": "development_train_only" if role in {"development_train", "development_holdout"} else "development_model_applied_to_validation",
                "labels_are_executable_fills": False,
            }
        for key, value in _legacy_duplicate_paired_summaries(shard, seed=0, replicates=BOOTSTRAP_REPLICATES).items():
            paired[f"{shard['family']}|{key}"] = value
    return {
        "artifact_version": "universal_entry_fast_formula_screen_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "role": role,
        "family_shards_completed": len(family_shards),
        "arms": arms,
        "paired_cluster_bootstrap_vs_baseline": paired,
        "metrics_are_simulated_non_executable": True,
        "winner_claim_allowed": False,
        "staging_touched": False,
        "limitations": [
            "All labels are experimental scaffold outcomes, not exchange fills.",
            "Historical filters/account states are not reconstructed; no formula is an executable-contract winner.",
            "Fee inputs are current sandbox taker proxies, slippage is scenario-based, and funding is omitted in this fast screen.",
            "The baseline arm reconstructs the runtime QUBO selector with raw predictor EV; peak-quality inputs are absent and default to zero, so it is a control proxy.",
            "Groups are stratified by philosophy, decision time, and venue mode; this is not cross-venue router selection.",
            "Overlapping four-hour labels are not compounded into a portfolio equity curve.",
            "Microstructure, carry, volatility-risk-premium, statistical-arbitrage families are pending/not represented unless present in the source tape.",
        ],
    }


def _legacy_duplicate_run_fast_formula_screen(
    run_dir: Path,
    *,
    requested_workers: int = 8,
    reserve_gb: float = 2.5,
    estimated_worker_gb: float = 0.75,
    resume: bool = False,
    memory_poll_seconds: float = 2.0,
    bootstrap_replicates: int = BOOTSTRAP_REPLICATES,
) -> Dict[str, Any]:
    run_dir = Path(run_dir)
    calibration_candidates = sorted(
        (run_dir / "replay" / "development").glob("bilateral_replay_calibration_*.report.json"),
        key=lambda path: path.stat().st_mtime,
    )
    if not calibration_candidates:
        raise FileNotFoundError("development-only replay calibration report is required")
    calibration_path = calibration_candidates[-1]
    calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
    if calibration.get("usable_for_formula_scoring") is not False or calibration.get("usable_for_live_execution") is not False:
        raise ValueError("calibration report must remain explicitly simulation-only")

    roles = ("development_train", "development_holdout", "validation")
    source_roles = {"development_train": "development", "development_holdout": "development", "validation": "validation"}
    source_paths = []
    for role in ("development", "validation"):
        source_paths.extend((run_dir / "replay" / role / f"bilateral_{name}.jsonl") for name in (
            "thesis_matrix", "contract_scaffold", "scaffold_outcomes", "raw_predictions",
        ))
    proposal_paths = [run_dir / role / "philosophy_proposals.jsonl" for role in ("development", "validation")]
    input_hashes = {str(path.relative_to(REPO_ROOT)).replace("\\", "/"): _sha256(path) for path in source_paths + proposal_paths if path.is_file()}
    if len(input_hashes) != len(source_paths) + len(proposal_paths):
        raise FileNotFoundError("one or more frozen proposal/replay inputs are missing")
    manifest = {
        "scope": "fast_simulated_same_tape_formula_screen",
        "source_run_id": run_dir.name,
        "input_sha256": input_hashes,
        "calibration_report_path": str(calibration_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "calibration_report_sha256": _sha256(calibration_path),
        "formula_arms": list(FORMULAS),
        "slippage_bps_per_fill": list(FRICTION_BPS_PER_FILL),
        "slot_capacity": SLOT_CAPACITY,
        "futures_max_new_per_cycle": FUTURES_MAX_NEW_PER_CYCLE,
        "adaptive_formula_included": False,
        "execution_enabled": False,
    }
    score_root = run_dir / "formula_screen"
    ledger = ProgressLedger(
        score_root,
        run_id=f"{run_dir.name}-formula-screen",
        manifest=manifest,
        resume=resume,
    )

    source_rows: Dict[str, List[Dict[str, Any]]] = {}
    input_counts = {}
    for role in ("development", "validation"):
        source_rows[role], input_counts[role] = _candidate_inputs(run_dir, role, calibration)
    training_rows = [row for row in source_rows["development"] if row["fold"] == "train"]
    if len({row["dedupe_cluster_id"] for row in training_rows}) < 30:
        raise ValueError("fewer than 30 independent development training clusters")
    training_stats = _training_stats(training_rows)
    correlation = _training_correlation(training_rows)
    families = sorted({row["family"] for role in source_rows.values() for row in role})
    jobs = []
    for role in roles:
        source_role = source_roles[role]
        if role == "development_train":
            role_rows = [row for row in source_rows[source_role] if row["fold"] == "train"]
        elif role == "development_holdout":
            role_rows = [row for row in source_rows[source_role] if row["fold"] == "validation"]
        else:
            role_rows = source_rows[source_role]
        for family in families:
            family_rows = [row for row in role_rows if row["family"] == family]
            if family_rows:
                jobs.append({"job_id": f"{role}:{family}", "role": role, "family": family, "rows": family_rows, "training_stats": training_stats, "correlation": correlation, "seed": 42})

    total_jobs = len(jobs)
    ledger.start("same_tape_formula_scoring", total_jobs=total_jobs, requested_workers=requested_workers)
    ledger.state["input_row_counts"] = input_counts
    ledger.state["eligible_training_rows"] = len(training_rows)
    ledger.state["training_clusters"] = len({row["dedupe_cluster_id"] for row in training_rows})
    ledger.state["roles"] = list(roles)
    ledger.state["formula_arms"] = list(FORMULAS)
    ledger.state["families_in_tape"] = families
    ledger.state["pending_canonical_families"] = list(PENDING_CANONICAL_FAMILIES)
    ledger.state["last_completed_job"] = None
    ledger._save()

    completed = ledger.state.setdefault("completed_jobs", {})
    pending_jobs = []
    for job in jobs:
        path = score_root / job["role"] / f"{job['family']}.json"
        record = completed.get(job["job_id"], {})
        if record.get("status") == "completed" and path.is_file() and _sha256(path) == record.get("result_sha256"):
            continue
        completed.pop(job["job_id"], None)
        pending_jobs.append(job)
    ledger.start("same_tape_formula_scoring", total_jobs=total_jobs, requested_workers=requested_workers)
    ledger.state["input_row_counts"] = input_counts
    ledger.state["eligible_training_rows"] = len(training_rows)
    ledger.state["training_clusters"] = len({row["dedupe_cluster_id"] for row in training_rows})
    ledger.state["roles"] = list(roles)
    ledger.state["formula_arms"] = list(FORMULAS)
    ledger.state["families_in_tape"] = families
    ledger.state["pending_canonical_families"] = list(PENDING_CANONICAL_FAMILIES)
    ledger._save()

    reserve_bytes = int(reserve_gb * GIB)
    worker_bytes = int(estimated_worker_gb * GIB)
    pending_index = 0
    active: Dict[Future, Dict[str, Any]] = {}
    failures = []
    completed_shards: Dict[str, List[Dict[str, Any]]] = {role: [] for role in roles}
    with ProcessPoolExecutor(max_workers=requested_workers) as executor:
        while pending_index < len(pending_jobs) or active:
            snapshot = memory_snapshot()
            safe_limit = safe_worker_limit(
                requested_workers,
                available_bytes=snapshot.get("available_bytes"),
                reserve_bytes=reserve_bytes,
                estimated_worker_bytes=worker_bytes,
            )
            ledger.update_runtime(active_workers=len(active), safe_worker_limit=safe_limit)
            if safe_limit == 0:
                ledger.state["state"] = "waiting_for_memory"
                ledger._save()
                time.sleep(memory_poll_seconds)
                continue
            ledger.state["state"] = "running"
            while pending_index < len(pending_jobs) and len(active) < safe_limit:
                job = pending_jobs[pending_index]
                pending_index += 1
                active[executor.submit(_score_job, job)] = job
                ledger.state["current_job"] = job["job_id"]
                ledger.update_runtime(active_workers=len(active), safe_worker_limit=safe_limit)
            done, _ = wait(active, timeout=memory_poll_seconds, return_when=FIRST_COMPLETED)
            for future in done:
                job = active.pop(future)
                try:
                    result = future.result()
                    output = score_root / job["role"] / f"{job['family']}.json"
                    _atomic_json(output, result)
                    ledger.complete_job(
                        job["job_id"],
                        checkpoint=str(output.relative_to(REPO_ROOT)).replace("\\", "/"),
                        result_sha256=_sha256(output),
                    )
                    completed_shards[job["role"]].append(result)
                    ledger.state["last_completed_job"] = job["job_id"]
                    ledger.state.pop("current_job", None)
                    ledger._save()
                except Exception as exc:
                    reason = f"{type(exc).__name__}: {str(exc)[:300]}"
                    failures.append(job["job_id"])
                    ledger.fail_job(job["job_id"], reason)

    for job in jobs:
        if job["job_id"] in completed:
            output = score_root / job["role"] / f"{job['family']}.json"
            if output.is_file() and _sha256(output) == completed[job["job_id"]].get("result_sha256"):
                if not any(item["family"] == job["family"] for item in completed_shards[job["role"]]):
                    completed_shards[job["role"]].append(json.loads(output.read_text(encoding="utf-8")))
    if failures:
        ledger.finish(state="partial", error=f"formula scoring shards failed: {', '.join(failures)}")
        return ledger.state

    reports = {}
    for role in roles:
        report = _summary_for_role(role, completed_shards[role])
        report["run_id"] = run_dir.name
        report["input_sha256"] = input_hashes
        report["calibration_report_path"] = manifest["calibration_report_path"]
        report["calibration_report_sha256"] = manifest["calibration_report_sha256"]
        report["training_stats"] = training_stats
        report["selection_policy"] = {
            "family_grouping": True,
            "decision_time_grouping": True,
            "venue_mode_grouping": True,
            "spot_long_slot_capacity": SLOT_CAPACITY,
            "futures_per_mode_cycle_slot_capacity": FUTURES_MAX_NEW_PER_CYCLE,
            "same_candidates_all_formula_arms": True,
        }
        report_path = score_root / f"{role}_formula_comparison.report.json"
        _atomic_json(report_path, report)
        reports[role] = str(report_path.relative_to(REPO_ROOT)).replace("\\", "/")
    summary = {
        "artifact_version": "universal_entry_fast_formula_screen_v1",
        "run_id": run_dir.name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phase": "same_tape_formula_screen_complete",
        "state": "simulation_screen_complete_review_required",
        "formula_arms": list(FORMULAS),
        "roles": list(roles),
        "family_results": reports,
        "family_shards": {role: [item["family"] for item in completed_shards[role]] for role in roles},
        "input_counts": input_counts,
        "training_clusters": len({row["dedupe_cluster_id"] for row in training_rows}),
        "pending_canonical_families": list(PENDING_CANONICAL_FAMILIES),
        "metrics_are_simulated_non_executable": True,
        "winner_claim_allowed": False,
        "staging_touched": False,
        "orders_submitted": False,
        "limitations": [
            "All labels are simulated scaffold outcomes; no formulas were compared against exchange-confirmed fills.",
            "Current sandbox fee schedules are proxies; slippage uses fixed per-fill sensitivities and historical funding is omitted.",
            "The baseline arm reconstructs runtime QUBO selection using raw predictor EV; peak-quality input is missing and defaults to zero, so it is a control proxy.",
            "Same-tape selection is stratified by philosophy, time, and venue mode; cross-venue routing is not scored.",
            "Adaptive formula selection and full 24-window simulation are later phases.",
            "Four canonical families remain pending in this fast tape: carry/funding-aware, volatility-risk-premium, statistical-arbitrage, and microstructure/order-flow.",
        ],
    }
    _atomic_json(score_root / "summary.json", summary)
    ledger.state["summary_path"] = str((score_root / "summary.json").relative_to(REPO_ROOT)).replace("\\", "/")
    ledger.state["reports"] = reports
    ledger.state["pending_canonical_families"] = list(PENDING_CANONICAL_FAMILIES)
    ledger.state["winner_claim_allowed"] = False
    ledger.finish(state="formula_screen_complete_no_winner_claim")
    return ledger.state


def _legacy_duplicate_main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-workers", type=int, default=8)
    parser.add_argument("--reserve-gb", type=float, default=2.5)
    parser.add_argument("--estimated-worker-gb", type=float, default=0.75)
    parser.add_argument("--bootstrap-replicates", type=int, default=BOOTSTRAP_REPLICATES)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    args = parser.parse_args()
    run_dir = args.run_root / args.run_id
    if not run_dir.is_dir():
        parser.error(f"formula source run not found: {run_dir}")
    try:
        state = _legacy_duplicate_run_fast_formula_screen(
            run_dir,
            requested_workers=args.max_workers,
            reserve_gb=args.reserve_gb,
            estimated_worker_gb=args.estimated_worker_gb,
            resume=args.resume,
            bootstrap_replicates=args.bootstrap_replicates,
        )
    except Exception as exc:
        parser.error(f"fast formula screen failed: {type(exc).__name__}: {str(exc)[:400]}")
    print(json.dumps({
        "run_id": state.get("run_id"),
        "state": state.get("state"),
        "phase": state.get("phase"),
        "counts": state.get("counts"),
        "workers": state.get("workers"),
        "summary_path": state.get("summary_path"),
        "winner_claim_allowed": state.get("winner_claim_allowed"),
    }, ensure_ascii=False))
    return 0 if state.get("state") == "formula_screen_complete_no_winner_claim" else 1


if __name__ == "__main__":
    raise SystemExit(main())