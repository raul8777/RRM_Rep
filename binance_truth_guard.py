from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

RUNTIME_KEY = "binance_truth_status"
RUNTIME_KEY_SPOT = "binance_truth_status_spot"


@dataclass
class TruthPosition:
    symbol: str
    side: str
    qty: float
    entry_price: float
    opened_at: Optional[str] = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _pos_key(symbol: str, side: str) -> str:
    return f"{symbol}_{side}"


def fetch_binance_futures_positions(futures_client) -> Dict[str, TruthPosition]:
    out: Dict[str, TruthPosition] = {}
    try:
        for p in futures_client.get_positions():
            amt = _safe_float(p.get("positionAmt"), 0.0)
            if amt == 0:
                continue
            sym = str(p.get("symbol") or "").strip().upper()
            if not sym:
                continue
            side = "SHORT" if amt < 0 else "LONG"
            out[_pos_key(sym, side)] = TruthPosition(
                symbol=sym,
                side=side,
                qty=abs(amt),
                entry_price=_safe_float(p.get("entryPrice"), 0.0),
                opened_at=None,
            )
    except Exception:
        return {}
    return out


def fetch_runtime_futures_positions(futures_client) -> Dict[str, TruthPosition]:
    out: Dict[str, TruthPosition] = {}
    for k, p in getattr(futures_client, "_open_positions", {}).items():
        if getattr(p, "closed", False):
            continue
        sym = str(getattr(p, "symbol", "") or "").strip().upper()
        side = str(getattr(p, "side", "") or "").strip().upper()
        if not sym or side not in ("SHORT", "LONG"):
            continue
        opened_at = None
        try:
            oa = getattr(p, "opened_at", None)
            if oa is not None:
                opened_at = oa.isoformat() if hasattr(oa, "isoformat") else str(oa)
        except Exception:
            opened_at = None
        out[_pos_key(sym, side)] = TruthPosition(
            symbol=sym,
            side=side,
            qty=_safe_float(getattr(p, "qty", 0.0), 0.0),
            entry_price=_safe_float(getattr(p, "entry_price", 0.0), 0.0),
            opened_at=opened_at,
        )
    return out


def evaluate_futures_parity(
    futures_client,
    qty_tol: float = 1e-8,
    entry_tol_pct: float = 0.002,
) -> Dict[str, Any]:
    """Compare runtime futures positions vs Binance futures positions.

    Returns a status payload with mismatch severity and per-symbol details.
    """
    bmap = fetch_binance_futures_positions(futures_client)
    rmap = fetch_runtime_futures_positions(futures_client)

    mismatches: List[Dict[str, Any]] = []

    for k, bp in bmap.items():
        rp = rmap.get(k)
        if rp is None:
            mismatches.append({
                "code": "BINANCE_MISMATCH_RUNTIME_MISSING",
                "severity": "critical",
                "symbol": bp.symbol,
                "side": bp.side,
                "detail": "exists_in_binance_not_in_runtime",
            })
            continue
        qty_diff = abs(bp.qty - rp.qty)
        if qty_diff > qty_tol:
            mismatches.append({
                "code": "BINANCE_MISMATCH_QTY",
                "severity": "critical",
                "symbol": bp.symbol,
                "side": bp.side,
                "binance_qty": bp.qty,
                "runtime_qty": rp.qty,
                "qty_diff": qty_diff,
            })
        if bp.entry_price > 0 and rp.entry_price > 0:
            entry_gap = abs(bp.entry_price - rp.entry_price) / max(bp.entry_price, 1e-9)
            if entry_gap > entry_tol_pct:
                mismatches.append({
                    "code": "BINANCE_MISMATCH_ENTRY_PRICE",
                    "severity": "high",
                    "symbol": bp.symbol,
                    "side": bp.side,
                    "binance_entry": bp.entry_price,
                    "runtime_entry": rp.entry_price,
                    "entry_gap_pct": round(entry_gap * 100.0, 4),
                })

    for k, rp in rmap.items():
        if k not in bmap:
            mismatches.append({
                "code": "BINANCE_MISMATCH_GHOST_RUNTIME",
                "severity": "high",
                "symbol": rp.symbol,
                "side": rp.side,
                "detail": "exists_in_runtime_not_in_binance",
            })

    severity = "ok"
    if any(m.get("severity") == "critical" for m in mismatches):
        severity = "critical"
    elif mismatches:
        severity = "warning"

    return {
        "ok": severity == "ok",
        "severity": severity,
        "mismatch_count": len(mismatches),
        "binance_count": len(bmap),
        "runtime_count": len(rmap),
        "binance_keys": sorted(bmap.keys()),
        "runtime_keys": sorted(rmap.keys()),
        "mismatches": mismatches[:50],
        "ts": _utc_now_iso(),
    }


