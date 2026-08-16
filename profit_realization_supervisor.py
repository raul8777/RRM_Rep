from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from config_helpers import get_param
from side_utils import normalize_side


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
    p = str(profile_label or "").strip().upper()
    if p == "A":
        return 1.25   # Wolf Hunter: max patience — let high-conviction runners reach full peak
    if p == "C":
        return 0.85   # Defensive: take profit faster, protect smaller gains
    return 1.0        # B = neutral baseline


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


def _peak_protection_pct(regime_mode: Optional[str], trade_type: Optional[str], settings=None, profile_label: Optional[str] = None) -> float:
    """Minimum fraction of peak_pnl_pct that must be preserved.
    If pnl_pct < peak * this value, trigger full close regardless of held time.
    F11: defends significant gains, prevents giving back peak to near-zero."""
    regime = str(regime_mode or "").strip().upper()
    if trade_type == "futures" or regime == "FUTURES_SHORT":
        return 0.75  # futures: defend 75% of peak (fast market)
    if regime == "PRESERVATION":
        return 0.80  # preservation: defend 80% (capital protection priority)
    if regime == "RANGE_SCALP":
        return 0.70  # range scalp: defend 70% (mini-rebound killer)
    if regime == "SPOT_BULL":
        # s95: per-operation profile instead of global active profile.
        _profile = str(profile_label or '').strip().upper()
        if _profile == 'A':
            return float(getattr(settings, 'peak_protection_spot_bull_profile_a_pct', 0.70))
        return float(getattr(settings, 'peak_protection_spot_bull_pct', 0.65))
    return 0.65      # neutral default


def _compute_momentum_score(
    pnl_velocity_1h: Optional[float],
    signal_strength: Optional[float],
    fng_value: Optional[float],
    min_velocity: float,
    hold_signal_strength: float,
) -> float:
    """F9: Unified momentum score 0-100 replacing binary signal_faded.
    >= 70: hold — momentum is active
    40-70: normal giveback evaluation
    <  40: accelerate exit — momentum exhausted"""
    score = 50.0  # neutral baseline

    # Component 1: PnL velocity (40% weight)
    # velocity > min_velocity → bullish momentum
    if pnl_velocity_1h is not None:
        vel_norm = max(-1.0, min(1.0, pnl_velocity_1h / max(min_velocity, 0.0001)))
        score += 40.0 * (vel_norm * 0.5)  # [-20, +20] contribution

    # Component 2: Signal strength (35% weight)
    # strength >= hold_signal_strength → signal confirms position
    if signal_strength is not None:
        sig_norm = max(0.0, min(1.0, signal_strength / max(hold_signal_strength, 0.001)))
        # Above hold threshold → positive; below → negative
        score += 35.0 * (sig_norm - 0.5) * 2.0  # [-35, +35] contribution

    # Component 3: Fear & Greed (25% weight)
    # F&G > 50 → greed helps momentum; < 50 → fear hurts it
    if fng_value is not None:
        fng_norm = (float(fng_value) - 50.0) / 50.0  # [-1, +1]
        score += 25.0 * fng_norm * 0.5  # [-12.5, +12.5] contribution

    return max(0.0, min(100.0, score))


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


