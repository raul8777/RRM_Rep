"""One-time fix: zero out corrupted entry_commission/exit_commission in trades_closed.
Ghost trades (PEPE/ADA/TRX) had commission stored in raw token units, not USDT.
Max realistic commission per trade: kelly_max_usdt=$1200 * 0.001 * 2 = $2.40.
Any row with sum > $10 is corrupt data.
"""
import sys
sys.path.insert(0, '/app')
from db_client import DatabaseClient
from config import Settings

s = Settings()
db = DatabaseClient(s.db_url)

# Preview
rows = db.execute_query(
    "SELECT id, symbol, close_reason, entry_commission, exit_commission "
    "FROM trades_closed "
    "WHERE (COALESCE(entry_commission,0) + COALESCE(exit_commission,0)) > 10.0 "
    "ORDER BY id"
)
print(f"Rows to fix: {len(rows)}")
for r in rows:
    print(f"  id={r['id']} {r['symbol']} reason={r['close_reason']} "
          f"entry_comm={r['entry_commission']} exit_comm={r['exit_commission']}")

if not rows:
    print("Nothing to fix.")
    sys.exit(0)

# Apply fix
affected = db.execute_update(
    "UPDATE trades_closed "
    "SET entry_commission = 0, exit_commission = 0 "
    "WHERE (COALESCE(entry_commission,0) + COALESCE(exit_commission,0)) > 10.0"
)
print(f"\nFixed {len(rows)} rows — entry_commission and exit_commission zeroed out.")

# Verify: re-check avg_comm for last 7 days
result = db.execute_query(
    "SELECT AVG(COALESCE(entry_commission,0) + COALESCE(exit_commission,0)) as avg_comm, "
    "COUNT(*) as cnt "
    "FROM trades_closed "
    "WHERE exit_time >= NOW() - INTERVAL '7 days' "
    "AND metadata->>'trade_type' = 'spot'"
)
print(f"\nPost-fix spot 7d avg_comm: {result}")
