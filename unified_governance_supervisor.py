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
    # SSO advisory — structured result passed from SmartScaleOut evaluation
    sso_advisory_should_close: bool = False
    sso_advisory_close_pct: float = 0.0
    sso_advisory_reason: str = ""
    sso_advisory_trigger: str = ""
    # PRS advisory — structured result passed from ProfitRealizationSupervisor evaluation
    prs_advisory_should_close: bool = False
    prs_advisory_close_pct: float = 0.0
    prs_advisory_reason: str = ""
    prs_advisory_detail: str = ""
    # Peak Capture Engine advisory (Wolf Hunter Profit PredExit)
    peak_capture_should_close: bool = False
    peak_capture_close_pct: float = 0.0
    peak_capture_reason: str = ""
    peak_capture_detail: str = ""
    peak_capture_prob_higher_peak: float = 0.0
    peak_capture_expected_future_pnl: float = 0.0
    # Fear & Greed index value (0-100) for exit modifier
    fear_greed_value: Optional[float] = None


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


def _loss_regime_scale(regime_mode: Optional[str], trade_type: Optional[str]) -> tuple:
    """Return (min_age_multiplier, escalation_age_multiplier) for loss branch.
    FUTURES_SHORT and fast regimes get tighter patience; PRESERVATION gets more.
    W3: regime-aware loss branch."""
    regime = str(regime_mode or "").strip().upper()
    tt = str(trade_type or "").strip().lower()
    if tt == "futures" or regime == "FUTURES_SHORT":
        return 0.40, 0.50   # e.g. B defaults: min_age 60→24m, esc_age 90→45m
    if regime == "RANGE_SCALP":
        return 0.65, 0.75   # min_age 60→39m, esc_age 90→68m
    if regime == "PRESERVATION":
        return 1.40, 1.80   # min_age 60→84m, esc_age 90→162m
    if regime == "SPOT_BULL":
        return 1.10, 1.30   # more patience in bull — may recover
    return 1.0, 1.0         # neutral


