"""Capture current Binance Spot Demo and USD-M Futures Testnet filters, read-only."""
from __future__ import annotations

import argparse
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
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "prospective_filter_snapshots"
SPOT_EXCHANGE_INFO_URL = "https://demo-api.binance.com/api/v3/exchangeInfo"
UM_EXCHANGE_INFO_URL = "https://testnet.binancefuture.com/fapi/v1/exchangeInfo"
UM_DEMO_EXCHANGE_INFO_URL = "https://demo-fapi.binance.com/fapi/v1/exchangeInfo"
SPOT_TIME_URL = "https://demo-api.binance.com/api/v3/time"
UM_TIME_URL = "https://testnet.binancefuture.com/fapi/v1/time"
UM_DEMO_TIME_URL = "https://demo-fapi.binance.com/fapi/v1/time"
ALLOWED_HOSTS = {"demo-api.binance.com", "testnet.binancefuture.com", "demo-fapi.binance.com"}
REQUIRED_FILTERS = {"LOT_SIZE", "MARKET_LOT_SIZE", "PRICE_FILTER"}
SERVER_TIME_TOLERANCE_MS = 60_000
FUTURES_EXCHANGE_INFO_SAMPLES = 3
FRESHNESS_HEADERS = ("Date", "Age", "Cache-Control", "ETag", "Last-Modified", "Expires", "Via", "CF-Cache-Status", "X-Cache", "X-Cache-Hits", "Server")
CACHE_BYPASS_HEADERS = {"Cache-Control": "no-cache, no-store, max-age=0", "Pragma": "no-cache"}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return _sha256_bytes(encoded)


def _selected_response_headers(headers: Any) -> Dict[str, str]:
    return {name: str(value) for name in FRESHNESS_HEADERS if (value := headers.get(name)) is not None}


def _request_exchange_info(url: str, *, timeout: int = 20) -> tuple[bytes, Dict[str, Any], Dict[str, str]]:
    from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"exchangeInfo host is not allowlisted: {parsed.hostname}")
    split_url = urlsplit(url)
    request_url = url
    if parsed.hostname in {"testnet.binancefuture.com", "demo-fapi.binance.com"}:
        query = parse_qsl(split_url.query, keep_blank_values=True)
        query.append(("_pilot_capture_nonce", uuid.uuid4().hex))
        request_url = urlunsplit((split_url.scheme, split_url.netloc, split_url.path, urlencode(query), split_url.fragment))
    request = urllib.request.Request(request_url, headers={"User-Agent": "universal-entry-pilot-filter-capture/1.0", **CACHE_BYPASS_HEADERS})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"exchangeInfo request returned HTTP {response.status}")
        raw = response.read()
        response_headers = _selected_response_headers(response.headers)
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise ValueError("exchangeInfo response has an unexpected schema")
    return raw, payload, response_headers


def _request_server_time(url: str, *, timeout: int = 20) -> tuple[int, Dict[str, str]]:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"time endpoint host is not allowlisted: {parsed.hostname}")
    request = urllib.request.Request(url, headers={"User-Agent": "universal-entry-pilot-filter-capture/1.0", **CACHE_BYPASS_HEADERS})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"time endpoint returned HTTP {response.status}")
        payload = json.loads(response.read())
        response_headers = _selected_response_headers(response.headers)
    server_time = payload.get("serverTime") if isinstance(payload, dict) else None
    if not isinstance(server_time, int):
        raise ValueError("time endpoint response is missing integer serverTime")
    return server_time, response_headers


def _audit_server_time(exchange_info_time: Any, time_endpoint_time: int) -> Dict[str, Any]:
    if not isinstance(exchange_info_time, int):
        return {"status": "unverifiable_missing_exchange_info_server_time", "skew_ms": None}
    skew_ms = exchange_info_time - time_endpoint_time
    return {
        "status": "fresh_within_tolerance" if abs(skew_ms) <= SERVER_TIME_TOLERANCE_MS else "inconsistent",
        "skew_ms": skew_ms,
        "tolerance_ms": SERVER_TIME_TOLERANCE_MS,
    }