def evaluate_spot_parity(
    position_manager,
    binance_client,
    qty_tol_pct: float = 0.05,
) -> Dict[str, Any]:
    """Compare DB SPOT positions vs Binance account balances.

    Mirrors evaluate_futures_parity() for SPOT.
    Detects: ghost in DB (severity=high), orphan in Binance (severity=warning),
    qty drift >qty_tol_pct (severity=warning).
    """
    db_positions: Dict[str, Any] = {}
    try:
        all_pos = position_manager.get_all_positions() if position_manager else {}
        for sym, pos_list in all_pos.items():
            _is_futures = any(
                (getattr(p, 'metadata', None) or {}).get('trade_type') == 'futures'
                for p in pos_list
            )
            if _is_futures:
                continue
            total_qty = sum(_safe_float(getattr(p, 'quantity', 0.0)) for p in pos_list)
            if total_qty > 0:
                db_positions[sym] = total_qty
    except Exception:
        return {"ok": True, "severity": "unknown", "mismatch_count": 0,
                "error": "db_positions_unavailable", "ts": _utc_now_iso()}

    binance_balances: Dict[str, float] = {}
    try:
        raw_balances = binance_client.get_account_balance() if binance_client else {}
        for asset, qty in raw_balances.items():
            if asset == "USDT":
                continue
            sym = f"{asset}USDT"
            if _safe_float(qty) > 0:
                binance_balances[sym] = _safe_float(qty)
    except Exception:
        return {"ok": True, "severity": "unknown", "mismatch_count": 0,
                "error": "binance_balance_unavailable", "ts": _utc_now_iso()}

    mismatches: List[Dict[str, Any]] = []

    for sym, db_qty in db_positions.items():
        bn_qty = binance_balances.get(sym, 0.0)
        if bn_qty == 0.0:
            mismatches.append({
                "code": "SPOT_GHOST_IN_DB",
                "severity": "high",
                "symbol": sym,
                "db_qty": db_qty,
                "binance_qty": 0.0,
                "detail": "exists_in_db_not_in_binance",
            })
        else:
            drift = abs(db_qty - bn_qty) / max(bn_qty, 1e-12)
            if drift > qty_tol_pct:
                mismatches.append({
                    "code": "SPOT_QTY_DRIFT",
                    "severity": "warning",
                    "symbol": sym,
                    "db_qty": db_qty,
                    "binance_qty": bn_qty,
                    "drift_pct": round(drift * 100.0, 4),
                })

    for sym, bn_qty in binance_balances.items():
        if sym not in db_positions and bn_qty > 0:
            mismatches.append({
                "code": "SPOT_ORPHAN_IN_BINANCE",
                "severity": "warning",
                "symbol": sym,
                "db_qty": 0.0,
                "binance_qty": bn_qty,
                "detail": "exists_in_binance_not_in_db",
            })

    severity = "ok"
    if any(m.get("severity") == "high" for m in mismatches):
        severity = "high"
    elif mismatches:
        severity = "warning"

    return {
        "ok": severity == "ok",
        "severity": severity,
        "mismatch_count": len(mismatches),
        "db_count": len(db_positions),
        "binance_count": len(binance_balances),
        "mismatches": mismatches[:50],
        "ts": _utc_now_iso(),
    }


