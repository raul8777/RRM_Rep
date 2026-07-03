#!/usr/bin/env python3
import argparse
import io
import json
import time
import traceback
import sys
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, '/app')

import run_trades


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--count', type=int, default=3)
    parser.add_argument('--sleep-seconds', type=float, default=2.0)
    parser.add_argument('--out', default=None)
    args = parser.parse_args()

    results = []
    for i in range(args.count):
        t0 = time.time()
        try:
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(buf):
                res = run_trades.execute_once()
            logs = buf.getvalue()
            results.append({
                'cycle': i + 1,
                'ok': True,
                'duration_sec': round(time.time() - t0, 2),
                'summary': {
                    'buy_orders': len(res.get('buy_orders', []) or []),
                    'sell_orders': len(res.get('sell_orders', []) or []),
                    'sl_tp_closes': len(res.get('sl_tp_closes', []) or []),
                    'futures_closes': len(res.get('futures_closes', []) or []),
                    'regime_mode': res.get('regime_mode'),
                },
                'log_tail': logs.splitlines()[-8:],
            })
        except Exception as exc:
            results.append({
                'cycle': i + 1,
                'ok': False,
                'duration_sec': round(time.time() - t0, 2),
                'error': str(exc),
                'traceback': traceback.format_exc(),
            })
        if i < args.count - 1 and args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    payload = {'ok': True, 'count': args.count, 'results': results}
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)
        print(json.dumps({'ok': True, 'out': args.out, 'count': args.count}, indent=2))
    else:
        print(json.dumps(payload, indent=2))


if __name__ == '__main__':
    main()
