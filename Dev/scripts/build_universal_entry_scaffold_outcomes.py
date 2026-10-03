"""Replay non-executable scaffold geometries against point-in-time 1m candles."""
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

from simulation.entry_outcome_resolver import resolve_contract_path
from simulation.mimic_feed import HistoricalDataFeed


DEFAULT_INPUT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "pilot_development" / "contract_scaffold.jsonl"
DEFAULT_OUTPUT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "pilot_development" / "scaffold_outcomes.jsonl"
NORMALIZED_ROOT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "normalized" / "pilot_development"
HORIZON_MINUTES = 240


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
                raise ValueError(f"invalid scaffold JSON at line {line_number}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"scaffold row at line {line_number} is not an object")
            yield row


def build_outcomes(
    input_path: Path = DEFAULT_INPUT,
    output_path: Path = DEFAULT_OUTPUT,
    normalized_root: Path = NORMALIZED_ROOT,
    *,
    replace: bool = False,
) -> Dict[str, Any]:
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    report_path = output_path.with_suffix(".report.json")
    if not replace and (output_path.exists() or report_path.exists()):
        raise FileExistsError(f"outcome artifact exists; pass --replace: {output_path}")
    scaffold_report_path = input_path.with_suffix(".report.json")
    scaffold_report = json.loads(scaffold_report_path.read_text(encoding="utf-8")) if scaffold_report_path.is_file() else {}
    source_manifest_hashes = scaffold_report.get("source_market_manifest_hashes", {})
    for manifest_key, expected_hash in source_manifest_hashes.items():
        try:
            venue, symbol = manifest_key.split(":", 1)
        except ValueError as exc:
            raise ValueError(f"invalid source market manifest key: {manifest_key}") from exc
        manifest_path = normalized_root / venue / f"{symbol}_1m.manifest.json"
        if not manifest_path.is_file() or _sha256(manifest_path) != expected_hash:
            raise ValueError(f"normalized market manifest changed since scaffold generation: {manifest_key}")
    source_parquet_hashes = scaffold_report.get("source_market_parquet_hashes", {})
    if source_manifest_hashes and not source_parquet_hashes:
        raise ValueError("scaffold report lacks normalized market Parquet hashes; regenerate scaffold")
    missing_parquet_hashes = sorted(f"{key}:1m" for key in source_manifest_hashes if f"{key}:1m" not in source_parquet_hashes)
    if missing_parquet_hashes:
        raise ValueError(f"scaffold report lacks normalized market Parquet hashes: {missing_parquet_hashes}")
    for parquet_key, expected_hash in source_parquet_hashes.items():
        try:
            venue, symbol, interval = parquet_key.split(":", 2)
        except ValueError as exc:
            raise ValueError(f"invalid source market Parquet key: {parquet_key}") from exc
        if venue not in {"spot", "um"} or interval not in {"1m", "1h"} or not symbol:
            raise ValueError(f"invalid source market Parquet key: {parquet_key}")
        parquet_path = normalized_root / venue / f"{symbol}_{interval}.parquet"
        if not parquet_path.is_file() or _sha256(parquet_path) != expected_hash:
            raise ValueError(f"normalized market Parquet changed since scaffold generation: {parquet_key}")

    feeds: Dict[str, HistoricalDataFeed] = {}
    outcomes = Counter()
    geometry_statuses = Counter()
    skipped_modes = Counter()
    rows_written = 0
    source_symbols = set()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as output_handle:
            for scaffold in _rows(input_path):
                symbol = str(scaffold.get("symbol") or "").upper()
                entry_time = scaffold.get("decision_time")
                for contract in scaffold.get("contracts") or []:
                    mode = str(contract.get("mode") or "")
                    geometry = contract.get("geometry") if isinstance(contract.get("geometry"), dict) else {}
                    geometry_status = str(geometry.get("status") or "missing")
                    geometry_statuses[geometry_status] += 1
                    if mode == "SPOT_MARGIN_SHORT":
                        skipped_modes[mode] += 1
                        continue
                    if geometry_status != "derived_point_in_time_geometry":
                        continue
                    contract_entry_price = geometry.get("entry_price")
                    side = str(contract.get("side") or "").upper()
                    venue = str(contract.get("venue") or "")
                    venue_dir = "um" if venue == "um" else "spot"
                    if venue_dir not in feeds:
                        feeds[venue_dir] = HistoricalDataFeed(str(normalized_root / venue_dir))
                    bars = feeds[venue_dir].get_klines(symbol, "1m")
                    stop = geometry.get("stop_price")
                    target_price = geometry.get("target_price")
                    if entry_time is None or contract_entry_price is None or stop is None or target_price is None:
                        result = {"outcome": "DATA_GAP", "label_valid": False, "reason": "GEOMETRY_FIELDS_MISSING"}
                    else:
                        result = resolve_contract_path(
                            bars,
                            side=side,
                            entry_time=float(entry_time),
                            entry_price=float(contract_entry_price),
                            stop_price=float(stop),
                            target_price=float(target_price),
                            horizon_minutes=HORIZON_MINUTES,
                            bar_seconds=60,
                            ambiguous_policy="stop_first",
                        )
                    outcomes[str(result.get("outcome"))] += 1
                    source_symbols.add(symbol)
                    output_handle.write(json.dumps({
                        "opportunity_event_id": scaffold.get("opportunity_event_id"),
                        "bilateral_thesis_id": scaffold.get("bilateral_thesis_id"),
                        "dedupe_cluster_id": scaffold.get("dedupe_cluster_id"),
                        "window_id": scaffold.get("window_id"),
                        "window_role": scaffold.get("window_role"),
                        "symbol": symbol,
                        "mode": mode,
                        "contract_symbol": contract.get("contract_symbol", symbol),
                        "source_market_manifest_key": contract.get("source_market_manifest_key"),
                        "source_market_manifest_sha256": contract.get("source_market_manifest_sha256"),
                        "side": side,
                        "thesis_role": scaffold.get("thesis_role", "observed"),
                        "thesis_type": scaffold.get("thesis_type"),
                        "decision_time": entry_time,
                        "entry_price": contract_entry_price,
                        "geometry": geometry,
                        "outcome": result,
                        "executable_at_decision": False,
                        "label_status": "experimental_scaffold_outcome_not_executable_fill",
                    }, ensure_ascii=False, allow_nan=False) + "\n")
                    rows_written += 1
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)

    report = {
        "artifact_version": "universal_entry_scaffold_outcomes_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_path": _display_path(input_path),
        "input_sha256": _sha256(input_path),
        "scaffold_source_market_manifest_hashes": source_manifest_hashes,
        "scaffold_source_market_parquet_hashes": source_parquet_hashes,
        "output_path": _display_path(output_path),
        "output_sha256": _sha256(output_path),
        "rows_written": rows_written,
        "symbols": sorted(source_symbols),
        "horizon_minutes": HORIZON_MINUTES,
        "outcome_counts": dict(sorted(outcomes.items())),
        "geometry_status_counts": dict(sorted(geometry_statuses.items())),
        "skipped_modes": dict(sorted(skipped_modes.items())),
        "label_is_executable": False,
        "limitations": [
            "Only venue/side-matched derived geometries are replayed; cross-side alternatives and invalid geometry remain pending/abstained.",
            "SPOT_MARGIN_SHORT is disabled by pilot policy and is never replayed, even as a price-path counterfactual.",
            "No venue filters, balances, costs, calibrated probabilities, or fills are retrofitted.",
            "These labels are experimental scaffold outcomes and cannot score formulas or authorize Staging.",
        ],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--normalized-root", type=Path, default=NORMALIZED_ROOT)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    try:
        report = build_outcomes(args.input, args.output, args.normalized_root, replace=args.replace)
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())