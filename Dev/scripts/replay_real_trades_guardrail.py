"""
Reprocesar trades reales cerrados bajo el guardrail USD.

Lee trades_closed de PostgreSQL (últimos N días) y recalcula, para cada trade,
qué hubiera pasado si el guardrail USD estuviera activo:
- SL/TP efectivo capado a max_loss_usd_per_trade
- Emergency gap stop a 1.5x
- PnL hipotético truncado a la pérdida máxima permitida

Es un sanity-check rápido, no un replay completo: no modela re-entradas,
slippage intrabar ni cambios de comportamiento del bot.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root))

from config import get_settings
from db_client import get_db_client


def build_db_client():
    settings = get_settings()
    db_url = getattr(settings, "db_url", None)
    if not db_url:
        db_url = (
            f"postgresql://{settings.db_user}:{settings.db_password}"
            f"@{settings.db_host}:{settings.db_port}/{settings.db_name}"
        )
    return get_db_client(db_url)


def fetch_real_trades(days: int) -> List[Dict[str, Any]]:
    client = build_db_client()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    query = """
        SELECT symbol, side, quantity, entry_price, exit_price,
               entry_time, exit_time, pnl, pnl_percentage, close_reason,
               entry_commission, exit_commission, metadata
        FROM trades_closed
        WHERE exit_time >= %s
        ORDER BY exit_time DESC
    """
    rows = client.execute_query(query, (since,))
    return rows


def simulate_guardrail(
    trade: Dict[str, Any],
    max_loss_usd: float = 18.0,
    emergency_mult: float = 1.5,
) -> Dict[str, Any]:
    qty = float(trade.get("quantity") or 0)
    entry = float(trade.get("entry_price") or 0)
    exit_p = float(trade.get("exit_price") or 0)
    side = trade.get("side")
    actual_pnl = float(trade.get("pnl") or 0)

    if qty <= 0 or entry <= 0 or exit_p <= 0:
        return {"error": "invalid_trade_data"}

    notional = qty * entry
    max_drop = min(max_loss_usd, notional * 0.95)

    if side == "BUY":
        effective_sl = entry - max_drop / qty
        sl_distance_pct = (effective_sl - entry) / entry * 100
    elif side == "SELL":
        effective_sl = entry + max_drop / qty
        sl_distance_pct = (effective_sl - entry) / entry * 100
    else:
        return {"error": f"unknown_side_{side}"}

    emergency_threshold = -max_loss_usd * emergency_mult
    hypothetical_reason = trade.get("close_reason") or "unknown"
    hypothetical_pnl = actual_pnl

    if actual_pnl < emergency_threshold:
        hypothetical_reason = "emergency_gap_stop"
        hypothetical_pnl = emergency_threshold
    elif actual_pnl < -max_loss_usd:
        hypothetical_reason = "usd_cap_stop"
        hypothetical_pnl = -max_loss_usd

    return {
        "symbol": trade.get("symbol"),
        "side": side,
        "entry_price": entry,
        "exit_price": exit_p,
        "quantity": qty,
        "notional": round(notional, 2),
        "actual_pnl": round(actual_pnl, 2),
        "hypothetical_pnl": round(hypothetical_pnl, 2),
        "pnl_improvement": round(hypothetical_pnl - actual_pnl, 2),
        "actual_close_reason": trade.get("close_reason"),
        "hypothetical_close_reason": hypothetical_reason,
        "usd_sl_price": round(effective_sl, 6),
        "usd_sl_distance_pct": round(sl_distance_pct, 4),
        "max_loss_usd": max_loss_usd,
        "exit_time": trade.get("exit_time").isoformat() if hasattr(trade.get("exit_time"), 'isoformat') else str(trade.get("exit_time")),
    }


def compute_summary(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    actual_total = sum(r["actual_pnl"] for r in records if "actual_pnl" in r)
    hyp_total = sum(r["hypothetical_pnl"] for r in records if "hypothetical_pnl" in r)
    capped = [r for r in records if r.get("hypothetical_close_reason") in ("usd_cap_stop", "emergency_gap_stop")]
    emergency = [r for r in records if r.get("hypothetical_close_reason") == "emergency_gap_stop"]
    over_20 = [r for r in records if r.get("actual_pnl", 0) < -20]
    over_20_hyp = [r for r in records if r.get("hypothetical_pnl", 0) < -20]
    actual_losses = [r["actual_pnl"] for r in records if r.get("actual_pnl", 0) < 0]
    hyp_losses = [r["hypothetical_pnl"] for r in records if r.get("hypothetical_pnl", 0) < 0]

    return {
        "trade_count": len(records),
        "actual_total_pnl": round(actual_total, 2),
        "hypothetical_total_pnl": round(hyp_total, 2),
        "improvement": round(hyp_total - actual_total, 2),
        "actual_max_loss": round(min(actual_losses) if actual_losses else 0.0, 2),
        "hypothetical_max_loss": round(min(hyp_losses) if hyp_losses else 0.0, 2),
        "capped_count": len(capped),
        "capped_pct": round(len(capped) / len(records), 4) if records else 0.0,
        "emergency_gap_count": len(emergency),
        "actual_losses_over_20": len(over_20),
        "actual_losses_over_20_pct": round(len(over_20) / len(actual_losses), 4) if actual_losses else 0.0,
        "hypothetical_losses_over_20": len(over_20_hyp),
        "hypothetical_losses_over_20_pct": round(len(over_20_hyp) / len(hyp_losses), 4) if hyp_losses else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay real trades under USD loss guardrail")
    parser.add_argument("--days", type=int, default=30, help="How many days of trades_closed to replay")
    parser.add_argument("--max-loss-usd", type=float, default=18.0)
    parser.add_argument("--emergency-mult", type=float, default=1.5)
    parser.add_argument("--output", required=True, help="Output JSON path")
    args = parser.parse_args()

    try:
        trades = fetch_real_trades(args.days)
    except Exception as e:
        print(f"[ReplayGuardrail] DB fetch failed: {e}")
        output = {"error": str(e), "records": [], "summary": {}}
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, default=str)
        return 1

    records = [simulate_guardrail(t, args.max_loss_usd, args.emergency_mult) for t in trades]
    summary = compute_summary(records)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": args.days,
        "max_loss_usd": args.max_loss_usd,
        "emergency_mult": args.emergency_mult,
        "summary": summary,
        "records": records,
    }

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"[ReplayGuardrail] Processed {len(records)} trades. Improvement: {summary['improvement']:+} USD")
    print(f"[ReplayGuardrail] Saved: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
