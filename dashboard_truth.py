from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _resolve_mode_label(row: Dict[str, Any]) -> str:
    for key in ("current_regime_mode", "entry_regime_mode", "regime_mode", "mode"):
        value = row.get(key)
        if value is None:
            continue
        txt = str(value).strip().upper()
        if txt:
            return txt
    return "UNKNOWN"


def verify_futures_dashboard_row(
    row: Dict[str, Any],
    *,
    qty_rel_tol: float = 0.05,
    pnl_abs_tol: float = 0.5,
    pnl_rel_tol: float = 0.5,
) -> Dict[str, Any]:
    """Validate that operable futures values shown in dashboard are Binance-anchored.

    Returns fields that can be merged into the row payload.
    """
    if not isinstance(row, dict):
        return {
            "truth_source": "unknown",
            "verification_status": "invalid_row",
            "verification_notes": ["row is not a dict"],
            "verification_mismatch": True,
        }

    if str(row.get("trade_type", "")).lower() != "futures":
        return {
            "truth_source": "not_applicable",
            "verification_status": "not_futures",
            "verification_notes": [],
            "verification_mismatch": False,
        }

    b_qty = _as_float(row.get("binance_quantity", row.get("quantity")))
    b_upnl = _as_float(row.get("binance_unrealized_pnl", row.get("unrealized_pnl")))
    db_qty = _as_float(row.get("db_entries_total_qty", b_qty))
    db_upnl = _as_float(row.get("db_entries_total_unrealized_pnl", b_upnl))

    d_qty = abs(db_qty - b_qty)
    d_upnl = abs(db_upnl - b_upnl)
    qty_tol = max(1e-9, abs(b_qty) * max(qty_rel_tol, 0.0))
    pnl_tol = max(max(pnl_abs_tol, 0.0), abs(b_upnl) * max(pnl_rel_tol, 0.0))

    notes: List[str] = []
    mismatch = False
    if d_qty > qty_tol:
        mismatch = True
        notes.append(
            f"db_qty ({db_qty:.8f}) differs from binance_qty ({b_qty:.8f}) by {d_qty:.8f}"
        )
    if d_upnl > pnl_tol:
        mismatch = True
        notes.append(
            f"db_unrealized ({db_upnl:.4f}) differs from binance_unrealized ({b_upnl:.4f}) by {d_upnl:.4f}"
        )

    status = "warning_mismatch" if mismatch else "verified"
    if not notes:
        notes.append("operable values anchored to Binance live net position")

    return {
        "truth_source": "binance_live",
        "operable_quantity": b_qty,
        "operable_unrealized_pnl": b_upnl,
        "historical_aggregate_quantity": db_qty,
        "historical_aggregate_unrealized_pnl": db_upnl,
        "verification_status": status,
        "verification_notes": notes,
        "verification_mismatch": mismatch,
        "verification_delta_qty": round(d_qty, 8),
        "verification_delta_unrealized_pnl": round(d_upnl, 4),
        "verification_checked_at": datetime.now(timezone.utc).isoformat(),
    }


