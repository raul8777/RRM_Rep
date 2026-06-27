#!/usr/bin/env python3
"""Verifica el estado final de todos los parametros en el proceso vivo del app."""
import urllib.request, json

KEY  = 'DfJsbMHzq1gYZo6rvTFk7RXU9ht3xLauS2yVQleBjCdPW4KI'
BASE = 'http://localhost:8000'

def get(path):
    req = urllib.request.Request(f'{BASE}{path}', headers={'X-API-Key': KEY})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

state_d = get('/system/runtime-state')
state   = state_d.get('state', {})

ov_raw  = state.get('kelly_config_override')
ct_raw  = state.get('category_thresholds_override')

ov = json.loads(ov_raw) if isinstance(ov_raw, str) else (ov_raw or {})
ct = json.loads(ct_raw) if isinstance(ct_raw, str) else (ct_raw or {})

SEP = '=' * 60
print(SEP)
print('  ESTADO FINAL — Todos los parametros persistidos en BD')
print(SEP)

print()
print('  KELLY / SL / TP SCALARS:')
TARGETS = {
    'kelly_max_usdt':         663.0,
    'kelly_max_pct_liquid':   0.621,
    'futures_kelly_max_usdt': 1162.0,
    'futures_sl_pct':         3.33,
    'futures_tp_pct':         7.68,
    'stop_loss_pct':          1.5,
    'min_signal_strength':    0.40,
}
for field, target in TARGETS.items():
    info  = ov.get(field, {})
    live  = info.get('new') if isinstance(info, dict) else None
    match = 'OK  ' if live is not None and round(float(live), 3) == round(float(target), 3) else 'DIFF'
    print(f'    {match}  {field:<28}  live={live!s:<8}  target={target}')

print()
print('  CATEGORY THRESHOLDS (SPOT + FUTURES via pnl_service.validate):')
CAT_TARGETS = {
    'LARGE_CAP':  {'min_prob': 0.55,  'take_profit_pct': 5.83},
    'MIDCAP_ALT': {'min_prob': 0.527, 'take_profit_pct': 5.83},
    'NARRATIVE':  {'min_prob': 0.527, 'take_profit_pct': 7.0},
    'MEME':       {'min_prob': 0.527, 'take_profit_pct': 5.83},
}
for cat, targets in CAT_TARGETS.items():
    c = ct.get(cat, {})
    parts = []
    for field, target in targets.items():
        live_v = c.get(field)
        live_r = round(float(live_v), 3) if live_v is not None else None
        tgt_r  = round(float(target), 3)
        tag    = 'OK' if live_r == tgt_r else f'DIFF(live={live_r})'
        parts.append(f'{field}={live_r}[{tag}]')
    print(f'    {cat:<12}  {" | ".join(parts)}')

print()
print('  NO APLICADOS (intencional):')
print('    SKIP  min_strength   optimal=0.359  live=0.40  [minimo absoluto validado]')
print('    SKIP  vol_spike_min  optimal=0.83   live=1.0+  [vol<1.0=sin convencion]')
print('    SKIP  kelly_base     optimal=76.0   live=100.0 [floor posicion base]')
print()
print(SEP)
