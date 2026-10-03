"""Plan or acquire checksummed Binance Vision USD-M feature archives."""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import io
import json
import shutil
import sys
import urllib.error
import urllib.request
import uuid
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Dev.scripts import acquire_universal_entry_archives as archive_core


WINDOWS_DIR = archive_core.WINDOWS_DIR
RAW_ROOT = archive_core.RAW_ROOT
FEATURE_ROOT = RAW_ROOT / "um"
FEATURES = {"metrics", "bookDepth"}


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.isdigit():
        number = int(normalized)
        if number >= 100_000_000_000_000:
            unit, divisor = "us", 1_000_000
        elif number >= 100_000_000_000:
            unit, divisor = "ms", 1_000
        else:
            unit, divisor = "s", 1
        return datetime.fromtimestamp(number / divisor, timezone.utc)
    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _inspect_archive(path: Path, dataset: str, target_day: date, *, contract_symbol: str | None = None) -> Dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        bad_member = archive.testzip()
        if bad_member:
            raise ValueError(f"ZIP CRC failed for {bad_member}")
        members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(members) != 1:
            raise ValueError(f"expected one CSV member, found {len(members)}")
        with archive.open(members[0]) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
            rows = list(reader)

    if not rows:
        raise ValueError("CSV is empty")
    header = rows[0]
    data = rows[1:] if header and not header[0].strip().isdigit() else rows
    expected_header = (
        ["create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
         "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
         "count_long_short_ratio", "sum_taker_long_short_vol_ratio"]
        if dataset == "metrics" else
        ["timestamp", "percentage", "depth", "notional"]
    )
    if dataset == "metrics" and [name.strip().lower() for name in header] != expected_header:
        raise ValueError(f"unexpected metrics schema: {header}")
    if dataset == "metrics" and contract_symbol and any(row[1] != contract_symbol for row in data if len(row) > 1):
        raise ValueError(f"metrics symbol mismatch for {contract_symbol}")
    if dataset == "bookDepth" and [name.strip().lower() for name in header] != expected_header:
        raise ValueError(f"unexpected bookDepth schema: {header}")
    if any(len(row) != len(expected_header) for row in data):
        raise ValueError("CSV has inconsistent row widths")

    timestamps = [_parse_timestamp(row[0]) for row in data]
    unique_timestamps = sorted(set(timestamps))
    gaps = [int((right - left).total_seconds()) for left, right in zip(unique_timestamps, unique_timestamps[1:])]
    expected_start = datetime.combine(target_day, time.min, tzinfo=timezone.utc)
    expected_end = expected_start + timedelta(days=1)

    if dataset == "metrics":
        full_day = (
            len(data) == 288
            and bool(unique_timestamps)
            and len(timestamps) == len(unique_timestamps)
            and unique_timestamps[0] == expected_start
            and unique_timestamps[-1] == expected_end - timedelta(minutes=5)
            and set(gaps) == {300}
        )
        details = {"expected_rows": 288, "rows": len(data), "unique_timestamps": len(unique_timestamps)}
    else:
        bands = Counter(timestamps)
        full_day = (
            bool(unique_timestamps)
            and
            unique_timestamps[0] <= expected_start + timedelta(seconds=30)
            and unique_timestamps[-1] >= expected_end - timedelta(minutes=1)
            and max(gaps) <= 90
        )
        details = {
            "rows": len(data),
            "unique_snapshots": len(unique_timestamps),
            "bands_per_snapshot": dict(Counter(bands.values())),
        }

    return {
        "csv_member": members[0],
        "columns": header,
        "timestamp_format": "epoch_or_text",
        "first_utc": unique_timestamps[0].isoformat() if unique_timestamps else None,
        "last_utc": unique_timestamps[-1].isoformat() if unique_timestamps else None,
        "cadence_seconds_min": min(gaps) if gaps else None,
        "cadence_seconds_max": max(gaps) if gaps else None,
        "full_day_coverage": full_day,
        **details,
    }


def _feature_url(dataset: str, contract_symbol: str, day: date) -> str:
    filename = f"{contract_symbol}-{dataset}-{day.isoformat()}.zip"
    return (
        "https://data.binance.vision/data/futures/um/daily/"
        f"{dataset}/{contract_symbol}/{filename}"
    )