def auto_reconcile_spot(
    position_manager,
    binance_client,
    db_client=None,
    dry_run: bool = True,
    qty_tol_pct: float = 0.05,
) -> Dict[str, Any]:
    """Advisory reconciliation of SPOT DB/Binance discrepancies.

    Returns a report with per-mismatch recommendations. When dry_run=False,
    applies safe mutations:
      - SPOT_GHOST_IN_DB: closes the DB position as 'truth_reconcile_ghost'.
      - SPOT_QTY_DRIFT: updates the DB residual quantity to match Binance.
      - SPOT_ORPHAN_IN_BINANCE: opens a synthetic DB position with market price.
    """
    from datetime import datetime, timezone
    from position_manager import Position
    status = evaluate_spot_parity(position_manager, binance_client, qty_tol_pct)
    recommendations: List[Dict[str, Any]] = []
    applied: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    _now = _utc_now_iso()
    _price_cache: Dict[str, float] = {}

    def _market_price(symbol: str) -> float:
        if symbol in _price_cache:
            return _price_cache[symbol]
        try:
            px = binance_client.get_symbol_price(symbol) if binance_client else 0.0
        except Exception:
            px = 0.0
        _price_cache[symbol] = px
        return px

    for mm in status.get("mismatches", []):
        code = mm.get("code")
        symbol = mm.get("symbol", "UNKNOWN")
        bn_qty = _safe_float(mm.get("binance_qty"), 0.0)
        db_qty = _safe_float(mm.get("db_qty"), 0.0)
        rec = {
            "code": code,
            "symbol": symbol,
            "binance_qty": bn_qty,
            "db_qty": db_qty,
            "dry_run": dry_run,
            "ts": _now,
        }
        recommendations.append(rec)
        if dry_run:
            skipped.append({**rec, "reason": "dry_run"})
            continue

        if code == "SPOT_GHOST_IN_DB" and position_manager:
            # Close DB ghost using current market price as exit reference
            try:
                pos = position_manager.get_position(symbol)
                if pos is not None:
                    px = _market_price(symbol)
                    if px <= 0:
                        skipped.append({**rec, "reason": "no_market_price"})
                        continue
                    position_manager.close_position(symbol, px, "truth_reconcile_ghost")
                    applied.append({**rec, "action": "close_ghost", "exit_price": px})
                    continue
            except Exception as e:
                skipped.append({**rec, "reason": f"close_error: {e}"})
                continue

        elif code == "SPOT_QTY_DRIFT" and position_manager:
            # Adjust residual to match Binance (trim or expand with synthetic average)
            try:
                pos_list = position_manager.get_all_positions().get(symbol)
                if not pos_list:
                    skipped.append({**rec, "reason": "no_positions"})
                    continue
                target_qty = bn_qty
                current_total = sum(_safe_float(getattr(p, "quantity", 0.0)) for p in pos_list)
                if current_total <= 0:
                    skipped.append({**rec, "reason": "zero_db_qty"})
                    continue
                if target_qty < current_total:
                    # Trim oldest entries first
                    remaining = current_total - target_qty
                    for p in pos_list:
                        if remaining <= 0:
                            break
                        pq = _safe_float(getattr(p, "quantity", 0.0))
                        trim = min(pq, remaining)
                        if trim >= pq:
                            position_manager.close_position(symbol, _market_price(symbol), "truth_reconcile_drift", qty_closed=pq)
                        else:
                            position_manager.close_position(symbol, _market_price(symbol), "truth_reconcile_drift", qty_closed=trim)
                        remaining -= trim
                    applied.append({**rec, "action": "trim_to_binance", "target_qty": target_qty})
                elif target_qty > current_total:
                    # Expand with synthetic position at market price
                    px = _market_price(symbol)
                    position_manager.add_position(
                        Position(
                            symbol=symbol,
                            side="BUY",
                            quantity=target_qty - current_total,
                            entry_price=px,
                            entry_time=datetime.now(timezone.utc).isoformat(),
                        ),
                        metadata={"source": "truth_reconcile_drift", "synthetic": True},
                    )
                    applied.append({**rec, "action": "expand_to_binance", "added_qty": target_qty - current_total, "entry_price": px})
                else:
                    applied.append({**rec, "action": "no_change"})
                continue
            except Exception as e:
                skipped.append({**rec, "reason": f"drift_error: {e}"})
                continue

        elif code == "SPOT_ORPHAN_IN_BINANCE" and position_manager:
            # Import orphan at current market price
            px = _market_price(symbol)
            if px <= 0:
                skipped.append({**rec, "reason": "no_market_price"})
                continue
            try:
                position_manager.add_position(
                    Position(
                        symbol=symbol,
                        side="BUY",
                        quantity=bn_qty,
                        entry_price=px,
                        entry_time=datetime.now(timezone.utc).isoformat(),
                    ),
                    metadata={"source": "truth_reconcile_orphan", "synthetic": True},
                )
                applied.append({**rec, "action": "import_orphan", "entry_price": px})
                continue
            except Exception as e:
                skipped.append({**rec, "reason": f"import_error: {e}"})
                continue

        skipped.append({**rec, "reason": "unhandled"})

    report = {
        "ok": status.get("ok", True),
        "severity": status.get("severity", "ok"),
        "mismatch_count": status.get("mismatch_count", 0),
        "dry_run": dry_run,
        "recommendations": recommendations,
        "applied": applied,
        "skipped": skipped,
        "ts": _now,
    }
    if db_client:
        try:
            persist_truth_status_spot(db_client, report, source="auto_reconcile_spot")
        except Exception:
            pass
    return report


