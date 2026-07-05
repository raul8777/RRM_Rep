from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

RUNTIME_KEY = "binance_truth_status"


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
