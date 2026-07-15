import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from simulation.mimic_lifecycle_adapter import _default_spot_bull_config, SimulationLifecycleAdapter
from simulation.mimic_feed import HistoricalDataFeed
from simulation.mimic_protocols import SimulationClock
from simulation.mimic_clients import SimulationBinanceClient
from lifecycle import ExitEngine, update_peak_pnl
from datetime import datetime, timezone

config = _default_spot_bull_config()
feed = HistoricalDataFeed(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'historical_data'))
klines = feed.get_klines('BTCUSDT', '1h')
print('BTC bars:', len(klines))

# Find bar at start of window
start = datetime(2026, 4, 15, tzinfo=timezone.utc)
ts = start.timestamp()
entry_bar = next((b for b in klines if b['open_time'] >= ts), None)
if entry_bar is None:
    entry_bar = klines[0]
entry_price = float(entry_bar['close'])
print(f'entry_price={entry_price:.2f} at {datetime.fromtimestamp(entry_bar["open_time"], tz=timezone.utc)}')

# Look at prices 50 and 100 cycles later
for offset in [0, 50, 100, 200, 300, 360]:
    idx = klines.index(entry_bar) + offset
    if idx >= len(klines):
        break
    bar = klines[idx]
    price = float(bar['close'])
    pnl_pct = (price - entry_price) / entry_price * 100
    print(f'offset={offset} price={price:.2f} pnl_pct={pnl_pct:.2f}%')

# Compute ATR at entry
from simulation.mimic_lifecycle_adapter import _approx_atr
atr = _approx_atr('BTCUSDT', {s: feed.get_klines(s, '1h') for s in ['BTCUSDT']})
print(f'ATR at entry={atr:.2f}')
print(f'partial target (1.5 ATR) = {1.5 * atr / entry_price * 100:.2f}%')
print(f'hard TP target (3.5 ATR) = {3.5 * atr / entry_price * 100:.2f}%')
print(f'hard SL target (2.25 ATR) = {2.25 * atr / entry_price * 100:.2f}%')

# Evaluate exit engine at offsets 50, 100, 200
from lifecycle.base import Position, Side
for offset in [50, 100, 200]:
    bar = klines[klines.index(entry_bar) + offset]
    price = float(bar['close'])
    clock = SimulationClock(start)
    clock._now = datetime.fromtimestamp(bar['open_time'], tz=timezone.utc)
    spot = SimulationBinanceClient(clock=clock, initial_balances={'USDT': 2910.0, 'BTC': 0.0068})
    spot.set_price_feed({'BTCUSDT': klines})
    adapter = SimulationLifecycleAdapter(spot_client=spot, futures_client=None, clock=clock, feed={'BTCUSDT': klines}, db_client=None)
    pos = Position(
        symbol='BTCUSDT', side=Side.LONG, quantity=0.0068, entry_price=entry_price,
        entry_time=start.timestamp(), position_usdt=510.72, category='LARGE_CAP',
        regime_at_entry='SPOT_BULL', profile_at_entry='A',
    )
    positions = update_peak_pnl([pos], adapter.price_resolver)
    ctx = adapter.build_context(config)
    engine = ExitEngine(price_resolver=adapter.price_resolver, atr_resolver=adapter.atr_resolver, rsi_resolver=adapter.rsi_resolver)
    decisions = engine.evaluate(positions, ctx, config.exit_policy, now=ctx.timestamp)
    print(f'Exit decisions at offset {offset} price={price:.2f} pnl_pct={(price-entry_price)/entry_price*100:.2f}%: {decisions}')
