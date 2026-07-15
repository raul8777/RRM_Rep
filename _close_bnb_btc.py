"""One-shot script to close remaining BNB and BTC records using the new manual_position_closer service."""
import sys
sys.path.insert(0, '/app')

from init_components import get_components
import manual_position_closer as mpc

comp = get_components()

for symbol in ('BNBUSDT', 'BTCUSDT'):
    print('\n=== ' + symbol + ' ===')
    prev = mpc.preview_spot(symbol, comp)
    print('preview:', prev)
    reason = prev.get('reason', '')
    if not prev.get('ok') or reason == 'no_open_position':
        print('  -> No DB position, skipping')
        continue

    if prev.get('binance_qty', 0) == 0:
        print('  -> Zero Binance balance, closing as ghost')
        ghost = mpc.close_ghost_db(symbol, comp, source='cli_fix')
        print('  ghost close:', ghost)
        continue

    result = mpc.execute_spot(symbol, comp, source='cli_fix')
    print('result:', result)

print('\nDone.')
