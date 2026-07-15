import json
from collections import defaultdict

with open('Dev/benchmarks/live_jul12_closed.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

trades = data['trades']

by_symbol = defaultdict(lambda: {'pnl': 0, 'count': 0})
for t in trades:
    s = t['symbol']
    by_symbol[s]['pnl'] += t['pnl']
    by_symbol[s]['count'] += 1

print('=== PnL by symbol ===')
for s, v in sorted(by_symbol.items(), key=lambda x: x[1]['pnl']):
    print(f'{s:12s} {v["count"]:2d} trades  PnL: {v["pnl"]:8.2f}')

by_reason = defaultdict(lambda: {'pnl': 0, 'count': 0})
for t in trades:
    r = t['close_reason']
    by_reason[r]['pnl'] += t['pnl']
    by_reason[r]['count'] += 1

print('\n=== PnL by close reason ===')
for r, v in sorted(by_reason.items(), key=lambda x: x[1]['pnl']):
    print(f'{r:40s} {v["count"]:2d}  PnL: {v["pnl"]:8.2f}')

neg_pred = [t for t in trades if t.get('last_pred_pnl') is not None and t['last_pred_pnl'] < 0]
print(f'\nTrades with negative predicted PnL at entry: {len(neg_pred)}/{len(trades)}')
print(f'Total PnL of those: {sum(t["pnl"] for t in neg_pred):.2f}')

low_vol = [t for t in trades if t.get('vol_spike_at_entry') is not None and t['vol_spike_at_entry'] < 1.0]
print(f'\nTrades with vol_spike < 1.0 at entry: {len(low_vol)}/{len(trades)}')
print(f'Total PnL of those: {sum(t["pnl"] for t in low_vol):.2f}')

both = [t for t in trades if t.get('last_pred_pnl') is not None and t['last_pred_pnl'] < 0 and t.get('vol_spike_at_entry') is not None and t['vol_spike_at_entry'] < 1.0]
print(f'\nTrades with BOTH negative pred AND low vol: {len(both)}/{len(trades)}')
print(f'Total PnL of those: {sum(t["pnl"] for t in both):.2f}')
