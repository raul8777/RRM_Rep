import math
import time
import requests
from config import get_settings
from binance_client import BinanceClient


def execute_once():
    s = get_settings()
    client = BinanceClient(api_key=s.binance_api_key, api_secret=s.binance_api_secret, testnet=s.binance_use_testnet)
    print('DRY_RUN=', s.dry_run)
    print('ALLOW_REAL_TRADING=', s.allow_real_trading)
    print('MAX_PER_TRADE_USDT=', s.max_per_trade_usdt)

    balances = client.get_account_balance()
    print('Non-zero balances (excluding USDT):')
    assets = []
    for asset, amt in balances.items():
        if asset.upper() != 'USDT' and amt > 0:
            print(asset, amt)
            assets.append((asset, amt))

    results = []
    for asset, amt in assets:
        symbol = asset.upper() + 'USDT'
        try:
            price = client.get_symbol_price(symbol)
        except Exception as e:
            print('Skip', symbol, 'price error', e)
            continue

        # Determine step size and min notional
        try:
            info = requests.get(f'https://api.binance.com/api/v3/exchangeInfo?symbol={symbol}', timeout=10).json()
            filters = info['symbols'][0]['filters']
            lot = next((f for f in filters if f['filterType'] == 'LOT_SIZE'), None)
            step = float(lot['stepSize']) if lot else 1.0
            min_notional = 0.0
            notf = next((f for f in filters if f['filterType'] == 'NOTIONAL'), None)
            if notf:
                min_notional = float(notf.get('minNotional', 0.0))
        except Exception as e:
            print('Could not get symbol info for', symbol, e)
            step = 1.0
            min_notional = 0.0

        max_usdt = s.max_per_trade_usdt
        qty_needed = math.floor((max_usdt / price) / step) * step
        qty_to_sell = min(amt, qty_needed)
        # ensure meets min_notional
        if qty_to_sell * price < min_notional:
            # compute minimal qty to meet min_notional
            needed = math.ceil((min_notional / price) / step) * step
            if needed > amt:
                print('Skipping', symbol, 'insufficient to meet min_notional')
                continue
            qty_to_sell = needed

        if qty_to_sell <= 0:
            continue

        payload = {"symbol": symbol, "action": "sell", "quantity": float(qty_to_sell)}
        print('Placing sell webhook for', payload)
        try:
            r = requests.post('http://127.0.0.1:8000/webhook', json=payload, headers={'X-Webhook-Secret': s.tradingview_webhook_secret}, timeout=30)
            print('Response:', r.status_code, r.text)
            results.append((symbol, qty_to_sell, r.status_code, r.text))
        except Exception as e:
            print('Webhook request failed for', symbol, e)

    print('Done. Results:', results)
    return results


def run_loop(interval_minutes: int = 5):
    """Run execute_once periodically every interval_minutes (blocking)."""
    while True:
        try:
            execute_once()
        except Exception as e:
            print('Error during execute_once:', e)
        time.sleep(max(1, interval_minutes) * 60)


if __name__ == '__main__':
    s = get_settings()
    run_loop(getattr(s, 'trade_cycle_minutes', 5))
