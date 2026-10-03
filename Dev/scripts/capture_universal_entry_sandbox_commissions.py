"""Capture current per-symbol Spot Demo and USD-M Testnet fee schedules (GET only)."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Iterable
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
NORMALIZED_ROOT = REPO_ROOT / "Dev/benchmarks/universal_entry_selection/normalized/pilot_development"
OUTPUT_ROOT = REPO_ROOT / "Dev/benchmarks/universal_entry_selection/pilot_development"
SPOT_HOST = "https://demo-api.binance.com"
FUTURES_HOST = "https://testnet.binancefuture.com"
ALLOWED_HOSTS = {"demo-api.binance.com", "testnet.binancefuture.com"}
REQUEST_TIMEOUT = 20


def _signed_get(
    host: str,
    path: str,
    params: Dict[str, Any],
    *,
    api_key: str,
    api_secret: str,
    timeout: int = REQUEST_TIMEOUT,
) -> tuple[Any, Dict[str, str], float]:
    if urlparse(host).scheme != "https" or urlparse(host).hostname not in ALLOWED_HOSTS:
        raise ValueError("non-sandbox host refused")
    canonical = urllib.parse.urlencode(params)
    signature = hmac.new(api_secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    url = f"{host}{path}?{canonical}&signature={signature}"
    request = urllib.request.Request(url, headers={"X-MBX-APIKEY": api_key, "User-Agent": "universal-entry-readonly-fees/1.0"})
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        headers = {key: response.headers.get(key) for key in ("Date", "X-Cache", "Via") if response.headers.get(key) is not None}
    elapsed = time.monotonic() - started
    return json.loads(body), headers, elapsed


def _server_clock(host: str, path: str) -> tuple[int, str | None]:
    if urlparse(host).scheme != "https" or urlparse(host).hostname not in ALLOWED_HOSTS:
        raise ValueError("non-sandbox host refused")
    request = urllib.request.Request(f"{host}{path}", headers={"User-Agent": "universal-entry-readonly-fees/1.0"})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        payload = json.loads(response.read())
        date_header = response.headers.get("Date")
    server_time = payload.get("serverTime") if isinstance(payload, dict) else None
    if not isinstance(server_time, int):
        raise ValueError(f"{path} omitted serverTime")
    return server_time, date_header


def _capture_venue(
    venue: str,
    host: str,
    time_path: str,
    fee_path: str,
    symbols: Iterable[str],
    *,
    api_key: str,
    api_secret: str,
) -> Dict[str, Any]:
    clock_ms, clock_date = _server_clock(host, time_path)
    clock_anchor = time.monotonic()
    clock_date_dt = parsedate_to_datetime(clock_date) if clock_date else None
    records = []
    for symbol in sorted(set(symbols)):
        timestamp = clock_ms + int((time.monotonic() - clock_anchor) * 1000)
        try:
            payload, headers, elapsed = _signed_get(
                host, fee_path,
                {"symbol": symbol, "recvWindow": 5000, "timestamp": timestamp},
                api_key=api_key, api_secret=api_secret,
            )
            if not isinstance(payload, dict) or str(payload.get("symbol")) != symbol:
                raise ValueError("commission response symbol mismatch")
            records.append({"symbol": symbol, "status": "verified", "rates": payload, "http_headers": headers, "request_elapsed_seconds": elapsed})
        except urllib.error.HTTPError as exc:
            try:
                error_payload = json.loads(exc.read())
            except Exception:
                error_payload = {}
            records.append({"symbol": symbol, "status": "unavailable", "http_status": exc.code, "binance_code": error_payload.get("code")})
        except Exception as exc:
            records.append({"symbol": symbol, "status": "unavailable", "error_type": type(exc).__name__})
    return {
        "venue": venue,
        "host": host,
        "time_endpoint": time_path,
        "time_endpoint_server_time_ms": clock_ms,
        "time_endpoint_http_date": clock_date,
        "time_endpoint_http_skew_ms": (clock_ms - int(clock_date_dt.timestamp() * 1000)) if clock_date_dt else None,
        "commission_endpoint": fee_path,
        "expected_symbols": len(set(symbols)),
        "verified_symbols": sum(row["status"] == "verified" for row in records),
        "records": records,
    }


def capture_commissions(api_key: str, api_secret: str, *, output_root: Path = OUTPUT_ROOT) -> Dict[str, Any]:
    if not api_key or not api_secret:
        raise ValueError("sandbox credentials unavailable")
    spot_symbols = set()
    um_symbols = set()
    for venue, destination in (("spot", spot_symbols), ("um", um_symbols)):
        venue_root = NORMALIZED_ROOT / venue
        for manifest_path in venue_root.glob("*_1m.manifest.json"):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            destination.add(str(manifest.get("contract_symbol") or manifest.get("symbol") or "").upper())
    if not spot_symbols or not um_symbols or "" in spot_symbols or "" in um_symbols:
        raise ValueError("normalized pilot symbol manifests missing or invalid")

    captured_at = datetime.now(timezone.utc).isoformat()
    report = {
        "artifact_version": "universal_entry_current_sandbox_commissions_v1",
        "captured_at": captured_at,
        "capture_type": "current_account_specific_fees_not_historical",
        "read_only": True,
        "credentials_used": True,
        "orders_submitted": False,
        "historical_fees_verified": False,
        "formula_scoring_allowed": False,
        "venues": {
            "spot_demo": _capture_venue("spot_demo", SPOT_HOST, "/api/v3/time", "/api/v3/account/commission", spot_symbols, api_key=api_key, api_secret=api_secret),
            "usd_m_futures_testnet": _capture_venue("usd_m_futures_testnet", FUTURES_HOST, "/fapi/v1/time", "/fapi/v1/commissionRate", um_symbols, api_key=api_key, api_secret=api_secret),
        },
        "limitations": [
            "These are current account-specific rates, not fees historically applied to the 2025 replay.",
            "They do not establish fills, slippage, balance state at historical decisions, or executable outcomes.",
        ],
    }
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"sandbox_commissions_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}_{uuid.uuid4().hex[:8]}.json"
    temp_path = output_path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp_path.replace(output_path)
    try:
        report["path"] = str(output_path.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        report["path"] = str(output_path.resolve())
    report["sha256"] = hashlib.sha256(output_path.read_bytes()).hexdigest()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    args = parser.parse_args()
    from config import get_settings

    settings = get_settings()
    if not settings.binance_use_testnet:
        parser.error("refusing account commission capture unless BINANCE_USE_TESTNET is enabled")
    try:
        report = capture_commissions(settings.binance_api_key, settings.binance_api_secret)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps({
        "path": report["path"], "sha256": report["sha256"], "captured_at": report["captured_at"],
        "spot_demo": {"verified": report["venues"]["spot_demo"]["verified_symbols"], "expected": report["venues"]["spot_demo"]["expected_symbols"]},
        "usd_m_futures_testnet": {"verified": report["venues"]["usd_m_futures_testnet"]["verified_symbols"], "expected": report["venues"]["usd_m_futures_testnet"]["expected_symbols"]},
        "orders_submitted": report["orders_submitted"], "historical_fees_verified": report["historical_fees_verified"],
    }, ensure_ascii=False))
    return 0 if all(report["venues"][venue]["verified_symbols"] == report["venues"][venue]["expected_symbols"] for venue in report["venues"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())