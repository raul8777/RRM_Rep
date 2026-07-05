from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from config_helpers import get_param


PROFIT_START_KEY = "profit_realization_started_at"
PROFIT_START_PNL_KEY = "profit_realization_started_pnl_pct"
LAST_DECISION_KEY = "profit_realization_last_decision"
LAST_DECISION_AT_KEY = "profit_realization_last_decision_at"


@dataclass
class ProfitRealizationContext:
    symbol: str
    trade_type: str
    side: str
    pnl_pct: float
    pnl_usdt: float
    entry_price: float
    current_price: float
    opened_at: Optional[datetime] = None
    profit_started_at: Optional[datetime] = None
    peak_pnl_pct: float = 0.0
    pnl_velocity_1h: Optional[float] = None
    current_signal_strength: Optional[float] = None
    regime_mode: Optional[str] = None
    profile_label: Optional[str] = None
    breakeven_done: bool = False
    trailing_active: bool = False
    tp_extensions: int = 0
    take_profit_price: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProfitRealizationDecision:
    should_close: bool = False
    close_pct: float = 0.0
    action: str = "hold"
    reason: str = ""
    detail: str = ""
    override_target: str = ""
    priority: int = 0
    diagnostics: str = ""
    state_patch: Dict[str, Any] = field(default_factory=dict)


def parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def mark_profit_started(metadata: Dict[str, Any], pnl_pct: float, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if pnl_pct <= 0:
        return False
    if metadata.get(PROFIT_START_KEY):
        return False
    metadata[PROFIT_START_KEY] = now.isoformat()
    metadata[PROFIT_START_PNL_KEY] = round(float(pnl_pct), 6)
    return True


def _profile_scale(profile_label: Optional[str]) -> float:
    # Option 2: keep a uniform policy across A/B/C profiles.
    # Aggressiveness is still controlled by trade_type + regime scaling.
    _ = str(profile_label or "").strip().upper()
    return 1.0


def _regime_scale(regime_mode: Optional[str], trade_type: Optional[str]) -> float:
    regime = str(regime_mode or "").strip().upper()
    if regime == "RANGE_SCALP":
        return 0.70
    if regime == "PRESERVATION":
        return 0.65
    if regime == "FUTURES_SHORT":
        return 0.75 if trade_type == "futures" else 0.80
    if regime == "SPOT_BULL":
        return 0.95 if trade_type == "spot" else 0.90
    return 1.0


def _trade_defaults(trade_type: str) -> Dict[str, float]:
    if trade_type == "futures":
        return {
            "min_profit_pct": 0.0030,
            "partial_minutes": 20.0,
            "full_minutes": 45.0,
            "drawback_pct": 0.0035,
            "partial_close_pct": 0.60,
            "max_extensions": 1.0,
            "min_velocity": 0.0005,
            "hold_signal_strength": 0.75,
        }
    return {
        "min_profit_pct": 0.0025,
        "partial_minutes": 35.0,
        "full_minutes": 70.0,
        "drawback_pct": 0.0040,
        "partial_close_pct": 0.60,
        "max_extensions": 2.0,
        "min_velocity": 0.0005,
        "hold_signal_strength": 0.75,
    }


def _held_minutes(now: datetime, started_at: Optional[datetime], fallback_at: Optional[datetime]) -> int:
    ref = started_at or fallback_at
    if ref is None:
        return 0
    try:
        delta = now - ref
        return max(0, int(delta.total_seconds() // 60))
    except Exception:
        return 0


def evaluate(context: ProfitRealizationContext, settings, now: Optional[datetime] = None) -> ProfitRealizationDecision:
    now = now or datetime.now(timezone.utc)
    trade_type = str(context.trade_type or "").strip().lower() or "spot"
    defaults = _trade_defaults(trade_type)

    enabled = bool(get_param(settings, "profit_realization_enabled", True))
    if trade_type == "futures":
        enabled = enabled and bool(get_param(settings, "profit_realization_futures_enabled", True))
        min_profit_pct = float(get_param(settings, "profit_realization_futures_min_profit_pct", defaults["min_profit_pct"]))
        partial_minutes = float(get_param(settings, "profit_realization_futures_partial_minutes", defaults["partial_minutes"]))
        full_minutes = float(get_param(settings, "profit_realization_futures_full_minutes", defaults["full_minutes"]))
        drawback_pct = float(get_param(settings, "profit_realization_futures_drawback_pct", defaults["drawback_pct"]))
        partial_close_pct = float(get_param(settings, "profit_realization_futures_partial_close_pct", defaults["partial_close_pct"]))
        max_extensions = int(get_param(settings, "profit_realization_futures_max_extensions", defaults["max_extensions"]))
    else:
        enabled = enabled and bool(get_param(settings, "profit_realization_spot_enabled", True))
        min_profit_pct = float(get_param(settings, "profit_realization_spot_min_profit_pct", defaults["min_profit_pct"]))
        partial_minutes = float(get_param(settings, "profit_realization_spot_partial_minutes", defaults["partial_minutes"]))
        full_minutes = float(get_param(settings, "profit_realization_spot_full_minutes", defaults["full_minutes"]))
        drawback_pct = float(get_param(settings, "profit_realization_spot_drawback_pct", defaults["drawback_pct"]))
        partial_close_pct = float(get_param(settings, "profit_realization_spot_partial_close_pct", defaults["partial_close_pct"]))
        max_extensions = int(get_param(settings, "profit_realization_spot_max_extensions", defaults["max_extensions"]))

    if not enabled or context.pnl_pct <= 0 or context.pnl_pct < min_profit_pct:
        return ProfitRealizationDecision()

    profile_scale = _profile_scale(context.profile_label)
    regime_scale = _regime_scale(context.regime_mode, trade_type)
    scale = max(0.50, min(1.35, profile_scale * regime_scale))

    partial_minutes = max(15.0, partial_minutes * scale)
    full_minutes = max(partial_minutes + 15.0, full_minutes * scale)
    drawback_pct = max(0.001, drawback_pct * (1.0 / max(0.75, scale)))
    min_velocity = float(defaults["min_velocity"])
    hold_signal_strength = float(defaults["hold_signal_strength"])

    held_min = _held_minutes(now, context.profit_started_at, context.opened_at)
    peak = max(float(context.peak_pnl_pct or 0.0), float(context.pnl_pct or 0.0))
    giveback_pct = max(0.0, peak - float(context.pnl_pct or 0.0))
    velocity = context.pnl_velocity_1h
    positive_velocity = velocity is None or velocity >= min_velocity
    signal_strength = context.current_signal_strength
    signal_known = signal_strength is not None
    strong_signal = signal_known and signal_strength >= hold_signal_strength
    signal_faded = signal_known and (not strong_signal)
    tp_extensions = max(0, int(context.tp_extensions or 0))
    extension_cap_hit = tp_extensions >= max_extensions

    if held_min < partial_minutes and giveback_pct < (drawback_pct * 0.5) and positive_velocity and signal_faded:
        return ProfitRealizationDecision()

    should_partial = False
    should_full = False
    reason = ""
    detail = ""
    override_target = "smarttp_extend"
    priority = 50

    if held_min >= full_minutes and ((giveback_pct >= drawback_pct) or (not positive_velocity) or extension_cap_hit or signal_faded):
        should_full = True
        reason = "profit_realization_supervisor_full"
        detail = (
            f"{trade_type.upper()} held {held_min}m in profit; pnl={context.pnl_pct*100:.2f}% peak={peak*100:.2f}% "
            f"giveback={giveback_pct*100:.2f}% >= {drawback_pct*100:.2f}% or signal faded"
        )
        priority = 90
    elif held_min >= partial_minutes and ((giveback_pct >= (drawback_pct * 0.5)) or (not positive_velocity) or extension_cap_hit or signal_faded):
        should_partial = True
        reason = "profit_realization_supervisor_partial"
        detail = (
            f"{trade_type.upper()} held {held_min}m in profit; pnl={context.pnl_pct*100:.2f}% peak={peak*100:.2f}% "
            f"giveback={giveback_pct*100:.2f}% velocity={None if velocity is None else round(float(velocity)*100, 4)}"
        )
        priority = 70

    if not should_partial and not should_full:
        return ProfitRealizationDecision()

    close_pct = 1.0 if should_full else max(0.10, min(1.0, partial_close_pct))
    diagnostics = (
        f"held_min={held_min}; partial_min={partial_minutes:.1f}; full_min={full_minutes:.1f}; "
        f"drawback={drawback_pct*100:.2f}%; giveback={giveback_pct*100:.2f}%; "
        f"velocity={None if velocity is None else round(float(velocity), 6)}; "
        f"strong_signal={strong_signal}; extensions={tp_extensions}; scale={scale:.2f}"
    )
    state_patch = {
        LAST_DECISION_KEY: {
            "action": "close",
            "reason": reason,
            "close_pct": round(close_pct, 4),
            "detail": detail,
            "diagnostics": diagnostics,
            "held_minutes": held_min,
            "giveback_pct": round(giveback_pct, 6),
            "pnl_pct": round(float(context.pnl_pct or 0.0), 6),
            "peak_pnl_pct": round(float(peak or 0.0), 6),
            "trade_type": trade_type,
            "symbol": context.symbol,
            "updated_at": now.isoformat(),
        },
        LAST_DECISION_AT_KEY: now.isoformat(),
    }
    if context.profit_started_at is None and context.pnl_pct > 0:
        state_patch[PROFIT_START_KEY] = now.isoformat()
        state_patch[PROFIT_START_PNL_KEY] = round(float(context.pnl_pct), 6)

    return ProfitRealizationDecision(
        should_close=True,
        close_pct=close_pct,
        action="close",
        reason=reason,
        detail=detail,
        override_target=override_target,
        priority=priority,
        diagnostics=diagnostics,
        state_patch=state_patch,
    )