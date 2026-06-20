"""
benchmark_snapshot.py — Captura el estado actual del sistema como snapshot/baseline.

Uso (dentro del container):
    python Dev/scripts/benchmark_snapshot.py [--label mi_label]

Uso desde Windows (via docker exec):
    docker exec crypto-agent-app python /app/Dev/scripts/benchmark_snapshot.py

Genera:
    Dev/benchmarks/snapshot_YYYYMMDD_HHMM.json  — timestamped snapshot
    Dev/benchmarks/baseline.json                 — sobreescrito siempre (referencia fija)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/app')

OUT_DIR = '/app/Dev/benchmarks'
os.makedirs(OUT_DIR, exist_ok=True)


def capture(label: str = "snapshot") -> dict:
    from config import Settings
    from binance_client import BinanceClient
    from position_manager import PositionManager
    from db_client import DatabaseClient
    from run_trades import _get_futures_client

    settings = Settings()
    client   = BinanceClient(
        settings.binance_api_key,
        settings.binance_api_secret,
        testnet=settings.binance_use_testnet,
    )

    # Spot liquid balance
    bal  = client.get_account_balance()
    usdt = float(bal.get('USDT', 0))

    # Spot closed trades from DB
    db           = DatabaseClient(settings.db_url)
    pm           = PositionManager(db)
    closed_spot  = pm.get_closed_positions(limit=1000)
    sp_wins      = sum(1 for p in closed_spot if float(getattr(p, 'pnl', 0) or 0) > 0)
    sp_pnl       = sum(float(getattr(p, 'pnl', 0) or 0) for p in closed_spot)

    # Futures open positions (from FuturesClient)
    fc     = _get_futures_client(settings)
    f_open = [p for p in fc._open_positions.values() if not p.closed]

    # Futures closed PnL from Binance trades API
    total_futures_pnl = 0.0
    futures_trades_count = 0
    futures_wins = 0
    try:
        raw_trades = client.client.futures_account_trades(limit=1000)
        from collections import defaultdict
        sym_pnl = defaultdict(float)
        for t in raw_trades:
            sym_pnl[t['symbol']] += float(t.get('realizedPnl', 0))
        total_futures_pnl = sum(sym_pnl.values())
        futures_wins = sum(1 for v in sym_pnl.values() if v > 0)
        futures_trades_count = len(sym_pnl)
    except Exception as exc:
        print(f"[WARN] Binance futures trades API: {exc}")

    # Market context
    try:
        import requests as _req
        fng_r = _req.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        fng_val = int(fng_r.json()['data'][0]['value'])
        fng_lbl = fng_r.json()['data'][0]['value_classification']
    except Exception:
        fng_val, fng_lbl = None, None

    now = datetime.now(timezone.utc)
    return {
        "captured_at":  now.isoformat(),
        "label":        label,
        "commits":      ["d454044", "ce6dc9b", "b6dc0e0", "5513feb"],
        "fixes_active": [
            "BUG-E1+E7", "BUG-E2", "BUG-E4", "BUG-E5",
            "BUG-E6", "BUG-E8", "BUG-E10", "MEJORA-A1",
        ],
        "capital": {
            "liquid_usdt": round(usdt, 4),
        },
        "spot": {
            "closed_total": len(closed_spot),
            "wins":         sp_wins,
            "losses":       len(closed_spot) - sp_wins,
            "win_rate":     round(sp_wins / len(closed_spot), 4) if closed_spot else None,
            "total_pnl":    round(sp_pnl, 4),
        },
        "futures": {
            "open_shorts":    len(f_open),
            "symbols_traded": futures_trades_count,
            "wins":           futures_wins,
            "total_pnl":      round(total_futures_pnl, 4),
        },
        "market": {
            "fng_value": fng_val,
            "fng_label": fng_lbl,
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="snapshot", help="Label for this snapshot")
    args = parser.parse_args()

    print("Capturing system snapshot...")
    snap = capture(label=args.label)

    ts        = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    snap_path = os.path.join(OUT_DIR, f"snapshot_{ts}.json")
    base_path = os.path.join(OUT_DIR, "baseline.json")

    with open(snap_path, "w") as f:
        json.dump(snap, f, indent=2, default=str)
    with open(base_path, "w") as f:
        json.dump(snap, f, indent=2, default=str)

    W = 55
    print(f"\n{'='*W}")
    print(f"  SNAPSHOT — {snap['captured_at']}")
    print(f"{'='*W}")
    print(f"  Liquid USDT:   ${snap['capital']['liquid_usdt']:>10,.2f}")
    print(f"  Spot cerrados: {snap['spot']['closed_total']:>5}  wins={snap['spot']['wins']}  PnL=${snap['spot']['total_pnl']:+.2f}")
    print(f"  Fut open:      {snap['futures']['open_shorts']:>5} SHORTs")
    print(f"  Fut PnL:       ${snap['futures']['total_pnl']:>10.2f} USDT")
    if snap['market']['fng_value']:
        print(f"  F&G:           {snap['market']['fng_value']} ({snap['market']['fng_label']})")
    print(f"{'='*W}")
    print(f"  Saved: {snap_path}")
    print(f"  Saved: {base_path}  ← baseline reference")


if __name__ == "__main__":
    main()
