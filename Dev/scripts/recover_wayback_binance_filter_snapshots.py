"""Recover pinned Internet Archive captures of Binance Spot Production exchangeInfo."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_NORMALIZED_ROOT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "normalized" / "pilot_development"
DEFAULT_OUTPUT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "historical_filter_snapshots" / "wayback_spot_production_2025.json"
WAYBACK_REPLAY_URL = "https://web.archive.org/web/{timestamp}id_/https://api.binance.com/api/v3/exchangeInfo"
PINNED_CAPTURES = (
    {"timestamp": "20250426005818", "cdx_digest": "5QOYG7RLER63T2XIIJQJYSNLWQOMJYZR"},
    {"timestamp": "20251209111642", "cdx_digest": "YQMOR3PAEF6QUVUQ3AS4VZNH7QKQYJ4F"},
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return _sha256_bytes(encoded)


def _decode_exchange_info(raw: bytes, archive_timestamp: str, cdx_digest: str, symbols: Iterable[str]) -> Dict[str, Any]:
    if raw.startswith(b"\x1f\x8b"):
        raw = gzip.decompress(raw)
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise ValueError("archived exchangeInfo response has an unexpected schema")
    server_time = payload.get("serverTime")
    if not isinstance(server_time, int):
        raise ValueError("archived exchangeInfo is missing integer serverTime")
    captured_date = datetime.strptime(archive_timestamp[:8], "%Y%m%d").date()
    server_date = datetime.fromtimestamp(server_time / 1000, timezone.utc).date()
    if captured_date != server_date:
        raise ValueError("exchangeInfo serverTime date does not match the pinned archive capture date")

    by_symbol = {str(item.get("symbol")): item for item in payload["symbols"] if isinstance(item, dict)}
    records = []
    for symbol in sorted(set(symbols)):
        item = by_symbol.get(symbol)
        filters = item.get("filters") if item and isinstance(item.get("filters"), list) else None
        records.append({
            "symbol": symbol,
            "status": str(item.get("status") or "unknown") if item else "not_listed_at_capture",
            "filter_snapshot": filters,
            "filter_snapshot_sha256": _canonical_hash(filters) if filters is not None else None,
        })

    captured_at = datetime.fromtimestamp(server_time / 1000, timezone.utc).isoformat()
    return {
        "venue": "spot_production",
        "source_kind": "internet_archive_replay_of_public_binance_exchangeInfo",
        "source_url": WAYBACK_REPLAY_URL.format(timestamp=archive_timestamp),
        "archive_capture_timestamp": archive_timestamp,
        "archive_cdx_digest": cdx_digest,
        "server_time": server_time,
        "captured_at": captured_at,
        "exchange_info_sha256": _sha256_bytes(raw),
        "exchange_info_symbol_count": len(payload["symbols"]),
        "symbols": records,
        "applicability": "point_observation_only_not_backdated",
    }


def _normalized_symbols(normalized_root: Path) -> List[str]:
    spot_root = normalized_root / "spot"
    um_root = normalized_root / "um"
    spot = {path.name.removesuffix("_1m.manifest.json") for path in spot_root.glob("*_1m.manifest.json")}
    um = {path.name.removesuffix("_1m.manifest.json") for path in um_root.glob("*_1m.manifest.json")}
    symbols = sorted(spot & um)
    if not symbols:
        raise FileNotFoundError("no normalized Spot/USD-M symbol intersection found")
    return symbols


def _fetch_capture(capture: Dict[str, str], symbols: Iterable[str], *, timeout: int = 30) -> Dict[str, Any]:
    url = WAYBACK_REPLAY_URL.format(timestamp=capture["timestamp"])
    request = urllib.request.Request(url, headers={"User-Agent": "universal-entry-historical-filter-audit/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"Wayback replay returned HTTP {response.status}")
        raw = response.read()
    return _decode_exchange_info(raw, capture["timestamp"], capture["cdx_digest"], symbols)


def recover_snapshots(
    *,
    normalized_root: Path = DEFAULT_NORMALIZED_ROOT,
    output_path: Path = DEFAULT_OUTPUT,
    timeout: int = 30,
) -> Dict[str, Any]:
    symbols = _normalized_symbols(normalized_root)
    captures = [_fetch_capture(capture, symbols, timeout=timeout) for capture in PINNED_CAPTURES]
    report = {
        "artifact_version": "historical_binance_filter_snapshots_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "credentials_used": False,
        "orders_submitted": False,
        "source_venue": "spot_production_only",
        "normalized_symbol_count": len(symbols),
        "captures": captures,
        "window_coverage": {
            "development_2025-02-05_to_2025-02-19": "no_capture_in_window",
            "validation_2025-12-14_to_2025-12-28": "nearest_capture_5_days_before_start; no_capture_in_window",
        },
        "venue_executable_at_time": "unknown_for_pilot_windows",
        "limitations": [
            "Snapshots are point observations from Binance Spot Production and do not describe Spot Demo or USD-M Futures Testnet.",
            "Neither capture falls inside a pilot window; no filter state is inferred between capture dates.",
            "Exchange filters alone do not establish historical account balances, order permissions, dynamic limits, or fillability.",
            "The archive digest is recorded as provenance; it is not a Binance signature.",
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(output_path)
    try:
        report["output"] = str(output_path.resolve().relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        report["output"] = str(output_path.resolve())
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--normalized-root", type=Path, default=DEFAULT_NORMALIZED_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    try:
        report = recover_snapshots(normalized_root=args.normalized_root, output_path=args.output, timeout=args.timeout)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    summary = []
    for capture in report["captures"]:
        status_counts: Dict[str, int] = {}
        for record in capture["symbols"]:
            status_counts[record["status"]] = status_counts.get(record["status"], 0) + 1
        summary.append({
            "archive_capture_timestamp": capture["archive_capture_timestamp"],
            "captured_at": capture["captured_at"],
            "exchange_info_symbol_count": capture["exchange_info_symbol_count"],
            "pilot_symbol_status_counts": status_counts,
            "exchange_info_sha256": capture["exchange_info_sha256"],
        })
    print(json.dumps({"output": report["output"], "source_venue": report["source_venue"], "captures": summary, "window_coverage": report["window_coverage"], "venue_executable_at_time": report["venue_executable_at_time"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())