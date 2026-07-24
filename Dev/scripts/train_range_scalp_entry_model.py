from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import joblib
import numpy as np
from sklearn.ensemble import ExtraTreesRegressor
from sklearn.model_selection import TimeSeriesSplit

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = REPO_ROOT / "Dev" / "benchmarks" / "entry_profile_calibration" / "range_scalp_calibration_dataset.jsonl"
DEFAULT_MODEL = REPO_ROOT / "Dev" / "benchmarks" / "entry_profile_calibration" / "quality_score_model_range_scalp_v2.pkl"
DEFAULT_REPORT = REPO_ROOT / "Dev" / "benchmarks" / "entry_profile_calibration" / "range_scalp_walkforward_report.json"
DEFAULT_THRESHOLDS = REPO_ROOT / "Dev" / "benchmarks" / "entry_profile_calibration" / "range_scalp_candidate_thresholds.json"
FEATURES = [
    "expected_pnl_pct", "peak_quality", "composite_score", "confluence", "short_term_score",
    "success_probability", "signal_strength", "range_score", "vol_score", "rsi_score",
    "pnl_score", "symbol_quality_score", "rsi_4h", "vol_spike", "range_position", "atr",
]


def _number(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("value", value.get("v"))
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _expected_pct(row: Dict[str, Any]) -> float | None:
    for key in ("expected_pnl_percentage", "expected_pnl_pct", "expected_return_pct"):
        result = _number(row.get(key))
        if result is not None:
            return result
    expected = _number(row.get("expected_pnl"))
    size = _number(row.get("position_size_usdt") or row.get("position_usdt"))
    if expected is not None and size and size > 0:
        return expected / size * 100.0
    return None


def _timestamp(row: Dict[str, Any]) -> str:
    return str(row.get("entry_time") or row.get("timestamp") or row.get("run_id") or "")


def _rows(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            outcome = _number(row.get("realized_net_pnl_pct"))
            if outcome is None:
                continue
            values: List[float] = []
            missing: List[str] = []
            for feature in FEATURES:
                value = _expected_pct(row) if feature == "expected_pnl_pct" else _number(row.get(feature))
                if value is None:
                    missing.append(feature)
                    value = 0.0
                values.append(value)
            row["_values"] = values
            row["_missing"] = missing
            row["_target"] = outcome
            records.append(row)
    return sorted(records, key=_timestamp)


def _metrics(target: np.ndarray, score: np.ndarray, coverage: float) -> Dict[str, float | int]:
    cutoff = float(np.quantile(score, 1.0 - coverage))
    selected = target[score >= cutoff]
    if not len(selected):
        return {"threshold": cutoff, "n": 0, "mean_net": 0.0, "win_rate": 0.0, "profit_factor": 0.0}
    gains = selected[selected > 0].sum()
    losses = -selected[selected < 0].sum()
    return {
        "threshold": cutoff,
        "n": int(len(selected)),
        "mean_net": float(selected.mean()),
        "win_rate": float((selected > 0).mean()),
        "profit_factor": float(gains / losses) if losses > 0 else float("inf"),
    }


def _choose_thresholds(target: np.ndarray, score: np.ndarray) -> Tuple[Dict[str, float], Dict[str, Dict[str, float | int]]]:
    candidates = [(0.05, "A", 30, 1.15, 0.50), (0.10, "A", 30, 1.15, 0.50), (0.15, "B", 50, 1.10, 0.0), (0.20, "B", 50, 1.10, 0.0), (0.25, "C", 75, 1.05, 0.0), (0.30, "C", 75, 1.05, 0.0)]
    selected: Dict[str, Dict[str, float | int]] = {}
    for coverage, tier, min_n, min_pf, min_wr in candidates:
        metric = _metrics(target, score, coverage)
        if metric["n"] >= min_n and metric["mean_net"] > 0 and metric["profit_factor"] >= min_pf and metric["win_rate"] >= min_wr:
            selected[tier] = metric
    if "A" not in selected:
        raise ValueError("No RANGE_SCALP A threshold meets holdout criteria")
    thresholds = {"A": float(selected["A"]["threshold"])}
    thresholds["B"] = float(selected.get("B", selected["A"])["threshold"])
    thresholds["C"] = float(selected.get("C", selected.get("B", selected["A"]))["threshold"])
    thresholds = dict(sorted(thresholds.items(), key=lambda item: ("ABC".index(item[0]))))
    return thresholds, selected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--model-output", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--report-output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--threshold-output", type=Path, default=DEFAULT_THRESHOLDS)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--splits", type=int, default=5)
    args = parser.parse_args()
    workers = max(1, min(10, args.workers))
    records = _rows(args.dataset)
    if len(records) < 150:
        raise SystemExit("Need at least 150 labeled RANGE_SCALP records")
    source_counts = Counter(str(row.get("source") or "unknown") for row in records)
    symbols = {str(row.get("symbol")) for row in records if row.get("symbol")}
    if len(symbols) < 10:
        raise SystemExit(f"Need at least 10 RANGE_SCALP symbols, found {len(symbols)}")
    x = np.asarray([row["_values"] for row in records], dtype=float)
    y = np.asarray([row["_target"] for row in records], dtype=float)
    tscv = TimeSeriesSplit(n_splits=args.splits)
    oof = np.full(len(records), np.nan, dtype=float)
    fold_metrics: List[Dict[str, Any]] = []
    for fold, (train_idx, test_idx) in enumerate(tscv.split(x), start=1):
        model = ExtraTreesRegressor(n_estimators=400, max_depth=8, min_samples_leaf=8, n_jobs=workers, random_state=42 + fold)
        model.fit(x[train_idx], y[train_idx])
        prediction = model.predict(x[test_idx])
        oof[test_idx] = prediction
        fold_metrics.append({
            "fold": fold,
            "train_n": int(len(train_idx)),
            "test_n": int(len(test_idx)),
            "corr": float(np.corrcoef(prediction, y[test_idx])[0, 1]) if len(test_idx) > 1 else 0.0,
        })
    mask = np.isfinite(oof)
    model = ExtraTreesRegressor(n_estimators=600, max_depth=8, min_samples_leaf=8, n_jobs=workers, random_state=42)
    model.fit(x, y)
    trained_at = datetime.now(timezone.utc).isoformat()
    try:
        thresholds, tier_metrics = _choose_thresholds(y[mask], oof[mask])
        promotion_eligible = True
        promotion_reason = None
    except ValueError as exc:
        thresholds = {}
        tier_metrics = {str(coverage): _metrics(y[mask], oof[mask], coverage) for coverage in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30)}
        promotion_eligible = False
        promotion_reason = str(exc)
    threshold_document = {
        "meta": {"dataset_version": "range_scalp_v1", "score_source": "range_scalp_model_v2", "trained_at": trained_at, "promotion_eligible": promotion_eligible},
        "thresholds": {"RANGE_SCALP": {**thresholds, "discard": float("-inf")}} if promotion_eligible else {},
        "validation": {"RANGE_SCALP": {"metrics": tier_metrics, "n_total": int(mask.sum()), "promotion_reason": promotion_reason}},
    }
    report = {
        "records": len(records),
        "symbols": len(symbols),
        "sources": source_counts,
        "folds": fold_metrics,
        "oof_corr": float(np.corrcoef(oof[mask], y[mask])[0, 1]),
        "promotion_eligible": promotion_eligible,
        "promotion_reason": promotion_reason,
        "thresholds": threshold_document,
        "feature_importances": dict(zip(FEATURES, map(float, model.feature_importances_))),
        "missing_feature_counts": Counter(feature for row in records for feature in row["_missing"]),
    }
    args.report_output.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=float), encoding="utf-8")
    if promotion_eligible:
        artifact = {
            "model": model,
            "features": FEATURES,
            "regime": "RANGE_SCALP",
            "dataset_version": "range_scalp_v1",
            "trained_at": trained_at,
            "workers": workers,
        }
        args.model_output.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(artifact, args.model_output)
        args.threshold_output.write_text(json.dumps(threshold_document, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False, default=float))
    return 0 if promotion_eligible else 2


if __name__ == "__main__":
    raise SystemExit(main())