def persist_truth_status_spot(db_client, status: Dict[str, Any], source: str) -> None:
    if not db_client:
        return
    payload = dict(status or {})
    payload["source"] = source
    payload["updated_at"] = _utc_now_iso()
    raw = json.dumps(payload, default=str)
    db_client.execute_update(
        "INSERT INTO trading_runtime_state (key, value, updated_at) "
        "VALUES (%s, %s, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
        (RUNTIME_KEY_SPOT, raw, raw),
    )


def get_truth_status_spot(db_client) -> Dict[str, Any]:
    if not db_client:
        return {"ok": True, "severity": "unknown", "mismatch_count": 0}
    try:
        rows = db_client.execute_query(
            "SELECT value, updated_at FROM trading_runtime_state WHERE key=%s LIMIT 1",
            (RUNTIME_KEY_SPOT,),
        )
        if not rows:
            return {"ok": True, "severity": "unknown", "mismatch_count": 0}
        raw = rows[0].get("value")
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        if not isinstance(data, dict):
            data = {"severity": "unknown", "mismatch_count": 0}
        ts = rows[0].get("updated_at")
        data["updated_at"] = ts.isoformat() if ts and hasattr(ts, "isoformat") else data.get("updated_at")
        return data
    except Exception:
        return {"ok": True, "severity": "unknown", "mismatch_count": 0}


