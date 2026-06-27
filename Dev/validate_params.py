#!/usr/bin/env python3
"""Valida que los parametros live del sistema coinciden con los optimos del backtest."""
import sys, os, urllib.request, json, time
sys.path.insert(0, '/app')

key = 'DfJsbMHzq1gYZo6rvTFk7RXU9ht3xLauS2yVQleBjCdPW4KI'

for i in range(12):
    try:
        urllib.request.urlopen('http://localhost:8000/health', timeout=3)
        break
    except Exception:
        time.sleep(3)

req = urllib.request.Request('http://localhost:8000/backtest/best', headers={'X-API-Key': key})
with urllib.request.urlopen(req, timeout=15) as r:
    best_d = json.loads(r.read())

from init_components import get_components
s = get_components().settings

best_p = best_d['best']['params']
cat_ps = best_d.get('category_patch_suggestion', {})

SEP = '=' * 70
print(SEP)
print('  VALIDACION: Parametros live vs optimos backtest (1000 runs)')
print(SEP)

# ── Kelly / SL / TP ──────────────────────────────────────────────────────────
CHECKS = [
    # (bt_key, settings_field, scale, skip_note_or_empty)
    ('kelly_base_usdt',        'kelly_base_usdt',        1.0,   'NO APLICA: mantener 100 (floor posicion base)'),
    ('kelly_max_usdt',         'kelly_max_usdt',          1.0,   ''),
    ('kelly_max_pct_liquid',   'kelly_max_pct_liquid',    1.0,   ''),
    ('futures_kelly_max_usdt', 'futures_kelly_max_usdt',  1.0,   ''),
    ('futures_sl_pct',         'futures_sl_pct',          100.0, ''),
    ('futures_tp_pct',         'futures_tp_pct',          100.0, ''),
    ('spot_sl_pct',            'stop_loss_pct',           100.0, ''),
    ('min_strength',           'min_signal_strength',     1.0,   'NO APLICA: minimo absoluto 0.40 validado'),
    ('min_prob',               None,                      1.0,   'VIA category_thresholds (ver seccion abajo)'),
    ('vol_spike_min',          None,                      1.0,   'NO APLICA: opt=0.83 < 1.0 = vol sin convencion'),
    ('spot_tp_pct',            None,                      100.0, 'VIA category_thresholds.take_profit_pct'),
]

ok_count  = 0
diff_count = 0
skip_count = 0

for bt_key, s_field, scale, note in CHECKS:
    bt_raw = best_p.get(bt_key)
    opt    = round(float(bt_raw) * scale, 3) if bt_raw is not None else None

    if not s_field or note.startswith('NO APLICA') or note.startswith('VIA'):
        status = 'SKIP'
        skip_count += 1
        live_r = '--'
    else:
        live_val = getattr(s, s_field, None)
        live_r   = round(float(live_val), 3) if live_val is not None else None
        if opt == live_r:
            status = 'OK'
            ok_count += 1
        else:
            status = 'DIFF'
            diff_count += 1

    note_str = f'  [{note}]' if note else ''
    print(f'  {status:<4}  {bt_key:<26}  opt={str(opt):<8}  live={str(live_r):<8}{note_str}')

# ── Category thresholds ───────────────────────────────────────────────────────
print()
print('  CATEGORY THRESHOLDS (pnl_service.validate — aplica SPOT y FUTURES):')
ct = getattr(s, 'category_thresholds', {}) or {}

CAT_TARGETS = {
    'LARGE_CAP':  {'min_prob': 0.55,  'take_profit_pct': 5.83, 'min_volspike': 1.2},
    'MIDCAP_ALT': {'min_prob': 0.527, 'take_profit_pct': 5.83, 'min_volspike': 1.5},
    'NARRATIVE':  {'min_prob': 0.527, 'take_profit_pct': 7.0,  'min_volspike': 1.5},
    'MEME':       {'min_prob': 0.527, 'take_profit_pct': 5.83, 'min_volspike': 2.5},
}

for cat, targets in CAT_TARGETS.items():
    c = ct.get(cat, {})
    row_parts = []
    for field, target in targets.items():
        live_v = c.get(field)
        live_r2 = round(float(live_v), 3) if live_v is not None else None
        target_r = round(float(target), 3)
        tag = 'OK' if live_r2 == target_r else f'DIFF(live={live_r2})'
        if live_r2 == target_r:
            ok_count += 1
        else:
            diff_count += 1
        row_parts.append(f'{field}={live_r2}[{tag}]')
    print(f'    {cat:<12}: {" | ".join(row_parts)}')

print()
print('  PENDIENTES EN category_patch_suggestion:')
if not cat_ps:
    print('    [ninguno] — todos los valores de categoria ya estan en optimos')
else:
    for cat2, fields in cat_ps.items():
        for f2, info in fields.items():
            print(f'    PENDING  {cat2}.{f2}: {info["current"]} -> {info["optimal"]}')

print()
print(SEP)
print(f'  RESULTADO: {ok_count} OK | {diff_count} DIFF | {skip_count} SKIP (intencional)')
print(SEP)
