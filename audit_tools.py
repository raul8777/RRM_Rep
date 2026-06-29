"""Audit consistency helpers for signal decisions and traceability."""

import json
from typing import Any, Dict, List, Optional


def _to_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def check_signal_audit_consistency(db_client, limit: int = 100) -> Dict[str, Any]:
    """
    Compare recent market_data signal rows with signal_decision audit entries.

    Returns a summary of decision coverage and a small list of mismatches.
    """
    if db_client is None:
        return {
            "available": False,
            "total_signals": 0,
            "signals_with_decision": 0,
            "auditable_signals": 0,
            "legacy_signals_without_signal_id": 0,
            "signals_missing_audit": 0,
            "coverage_pct": 0.0,
            "matched_by_signal_id": 0,
            "matched_by_symbol": 0,
            "mismatches": [],
        }

    signal_rows = db_client.execute_query(
        """
        SELECT data, timestamp
        FROM market_data
        WHERE data_type = 'signal'
        ORDER BY timestamp DESC
        LIMIT %s
        """,
        (limit,),
    )
    audit_rows = db_client.execute_query(
        """
        SELECT symbol, trade_type, event, detail, binance_confirmed, ts
        FROM trades_audit
        WHERE event = 'signal_decision'
        ORDER BY ts DESC
        LIMIT %s
        """,
        (max(limit * 5, limit),),
    )

    audit_by_signal_id: Dict[str, Dict[str, Any]] = {}
    audit_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for row in audit_rows:
        detail = _to_dict(row.get("detail"))
        symbol = row.get("symbol") or detail.get("symbol")
        if not symbol:
            continue
        entry = {
            "symbol": symbol,
            "trade_type": row.get("trade_type"),
            "detail": detail,
            "decision": detail.get("decision"),
            "reason": detail.get("reason"),
            "signal_id": detail.get("signal_id"),
            "created_at": row.get("ts"),
        }
        signal_id = detail.get("signal_id")
        if signal_id:
            audit_by_signal_id[str(signal_id)] = entry
        audit_by_symbol.setdefault(symbol, []).append(entry)

    total_signals = 0
    signals_with_decision = 0
    auditable_signals = 0
    legacy_signals_without_signal_id = 0
    matched_by_signal_id = 0
    matched_by_symbol = 0
    mismatches: List[Dict[str, Any]] = []

    for row in signal_rows:
        signal = _to_dict(row.get("data"))
        if not signal:
            continue
        total_signals += 1
        symbol = signal.get("symbol")
        signal_id = signal.get("signal_id")
        decision = signal.get("decision")
        reason = signal.get("decision_reason")
        has_decision = bool(decision and reason)
        is_auditable = bool(signal_id)

        matched = None
        if signal_id and str(signal_id) in audit_by_signal_id:
            matched = audit_by_signal_id[str(signal_id)]
            matched_by_signal_id += 1
        elif symbol and symbol in audit_by_symbol:
            for audit_entry in audit_by_symbol[symbol]:
                if audit_entry.get("decision") == decision and audit_entry.get("reason") == reason:
                    matched = audit_entry
                    matched_by_symbol += 1
                    break

        if has_decision:
            signals_with_decision += 1
            if is_auditable:
                auditable_signals += 1
            else:
                legacy_signals_without_signal_id += 1
            if is_auditable and matched is None:
                mismatches.append({
                    "symbol": symbol,
                    "signal_id": signal_id,
                    "decision": decision,
                    "reason": reason,
                    "timestamp": row.get("timestamp"),
                })

    signals_missing_audit = len(mismatches)
    coverage_pct = round((auditable_signals - signals_missing_audit) / auditable_signals * 100, 2) if auditable_signals else 100.0

    return {
        "available": True,
        "total_signals": total_signals,
        "signals_with_decision": signals_with_decision,
        "auditable_signals": auditable_signals,
        "legacy_signals_without_signal_id": legacy_signals_without_signal_id,
        "signals_missing_audit": signals_missing_audit,
        "coverage_pct": coverage_pct,
        "matched_by_signal_id": matched_by_signal_id,
        "matched_by_symbol": matched_by_symbol,
        "mismatches": mismatches[:20],
    }