def auto_reconcile_futures(
    futures_client,
    db_client=None,
    dry_run: bool = True,
    qty_tol: float = 1e-8,
    entry_tol_pct: float = 0.002,
) -> Dict[str, Any]:
    """Advisory reconciliation of FUTURES runtime/Binance discrepancies.

    Uses the existing parity evaluation. When dry_run=False, applies safe mutations:
      - BINANCE_MISMATCH_RUNTIME_MISSING: import position from Binance into runtime.
      - BINANCE_MISMATCH_GHOST_RUNTIME: mark runtime position as closed.
      - BINANCE_MISMATCH_QTY: update runtime qty to Binance qty.
      - BINANCE_MISMATCH_ENTRY_PRICE: update runtime entry_price to Binance entry.
    """
    status = evaluate_futures_parity(futures_client, qty_tol=qty_tol, entry_tol_pct=entry_tol_pct)
    recommendations: List[Dict[str, Any]] = []
    applied: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    _now = _utc_now_iso()

    for mm in status.get("mismatches", []):
        code = mm.get("code")
        symbol = mm.get("symbol", "UNKNOWN")
        side = mm.get("side", "UNKNOWN")
        key = _pos_key(symbol, side)
        rec = {**mm, "dry_run": dry_run, "ts": _now}
        recommendations.append(rec)
        if dry_run:
            skipped.append({**rec, "reason": "dry_run"})
            continue

        if code == "BINANCE_MISMATCH_RUNTIME_MISSING":
            # Import missing futures position from Binance
            try:
                _pos = None
                for p in futures_client.get_positions():
                    if str(p.get("symbol") or "").strip().upper() == symbol:
                        _amt = _safe_float(p.get("positionAmt"), 0.0)
                        _side = "SHORT" if _amt < 0 else "LONG"
                        if _side == side:
                            _pos = p
                            break
                if _pos is None:
                    skipped.append({**rec, "reason": "binance_position_not_found"})
                    continue
                _qty = abs(_safe_float(_pos.get("positionAmt"), 0.0))
                _entry = _safe_float(_pos.get("entryPrice"), 0.0)
                _sl = _safe_float(_pos.get("stopLossPrice"), 0.0) or _entry
                _tp = _safe_float(_pos.get("takeProfitPrice"), 0.0) or _entry
                from futures_client import FuturesPosition
                _new_pos = FuturesPosition(
                    symbol=symbol, side=side, qty=_qty, entry_price=_entry,
                    sl_price=_sl, tp_price=_tp,
                    order_id=f"RECONCILE_{_now}",
                    metadata={"source": "truth_reconcile_import", "synthetic": True},
                )
                futures_client._open_positions[key] = _new_pos
                if hasattr(futures_client, "_save_futures_open"):
                    futures_client._save_futures_open(_new_pos)
                applied.append({**rec, "action": "import", "qty": _qty, "entry_price": _entry})
                continue
            except Exception as e:
                skipped.append({**rec, "reason": f"import_error: {e}"})
                continue

        elif code == "BINANCE_MISMATCH_GHOST_RUNTIME":
            # Mark runtime-only position as closed/ghost
            try:
                _pos = getattr(futures_client, "_open_positions", {}).get(key)
                if _pos is None:
                    skipped.append({**rec, "reason": "runtime_position_not_found"})
                    continue
                _pos.closed = True
                _pos.pnl = 0.0
                if db_client and hasattr(futures_client, "_save_futures_closed"):
                    try:
                        futures_client._save_futures_closed(_pos, "truth_reconcile_ghost")
                    except Exception:
                        pass
                applied.append({**rec, "action": "mark_ghost"})
                continue
            except Exception as e:
                skipped.append({**rec, "reason": f"ghost_error: {e}"})
                continue

        elif code == "BINANCE_MISMATCH_QTY":
            # Adjust runtime qty to match Binance
            try:
                _pos = getattr(futures_client, "_open_positions", {}).get(key)
                if _pos is None:
                    skipped.append({**rec, "reason": "runtime_position_not_found"})
                    continue
                _pos.qty = _safe_float(mm.get("binance_qty"), _pos.qty)
                applied.append({**rec, "action": "adjust_qty", "new_qty": _pos.qty})
                continue
            except Exception as e:
                skipped.append({**rec, "reason": f"qty_error: {e}"})
                continue

        elif code == "BINANCE_MISMATCH_ENTRY_PRICE":
            # Adjust runtime entry_price to Binance
            try:
                _pos = getattr(futures_client, "_open_positions", {}).get(key)
                if _pos is None:
                    skipped.append({**rec, "reason": "runtime_position_not_found"})
                    continue
                _pos.entry_price = _safe_float(mm.get("binance_entry"), _pos.entry_price)
                applied.append({**rec, "action": "adjust_entry", "new_entry": _pos.entry_price})
                continue
            except Exception as e:
                skipped.append({**rec, "reason": f"entry_error: {e}"})
                continue

        skipped.append({**rec, "reason": "unhandled"})

    report = {
        "ok": status.get("ok", True),
        "severity": status.get("severity", "ok"),
        "mismatch_count": status.get("mismatch_count", 0),
        "dry_run": dry_run,
        "recommendations": recommendations,
        "applied": applied,
        "skipped": skipped,
        "ts": _now,
    }
    if db_client:
        try:
            persist_truth_status(db_client, report, source="auto_reconcile_futures")
        except Exception:
            pass
    return report


def persist_truth_status(db_client, status: Dict[str, Any], source: str) -> None:
    if not db_client:
        return
    payload = dict(status or {})
    payload["source"] = source
    payload["updated_at"] = _utc_now_iso()
    raw = json.dumps(payload, default=str)
    db_client.execute_update(
        "INSERT INTO trading_runtime_state (key, value, updated_at) "
        "VALUES (%s, %s, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
        (RUNTIME_KEY, raw, raw),
    )


