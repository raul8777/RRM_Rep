"""Internal baseline capture — runs inside the container using app modules directly."""
import sys
sys.path.insert(0, '/app')
import json
import os
from datetime import datetime, timezone

from config import Settings
from binance_client import BinanceClient
from position_manager import PositionManager
from db_client import DatabaseClient

settings = Settings()
client = BinanceClient(
    settings.binance_api_key,
    settings.binance_api_secret,
    testnet=settings.binance_use_testnet,
)

# Spot balance
bal  = client.get_account_balance()
usdt = float(bal.get('USDT', 0))

# Spot position manager (closed trades)
db = DatabaseClient(settings.db_url)
pm = PositionManager(db)
closed_spot = pm.get_closed_positions(limit=500)
sp_wins = sum(1 for p in closed_spot if float(getattr(p, 'pnl', 0) or 0) > 0)
sp_pnl  = sum(float(getattr(p, 'pnl', 0) or 0) for p in closed_spot)

# Futures — read from FuturesClient cache
from run_trades import _get_futures_client
fc = _get_futures_client(settings)
f_open   = [p for p in fc._open_positions.values() if not p.closed]
f_closed = [p for p in fc._open_positions.values() if p.closed]
f_wins   = sum(1 for p in f_closed if float(getattr(p, 'realized_pnl', 0) or 0) > 0)
f_pnl    = sum(float(getattr(p, 'realized_pnl', 0) or 0) for p in f_closed)

# Also grab futures from Binance directly for closed trades
try:
    raw_trades = client.client.futures_account_trades(limit=500)
    from collections import defaultdict
    sym_pnl = defaultdict(float)
    for t in raw_trades:
        sym_pnl[t['symbol']] += float(t.get('realizedPnl', 0))
    total_futures_pnl_binance = sum(sym_pnl.values())
    futures_closed_binance = len(set(t['orderId'] for t in raw_trades if float(t.get('realizedPnl', 0)) != 0))
    futures_wins_binance = sum(1 for v in sym_pnl.values() if v > 0)
except Exception as e:
    print(f"[WARN] Binance futures trades API: {e}")
    total_futures_pnl_binance = f_pnl
    futures_closed_binance = len(f_closed)
    futures_wins_binance = f_wins

snap = {
    'captured_at':       datetime.now(timezone.utc).isoformat(),
    'captured_at_local': 'jun 20 2026 16:13 UTC-6',
    'label':             'baseline_post_session5',
    'commits':           ['d454044', 'ce6dc9b', 'b6dc0e0', '5513feb'],
    'fixes_active': [
        'BUG-E1+E7', 'BUG-E2', 'BUG-E4', 'BUG-E5',
        'BUG-E6', 'BUG-E8', 'BUG-E10', 'MEJORA-A1',
    ],
    'capital': {
        'total_usdt':  usdt,
        'liquid_usdt': usdt,
    },
    'futures': {
        'open_shorts':            len(f_open),
        'closed_binance_orders':  futures_closed_binance,
        'wins_by_symbol':         futures_wins_binance,
        'total_pnl_binance':      round(total_futures_pnl_binance, 4),
        'cached_closed':          len(f_closed),
        'cached_wins':            f_wins,
        'cached_pnl':             round(f_pnl, 4),
    },
    'spot': {
        'closed_positions': len(closed_spot),
        'wins':             sp_wins,
        'losses':           len(closed_spot) - sp_wins,
        'win_rate':         round(sp_wins / len(closed_spot), 4) if closed_spot else None,
        'total_pnl':        round(sp_pnl, 4),
    },
}

os.makedirs('/app/Dev/benchmarks', exist_ok=True)
ts        = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')
snap_path = f'/app/Dev/benchmarks/snapshot_{ts}.json'
base_path = '/app/Dev/benchmarks/baseline.json'

with open(snap_path, 'w') as f:
    json.dump(snap, f, indent=2, default=str)
with open(base_path, 'w') as f:
    json.dump(snap, f, indent=2, default=str)

print('='*55)
print(f'  BASELINE CAPTURADO — {snap["captured_at_local"]}')
print('='*55)
print(f'  Capital líquido USDT: ${usdt:,.2f}')
print(f'  Spot cerrados:        {len(closed_spot)} trades  (wins={sp_wins}  PnL=${sp_pnl:+.2f})')
print(f'  Fut open SHORTs:      {len(f_open)}')
print(f'  Fut PnL Binance:      ${total_futures_pnl_binance:+.2f} USDT')
print('='*55)
print(f'  Saved: {snap_path}')
print(f'  Saved: {base_path}')
