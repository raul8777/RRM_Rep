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
    "last_at": None,
}


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _compute_unrealized(entry: float, qty: float, price: float, side: str, leverage: float = 1.0) -> float:
    if entry <= 0 or qty <= 0 or price <= 0:
        return 0.0
    if side.upper() == "LONG":
        return (price - entry) / entry * qty * price * leverage
    return (entry - price) / entry * qty * price * leverage


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
    for snapshot in snapshots:
        guard = _compute_profit_guard_target(snapshot, settings)
        if not guard:
            continue

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
    qty = abs(float(pos.quantity or 0.0))
    side = str(pos.side).upper()
    leverage = float(getattr(pos, "leverage", 10) or 10)
    unrealized_pct = _compute_unrealized_pct(entry, price, side)
    unrealized_usdt = _compute_unrealized(entry, qty, price, side, leverage=leverage)
    sltp = _extract_sl_tp(getattr(pos, "_smart_meta", None) or {}, pos.sl_price or 0.0, pos.tp_price or 0.0)
    smart = getattr(pos, "_smart_meta", {}) or {}
    metrics = _fetch_futures_metrics(futures_client, pos.symbol)
    return {
        "trade_type": "futures",
        "symbol": pos.symbol,
        "side": side,
        "position_id": str(getattr(pos, "position_id", "") or ""),
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

        # FUTURES positions
        if futures_client:
            try:
                fut_positions = list(futures_client._open_positions.items())
            except Exception:
                fut_positions = []
            for key, pos in fut_positions:
                if getattr(pos, "closed", False):
                    continue
                try:
                    price = float(futures_client.get_price(pos.symbol))
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
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    # Dynamic 1s profit-follow guard across all modes (SPOT + FUTURES).
    guard_actions = _run_profit_guard_cycle(components, snapshots, settings)
    if guard_actions:
        summary["profit_guard_actions"] = len(guard_actions)
        summary["profit_guard_hard"] = sum(1 for a in guard_actions if a.get("lock_mode") == "hard")
        summary["profit_guard_soft"] = sum(1 for a in guard_actions if a.get("lock_mode") == "soft")
        summary["profit_guard_symbols"] = sorted({str(a.get("symbol") or "") for a in guard_actions if a.get("symbol")})

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