def audit_truth_mismatches(db_client, status: Dict[str, Any], source: str) -> None:
    if not db_client:
        return
    mismatches = (status or {}).get("mismatches") or []
    for mm in mismatches[:20]:
        code = str(mm.get("code") or "BINANCE_MISMATCH_UNKNOWN")
        sym = str(mm.get("symbol") or "SYSTEM")
        detail = {
            "source": source,
            "severity": mm.get("severity"),
            "payload": mm,
            "status_severity": (status or {}).get("severity"),
            "status_ts": (status or {}).get("ts"),
        }
        try:
            db_client.execute_update(
                "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
                "VALUES (%s, %s, %s, %s, %s)",
                (sym, "system", code, json.dumps(detail, default=str), False),
            )
        except Exception:
            # Never break cycle on audit persistence
            pass


def get_truth_status(db_client) -> Dict[str, Any]:
    if not db_client:
        return {"ok": True, "severity": "unknown", "mismatch_count": 0}
    try:
        rows = db_client.execute_query(
            "SELECT value, updated_at FROM trading_runtime_state WHERE key=%s LIMIT 1",
            (RUNTIME_KEY,),
        )
        if not rows:
            return {"ok": True, "severity": "unknown", "mismatch_count": 0}
        raw = rows[0].get("value")
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        if not isinstance(data, dict):
            data = {"severity": "unknown", "mismatch_count": 0}
        ts = rows[0].get("updated_at")
        data["updated_at"] = ts.isoformat() if ts and hasattr(ts, "isoformat") else data.get("updated_at")
        return data
    except Exception:
        return {"ok": True, "severity": "unknown", "mismatch_count": 0}


def get_truth_status_futures(db_client) -> Dict[str, Any]:
    """Alias for get_truth_status; futures reconciliation uses RUNTIME_KEY."""
    return get_truth_status(db_client)


def is_truth_critical(db_client, max_age_seconds: int = 900) -> bool:
    status = get_truth_status(db_client)
    sev = str(status.get("severity") or "unknown").lower()
    if sev != "critical":
        return False
    uat = status.get("updated_at") or status.get("ts")
    if not uat:
        return True
    try:
        dt = datetime.fromisoformat(str(uat).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        return age <= max_age_seconds
    except Exception:
        return True


def compute_basket_reset_decision(
    positions: List[Dict[str, Any]],
    pnl_threshold_usd: float = 10.0,
    min_avg_age_hours: float = 4.0,
    momentum_positive_count_limit: int = 1,
) -> Dict[str, Any]:
    """Return reset recommendation for open basket.

    Positions may contain: symbol, unrealized_pnl, hours_held, pnl_pct, momentum_score.
    """
    if not positions:
        return {
            "trigger": False,
            "reason": "no_open_positions",
            "combined_pnl_usd": 0.0,
            "avg_age_hours": 0.0,
            "n_positions": 0,
        }

    combined = 0.0
    age_sum = 0.0
    momentum_positive = 0

    for p in positions:
        combined += _safe_float(p.get("unrealized_pnl"), 0.0)
        age_sum += _safe_float(p.get("hours_held"), 0.0)
        pnl_pct = _safe_float(p.get("pnl_pct"), 0.0)
        mscore = _safe_float(p.get("momentum_score"), 0.0)
        if pnl_pct > 0.002 and mscore > 0:
            momentum_positive += 1

    n = len(positions)
    avg_age = age_sum / max(n, 1)
    trigger = (
        combined <= pnl_threshold_usd
        and avg_age >= min_avg_age_hours
        and momentum_positive <= momentum_positive_count_limit
    )

    return {
        "trigger": bool(trigger),
        "reason": (
            "basket_underperforming_reset"
            if trigger
            else "basket_hold"
        ),
        "combined_pnl_usd": round(combined, 4),
        "avg_age_hours": round(avg_age, 4),
        "n_positions": n,
        "momentum_positive_positions": momentum_positive,
        "thresholds": {
            "pnl_threshold_usd": pnl_threshold_usd,
            "min_avg_age_hours": min_avg_age_hours,
            "momentum_positive_count_limit": momentum_positive_count_limit,
        },
        "ts": _utc_now_iso(),
    }
