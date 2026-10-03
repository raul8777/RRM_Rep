from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional


POLICY_VERSION = "directional_v2"


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _align_target_stop_with_side(
    base_price: Optional[float],
    target_price: Optional[float],
    stop_price: Optional[float],
    side: str,
) -> tuple[Optional[float], Optional[float]]:
    """Swap target/stop when both are inverted for the directional side."""
    base = _number(base_price)
    target = _number(target_price)
    stop = _number(stop_price)
    if base is None or target is None or stop is None:
        return target_price, stop_price
    is_short = str(side or "").upper() == "SHORT"
    short_inverted = target >= base and stop <= base
    long_inverted = target <= base and stop >= base
    if (is_short and short_inverted) or ((not is_short) and long_inverted):
        return stop, target
    return target_price, stop_price


def evaluate_direction(
    row: Dict[str, Any],
    policy: Dict[str, Any],
    current_price: Optional[float],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    data = row.get("data") or {}
    direction = dict(data.get("direction") or {}) if isinstance(data, dict) else {}
    side = str(direction.get("side") or "LONG").upper()
    base_price = _number(direction.get("base_price")) or _number(row.get("signal_price"))
    last_price = _number(direction.get("last_observed_price")) or base_price
    price = _number(current_price)
    stored_target = _number(direction.get("target_price"))
    stored_stop = _number(direction.get("stop_price"))
    raw_target = _number(policy.get("target_price"))
    raw_stop = _number(policy.get("stop_price"))
    if stored_target is not None and stored_stop is not None:
        target_price = stored_target
        stop_price = stored_stop
    else:
        target_price, stop_price = _align_target_stop_with_side(base_price, raw_target, raw_stop, side)
    min_progress_bps = max(0.0, float(cfg.get("direction_min_progress_bps", 0.0) or 0.0))
    max_adverse_bps = max(0.0, float(cfg.get("direction_max_adverse_bps", 0.0) or 0.0))
    required = max(1, int(cfg.get("direction_required_confirmations", 1) or 1))
    previous_confirmations = int(direction.get("directional_confirmations", 0) or 0)
    state = str(direction.get("state") or "awaiting_confirmation")
    snapshot: Dict[str, Any] = {
        "policy_version": POLICY_VERSION,
        "evaluated_at": _now_iso(),
        "state_before": state,
        "side": side,
        "base_price": base_price,
        "previous_price": last_price,
        "current_price": price,
        "target_price": target_price,
        "stop_price": stop_price,
        "required_confirmations": required,
        "previous_confirmations": previous_confirmations,
        "min_progress_bps": min_progress_bps,
        "max_adverse_bps": max_adverse_bps,
        "would_change_execution": False,
    }
    if price is None:
        snapshot.update({"action": "HOLD", "reason": "DIRECTION_V2_PRICE_MISSING", "terminal": False})
        return snapshot
    if base_price is None or last_price is None:
        snapshot.update({"action": "HOLD", "reason": "DIRECTION_V2_REFERENCE_MISSING", "terminal": False})
        return snapshot
    is_short = side == "SHORT"
    sign = -1.0 if is_short else 1.0
    progress_bps = sign * (price - last_price) / last_price * 10_000.0
    base_progress_bps = sign * (price - base_price) / base_price * 10_000.0
    target_progress = True
    if target_price is not None:
        target_progress = abs(target_price - price) < abs(target_price - last_price)
    snapshot.update({
        "progress_bps": round(progress_bps, 6),
        "base_progress_bps": round(base_progress_bps, 6),
        "target_progress": target_progress,
    })
    if str(row.get("regime_mode") or "").upper() == "RANGE_SCALP" and target_price is None:
        snapshot.update({"action": "HOLD", "reason": "DIRECTION_V2_TARGET_MISSING", "terminal": False})
        return snapshot
    if target_price is not None and ((not is_short and target_price <= base_price) or (is_short and target_price >= base_price)):
        snapshot.update({"action": "INVALIDATE", "reason": "DIRECTION_V2_TARGET_INVALID", "terminal": True})
        return snapshot
    if stop_price is not None and ((not is_short and price <= stop_price) or (is_short and price >= stop_price)):
        snapshot.update({"action": "INVALIDATE", "reason": "DIRECTION_V2_STOP_BREACHED", "terminal": True})
        return snapshot
    if max_adverse_bps and base_progress_bps <= -max_adverse_bps:
        snapshot.update({"action": "INVALIDATE", "reason": "DIRECTION_V2_DRAWDOWN_LIMIT", "terminal": True})
        return snapshot
    if progress_bps >= min_progress_bps and target_progress:
        confirmations = previous_confirmations + 1
        snapshot.update({
            "action": "CONFIRM" if confirmations >= required else "HOLD",
            "reason": "DIRECTION_V2_CONFIRMED" if confirmations >= required else "DIRECTION_V2_CONFIRMING",
            "terminal": False,
            "next_confirmations": confirmations,
        })
        return snapshot
    if abs(progress_bps) < min_progress_bps:
        snapshot.update({
            "action": "HOLD",
            "reason": "DIRECTION_V2_NOISE_HOLD",
            "terminal": False,
            "next_confirmations": previous_confirmations,
        })
        return snapshot
    snapshot.update({
        "action": "HOLD",
        "reason": "DIRECTION_V2_RECOVERY_PENDING",
        "terminal": False,
        "next_confirmations": 0,
    })
    return snapshot


evaluate_shadow = evaluate_direction
