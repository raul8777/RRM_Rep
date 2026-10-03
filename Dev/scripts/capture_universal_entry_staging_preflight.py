"""Capture a read-only Staging preflight without authorizing or submitting orders."""
from __future__ import annotations

import argparse
import json
import math
import os
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = REPO_ROOT / "Dev/benchmarks/universal_entry_selection/pilot_development"
API_ROOT = "http://127.0.0.1:8001"
PER_ORDER_CAP_USDT = 100.0


def _get_json(path: str, *, api_key: str | None = None, timeout: int = 45) -> Dict[str, Any]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    request = urllib.request.Request(f"{API_ROOT}{path}", headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise ValueError(f"unexpected response from {path}")
    return payload


def _position_count(payload: Dict[str, Any]) -> int | None:
    count = payload.get("count")
    if isinstance(count, int) and count >= 0:
        return count
    rows = payload.get("positions")
    return len(rows) if isinstance(rows, list) else None


def evaluate_preflight(
    *, status: Dict[str, Any], control: Dict[str, Any], capital: Dict[str, Any],
    spot_positions: Dict[str, Any], pilot_status: Dict[str, Any], settings: Any,
) -> Dict[str, Any]:
    runtime_flags = status.get("runtime_flags", {})
    spot = capital.get("spot") if isinstance(capital.get("spot"), dict) else {}
    futures = capital.get("futures") if isinstance(capital.get("futures"), dict) else {}
    spot_usdt = spot.get("usdt") if isinstance(spot.get("usdt"), dict) else {}
    futures_usdt = (futures.get("assets") or {}).get("USDT", {})
    liquidity = capital.get("proactive_liquidity") if isinstance(capital.get("proactive_liquidity"), dict) else {}
    spot_spendable = (liquidity.get("spot") or {}).get("spendable_usdt")
    futures_spendable = (liquidity.get("futures") or {}).get("spendable_usdt")
    filters = pilot_status.get("prospective_sandbox_filter_snapshot", {})
    fees = pilot_status.get("current_sandbox_commission_snapshot", {})
    phases = pilot_status.get("phases", {})
    offline_ready = bool(
        pilot_status.get("data", {}).get("development", {}).get("complete")
        and pilot_status.get("proposals", {}).get("complete")
        and pilot_status.get("philosophy_generation_audit", {}).get("row_by_row_cluster_review", {}).get("passed")
        and pilot_status.get("bilateral_contract_scaffold", {}).get("source_integrity_verified")
        and pilot_status.get("bilateral_scaffold_outcomes", {}).get("source_binding_matches")
        and pilot_status.get("replay_parity", {}).get("parity_passed")
        and phases.get("contract_tape") == "executable_tape_ready"
    )
    effective_limits = {
        "max_per_trade_usdt": getattr(settings, "max_per_trade_usdt", None),
        "staging_pilot_max_order_notional_usdt": getattr(settings, "staging_pilot_max_order_notional_usdt", None),
        "futures_usdt_per_trade": getattr(settings, "futures_usdt_per_trade", None),
        "max_positions": getattr(settings, "max_positions", None),
        "max_positions_per_symbol": getattr(settings, "max_positions_per_symbol", None),
        "futures_max_new_per_cycle": getattr(settings, "futures_max_new_per_cycle", None),
        "max_positions_spot_bull": getattr(settings, "max_positions_spot_bull", None),
        "max_positions_range_scalp": getattr(settings, "max_positions_range_scalp", None),
        "max_positions_preservation": getattr(settings, "max_positions_preservation", None),
        "max_positions_futures_short": getattr(settings, "max_positions_futures_short", None),
        "futures_max_shorts": getattr(settings, "futures_max_shorts", None),
        "futures_max_longs": getattr(settings, "futures_max_longs", None),
        "futures_max_largecap_shorts": getattr(settings, "futures_max_largecap_shorts", None),
        "overflow_max_extra": getattr(settings, "overflow_max_extra", None),
        "futures_overflow_max_extra": getattr(settings, "futures_overflow_max_extra", None),
    }
    for key in ("max_per_trade_usdt", "futures_usdt_per_trade"):
        value = effective_limits[key]
        effective_limits[f"{key}_within_100_usdt"] = (
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 < value <= PER_ORDER_CAP_USDT
        )
    effective_limits["single_position_limits_verified"] = all(
        effective_limits[key] == 1
        for key in (
            "max_positions", "max_positions_per_symbol", "futures_max_new_per_cycle",
            "max_positions_spot_bull", "max_positions_range_scalp", "max_positions_preservation",
            "max_positions_futures_short", "futures_max_shorts", "futures_max_longs",
            "futures_max_largecap_shorts",
        )
    )
    effective_limits["overflow_limits_disabled"] = (
        effective_limits["overflow_max_extra"] == 0
        and effective_limits["futures_overflow_max_extra"] == 0
    )
    cap = effective_limits["staging_pilot_max_order_notional_usdt"]
    effective_limits["staging_futures_notional_cap_verified"] = (
        isinstance(cap, (int, float)) and not isinstance(cap, bool) and math.isfinite(cap)
        and 0 < cap <= PER_ORDER_CAP_USDT
    )
    effective_limits["cross_venue_single_position_guard_verified"] = False
    effective_limits["pilot_limits_verified"] = (
        effective_limits["max_per_trade_usdt_within_100_usdt"]
        and effective_limits["futures_usdt_per_trade_within_100_usdt"]
        and effective_limits["single_position_limits_verified"]
        and effective_limits["overflow_limits_disabled"]
        and effective_limits["staging_futures_notional_cap_verified"]
        and effective_limits["cross_venue_single_position_guard_verified"]
    )
    checks = {
        "environment_is_staging": status.get("environment") == "staging" and runtime_flags.get("APP_ENV") == "staging",
        "testnet_routing_enabled": str(status.get("binance_testnet", "")).lower() == "true" and str(runtime_flags.get("BINANCE_USE_TESTNET", "")).lower() == "true",
        "active_sandbox_execution_mode": str(runtime_flags.get("DRY_RUN", "")).lower() == "false" and str(runtime_flags.get("ALLOW_REAL_TRADING", "")).lower() == "true",
        "new_entries_not_paused": (control.get("pause_new_entries") or {}).get("active") is False,
        "spot_account_read_ok": capital.get("ok") is True and spot.get("status") == "ok",
        "futures_account_read_ok": capital.get("ok") is True and futures.get("status") == "ok",
        "spot_funded_for_100usdt_cap": isinstance(spot_spendable, (int, float)) and spot_spendable >= PER_ORDER_CAP_USDT,
        "futures_funded_for_100usdt_cap": isinstance(futures_spendable, (int, float)) and futures_spendable >= PER_ORDER_CAP_USDT,
        "no_spot_positions_open": _position_count(spot_positions) == 0,
        "no_futures_positions_open": len(futures.get("open_positions") or []) == 0,
        "current_filters_verified": filters.get("filters_currentness_verified") is True,
        "current_commissions_verified": fees.get("current_account_commissions_verified") is True,
        "pilot_order_and_position_limits_enforced": effective_limits["pilot_limits_verified"],
        "offline_gates_ready": offline_ready,
    }
    runtime_checks = {key: value for key, value in checks.items() if key != "offline_gates_ready"}
    runtime_preflight_passed = all(runtime_checks.values())
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "environment": status.get("environment"),
        "runtime_flags": runtime_flags,
        "effective_state": (control.get("unified_status") or {}).get("effective_state"),
        "pause_new_entries": (control.get("pause_new_entries") or {}).get("active"),
        "spot": {
            "status": spot.get("status"), "usdt_free": spot_usdt.get("free"),
            "usdt_locked": spot_usdt.get("locked"), "spendable_usdt": spot_spendable,
            "open_position_count": _position_count(spot_positions),
        },
        "futures": {
            "status": futures.get("status"), "usdt_available": futures_usdt.get("availableBalance"),
            "spendable_usdt": futures_spendable,
            "open_position_count": len(futures.get("open_positions") or []),
        },
        "filter_snapshot_path": filters.get("path"),
        "filter_snapshot_age_seconds": (filters.get("testnet_currentness_evidence") or {}).get("capture_age_seconds"),
        "commission_snapshot_path": fees.get("path"),
        "commission_snapshot_age_seconds": fees.get("capture_age_seconds"),
        "per_order_cap_usdt": PER_ORDER_CAP_USDT,
        "effective_pilot_limits": effective_limits,
        "checks": checks,
        "runtime_preflight_passed": runtime_preflight_passed,
        "offline_gates_ready": offline_ready,
        "canary_eligible": runtime_preflight_passed and offline_ready,
        "orders_allowed": False,
        "orders_submitted": False,
        "production_touched": False,
        "limitations": [
            "Read-only preflight snapshot; it does not reserve funds or guarantee account state remains unchanged.",
            "Offline contract/calibration/cohort gates remain separate from current account readiness.",
        ],
    }