def _sample_exchange_info_responses(
    url: str,
    symbols_by_venue: Dict[str, List[str]],
    *,
    timeout: int,
    samples: int = FUTURES_EXCHANGE_INFO_SAMPLES,
) -> List[Dict[str, Any]]:
    result = []
    for _ in range(max(1, samples)):
        raw, payload, response_headers = _request_exchange_info(url, timeout=timeout)
        symbols = {str(item.get("symbol")): item for item in payload["symbols"] if isinstance(item, dict)}
        filter_fingerprints = {}
        for venue, venue_symbols in symbols_by_venue.items():
            for symbol in venue_symbols:
                item = symbols.get(symbol)
                if item is None:
                    filter_fingerprints[f"{venue}:{symbol}"] = None
                    continue
                filters = item.get("filters") if isinstance(item.get("filters"), list) else []
                filter_fingerprints[f"{venue}:{symbol}"] = _canonical_hash({
                    "status": item.get("status"),
                    "filters": filters,
                })
        result.append({
            "exchange_info_sha256": _sha256_bytes(raw),
            "exchange_info_catalog_sha256_excluding_server_time": _canonical_hash({
                key: value for key, value in payload.items() if key != "serverTime"
            }),
            "server_time": payload.get("serverTime"),
            "symbol_count": len(payload["symbols"]),
            "response_headers": response_headers,
            "pilot_filter_fingerprints": filter_fingerprints,
        })
    return result