def evaluate(context: UnifiedGovernanceContext, settings, now: Optional[datetime] = None) -> UnifiedGovernanceDecision:
    now = now or datetime.now(timezone.utc)
    meta = dict(context.metadata or {})

    enabled = bool(get_param(settings, "unified_governance_enabled", True))
    if not enabled:
        return UnifiedGovernanceDecision()

    # Profit branch: UG as single arbiter, using PRS + SSO as advisories.
    if context.pnl_pct > 0 and bool(get_param(settings, "unified_governance_profit_enabled", True)):
        if not context.regime_mode:
            print(f"[UG] {context.symbol}: regime_mode=None — profit branch using neutral regime scale (trade_type={context.trade_type}, profile={context.profile_label})")

        # ── F8: Fear & Greed exit modifier ───────────────────────────────────
        # F&G < 30 (Fear): reduce patience 15% — market can reverse quickly
        # F&G > 70 (Greed): extend patience 10% — momentum is helping us
        _fng = context.fear_greed_value
        if _fng is None:
            _fng = float(meta.get("fear_greed_value", 50))
        _fng_exit_modifier = 1.0
        if _fng < 30:
            _fng_exit_modifier = 0.85
        elif _fng > 70:
            _fng_exit_modifier = 1.10

        # ── F7: SSO advisory — if SSO fired a full close, give it high weight ─
        _sso_confirms_close = context.sso_advisory_should_close and context.sso_advisory_close_pct >= 1.0
        _sso_confirms_partial = context.sso_advisory_should_close and context.sso_advisory_close_pct < 1.0

        # ── F8: Wolf Hunter Peak Capture advisory ─────────────────────────────
        _peak_capture_close = context.peak_capture_should_close
        _peak_capture_pct = float(context.peak_capture_close_pct or 1.0)
        _early_peak_protection = (
            context.peak_capture_reason == "profit_predexit_early_peak_protection"
            and 0.0 < _peak_capture_pct < 1.0
        )

        # ── PRS evaluation with F&G-modified metadata ────────────────────────
        _prs_meta = dict(meta)
        _prs_meta["ug_fng_exit_modifier"] = round(_fng_exit_modifier, 4)
        _prs_meta["ug_fng_value"] = round(float(_fng), 2)  # W8: persist real F&G value
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
            metadata=_prs_meta,
        )
        pr_decision = evaluate_profit_realization(pr_ctx, settings)

        # ── UG arbitration logic ──────────────────────────────────────────────
        # Priority matrix:
        #   SSO full close + PRS agrees       → close 100%, priority 92
        #   SSO full close alone              → close 100%, priority 82
        #   SSO partial + PRS close           → escalate to full close, priority 88  (W1)
        #   SSO partial alone                 → respect partial %, priority 75       (W1)
        #   PRS close + F&G fear (<30)        → close with urgency, priority 80
        #   PRS close (normal)                → close, priority 70-90 (from PRS)
        #   PRS advisory_should_close         → advisory was pre-computed externally (F6)
        #   All hold                          → hold
        _prs_close = pr_decision.should_close or context.prs_advisory_should_close
        _prs_reason = pr_decision.reason or context.prs_advisory_reason
        _prs_detail = pr_decision.detail or context.prs_advisory_detail
        _prs_close_pct = float(pr_decision.close_pct or context.prs_advisory_close_pct or 1.0)
        _prs_priority = int(pr_decision.priority or 70)

        _should_close = False
        _close_pct = 0.0
        _reason = ""
        _detail = ""
        _priority = 0

        if _sso_confirms_close and _prs_close:
            _should_close = True
            _close_pct = 1.0
            _reason = context.sso_advisory_reason or _prs_reason
            _detail = f"SSO {context.sso_advisory_trigger} + PRS agree | F&G={_fng:.0f} modifier={_fng_exit_modifier:.2f}"
            _priority = 92
        elif _sso_confirms_close:
            _should_close = True
            _close_pct = 1.0
            _reason = context.sso_advisory_reason
            _detail = f"SSO {context.sso_advisory_trigger} full close (PRS hold) | F&G={_fng:.0f}"
            _priority = 82
        elif _sso_confirms_partial and _prs_close:
            # W1: SSO partial + PRS also wants to close
            _allow_escalation = bool(getattr(settings, 'ug_allow_partial_to_full_escalation', False))
            if _allow_escalation:
                # escalate to full exit
                _should_close = True
                _close_pct = 1.0
                _reason = context.sso_advisory_reason or _prs_reason
                _detail = (
                    f"SSO {context.sso_advisory_trigger} partial({context.sso_advisory_close_pct*100:.0f}%) "
                    f"escalated by PRS | F&G={_fng:.0f} modifier={_fng_exit_modifier:.2f}"
                )
                _priority = 88
            else:
                # keep the largest requested partial close, never kill a winner
                _max_partial_pct = max(float(context.sso_advisory_close_pct), float(_prs_close_pct))
                _should_close = True
                _close_pct = _max_partial_pct
                _reason = context.sso_advisory_reason or _prs_reason
                _detail = (
                    f"SSO {context.sso_advisory_trigger} partial({context.sso_advisory_close_pct*100:.0f}%) "
                    f"+ PRS capped at {_max_partial_pct*100:.0f}% (no escalation) | F&G={_fng:.0f}"
                )
                _priority = 80
        elif _sso_confirms_partial:
            # W1: SSO partial alone → execute the partial close as-is
            _should_close = True
            _close_pct = float(context.sso_advisory_close_pct)
            _reason = context.sso_advisory_reason
            _detail = (
                f"SSO {context.sso_advisory_trigger} partial {context.sso_advisory_close_pct*100:.0f}% "
                f"(PRS hold) | F&G={_fng:.0f}"
            )
            _priority = 75
        elif _peak_capture_close and _sso_confirms_close:
            # Peak Capture + SSO agree → highest confidence exit
            _should_close = True
            _close_pct = 1.0
            _reason = context.peak_capture_reason or context.sso_advisory_reason
            _detail = (
                f"PeakCapture({context.peak_capture_reason}) + SSO {context.sso_advisory_trigger} "
                f"prob_higher={context.peak_capture_prob_higher_peak:.2f} | F&G={_fng:.0f}"
            )
            _priority = 93
        elif _early_peak_protection:
            _should_close = True
            _close_pct = _peak_capture_pct
            _reason = context.peak_capture_reason
            _detail = (
                f"WolfHunterEarly fixed partial({_peak_capture_pct*100:.0f}%): "
                f"{context.peak_capture_detail} | F&G={_fng:.0f}"
            )
            _priority = 84
        elif _peak_capture_close and _prs_close:
            # Peak Capture + PRS agree → escalate to full exit
            _should_close = True
            _close_pct = 1.0
            _reason = context.peak_capture_reason or _prs_reason
            _detail = (
                f"PeakCapture({context.peak_capture_reason}) + PRS agree "
                f"prob_higher={context.peak_capture_prob_higher_peak:.2f} | F&G={_fng:.0f}"
            )
            _priority = 87
        elif _peak_capture_close and _peak_capture_pct >= 1.0:
            _should_close = True
            _close_pct = 1.0
            _reason = context.peak_capture_reason
            _detail = (
                f"PeakCapture full exit: {context.peak_capture_detail} "
                f"prob_higher={context.peak_capture_prob_higher_peak:.2f} | F&G={_fng:.0f}"
            )
            _priority = 85
        elif _peak_capture_close:
            # Peak Capture partial → take profit dynamically
            _should_close = True
            _close_pct = _peak_capture_pct
            _reason = context.peak_capture_reason
            _detail = (
                f"PeakCapture partial({context.peak_capture_close_pct*100:.0f}%): "
                f"{context.peak_capture_detail} "
                f"prob_higher={context.peak_capture_prob_higher_peak:.2f} | F&G={_fng:.0f}"
            )
            _priority = 83
        elif _prs_close and _fng < 30:
            _should_close = True
            _close_pct = _prs_close_pct
            _reason = _prs_reason
            _detail = f"{_prs_detail} | F&G={_fng:.0f} fear-urgency modifier={_fng_exit_modifier:.2f}"
            _priority = 80
        elif _prs_close:
            _should_close = True
            _close_pct = _prs_close_pct
            _reason = _prs_reason
            _detail = f"{_prs_detail} | F&G={_fng:.0f} modifier={_fng_exit_modifier:.2f}"
            _priority = _prs_priority

        _ug_diagnostics = (
            f"sso_advisory={context.sso_advisory_trigger or 'none'}(close={context.sso_advisory_should_close},"
            f"pct={context.sso_advisory_close_pct:.2f}); "
            f"prs_advisory(close={context.prs_advisory_should_close}); "
            f"prs_eval(close={pr_decision.should_close},pct={pr_decision.close_pct:.2f}); "
            f"fng={_fng:.0f}(mod={_fng_exit_modifier:.2f}); "
            f"{pr_decision.diagnostics}"
        )

        if _should_close:
            state_patch = dict(pr_decision.state_patch or {})
            state_patch[LAST_DECISION_KEY] = {
                "action": "close",
                "reason": _reason,
                "trade_state": "profit",
                "profile": str(context.profile_label or "B").upper(),
                "regime": str(context.regime_mode or ""),
                "fng_modifier": _fng_exit_modifier,
                "sso_trigger": context.sso_advisory_trigger or "",
                "updated_at": now.isoformat(),
            }
            return UnifiedGovernanceDecision(
                action="close",
                should_close=True,
                close_pct=_close_pct,
                reason=_reason,
                detail=_detail,
                priority=_priority,
                override_target=pr_decision.override_target or "smarttp_extend",
                diagnostics=_ug_diagnostics,
                state_patch=state_patch,
            )

        return UnifiedGovernanceDecision(
            action="hold",
            should_close=False,
            priority=30,
            reason="unified_governance_profit_hold",
            diagnostics=_ug_diagnostics,
            state_patch={
                LAST_DECISION_KEY: {
                    "action": "hold",
                    "reason": "unified_governance_profit_hold",
                    "trade_state": "profit",
                    "profile": str(context.profile_label or "B").upper(),
                    "regime": str(context.regime_mode or ""),
                    "fng_modifier": _fng_exit_modifier,
                    "updated_at": now.isoformat(),
                }
            },
        )

    # Loss branch.
    if not bool(get_param(settings, "unified_governance_loss_enabled", True)):
        return UnifiedGovernanceDecision()

    if context.pnl_pct >= 0:
        return UnifiedGovernanceDecision()

    _loss_min_usdt = float(get_param(settings, "unified_governance_loss_min_usdt", 1.50))
    if abs(context.pnl_usdt) < _loss_min_usdt:
        return UnifiedGovernanceDecision(
            action="hold",
            should_close=False,
            priority=20,
            reason="unified_governance_loss_below_min_usdt",
            diagnostics=f"pnl_usdt={context.pnl_usdt:.4f} < min={_loss_min_usdt:.2f}",
        )

    if not context.regime_mode:
        print(f"[UG] {context.symbol}: regime_mode=None — loss branch using neutral thresholds (trade_type={context.trade_type}, profile={context.profile_label})")

    opened_at = context.opened_at or _to_dt(meta.get("opened_at"))
    held_min = _held_minutes(now, opened_at)

    # W3: regime-aware min_age and escalation_age
    _regime_min_mult, _regime_esc_mult = _loss_regime_scale(context.regime_mode, context.trade_type)
    min_age = _get_profile_minutes(
        settings,
        context.trade_type,
        context.profile_label,
        "loss_min_age",
        {"A": 30.0, "B": 60.0, "C": 45.0},
    ) * _regime_min_mult
    escalation_age = _get_profile_minutes(
        settings,
        context.trade_type,
        context.profile_label,
        "loss_escalation_age",
        {"A": 120.0, "B": 90.0, "C": 60.0},
    ) * _regime_esc_mult

    # s78-fix: in RANGE_SCALP mean-reversion is minutes-scale; cap patience for losers
    if str(context.regime_mode or "").strip().upper() == "RANGE_SCALP":
        min_age = min(min_age, 15.0)
        escalation_age = min(escalation_age, 60.0)

    distance_min = float(get_param(settings, "unified_governance_loss_distance_to_sl_min_pct", 0.15))
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

    # W3: velocity-based deterioration (unchanged) + 2-cycle adverse signal timer
    _adverse_signal_cycles = int(loss_state.get("adverse_signal_cycles", 0) or 0)
    _required_adverse_cycles = int(get_param(settings, "unified_governance_loss_adverse_signal_cycles", 2))
    # FUTURES_SHORT: 1 cycle is enough (fast market, no recovery window)
    if str(context.trade_type or "").strip().lower() == "futures" or str(context.regime_mode or "").upper() == "FUTURES_SHORT":
        _required_adverse_cycles = 1

    _signal_against = False
    if context.current_signal_direction:
        sig = str(context.current_signal_direction).strip().upper()
        against_short = context.side.upper() == "SHORT" and sig == "BUY"
        against_long = context.side.upper() in ("LONG", "BUY") and sig == "SELL"
        _signal_against = (against_short or against_long) and (context.current_signal_strength or 0.0) >= min_signal_strength

    if _signal_against:
        _adverse_signal_cycles += 1
    else:
        _adverse_signal_cycles = max(0, _adverse_signal_cycles - 1)  # B4: decay instead of hard reset — prevents oscillating signals from never reaching threshold

    deterioration = False
    if context.pnl_velocity_1h is not None:
        deterioration = float(context.pnl_velocity_1h) <= -abs(accel_threshold)
    # Adverse signal requires N consecutive cycles to trigger deterioration (W3 timer)
    if _signal_against and _adverse_signal_cycles >= _required_adverse_cycles:
        deterioration = True

    near_sl = _distance_guard(context.distance_to_sl_pct, distance_min)

    # s78-fix: fast loss cut / peak drawdown / soft supervisor escalation before age/near-sl guards
    _regime_upper = str(context.regime_mode or "").strip().upper()
    _fast_loss_threshold = -0.0075 if _regime_upper == "RANGE_SCALP" else -0.0150
    if context.pnl_pct <= _fast_loss_threshold and (context.pnl_velocity_1h or 0) < 0:
        action = "close"
        reason = "unified_governance_loss_fast_cut"
        should_close = True
        close_pct = 1.0
        priority = 96
    elif context.peak_pnl_pct >= 0.002 and (context.peak_pnl_pct - context.pnl_pct) > 0.0015:
        action = "close"
        reason = "unified_governance_peak_drawdown_cut"
        should_close = True
        close_pct = 1.0
        priority = 96
    elif (context.sso_advisory_should_close or context.prs_advisory_should_close) and context.pnl_pct < 0:
        action = "close"
        reason = "unified_governance_soft_supervisor_escalated"
        should_close = True
        close_pct = 1.0
        priority = 94
        if context.sso_advisory_reason:
            reason += f" ({context.sso_advisory_reason})"
        elif context.prs_advisory_reason:
            reason += f" ({context.prs_advisory_reason})"
    else:
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
        "adverse_signal_cycles": _adverse_signal_cycles,
        "last_eval_at": now.isoformat(),
        "last_eval_reason": reason,
        "profile_applied": str(context.profile_label or "B").upper(),
        "regime_applied": str(context.regime_mode or ""),
        "regime_min_mult": round(_regime_min_mult, 2),
        "regime_esc_mult": round(_regime_esc_mult, 2),
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
            f"held_min={held_min:.0f}; min_age={min_age:.0f}; esc_age={escalation_age:.0f}; "
            f"pnl_pct={context.pnl_pct*100:.2f}; near_sl={near_sl}; det={deterioration}; "
            f"adverse_cycles={_adverse_signal_cycles}/{_required_adverse_cycles}; "
            f"regime_scale=({_regime_min_mult:.2f},{_regime_esc_mult:.2f})"
        ),
        priority=priority,
        override_target="risk_containment" if should_close else "watchdog",
        diagnostics=(
            f"distance_to_sl_pct={context.distance_to_sl_pct}; "
            f"pnl_velocity_1h={context.pnl_velocity_1h}; signal_strength={context.current_signal_strength}; "
            f"adverse_signal_cycles={_adverse_signal_cycles}; required={_required_adverse_cycles}"
        ),
        state_patch=state_patch,
    )
