from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict


POLICY_VERSION = "ug_loss_policy_v1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def evaluate_shadow(context: Any, decision: Any) -> Dict[str, Any]:
    lifecycle_structure_decision = getattr(context, "lifecycle_structure_decision", None) or {}
    pnl_pct = float(getattr(context, "pnl_pct", 0.0) or 0.0)
    pnl_usdt = float(getattr(context, "pnl_usdt", 0.0) or 0.0)
    distance_to_sl_pct = getattr(context, "distance_to_sl_pct", None)
    opened_at = getattr(context, "opened_at", None)
    action = str(getattr(decision, "action", "hold") or "hold")
    reason = str(getattr(decision, "reason", "") or "")
    should_close = bool(getattr(decision, "should_close", False))
    close_pct = float(getattr(decision, "close_pct", 0.0) or 0.0)
    side = str(getattr(context, "side", None) or "")
    regime_mode = str(getattr(context, "regime_mode", None) or "")
    profile_label = str(getattr(context, "profile_label", None) or "")
    return {
        "policy_version": POLICY_VERSION,
        "evaluated_at": _now_iso(),
        "lifecycle_structure_decision": lifecycle_structure_decision,
        "pnl_pct": pnl_pct,
        "pnl_usdt": pnl_usdt,
        "distance_to_sl_pct": distance_to_sl_pct,
        "opened_at": opened_at.isoformat() if hasattr(opened_at, "isoformat") else opened_at,
        "action": action,
        "reason": reason,
        "should_close": should_close,
        "close_pct": close_pct,
        "side": side,
        "regime_mode": regime_mode,
        "profile_label": profile_label,
        "would_change_execution": False,
        "applied": False,
    }


def rollout_applies(mode: str, key: str, canary_fraction: float) -> bool:
    mode_norm = str(mode or "shadow").strip().lower()
    if mode_norm == "active":
        return True
    if mode_norm != "active_canary":
        return False
    frac = max(0.0, min(1.0, float(canary_fraction or 0.0)))
    if frac <= 0.0:
        return False
    if frac >= 1.0:
        return True
    digest = hashlib.md5(str(key or "").encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) / 0xFFFFFFFF
    return bucket < frac


def apply_shadow_result_to_decision(decision: Any, shadow_result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "applied": False,
        "action": str(getattr(decision, "action", "hold") or "hold"),
        "reason": str(getattr(decision, "reason", "") or ""),
        "detail": str(getattr(decision, "detail", "") or ""),
        "should_close": bool(getattr(decision, "should_close", False)),
        "close_pct": float(getattr(decision, "close_pct", 0.0) or 0.0),
        "shadow": dict(shadow_result or {}),
    }