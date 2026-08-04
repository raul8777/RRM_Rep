from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    settings=None,
    qty_tol_pct: float = 0.05,
    min_notional_usdt: float = 5.0,
) -> Dict[str, Any]:
    """Compare DB SPOT positions vs Binance account balances.

    Mirrors evaluate_futures_parity() for SPOT.
    Detects: ghost in DB (severity=high), orphan in Binance (severity=warning),
    qty drift >qty_tol_pct (severity=warning).

    Orphan balances below min_notional_usdt are ignored as dust and reported
    only in the dust_skipped counter, reducing noise from airdrops/sweep dust.
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
    _keep_bnb_for_commission = bool(getattr(settings, 'use_bnb_for_commission', True)) or bool(
        getattr(settings, 'auto_bnb_for_commission', True)
    )
    _ignore_bnb_in_parity = bool(getattr(settings, 'spot_ignore_bnb_when_commission_enabled', True))
    _skip_bnb = _keep_bnb_for_commission and _ignore_bnb_in_parity
    # Assets excluded from parity: not tradable/dust tokens and stablecoins handled separately
    _excluded_assets = {"USDT", "BTTC", "WIN", "JST", "IQ"}
    try:
        raw_balances = binance_client.get_account_balance() if binance_client else {}
        for asset, qty in raw_balances.items():
            if asset in _excluded_assets:
                continue
            if asset == "BNB" and _skip_bnb:
                continue
            sym = f"{asset}USDT"
            if _safe_float(qty) > 0:
                binance_balances[sym] = _safe_float(qty)
    except Exception:
        return {"ok": True, "severity": "unknown", "mismatch_count": 0,
                "error": "binance_balance_unavailable", "ts": _utc_now_iso()}

    mismatches: List[Dict[str, Any]] = []
    dust_skipped: List[Dict[str, Any]] = []

    # Single bulk price fetch to evaluate materiality of orphans/drift.
    _prices: Dict[str, float] = {}
    try:
        if binance_client and hasattr(binance_client, "client") and binance_client.client:
            _tickers = binance_client.client.get_symbol_ticker() or []
            for t in _tickers:
                _s = str(t.get("symbol") or "").upper()
                if _s:
                    _prices[_s] = _safe_float(t.get("price"), 0.0)
    except Exception:
        _prices = {}

    def _notional(sym: str, qty: float) -> float:
        if qty <= 0:
            return 0.0
        px = _prices.get(sym)
        if not px and binance_client:
            try:
                px = _safe_float(binance_client.get_symbol_price(sym), 0.0)
            except Exception:
                px = 0.0
        return qty * _safe_float(px, 0.0)

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
                _drift_notional = _notional(sym, abs(db_qty - bn_qty))
                # Only flag drift if it is material; otherwise treat as rounding dust.
                if _drift_notional >= min_notional_usdt:
                    mismatches.append({
                        "code": "SPOT_QTY_DRIFT",
                        "severity": "warning",
                        "symbol": sym,
                        "db_qty": db_qty,
                        "binance_qty": bn_qty,
                        "drift_pct": round(drift * 100.0, 4),
                        "drift_notional_usdt": round(_drift_notional, 4),
                    })

    for sym, bn_qty in binance_balances.items():
        if sym not in db_positions and bn_qty > 0:
            _bn_notional = _notional(sym, bn_qty)
            if _bn_notional < min_notional_usdt:
                dust_skipped.append({
                    "symbol": sym,
                    "binance_qty": bn_qty,
                    "notional_usdt": round(_bn_notional, 4),
                })
                continue
            mismatches.append({
                "code": "SPOT_ORPHAN_IN_BINANCE",
                "severity": "warning",
                "symbol": sym,
                "db_qty": 0.0,
                "binance_qty": bn_qty,
                "notional_usdt": round(_bn_notional, 4),
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
        "excluded_assets": ["BNB"] if _skip_bnb else [],
        "dust_skipped_count": len(dust_skipped),
        "dust_skipped": dust_skipped[:50],
        "mismatches": mismatches[:50],
        "ts": _utc_now_iso(),
    }


def auto_reconcile_spot(
    position_manager,
    binance_client,
    db_client=None,
    settings=None,
    dry_run: bool = True,
    qty_tol_pct: float = 0.05,
    min_notional_usdt: float = 1.0,
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
    status = evaluate_spot_parity(
        position_manager, binance_client, settings=settings, qty_tol_pct=qty_tol_pct,
        min_notional_usdt=min_notional_usdt,
    )
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
                            metadata={"source": "truth_reconcile_drift", "synthetic": True},
                        )
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
                        metadata={"source": "truth_reconcile_orphan", "synthetic": True},
                    )
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
    auto_fix_codes: Optional[set] = None,
) -> Dict[str, Any]:
    """Advisory reconciliation of FUTURES runtime/Binance discrepancies.

    Uses the existing parity evaluation. When dry_run=False, applies safe mutations:
      - BINANCE_MISMATCH_RUNTIME_MISSING: import position from Binance into runtime.
      - BINANCE_MISMATCH_GHOST_RUNTIME: mark runtime position as closed.
      - BINANCE_MISMATCH_QTY: update runtime qty to Binance qty.
      - BINANCE_MISMATCH_ENTRY_PRICE: update runtime entry_price to Binance entry.

    s109: `auto_fix_codes` lets a caller stay in `dry_run=True` overall (safe
    default) while still allowing specific, low-risk mismatch codes to be
    auto-applied — e.g. `{"BINANCE_MISMATCH_RUNTIME_MISSING"}`, which is
    purely additive (importing a position that genuinely exists on Binance
    can never close/modify anything, unlike `BINANCE_MISMATCH_GHOST_RUNTIME`
    which marks a runtime position as closed and deserves more caution).
    """
    status = evaluate_futures_parity(futures_client, qty_tol=qty_tol, entry_tol_pct=entry_tol_pct)
    recommendations: List[Dict[str, Any]] = []
    applied: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    _now = _utc_now_iso()
    _auto_fix_codes = auto_fix_codes or set()

    for mm in status.get("mismatches", []):
        code = mm.get("code")
        symbol = mm.get("symbol", "UNKNOWN")
        side = mm.get("side", "UNKNOWN")
        key = _pos_key(symbol, side)
        _code_dry_run = dry_run and code not in _auto_fix_codes
        rec = {**mm, "dry_run": _code_dry_run, "ts": _now}
        recommendations.append(rec)
        if _code_dry_run:
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
                # s114: `/fapi/v2/positionRisk` (what `get_positions()` returns) has no
                # `stopLossPrice`/`takeProfitPrice` fields — reading them here always
                # returned 0.0, and `0.0 or _entry` silently collapsed both SL and TP to
                # the entry price. The very next price tick then satisfied either
                # `is_sl_hit()` or `is_tp_hit()` trivially, closing the "reconciled"
                # position within seconds (TAOUSDT/ONDOUSDT/WIFUSDT/DOGEUSDT/EDENUSDT —
                # some even closed at a LOSS while labeled `take_profit`). Mirrors
                # `_sync_open_positions`'s already-correct recovery: sane entry-based
                # defaults (3%/6%, same as that function), overridden by the real
                # STOP_MARKET/TAKE_PROFIT_MARKET algo order trigger price when one
                # exists. See calibrar-ciclo-vida-52fe5d.md s114.
                # Entry-based defaults (3%/6%, same ratios as `_sync_open_positions`) —
                # plain arithmetic, deliberately not tick-aligned here: the goal is a
                # sane, never-collapsed-to-entry starting point, not exchange-precise
                # rounding (the real algo order trigger price, when found below, is
                # already exchange-aligned by construction).
                if side == "SHORT":
                    _sl = _entry * 1.03
                    _tp = _entry * 0.94
                else:
                    _sl = _entry * 0.97
                    _tp = _entry * 1.06
                _sl_order_id = None
                _tp_order_id = None
                try:
                    _algo_resp = futures_client._get("/fapi/v1/openAlgoOrders", signed=True)
                    _algo_list = _algo_resp if isinstance(_algo_resp, list) else _algo_resp.get("orders", _algo_resp.get("algoOrders", []))
                    for _ao in _algo_list:
                        if str(_ao.get("symbol") or "").strip().upper() != symbol:
                            continue
                        _ao_pos_side = _ao.get("positionSide", "BOTH")
                        if not (_ao_pos_side == side or _ao_pos_side == "BOTH"):
                            continue
                        _ao_type = _ao.get("type") or _ao.get("orderType", "")
                        _trigger = _safe_float(_ao.get("triggerPrice"), 0.0)
                        if _trigger <= 0:
                            continue
                        if _ao_type == "STOP_MARKET":
                            _sl = _trigger
                            _sl_order_id = str(_ao.get("algoId", _ao.get("orderId", "")))
                        elif _ao_type == "TAKE_PROFIT_MARKET":
                            _tp = _trigger
                            _tp_order_id = str(_ao.get("algoId", _ao.get("orderId", "")))
                except Exception as _algo_err:
                    print(f"[TruthGuard] {symbol}: openAlgoOrders lookup failed ({_algo_err}), using entry-based SL/TP defaults")
                # Defensive guard: never persist a position where SL/TP collapsed onto
                # the entry price (or onto each other) — that is the exact signature of
                # the bug this fix addresses, not a legitimate zero-risk position.
                if _sl is None or _tp is None or _entry <= 0 or abs(_sl - _entry) < 1e-12 or abs(_tp - _entry) < 1e-12 or abs(_sl - _tp) < 1e-12:
                    skipped.append({**rec, "reason": "sl_tp_collapsed_to_entry_guard"})
                    continue
                # s118: cleanup stale synthetic rows before importing the current
                # Binance position, so partial closes do not accumulate duplicate
                # trades_open rows for the same underlying position.
                if db_client:
                    try:
                        _db_side = "SELL" if side == "SHORT" else "BUY"
                        db_client.execute_update(
                            """
                            DELETE FROM trades_open
                            WHERE symbol = %s AND side = %s
                              AND metadata->>'trade_type' = 'futures'
                              AND metadata->>'source' = 'truth_reconcile_import'
                            """,
                            (symbol, _db_side),
                        )
                    except Exception as _cleanup_err:
                        print(f"[TruthGuard] {symbol}: failed to cleanup stale synthetic rows ({_cleanup_err})")
                from futures_client import FuturesPosition
                _new_pos = FuturesPosition(
                    symbol=symbol, side=side, qty=_qty, entry_price=_entry,
                    sl_price=_sl, tp_price=_tp,
                    order_id=f"RECONCILE_{_now}",
                    sl_order_id=_sl_order_id, tp_order_id=_tp_order_id,
                    _smart_meta={"source": "truth_reconcile_import", "synthetic": True, "learning_eligible": False},
                )
                futures_client._open_positions[key] = _new_pos
                if hasattr(futures_client, "_save_futures_open"):
                    futures_client._save_futures_open(_new_pos)
                applied.append({**rec, "action": "import", "qty": _qty, "entry_price": _entry, "sl_price": _sl, "tp_price": _tp})
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
                _pos.pnl = 0.0
                _persisted = False
                if db_client and hasattr(futures_client, "_save_futures_closed"):
                    try:
                        _persisted = futures_client._save_futures_closed(_pos, "truth_reconcile_ghost")
                    except Exception:
                        _persisted = False
                if _persisted:
                    _pos.closed = True
                    applied.append({**rec, "action": "mark_ghost"})
                else:
                    skipped.append({**rec, "reason": "persist_failed"})
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


# ── Phase 2: Closed-positions parity audit (report-only) ─────────────────────


def _parse_iso_ts(v: Any) -> Optional[datetime]:
    try:
        s = str(v).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _match_db_trade_to_binance(
    db_trade: Dict[str, Any],
    binance_trades: List[Dict[str, Any]],
    qty_tol_pct: float = 0.05,
    time_tol_minutes: int = 5,
) -> Optional[Dict[str, Any]]:
    """Return the best-matching Binance trade for a DB closed position.

    Matching uses quantity first, then time proximity.  Binance trades must
    already be filtered to the correct side/close direction.
    """
    db_exit = _parse_iso_ts(db_trade.get("exit_time"))
    db_qty = _safe_float(db_trade.get("quantity"), 0.0)
    if db_qty <= 0:
        return None

    best: Optional[Dict[str, Any]] = None
    best_score = float("inf")
    for bt in binance_trades:
        bn_qty = _safe_float(bt.get("qty"), 0.0)
        if bn_qty <= 0:
            continue
        qty_ratio = min(db_qty, bn_qty) / max(db_qty, bn_qty)
        if qty_ratio < (1.0 - qty_tol_pct):
            continue

        bn_ts = bt.get("time_ms") or bt.get("time")
        if bn_ts:
            try:
                bn_dt = datetime.fromtimestamp(int(bn_ts) / 1000.0, tz=timezone.utc)
            except Exception:
                bn_dt = None
        else:
            bn_dt = None

        time_min = float("inf")
        if db_exit and bn_dt:
            time_min = abs((db_exit - bn_dt).total_seconds()) / 60.0
            if time_min > time_tol_minutes:
                continue

        # Score: prefer exact qty match, then time proximity
        score = (1.0 - qty_ratio) * 1000.0 + time_min
        if score < best_score:
            best_score = score
            best = bt

    return best


def _fetch_db_closed_positions(
    db_client,
    since: datetime,
    limit: int = 10000,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (spot_closed, futures_closed) from trades_closed since ``since``."""
    if not db_client:
        return [], []
    try:
        query = """
            SELECT symbol, side, quantity, entry_price, exit_price,
                   entry_time, exit_time, pnl, pnl_percentage, close_reason,
                   metadata, entry_commission, exit_commission
            FROM trades_closed
            WHERE exit_time >= %s
              AND COALESCE(is_corrupted, FALSE) = FALSE
            ORDER BY exit_time DESC
            LIMIT %s
        """
        rows = db_client.execute_query(query, (since.isoformat(), limit))
    except Exception as e:
        return [], [{"error": str(e)}]

    spot_closed: List[Dict[str, Any]] = []
    futures_closed: List[Dict[str, Any]] = []
    for row in rows:
        meta = row.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        trade_type = str(meta.get("trade_type") or "").lower()
        rec = {
            "symbol": str(row.get("symbol") or "").upper(),
            "side": str(row.get("side") or "").upper(),
            "quantity": _safe_float(row.get("quantity"), 0.0),
            "entry_price": _safe_float(row.get("entry_price"), 0.0),
            "exit_price": _safe_float(row.get("exit_price"), 0.0),
            "entry_time": row.get("entry_time"),
            "exit_time": row.get("exit_time"),
            "pnl": _safe_float(row.get("pnl"), 0.0),
            "pnl_percentage": _safe_float(row.get("pnl_percentage"), 0.0),
            "close_reason": str(row.get("close_reason") or ""),
            "entry_commission": _safe_float(row.get("entry_commission"), 0.0),
            "exit_commission": _safe_float(row.get("exit_commission"), 0.0),
            "metadata": meta,
        }
        if trade_type == "futures":
            futures_closed.append(rec)
        else:
            spot_closed.append(rec)
    return spot_closed, futures_closed


