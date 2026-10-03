"""Recover missing public USD-M development funding without implying Testnet fills."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE = REPO_ROOT / "historical_data/external/funding_rate_20250109_20260630.parquet"
MANIFEST = SOURCE.parent / "manifest.json"
NORMALIZED = REPO_ROOT / "Dev/benchmarks/universal_entry_selection/normalized/pilot_development/um"
OUTPUT = REPO_ROOT / "Dev/benchmarks/universal_entry_selection/pilot_development/public_production_funding_coverage.json"
START_MS = int(datetime(2025, 2, 5, tzinfo=timezone.utc).timestamp() * 1000)
END_MS = int(datetime(2025, 2, 20, tzinfo=timezone.utc).timestamp() * 1000)
PUBLIC_HOST = "https://fapi.binance.com"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def recover_funding(
    source: Path = SOURCE, manifest_path: Path = MANIFEST,
    normalized: Path = NORMALIZED, output: Path = OUTPUT,
) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    hashes = [row["sha256"] for row in manifest["files"] if row["file"] == source.name]
    if len(hashes) != 1 or _sha256(source) != hashes[0]:
        raise ValueError("existing funding Parquet SHA-256 does not match its manifest")
    contracts = sorted({str(json.loads(path.read_text(encoding="utf-8")).get("contract_symbol")) for path in normalized.glob("*_1m.manifest.json")})
    if not contracts or "None" in contracts:
        raise ValueError("missing pilot contract symbol manifests")
    frame = pd.read_parquet(source)
    required = {"symbol", "funding_time", "funding_rate", "source_url"}
    if not required.issubset(frame.columns):
        raise ValueError("existing funding Parquet schema is incomplete")
    times = pd.to_datetime(frame["funding_time"], utc=True, errors="raise")
    selected = frame.loc[(times >= pd.Timestamp(START_MS, unit="ms", tz="UTC")) & (times < pd.Timestamp(END_MS, unit="ms", tz="UTC"))].copy()
    selected["funding_time"] = times.loc[selected.index]
    if selected.duplicated(["symbol", "funding_time"]).any():
        raise ValueError("duplicate symbol/funding timestamp in local source")
    if selected["source_url"].isna().any() or not selected["source_url"].map(lambda url: str(url).startswith(PUBLIC_HOST + "/fapi/v1/fundingRate?")).all():
        raise ValueError("local funding source is not public Binance USD-M")
    local_contracts = set(selected["symbol"]) & set(contracts)
    missing = sorted(set(contracts) - local_contracts)
    if missing != ["1000PEPEUSDT"]:
        raise ValueError(f"unexpected public funding coverage gap: {missing}")
    url = PUBLIC_HOST + "/fapi/v1/fundingRate?" + urlencode({
        "symbol": missing[0], "startTime": START_MS, "endTime": END_MS - 1, "limit": 1000,
    })
    request = urllib.request.Request(url, headers={"User-Agent": "universal-entry-public-funding-recovery/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
    rows = json.loads(raw)
    if not isinstance(rows, list) or not rows or len(rows) >= 1000:
        raise ValueError("public funding response empty, truncated, or invalid")
    timestamps = []
    for row in rows:
        if not isinstance(row, dict) or row.get("symbol") != missing[0]:
            raise ValueError("public funding symbol mismatch")
        stamp = row.get("fundingTime")
        if not isinstance(stamp, int) or not START_MS <= stamp < END_MS:
            raise ValueError("public funding event outside development window")
        if not math.isfinite(float(row["fundingRate"])) or not math.isfinite(float(row["markPrice"])) or float(row["markPrice"]) <= 0:
            raise ValueError("invalid public funding rate or mark price")
        timestamps.append(stamp)
    if timestamps != sorted(set(timestamps)):
        raise ValueError("public funding events duplicate or out of order")
    output.parent.mkdir(parents=True, exist_ok=True)
    raw_path = output.with_name("public_production_funding_1000PEPEUSDT_20250205_20250220.json")
    if output.exists() or raw_path.exists():
        raise FileExistsError("funding recovery already saved; preserve the existing evidence")
    raw_path.write_bytes(raw)
    report = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "window_start_ms": START_MS, "window_end_exclusive_ms": END_MS,
        "source_venue": "usd_m_public_production_only", "testnet_historical_funding_verified": False,
        "local_source_path": str(source), "local_source_sha256": hashes[0],
        "local_pilot_contracts": len(local_contracts), "local_window_rows": len(selected),
        "recovered_symbol": missing[0], "recovered_rows": len(rows),
        "recovered_url": url, "recovered_raw_path": str(raw_path),
        "recovered_raw_sha256": hashlib.sha256(raw).hexdigest(),
        "first_funding_time_ms": timestamps[0], "last_funding_time_ms": timestamps[-1],
        "formula_scoring_allowed": False, "venue_executable_at_time": None,
        "limitations": "Public Production funding is not historical Testnet funding, account commissions, execution slippage or fills.",
    }
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    args = parser.parse_args()
    print(json.dumps(recover_funding(), ensure_ascii=False))