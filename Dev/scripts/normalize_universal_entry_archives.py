"""Normalize verified Binance Vision kline archives into isolated venue Parquet."""
from __future__ import annotations

import argparse
import csv
import concurrent.futures
import hashlib
import io
import json
import re
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

try:
    import pandas as pd
except Exception:
    pd = None


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW_ROOT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "raw" / "binance_archives"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "normalized"
INTERVAL_SECONDS = {"1m": 60, "5m": 300, "1h": 3600}
CHECKSUM_PATTERN = re.compile(r"(?i)\b([0-9a-f]{64})\b")
KLINE_COLUMNS = (
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_base_volume", "taker_buy_quote_volume", "ignore",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_checksum(path: Path) -> str:
    sidecar = Path(f"{path}.CHECKSUM")
    if not sidecar.is_file():
        raise FileNotFoundError(f"missing Binance checksum sidecar: {sidecar}")
    match = CHECKSUM_PATTERN.search(sidecar.read_text(encoding="utf-8"))
    if not match:
        raise ValueError(f"invalid Binance checksum sidecar: {sidecar}")
    expected = match.group(1).lower()
    actual = _sha256(path)
    if actual != expected:
        raise ValueError(f"SHA-256 mismatch for {path.name}")
    return actual


def _timestamp_divisor(timestamp: int) -> int:
    magnitude = abs(timestamp)
    if magnitude >= 100_000_000_000_000:
        return 1_000_000
    if magnitude >= 100_000_000_000:
        return 1_000
    return 1


def _read_daily_archive(path: Path, interval: str) -> List[Dict[str, Any]]:
    with zipfile.ZipFile(path) as archive:
        corrupt_member = archive.testzip()
        if corrupt_member:
            raise ValueError(f"ZIP CRC failed for {corrupt_member} in {path.name}")
        csv_members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(csv_members) != 1:
            raise ValueError(f"expected one CSV member in {path.name}, found {len(csv_members)}")
        with archive.open(csv_members[0]) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
            first = next(reader, None)
            if not first:
                raise ValueError(f"empty CSV in {path.name}")
            rows: Iterable[List[str]] = reader
            if not first[0].strip().isdigit():
                rows = reader
            else:
                rows = iter([first, *reader])
            raw_rows = [row for row in rows if row]

    if not raw_rows:
        raise ValueError(f"no kline rows in {path.name}")
    if any(len(row) < 6 for row in raw_rows):
        raise ValueError(f"kline row has fewer than six columns in {path.name}")

    timestamps = [int(row[0]) for row in raw_rows]
    divisor = _timestamp_divisor(timestamps[0])
    interval_seconds = INTERVAL_SECONDS[interval]
    expected_rows = 86400 // interval_seconds
    if len(timestamps) != expected_rows:
        raise ValueError(f"{path.name} has {len(timestamps)} rows; expected {expected_rows}")
    if len(set(timestamps)) != len(timestamps):
        raise ValueError(f"duplicate kline timestamps in {path.name}")
    expected_step = interval_seconds * divisor
    if timestamps[0] % (86400 * divisor) != 0 or timestamps[-1] - timestamps[0] != (expected_rows - 1) * expected_step:
        raise ValueError(f"daily bounds/cadence mismatch in {path.name}")
    if any(right - left != expected_step for left, right in zip(timestamps, timestamps[1:])):
        raise ValueError(f"intraday cadence gap in {path.name}")

    normalized: List[Dict[str, Any]] = []
    for row, timestamp in zip(raw_rows, timestamps):
        item: Dict[str, Any] = {
            "open_time": timestamp / divisor,
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        }
        if len(row) > 7:
            item["quote_volume"] = float(row[7])
        if len(row) > 8:
            item["trades"] = int(float(row[8]))
        normalized.append(item)
    return normalized


def normalize_archive_range(
    *,
    venue: str,
    symbol: str,
    interval: str,
    start_date: date,
    end_date: date,
    raw_root: Path = DEFAULT_RAW_ROOT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    contract_symbol: str | None = None,
    replace: bool = False,
) -> Dict[str, Any]:
    if venue not in {"spot", "um"}:
        raise ValueError("venue must be 'spot' or 'um'")
    if interval not in INTERVAL_SECONDS:
        raise ValueError(f"interval must be one of {sorted(INTERVAL_SECONDS)}")
    if start_date > end_date:
        raise ValueError("start_date must be on or before end_date")

    symbol = symbol.upper()
    explicit_contract_symbol = contract_symbol.upper() if contract_symbol else None
    contract_candidates = [explicit_contract_symbol or symbol]
    if venue == "um" and explicit_contract_symbol is None and symbol.endswith("USDT"):
        contract_candidates.append(f"1000{symbol[:-4]}USDT")
    archives: List[Path] = []
    source_contract_symbols = set()
    all_rows: List[Dict[str, Any]] = []
    cursor = start_date
    while cursor <= end_date:
        archive_path = None
        for candidate in contract_candidates:
            nested_path = raw_root / venue / candidate / interval / f"{candidate}-{interval}-{cursor.isoformat()}.zip"
            legacy_path = raw_root / f"{candidate}-{interval}-{cursor.isoformat()}-{venue}.zip"
            if nested_path.is_file():
                archive_path = nested_path
                break
            if legacy_path.is_file():
                archive_path = legacy_path
                break
        if archive_path is None:
            raise FileNotFoundError(f"missing archive for {venue}/{symbol} {interval} {cursor}")
        checksum = _verify_checksum(archive_path)
        daily_rows = _read_daily_archive(archive_path, interval)
        all_rows.extend(daily_rows)
        archives.append(archive_path)
        source_contract_symbols.add(archive_path.name.split(f"-{interval}-", 1)[0])
        cursor += timedelta(days=1)

    if len(source_contract_symbols) != 1:
        raise ValueError(f"mixed archive contract symbols for {symbol}: {sorted(source_contract_symbols)}")
    resolved_contract_symbol = next(iter(source_contract_symbols))
    if len({row["open_time"] for row in all_rows}) != len(all_rows):
        raise ValueError("duplicate timestamps across daily archives")
    if pd is None:
        raise RuntimeError("pandas is required to write normalized Parquet")

    output_dir = output_root / venue
    output_path = output_dir / f"{symbol}_{interval}.parquet"
    manifest_path = output_dir / f"{symbol}_{interval}.manifest.json"
    if (output_path.exists() or manifest_path.exists()) and not replace:
        raise FileExistsError(f"normalized output exists; pass --replace to replace it: {output_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(all_rows).sort_values("open_time", kind="stable")
    parquet_temp = output_dir / f".{output_path.name}.{uuid.uuid4().hex}.tmp"
    manifest_temp = output_dir / f".{manifest_path.name}.{uuid.uuid4().hex}.tmp"
    manifest = {
        "dataset_version": "universal_entry_klines_v1",
        "venue": venue,
        "symbol": symbol,
        "contract_symbol": resolved_contract_symbol,
        "interval": interval,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "rows": len(frame),
        "first_open_time_utc": datetime.fromtimestamp(float(frame.iloc[0]["open_time"]), timezone.utc).isoformat(),
        "last_open_time_utc": datetime.fromtimestamp(float(frame.iloc[-1]["open_time"]), timezone.utc).isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "archives": [
            {"path": str(path), "sha256": _verify_checksum(path)}
            for path in archives
        ],
    }
    try:
        frame.to_parquet(parquet_temp, index=False)
        manifest_temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        parquet_temp.replace(output_path)
        manifest_temp.replace(manifest_path)
    finally:
        parquet_temp.unlink(missing_ok=True)
        manifest_temp.unlink(missing_ok=True)
    return {"output": str(output_path), "manifest": str(manifest_path), "rows": len(frame), "venue": venue, "symbol": symbol}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venue", choices=("spot", "um"), required=True)
    parser.add_argument("--symbol", action="append", required=True, help="Opportunity symbol; repeat for a bounded batch")
    parser.add_argument("--contract-symbol", help="Archive symbol, for example 1000PEPEUSDT on USD-M")
    parser.add_argument("--interval", choices=tuple(INTERVAL_SECONDS), required=True)
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--workers", type=int, default=1, help="Bounded normalization workers (1-6)")
    parser.add_argument("--replace", action="store_true", help="Replace this isolated normalized output")
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 6:
        parser.error("--workers must be between 1 and 6")
    if args.contract_symbol and len(args.symbol) != 1:
        parser.error("--contract-symbol can only be used with one --symbol")
    try:
        def normalize(symbol: str) -> Dict[str, Any]:
            return normalize_archive_range(
                venue=args.venue,
                symbol=symbol,
                contract_symbol=args.contract_symbol,
                interval=args.interval,
                start_date=args.start_date,
                end_date=args.end_date,
                raw_root=args.raw_root,
                output_root=args.output_root,
                replace=args.replace,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            reports = list(executor.map(normalize, dict.fromkeys(item.upper() for item in args.symbol)))
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps({"workers": args.workers, "symbols": len(reports), "reports": reports}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())