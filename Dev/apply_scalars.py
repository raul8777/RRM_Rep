#!/usr/bin/env python3
"""Re-aplica los kelly scalars optimos de sesion 22 al proceso vivo."""
import urllib.request, json

KEY = 'DfJsbMHzq1gYZo6rvTFk7RXU9ht3xLauS2yVQleBjCdPW4KI'
BASE = 'http://localhost:8000'

payload = {
    'kelly_max_usdt':         663.0,
    'kelly_max_pct_liquid':   0.621,
    'futures_kelly_max_usdt': 1162.0,
    'futures_sl_pct':         3.33,
    'futures_tp_pct':         7.68,
    'stop_loss_pct':          1.5,
    'min_signal_strength':    0.40,
}

req = urllib.request.Request(
    f'{BASE}/config/kelly',
    data=json.dumps(payload).encode(),
    headers={'X-API-Key': KEY, 'Content-Type': 'application/json'},
    method='PATCH'
)
with urllib.request.urlopen(req, timeout=15) as r:
    d = json.loads(r.read())

print('ok:', d.get('ok'))
print('applied:')
for field, info in d.get('applied', {}).items():
    if isinstance(info, dict) and 'old' in info:
        print(f'  {field}: {info["old"]} -> {info["new"]}')
    else:
        print(f'  {field}: {info}')
if d.get('rejected'):
    print('rejected:', d['rejected'])