def _fetch_binance_spot_closing_trades(
    binance_client,
    symbols: List[str],
    cutoff_ms: int,
    limit: int = 100,
) -> Dict[str, List[Dict[str, Any]]]:
    """Fetch recent SPOT myTrades per symbol and keep only SELL trades after cutoff."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    if not binance_client or not symbols:
        return out
    for sym in symbols:
        try:
            raw = binance_client.get_my_trades(symbol=sym, limit=limit) or []
        except Exception as e:
            out[sym] = [{"error": str(e)}]
            continue
        sells = []
        for t in raw:
            ts = int(t.get("time", 0))
            if ts < cutoff_ms:
                continue
            is_buyer = bool(t.get("isBuyer", True))
            if is_buyer:
                continue  # only SELL closes a long SPOT position
            qty = _safe_float(t.get("qty"), 0.0) or _safe_float(t.get("quantity"), 0.0)
            price = _safe_float(t.get("price"), 0.0)
            comm = _safe_float(t.get("commission"), 0.0)
            c_asset = t.get("commissionAsset", "USDT")
            if c_asset not in ("USDT", "BNB") and price > 0:
                comm = comm * price
            sells.append({
                "symbol": sym,
                "qty": qty,
                "price": price,
                "commission": comm,
                "commission_asset": c_asset,
                "time_ms": ts,
                "order_id": str(t.get("orderId") or ""),
                "trade_id": str(t.get("id") or ""),
            })
        out[sym] = sells
    return out


def _fetch_binance_futures_closing_trades(
    futures_client,
    symbols: List[str],
    days: int = 30,
) -> Dict[str, List[Dict[str, Any]]]:
    """Fetch recent FUTURES closing trades per symbol (realizedPnl != 0)."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    if not futures_client or not symbols:
        return out
    try:
        trades = futures_client.get_recent_trades(days=days, extra_symbols=symbols) or []
    except Exception as e:
        return {"_error": [{"error": str(e)}]}

    for t in trades:
        sym = str(t.get("symbol") or "").upper()
        if sym not in out:
            out[sym] = []
        out[sym].append(t)
    return out