def verify_open_dashboard_row(
    row: Dict[str, Any],
    *,
    qty_rel_tol: float = 0.05,
    pnl_abs_tol: float = 0.5,
    pnl_rel_tol: float = 0.5,
) -> Dict[str, Any]:
    """Validate open dashboard rows for both spot and futures.

    Futures rows are Binance-anchored with DB-vs-live deltas.
    Spot rows are marked verified only when explicit live Binance quantity is provided.
    """
    if not isinstance(row, dict):
        return {
            "truth_scope": "open_position",
            "verification_mode": "UNKNOWN",
            "truth_source": "unknown",
            "verification_status": "invalid_row",
            "verification_notes": ["row is not a dict"],
            "verification_mismatch": True,
            "verification_checked_at": datetime.now(timezone.utc).isoformat(),
        }

    trade_type = str(row.get("trade_type") or "spot").strip().lower()
    mode_label = _resolve_mode_label(row)

    if trade_type == "futures":
        out = verify_futures_dashboard_row(
            row,
            qty_rel_tol=qty_rel_tol,
            pnl_abs_tol=pnl_abs_tol,
            pnl_rel_tol=pnl_rel_tol,
        )
        out["truth_scope"] = "open_position"
        out["verification_trade_type"] = "futures"
        out["verification_mode"] = mode_label
        return out

    db_qty = _as_float(row.get("quantity"))
    live_qty_raw = row.get("binance_quantity")
    if live_qty_raw is None:
        return {
            "truth_scope": "open_position",
            "verification_trade_type": "spot",
            "verification_mode": mode_label,
            "truth_source": "spot_db_without_live_qty",
            "operable_quantity": db_qty,
            "historical_aggregate_quantity": db_qty,
            "verification_status": "estimated_or_unverified",
            "verification_notes": ["spot row has no explicit Binance live quantity attached"],
            "verification_mismatch": False,
            "verification_delta_qty": 0.0,
            "verification_checked_at": datetime.now(timezone.utc).isoformat(),
        }

    live_qty = _as_float(live_qty_raw)
    d_qty = abs(db_qty - live_qty)
    qty_tol = max(1e-9, abs(live_qty) * max(qty_rel_tol, 0.0))
    mismatch = d_qty > qty_tol
    status = "warning_mismatch" if mismatch else "verified"
    notes = [
        (
            f"spot db_qty ({db_qty:.8f}) differs from binance_qty ({live_qty:.8f}) by {d_qty:.8f}"
            if mismatch
            else "spot quantity consistent with Binance live quantity"
        )
    ]
    return {
        "truth_scope": "open_position",
        "verification_trade_type": "spot",
        "verification_mode": mode_label,
        "truth_source": "binance_live" if not mismatch else "binance_live_with_drift",
        "operable_quantity": live_qty,
        "historical_aggregate_quantity": db_qty,
        "verification_status": status,
        "verification_notes": notes,
        "verification_mismatch": mismatch,
        "verification_delta_qty": round(d_qty, 8),
        "verification_checked_at": datetime.now(timezone.utc).isoformat(),
    }


def summarize_closed_position_verification(metadata: Any, close_reason: str) -> Dict[str, Any]:
    """Extract exchange-verification semantics for a closed position row."""
    meta = metadata if isinstance(metadata, dict) else {}
    contract = meta.get("close_contract") if isinstance(meta.get("close_contract"), dict) else {}
    exit_fill = meta.get("exit_fill") if isinstance(meta.get("exit_fill"), dict) else {}

    exchange_verified = bool(
        contract.get("exchange_confirmed")
        or exit_fill.get("binance_confirmed")
        or meta.get("binance_confirmed")
    )
    close_source = str(meta.get("close_source") or contract.get("close_source") or "").strip().lower()
    reconciliation_state = str(meta.get("reconciliation_state") or contract.get("reconciliation_state") or "").strip().lower()
    synthetic = bool(meta.get("synthetic") or contract.get("synthetic"))
    accounting_mode = str(meta.get("pnl_accounting_mode") or "unknown")

    if exchange_verified:
        status = "exchange_confirmed"
        notes = ["Binance-confirmed close/fill"]
    elif synthetic or close_source == "reconciliation":
        status = "reconciled"
        notes = ["Reconciled/synthetic close (not direct live fill)"]
    else:
        status = "estimated_or_unverified"
        notes = ["No Binance fill confirmation found in metadata"]

    return {
        "close_verification_status": status,
        "exchange_verified": exchange_verified,
        "close_source": close_source,
        "reconciliation_state": reconciliation_state,
        "synthetic_close": synthetic,
        "pnl_accounting_mode": accounting_mode,
        "close_verification_notes": notes,
        "close_reason": str(close_reason or ""),
    }