def _sr_resistance_acceleration(
    context: ProfitRealizationContext,
    settings: Any,
) -> Dict[str, Any]:
    metadata = context.metadata if isinstance(context.metadata, dict) else {}
    enabled = bool(get_param(settings, "profit_realization_sr_accel_enabled", True))
    regime = str(context.regime_mode or "").strip().upper()
    side = normalize_side(context.side)
    if not enabled or regime != "RANGE_SCALP" or side not in ("LONG", "SHORT"):
        return {"active": False, "score": 0, "zone": 0.0}

    zone_min = float(get_param(settings, "profit_realization_sr_resistance_zone_min", 0.68))
    support_zone_max = float(get_param(settings, "profit_realization_sr_support_zone_max", 1.0 - zone_min))
    zone_raw = metadata.get("sr_zone_position")
    if zone_raw is None:
        return {"active": False, "score": 0, "zone": 0.0}
    zone = float(zone_raw)
    if side == "LONG":
        near_edge = zone >= zone_min
        sr_reaction = bool(
            metadata.get("resistance_rejection")
            or metadata.get("resistance_rejection_sell")
            or metadata.get("sr_resistance_rejection")
        )
        predexit_sr = bool(
            metadata.get("profit_predexit_sr_resistance")
            or metadata.get("sr_exit_triggered")
            or metadata.get("peak_capture_sr_signal")
        )
        edge_name = "resistance"
    else:
        near_edge = zone <= support_zone_max
        sr_reaction = bool(
            metadata.get("support_hold")
            or metadata.get("support_hold_buy")
            or metadata.get("sr_support_hold")
        )
        predexit_sr = bool(
            metadata.get("profit_predexit_sr_support")
            or metadata.get("sr_exit_triggered")
            or metadata.get("peak_capture_sr_signal")
        )
        edge_name = "support"

    score = int(near_edge) + int(sr_reaction) + int(predexit_sr)
    return {
        "active": score > 0,
        "score": score,
        "zone": zone,
        "side": side,
        "edge": edge_name,
        "zone_min": zone_min,
        "support_zone_max": support_zone_max,
        "near_edge": near_edge,
        "sr_reaction": sr_reaction,
        "predexit_sr": predexit_sr,
    }


