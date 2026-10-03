"""Build a read-only symbol/window coverage manifest for entry replay inputs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WINDOWS_DIR = REPO_ROOT / "Dev" / "benchmarks" / "windows_calibration"
DEFAULT_DATA_DIR = REPO_ROOT / "historical_data"
DEFAULT_OUTPUT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "data_coverage_manifest.json"
INTERVALS = {"1m": pd.Timedelta(minutes=1), "5m": pd.Timedelta(minutes=5), "1h": pd.Timedelta(hours=1)}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_timestamps(path: Path) -> Dict[str, Any]:
    frame = pd.read_parquet(path, columns=["open_time"])
    values = frame["open_time"]
    if pd.api.types.is_datetime64_any_dtype(values):
        timestamps = pd.to_datetime(values, utc=True, errors="coerce")
    else:
        numeric = pd.to_numeric(values, errors="coerce")
        finite = numeric.dropna()
        unit = "ms" if not finite.empty and float(finite.abs().median()) >= 100_000_000_000 else "s"
        timestamps = pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")
    valid = timestamps.dropna().sort_values()
    duplicates = int(valid.duplicated().sum())
    gaps = valid.diff().dropna()
    max_gap = gaps.max() if not gaps.empty else pd.Timedelta(0)
    return {
        "timestamps": valid,
        "rows": int(len(frame)),
        "invalid_timestamps": int(timestamps.isna().sum()),
        "duplicate_timestamps": duplicates,
        "start": valid.iloc[0].isoformat() if not valid.empty else None,
        "end": valid.iloc[-1].isoformat() if not valid.empty else None,
        "max_gap_seconds": float(max_gap.total_seconds()),
        "sha256": _file_sha256(path),
        "size_bytes": path.stat().st_size,
    }


def _load_windows(windows_dir: Path) -> List[Dict[str, Any]]:
    windows: List[Dict[str, Any]] = []
    for path in sorted(windows_dir.glob("*.json")):
        if path.name == "window_plan.json":
            continue
        with path.open("r", encoding="utf-8") as source:
            item = json.load(source)
        if not item.get("start") or not item.get("end"):
            continue
        start = pd.Timestamp(item["start"])
        end = pd.Timestamp(item["end"])
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
        end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
        windows.append({
            "window_id": str(item.get("window_id") or path.stem),
            "path": str(path.relative_to(REPO_ROOT)),
            "start": start,
            "end": end,
            "symbols": sorted({str(symbol).upper() for symbol in item.get("symbols", []) if symbol}),
            "regime": item.get("dominant_regime") or item.get("regime"),
            "role": item.get("role"),
        })
    return windows


def build_manifest(windows_dir: Path, data_dir: Path, intervals: List[str]) -> Dict[str, Any]:
    windows = _load_windows(windows_dir)
    unknown_intervals = sorted(set(intervals) - set(INTERVALS))
    if unknown_intervals:
        raise ValueError(f"Unsupported intervals: {unknown_intervals}")

    symbols = sorted({symbol for window in windows for symbol in window["symbols"]})
    source_cache: Dict[tuple[str, str], Dict[str, Any]] = {}
    file_errors: Dict[tuple[str, str], str] = {}
    entries: List[Dict[str, Any]] = []
    status_counts: Dict[str, int] = {}

    for symbol in symbols:
        for interval in intervals:
            path = data_dir / f"{symbol}_{interval}.parquet"
            try:
                source_cache[(symbol, interval)] = _load_timestamps(path) if path.exists() else None
            except Exception as exc:
                file_errors[(symbol, interval)] = f"read_error:{type(exc).__name__}:{exc}"
                source_cache[(symbol, interval)] = None

    for window in windows:
        for symbol in window["symbols"]:
            for interval in intervals:
                expected = pd.date_range(window["start"], window["end"], freq=INTERVALS[interval])
                source = source_cache.get((symbol, interval))
                base = {
                    "window_id": window["window_id"],
                    "window_manifest": window["path"],
                    "regime": window["regime"],
                    "role": window["role"],
                    "symbol": symbol,
                    "interval": interval,
                    "window_start": window["start"].isoformat(),
                    "window_end": window["end"].isoformat(),
                    "expected_bars": int(len(expected)),
                }
                if source is None:
                    status = "read_error" if (symbol, interval) in file_errors else "missing_file"
                    entry = {**base, "status": status, "error": file_errors.get((symbol, interval))}
                else:
                    timestamps = source["timestamps"]
                    within = timestamps[(timestamps >= window["start"]) & (timestamps <= window["end"])]
                    present_mask = expected.isin(within)
                    present = int(present_mask.sum())
                    coverage = present / max(1, len(expected))
                    interval_seconds = INTERVALS[interval].total_seconds()
                    within_gaps = within.diff().dropna()
                    max_window_gap = float(within_gaps.max().total_seconds()) if not within_gaps.empty else 0.0
                    if source["invalid_timestamps"]:
                        status = "invalid_timestamps"
                    elif source["duplicate_timestamps"]:
                        status = "duplicate_timestamps"
                    elif within.empty or within.iloc[0] > window["start"] + INTERVALS[interval] or within.iloc[-1] < window["end"] - INTERVALS[interval]:
                        status = "window_not_covered"
                    elif not bool(present_mask.all()) or max_window_gap > interval_seconds * 1.5:
                        status = "gaps_within_window"
                    else:
                        status = "complete"
                    entry = {
                        **base,
                        "status": status,
                        "present_bars": present,
                        "coverage_pct": round(coverage * 100.0, 4),
                        "missing_bars": int(len(expected) - present),
                        "max_gap_seconds_in_window": max_window_gap,
                        "source_start": source["start"],
                        "source_end": source["end"],
                        "source_rows": source["rows"],
                        "source_invalid_timestamps": source["invalid_timestamps"],
                        "source_duplicate_timestamps": source["duplicate_timestamps"],
                        "source_sha256": source["sha256"],
                        "source_size_bytes": source["size_bytes"],
                        "download_required": status != "complete",
                    }
                entries.append(entry)
                status_counts[entry["status"]] = status_counts.get(entry["status"], 0) + 1

    return {
        "manifest_id": "universal_entry_selection_data_coverage_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "local historical_data parquet + windows_calibration manifests",
        "windows_dir": str(windows_dir.relative_to(REPO_ROOT)),
        "data_dir": str(data_dir.relative_to(REPO_ROOT)),
        "intervals": intervals,
        "window_count": len(windows),
        "unique_symbol_count": len(symbols),
        "symbol_window_interval_count": len(entries),
        "status_counts": status_counts,
        "entries": entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows-dir", type=Path, default=DEFAULT_WINDOWS_DIR)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--intervals", default="1m,5m,1h")
    args = parser.parse_args()
    intervals = [item.strip() for item in args.intervals.split(",") if item.strip()]
    report = build_manifest(args.windows_dir, args.data_dir, intervals)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as target:
        json.dump(report, target, ensure_ascii=False, indent=2)
        target.write("\n")
    print(json.dumps({key: report[key] for key in (
        "manifest_id", "window_count", "unique_symbol_count",
        "symbol_window_interval_count", "status_counts",
    )}, ensure_ascii=False, indent=2))
    print(f"manifest_path={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())