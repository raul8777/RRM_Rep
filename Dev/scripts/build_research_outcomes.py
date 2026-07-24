from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import psycopg2

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = REPO_ROOT / "Dev" / "benchmarks" / "research_data" / "executed_position_outcomes.jsonl"
DEFAULT_REPORT = REPO_ROOT / "Dev" / "benchmarks" / "research_data" / "executed_position_outcomes_report.json"
EXCLUDED_REASONS = {
    "ghost_balance_mismatch",
    "dust_below_min_notional",
    "manual_close",
    "manual_urgent_cleanup_20260707",
    "manual_urgent_cleanup_remaining_20260707",
    "manual_force_close_dust_runner",
    "manual_force_close_small_runner",
    "manual_liquidation_idle_capital",
    "manual_rebalance",
}
LIVE_V1_REQUIRED = (
    "expected_pnl_pct",
    "success_probability",
    "strength",
    "rsi",
    "rsi_4h",
    "vol_spike",
    "range_position",
    "bb_position",
    "atr",
    "trend_4h",
)


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)


def _position_key(row: Dict[str, Any], metadata: Dict[str, Any]) -> str:
    close_group_id = metadata.get("close_group_id")
    if close_group_id:
        return f"close_group:{close_group_id}"
    order_id = metadata.get("entry_order_id") or metadata.get("order_id")
    entry_time = _iso(row.get("entry_time")) or ""
    if order_id:
        return f"entry_order:{order_id}:{row.get('symbol')}:{row.get('side')}:{entry_time}"
    return f"legacy:{row.get('symbol')}:{row.get('side')}:{entry_time}"


def _regime(metadata: Dict[str, Any]) -> str:
    return str(metadata.get("entry_regime") or metadata.get("regime_at_entry") or metadata.get("regime_mode") or metadata.get("regime") or "UNKNOWN").upper()


def _entry_features(row: Dict[str, Any], metadata: Dict[str, Any]) -> Dict[str, Any]:
    indicators = _as_dict(row.get("indicators_at_entry"))
    prediction = _as_dict(row.get("pnl_prediction"))
    signal = _as_dict(metadata.get("entry_signal") or metadata.get("signal_meta"))
    model = _as_dict(metadata.get("entry_model_features"))
    return {**indicators, **signal, **prediction, **metadata, **model}


def _expected_pnl_pct(features: Dict[str, Any]) -> Any:
    for key in ("expected_pnl_pct", "expected_pnl_percentage"):
        if features.get(key) is not None:
            return features[key]
    try:
        expected = float(features.get("expected_pnl"))
        size = float(features.get("position_size_usdt") or features.get("position_usdt"))
        return expected / size * 100.0 if size > 0 else None
    except (TypeError, ValueError):
        return None


