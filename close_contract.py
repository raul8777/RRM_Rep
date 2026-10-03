from __future__ import annotations

from typing import Any, Dict, Optional


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def infer_close_source(close_reason: str, *, manual_close: bool = False, synthetic: bool = False) -> str:
    reason = str(close_reason or "").strip().lower()
    if manual_close or reason in {"manual", "manual_close"}:
        return "manual"
    if synthetic or reason.startswith("truth_reconcile") or reason.startswith("reconcile_"):
        return "reconciliation"
    return "lifecycle"


def infer_reconciliation_state(
    close_reason: str,
    *,
    close_source: Optional[str] = None,
    exchange_confirmed: bool = False,
    synthetic: bool = False,
    partial: bool = False,
) -> str:
    reason = str(close_reason or "").strip().lower()
    source = str(close_source or "").strip().lower()
    if synthetic or source == "reconciliation" or reason.startswith("truth_reconcile"):
        return "synthetic_imported" if source == "reconciliation" and synthetic else "reconciled"
    if reason in {
        "ghost_balance_mismatch",
        "reconcile_missing_balance",
        "binance_closed_untracked",
        "pretrade_ghost_auto_close",
    }:
        return "reconciled"
    if reason in {"manual", "manual_close"} or source == "manual":
        return "manual_confirmed"
    if exchange_confirmed:
        return "exchange_confirmed"
    if partial:
        return "partial_closed"
    return "lifecycle_closed"


def apply_close_contract(
    metadata: Optional[Dict[str, Any]],
    *,
    trade_type: str,
    position_side: str,
    close_reason: str,
    close_source: Optional[str] = None,
    close_state: Optional[str] = None,
    close_intent: Optional[str] = None,
    reconciliation_state: Optional[str] = None,
    exchange_confirmed: bool = False,
    synthetic: bool = False,
    learning_eligible: Optional[bool] = None,
    close_group_id: Optional[str] = None,
    partial: bool = False,
    venue: str = "binance",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    meta = _as_dict(metadata)
    reason = str(close_reason or "").strip()
    intent = str(close_intent or reason or "").strip()
    source = infer_close_source(reason, manual_close=(close_source == "manual"), synthetic=synthetic) if close_source is None else str(close_source).strip().lower()
    state = str(close_state or ("partial_closed" if partial else "closed")).strip().lower()
    recon = reconciliation_state or infer_reconciliation_state(
        reason,
        close_source=source,
        exchange_confirmed=exchange_confirmed,
        synthetic=synthetic,
        partial=partial,
    )
    contract = {
        "version": 1,
        "venue": venue,
        "trade_type": str(trade_type or "spot").strip().lower(),
        "position_side": str(position_side or "").strip().upper(),
        "close_reason": reason,
        "close_intent": intent,
        "close_source": source,
        "close_state": state,
        "reconciliation_state": recon,
        "exchange_confirmed": bool(exchange_confirmed),
        "synthetic": bool(synthetic),
        "learning_eligible": learning_eligible,
        "close_group_id": close_group_id,
        "partial_close": bool(partial),
    }
    meta.update({k: v for k, v in contract.items() if v is not None})
    meta["close_contract"] = contract
    if extra:
        meta.update(extra)
    if partial:
        meta["partial_close"] = True
    if learning_eligible is not None:
        meta["learning_eligible"] = bool(learning_eligible)
    return meta