def evaluate(context: ProfitRealizationContext, settings, now: Optional[datetime] = None) -> ProfitRealizationDecision:
    now = now or datetime.now(timezone.utc)
    trade_type = str(context.trade_type or "").strip().lower() or "spot"
    defaults = _trade_defaults(trade_type)

    enabled = bool(get_param(settings, "profit_realization_enabled", True))
    if trade_type == "futures":
        enabled = enabled and bool(get_param(settings, "profit_realization_futures_enabled", True))
        min_profit_pct = float(get_param(settings, "profit_realization_futures_min_profit_pct", defaults["min_profit_pct"]))
        partial_min_profit_mult = float(get_param(settings, "profit_realization_futures_partial_min_profit_multiplier", 2.5))
        partial_minutes = float(get_param(settings, "profit_realization_futures_partial_minutes", defaults["partial_minutes"]))
        full_minutes = float(get_param(settings, "profit_realization_futures_full_minutes", defaults["full_minutes"]))
        drawback_pct = float(get_param(settings, "profit_realization_futures_drawback_pct", defaults["drawback_pct"]))
        partial_close_pct = float(get_param(settings, "profit_realization_futures_partial_close_pct", defaults["partial_close_pct"]))
        max_extensions = int(get_param(settings, "profit_realization_futures_max_extensions", defaults["max_extensions"]))
    else:
        enabled = enabled and bool(get_param(settings, "profit_realization_spot_enabled", True))
        min_profit_pct = float(get_param(settings, "profit_realization_spot_min_profit_pct", defaults["min_profit_pct"]))
        partial_min_profit_mult = float(get_param(settings, "profit_realization_spot_partial_min_profit_multiplier", 2.5))
        partial_minutes = float(get_param(settings, "profit_realization_spot_partial_minutes", defaults["partial_minutes"]))
        full_minutes = float(get_param(settings, "profit_realization_spot_full_minutes", defaults["full_minutes"]))
        # C2: regime-specific base override — applied before scale so profile×fng still work on top
        _regime_upper = str(context.regime_mode or "").strip().upper()
        if _regime_upper == "RANGE_SCALP":
            partial_minutes = float(get_param(settings, "profit_realization_spot_partial_minutes_range_scalp", 35.0))
            full_minutes = float(get_param(settings, "profit_realization_spot_full_minutes_range_scalp", 70.0))
        drawback_pct = float(get_param(settings, "profit_realization_spot_drawback_pct", defaults["drawback_pct"]))
        partial_close_pct = float(get_param(settings, "profit_realization_spot_partial_close_pct", defaults["partial_close_pct"]))
        max_extensions = int(get_param(settings, "profit_realization_spot_max_extensions", defaults["max_extensions"]))

    if not enabled:
        return ProfitRealizationDecision()

    # s78-fix: evaluate negative PnL too, but skip tiny positives below min_profit
    _loss_branch = False
    if context.pnl_pct <= 0:
        _loss_branch = True
    elif context.pnl_pct < min_profit_pct:
        return ProfitRealizationDecision()

    partial_min_profit_mult = max(1.0, partial_min_profit_mult)
    partial_min_pnl_pct = max(min_profit_pct, min_profit_pct * partial_min_profit_mult)

    if not context.regime_mode:
        print(f"[PRS] {context.symbol}: regime_mode=None — using neutral scale (trade_type={trade_type}, profile={context.profile_label})")

    profile_scale = _profile_scale(context.profile_label)
    regime_scale = _regime_scale(context.regime_mode, trade_type)
    scale = max(0.50, min(1.35, profile_scale * regime_scale))

    # F10: Apply Fear & Greed exit modifier injected by UG (if present)
    _fng_meta_modifier = float(context.metadata.get("ug_fng_exit_modifier", 1.0) or 1.0)
    _fng_meta_modifier = max(0.70, min(1.30, _fng_meta_modifier))  # guard: never more than 30% change
    scale_with_fng = scale * _fng_meta_modifier

    partial_minutes = max(15.0, partial_minutes * scale_with_fng)
    full_minutes = max(partial_minutes + 15.0, full_minutes * scale_with_fng)
    drawback_pct = max(0.001, drawback_pct * (1.0 / max(0.75, scale_with_fng)))
    sr_accel = _sr_resistance_acceleration(context, settings)
    if bool(sr_accel.get("active")):
        partial_mult = float(get_param(settings, "profit_realization_sr_accel_partial_multiplier", 0.80))
        full_mult = float(get_param(settings, "profit_realization_sr_accel_full_multiplier", 0.85))
        drawback_mult = float(get_param(settings, "profit_realization_sr_accel_drawback_multiplier", 0.85))
        partial_mult = max(0.50, min(1.00, partial_mult))
        full_mult = max(0.50, min(1.00, full_mult))
        drawback_mult = max(0.50, min(1.00, drawback_mult))
        partial_minutes = max(10.0, partial_minutes * partial_mult)
        full_minutes = max(partial_minutes + 10.0, full_minutes * full_mult)
        drawback_pct = max(0.0008, drawback_pct * drawback_mult)
    min_velocity = float(defaults["min_velocity"])
    hold_signal_strength = float(defaults["hold_signal_strength"])

    # F9: Unified momentum score replaces binary signal_faded
    # W8: Use ug_fng_value (real F&G) when available; fallback to modifier-based approximation
    _fng_real = context.metadata.get("ug_fng_value")
    if _fng_real is not None:
        _fng_for_score = max(0.0, min(100.0, float(_fng_real)))
    else:
        _fng_mod = float(context.metadata.get("ug_fng_exit_modifier", 1.0) or 1.0)
        _fng_for_score = max(0.0, min(100.0, 50.0 + (_fng_mod - 1.0) * 250.0))
    momentum_score = _compute_momentum_score(
        pnl_velocity_1h=context.pnl_velocity_1h,
        signal_strength=context.current_signal_strength,
        fng_value=_fng_for_score,
        min_velocity=min_velocity,
        hold_signal_strength=hold_signal_strength,
    )
    momentum_low = momentum_score < 40.0   # exhausted — accelerate exit
    momentum_high = momentum_score >= 70.0  # strong — extend patience

    # When momentum is high, extend partial_minutes by up to 25%
    if momentum_high:
        partial_minutes = min(full_minutes - 10.0, partial_minutes * 1.25)
    # When momentum is low, reduce partial_minutes by 25% (act faster)
    elif momentum_low:
        partial_minutes = max(10.0, partial_minutes * 0.75)

    # C1: SPOT_BULL + high momentum floor — never exit profitable positions before 45min
    # Prevents PRS from closing at 23min in SPOT_BULL where the trend has more room to run
    if (trade_type == "spot"
            and str(context.regime_mode or "").upper() == "SPOT_BULL"
            and momentum_high):
        _spot_bull_floor = float(get_param(settings, "profit_realization_spot_bull_min_partial_minutes", 45.0))
        partial_minutes = max(partial_minutes, _spot_bull_floor)

    held_min = _held_minutes(now, context.profit_started_at, context.opened_at)
    peak = max(float(context.peak_pnl_pct or 0.0), float(context.pnl_pct or 0.0))
    giveback_pct = max(0.0, peak - float(context.pnl_pct or 0.0))
    velocity = context.pnl_velocity_1h
    positive_velocity = velocity is None or velocity >= min_velocity
    signal_strength = context.current_signal_strength
    signal_known = signal_strength is not None
    strong_signal = signal_known and signal_strength >= hold_signal_strength
    signal_faded = signal_known and (not strong_signal) and not momentum_high
    tp_extensions = max(0, int(context.tp_extensions or 0))
    extension_cap_hit = tp_extensions >= max_extensions

    # F11: Peak protection — close immediately if significant gains are being given back
    # W7: Minimum threshold is ATR-aware to avoid triggering on normal volatility noise
    # s120: momentum-aware — when momentum is high, relax threshold to let winners run
    _peak_protect = _peak_protection_pct(context.regime_mode, trade_type, settings, profile_label=context.profile_label)
    if momentum_high:
        # High momentum: reduce protection aggressiveness — allow up to 50% giveback
        # before cutting (instead of 35%). This prevents cutting runners mid-trend.
        _peak_protect = min(_peak_protect, 0.50)
    _atr_4h_pct = float(context.metadata.get("atr_4h_pct", 0.0) or 0.0)
    _atr_floor = _atr_4h_pct * 1.5 if _atr_4h_pct > 0 else 0.0
    _peak_protect_min = max(min_profit_pct * 3.0, _atr_floor)
    # FUTURES: needs higher threshold due to leverage amplifying noise
    if trade_type == "futures":
        _peak_protect_min = max(_peak_protect_min, 0.015)
    # s120: higher floor when momentum is active — don't trigger on small peaks
    if momentum_high:
        _peak_protect_min = max(_peak_protect_min, 0.02)
    _peak_protection_fired = (
        peak >= _peak_protect_min
        and float(context.pnl_pct or 0.0) < peak * _peak_protect
        and float(context.pnl_pct or 0.0) > 0  # still in profit, but degrading fast
    )
    if _peak_protection_fired:
        _pp_detail = (
            f"{trade_type.upper()} peak={peak*100:.2f}% dropped to pnl={context.pnl_pct*100:.2f}% "
            f"(< {_peak_protect*100:.0f}% of peak={peak*_peak_protect*100:.2f}%) "
            f"regime={context.regime_mode or 'NEUTRAL'} score={momentum_score:.0f}"
        )
        _pp_diagnostics = (
            f"peak_protection: peak={peak*100:.2f}% protect={_peak_protect:.2f} "
            f"threshold={peak*_peak_protect*100:.2f}% current={context.pnl_pct*100:.2f}%; "
            f"momentum_score={momentum_score:.1f}; fng_mod={_fng_meta_modifier:.2f}"
        )
        _pp_state_patch = {
            LAST_DECISION_KEY: {
                "action": "close",
                "reason": "prs_peak_protection",
                "close_source": "lifecycle",
                "close_state": "closed",
                "close_intent": "prs_peak_protection",
                "close_pct": 1.0,
                "detail": _pp_detail,
                "diagnostics": _pp_diagnostics,
                "peak_pnl_pct": round(float(peak), 6),
                "pnl_pct": round(float(context.pnl_pct or 0.0), 6),
                "peak_protect_pct": _peak_protect,
                "momentum_score": round(momentum_score, 1),
                "trade_type": trade_type,
                "symbol": context.symbol,
                "updated_at": now.isoformat(),
            },
            LAST_DECISION_AT_KEY: now.isoformat(),
        }
        return ProfitRealizationDecision(
            should_close=True,
            close_pct=1.0,
            action="close",
            reason="prs_peak_protection",
            detail=_pp_detail,
            override_target="peak_protection",
            priority=95,
            diagnostics=_pp_diagnostics,
            state_patch=_pp_state_patch,
        )

    # s78-fix: loss-side PRS exit — cut when momentum/signal has reversed and we are losing
    if _loss_branch:
        if (context.pnl_pct <= -0.005 and (momentum_low or (not positive_velocity and signal_faded))
                and held_min >= 10):
            _loss_detail = (
                f"{trade_type.upper()} loss pnl={context.pnl_pct*100:.2f}% "
                f"momentum_score={momentum_score:.0f} velocity={None if velocity is None else round(float(velocity)*100, 4)}"
            )
            _loss_diag = (
                f"loss_branch: pnl_pct={context.pnl_pct*100:.2f}%; "
                f"momentum_score={momentum_score:.1f}; momentum_low={momentum_low}; "
                f"positive_velocity={positive_velocity}; signal_faded={signal_faded}; held_min={held_min}"
            )
            _loss_patch = {
                LAST_DECISION_KEY: {
                    "action": "close",
                    "reason": "prs_loss_momentum_exit",
                    "close_source": "lifecycle",
                    "close_state": "closed",
                    "close_intent": "prs_loss_momentum_exit",
                    "close_pct": 1.0,
                    "detail": _loss_detail,
                    "diagnostics": _loss_diag,
                    "pnl_pct": round(float(context.pnl_pct or 0.0), 6),
                    "peak_pnl_pct": round(float(peak or 0.0), 6),
                    "momentum_score": round(momentum_score, 1),
                    "trade_type": trade_type,
                    "symbol": context.symbol,
                    "updated_at": now.isoformat(),
                },
                LAST_DECISION_AT_KEY: now.isoformat(),
            }
            return ProfitRealizationDecision(
                should_close=True,
                close_pct=1.0,
                action="close",
                reason="prs_loss_momentum_exit",
                detail=_loss_detail,
                override_target="risk_containment",
                priority=60,
                diagnostics=_loss_diag,
                state_patch=_loss_patch,
            )
        return ProfitRealizationDecision()

    if held_min < partial_minutes and giveback_pct < (drawback_pct * 0.5) and positive_velocity and signal_faded:
        return ProfitRealizationDecision()

    should_partial = False
    should_full = False
    reason = ""
    detail = ""
    override_target = "smarttp_extend"
    priority = 50

    sr_partial_trigger = bool(
        sr_accel.get("active")
        and held_min >= max(10.0, partial_minutes * 0.80)
        and giveback_pct >= (drawback_pct * 0.30)
    )
    sr_full_trigger = bool(
        sr_accel.get("active")
        and held_min >= max(partial_minutes + 10.0, full_minutes * 0.85)
        and giveback_pct >= (drawback_pct * 0.65)
    )

    full_close_evidence = [
        (giveback_pct >= drawback_pct, "giveback"),
        (not positive_velocity, "velocity"),
        (extension_cap_hit, "extension_cap"),
        (signal_faded, "signal"),
        (momentum_low, "momentum"),
        (sr_full_trigger, "sr_accel"),
    ]
    full_close_evidence_active = [name for triggered, name in full_close_evidence if triggered]

    if held_min >= full_minutes and full_close_evidence_active:
        should_full = True
        reason = "profit_realization_supervisor_full_sr_accel" if sr_full_trigger else "profit_realization_supervisor_full"
        detail = (
            f"{trade_type.upper()} held {held_min}m in profit; pnl={context.pnl_pct*100:.2f}% peak={peak*100:.2f}% "
            f"giveback={giveback_pct*100:.2f}% >= {drawback_pct*100:.2f}% | evidence={','.join(full_close_evidence_active)} | score={momentum_score:.0f}"
        )
        priority = 90
    elif (held_min >= partial_minutes and ((giveback_pct >= (drawback_pct * 0.5)) or (not positive_velocity) or extension_cap_hit or signal_faded or momentum_low)) or sr_partial_trigger:
        should_partial = True
        reason = "profit_realization_supervisor_partial_sr_accel" if sr_partial_trigger else "profit_realization_supervisor_partial"
        detail = (
            f"{trade_type.upper()} held {held_min}m in profit; pnl={context.pnl_pct*100:.2f}% peak={peak*100:.2f}% "
            f"giveback={giveback_pct*100:.2f}% velocity={None if velocity is None else round(float(velocity)*100, 4)} | score={momentum_score:.0f}"
        )
        priority = 70

    if not should_partial and not should_full:
        return ProfitRealizationDecision(
            should_close=False,
            action="hold",
            reason="profit_realization_time_without_degradation_evidence",
            detail=(
                f"{trade_type.upper()} held {held_min}m but no degradation evidence; "
                f"positive_velocity={positive_velocity}, signal_faded={signal_faded}, momentum_low={momentum_low}, "
                f"giveback={giveback_pct*100:.2f}% / threshold={drawback_pct*100:.2f}%"
            ),
            diagnostics=(
                f"held_min={held_min}; full_minutes={full_minutes}; partial_minutes={partial_minutes}; "
                f"giveback_pct={giveback_pct*100:.2f}%; drawback_pct={drawback_pct*100:.2f}%; "
                f"positive_velocity={positive_velocity}; signal_faded={signal_faded}; momentum_low={momentum_low}; "
                f"sr_accel_active={bool(sr_accel.get('active'))}; momentum_score={momentum_score:.1f}"
            ),
        )

    # Low-edge partial deferral: avoid trimming winners too early when profit is
    # still near the minimum threshold and there is no strong protective signal.
    if should_partial and not should_full:
        protective_signal = bool(
            momentum_low
            or sr_partial_trigger
            or extension_cap_hit
            or giveback_pct >= drawback_pct
        )
        if float(context.pnl_pct or 0.0) < partial_min_pnl_pct and not protective_signal:
            _defer_detail = (
                f"{trade_type.upper()} partial deferred: pnl={context.pnl_pct*100:.2f}% "
                f"< min_partial={partial_min_pnl_pct*100:.2f}% without protective signal"
            )
            _defer_diag = (
                f"partial_defer_low_pnl: pnl_pct={context.pnl_pct*100:.2f}%; "
                f"min_partial_pct={partial_min_pnl_pct*100:.2f}%; "
                f"momentum_low={momentum_low}; sr_partial_trigger={sr_partial_trigger}; "
                f"extension_cap_hit={extension_cap_hit}; giveback_pct={giveback_pct*100:.2f}%; "
                f"drawback_pct={drawback_pct*100:.2f}%"
            )
            return ProfitRealizationDecision(
                should_close=False,
                action="hold",
                reason="profit_realization_partial_deferred_low_pnl",
                detail=_defer_detail,
                diagnostics=_defer_diag,
            )

    close_pct = 1.0 if should_full else max(0.10, min(1.0, partial_close_pct))
    diagnostics = (
        f"held_min={held_min}; partial_min={partial_minutes:.1f}; full_min={full_minutes:.1f}; "
        f"drawback={drawback_pct*100:.2f}%; giveback={giveback_pct*100:.2f}%; "
        f"velocity={None if velocity is None else round(float(velocity), 6)}; "
        f"momentum_score={momentum_score:.1f}; momentum_high={momentum_high}; momentum_low={momentum_low}; "
        f"strong_signal={strong_signal}; extensions={tp_extensions}; scale={scale:.2f}; fng_mod={_fng_meta_modifier:.2f}; "
        f"sr_accel_active={bool(sr_accel.get('active'))}; sr_score={int(sr_accel.get('score', 0))}; "
        f"sr_zone={float(sr_accel.get('zone', 0.0)):.3f}; sr_zone_min={float(sr_accel.get('zone_min', 0.0)):.3f}"
    )
    state_patch = {
        LAST_DECISION_KEY: {
            "action": "close",
            "reason": reason,
            "close_source": "lifecycle",
            "close_state": "partial_closed" if close_pct < 1.0 else "closed",
            "close_intent": reason,
            "close_pct": round(close_pct, 4),
            "detail": detail,
            "diagnostics": diagnostics,
            "held_minutes": held_min,
            "giveback_pct": round(giveback_pct, 6),
            "pnl_pct": round(float(context.pnl_pct or 0.0), 6),
            "peak_pnl_pct": round(float(peak or 0.0), 6),
            "momentum_score": round(momentum_score, 1),
            "fng_modifier": round(_fng_meta_modifier, 4),
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