def build_plan(
    windows: Iterable[Dict[str, Any]],
    dataset: str,
    catalog: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int]:
    grouped: Dict[Tuple[str, str, date], set[str]] = defaultdict(set)
    prelisting_skips = 0
    for window in windows:
        day = window["start"]
        while day <= window["end"]:
            for opportunity_symbol in window["symbols"]:
                contract = catalog[opportunity_symbol]
                contract_symbol = contract["contract_symbol"]
                onboard = contract.get("onboard_date")
                if onboard and day < date.fromisoformat(onboard):
                    prelisting_skips += 1
                    continue
                grouped[(opportunity_symbol, contract_symbol, day)].add(window["window_id"])
            day += timedelta(days=1)

    jobs: List[Dict[str, Any]] = []
    for (opportunity_symbol, contract_symbol, day), window_ids in sorted(grouped.items()):
        filename = f"{contract_symbol}-{dataset}-{day.isoformat()}.zip"
        archive_path = Path("um") / dataset / contract_symbol / filename
        url = _feature_url(dataset, contract_symbol, day)
        jobs.append({
            "dataset": dataset,
            "venue": "um",
            "symbol": opportunity_symbol,
            "contract_symbol": contract_symbol,
            "symbol_mapping": catalog[opportunity_symbol]["mapping"],
            "date": day.isoformat(),
            "window_ids": sorted(window_ids),
            "url": url,
            "checksum_url": f"{url}.CHECKSUM",
            "archive_relative_path": str(archive_path),
        })
    return jobs, prelisting_skips


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_bytes(url: str, target: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "universal-entry-benchmark/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as output:
        shutil.copyfileobj(response, output)


def _existing_result(job: Dict[str, Any], path: Path) -> Optional[Dict[str, Any]]:
    sidecar = Path(f"{path}.CHECKSUM")
    if not path.exists():
        return None
    if not sidecar.exists():
        return {**job, "status": "existing_unverified", "archive": str(path)}
    expected = archive_core._expected_checksum(sidecar.read_text(encoding="utf-8"))
    actual = _sha256(path)
    if expected != actual:
        return {**job, "status": "existing_checksum_mismatch", "archive": str(path), "sha256": actual}
    try:
        validation = _inspect_archive(path, job["dataset"], date.fromisoformat(job["date"]), contract_symbol=job["contract_symbol"])
    except Exception as exc:
        return {**job, "status": "existing_parse_error", "archive": str(path), "error": f"{type(exc).__name__}: {str(exc)[:240]}"}
    status = "already_verified" if validation["full_day_coverage"] else "partial_day"
    return {**job, "status": status, "archive": str(path), "sha256": actual, "validation": validation}


def acquire_job(job: Dict[str, Any]) -> Dict[str, Any]:
    target = RAW_ROOT / Path(job["archive_relative_path"])
    existing = _existing_result(job, target)
    if existing is not None:
        return existing

    target.parent.mkdir(parents=True, exist_ok=True)
    archive_part = Path(f"{target}.{uuid.uuid4().hex}.part")
    checksum_part = Path(f"{target}.CHECKSUM.{uuid.uuid4().hex}.part")
    try:
        last_error: Optional[Exception] = None
        for _ in range(3):
            try:
                _download_bytes(job["url"], archive_part)
                _download_bytes(job["checksum_url"], checksum_part)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return {**job, "status": "not_found", "http_status": 404}
                last_error = exc
            except Exception as exc:
                last_error = exc
        else:
            raise last_error or RuntimeError("download failed")

        expected = archive_core._expected_checksum(checksum_part.read_text(encoding="utf-8"))
        actual = _sha256(archive_part)
        if expected != actual:
            raise ValueError("official SHA-256 checksum mismatch")
        validation = _inspect_archive(archive_part, job["dataset"], date.fromisoformat(job["date"]), contract_symbol=job["contract_symbol"])
        archive_part.replace(target)
        checksum_part.replace(Path(f"{target}.CHECKSUM"))
        return {
            **job,
            "status": "downloaded" if validation["full_day_coverage"] else "partial_day",
            "archive": str(target),
            "sha256": actual,
            "bytes": target.stat().st_size,
            "validation": validation,
        }
    except Exception as exc:
        return {**job, "status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    finally:
        for temp in (archive_part, checksum_part):
            if temp.exists():
                temp.unlink()


def _write_report(path: Path, report: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(f"{path}.{uuid.uuid4().hex}.tmp")
    with temp.open("w", encoding="utf-8") as target:
        json.dump(report, target, ensure_ascii=False, indent=2)
        target.write("\n")
    temp.replace(path)


def audit_pilot_development_features(
    dataset: str,
    *,
    normalized_root: Path = archive_core.REPO_ROOT / "Dev/benchmarks/universal_entry_selection/normalized/pilot_development/um",
    raw_root: Path = RAW_ROOT,
) -> Dict[str, Any]:
    if dataset not in FEATURES:
        raise ValueError(f"unsupported feature dataset: {dataset}")
    manifests = sorted(normalized_root.glob("*_1m.manifest.json"))
    if not manifests:
        raise ValueError("pilot development USD-M manifests missing")
    results = []
    for manifest_path in manifests:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        symbol = manifest_path.name.removesuffix("_1m.manifest.json")
        contract_symbol = str(manifest.get("contract_symbol") or "")
        if not contract_symbol:
            raise ValueError(f"missing contract symbol in {manifest_path}")
        for offset in range(15):
            day = date(2025, 2, 5) + timedelta(days=offset)
            relative = Path("um") / dataset / contract_symbol / f"{contract_symbol}-{dataset}-{day.isoformat()}.zip"
            path = raw_root / relative
            job = {"dataset": dataset, "symbol": symbol, "contract_symbol": contract_symbol, "date": day.isoformat()}
            result = _existing_result(job, path)
            results.append(result if result is not None else {**job, "status": "missing_archive"})
    counts = Counter(item["status"] for item in results)
    return {
        "dataset": dataset,
        "source_venue": "binance_vision_usd_m_public_market_only",
        "window": "pilot_development_20250205_20250219",
        "contract_count": len(manifests),
        "expected_archive_count": len(results),
        "result_counts": dict(counts),
        "all_full_days_verified": counts.get("already_verified", 0) == len(results),
        "formula_scoring_allowed": False,
        "testnet_account_or_filter_history_verified": False,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=sorted(FEATURES), required=True)
    parser.add_argument("--audit-pilot-development", action="store_true", help="Verify existing 2025 pilot feature archives without network access")
    parser.add_argument("--window-id")
    parser.add_argument("--all-windows", action="store_true", help="Select all calibration windows intersecting the date range")
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-jobs", type=int, default=500)
    parser.add_argument("--download", action="store_true", help="Download and verify archives; otherwise write plan only")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    if args.audit_pilot_development:
        if args.download or args.window_id or args.all_windows or args.start_date or args.end_date:
            parser.error("pilot development audit cannot be combined with acquisition options")
        report_path = args.report or archive_core.REPO_ROOT / "Dev/benchmarks/universal_entry_selection/pilot_development" / f"feature_source_audit_{args.dataset}.json"
        report = audit_pilot_development_features(args.dataset)
        report["generated_at"] = datetime.now(timezone.utc).isoformat()
        _write_report(report_path, report)
        print(json.dumps({"report": str(report_path), "result_counts": report["result_counts"], "all_full_days_verified": report["all_full_days_verified"]}, ensure_ascii=False))
        return 0

    if args.workers < 1 or args.workers > 6:
        parser.error("--workers must be between 1 and 6")
    if args.start_date and args.end_date and args.start_date > args.end_date:
        parser.error("--start-date must be on or before --end-date")
    if args.window_id and args.all_windows:
        parser.error("use either --window-id or --all-windows, not both")
    if args.download and (not (args.window_id or args.all_windows) or not args.start_date or not args.end_date):
        parser.error("downloads require --window-id or --all-windows plus --start-date and --end-date")

    selected_window_id = None if args.all_windows else args.window_id
    windows = archive_core._load_windows(WINDOWS_DIR, selected_window_id, args.start_date, args.end_date)
    if not windows:
        parser.error("no calibration windows selected")
    symbols = {symbol for window in windows for symbol in window["symbols"]}
    catalog, catalog_metadata = archive_core._load_um_catalog(symbols)
    jobs, prelisting_skips = build_plan(windows, args.dataset, catalog)
    if args.download and len(jobs) > args.max_jobs:
        parser.error(f"planned {len(jobs)} archives exceeds --max-jobs={args.max_jobs}")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    report_path = args.report or FEATURE_ROOT / f"{args.dataset}_acquisition_{run_id}.json"
    report: Dict[str, Any] = {
        "run_id": run_id,
        "dataset": args.dataset,
        "source": "Binance Vision USD-M daily feature archives",
        "selection_scope": "all_windows" if args.all_windows else (args.window_id or "all_windows_plan"),
        "plan_only": not args.download,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "windows": [
            {"window_id": window["window_id"], "regime": window["regime"], "start": window["start"].isoformat(), "end": window["end"].isoformat(), "symbols": len(window["symbols"])}
            for window in windows
        ],
        "um_contract_catalog": catalog_metadata,
        "um_contract_mappings": catalog,
        "prelisting_targets_skipped": prelisting_skips,
        "planned_archive_count": len(jobs),
        "workers": args.workers if args.download else 0,
        "jobs": jobs if not args.download else [],
        "results": [],
    }
    _write_report(report_path, report)

    if args.download:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(acquire_job, job) for job in jobs]
            for future in concurrent.futures.as_completed(futures):
                report["results"].append(future.result())
                if len(report["results"]) % 20 == 0:
                    _write_report(report_path, report)
        counts = Counter(row.get("status", "unknown") for row in report["results"])
        report["result_counts"] = dict(counts)
        _write_report(report_path, report)

    print(json.dumps({
        "dataset": args.dataset,
        "plan_only": report["plan_only"],
        "planned_archive_count": report["planned_archive_count"],
        "prelisting_targets_skipped": report["prelisting_targets_skipped"],
        "report": str(report_path),
        "result_counts": report.get("result_counts", {}),
    }, ensure_ascii=False, indent=2))
    return 1 if any(row.get("status") == "failed" for row in report["results"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())