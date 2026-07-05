from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from config_helpers import get_param
from profit_realization_supervisor import (
    ProfitRealizationContext,
    evaluate as evaluate_profit_realization,
)


LOSS_STATE_KEY = "unified_governance_loss_state"
LAST_DECISION_KEY = "unified_governance_last_decision"


@dataclass
class UnifiedGovernanceContext:
    symbol: str
    trade_type: str
    side: str
    pnl_pct: float
    pnl_usdt: float
    entry_price: float
    current_price: float
    opened_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    regime_mode: Optional[str] = None
    profile_label: Optional[str] = None
    distance_to_sl_pct: Optional[float] = None
    pnl_velocity_1h: Optional[float] = None
    current_signal_strength: Optional[float] = None
    current_signal_direction: Optional[str] = None
    tp_extensions: int = 0
    peak_pnl_pct: float = 0.0
    profit_started_at: Optional[datetime] = None
    breakeven_done: bool = False
    trailing_active: bool = False
    take_profit_price: Optional[float] = None
    monitor_only: bool = False
    tier1_soft_reason: Optional[str] = None


@dataclass
class UnifiedGovernanceDecision:
    action: str = "hold"  # hold | watchdog | escalate | close
    should_close: bool = False
    close_pct: float = 0.0
    reason: str = ""
    detail: str = ""
    priority: int = 0
    override_target: str = ""
    diagnostics: str = ""
    state_patch: Dict[str, Any] = field(default_factory=dict)


