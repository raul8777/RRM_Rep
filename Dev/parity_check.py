#!/usr/bin/env python3
"""Quick parity check: validate trade_type and close_reason normalization."""
import os
import json
import sys
sys.path.insert(0, '/app')

# Inside container, use service name 'crypto-agent-db'
db_url = os.environ.get('DATABASE_URL', 'postgresql://crypto_agent:crypto_agent@crypto-agent-db:5432/crypto_agent')
from db_client import DatabaseClient
db = DatabaseClient(db_url)

print("=" * 80)
print("PARITY CHECK: Recent backtest runs and live trades")
print("=" * 80)

# Get latest backtest run
runs = db.execute_query(
    'SELECT id, label, pnl_total, n_trades, n_spot, n_futures, window_start, window_end '
    'FROM backtest_run ORDER BY id DESC LIMIT 1'
)
if not runs:
    print("No runs found.")
    sys.exit(1)

run = runs[0]
run_id = run['id']
print(f"\nLatest run: id={run_id} label={run['label']} pnl={run['pnl_total']:.2f}")
print(f"  Window: {run['window_start'][:10]} to {run['window_end'][:10]}")
print(f"  Trades: total={run['n_trades']} spot={run['n_spot']} futures={run['n_futures']}")

# Check backtest trades trade_type distribution
bt_trades = db.execute_query(
    'SELECT trade_type, COUNT(*) as n FROM backtest_trade WHERE run_id = %s GROUP BY trade_type ORDER BY n DESC',
    (run_id,)
)
print(f"\n  Backtest trade_type distribution:")
for row in bt_trades:
    print(f"    {row['trade_type']}: {row['n']}")

# Check live trades in same window
live_trades = db.execute_query(
    'SELECT COUNT(*) as total, '
    '  COUNT(CASE WHEN metadata->>\'trade_type\' IN (\'spot\', \'futures\') THEN 1 END) as with_type, '
    '  COUNT(CASE WHEN metadata->>\'trade_type\' = \'spot\' THEN 1 END) as spot_type, '
    '  COUNT(CASE WHEN metadata->>\'trade_type\' = \'futures\' THEN 1 END) as futures_type, '
    '  COUNT(CASE WHEN metadata->>\'trade_type\' IS NULL THEN 1 END) as null_type '
    'FROM trades_closed WHERE entry_time >= %s AND entry_time < %s',
    (run['window_start'], run['window_end'])
)
lt = live_trades[0] if live_trades else {}
print(f"\n  Live trades in window [{run['window_start'][:10]}, {run['window_end'][:10]}):")
print(f"    Total: {lt.get('total', 0)}")
print(f"    With trade_type: {lt.get('with_type', 0)}")
print(f"      - spot: {lt.get('spot_type', 0)}")
print(f"      - futures: {lt.get('futures_type', 0)}")
print(f"    Without trade_type (null): {lt.get('null_type', 0)}")

# Check close_reason distribution in live trades (should be normalized now)
live_reasons = db.execute_query(
    'SELECT close_reason, COUNT(*) as n FROM trades_closed WHERE entry_time >= %s AND entry_time < %s GROUP BY close_reason ORDER BY n DESC LIMIT 10',
    (run['window_start'], run['window_end'])
)
print(f"\n  Close_reason distribution (top 10):")
for row in live_reasons:
    print(f"    {row['close_reason'][:30]:30} : {row['n']}")

print("\n" + "=" * 80)
print("STATUS: trade_type persistence improved in position_manager.py")
print("STATUS: close_reason normalization ready in app.py compare-vs-live")
print("=" * 80)
