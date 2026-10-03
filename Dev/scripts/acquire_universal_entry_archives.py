"""Plan or acquire venue-separated Binance Vision daily kline archives."""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import io
import json
import re
import shutil
import urllib.error
import urllib.request
import uuid
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[2]
WINDOWS_DIR = REPO_ROOT / "Dev" / "benchmarks" / "windows_calibration"
COVERAGE_PATH = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "data_coverage_manifest.json"
RAW_ROOT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "raw" / "binance_archives"
UM_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
INTERVAL_SECONDS = {"1m": 60, "5m": 300, "1h": 3600}
VENUE_PATHS = {"spot": "spot", "um": "futures/um"}
CHECKSUM_PATTERN = re.compile(r"(?i)\b([0-9a-f]{64})\b")


def _as_date(value: str) -> date:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.date()


def _load_coverage(path: Path) -> Dict[Tuple[str, str, str], str]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as source:
        manifest = json.load(source)
    return {
        (entry["window_id"], entry["symbol"], entry["interval"]): entry["status"]
        for entry in manifest.get("entries", [])
    }


def _load_um_catalog(symbols: Iterable[str]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    request = urllib.request.Request(UM_EXCHANGE_INFO_URL, headers={"User-Agent": "universal-entry-benchmark/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
    payload = json.loads(raw)
    contracts = {item["symbol"]: item for item in payload.get("symbols", [])}
    catalog: Dict[str, Dict[str, Any]] = {}
    for opportunity_symbol in sorted(set(symbols)):
        contract = contracts.get(opportunity_symbol)
        mapping = "direct"
        if not contract and opportunity_symbol.endswith("USDT"):
            base = opportunity_symbol[:-4]
            contract = contracts.get(f"1000{base}USDT")
            mapping = "1000x_alias"
        if not contract:
            catalog[opportunity_symbol] = {
                "contract_symbol": opportunity_symbol,
                "mapping": "unresolved_current_catalog",
                "status": None,
                "onboard_date": None,
            }
            continue
        onboard_ms = int(contract.get("onboardDate") or 0)
        onboard_date = (
            datetime.fromtimestamp(onboard_ms / 1000, timezone.utc).date().isoformat()
            if onboard_ms > 0 else None
        )
        catalog[opportunity_symbol] = {
            "contract_symbol": contract["symbol"],
            "mapping": mapping,
            "status": contract.get("status"),
            "onboard_date": onboard_date,
        }
    metadata = {
        "source": UM_EXCHANGE_INFO_URL,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    return catalog, metadata


def _load_windows(
    windows_dir: Path,
    window_id: Optional[str],
    start_filter: Optional[date],
    end_filter: Optional[date],
) -> List[Dict[str, Any]]:
    windows: List[Dict[str, Any]] = []
    for path in sorted(windows_dir.glob("*.json")):
        if path.name == "window_plan.json":
            continue
        with path.open("r", encoding="utf-8") as source:
            item = json.load(source)
        current_id = str(item.get("window_id") or path.stem)
        if window_id and current_id != window_id:
            continue
        if not item.get("start") or not item.get("end"):
            continue
        start = _as_date(item["start"])
        end = _as_date(item["end"])
        start = max(start, start_filter) if start_filter else start
        end = min(end, end_filter) if end_filter else end
        if start > end:
            continue
        windows.append({
            "window_id": current_id,
            "regime": item.get("dominant_regime") or item.get("regime"),
            "start": start,
            "end": end,
            "symbols": sorted({str(symbol).upper() for symbol in item.get("symbols", []) if symbol}),
        })
    if window_id and not windows:
        raise ValueError(f"No calibration window found for {window_id}")
    return windows


def _archive_url(venue: str, symbol: str, interval: str, day: date) -> str:
    filename = f"{symbol}-{interval}-{day.isoformat()}.zip"
    return (
        "https://data.binance.vision/data/"
        f"{VENUE_PATHS[venue]}/daily/klines/{symbol}/{interval}/{filename}"
    )


def build_plan(
    windows: Iterable[Dict[str, Any]],
    intervals: List[str],
    venues: List[str],
    coverage: Dict[Tuple[str, str, str], str],
    um_catalog: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int, int]:
    grouped: Dict[Tuple[str, str, str, str, str, date], set[str]] = defaultdict(set)
    skipped_spot_targets = 0
    skipped_um_prelisting = 0
    for window in windows:
        day = window["start"]
        while day <= window["end"]:
            for symbol in window["symbols"]:
                for interval in intervals:
                    for venue in venues:
                        if venue == "spot" and coverage.get((window["window_id"], symbol, interval)) == "complete":
                            skipped_spot_targets += 1
                            continue
                        contract_symbol = symbol
                        mapping = "direct"
                        if venue == "um":
                            contract = um_catalog[symbol]
                            contract_symbol = contract["contract_symbol"]
                            mapping = contract["mapping"]
                            onboard_date = contract.get("onboard_date")
                            if onboard_date and day < date.fromisoformat(onboard_date):
                                skipped_um_prelisting += 1
                                continue
                        grouped[(venue, symbol, contract_symbol, mapping, interval, day)].add(window["window_id"])
            day += timedelta(days=1)

    jobs: List[Dict[str, Any]] = []
    for (venue, symbol, contract_symbol, mapping, interval, day), window_ids in sorted(grouped.items()):
        filename = f"{contract_symbol}-{interval}-{day.isoformat()}.zip"
        relative_path = Path(venue) / contract_symbol / interval / filename
        url = _archive_url(venue, contract_symbol, interval, day)
        jobs.append({
            "venue": venue,
            "symbol": symbol,
            "contract_symbol": contract_symbol,
            "symbol_mapping": mapping,
            "interval": interval,
            "date": day.isoformat(),
            "window_ids": sorted(window_ids),
            "url": url,
            "checksum_url": f"{url}.CHECKSUM",
            "archive_relative_path": str(relative_path),
        })
    return jobs, skipped_spot_targets, skipped_um_prelisting


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_checksum(text: str) -> str:
    match = CHECKSUM_PATTERN.search(text)
    if not match:
        raise ValueError("checksum sidecar does not contain a SHA-256 digest")
    return match.group(1).lower()


def _timestamp_unit(value: int) -> Tuple[str, int]:
    magnitude = abs(value)
    if magnitude >= 100_000_000_000_000:
        return "us", 1_000_000
    if magnitude >= 100_000_000_000:
        return "ms", 1_000
    return "s", 1


def _inspect_archive(path: Path, interval: str) -> Dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        bad_member = archive.testzip()
        if bad_member:
            raise ValueError(f"ZIP CRC failed for {bad_member}")
        members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(members) != 1:
            raise ValueError(f"expected one CSV member, found {len(members)}")
        with archive.open(members[0]) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
            first = next(reader, None)
            if not first:
                raise ValueError("CSV is empty")
            has_header = not first[0].strip().isdigit()
            rows = 0
            timestamps: List[int] = []
            if not has_header:
                timestamps.append(int(first[0]))
                rows += 1
            for row in reader:
                if row:
                    timestamps.append(int(row[0]))
                    rows += 1

    if not timestamps:
        raise ValueError("CSV contains no timestamped rows")
    unit, divisor = _timestamp_unit(timestamps[0])
    step = INTERVAL_SECONDS[interval] * divisor
    unique = len(set(timestamps))
    expected_rows = 86400 // INTERVAL_SECONDS[interval]
    first_seconds = timestamps[0] / divisor
    last_seconds = timestamps[-1] / divisor
    first_utc = datetime.fromtimestamp(first_seconds, timezone.utc).isoformat()
    last_utc = datetime.fromtimestamp(last_seconds, timezone.utc).isoformat()
    full_day = (
        rows == expected_rows
        and unique == rows
        and timestamps[0] % (86400 * divisor) == 0
        and timestamps[-1] - timestamps[0] == (expected_rows - 1) * step
        and all(right - left == step for left, right in zip(timestamps, timestamps[1:]))
    )
    return {
        "csv_member": members[0],
        "header": has_header,
        "columns": len(first),
        "rows": rows,
        "unique_timestamps": unique,
        "timestamp_unit": unit,
        "first_utc": first_utc,
        "last_utc": last_utc,
        "full_day_cadence": full_day,
        "expected_rows": expected_rows,
    }


def _download_bytes(url: str, target: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "universal-entry-benchmark/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as output:
        shutil.copyfileobj(response, output)


def _verified_existing(path: Path) -> Optional[Dict[str, Any]]:
    sidecar = Path(f"{path}.CHECKSUM")
    if not path.exists():
        return None
    if not sidecar.exists():
        return {"status": "existing_unverified", "archive": str(path)}
    expected = _expected_checksum(sidecar.read_text(encoding="utf-8"))
    actual = _sha256(path)
    if expected != actual:
        return {"status": "existing_checksum_mismatch", "archive": str(path), "sha256": actual}
    return {"status": "already_verified", "archive": str(path), "sha256": actual}


def acquire_job(job: Dict[str, Any], raw_root: Path) -> Dict[str, Any]:
    archive_path = raw_root / Path(job["archive_relative_path"])
    existing = _verified_existing(archive_path)
    legacy_path = raw_root / f"{job['contract_symbol']}-{job['interval']}-{job['date']}-{job['venue']}.zip"
    if existing is None:
        existing = _verified_existing(legacy_path)
    if existing is not None:
        if existing["status"] == "already_verified":
            try:
                existing["validation"] = _inspect_archive(Path(existing["archive"]), job["interval"])
            except Exception as exc:
                existing["validation_error"] = f"{type(exc).__name__}: {exc}"
                existing["status"] = "verified_archive_parse_error"
        return {**job, **existing}

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_temp = Path(f"{archive_path}.part")
    checksum_path = Path(f"{archive_path}.CHECKSUM")
    checksum_temp = Path(f"{checksum_path}.part")
    try:
        last_error: Optional[Exception] = None
        for _ in range(3):
            try:
                _download_bytes(job["url"], archive_temp)
                _download_bytes(job["checksum_url"], checksum_temp)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return {**job, "status": "not_found", "http_status": 404}
                last_error = exc
            except Exception as exc:
                last_error = exc
        else:
            raise last_error or RuntimeError("download failed")

        expected = _expected_checksum(checksum_temp.read_text(encoding="utf-8"))
        actual = _sha256(archive_temp)
        if expected != actual:
            raise ValueError("official SHA-256 checksum mismatch")
        validation = _inspect_archive(archive_temp, job["interval"])
        archive_temp.replace(archive_path)
        checksum_temp.replace(checksum_path)
        return {
            **job,
            "status": "downloaded",
            "archive": str(archive_path),
            "sha256": actual,
            "bytes": archive_path.stat().st_size,
            "validation": validation,
        }
    except Exception as exc:
        return {**job, "status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    finally:
        for temp_path in (archive_temp, checksum_temp):
            if temp_path.exists():
                temp_path.unlink()


def _write_report(path: Path, report: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = Path(f"{path}.{uuid.uuid4().hex}.tmp")
    with temp_path.open("w", encoding="utf-8") as target:
        json.dump(report, target, ensure_ascii=False, indent=2)
        target.write("\n")
    temp_path.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-id")
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--intervals", default="1m")
    parser.add_argument("--venues", default="spot,um")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-jobs", type=int, default=1000)
    parser.add_argument("--download", action="store_true", help="Download and verify archives; otherwise only write a plan")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    if args.workers < 1 or args.workers > 6:
        parser.error("--workers must be between 1 and 6")
    if args.start_date and args.end_date and args.start_date > args.end_date:
        parser.error("--start-date must be on or before --end-date")
    if args.download and (not args.window_id or not args.start_date or not args.end_date):
        parser.error("downloads require --window-id, --start-date and --end-date")

    intervals = [item.strip() for item in args.intervals.split(",") if item.strip()]
    venues = [item.strip().lower() for item in args.venues.split(",") if item.strip()]
    if not intervals or set(intervals) - set(INTERVAL_SECONDS):
        parser.error(f"--intervals must be selected from {sorted(INTERVAL_SECONDS)}")
    if not venues or set(venues) - set(VENUE_PATHS):
        parser.error(f"--venues must be selected from {sorted(VENUE_PATHS)}")

    windows = _load_windows(WINDOWS_DIR, args.window_id, args.start_date, args.end_date)
    if not windows:
        parser.error("no calibration windows selected")
    symbols = {symbol for window in windows for symbol in window["symbols"]}
    um_catalog, um_catalog_metadata = _load_um_catalog(symbols) if "um" in venues else ({}, {})
    jobs, skipped_spot_targets, skipped_um_prelisting = build_plan(
        windows, intervals, venues, _load_coverage(COVERAGE_PATH), um_catalog,
    )
    if args.download and len(jobs) > args.max_jobs:
        parser.error(f"planned {len(jobs)} archives exceeds --max-jobs={args.max_jobs}")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    report_path = args.report or RAW_ROOT / f"acquisition_{run_id}.json"
    report: Dict[str, Any] = {
        "run_id": run_id,
        "source": "Binance Vision daily klines",
        "plan_only": not args.download,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "windows": [{"window_id": w["window_id"], "regime": w["regime"], "start": w["start"].isoformat(), "end": w["end"].isoformat(), "symbols": len(w["symbols"])} for w in windows],
        "intervals": intervals,
        "venues": venues,
        "um_contract_catalog": um_catalog_metadata,
        "um_contract_mappings": um_catalog,
        "spot_complete_archive_targets_skipped": skipped_spot_targets,
        "um_prelisting_archive_targets_skipped": skipped_um_prelisting,
        "planned_archive_count": len(jobs),
        "workers": args.workers if args.download else 0,
        "jobs": jobs if not args.download else [],
        "results": [],
    }
    _write_report(report_path, report)

    if args.download:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(acquire_job, job, RAW_ROOT) for job in jobs]
            for future in concurrent.futures.as_completed(futures):
                report["results"].append(future.result())
                _write_report(report_path, report)
        summary: Dict[str, int] = defaultdict(int)
        for result in report["results"]:
            summary[result["status"]] += 1
        report["result_counts"] = dict(summary)
        _write_report(report_path, report)

    print(json.dumps({
        "plan_only": report["plan_only"],
        "planned_archive_count": report["planned_archive_count"],
        "spot_complete_archive_targets_skipped": report["spot_complete_archive_targets_skipped"],
        "um_prelisting_archive_targets_skipped": report["um_prelisting_archive_targets_skipped"],
        "report": str(report_path),
        "result_counts": report.get("result_counts", {}),
    }, ensure_ascii=False, indent=2))
    return 1 if any(result.get("status") == "failed" for result in report["results"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())