def _summarize_response_samples(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    body_hashes = sorted({str(sample.get("exchange_info_sha256")) for sample in samples})
    server_times = sorted({sample.get("server_time") for sample in samples}, key=lambda value: str(value))
    catalog_hashes = sorted({
        str(sample.get("exchange_info_catalog_sha256_excluding_server_time"))
        for sample in samples
        if sample.get("exchange_info_catalog_sha256_excluding_server_time") is not None
    })
    fingerprint_maps = [sample.get("pilot_filter_fingerprints", {}) for sample in samples]
    keys = sorted({key for mapping in fingerprint_maps for key in mapping})
    unstable_filters = sorted(
        key for key in keys
        if len({json.dumps(mapping.get(key), sort_keys=True) for mapping in fingerprint_maps}) > 1
    )
    missing_filters = sorted(key for key in keys if any(mapping.get(key) is None for mapping in fingerprint_maps))
    pilot_filters_stable = bool(keys) and not unstable_filters and not missing_filters and all(
        set(mapping) == set(keys) for mapping in fingerprint_maps
    )
    return {
        "sample_count": len(samples),
        "unique_exchange_info_body_hashes": len(body_hashes),
        "exchange_info_body_hashes": body_hashes,
        "unique_server_times": len(server_times),
        "server_times": server_times,
        "unique_catalog_hashes_excluding_server_time": len(catalog_hashes),
        "catalog_hashes_excluding_server_time": catalog_hashes,
        "body_variation_limited_to_server_time": len(body_hashes) > 1 and len(catalog_hashes) == 1,
        "pilot_filter_fingerprint_count": len(keys),
        "pilot_filters_stable": pilot_filters_stable,
        "unstable_pilot_filter_fingerprints": unstable_filters,
        "missing_pilot_filter_fingerprints": missing_filters,
        "responses_consistent": len(body_hashes) == 1 and len(server_times) == 1 and not unstable_filters,
        "samples": samples,
    }


def _symbol_record(payload: Dict[str, Any], contract_symbol: str, *, venue: str, raw_sha256: str, captured_at: str) -> Dict[str, Any]:
    symbol_info = next((item for item in payload["symbols"] if item.get("symbol") == contract_symbol), None)
    if symbol_info is None:
        return {
            "status": "not_listed_at_capture",
            "contract_symbol": contract_symbol,
            "filter_snapshot": None,
            "filter_snapshot_sha256": None,
            "exchange_filters_validated": False,
            "missing_reason": "symbol_absent_from_exchangeInfo_snapshot",
        }
    filters = symbol_info.get("filters") if isinstance(symbol_info.get("filters"), list) else []
    filter_types = {str(item.get("filterType")) for item in filters if isinstance(item, dict)}
    notional_present = bool(filter_types.intersection({"MIN_NOTIONAL", "NOTIONAL"}))
    required_present = REQUIRED_FILTERS.issubset(filter_types) and notional_present
    return {
        "status": str(symbol_info.get("status") or "unknown"),
        "contract_symbol": contract_symbol,
        "base_asset": symbol_info.get("baseAsset"),
        "quote_asset": symbol_info.get("quoteAsset"),
        "filter_snapshot": filters,
        "filter_snapshot_sha256": _canonical_hash(filters),
        "exchange_filters_validated": required_present,
        "missing_filter_types": sorted((REQUIRED_FILTERS - filter_types) | (set() if notional_present else {"MIN_NOTIONAL_OR_NOTIONAL"})),
        "exchange_info_sha256": raw_sha256,
        "exchange_info_server_time": payload.get("serverTime"),
        "filter_snapshot_retrieved_at": captured_at,
        "filter_snapshot_venue": venue,
    }


def capture_pilot_filters(
    *,
    normalized_root: Path = DEFAULT_NORMALIZED_ROOT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    timeout: int = 20,
) -> Dict[str, Any]:
    spot_dir = normalized_root / "spot"
    um_dir = normalized_root / "um"
    spot_manifests = sorted(spot_dir.glob("*_1m.manifest.json"))
    um_manifests = sorted(um_dir.glob("*_1m.manifest.json"))
    symbols = sorted({path.name.removesuffix("_1m.manifest.json") for path in spot_manifests} & {path.name.removesuffix("_1m.manifest.json") for path in um_manifests})
    if not symbols:
        raise FileNotFoundError("no normalized Spot/USD-M symbol intersection found")

    spot_raw, spot_payload, spot_headers = _request_exchange_info(SPOT_EXCHANGE_INFO_URL, timeout=timeout)
    um_raw, um_payload, um_headers = _request_exchange_info(UM_EXCHANGE_INFO_URL, timeout=timeout)
    spot_time, spot_time_headers = _request_server_time(SPOT_TIME_URL, timeout=timeout)
    um_time, um_time_headers = _request_server_time(UM_TIME_URL, timeout=timeout)
    um_demo_raw, um_demo_payload, um_demo_headers = _request_exchange_info(UM_DEMO_EXCHANGE_INFO_URL, timeout=timeout)
    um_demo_time, um_demo_time_headers = _request_server_time(UM_DEMO_TIME_URL, timeout=timeout)
    spot_time_audit = _audit_server_time(spot_payload.get("serverTime"), spot_time)
    um_time_audit = _audit_server_time(um_payload.get("serverTime"), um_time)
    um_demo_time_audit = _audit_server_time(um_demo_payload.get("serverTime"), um_demo_time)
    spot_time_audit["exchange_info_http_headers"] = spot_headers
    spot_time_audit["time_endpoint_http_headers"] = spot_time_headers
    um_time_audit["exchange_info_http_headers"] = um_headers
    um_time_audit["time_endpoint_http_headers"] = um_time_headers
    um_demo_time_audit["exchange_info_http_headers"] = um_demo_headers
    um_demo_time_audit["time_endpoint_http_headers"] = um_demo_time_headers
    captured_at = datetime.now(timezone.utc).isoformat()
    spot_hash = _sha256_bytes(spot_raw)
    um_hash = _sha256_bytes(um_raw)
    um_demo_hash = _sha256_bytes(um_demo_raw)
    pilot_contract_symbols = {"spot_demo": [], "usd_m_futures_testnet": [], "usd_m_futures_demo": []}
    for symbol in symbols:
        manifest = json.loads((um_dir / f"{symbol}_1m.manifest.json").read_text(encoding="utf-8"))
        contract_symbol = str(manifest.get("contract_symbol") or symbol)
        pilot_contract_symbols["spot_demo"].append(symbol)
        pilot_contract_symbols["usd_m_futures_testnet"].append(contract_symbol)
        pilot_contract_symbols["usd_m_futures_demo"].append(contract_symbol)
    um_samples = _sample_exchange_info_responses(
        UM_EXCHANGE_INFO_URL,
        {"usd_m_futures_testnet": pilot_contract_symbols["usd_m_futures_testnet"]},
        timeout=timeout,
    )
    um_demo_samples = _sample_exchange_info_responses(
        UM_DEMO_EXCHANGE_INFO_URL,
        {"usd_m_futures_demo": pilot_contract_symbols["usd_m_futures_demo"]},
        timeout=timeout,
    )
    um_sample_audit = _summarize_response_samples(um_samples)
    um_demo_sample_audit = _summarize_response_samples(um_demo_samples)
    for time_audit, sample_audit in ((um_time_audit, um_sample_audit), (um_demo_time_audit, um_demo_sample_audit)):
        time_audit["exchange_info_time_status"] = time_audit["status"]
        time_audit["pilot_filters_stable_across_samples"] = sample_audit["pilot_filters_stable"]
        time_audit["response_body_drift_across_samples"] = sample_audit["unique_exchange_info_body_hashes"] > 1
        if not sample_audit["pilot_filters_stable"]:
            time_audit["status"] = "pilot_filter_fingerprints_inconsistent"
        elif time_audit["exchange_info_time_status"] != "fresh_within_tolerance":
            time_audit["status"] = "exchange_info_server_time_ignored_pilot_filters_stable"
        elif sample_audit["unique_exchange_info_body_hashes"] > 1:
            time_audit["status"] = "non_pilot_exchange_info_fields_drift_filters_stable"
    records = []
    for symbol in symbols:
        um_manifest = json.loads((um_dir / f"{symbol}_1m.manifest.json").read_text(encoding="utf-8"))
        um_contract_symbol = str(um_manifest.get("contract_symbol") or symbol)
        records.append({
            "opportunity_symbol": symbol,
            "spot_demo": _symbol_record(spot_payload, symbol, venue="spot_demo", raw_sha256=spot_hash, captured_at=captured_at),
            "usd_m_futures_testnet": _symbol_record(um_payload, um_contract_symbol, venue="usd_m_futures_testnet", raw_sha256=um_hash, captured_at=captured_at),
            "spot_margin_short": {
                "status": "disabled_by_pilot_policy",
                "venue_executable_at_time": False,
                "borrow_and_margin_state": "not_queried_margin_disabled",
            },
        })
    report = {
        "snapshot_version": "binance_pilot_exchange_filters_v4",
        "capture_type": "prospective_response_capture_not_historical",
        "captured_at": captured_at,
        "request_cache_policy": {
            "headers": CACHE_BYPASS_HEADERS,
            "exchange_info_nonce_query_parameter": "_pilot_capture_nonce (Futures hosts only)",
            "spot_demo_nonce_query_parameter": False,
            "response_cache_headers_recorded": ["Age", "Via", "CF-Cache-Status", "X-Cache", "X-Cache-Hits"],
        },
        "read_only": True,
        "credentials_used": False,
        "orders_submitted": False,
        "source_server_time_checks": {
            "spot_demo": {**spot_time_audit, "exchange_info_server_time": spot_payload.get("serverTime"), "time_endpoint_server_time": spot_time},
            "usd_m_futures_testnet": {**um_time_audit, "exchange_info_server_time": um_payload.get("serverTime"), "time_endpoint_server_time": um_time, "repeated_response_audit": {k: v for k, v in um_sample_audit.items() if k != "samples"}},
            "usd_m_futures_demo": {**um_demo_time_audit, "exchange_info_server_time": um_demo_payload.get("serverTime"), "time_endpoint_server_time": um_demo_time, "repeated_response_audit": {k: v for k, v in um_demo_sample_audit.items() if k != "samples"}},
        },
        "venue_executable_at_capture": {
            "spot_demo": "unknown_account_state_not_queried",
            "usd_m_futures_testnet": "unknown_account_state_not_queried",
        },
        "sources": {
            "spot_demo": {"url": SPOT_EXCHANGE_INFO_URL, "sha256": spot_hash, "server_time": spot_payload.get("serverTime"), "response_headers": spot_headers, "time_endpoint_url": SPOT_TIME_URL, "time_response_headers": spot_time_headers},
            "usd_m_futures_testnet": {"url": UM_EXCHANGE_INFO_URL, "sha256": um_hash, "server_time": um_payload.get("serverTime"), "response_headers": um_headers, "time_endpoint_url": UM_TIME_URL, "time_response_headers": um_time_headers, "repeated_samples": um_sample_audit},
            "usd_m_futures_demo_comparison_only": {"url": UM_DEMO_EXCHANGE_INFO_URL, "sha256": um_demo_hash, "server_time": um_demo_payload.get("serverTime"), "response_headers": um_demo_headers, "time_endpoint_url": UM_DEMO_TIME_URL, "time_response_headers": um_demo_time_headers, "repeated_samples": um_demo_sample_audit},
        },
        "symbols": records,
        "limitations": [
            "These are public Demo/Testnet responses observed at captured_at, not historical snapshots; USD-M exchangeInfo serverTime is explicitly non-authoritative per Binance documentation. Check USD-M /time and HTTP Date, repeated pilot filters, host, and capture age independently.",
            "They do not capture historical account balances, margin borrow availability, fills, or dynamic account limits.",
            "A successful filter-shape check does not establish account-level executability; account state was not queried.",
        ],
    }
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"exchange_filters_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}_{uuid.uuid4().hex[:8]}.json"
    temporary = output_path.with_name(f".{output_path.name}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(output_path)
    report["output"] = str(output_path.relative_to(REPO_ROOT)).replace("\\", "/")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--normalized-root", type=Path, default=DEFAULT_NORMALIZED_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args()
    try:
        report = capture_pilot_filters(normalized_root=args.normalized_root, output_root=args.output_root, timeout=args.timeout)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    validated_counts = {
        mode: sum(bool(row.get(mode, {}).get("exchange_filters_validated")) for row in report["symbols"])
        for mode in ("spot_demo", "usd_m_futures_testnet")
    }
    print(json.dumps({
        "output": report["output"],
        "captured_at": report["captured_at"],
        "read_only": report["read_only"],
        "credentials_used": report["credentials_used"],
        "orders_submitted": report["orders_submitted"],
        "source_server_time_checks": {
            venue: {"status": check["status"], "skew_ms": check["skew_ms"]}
            for venue, check in report["source_server_time_checks"].items()
        },
        "filter_shape_coverage": validated_counts,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
