"""Generate raw side-specific predictor snapshots for the bilateral thesis matrix."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from pnl_predictor import PnLPredictor

DEFAULT_INPUT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "pilot_development" / "bilateral_thesis_matrix.jsonl"
DEFAULT_OUTPUT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "pilot_development" / "bilateral_raw_predictions.jsonl"


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


def build_predictions(input_path: Path = DEFAULT_INPUT, output_path: Path = DEFAULT_OUTPUT, *, replace: bool = False) -> dict[str, Any]:
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    report_path = output_path.with_suffix(".report.json")
    if not replace and (output_path.exists() or report_path.exists()):
        raise FileExistsError(f"prediction artifact exists; pass --replace: {output_path}")
    predictor = PnLPredictor()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    rows = 0
    statuses: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    thesis_ids = set()
    try:
        with input_path.open("r", encoding="utf-8") as source, temporary.open("w", encoding="utf-8", newline="\n") as target:
            for line_number, line in enumerate(source, 1):
                try:
                    matrix_row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid matrix JSON at line {line_number}") from exc
                event_id = str(matrix_row.get("opportunity_event_id") or "")
                thesis_id = str(matrix_row.get("bilateral_thesis_id") or "")
                if not event_id or not thesis_id or thesis_id in thesis_ids:
                    raise ValueError("prediction matrix contains duplicate or missing bilateral_thesis_id")
                thesis_ids.add(thesis_id)
                features = matrix_row.get("feature_snapshot") if isinstance(matrix_row.get("feature_snapshot"), dict) else {}
                predictor_indicators = features.get("predictor_indicators") if isinstance(features.get("predictor_indicators"), dict) else {}
                predictor_input = {**features, **predictor_indicators}
                entry_price = matrix_row.get("entry_price")
                side = str(matrix_row.get("side") or "").upper()
                try:
                    prediction = predictor.predict_pnl(
                        str(matrix_row.get("symbol") or ""),
                        float(entry_price),
                        predictor_input,
                        cycle_duration_minutes=int(matrix_row.get("ev_horizon_minutes") or 240),
                        position_size_usdt=100.0,
                        trade_type="futures" if side == "SHORT" else "spot",
                        trade_side=side,
                        settings=None,
                    )
                except Exception as exc:
                    prediction = {
                        "valid": False,
                        "prediction_status": "error",
                        "error_code": type(exc).__name__,
                        "error_message": str(exc)[:300],
                    }
                probability = prediction.get("success_probability")
                expected_pnl = prediction.get("expected_pnl")
                raw_valid = (
                    isinstance(probability, (int, float))
                    and 0.0 <= float(probability) <= 1.0
                    and isinstance(expected_pnl, (int, float))
                )
                status = str(
                    prediction.get("prediction_status")
                    or ("raw_available" if raw_valid else "invalid")
                )
                statuses[status] += 1
                if prediction.get("error_code"):
                    errors[str(prediction["error_code"])] += 1
                target.write(json.dumps({
                    "bilateral_thesis_id": matrix_row.get("bilateral_thesis_id"),
                    "opportunity_event_id": event_id,
                    "dedupe_cluster_id": matrix_row.get("dedupe_cluster_id"),
                    "window_id": matrix_row.get("window_id"),
                    "symbol": matrix_row.get("symbol"),
                    "thesis_role": matrix_row.get("thesis_role"),
                    "thesis_type": matrix_row.get("thesis_type"),
                    "side": side,
                    "decision_time": matrix_row.get("decision_time"),
                    "entry_time": matrix_row.get("entry_time"),
                    "entry_price": entry_price,
                    "feature_snapshot": features,
                    "prediction": prediction,
                    "prediction_status": status,
                    "calibration_status": "raw_uncalibrated",
                    "formula_scoring_allowed": False,
                }, ensure_ascii=False, allow_nan=False) + "\n")
                rows += 1
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)
    report = {
        "artifact_version": "universal_entry_raw_predictions_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_path": _display_path(input_path),
        "input_sha256": _sha256(input_path),
        "output_path": _display_path(output_path),
        "output_sha256": _sha256(output_path),
            "rows": rows,
            "output": _display_path(output_path),
        "status_counts": dict(sorted(statuses.items())),
        "error_counts": dict(sorted(errors.items())),
        "calibration_status": "raw_uncalibrated",
        "formula_scoring_allowed": False,
        "limitations": [
            "Raw model output is not calibrated probability.",
            "No contract, venue filter, cost, balance, fill, or formula decision is inferred.",
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
        report = build_predictions(args.input, args.output, replace=args.replace)
    except (FileNotFoundError, FileExistsError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