def capture_preflight(api_key: str, *, settings: Any, output_root: Path = OUTPUT_ROOT) -> Dict[str, Any]:
    if not api_key:
        raise ValueError("DASHBOARD_API_KEY unavailable")
    status = _get_json("/status")
    status["runtime_flags"] = {
        name: os.environ.get(name, "")
        for name in ("APP_ENV", "BINANCE_USE_TESTNET", "DRY_RUN", "ALLOW_REAL_TRADING")
    }
    control = _get_json("/control/status")
    capital = _get_json("/portfolio/capital", api_key=api_key)
    spot_positions = _get_json("/positions/open")
    from Dev.scripts.universal_entry_pilot_status import build_status

    pilot_status = build_status()
    report = evaluate_preflight(status=status, control=control, capital=capital, spot_positions=spot_positions, pilot_status=pilot_status, settings=settings)
    output_root.mkdir(parents=True, exist_ok=True)
    name = f"staging_preflight_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}_{uuid.uuid4().hex[:8]}.json"
    path = output_root / name
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)
    report["path"] = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    args = parser.parse_args()
    if os.environ.get("APP_ENV") != "staging" or os.environ.get("BINANCE_USE_TESTNET", "").lower() != "true":
        parser.error("refusing preflight capture outside Binance Testnet Staging")
    try:
        from config import get_settings
        report = capture_preflight(os.environ.get("DASHBOARD_API_KEY", ""), settings=get_settings())
    except Exception as exc:
        parser.error(f"read-only preflight failed: {type(exc).__name__}: {str(exc)[:180]}")
    print(json.dumps({
        "path": report["path"], "captured_at": report["captured_at"],
        "runtime_preflight_passed": report["runtime_preflight_passed"],
        "offline_gates_ready": report["offline_gates_ready"],
        "canary_eligible": report["canary_eligible"], "orders_allowed": report["orders_allowed"],
        "checks": report["checks"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())