def _feature_snapshot(features: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    snapshot = {key: features.get(key) for key in LIVE_V1_REQUIRED}
    snapshot["expected_pnl_pct"] = _expected_pnl_pct(features)
    missing = sorted(key for key, value in snapshot.items() if value is None)
    return snapshot, missing


def _iter_closed(conn: Any, days: int) -> Iterator[Dict[str, Any]]:
    cursor = conn.cursor(name="research_position_outcomes")
    cursor.itersize = 500
    cursor.execute(
        """
        SELECT id, symbol, side, quantity, entry_price, exit_price, entry_time, exit_time,
               pnl, pnl_percentage, close_reason, metadata, entry_commission,
               exit_commission, pnl_prediction, indicators_at_entry, is_synthetic,
               corrupt, is_corrupted, learning_eligible
        FROM trades_closed
        WHERE exit_time >= NOW() - (%s * INTERVAL '1 day')
        ORDER BY entry_time ASC, id ASC
        """,
        (days,),
    )
    columns = (
        "id", "symbol", "side", "quantity", "entry_price", "exit_price", "entry_time", "exit_time",
        "pnl", "pnl_percentage", "close_reason", "metadata", "entry_commission",
        "exit_commission", "pnl_prediction", "indicators_at_entry", "is_synthetic",
        "corrupt", "is_corrupted", "learning_eligible",
    )
    for values in cursor:
        yield dict(zip(columns, values))


def _ready_candidates(conn: Any) -> Dict[int, Dict[str, Any]]:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, symbol, trade_type, regime_mode, profile_label, status,
               ready_at, validated_at, opened_at, opened_trade_id, signal_meta, config_snapshot, data
        FROM ready_candidates
        """
    )
    columns = (
        "id", "symbol", "trade_type", "regime_mode", "profile_label", "status",
        "ready_at", "validated_at", "opened_at", "opened_trade_id", "signal_meta", "config_snapshot", "data",
    )
    result: Dict[int, Dict[str, Any]] = {}
    for values in cursor:
        row = dict(zip(columns, values))
        row["signal_meta"] = _as_dict(row["signal_meta"])
        row["config_snapshot"] = _as_dict(row["config_snapshot"])
        row["data"] = _as_dict(row["data"])
        result[int(row["id"])] = row
    return result


def _group_outcomes(rows: Iterator[Dict[str, Any]], ready: Dict[int, Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        metadata = _as_dict(row.get("metadata"))
        groups.setdefault(_position_key(row, metadata), []).append({**row, "_metadata": metadata})
    for key, group in groups.items():
        first = group[0]
        metadata = first["_metadata"]
        reasons = [str(row.get("close_reason") or "") for row in group]
        features = _entry_features(first, metadata)
        snapshot, missing = _feature_snapshot(features)
        ready_id_raw = metadata.get("ready_candidate_id")
        try:
            ready_id = int(ready_id_raw) if ready_id_raw is not None else None
        except (TypeError, ValueError):
            ready_id = None
        ready_row = ready.get(ready_id) if ready_id is not None else None
        source = "real_closed_trade"
        excluded = bool(first.get("is_synthetic") or first.get("corrupt") or first.get("is_corrupted") or any(reason in EXCLUDED_REASONS for reason in reasons))
        total_pnl = sum(float(row.get("pnl") or 0.0) for row in group)
        quantities = [abs(float(row.get("quantity") or 0.0)) for row in group]
        weighted_pct_numerator = sum(float(row.get("pnl_percentage") or 0.0) * quantity for row, quantity in zip(group, quantities))
        total_quantity = sum(quantities)
        profile = metadata.get("peak_quality_profile") or (ready_row or {}).get("profile_label")
        yield {
            "dataset_version": "executed_position_outcomes_v1",
            "source": source,
            "position_key": key,
            "close_ids": [int(row["id"]) for row in group],
            "symbol": first.get("symbol"),
            "side": first.get("side"),
            "entry_time": _iso(first.get("entry_time")),
            "exit_time": max((_iso(row.get("exit_time")) or "" for row in group), default=None),
            "regime": _regime(metadata),
            "regime_profile": metadata.get("entry_profile_label") or metadata.get("profile_label"),
            "entry_quality_profile": profile,
            "ready_candidate_id": ready_id,
            "ready_candidate": {
                "cycle_id": (ready_row or {}).get("cycle_id"),
                "status": (ready_row or {}).get("status"),
                "opened_trade_id": (ready_row or {}).get("opened_trade_id"),
                "link_status": "exact_ready_candidate" if ready_row else ("unlinked_legacy" if ready_id is None else "missing_ready_candidate"),
            },
            "close_count": len(group),
            "partial_close_count": sum(bool(row["_metadata"].get("partial_close")) for row in group),
            "exit_reason_set": sorted(set(reasons)),
            "realized_net_pnl_usdt": total_pnl,
            "realized_net_pnl_pct": weighted_pct_numerator / total_quantity if total_quantity else None,
            "entry_commission_usdt": sum(float(row.get("entry_commission") or 0.0) for row in group),
            "exit_commission_usdt": sum(float(row.get("exit_commission") or 0.0) for row in group),
            "entry_features": snapshot,
            "feature_schema_version": "range_scalp_live_v1",
            "feature_missing": missing,
            "feature_completeness": (len(LIVE_V1_REQUIRED) - len(missing)) / len(LIVE_V1_REQUIRED),
            "training_eligible": not excluded,
            "exclusion_reasons": sorted(set(reasons).intersection(EXCLUDED_REASONS)) if excluded else [],
        }


def build_outcomes(days: int, db_url: str) -> Tuple[Iterator[Dict[str, Any]], Any]:
    conn = psycopg2.connect(db_url)
    ready = _ready_candidates(conn)
    return _group_outcomes(_iter_closed(conn, days), ready), conn


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report-output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--db-url", default=os.environ.get("DB_URL", "postgresql://crypto_agent:crypto_agent_password@postgres:5432/crypto_agent"))
    args = parser.parse_args()
    outcomes, conn = build_outcomes(args.days, args.db_url)
    summary: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with args.output.open("w", encoding="utf-8") as handle:
            for outcome in outcomes:
                handle.write(json.dumps(outcome, ensure_ascii=False, default=str) + "\n")
                summary["positions"] += 1
                summary[f"regime:{outcome['regime']}"] += 1
                summary[f"link:{outcome['ready_candidate']['link_status']}"] += 1
                summary["training_eligible"] += int(outcome["training_eligible"])
                for feature in outcome["feature_missing"]:
                    missing[feature] += 1
    finally:
        conn.close()
    report = {
        "dataset_version": "executed_position_outcomes_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": args.days,
        "summary": dict(summary),
        "missing_feature_counts": dict(missing),
        "output": str(args.output),
    }
    args.report_output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
