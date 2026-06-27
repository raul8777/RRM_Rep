#!/usr/bin/env python3
"""Compara parametros live del APP (via API) con los optimos del backtest."""
import urllib.request, json, time

KEY = 'DfJsbMHzq1gYZo6rvTFk7RXU9ht3xLauS2yVQleBjCdPW4KI'
BASE = 'http://localhost:8000'

def get(path):
    req = urllib.request.Request(f'{BASE}{path}', headers={'X-API-Key': KEY})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

# Wait app
for i in range(12):
    try:
        urllib.request.urlopen(f'{BASE}/health', timeout=3); break
    except Exception:
        time.sleep(3)

best_d = get('/backtest/best')
cfg    = get('/config/kelly')

best_p = best_d['best']['params']
cat_ps = best_d.get('category_patch_suggestion', {})
spot   = cfg.get('spot', {})
fut    = cfg.get('futures', {})

SEP = '=' * 68
print(SEP)
print('  PARAMETROS LIVE (proceso app) vs OPTIMOS backtest 1000 runs')
print(SEP)

# Kelly scalars — comparacion con targets sesion 22
KELLY_TARGETS = [
    ('kelly_base_usdt',        spot.get('base_usdt'),  76.0,   100.0,  'NO APLICA: mantener 100'),
    ('kelly_max_usdt',         spot.get('max_usdt'),   663.0,  663.0,  ''),
    ('kelly_max_pct_liquid',   spot.get('cap_pct_liquid'), 0.621, 0.621, ''),
    ('futures_kelly_max_usdt', fut.get('max_usdt'),    1162.0, 1162.0, ''),
]
for label, live_v, opt, target, note in KELLY_TARGETS:
    live_r  = round(float(live_v), 3) if live_v is not None else None
    target_r = round(float(target), 3)
    if note:
        status = 'SKIP'
    elif live_r == target_r:
        status = 'OK  '
    else:
        status = 'DIFF'
    n = f'  [{note}]' if note else ''
    print(f'  {status}  {label:<26}  live={live_r!s:<8}  target={target_r!s:<8}{n}')

print()
print('  CATEGORY THRESHOLDS (app lee desde live settings post-startup-restore):')
if not cat_ps:
    print('  OK   category_patch_suggestion = [] -> todos los valores en optimos')
else:
    for cat, fields in cat_ps.items():
        for f, info in fields.items():
            cur = info['current']
            opt2 = info['optimal']
            print(f'  DIFF {cat}.{f}: live={cur} target={opt2}')

print()
print('  SL / TP (via /backtest/best param_diff):')
diff = best_d.get('param_diff', {})
SL_TP_MAP = [
    ('futures_sl_pct', 0.0333, 'futures_sl_pct', 100.0),
    ('futures_tp_pct', 0.0768, 'futures_tp_pct', 100.0),
    ('spot_sl_pct',    0.015,  'stop_loss_pct',   100.0),
    ('min_strength',   0.359,  'SKIP',             1.0),
    ('min_prob',       0.527,  'VIA_CAT',          1.0),
    ('vol_spike_min',  0.83,   'NO_APLICA',        1.0),
]
for bt_key, bt_raw, settings_f, scale in SL_TP_MAP:
    opt = round(bt_raw * scale, 3)
    if settings_f in ('SKIP', 'VIA_CAT', 'NO_APLICA'):
        notes = {'SKIP': 'NO APLICA: minimo 0.40', 'VIA_CAT': 'aplicado via category_thresholds', 'NO_APLICA': 'NO APLICA: vol<1.0=sin convencion'}
        print(f'  SKIP  {bt_key:<26}  opt={opt!s:<8}  [{notes[settings_f]}]')
    else:
        # Check from baseline params
        bline = best_d.get('baseline', {}).get('params', {})
        base_v = bline.get(bt_key)
        base_r = round(float(base_v) * scale, 3) if base_v is not None else None
        print(f'  INFO  {bt_key:<26}  opt={opt!s:<8}  baseline={base_r!s:<8}  (verifica en /config/kelly futures)')

print()
print(SEP)
print('  FUT SL/TP no expuestos en /config/kelly — verificar via BD:')
print(SEP)