def evaluate_closed_positions_parity(
    position_manager,
    futures_client,
    binance_client,
    days: int = 30,
    qty_tol_pct: float = 0.05,
    pnl_tol_usdt: float = 1.0,
    commission_tol_usdt: float = 1.0,
    time_tol_minutes: int = 5,
) -> Dict[str, Any]:
    """Compare ``trades_closed`` against Binance executed closing trades.

    This is a **report-only** audit.  It detects:
      - DB_CLOSED_MISSING_IN_BINANCE: closed position in DB with no Binance trade.
      - BINANCE_CLOSED_MISSING_IN_DB: Binance closing trade without a DB record.
      - CLOSED_QTY_MISMATCH: quantity differs beyond tolerance.
      - CLOSED_PNL_MISMATCH: futures realized PnL differs beyond tolerance.
      - CLOSED_COMMISSION_MISMATCH: spot exit commission differs beyond tolerance.
      - CLOSED_PRICE_MISMATCH: spot exit price differs beyond tolerance.

    Returns a dict with totals, per-symbol summaries, and a mismatch list.
    """
    db_client = getattr(position_manager, "db_client", None) if position_manager else None
    if not db_client:
        return {
            "ok": True, "severity": "unknown", "mismatch_count": 0,
            "error": "db_client_unavailable", "ts": _utc_now_iso(),
        }

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_ms = int(cutoff.timestamp() * 1000)

    spot_db, futures_db = _fetch_db_closed_positions(db_client, cutoff)

    spot_symbols = sorted({r["symbol"] for r in spot_db})
    futures_symbols = sorted({r["symbol"] for r in futures_db})

    spot_bn = _fetch_binance_spot_closing_trades(binance_client, spot_symbols, cutoff_ms)
    futures_bn = _fetch_binance_futures_closing_trades(futures_client, futures_symbols, days)

    mismatches: List[Dict[str, Any]] = []
    matched_spot: List[Dict[str, Any]] = []
    matched_futures: List[Dict[str, Any]] = []

    # ── SPOT reconciliation ───────────────────────────────────────────────────
    for rec in spot_db:
        sym = rec["symbol"]
        sells = spot_bn.get(sym, [])
        if sells and "error" in sells[0]:
            mismatches.append({
                "code": "BINANCE_SPOT_TRADES_UNAVAILABLE",
                "severity": "warning",
                "symbol": sym,
                "detail": sells[0].get("error"),
            })
            continue

        match = _match_db_trade_to_binance(rec, sells, qty_tol_pct, time_tol_minutes)
        if not match:
            mismatches.append({
                "code": "DB_CLOSED_MISSING_IN_BINANCE",
                "severity": "high",
                "symbol": sym,
                "trade_type": "spot",
                "db_qty": rec["quantity"],
                "db_exit_time": rec["exit_time"],
                "db_close_reason": rec["close_reason"],
                "detail": "no_matching_sell_trade_on_binance",
            })
            continue

        matched_spot.append({
            "symbol": sym,
            "db_qty": rec["quantity"],
            "binance_qty": match["qty"],
            "db_exit_price": rec["exit_price"],
            "binance_exit_price": match["price"],
            "db_exit_commission": rec["exit_commission"],
            "binance_commission": match["commission"],
            "db_pnl": rec["pnl"],
        })

        qty_diff = abs(rec["quantity"] - match["qty"])
        if qty_diff / max(rec["quantity"], 1e-12) > qty_tol_pct:
            mismatches.append({
                "code": "CLOSED_QTY_MISMATCH",
                "severity": "high",
                "symbol": sym,
                "trade_type": "spot",
                "db_qty": rec["quantity"],
                "binance_qty": match["qty"],
                "qty_diff": round(qty_diff, 8),
            })

        if rec["exit_price"] > 0 and match["price"] > 0:
            price_gap = abs(rec["exit_price"] - match["price"]) / max(rec["exit_price"], 1e-12)
            if price_gap > 0.002:
                mismatches.append({
                    "code": "CLOSED_PRICE_MISMATCH",
                    "severity": "warning",
                    "symbol": sym,
                    "trade_type": "spot",
                    "db_exit_price": rec["exit_price"],
                    "binance_exit_price": match["price"],
                    "price_gap_pct": round(price_gap * 100.0, 4),
                })

        comm_diff = abs(rec["exit_commission"] - match["commission"])
        if comm_diff > commission_tol_usdt:
            mismatches.append({
                "code": "CLOSED_COMMISSION_MISMATCH",
                "severity": "warning",
                "symbol": sym,
                "trade_type": "spot",
                "db_exit_commission": rec["exit_commission"],
                "binance_commission": match["commission"],
                "commission_diff": round(comm_diff, 8),
            })

    # Detect Binance SPOT sells not present in DB
    db_spot_keys = {(r["symbol"], _parse_iso_ts(r["exit_time"])) for r in spot_db}
    for sym, sells in spot_bn.items():
        for s in sells:
            if "error" in s:
                continue
            bn_dt = datetime.fromtimestamp(int(s["time_ms"]) / 1000.0, tz=timezone.utc)
            # Approximate match by symbol + time proximity
            found = any(
                sym == db_sym and bn_dt and db_dt and abs((bn_dt - db_dt).total_seconds()) <= time_tol_minutes * 60
                for db_sym, db_dt in db_spot_keys
            )
            if not found:
                mismatches.append({
                    "code": "BINANCE_CLOSED_MISSING_IN_DB",
                    "severity": "high",
                    "symbol": sym,
                    "trade_type": "spot",
                    "binance_qty": s["qty"],
                    "binance_exit_price": s["price"],
                    "binance_time": bn_dt.isoformat() if bn_dt else None,
                    "detail": "sell_trade_on_binance_without_db_closed_record",
                })

    # ── FUTURES reconciliation ────────────────────────────────────────────────
    for rec in futures_db:
        sym = rec["symbol"]
        db_side = rec["side"]  # BUY=LONG, SELL=SHORT
        expected_bn_side = "LONG" if db_side == "BUY" else "SHORT"
        trades = futures_bn.get(sym, [])
        if isinstance(trades, list) and trades and "error" in trades[0]:
            mismatches.append({
                "code": "BINANCE_FUTURES_TRADES_UNAVAILABLE",
                "severity": "warning",
                "symbol": sym,
                "detail": trades[0].get("error"),
            })
            continue

        side_trades = [t for t in trades if str(t.get("side") or "").upper() == expected_bn_side]
        match = _match_db_trade_to_binance(rec, side_trades, qty_tol_pct, time_tol_minutes)
        if not match:
            mismatches.append({
                "code": "DB_CLOSED_MISSING_IN_BINANCE",
                "severity": "high",
                "symbol": sym,
                "trade_type": "futures",
                "db_side": db_side,
                "db_qty": rec["quantity"],
                "db_exit_time": rec["exit_time"],
                "db_close_reason": rec["close_reason"],
                "detail": "no_matching_closing_trade_on_binance",
            })
            continue

        matched_futures.append({
            "symbol": sym,
            "db_side": db_side,
            "db_qty": rec["quantity"],
            "binance_qty": match.get("qty"),
            "db_pnl": rec["pnl"],
            "binance_pnl": match.get("pnl"),
        })

        qty_diff = abs(rec["quantity"] - _safe_float(match.get("qty"), 0.0))
        if qty_diff / max(rec["quantity"], 1e-12) > qty_tol_pct:
            mismatches.append({
                "code": "CLOSED_QTY_MISMATCH",
                "severity": "high",
                "symbol": sym,
                "trade_type": "futures",
                "db_qty": rec["quantity"],
                "binance_qty": match.get("qty"),
                "qty_diff": round(qty_diff, 8),
            })

        bn_pnl = _safe_float(match.get("pnl"), 0.0)
        pnl_diff = abs(rec["pnl"] - bn_pnl)
        if pnl_diff > pnl_tol_usdt:
            mismatches.append({
                "code": "CLOSED_PNL_MISMATCH",
                "severity": "high",
                "symbol": sym,
                "trade_type": "futures",
                "db_pnl": round(rec["pnl"], 4),
                "binance_pnl": round(bn_pnl, 4),
                "pnl_diff": round(pnl_diff, 4),
            })

    # Detect Binance FUTURES closing trades not in DB
    db_futures_keys = {(r["symbol"], _parse_iso_ts(r["exit_time"])) for r in futures_db}
    for sym, trades in futures_bn.items():
        if not isinstance(trades, list):
            continue
        for t in trades:
            bn_ts = t.get("closed_at") or t.get("time")
            bn_dt = _parse_iso_ts(bn_ts)
            if not bn_dt:
                continue
            found = any(
                sym == db_sym and bn_dt and db_dt and abs((bn_dt - db_dt).total_seconds()) <= time_tol_minutes * 60
                for db_sym, db_dt in db_futures_keys
            )
            if not found:
                mismatches.append({
                    "code": "BINANCE_CLOSED_MISSING_IN_DB",
                    "severity": "high",
                    "symbol": sym,
                    "trade_type": "futures",
                    "binance_side": t.get("side"),
                    "binance_qty": t.get("qty"),
                    "binance_pnl": t.get("pnl"),
                    "binance_time": bn_dt.isoformat(),
                    "detail": "closing_trade_on_binance_without_db_closed_record",
                })

    total_db_pnl = sum(r["pnl"] for r in spot_db) + sum(r["pnl"] for r in futures_db)
    total_bn_pnl = (
        sum(_safe_float(m.get("binance_pnl"), 0.0) for m in matched_futures)
    )  # SPOT PnL not directly available from closing trade alone

    severity = "ok"
    if any(m.get("severity") == "high" for m in mismatches):
        severity = "warning"
    if any(m.get("severity") == "critical" for m in mismatches):
        severity = "critical"

    return {
        "ok": severity == "ok",
        "severity": severity,
        "mismatch_count": len(mismatches),
        "window_days": days,
        "spot_db_count": len(spot_db),
        "futures_db_count": len(futures_db),
        "spot_matched_count": len(matched_spot),
        "futures_matched_count": len(matched_futures),
        "total_db_pnl": round(total_db_pnl, 4),
        "total_binance_pnl_futures": round(total_bn_pnl, 4),
        "mismatches": mismatches[:50],
        "matched_spot": matched_spot[:50],
        "matched_futures": matched_futures[:50],
        "ts": _utc_now_iso(),
    }
