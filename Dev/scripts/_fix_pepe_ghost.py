"""
_fix_pepe_ghost.py — Limpieza del registro fantasma PEPEUSDT id=908

Mueve el registro de trades_open a trades_closed con close_reason='ghost_balance_mismatch'
y elimina el registro de trades_open para desbloquear el sistema.

Uso:
    docker compose exec app python Dev/scripts/_fix_pepe_ghost.py
"""
import os
import sys
import json
import urllib.request
from datetime import datetime, timezone

import psycopg2

DB_URL = os.environ.get("DB_URL", "")
if not DB_URL:
    print("ERROR: DB_URL not set")
    sys.exit(1)

conn = psycopg2.connect(DB_URL)
cur = conn.cursor()

# 1. Read ghost record
cur.execute(
    "SELECT id, symbol, side, quantity, price, stop_loss, take_profit, entry_time, "
    "metadata, pnl_prediction, entry_commission, strategy_id, indicators_at_entry "
    "FROM trades_open WHERE id=%s",
    (908,)
)
row = cur.fetchone()
if not row:
    print("ERROR: trades_open id=908 not found — already cleaned?")
    conn.close()
    sys.exit(0)

(db_id, symbol, side, quantity, entry_price, stop_loss, take_profit, entry_time,
 metadata, pnl_prediction, entry_commission, strategy_id, indicators_at_entry) = row

print(f"Ghost record: id={db_id} symbol={symbol} qty={quantity} entry={entry_price}")

# 2. Get current exit price
try:
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        exit_price = float(json.loads(resp.read())["price"])
    print(f"Exit price from Binance: {exit_price}")
except Exception as e:
    exit_price = float(entry_price)
    print(f"Could not fetch price ({e}), using entry_price: {exit_price}")

# 3. Build ghost metadata
ghost_meta = dict(metadata) if isinstance(metadata, dict) else {}
ghost_meta["ghost_cleanup"] = True
ghost_meta["ghost_reason"] = "binance_balance_mismatch"
ghost_meta["ghost_binance_balance"] = 0.17
ghost_meta["ghost_db_qty"] = float(quantity)
ghost_meta["ghost_cleaned_at"] = datetime.now(timezone.utc).isoformat()
ghost_meta["close_reason"] = "ghost_balance_mismatch"

# 4. Calculate PnL — real balance is 0.17 PEPE, not 268M
real_qty = 0.17
real_pnl = real_qty * (exit_price - float(entry_price))
real_pnl_pct = (exit_price / float(entry_price) - 1.0) * 100.0

print(f"Real qty: {real_qty} PEPE | PnL: ${real_pnl:.4f} ({real_pnl_pct:.2f}%)")

exit_time = datetime.now(timezone.utc)

# 5. Insert into trades_closed
cur.execute(
    """
    INSERT INTO trades_closed
        (symbol, side, quantity, entry_price, exit_price, strategy_id,
         entry_time, exit_time, pnl, pnl_percentage, close_reason,
         metadata, entry_commission, exit_commission, pnl_prediction,
         indicators_at_entry, is_corrupted, data_version, corrupt)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """,
    (
        symbol, side, real_qty, float(entry_price), exit_price, strategy_id,
        entry_time, exit_time, real_pnl, real_pnl_pct, "ghost_balance_mismatch",
        json.dumps(ghost_meta), float(entry_commission), 0.0,
        json.dumps(pnl_prediction) if pnl_prediction else None,
        json.dumps(indicators_at_entry) if indicators_at_entry else None,
        True, 1, True
    )
)
print("Inserted into trades_closed")

# 6. Delete from trades_open
cur.execute("DELETE FROM trades_open WHERE id=%s", (db_id,))
print(f"Deleted trades_open id={db_id}")

conn.commit()
print("DONE — ghost PEPEUSDT cleaned successfully")
conn.close()
