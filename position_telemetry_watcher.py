"""High-frequency telemetry watcher for open positions.

Captures position behavior snapshots every second (configurable) without taking
any trading action.  Designed to be lightweight, fail-safe, and isolated from
the hard-risk monitor and the trade cycle.

Modes (governed by ``position_reaction_policy_mode``):
  - ``telemetry_only``: only persists snapshots / audit events.
  - ``hard_rules``: may take defensive actions based on simple explicit rules.
  - ``ml_assisted``: reserved for future model-driven reactions.

Current default: ``telemetry_only``.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from config_helpers import get_param

_lock = threading.Lock()
_last_db_flush: Dict[str, float] = {}
_last_audit_flush: Dict[str, float] = {}

# Shadow metrics for sr_structure_invalidation_exit
try:
    from collections import deque
except Exception:
    deque = list
_shadow_decisions: deque = deque(maxlen=1000)
_shadow_metrics: Dict[str, Any] = {
    "evaluations": 0,
    "would_exit": 0,
    "by_symbol": {},
    "profit_guard_actions": 0,
    "profit_guard_hard": 0,
    "profit_guard_soft": 0,
    "drawdown_actions": 0,
    "drawdown_cuts": 0,
    "drawdown_tightens": 0,
    "drawdown_recoverable_windows": 0,
    "drawdown_failed_recovery_windows": 0,
    "drawdown_shock_events": 0,
    "drawdown_deferred_cuts": 0,
    "profit_positions_evaluated": 0,
    "loss_positions_evaluated": 0,
    "profit_guard_candidates": 0,
    "downstream_surfaces": [
        "smart_scale_out",
        "profit_realization_supervisor",
        "unified_governance_supervisor",
    ],
    "last_summary": {},
    "last_at": None,
}


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _compute_unrealized(entry: float, qty: float, price: float, side: str, leverage: float = 1.0) -> float:
    if entry <= 0 or qty <= 0 or price <= 0:
        return 0.0
    if side.upper() == "LONG":
        return (price - entry) * qty
    return (entry - price) * qty


def _compute_unrealized_pct(entry: float, price: float, side: str) -> float:
    if entry <= 0 or price <= 0:
        return 0.0
    if side.upper() == "LONG":
        return (price - entry) / entry
    return (entry - price) / entry


def _extract_sl_tp(metadata: Optional[Dict[str, Any]], default_sl: float = 0.0, default_tp: float = 0.0) -> Dict[str, float]:
    if not metadata or not isinstance(metadata, dict):
        return {"sl": float(default_sl), "tp": float(default_tp)}
    smart = metadata.get("_smart_meta") or {}
    sl = float(smart.get("sl_price") or metadata.get("stop_loss") or default_sl)
    tp = float(smart.get("tp_price") or metadata.get("take_profit") or default_tp)
    return {"sl": sl, "tp": tp}


def _fetch_recent_signals(db_client: Any, symbols: List[str], lookback_minutes: float) -> Dict[str, List[Dict[str, Any]]]:
    """Fetch recent autonomous signals for the given symbols. Fail-open."""
    out: Dict[str, List[Dict[str, Any]]] = {s: [] for s in symbols}
    if not db_client or not symbols:
        return out
    try:
        since = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
        placeholders = ",".join(["%s"] * len(symbols))
        rows = db_client.execute_query(
            f"""SELECT symbol, timestamp, data FROM market_data
               WHERE data_type='signal' AND symbol IN ({placeholders})
                 AND timestamp >= %s
               ORDER BY symbol, timestamp""",
            tuple(symbols) + (since,),
        ) or []
        for row in rows:
            symbol = row.get("symbol")
            ts = row.get("timestamp")
            data = row.get("data")
            d = data if isinstance(data, dict) else json.loads(data)
            d["_ts"] = ts
            out.setdefault(symbol, []).append(d)
    except Exception:
        pass
    return out


def _evaluate_sr_invalidation_for_snapshot(snapshot: Dict[str, Any], signals: List[Dict[str, Any]], settings: Any) -> Optional[Dict[str, Any]]:
    """Shadow evaluation: would the SR structure-invalidation early exit fire?"""
    enabled = bool(get_param(settings, "sr_invalidation_exit_enabled", True))
    if not enabled:
        return None
    side = snapshot.get("side", "").upper()
    opposite_action = "SELL" if side == "LONG" else "BUY" if side == "SHORT" else None
    if opposite_action is None:
        return None
    min_strength = float(get_param(settings, "sr_invalidation_min_strength", 0.50) or 0.50)
    confirmation_bars = int(get_param(settings, "sr_invalidation_confirmation_bars", 3) or 3)
    only_with_loss = bool(get_param(settings, "sr_invalidation_only_with_loss", True))
    loss_pct = float(get_param(settings, "sr_invalidation_loss_pct", 0.5) or 0.5)
    unrealized_pct = float(snapshot.get("unrealized_pnl_pct", 0.0) or 0.0)
    if only_with_loss and unrealized_pct > -abs(loss_pct):
        return None
    # Filter adverse signals with enough strength
    adverse = [
        s for s in signals
        if s.get("action") == opposite_action and float(s.get("strength") or 0.0) >= min_strength
    ]
    if len(adverse) < confirmation_bars:
        return None
    avg_strength = sum(float(s.get("strength") or 0.0) for s in adverse) / len(adverse)
    first_ts = adverse[0].get("_ts")
    return {
        "would_exit": True,
        "reason": "sr_structure_invalidation_shadow",
        "side": side,
        "opposite_action": opposite_action,
        "signals_count": len(adverse),
        "avg_strength": round(avg_strength, 4),
        "unrealized_pnl_pct": unrealized_pct,
        "first_signal_ts": first_ts.isoformat() if first_ts else None,
    }


def _audit_shadow_decision(db_client: Any, snapshot: Dict[str, Any], decision: Dict[str, Any], throttle_seconds: float) -> None:
    if not db_client:
        return
    now = _now_ts()
    key = f"sr_invalidation_shadow_{snapshot.get('symbol')}"
    with _lock:
        if now - _last_audit_flush.get(key, 0.0) < throttle_seconds:
            return
        _last_audit_flush[key] = now
    try:
        db_client.execute_update(
            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s, %s, %s, %s, %s)",
            (
                snapshot.get("symbol", ""),
                snapshot.get("trade_type", ""),
                "sr_invalidation_shadow_decision",
                json.dumps({
                    "position_id": snapshot.get("position_id"),
                    "side": snapshot.get("side"),
                    "unrealized_pnl_pct": snapshot.get("unrealized_pnl_pct"),
                    **decision,
                }),
                False,
            ),
        )
    except Exception as e:
        print(f"[PositionTelemetry] shadow audit write failed: {e}")


def _distance_to_sr(price: float, sr_level: Optional[float], side: str) -> Optional[float]:
    if not sr_level or sr_level <= 0:
        return None
    if side.upper() == "LONG":
        return (sr_level - price) / price
    return (price - sr_level) / price


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _to_percent(value: Any, *, assume_fraction_threshold: float = 2.0) -> float:
    """Normalize percentage-like values to percent units.

    Many metadata fields are stored as fractions (0.01 = 1%), while telemetry
    snapshots use percent units (1.0 = 1%). This helper converts mixed values
    to percent units.
    """
    raw = _to_float(value, 0.0)
    if abs(raw) <= assume_fraction_threshold:
        return raw * 100.0
    return raw


def _compute_profit_guard_target(snapshot: Dict[str, Any], settings: Any) -> Optional[Dict[str, Any]]:
    if not bool(get_param(settings, "position_profit_guard_enabled", True)):
        return None

    current_pct = _to_float(snapshot.get("unrealized_pnl_pct"), 0.0)
    peak_pct = _to_percent(snapshot.get("peak_pnl_pct"), assume_fraction_threshold=1.5)
    peak_pct = max(peak_pct, current_pct)

    activation_pct = max(0.0, _to_float(get_param(settings, "position_profit_guard_activation_pct", 0.35), 0.35))
    min_peak_pct = max(activation_pct, _to_float(get_param(settings, "position_profit_guard_min_peak_pct", 0.50), 0.50))
    if current_pct < activation_pct or peak_pct < min_peak_pct:
        return None

    giveback_pct = max(0.0, peak_pct - current_pct)
    min_giveback_pct = max(0.0, _to_float(get_param(settings, "position_profit_guard_min_giveback_pct", 0.25), 0.25))
    giveback_factor = max(0.0, _to_float(get_param(settings, "position_profit_guard_giveback_factor", 0.55), 0.55))
    allowed_giveback_pct = max(min_giveback_pct, peak_pct * giveback_factor)

    hard_lock_factor = max(0.0, _to_float(get_param(settings, "position_profit_guard_hard_lock_factor", 0.45), 0.45))
    soft_lock_factor = max(0.0, _to_float(get_param(settings, "position_profit_guard_soft_lock_factor", 0.25), 0.25))
    floor_lock_pct = max(0.0, _to_float(get_param(settings, "position_profit_guard_floor_lock_pct", 0.05), 0.05))
    lock_mode = "hard" if giveback_pct >= allowed_giveback_pct else "soft"
    lock_factor = hard_lock_factor if lock_mode == "hard" else soft_lock_factor
    lock_pct = max(floor_lock_pct, peak_pct * lock_factor)

    entry_price = _to_float(snapshot.get("entry_price"), 0.0)
    current_price = _to_float(snapshot.get("current_price"), 0.0)
    if entry_price <= 0 or current_price <= 0:
        return None

    side = str(snapshot.get("side") or "").upper()
    sl_buffer_pct = max(0.0, _to_float(get_param(settings, "position_profit_guard_sl_buffer_pct", 0.20), 0.20))
    min_step_pct = max(0.0, _to_float(get_param(settings, "position_profit_guard_min_step_pct", 0.03), 0.03))
    current_sl = _to_float(snapshot.get("stop_loss"), 0.0)

    if side == "LONG":
        target_sl = entry_price * (1.0 + (lock_pct / 100.0))
        max_safe_sl = current_price * (1.0 - (sl_buffer_pct / 100.0))
        target_sl = min(target_sl, max_safe_sl)
        if target_sl <= 0 or target_sl >= current_price:
            return None
        if current_sl > 0 and target_sl <= current_sl * (1.0 + min_step_pct / 100.0):
            return None
    elif side == "SHORT":
        target_sl = entry_price * (1.0 - (lock_pct / 100.0))
        min_safe_sl = current_price * (1.0 + (sl_buffer_pct / 100.0))
        target_sl = max(target_sl, min_safe_sl)
        if target_sl <= current_price:
            return None
        if current_sl > 0 and target_sl >= current_sl * (1.0 - min_step_pct / 100.0):
            return None
    else:
        return None

    return {
        "target_sl": round(target_sl, 8),
        "side": side,
        "current_pct": round(current_pct, 6),
        "peak_pct": round(peak_pct, 6),
        "giveback_pct": round(giveback_pct, 6),
        "allowed_giveback_pct": round(allowed_giveback_pct, 6),
        "lock_mode": lock_mode,
        "lock_factor": round(lock_factor, 6),
        "lock_pct": round(lock_pct, 6),
    }


def _persist_spot_profit_guard(
    position_manager: Any,
    snapshot: Dict[str, Any],
    guard: Dict[str, Any],
) -> bool:
    if position_manager is None:
        return False
    symbol = str(snapshot.get("symbol") or "")
    position_id = str(snapshot.get("position_id") or "")
    if not symbol or not position_id:
        return False
    try:
        position_manager.update_position_sl(position_id, float(guard["target_sl"]))
        for pos in (position_manager.get_all_positions() or {}).get(symbol, []):
            if str(getattr(pos, "position_id", "")) != position_id:
                continue
            metadata = dict(getattr(pos, "metadata", {}) or {})
            metadata["profit_guard_last"] = {
                **guard,
                "ts": datetime.now(timezone.utc).isoformat(),
                "source": "position_telemetry_watcher",
                "trade_type": "spot",
            }
            position_manager.update_position_metadata(position_id, metadata)
            try:
                position_manager._audit(symbol, "profit_guard_sl_update", {
                    "trade_type": "spot",
                    "position_id": position_id,
                    **guard,
                })
            except Exception:
                pass
            return True
    except Exception as exc:
        print(f"[PositionTelemetry] SPOT profit guard failed for {symbol}: {exc}")
    return False


def _find_open_futures_position(futures_client: Any, snapshot: Dict[str, Any]) -> Optional[Any]:
    if futures_client is None:
        return None
    symbol = str(snapshot.get("symbol") or "")
    side = str(snapshot.get("side") or "").upper()
    for _key, pos in list(getattr(futures_client, "_open_positions", {}).items()):
        if getattr(pos, "closed", False):
            continue
        if str(getattr(pos, "symbol", "")) == symbol and str(getattr(pos, "side", "")).upper() == side:
            return pos
    return None


def _persist_futures_profit_guard(
    futures_client: Any,
    snapshot: Dict[str, Any],
    guard: Dict[str, Any],
) -> bool:
    if futures_client is None:
        return False
    symbol = str(snapshot.get("symbol") or "")
    side = str(snapshot.get("side") or "").upper()
    current_price = _to_float(snapshot.get("current_price"), 0.0)
    if not symbol or side not in {"LONG", "SHORT"} or current_price <= 0:
        return False
    try:
        ok = futures_client.update_sl_tp(
            symbol,
            side,
            new_sl=float(guard["target_sl"]),
            current_price=current_price,
        )
        if not ok:
            return False
        pos = _find_open_futures_position(futures_client, snapshot)
        if pos is not None:
            smart = getattr(pos, "_smart_meta", {}) or {}
            smart["profit_guard_last"] = {
                **guard,
                "ts": datetime.now(timezone.utc).isoformat(),
                "source": "position_telemetry_watcher",
                "trade_type": "futures",
            }
            pos._smart_meta = smart
            try:
                futures_client._persist_smart_meta(pos)
            except Exception:
                pass
        try:
            futures_client._audit(symbol, "profit_guard_sl_update", {
                "trade_type": "futures",
                **guard,
            })
        except Exception:
            pass
        return True
    except Exception as exc:
        print(f"[PositionTelemetry] FUTURES profit guard failed for {symbol}: {exc}")
    return False


###############################################################################
# ── Intelligent Drawdown Monitor (s120) ──────────────────────────────────────
#
# Runs every 1s cycle. When a position crosses a drawdown threshold ($10 default),
# it activates deep trend analysis using a circular buffer of price snapshots
# to determine if the loss is a temporary dip (recoverable) or a definitive
# trend reversal (cut). This replaces blind hard SLs with intelligence.
#
# Zero extra API cost: re-uses the price already fetched by telemetry snapshot.
# Decision factors: velocity, acceleration, support proximity, BTC momentum.
###############################################################################

_drawdown_buffers: Dict[str, List[Dict[str, float]]] = {}  # position_id -> price history
_drawdown_decisions: Dict[str, Dict[str, Any]] = {}  # position_id -> last decision
_DRAWDOWN_BUFFER_SIZE = 60  # 60 seconds of data at 1s interval
_drawdown_cycle_shadow: Dict[str, int] = {
    "loss_positions": 0,
    "recoverable_windows": 0,
    "failed_recovery_windows": 0,
    "shock_events": 0,
    "deferred_cuts": 0,
}


def _update_drawdown_buffer(position_id: str, price: float, unrealized_usdt: float) -> List[Dict[str, float]]:
    """Append price tick to circular buffer. Returns current buffer."""
    now = _now_ts()
    buf = _drawdown_buffers.setdefault(position_id, [])
    buf.append({"ts": now, "price": price, "pnl_usdt": unrealized_usdt})
    # Keep only last N entries
    if len(buf) > _DRAWDOWN_BUFFER_SIZE:
        _drawdown_buffers[position_id] = buf[-_DRAWDOWN_BUFFER_SIZE:]
    return _drawdown_buffers[position_id]


def _analyze_drawdown_trend(buf: List[Dict[str, float]], side: str) -> Dict[str, Any]:
    """Analyze price buffer to determine if trend is recoverable or definitive.

    Returns dict with:
      - velocity_per_sec: rate of price change (negative = against position)
      - acceleration: second derivative (negative = getting worse)
      - is_decelerating: True if the fall is slowing down
      - recovery_signal: True if price started bouncing back in last 10s
    """
    if len(buf) < 10:
        return {"velocity_per_sec": 0.0, "acceleration": 0.0,
                "is_decelerating": False, "recovery_signal": False, "data_points": len(buf)}

    # Use last 30s for velocity, last 10s for recent trend
    recent = buf[-10:]
    full = buf[-30:] if len(buf) >= 30 else buf

    # Velocity: price change per second over full window
    dt = full[-1]["ts"] - full[0]["ts"]
    if dt < 1.0:
        dt = 1.0
    base_price = full[0]["price"]
    price_delta = (full[-1]["price"] - base_price) / base_price if base_price > 0 else 0.0
    # For LONG: positive velocity = good, negative = bad
    # For SHORT: negative velocity = good, positive = bad
    if side.upper() == "SHORT":
        price_delta = -price_delta
    velocity = price_delta / dt

    # Acceleration: compare velocity of first half vs second half
    mid = len(full) // 2
    first_half = full[:mid]
    second_half = full[mid:]
    if len(first_half) >= 2 and len(second_half) >= 2:
        dt1 = max(1.0, first_half[-1]["ts"] - first_half[0]["ts"])
        dt2 = max(1.0, second_half[-1]["ts"] - second_half[0]["ts"])
        first_base = first_half[0]["price"]
        second_base = second_half[0]["price"]
        v1_delta = ((first_half[-1]["price"] - first_base) / first_base) if first_base > 0 else 0.0
        v2_delta = ((second_half[-1]["price"] - second_base) / second_base) if second_base > 0 else 0.0
        if side.upper() == "SHORT":
            v1_delta, v2_delta = -v1_delta, -v2_delta
        v1 = v1_delta / dt1
        v2 = v2_delta / dt2
        acceleration = v2 - v1
    else:
        acceleration = 0.0

    # Recovery signal: price in last 10s moving back toward entry
    recent_dt = max(1.0, recent[-1]["ts"] - recent[0]["ts"])
    recent_base = recent[0]["price"]
    recent_delta = ((recent[-1]["price"] - recent_base) / recent_base) if recent_base > 0 else 0.0
    if side.upper() == "SHORT":
        recent_delta = -recent_delta
    recovery_signal = recent_delta > 0 and len(recent) >= 5

    is_decelerating = velocity < 0 and acceleration > 0

    return {
        "velocity_per_sec": round(velocity, 8),
        "acceleration": round(acceleration, 10),
        "is_decelerating": is_decelerating,
        "recovery_signal": recovery_signal,
        "data_points": len(buf),
        "recent_velocity": round(recent_delta / recent_dt, 8),
    }


def _evaluate_drawdown_intelligence(
    snapshot: Dict[str, Any],
    trend: Dict[str, Any],
    settings: Any,
) -> Dict[str, Any]:
    """Determine action: HOLD (recoverable) vs CUT (definitive trend).

    Decision matrix:
    - CUT if: velocity strongly negative + accelerating + no support nearby
    - TIGHTEN_SL if: velocity negative but decelerating (tighten SL to limit further loss)
    - HOLD if: recovery signal detected or very close to support level
    """
    velocity = trend["velocity_per_sec"]
    acceleration = trend["acceleration"]
    is_decelerating = trend["is_decelerating"]
    recovery_signal = trend["recovery_signal"]

    # Support proximity (if available)
    dist_to_support = snapshot.get("dist_to_support_pct")
    near_support = dist_to_support is not None and abs(dist_to_support) < 0.005  # within 0.5%

    # Thresholds (configurable)
    cut_velocity_threshold = _to_float(
        get_param(settings, "drawdown_cut_velocity_threshold", -0.0003), -0.0003)
    cut_min_data_points = int(get_param(settings, "drawdown_cut_min_data_points", 20) or 20)

    # Not enough data yet — always hold
    if trend["data_points"] < cut_min_data_points:
        return {"action": "HOLD", "reason": "insufficient_data", "confidence": 0.3}

    # Recovery detected
    if recovery_signal and is_decelerating:
        return {"action": "HOLD", "reason": "recovery_signal_decelerating", "confidence": 0.7}

    # Near support — likely bounce zone
    if near_support and is_decelerating:
        return {"action": "HOLD", "reason": "near_support_decelerating", "confidence": 0.6}

    # Definitive trend: fast velocity + accelerating
    if velocity < cut_velocity_threshold and acceleration < 0 and not near_support:
        return {"action": "CUT", "reason": "definitive_trend_accelerating", "confidence": 0.8}

    # Moderate adverse: decelerating but still falling — tighten SL
    if velocity < 0 and is_decelerating:
        return {"action": "TIGHTEN_SL", "reason": "falling_but_decelerating", "confidence": 0.5}

    # Slow bleed without recovery
    if velocity < cut_velocity_threshold * 0.5 and not recovery_signal:
        return {"action": "TIGHTEN_SL", "reason": "slow_bleed_no_recovery", "confidence": 0.5}

    return {"action": "HOLD", "reason": "no_clear_signal", "confidence": 0.4}


def _assess_drawdown_recovery_window(
    snapshot: Dict[str, Any],
    trend: Dict[str, Any],
    prior_state: Optional[Dict[str, Any]],
    settings: Any,
) -> Dict[str, Any]:
    """Assess whether a loss-side position should defer a hard cut for recovery.

    This creates a small state machine:
      - monitoring: loss detected, waiting for meaningful rescue behavior
      - recoverable: rescue signal seen, defer hard cut while structure improves
      - failed_recovery: rescue failed, allow hard cut path to proceed
    """
    threshold_usdt = _to_float(get_param(settings, "drawdown_monitor_threshold_usdt", -10.0), -10.0)
    rescue_score_min_usdt = _to_float(get_param(settings, "drawdown_recovery_score_min_usdt", 3.0), 3.0)
    failed_recovery_giveback_usdt = _to_float(get_param(settings, "drawdown_failed_recovery_giveback_usdt", 4.0), 4.0)
    shock_recent_velocity_threshold = _to_float(
        get_param(settings, "drawdown_shock_recent_velocity_threshold", -0.0012),
        -0.0012,
    )

    unrealized = _to_float(snapshot.get("unrealized_pnl_usdt"), 0.0)
    state = dict(prior_state or {})

    # Reset state if we are no longer in loss-side drawdown territory.
    if unrealized >= threshold_usdt:
        return {
            "phase": "inactive",
            "defer_cut": False,
            "failed_recovery": False,
            "shock": False,
            "state": {},
        }

    threshold_cross_pnl = _to_float(state.get("threshold_cross_pnl"), unrealized)
    rescue_best = max(_to_float(state.get("rescue_best_unrealized"), unrealized), unrealized)
    rescue_observed = bool(state.get("rescue_observed", False))

    recovery_candidate = bool(trend.get("recovery_signal")) or _to_float(trend.get("recent_velocity"), 0.0) > 0.0
    rescue_score = rescue_best - threshold_cross_pnl
    if recovery_candidate and rescue_score >= rescue_score_min_usdt:
        rescue_observed = True

    giveback_from_best = rescue_best - unrealized
    failed_recovery = (
        rescue_observed
        and _to_float(trend.get("recent_velocity"), 0.0) < 0.0
        and _to_float(trend.get("acceleration"), 0.0) < 0.0
        and giveback_from_best >= failed_recovery_giveback_usdt
    )

    shock = (
        _to_float(trend.get("recent_velocity"), 0.0) <= shock_recent_velocity_threshold
        and _to_float(trend.get("acceleration"), 0.0) < 0.0
        and not bool(trend.get("recovery_signal"))
    )

    defer_cut = bool((recovery_candidate or rescue_observed) and not failed_recovery and not shock)

    if failed_recovery:
        phase = "failed_recovery"
    elif defer_cut:
        phase = "recoverable"
    else:
        phase = "monitoring"

    state.update({
        "phase": phase,
        "threshold_cross_pnl": round(threshold_cross_pnl, 6),
        "rescue_best_unrealized": round(rescue_best, 6),
        "rescue_observed": rescue_observed,
        "recovery_candidate": recovery_candidate,
        "rescue_score": round(rescue_score, 6),
        "giveback_from_best": round(giveback_from_best, 6),
        "failed_recovery": failed_recovery,
        "shock": shock,
    })

    return {
        "phase": phase,
        "defer_cut": defer_cut,
        "failed_recovery": failed_recovery,
        "shock": shock,
        "state": state,
    }


def _evaluate_profit_erosion(
    snapshot: Dict[str, Any],
    trend: Dict[str, Any],
    settings: Any,
) -> Dict[str, Any]:
    """Evaluate profit erosion for positions still in profit but losing momentum.

    CRITICAL: This function NEVER closes a profitable position. It only tightens
    SL to lock in a portion of realized gains. The philosophy is defensive:
    - If momentum is positive or neutral → do nothing (let winners run)
    - If momentum reverses AND accelerates → protect gains by tightening SL
    - Always leave room for normal volatility (ATR-aware buffer)

    Anti-strangulation guarantees:
    1. Only acts when velocity is negative for >= 15s (not a single bad tick)
    2. Only tightens to protect 30-50% of peak, never locks 100%
    3. Never moves SL above current price (which would trigger immediate close)
    4. Respects resistance/support proximity — if near resistance on a LONG, the
       pullback is expected (partial retracement at resistance)
    5. Recent velocity must be negative (last 10s still falling, not just noise)
    """
    velocity = trend["velocity_per_sec"]
    recent_velocity = trend.get("recent_velocity", 0.0)
    acceleration = trend["acceleration"]
    recovery_signal = trend["recovery_signal"]
    data_points = trend["data_points"]

    # Need at least 15s of data before any erosion decision
    if data_points < 15:
        return {"action": "HOLD", "reason": "erosion_insufficient_data", "confidence": 0.2}

    # If recent velocity is positive (last 10s bouncing back) → never act
    if recovery_signal or recent_velocity > 0:
        return {"action": "HOLD", "reason": "erosion_recovery_detected", "confidence": 0.7}

    # If velocity is positive or neutral → do nothing (still running)
    erosion_velocity_threshold = _to_float(
        get_param(settings, "erosion_velocity_threshold", -0.00015), -0.00015)
    if velocity >= erosion_velocity_threshold:
        return {"action": "HOLD", "reason": "erosion_velocity_neutral", "confidence": 0.6}

    # Near resistance on LONG (or near support on SHORT) — expected pullback zone
    dist_to_resistance = snapshot.get("dist_to_resistance_pct")
    side = str(snapshot.get("side") or "LONG").upper()
    if side == "LONG" and dist_to_resistance is not None and abs(dist_to_resistance) < 0.008:
        return {"action": "HOLD", "reason": "erosion_near_resistance_expected_pullback", "confidence": 0.5}
    dist_to_support = snapshot.get("dist_to_support_pct")
    if side == "SHORT" and dist_to_support is not None and abs(dist_to_support) < 0.008:
        return {"action": "HOLD", "reason": "erosion_near_support_expected_pullback", "confidence": 0.5}

    # Erosion confirmed: velocity negative + recent also negative + accelerating
    if velocity < erosion_velocity_threshold and recent_velocity < 0 and acceleration < 0:
        return {"action": "PROTECT_PROFIT", "reason": "erosion_accelerating_reversal", "confidence": 0.7}

    # Moderate erosion: velocity negative but decelerating — gentler protection
    if velocity < erosion_velocity_threshold and recent_velocity < 0:
        return {"action": "PROTECT_PROFIT_GENTLE", "reason": "erosion_steady_decline", "confidence": 0.5}

    return {"action": "HOLD", "reason": "erosion_no_clear_signal", "confidence": 0.4}


def _run_drawdown_monitor_cycle(
    components: Any, snapshots: List[Dict[str, Any]], settings: Any
) -> List[Dict[str, Any]]:
    """Evaluate ALL positions (in profit and in loss) with intelligent analysis.

    Two modes of operation:
    - Positions in loss (< threshold): drawdown analysis → CUT or TIGHTEN_SL
    - Positions in profit but eroding: erosion analysis → PROTECT_PROFIT (lock gains)

    Anti-strangulation: profit erosion NEVER closes. It only tightens SL to protect
    a fraction of the peak, always leaving 50%+ headroom for normal retracement.
    """
    if not snapshots:
        return []
    mode = str(get_param(settings, "position_reaction_policy_mode", "telemetry_only") or "telemetry_only").strip().lower()
    if mode not in {"hard_rules", "ml_assisted"}:
        return []
    if not bool(get_param(settings, "drawdown_intelligent_monitor_enabled", True)):
        return []

    threshold_usdt = _to_float(
        get_param(settings, "drawdown_monitor_threshold_usdt", -10.0), -10.0)
    # Erosion: only activate when position has had meaningful profit first
    erosion_min_peak_usdt = _to_float(
        get_param(settings, "erosion_min_peak_usdt", 5.0), 5.0)
    # How much of peak to protect (0.30 = lock 30% of peak as floor)
    erosion_lock_fraction = _to_float(
        get_param(settings, "erosion_lock_fraction", 0.30), 0.30)
    erosion_lock_fraction_gentle = _to_float(
        get_param(settings, "erosion_lock_fraction_gentle", 0.15), 0.15)

    actions: List[Dict[str, Any]] = []
    cycle_shadow = {
        "loss_positions": 0,
        "recoverable_windows": 0,
        "failed_recovery_windows": 0,
        "shock_events": 0,
        "deferred_cuts": 0,
    }
    for snapshot in snapshots:
        unrealized = _to_float(snapshot.get("unrealized_pnl_usdt"), 0.0)
        pos_id = snapshot.get("position_id", "")
        if not pos_id:
            continue
        price = _to_float(snapshot.get("current_price"), 0.0)

        # Always update buffer for all positions (provides continuity)
        buf = _update_drawdown_buffer(pos_id, price, unrealized)
        side = str(snapshot.get("side") or "LONG").upper()

        # Analyze trend from buffer (shared for both cases)
        trend = _analyze_drawdown_trend(buf, side)

        # ── Case A: Position in loss territory ──
        if unrealized < threshold_usdt:
            cycle_shadow["loss_positions"] += 1
            decision = _evaluate_drawdown_intelligence(snapshot, trend, settings)

            prev = _drawdown_decisions.get(pos_id, {})
            recovery_eval = _assess_drawdown_recovery_window(
                snapshot,
                trend,
                prev.get("_recovery_state", {}),
                settings,
            )
            decision["_recovery_state"] = recovery_eval.get("state", {})
            decision["_recovery_phase"] = recovery_eval.get("phase", "monitoring")
            if recovery_eval.get("phase") == "recoverable":
                cycle_shadow["recoverable_windows"] += 1
            if recovery_eval.get("failed_recovery", False):
                cycle_shadow["failed_recovery_windows"] += 1
            if recovery_eval.get("shock", False):
                cycle_shadow["shock_events"] += 1

            # Merge with previous decision (avoid flip-flopping)
            consecutive_cuts = prev.get("_consecutive_cuts", 0)
            if decision["action"] == "CUT" and not recovery_eval.get("defer_cut", False):
                consecutive_cuts += 1
            else:
                consecutive_cuts = 0

            if decision["action"] == "CUT" and recovery_eval.get("defer_cut", False):
                cycle_shadow["deferred_cuts"] += 1
                decision["action"] = "HOLD"
                decision["reason"] = f"recovery_window_{decision.get('reason', 'defer_cut')}"

            decision["_consecutive_cuts"] = consecutive_cuts
            _drawdown_decisions[pos_id] = decision

            min_consecutive_for_cut = int(get_param(settings, "drawdown_cut_min_consecutive", 3) or 3)
            shock_immediate_cut_enabled = bool(get_param(settings, "drawdown_shock_immediate_cut_enabled", True))
            effective_min_for_cut = 1 if (shock_immediate_cut_enabled and recovery_eval.get("shock", False)) else min_consecutive_for_cut

            if decision["action"] == "CUT" and consecutive_cuts >= effective_min_for_cut:
                entry_price = _to_float(snapshot.get("entry_price"), 0.0)
                current_price = _to_float(snapshot.get("current_price"), 0.0)
                if entry_price > 0 and current_price > 0:
                    if side == "LONG":
                        emergency_sl = current_price * 0.999
                    else:
                        emergency_sl = current_price * 1.001
                    actions.append({
                        "action": "CUT",
                        "symbol": snapshot.get("symbol"),
                        "position_id": pos_id,
                        "trade_type": snapshot.get("trade_type"),
                        "side": side,
                        "emergency_sl": round(emergency_sl, 8),
                            "current_price": round(current_price, 8),
                        "unrealized_usdt": round(unrealized, 2),
                        "trend": trend,
                        "decision": decision,
                        "recovery_phase": recovery_eval.get("phase"),
                        "shock": recovery_eval.get("shock", False),
                    })

            elif decision["action"] == "TIGHTEN_SL":
                current_sl = _to_float(snapshot.get("stop_loss"), 0.0)
                current_price = _to_float(snapshot.get("current_price"), 0.0)
                tighten_fraction = _to_float(
                    get_param(settings, "drawdown_recovery_tighten_fraction", 0.02),
                    0.02,
                ) if recovery_eval.get("phase") == "recoverable" else _to_float(
                    get_param(settings, "drawdown_default_tighten_fraction", 0.05),
                    0.05,
                )
                if current_sl > 0 and current_price > 0:
                    if side == "LONG":
                        gap = current_price - current_sl
                        tighter_sl = current_sl + gap * tighten_fraction
                        if tighter_sl > current_sl and tighter_sl < current_price * 0.998:
                            actions.append({
                                "action": "TIGHTEN_SL",
                                "symbol": snapshot.get("symbol"),
                                "position_id": pos_id,
                                "trade_type": snapshot.get("trade_type"),
                                "side": side,
                                "target_sl": round(tighter_sl, 8),
                                "current_price": round(current_price, 8),
                                "unrealized_usdt": round(unrealized, 2),
                                "trend": trend,
                                "decision": decision,
                                "recovery_phase": recovery_eval.get("phase"),
                            })
                    elif side == "SHORT":
                        gap = current_sl - current_price
                        tighter_sl = current_sl - gap * tighten_fraction
                        if tighter_sl < current_sl and tighter_sl > current_price * 1.002:
                            actions.append({
                                "action": "TIGHTEN_SL",
                                "symbol": snapshot.get("symbol"),
                                "position_id": pos_id,
                                "trade_type": snapshot.get("trade_type"),
                                "side": side,
                                "target_sl": round(tighter_sl, 8),
                                "current_price": round(current_price, 8),
                                "unrealized_usdt": round(unrealized, 2),
                                "trend": trend,
                                "decision": decision,
                                "recovery_phase": recovery_eval.get("phase"),
                            })

        # ── Case B: Position in profit but eroding ──
        # Only evaluate if position has had meaningful profit (avoid noise on
        # positions that just opened and are oscillating near entry)
        elif unrealized > 0:
            peak_pnl_pct = _to_float(snapshot.get("peak_pnl_pct"), 0.0)
            # Estimate peak USD from peak_pnl_pct and entry_price/qty
            entry_price = _to_float(snapshot.get("entry_price"), 0.0)
            qty = _to_float(snapshot.get("quantity"), 0.0)
            peak_usdt_estimate = abs(peak_pnl_pct / 100.0) * entry_price * qty if entry_price > 0 and qty > 0 else 0.0
            # Also check buffer history for peak
            buf_peak = max((tick["pnl_usdt"] for tick in buf), default=0.0)
            effective_peak = max(peak_usdt_estimate, buf_peak, unrealized)

            if effective_peak >= erosion_min_peak_usdt:
                erosion_decision = _evaluate_profit_erosion(snapshot, trend, settings)

                if erosion_decision["action"] in ("PROTECT_PROFIT", "PROTECT_PROFIT_GENTLE"):
                    # Calculate protective SL: lock a fraction of peak profit
                    lock_frac = (erosion_lock_fraction
                                 if erosion_decision["action"] == "PROTECT_PROFIT"
                                 else erosion_lock_fraction_gentle)
                    current_sl = _to_float(snapshot.get("stop_loss"), 0.0)
                    current_price = _to_float(snapshot.get("current_price"), 0.0)

                    if entry_price > 0 and current_price > 0:
                        # Target SL = entry + lock_frac * (peak_price - entry)
                        # This guarantees we keep at least lock_frac of the peak move
                        if side == "LONG":
                            peak_price_est = entry_price * (1.0 + abs(peak_pnl_pct) / 100.0)
                            target_sl = entry_price + (peak_price_est - entry_price) * lock_frac
                            # Safety: never set SL above 97% of current price
                            target_sl = min(target_sl, current_price * 0.97)
                            # Only tighten, never loosen
                            if current_sl > 0 and target_sl <= current_sl:
                                continue
                            # Sanity: SL must be below current price
                            if target_sl >= current_price:
                                continue
                        elif side == "SHORT":
                            peak_price_est = entry_price * (1.0 - abs(peak_pnl_pct) / 100.0)
                            target_sl = entry_price - (entry_price - peak_price_est) * lock_frac
                            # Safety: never set SL below 103% of current price
                            target_sl = max(target_sl, current_price * 1.03)
                            # Only tighten, never loosen
                            if current_sl > 0 and target_sl >= current_sl:
                                continue
                            if target_sl <= current_price:
                                continue
                        else:
                            continue

                        actions.append({
                            "action": "PROTECT_PROFIT",
                            "symbol": snapshot.get("symbol"),
                            "position_id": pos_id,
                            "trade_type": snapshot.get("trade_type"),
                            "side": side,
                            "target_sl": round(target_sl, 8),
                            "unrealized_usdt": round(unrealized, 2),
                            "peak_usdt_estimate": round(effective_peak, 2),
                            "lock_fraction": lock_frac,
                            "trend": trend,
                            "decision": erosion_decision,
                        })

    with _lock:
        _drawdown_cycle_shadow.update(cycle_shadow)
    return actions


def _apply_drawdown_actions(
    components: Any, actions: List[Dict[str, Any]], settings: Any
) -> int:
    """Persist drawdown monitor decisions (SL updates). Returns count of applied."""
    if not actions:
        return 0
    manager = getattr(components, "position_manager", None)
    futures_client = None
    if bool(get_param(settings, "futures_enabled", True)):
        try:
            from run_trades import _get_futures_client
            futures_client = _get_futures_client(settings, db_client=getattr(components, "db_client", None))
        except Exception:
            futures_client = None

    applied = 0
    for act in actions:
        target_sl = act.get("emergency_sl") or act.get("target_sl")
        if not target_sl:
            continue
        trade_type = str(act.get("trade_type") or "").lower()
        try:
            if trade_type == "spot" and manager:
                pos_id = act["position_id"]
                manager.update_position_sl(pos_id, float(target_sl))
                applied += 1
                print(f"[DrawdownMonitor] {act['action']} {act.get('symbol')} SL→{target_sl:.6f} (pnl={act.get('unrealized_usdt'):.2f})")
            elif trade_type == "futures" and futures_client:
                snapshot_for_guard = {"symbol": act.get("symbol"), "side": act.get("side"), "current_price": 0.0}
                ok = futures_client.update_sl_tp(
                    act.get("symbol"), act.get("side"),
                    new_sl=float(target_sl), current_price=float(act.get("current_price", 0)),
                )
                if ok:
                    applied += 1
                    print(f"[DrawdownMonitor] {act['action']} {act.get('symbol')} SL→{target_sl:.6f} (pnl={act.get('unrealized_usdt'):.2f})")
        except Exception as exc:
            print(f"[DrawdownMonitor] failed to apply {act['action']} for {act.get('symbol')}: {exc}")

    # Audit
    db_client = getattr(components, "db_client", None)
    if db_client and actions:
        try:
            for act in actions[:5]:  # limit audit volume
                db_client.execute_update(
                    "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s, %s, %s, %s, %s)",
                    (act.get("symbol", ""), act.get("trade_type", ""), f"drawdown_monitor_{act['action'].lower()}",
                     json.dumps({k: v for k, v in act.items() if k != "trend"}, default=str), False),
                )
        except Exception:
            pass
    return applied


def cleanup_drawdown_buffers(active_position_ids: set) -> None:
    """Remove buffers for closed positions. Call periodically."""
    stale = [pid for pid in _drawdown_buffers if pid not in active_position_ids]
    for pid in stale:
        _drawdown_buffers.pop(pid, None)
        _drawdown_decisions.pop(pid, None)


def _run_profit_guard_cycle(components: Any, snapshots: List[Dict[str, Any]], settings: Any) -> List[Dict[str, Any]]:
    """Apply cross-mode profit guard actions from 1s telemetry snapshots.

    Works across all regimes and both trade types. It only tightens SL and
    persists auditable state; it never widens risk.
    """
    if not snapshots:
        return []
    mode = str(get_param(settings, "position_reaction_policy_mode", "telemetry_only") or "telemetry_only").strip().lower()
    if mode not in {"hard_rules", "ml_assisted"}:
        return []
    if not bool(get_param(settings, "position_profit_guard_enabled", True)):
        return []

    manager = getattr(components, "position_manager", None)
    futures_client = None
    if bool(get_param(settings, "futures_enabled", True)):
        try:
            from run_trades import _get_futures_client
            futures_client = _get_futures_client(settings, db_client=getattr(components, "db_client", None))
        except Exception:
            futures_client = None

    actions: List[Dict[str, Any]] = []
    candidates = 0
    for snapshot in snapshots:
        guard = _compute_profit_guard_target(snapshot, settings)
        if not guard:
            continue
        candidates += 1

        trade_type = str(snapshot.get("trade_type") or "").lower()
        applied = False
        if trade_type == "spot":
            applied = _persist_spot_profit_guard(manager, snapshot, guard)
        elif trade_type == "futures":
            applied = _persist_futures_profit_guard(futures_client, snapshot, guard)

        if applied:
            actions.append({
                "symbol": snapshot.get("symbol"),
                "trade_type": trade_type,
                "side": guard.get("side"),
                "target_sl": guard.get("target_sl"),
                "lock_mode": guard.get("lock_mode"),
                "giveback_pct": guard.get("giveback_pct"),
                "allowed_giveback_pct": guard.get("allowed_giveback_pct"),
            })
    with _lock:
        _shadow_metrics["profit_guard_candidates"] += candidates
    return actions


def _fetch_futures_metrics(futures_client: Any, symbol: str) -> Dict[str, Any]:
    """Lightweight futures-only enrichment. Fail-open."""
    out: Dict[str, Any] = {}
    try:
        if hasattr(futures_client, "_client") and futures_client._client:
            client = futures_client._client
            # funding rate
            try:
                fr = client.funding_rate(symbol=symbol, limit=1)
                if fr:
                    out["funding_rate_annualized"] = float(fr[0].get("fundingRate", 0.0) or 0.0) * 100 * 365
                    out["funding_time"] = fr[0].get("fundingTime")
            except Exception:
                pass
            # open interest
            try:
                oi = client.open_interest(symbol=symbol)
                if oi:
                    out["open_interest"] = float(oi.get("openInterest", 0.0) or 0.0)
            except Exception:
                pass
    except Exception:
        pass
    return out


def _snapshot_spot_position(symbol: str, pos: Any, price: float) -> Dict[str, Any]:
    entry = float(pos.entry_price or 0.0)
    qty = float(pos.quantity or 0.0)
    unrealized_pct = _compute_unrealized_pct(entry, price, "LONG")
    unrealized_usdt = _compute_unrealized(entry, qty, price, "LONG", leverage=1.0)
    sltp = _extract_sl_tp(getattr(pos, "metadata", None), pos.stop_loss or 0.0, 0.0)
    meta = getattr(pos, "metadata", {}) or {}
    smart = meta.get("_smart_meta") or {}
    return {
        "trade_type": "spot",
        "symbol": symbol,
        "side": "LONG",
        "position_id": str(pos.position_id),
        "entry_price": entry,
        "quantity": qty,
        "current_price": price,
        "unrealized_pnl_pct": round(unrealized_pct * 100, 4),
        "unrealized_pnl_usdt": round(unrealized_usdt, 4),
        "stop_loss": sltp["sl"],
        "take_profit": sltp["tp"],
        "peak_pnl_pct": round(float(smart.get("peak_pnl_pct", 0.0) or 0.0), 4),
        "breakeven_done": bool(smart.get("breakeven_done", False)),
        "trail_best_price": float(smart.get("trail_best_price") or 0.0),
        "support_level": smart.get("support_level"),
        "resistance_level": smart.get("resistance_level"),
        "dist_to_support_pct": round(_distance_to_sr(price, smart.get("support_level"), "LONG") or 0.0, 4) if smart.get("support_level") else None,
        "dist_to_resistance_pct": round(_distance_to_sr(price, smart.get("resistance_level"), "LONG") or 0.0, 4) if smart.get("resistance_level") else None,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def _snapshot_futures_position(pos: Any, price: float, futures_client: Any) -> Dict[str, Any]:
    entry = float(pos.entry_price or 0.0)
    qty = abs(float(getattr(pos, "qty", getattr(pos, "quantity", 0.0)) or 0.0))
    side = str(pos.side).upper()
    leverage = float(getattr(pos, "leverage", getattr(futures_client, "_active_leverage", 1)) or 1)
    unrealized_pct = _compute_unrealized_pct(entry, price, side)
    unrealized_usdt = _compute_unrealized(entry, qty, price, side, leverage=leverage)
    sltp = _extract_sl_tp(getattr(pos, "_smart_meta", None) or {}, pos.sl_price or 0.0, pos.tp_price or 0.0)
    smart = getattr(pos, "_smart_meta", {}) or {}
    metrics = _fetch_futures_metrics(futures_client, pos.symbol)
    return {
        "trade_type": "futures",
        "symbol": pos.symbol,
        "side": side,
        "position_id": str(getattr(pos, "position_id", "") or f"{pos.symbol}_{side}"),
        "entry_price": entry,
        "quantity": qty,
        "leverage": leverage,
        "current_price": price,
        "unrealized_pnl_pct": round(unrealized_pct * 100, 4),
        "unrealized_pnl_usdt": round(unrealized_usdt, 4),
        "stop_loss": sltp["sl"],
        "take_profit": sltp["tp"],
        "peak_pnl_pct": round(float(smart.get("peak_pnl_pct", 0.0) or 0.0), 4),
        "breakeven_done": bool(smart.get("breakeven_done", False)),
        "trail_best_price": float(smart.get("trail_best_price") or 0.0),
        "support_level": smart.get("support_level"),
        "resistance_level": smart.get("resistance_level"),
        "dist_to_support_pct": round(_distance_to_sr(price, smart.get("support_level"), side) or 0.0, 4) if smart.get("support_level") else None,
        "dist_to_resistance_pct": round(_distance_to_sr(price, smart.get("resistance_level"), side) or 0.0, 4) if smart.get("resistance_level") else None,
        **metrics,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def _persist_snapshots(db_client: Any, snapshots: List[Dict[str, Any]], throttle_seconds: float) -> None:
    if not db_client or not snapshots:
        return
    now = _now_ts()
    with _lock:
        if now - _last_db_flush.get("all", 0.0) < throttle_seconds:
            return
        _last_db_flush["all"] = now
    try:
        # Ensure table exists (idempotent)
        db_client.execute_update(
            """
            CREATE TABLE IF NOT EXISTS position_telemetry (
                id BIGSERIAL PRIMARY KEY,
                ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                symbol TEXT NOT NULL,
                trade_type TEXT NOT NULL,
                side TEXT NOT NULL,
                position_id TEXT NOT NULL,
                snapshot JSONB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_position_telemetry_lookup
                ON position_telemetry (symbol, position_id, ts DESC);
            """
        )
    except Exception:
        pass
    try:
        for snap in snapshots:
            db_client.execute_update(
                "INSERT INTO position_telemetry (ts, symbol, trade_type, side, position_id, snapshot) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    snap["ts"],
                    snap["symbol"],
                    snap["trade_type"],
                    snap["side"],
                    snap["position_id"],
                    json.dumps(snap),
                ),
            )
    except Exception as e:
        print(f"[PositionTelemetry] DB write failed: {e}")


def _audit_summary(db_client: Any, summary: Dict[str, Any], throttle_seconds: float) -> None:
    if not db_client:
        return
    now = _now_ts()
    key = "position_telemetry_summary"
    with _lock:
        if now - _last_audit_flush.get(key, 0.0) < throttle_seconds:
            return
        _last_audit_flush[key] = now
    try:
        db_client.execute_update(
            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s, %s, %s, %s, %s)",
            ("system", "system", "position_telemetry_summary", json.dumps(summary), False),
        )
    except Exception as e:
        print(f"[PositionTelemetry] audit write failed: {e}")


def collect_position_telemetry(components: Any) -> List[Dict[str, Any]]:
    """Collect one round of snapshots for all open positions (SPOT + FUTURES)."""
    snapshots: List[Dict[str, Any]] = []
    try:
        client = getattr(components, "binance_client", None)
        manager = getattr(components, "position_manager", None)
        settings = getattr(components, "settings", None)
        futures_client = None
        if settings and bool(get_param(settings, "futures_enabled", True)):
            try:
                from run_trades import _get_futures_client
                futures_client = _get_futures_client(settings, db_client=getattr(components, "db_client", None))
            except Exception:
                pass

        # SPOT positions
        if manager and client:
            try:
                positions = manager.get_all_positions() or {}
            except Exception:
                positions = {}
            for symbol, pos_list in list(positions.items()):
                if not pos_list:
                    continue
                try:
                    price = float(client.get_symbol_price(symbol, use_cache=False))
                except Exception:
                    continue
                for pos in pos_list:
                    try:
                        snapshots.append(_snapshot_spot_position(symbol, pos, price))
                    except Exception:
                        pass

        # FUTURES positions — prefer WebSocket mark price (zero API weight)
        if futures_client:
            # Try to get mark prices from event stream (zero cost)
            event_stream = getattr(components, "event_stream", None) or getattr(
                getattr(components, "orchestrator", None), "event_stream", None)
            mark_prices = {}
            if event_stream and hasattr(event_stream, "get_all_mark_prices"):
                try:
                    mark_prices = event_stream.get_all_mark_prices() or {}
                except Exception:
                    pass
            try:
                fut_positions = list(futures_client._open_positions.items())
            except Exception:
                fut_positions = []
            for key, pos in fut_positions:
                if getattr(pos, "closed", False):
                    continue
                try:
                    # Use WebSocket mark price first (free), fallback to REST (cached 15s)
                    ws_price = mark_prices.get(pos.symbol)
                    price = float(ws_price) if ws_price and ws_price > 0 else float(futures_client.get_price(pos.symbol))
                except Exception:
                    continue
                try:
                    snapshots.append(_snapshot_futures_position(pos, price, futures_client))
                except Exception:
                    pass
    except Exception as e:
        print(f"[PositionTelemetry] collection error: {e}")
    return snapshots


def run_telemetry_cycle(components: Any) -> List[Dict[str, Any]]:
    """One telemetry cycle: collect, persist (throttled), audit summary."""
    settings = getattr(components, "settings", None)
    if not settings or not bool(get_param(settings, "position_telemetry_watcher_enabled", True)):
        return []
    snapshots = collect_position_telemetry(components)
    if not snapshots:
        return []
    db_client = getattr(components, "db_client", None)
    throttle = float(get_param(settings, "position_telemetry_db_throttle_seconds", 5.0) or 5.0)
    _persist_snapshots(db_client, snapshots, throttle)
    summary = {
        "count": len(snapshots),
        "spot": sum(1 for s in snapshots if s["trade_type"] == "spot"),
        "futures": sum(1 for s in snapshots if s["trade_type"] == "futures"),
        "long": sum(1 for s in snapshots if s["side"] == "LONG"),
        "short": sum(1 for s in snapshots if s["side"] == "SHORT"),
        "avg_unrealized_pnl_pct": round(
            sum(float(s.get("unrealized_pnl_pct", 0.0) or 0.0) for s in snapshots) / len(snapshots), 4
        ) if snapshots else 0.0,
        "profit_positions_evaluated": sum(1 for s in snapshots if _to_float(s.get("unrealized_pnl_usdt"), 0.0) > 0),
        "loss_positions_evaluated": sum(1 for s in snapshots if _to_float(s.get("unrealized_pnl_usdt"), 0.0) < 0),
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    # Dynamic 1s profit-follow guard across all modes (SPOT + FUTURES).
    guard_actions = _run_profit_guard_cycle(components, snapshots, settings)
    if guard_actions:
        summary["profit_guard_actions"] = len(guard_actions)
        summary["profit_guard_hard"] = sum(1 for a in guard_actions if a.get("lock_mode") == "hard")
        summary["profit_guard_soft"] = sum(1 for a in guard_actions if a.get("lock_mode") == "soft")
        summary["profit_guard_symbols"] = sorted({str(a.get("symbol") or "") for a in guard_actions if a.get("symbol")})
    summary["profit_guard_candidates"] = _shadow_metrics.get("profit_guard_candidates", 0)

    # s120: Intelligent drawdown monitor — analyze trend for positions in loss
    dd_actions = _run_drawdown_monitor_cycle(components, snapshots, settings)
    dd_applied = _apply_drawdown_actions(components, dd_actions, settings)
    if dd_actions:
        summary["drawdown_monitor_evaluated"] = len(dd_actions)
        summary["drawdown_monitor_applied"] = dd_applied
        summary["drawdown_monitor_cuts"] = sum(1 for a in dd_actions if a.get("action") == "CUT")
        summary["drawdown_monitor_tightens"] = sum(1 for a in dd_actions if a.get("action") == "TIGHTEN_SL")

    with _lock:
        summary["drawdown_recoverable_windows"] = int(_drawdown_cycle_shadow.get("recoverable_windows", 0))
        summary["drawdown_failed_recovery_windows"] = int(_drawdown_cycle_shadow.get("failed_recovery_windows", 0))
        summary["drawdown_shock_events"] = int(_drawdown_cycle_shadow.get("shock_events", 0))
        summary["drawdown_deferred_cuts"] = int(_drawdown_cycle_shadow.get("deferred_cuts", 0))

    # Cleanup stale buffers (positions that closed)
    active_ids = {s.get("position_id") for s in snapshots if s.get("position_id")}
    cleanup_drawdown_buffers(active_ids)

    with _lock:
        _shadow_metrics["profit_positions_evaluated"] += summary.get("profit_positions_evaluated", 0)
        _shadow_metrics["loss_positions_evaluated"] += summary.get("loss_positions_evaluated", 0)
        _shadow_metrics["profit_guard_actions"] += len(guard_actions)
        _shadow_metrics["profit_guard_hard"] += summary.get("profit_guard_hard", 0)
        _shadow_metrics["profit_guard_soft"] += summary.get("profit_guard_soft", 0)
        _shadow_metrics["drawdown_actions"] += len(dd_actions)
        _shadow_metrics["drawdown_cuts"] += summary.get("drawdown_monitor_cuts", 0)
        _shadow_metrics["drawdown_tightens"] += summary.get("drawdown_monitor_tightens", 0)
        _shadow_metrics["drawdown_recoverable_windows"] += summary.get("drawdown_recoverable_windows", 0)
        _shadow_metrics["drawdown_failed_recovery_windows"] += summary.get("drawdown_failed_recovery_windows", 0)
        _shadow_metrics["drawdown_shock_events"] += summary.get("drawdown_shock_events", 0)
        _shadow_metrics["drawdown_deferred_cuts"] += summary.get("drawdown_deferred_cuts", 0)
        _shadow_metrics["last_summary"] = dict(summary)

    _audit_summary(db_client, summary, throttle)

    # ── SR structure-invalidation early exit (shadow) ──
    try:
        lookback = float(get_param(settings, "sr_invalidation_lookback_minutes", 10.0) or 10.0)
        symbols = list({s["symbol"] for s in snapshots if s.get("symbol")})
        recent_signals = _fetch_recent_signals(db_client, symbols, lookback)
        with _lock:
            _shadow_metrics["evaluations"] += len(snapshots)
            _shadow_metrics["last_at"] = datetime.now(timezone.utc).isoformat()
        for snap in snapshots:
            decision = _evaluate_sr_invalidation_for_snapshot(snap, recent_signals.get(snap.get("symbol", ""), []), settings)
            if decision:
                _audit_shadow_decision(db_client, snap, decision, throttle)
                sym = snap.get("symbol", "unknown")
                with _lock:
                    _shadow_metrics["would_exit"] += 1
                    _shadow_metrics["by_symbol"][sym] = _shadow_metrics["by_symbol"].get(sym, 0) + 1
                _shadow_decisions.append({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "symbol": sym,
                    "position_id": snap.get("position_id"),
                    "side": snap.get("side"),
                    "unrealized_pnl_pct": snap.get("unrealized_pnl_pct"),
                    **decision,
                })
    except Exception as e:
        print(f"[PositionTelemetry] shadow evaluation error: {e}")

    return snapshots


def get_sr_invalidation_shadow_metrics() -> Dict[str, Any]:
    """Expose shadow metrics for /metrics/shadow-readiness."""
    with _lock:
        return {
            "name": "sr_structure_invalidation_exit",
            "mode": "shadow",
            "enabled": True,
            "evaluations": _shadow_metrics["evaluations"],
            "would_exit_total": _shadow_metrics["would_exit"],
            "would_exit_per_symbol": dict(_shadow_metrics["by_symbol"]),
            "profit_guard_actions": _shadow_metrics["profit_guard_actions"],
            "profit_guard_candidates": _shadow_metrics["profit_guard_candidates"],
            "profit_guard_hard": _shadow_metrics["profit_guard_hard"],
            "profit_guard_soft": _shadow_metrics["profit_guard_soft"],
            "profit_positions_evaluated": _shadow_metrics["profit_positions_evaluated"],
            "loss_positions_evaluated": _shadow_metrics["loss_positions_evaluated"],
            "drawdown_actions": _shadow_metrics["drawdown_actions"],
            "drawdown_cuts": _shadow_metrics["drawdown_cuts"],
            "drawdown_tightens": _shadow_metrics["drawdown_tightens"],
            "drawdown_recoverable_windows": _shadow_metrics["drawdown_recoverable_windows"],
            "drawdown_failed_recovery_windows": _shadow_metrics["drawdown_failed_recovery_windows"],
            "drawdown_shock_events": _shadow_metrics["drawdown_shock_events"],
            "drawdown_deferred_cuts": _shadow_metrics["drawdown_deferred_cuts"],
            "downstream_surfaces": list(_shadow_metrics["downstream_surfaces"]),
            "last_summary": dict(_shadow_metrics["last_summary"]),
            "last_decisions": list(_shadow_decisions)[-20:],
            "last_at": _shadow_metrics["last_at"],
        }


def position_telemetry_loop(stop_event: Optional[threading.Event], components_provider: Any, interval_seconds: float = 1.0) -> None:
    """Long-running telemetry loop."""
    while True:
        if stop_event and stop_event.is_set():
            break
        try:
            components = components_provider()
            if components:
                run_telemetry_cycle(components)
        except Exception as e:
            print(f"[PositionTelemetry] loop error: {e}")
        time.sleep(max(0.5, interval_seconds))