def _to_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def _held_minutes(now: datetime, opened_at: Optional[datetime]) -> int:
    if opened_at is None:
        return 0
    try:
        return max(0, int((now - opened_at).total_seconds() // 60))
    except Exception:
        return 0


def _profile_value(profile_label: Optional[str], a: float, b: float, c: float) -> float:
    p = str(profile_label or "").strip().upper() or "B"
    if p == "A":
        return float(a)
    if p == "C":
        return float(c)
    return float(b)


def _get_profile_minutes(settings, trade_type: str, profile_label: Optional[str], base_key: str, fallback: Dict[str, float]) -> float:
    p = str(profile_label or "").strip().upper() or "B"
    tt = str(trade_type or "").strip().lower() or "spot"
    key_trade = f"unified_governance_{tt}_{base_key}_profile_{p.lower()}"
    key_global = f"unified_governance_{base_key}_profile_{p.lower()}"
    if tt == "futures":
        default = fallback.get(p, fallback.get("B", 60.0))
    else:
        default = fallback.get(p, fallback.get("B", 60.0))
    return float(get_param(settings, key_trade, get_param(settings, key_global, default)))


def _distance_guard(distance_to_sl_pct: Optional[float], threshold_pct: float) -> bool:
    if distance_to_sl_pct is None:
        return False
    return distance_to_sl_pct <= threshold_pct


def evaluate(context: UnifiedGovernanceContext, settings, now: Optional[datetime] = None) -> UnifiedGovernanceDecision:
    now = now or datetime.now(timezone.utc)
    meta = dict(context.metadata or {})

    enabled = bool(get_param(settings, "unified_governance_enabled", True))
    if not enabled:
        return UnifiedGovernanceDecision()

    # Profit branch: delegate to PRS under unified contract.
    if context.pnl_pct > 0 and bool(get_param(settings, "unified_governance_profit_enabled", True)):
        pr_ctx = ProfitRealizationContext(
            symbol=context.symbol,
            trade_type=context.trade_type,
            side=context.side,
            pnl_pct=context.pnl_pct,
            pnl_usdt=context.pnl_usdt,
            entry_price=context.entry_price,
            current_price=context.current_price,
            opened_at=context.opened_at,
            profit_started_at=context.profit_started_at,
            peak_pnl_pct=max(context.peak_pnl_pct, context.pnl_pct),
            pnl_velocity_1h=context.pnl_velocity_1h,
            current_signal_strength=context.current_signal_strength,
            regime_mode=context.regime_mode,
            profile_label=context.profile_label,
            breakeven_done=context.breakeven_done,
            trailing_active=context.trailing_active,
            tp_extensions=context.tp_extensions,
            take_profit_price=context.take_profit_price,
            metadata=meta,
        )
        pr_decision = evaluate_profit_realization(pr_ctx, settings)
        if pr_decision.should_close:
            state_patch = dict(pr_decision.state_patch or {})
            state_patch[LAST_DECISION_KEY] = {
                "action": "close",
                "reason": pr_decision.reason,
                "trade_state": "profit",
                "profile": str(context.profile_label or "B").upper(),
                "regime": str(context.regime_mode or ""),
                "updated_at": now.isoformat(),
            }
            return UnifiedGovernanceDecision(
                action="close",
                should_close=True,
                close_pct=float(pr_decision.close_pct or 1.0),
                reason=pr_decision.reason,
                detail=pr_decision.detail,
                priority=max(70, int(pr_decision.priority or 70)),
                override_target=pr_decision.override_target or "smarttp_extend",
                diagnostics=pr_decision.diagnostics,
                state_patch=state_patch,
            )

        return UnifiedGovernanceDecision(
            action="hold",
            should_close=False,
            priority=30,
            reason="unified_governance_profit_hold",
            diagnostics="profit_state_no_realization",
            state_patch={
                LAST_DECISION_KEY: {
                    "action": "hold",
                    "reason": "unified_governance_profit_hold",
                    "trade_state": "profit",
                    "profile": str(context.profile_label or "B").upper(),
                    "regime": str(context.regime_mode or ""),
                    "updated_at": now.isoformat(),
                }
            },
        )

    # Loss branch.
    if not bool(get_param(settings, "unified_governance_loss_enabled", True)):
        return UnifiedGovernanceDecision()

    if context.pnl_pct >= 0:
        return UnifiedGovernanceDecision()

    opened_at = context.opened_at or _to_dt(meta.get("opened_at"))
    held_min = _held_minutes(now, opened_at)

    min_age = _get_profile_minutes(
        settings,
        context.trade_type,
        context.profile_label,
        "loss_min_age",
        {"A": 30.0, "B": 60.0, "C": 45.0},
    )
    escalation_age = _get_profile_minutes(
        settings,
        context.trade_type,
        context.profile_label,
        "loss_escalation_age",
        {"A": 120.0, "B": 90.0, "C": 60.0},
    )

    distance_min = float(get_param(settings, "unified_governance_loss_distance_to_sl_min_pct", 0.5))
    accel_cfg = float(get_param(settings, "unified_governance_loss_trajectory_acceleration_pct", 0.5))
    accel_threshold = accel_cfg / 100.0 if accel_cfg > 1 else accel_cfg
    min_signal_strength = float(get_param(settings, "unified_governance_loss_signal_reversal_strength", 0.65))

    # Monitor-only guard: when degraded, close severe losers faster.
    monitor_force_loss = float(get_param(settings, "unified_governance_monitor_only_force_loss_pct", 2.0))
    monitor_force_age = float(get_param(settings, "unified_governance_monitor_only_force_age_minutes", 90.0))
    if context.monitor_only and context.pnl_pct <= -(monitor_force_loss / 100.0) and held_min >= monitor_force_age:
        state_patch = {
            LAST_DECISION_KEY: {
                "action": "close",
                "reason": "unified_governance_monitor_only_defensive_close",
                "trade_state": "loss",
                "profile": str(context.profile_label or "B").upper(),
                "regime": str(context.regime_mode or ""),
                "updated_at": now.isoformat(),
            }
        }
        return UnifiedGovernanceDecision(
            action="close",
            should_close=True,
            close_pct=1.0,
            reason="unified_governance_monitor_only_defensive_close",
            detail=f"monitor_only with loss={context.pnl_pct*100:.2f}% held_min={held_min}",
            priority=95,
            override_target="risk_containment",
            diagnostics=f"held_min={held_min}; monitor_only=True",
            state_patch=state_patch,
        )

    loss_state = dict(meta.get(LOSS_STATE_KEY) or {})
    entered_loss_at = _to_dt(loss_state.get("entered_loss_at"))
    if entered_loss_at is None:
        entered_loss_at = now
    peak_loss_pct = float(loss_state.get("peak_loss_pct", context.pnl_pct))
    peak_loss_pct = min(peak_loss_pct, context.pnl_pct)
    watchdog_level = int(loss_state.get("watchdog_level", 0) or 0)

    deterioration = False
    if context.pnl_velocity_1h is not None:
        deterioration = float(context.pnl_velocity_1h) <= -abs(accel_threshold)
    if context.current_signal_direction:
        sig = str(context.current_signal_direction).strip().upper()
        against_short = context.side.upper() == "SHORT" and sig == "BUY"
        against_long = context.side.upper() in ("LONG", "BUY") and sig == "SELL"
        if (against_short or against_long) and (context.current_signal_strength or 0.0) >= min_signal_strength:
            deterioration = True

    near_sl = _distance_guard(context.distance_to_sl_pct, distance_min)

    # Keep hard SL precedence: do not override when we are already too close.
    if near_sl:
        action = "watchdog"
        reason = "unified_governance_near_sl_hold"
        should_close = False
        close_pct = 0.0
        priority = 45
    elif held_min < min_age:
        action = "hold"
        reason = "unified_governance_loss_hold_min_age"
        should_close = False
        close_pct = 0.0
        priority = 35
    elif deterioration or held_min >= escalation_age:
        action = "close"
        reason = "unified_governance_loss_defensive_close"
        should_close = True
        close_pct = 1.0
        priority = 92
    else:
        action = "watchdog"
        reason = "unified_governance_loss_watchdog"
        should_close = False
        close_pct = 0.0
        priority = 55

    if action == "watchdog":
        watchdog_level += 1

    loss_state_patch = {
        "entered_loss_at": entered_loss_at.isoformat(),
        "peak_loss_pct": round(float(peak_loss_pct), 6),
        "watchdog_level": watchdog_level,
        "last_eval_at": now.isoformat(),
        "last_eval_reason": reason,
        "profile_applied": str(context.profile_label or "B").upper(),
        "regime_applied": str(context.regime_mode or ""),
    }

    state_patch = {
        LOSS_STATE_KEY: loss_state_patch,
        LAST_DECISION_KEY: {
            "action": action,
            "reason": reason,
            "trade_state": "loss",
            "profile": str(context.profile_label or "B").upper(),
            "regime": str(context.regime_mode or ""),
            "priority": priority,
            "updated_at": now.isoformat(),
        },
    }

    return UnifiedGovernanceDecision(
        action=action,
        should_close=should_close,
        close_pct=close_pct,
        reason=reason,
        detail=(
            f"held_min={held_min}; min_age={min_age}; esc_age={escalation_age}; "
            f"pnl_pct={context.pnl_pct*100:.2f}; near_sl={near_sl}; det={deterioration}"
        ),
        priority=priority,
        override_target="risk_containment" if should_close else "watchdog",
        diagnostics=(
            f"distance_to_sl_pct={context.distance_to_sl_pct}; "
            f"pnl_velocity_1h={context.pnl_velocity_1h}; signal_strength={context.current_signal_strength}"
        ),
        state_patch=state_patch,
    )
