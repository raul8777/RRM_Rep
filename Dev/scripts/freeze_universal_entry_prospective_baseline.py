"""Freeze a non-secret baseline manifest for the approved prospective feasibility pilot."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[2]
PILOT_ROOT = REPO_ROOT / "Dev/benchmarks/universal_entry_selection"
OUTPUT_ROOT = PILOT_ROOT / "pilot_development"
SAFE_SETTINGS = (
    "futures_enabled", "futures_leverage", "max_loss_usd_per_trade", "max_per_trade_usdt",
    "staging_pilot_max_order_notional_usdt", "futures_usdt_per_trade", "futures_max_shorts",
    "futures_max_longs", "futures_max_largecap_shorts", "futures_max_new_per_cycle",
    "max_positions", "max_positions_per_symbol", "max_positions_spot_bull",
    "max_positions_range_scalp", "max_positions_preservation", "max_positions_futures_short",
    "overflow_max_extra", "futures_overflow_max_extra", "range_scalp_min_stop_atr_multiple",
    "range_scalp_direction_min_probability", "range_scalp_sl_pct", "range_scalp_tp_pct",
    "opportunity_contract_spot_entry_fee_rate", "opportunity_contract_spot_exit_fee_rate",
    "opportunity_contract_futures_entry_fee_rate", "opportunity_contract_futures_exit_fee_rate",
    "slippage_pct", "trade_horizon_minutes", "max_positions_per_symbol", "max_positions",
)
SOURCE_FILES = (
    "run_trades.py", "staged_entry.py", "directional_arbiter.py", "opportunity_contract.py",
    "entry_decision.py", "portfolio_selector.py", "futures_client.py", "binance_client.py",
    "config.py", "config_helpers.py", "pnl_predictor.py", "simulation/entry_outcome_resolver.py",
    "Dev/scripts/build_universal_entry_contract_scaffold.py",
    "Dev/scripts/build_universal_entry_scaffold_outcomes.py",
    "Dev/scripts/capture_binance_pilot_filter_snapshots.py",
    "Dev/scripts/capture_universal_entry_sandbox_commissions.py",
    "Dev/scripts/build_universal_entry_cost_sensitivity.py",
    "Dev/scripts/fit_bilateral_replay_calibration.py",
    "Dev/scripts/capture_universal_entry_staging_preflight.py",
    "Dev/scripts/freeze_universal_entry_prospective_baseline.py",
    "Dev/scripts/universal_entry_pilot_status.py",
    "docker-compose.staging.yml",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _latest_json(directory: Path, pattern: str) -> Path | None:
    paths = sorted(directory.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[0] if paths else None


def _git_snapshot() -> Dict[str, Any]:
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        status = subprocess.run(["git", "status", "--porcelain=v1"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=20, check=True).stdout
        return {"head": head, "worktree_clean": not bool(status.strip()), "dirty_status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(), "dirty_path_count": sum(bool(line.strip()) for line in status.splitlines())}
    except Exception as exc:
        external_head = os.environ.get("PILOT_BASELINE_GIT_HEAD")
        external_dirty_hash = os.environ.get("PILOT_BASELINE_DIRTY_STATUS_SHA256")
        external_dirty_count = os.environ.get("PILOT_BASELINE_DIRTY_PATH_COUNT")
        if external_head and external_dirty_hash and external_dirty_count:
            return {
                "head": external_head,
                "worktree_clean": int(external_dirty_count) == 0,
                "dirty_status_sha256": external_dirty_hash,
                "dirty_path_count": int(external_dirty_count),
                "git_snapshot_source": "host_supplied_read_only_git_snapshot",
                "container_git_unavailable": type(exc).__name__,
            }
        return {"head": None, "worktree_clean": False, "git_status_error": type(exc).__name__}


def build_baseline_manifest(*, settings: Any, output_root: Path = OUTPUT_ROOT) -> Dict[str, Any]:
    if os.environ.get("APP_ENV") != "staging" or str(os.environ.get("BINANCE_USE_TESTNET", "")).lower() != "true":
        raise ValueError("baseline capture is restricted to Binance Testnet Staging")
    if str(getattr(settings, "app_env", "staging")).lower() != "staging" or not bool(getattr(settings, "binance_use_testnet", False)):
        raise ValueError("effective settings are not Staging/Testnet")
    if str(getattr(settings, "dry_run", True)).lower() == "true" or str(getattr(settings, "allow_real_trading", False)).lower() != "true":
        raise ValueError("prospective feasibility baseline expects the active sandbox execution mode")

    settings_snapshot = {name: getattr(settings, name, None) for name in SAFE_SETTINGS}
    source_hashes = {
        name: _sha256(REPO_ROOT / name)
        for name in SOURCE_FILES
        if (REPO_ROOT / name).is_file()
    }
    proposals = PILOT_ROOT / "pilot_development/philosophy_proposals.jsonl"
    scaffold = PILOT_ROOT / "pilot_development/bilateral_contract_scaffold.jsonl"
    outcomes = PILOT_ROOT / "pilot_development/bilateral_scaffold_outcomes.jsonl"
    raw_predictions = PILOT_ROOT / "pilot_development/bilateral_raw_predictions.jsonl"
    raw_predictions_report_path = raw_predictions.with_suffix(".report.json")
    raw_predictions_report = json.loads(raw_predictions_report_path.read_text(encoding="utf-8")) if raw_predictions_report_path.is_file() else {}
    data_hashes = {
        name: _sha256(path)
        for name, path in (("development_proposals", proposals), ("historical_non_executable_scaffold", scaffold),
                           ("historical_non_executable_outcomes", outcomes), ("raw_predictions", raw_predictions))
        if path.is_file()
    }
    fee_path = _latest_json(output_root, "sandbox_commissions_*.json")
    filter_path = _latest_json(PILOT_ROOT / "prospective_filter_snapshots", "exchange_filters_*.json")
    preflight_path = _latest_json(output_root, "staging_preflight_*.json")
    cost_report_path = _latest_json(output_root, "bilateral_cost_sensitivity_*.report.json")
    cost_report = json.loads(cost_report_path.read_text(encoding="utf-8")) if cost_report_path else {}
    fee_report = json.loads(fee_path.read_text(encoding="utf-8")) if fee_path else {}
    filter_report = json.loads(filter_path.read_text(encoding="utf-8")) if filter_path else {}
    preflight_report = json.loads(preflight_path.read_text(encoding="utf-8")) if preflight_path else {}
    fees_fresh = False
    if fee_report.get("captured_at"):
        captured = datetime.fromisoformat(str(fee_report["captured_at"]).replace("Z", "+00:00"))
        fees_fresh = 0 <= (datetime.now(timezone.utc) - captured).total_seconds() <= 300
    preflight_fresh = False
    if preflight_report.get("captured_at"):
        captured = datetime.fromisoformat(str(preflight_report["captured_at"]).replace("Z", "+00:00"))
        preflight_fresh = 0 <= (datetime.now(timezone.utc) - captured).total_seconds() <= 300
    try:
        from Dev.scripts.universal_entry_pilot_status import _current_testnet_filter_evidence
        filter_evidence = _current_testnet_filter_evidence(filter_report)
    except Exception as exc:
        filter_evidence = {"verified": False, "blocking_reasons": [type(exc).__name__]}

    manifests = {}
    for venue in ("spot", "um"):
        manifests[venue] = {
            path.name.removesuffix("_1m.manifest.json"): _sha256(path)
            for path in sorted((PILOT_ROOT / "normalized/pilot_development" / venue).glob("*_1m.manifest.json"))
        }
    symbols = sorted(set(manifests["spot"]) & set(manifests["um"]))
    max_per_order = getattr(settings, "max_per_trade_usdt", None)
    staging_notional_cap = getattr(settings, "staging_pilot_max_order_notional_usdt", None)
    futures_per_order = getattr(settings, "futures_usdt_per_trade", None)
    max_positions = getattr(settings, "max_positions", None)
    max_positions_per_symbol = getattr(settings, "max_positions_per_symbol", None)
    futures_max_new_per_cycle = getattr(settings, "futures_max_new_per_cycle", None)
    mode_position_limits = {
        key: getattr(settings, key, None)
        for key in ("max_positions_spot_bull", "max_positions_range_scalp", "max_positions_preservation", "max_positions_futures_short")
    }
    overflow_limits_disabled = (
        getattr(settings, "overflow_max_extra", None) == 0
        and getattr(settings, "futures_overflow_max_extra", None) == 0
    )
    cross_venue_single_position_guard_verified = False
    limits_verified = bool(
        isinstance(max_per_order, (int, float)) and not isinstance(max_per_order, bool) and 0 < max_per_order <= 100
        and isinstance(staging_notional_cap, (int, float)) and not isinstance(staging_notional_cap, bool) and 0 < staging_notional_cap <= 100
        and isinstance(futures_per_order, (int, float)) and not isinstance(futures_per_order, bool) and 0 < futures_per_order <= 100
        and max_positions == 1 and max_positions_per_symbol == 1 and futures_max_new_per_cycle == 1
        and all(value == 1 for value in mode_position_limits.values())
        and getattr(settings, "futures_max_shorts", None) == 1
        and getattr(settings, "futures_max_longs", None) == 1
        and getattr(settings, "futures_max_largecap_shorts", None) == 1
        and overflow_limits_disabled
        and cross_venue_single_position_guard_verified
    )
    snapshot = {
        "artifact_version": "universal_entry_prospective_feasibility_baseline_v1",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "scope": "prospective_staging_functional_feasibility_only",
        "scope_approval": "user-approved 2026-10-01 follow-up; not the historical formula benchmark",
        "selector": "baseline_current_universal",
        "formula_comparison_in_scope": False,
        "profitability_claim_allowed": False,
        "historical_1154_161_cohort_reproduced": False,
        "historical_replay_account_state_required": False,
        "historical_replay_account_state_is_simulated": True,
        "historical_scaffold_and_labels_executable": False,
        "runtime": {
            "APP_ENV": os.environ.get("APP_ENV"),
            "BINANCE_USE_TESTNET": os.environ.get("BINANCE_USE_TESTNET"),
            "DRY_RUN": os.environ.get("DRY_RUN"),
            "ALLOW_REAL_TRADING": os.environ.get("ALLOW_REAL_TRADING"),
            "environment": "staging",
            "market_hosts": {"spot": "https://demo-api.binance.com/api", "futures": "https://testnet.binancefuture.com"},
        },
        "settings_non_secret_allowlist": settings_snapshot,
        "git": _git_snapshot(),
        "source_code_sha256": source_hashes,
        "development_source_manifest_sha256": manifests,
        "matched_development_symbols": symbols,
        "development_proposal_artifact_sha256": data_hashes.get("development_proposals"),
        "historical_diagnostic_artifacts_sha256": data_hashes,
        "predictor": {
            "raw_prediction_artifact_sha256": data_hashes.get("raw_predictions"),
            "weights_hash": raw_predictions_report.get("weights_sha256") or raw_predictions_report.get("predictor_weights_sha256"),
            "report_sha256": _sha256(raw_predictions_report_path) if raw_predictions_report_path.is_file() else None,
        },
        "current_fee_snapshot": {
            "path": str(fee_path.relative_to(REPO_ROOT)).replace("\\", "/") if fee_path and fee_path.is_relative_to(REPO_ROOT) else (str(fee_path) if fee_path else None), "sha256": _sha256(fee_path) if fee_path else None,
            "captured_at": fee_report.get("captured_at"), "fresh_within_300_seconds": fees_fresh,
            "verified_symbols_by_venue": {key: value.get("verified_symbols") for key, value in fee_report.get("venues", {}).items()},
            "historical_fees_verified": False,
        },
        "current_filter_snapshot": {
            "path": str(filter_path.relative_to(REPO_ROOT)).replace("\\", "/") if filter_path and filter_path.is_relative_to(REPO_ROOT) else (str(filter_path) if filter_path else None), "sha256": _sha256(filter_path) if filter_path else None,
            "captured_at": filter_report.get("captured_at"), "testnet_currentness": filter_evidence,
            "historical_filters_verified": False,
        },
        "current_account_preflight": {
            "path": str(preflight_path.relative_to(REPO_ROOT)).replace("\\", "/") if preflight_path and preflight_path.is_relative_to(REPO_ROOT) else (str(preflight_path) if preflight_path else None),
            "sha256": _sha256(preflight_path) if preflight_path else None,
            "captured_at": preflight_report.get("captured_at"),
            "fresh_within_300_seconds": preflight_fresh,
            "runtime_preflight_passed": preflight_report.get("runtime_preflight_passed") is True,
            "canary_eligible_at_capture": preflight_report.get("canary_eligible") is True,
        },
        "cost_sensitivity_report": {
            "path": str(cost_report_path.relative_to(REPO_ROOT)).replace("\\", "/") if cost_report_path and cost_report_path.is_relative_to(REPO_ROOT) else (str(cost_report_path) if cost_report_path else None),
            "rows_path": cost_report.get("rows_path"),
            "rows_sha256": cost_report.get("rows_sha256"), "historical_testnet_costs_verified": False,
            "executable_replay": False,
        },
        "current_preflight": {
            "path": str(preflight_path.relative_to(REPO_ROOT)).replace("\\", "/") if preflight_path and preflight_path.is_relative_to(REPO_ROOT) else (str(preflight_path) if preflight_path else None),
            "sha256": _sha256(preflight_path) if preflight_path else None,
            "captured_at": preflight_report.get("captured_at"),
            "fresh_within_300_seconds": preflight_fresh,
            "runtime_preflight_passed": preflight_report.get("runtime_preflight_passed") is True,
            "canary_eligible": preflight_report.get("canary_eligible") is True,
        },
        "capital_basis": {"quote_notional_scenario_usdt": 100.0, "account_balance_claim": "current_live_preflight_only", "cross_wallet_transfer": False},
        "safety": {
            "spot_margin_enabled": False, "one_open_position_max": 1, "ordinary_selector_only": True,
            "manual_order_helper": False, "effective_limits_verified": limits_verified,
            "cross_venue_single_position_guard_verified": cross_venue_single_position_guard_verified,
            "effective_limits": {
                "max_per_trade_usdt": max_per_order, "staging_pilot_max_order_notional_usdt": staging_notional_cap,
                "futures_usdt_per_trade": futures_per_order, "futures_leverage": getattr(settings, "futures_leverage", None),
                "max_positions": max_positions, "max_positions_per_symbol": max_positions_per_symbol,
                **mode_position_limits,
                "futures_max_shorts": getattr(settings, "futures_max_shorts", None),
                "futures_max_longs": getattr(settings, "futures_max_longs", None),
                "futures_max_new_per_cycle": futures_max_new_per_cycle,
                "overflow_max_extra": getattr(settings, "overflow_max_extra", None),
                "futures_overflow_max_extra": getattr(settings, "futures_overflow_max_extra", None),
                "overflow_limits_disabled": overflow_limits_disabled,
            },
        },
        "orders_submitted_by_baseline_capture": False,
        "production_touched": False,
    }
    rendered = json.dumps(snapshot, indent=2, ensure_ascii=False, allow_nan=False)
    snapshot["manifest_sha256"] = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    output_root.mkdir(parents=True, exist_ok=True)
    path = output_root / f"prospective_feasibility_baseline_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}_{uuid.uuid4().hex[:8]}.json"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)
    snapshot["path"] = str(path.relative_to(REPO_ROOT)).replace("\\", "/") if path.is_relative_to(REPO_ROOT) else str(path)
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    args = parser.parse_args()
    from config import get_settings

    try:
        report = build_baseline_manifest(settings=get_settings())
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps({"path": report["path"], "captured_at": report["captured_at"], "manifest_sha256": report["manifest_sha256"], "matched_symbols": len(report["matched_development_symbols"]), "git_head": report["git"]["head"], "worktree_clean": report["git"]["worktree_clean"], "fee_snapshot_fresh": report["current_fee_snapshot"]["fresh_within_300_seconds"], "filters_current": report["current_filter_snapshot"]["testnet_currentness"]["verified"], "orders_submitted": report["orders_submitted_by_baseline_capture"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())