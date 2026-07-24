from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple


LOSS_FOCUS_STATE_KEY = "loss_focus_state"


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


def evaluate_loss_focus(
    metadata: Optional[Dict[str, Any]],
    pnl_usdt_net: float,
    threshold_usdt: float,
    reset_usdt: float,
    now: Optional[datetime] = None,
) -> Tuple[Dict[str, Any], Optional[str]]:
    now = now or datetime.now(timezone.utc)
    state = dict((metadata or {}).get(LOSS_FOCUS_STATE_KEY) or {})
    threshold = -abs(float(threshold_usdt))
    reset = -abs(float(reset_usdt))
    pnl = float(pnl_usdt_net)
    active = bool(state.get("active"))

    if active and pnl > reset:
        state.update({
            "active": False,
            "recovered_at": now.isoformat(),
            "recovered_pnl_usdt": round(pnl, 8),
            "last_event": "loss_focus_recovered",
        })
        return state, "loss_focus_recovered"

    if pnl <= threshold:
        if not active:
            state.update({
                "active": True,
                "episodes": int(state.get("episodes", 0) or 0) + 1,
                "triggered_at": now.isoformat(),
                "trigger_pnl_usdt": round(pnl, 8),
                "minimum_pnl_usdt": round(pnl, 8),
                "last_event": "loss_focus_triggered",
            })
            return state, "loss_focus_triggered"
        if pnl < float(state.get("minimum_pnl_usdt", pnl)):
            state.update({
                "minimum_pnl_usdt": round(pnl, 8),
                "minimum_updated_at": now.isoformat(),
                "last_event": "loss_focus_drawdown_update",
            })
            return state, "loss_focus_drawdown_update"

    return state, None


def build_loss_focus_snapshot(
    *,
    position: Any,
    metadata: Optional[Dict[str, Any]],
    pnl_usdt_net: float,
    pnl_pct_net: float,
    pnl_usdt_gross: float,
    current_price: float,
    regime_mode: Optional[str],
    runtime_profile: Optional[str],
    signal_strength: Optional[float],
    signal_direction: Optional[str],
    pnl_velocity_1h: Optional[float],
    peak_pnl_pct: float,
    fear_greed_value: Optional[float],
) -> Dict[str, Any]:
    meta = dict(metadata or {})
    entry_price = float(getattr(position, "entry_price", 0.0) or 0.0)
    stop_loss = float(getattr(position, "stop_loss", 0.0) or 0.0)
    take_profit = float(getattr(position, "take_profit", 0.0) or 0.0)
    distance_to_sl_pct = None
    if current_price > 0 and stop_loss > 0:
        distance_to_sl_pct = ((current_price - stop_loss) / current_price) * 100.0
    return _json_safe({
        "position_id": str(getattr(position, "position_id", "") or ""),
        "symbol": str(getattr(position, "symbol", "") or ""),
        "trade_type": str(meta.get("trade_type") or "spot"),
        "side": str(getattr(position, "side", "") or ""),
        "entry_context_status": str(meta.get("entry_context_status") or "canonical"),
        "entry_order_id": meta.get("entry_order_id"),
        "entry_fill_id": meta.get("entry_fill_id"),
        "entry_price": entry_price,
        "entry_time": (
            getattr(position, "entry_time", None).isoformat()
            if isinstance(getattr(position, "entry_time", None), datetime)
            else getattr(position, "entry_time", None)
        ),
        "quantity": float(getattr(position, "quantity", 0.0) or 0.0),
        "entry_commission_usdt": float(getattr(position, "entry_commission", 0.0) or 0.0),
        "current_price": float(current_price),
        "pnl_usdt_net": round(float(pnl_usdt_net), 8),
        "pnl_pct_net": round(float(pnl_pct_net), 8),
        "pnl_usdt_gross": round(float(pnl_usdt_gross), 8),
        "peak_pnl_pct": round(float(peak_pnl_pct), 8),
        "pnl_velocity_1h": None if pnl_velocity_1h is None else round(float(pnl_velocity_1h), 8),
        "stop_loss": stop_loss or None,
        "take_profit": take_profit or None,
        "distance_to_sl_pct": None if distance_to_sl_pct is None else round(distance_to_sl_pct, 8),
        "trailing_active": bool(meta.get("trailing_active")),
        "breakeven_done": bool(meta.get("breakeven_done")),
        "regime_mode": regime_mode,
        "regime_at_entry": meta.get("regime_at_entry"),
        "runtime_profile": runtime_profile,
        "runtime_profile_at_entry": meta.get("profile_at_entry"),
        "quality_profile_at_entry": meta.get("peak_quality_profile"),
        "quality_score_at_entry": meta.get("peak_quality_score"),
        "expected_pnl_at_entry": meta.get("expected_pnl"),
        "success_probability_at_entry": meta.get("success_probability"),
        "signal_strength": signal_strength,
        "signal_direction": signal_direction,
        "fear_greed_value": fear_greed_value,
        "last_pred_pnl": meta.get("last_pred_pnl"),
        "last_pred_prob": meta.get("last_pred_prob"),
        "last_ug_decision": meta.get("unified_governance_last_decision"),
        "last_sso_state": meta.get("sso_pnl_snapshot"),
        "config_snapshot_id": meta.get("config_snapshot_id"),
    })
