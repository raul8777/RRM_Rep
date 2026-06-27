#!/usr/bin/env python3
"""Valida que el Pair Scanner y el screener contextualizado funcionen según el régimen."""
import urllib.request
import json
import time

KEY = 'DfJsbMHzq1gYZo6rvTFk7RXU9ht3xLauS2yVQleBjCdPW4KI'
BASE = 'http://localhost:8000'


def get(path):
    req = urllib.request.Request(f'{BASE}{path}', headers={'X-API-Key': KEY})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


# Wait for app
for i in range(12):
    try:
        urllib.request.urlopen(f'{BASE}/health', timeout=3)
        break
    except Exception:
        time.sleep(3)

summary = get('/dashboard/summary')
regime = summary.get('market_regime', {}).get('mode', 'SPOT_BULL')
screener = summary.get('pair_screener', {})
candidates = screener.get('candidates', []) if isinstance(screener, dict) else []

print(f"Régimen actual: {regime}")
print(f"Candidatos en scanner: {len(candidates)}")
print(f"Nota del scanner: {screener.get('scanner_note', '-')}")
print()

errors = []
warnings = []

# 1. Verificar que no hay bug de escala (todos los LONG deben tener strength >= 0.6)
raw_longs = [c for c in candidates if c.get('bias') == 'LONG']
raw_shorts = [c for c in candidates if c.get('bias') == 'SHORT']
raw_neutral = [c for c in candidates if c.get('bias') == 'NEUTRAL']
print(f"RAW bias — LONG={len(raw_longs)}, SHORT={len(raw_shorts)}, NEUTRAL={len(raw_neutral)}")

for c in candidates:
    score = float(c.get('composite_score', 0))
    strength = float(c.get('strength', score / 100.0))
    bias = c.get('bias')
    if bias == 'LONG' and strength < 0.6:
        errors.append(f"Escala bug: {c['symbol']} marcado LONG pero strength={strength:.3f} < 0.6")
    if bias == 'SHORT' and strength > 0.35:
        errors.append(f"Escala bug: {c['symbol']} marcado SHORT pero strength={strength:.3f} > 0.35")

# 2. Verificar que el regime_adjusted_bias refleja el régimen
if regime == 'FUTURES_SHORT':
    regime_shorts = [c for c in candidates if c.get('regime_adjusted_bias') == 'SHORT']
    print(f"Régimen bias — SHORT={len(regime_shorts)}")
    if len(regime_shorts) == 0:
        warnings.append("FUTURES_SHORT: no hay candidatos SHORT ajustados por régimen")

    # Los mejores candidatos SHORT deben ser rebotes, no caídas sobreextendidas
    short_candidates = sorted(
        [c for c in candidates if c.get('regime_adjusted_bias') == 'SHORT'],
        key=lambda x: float(x.get('vol_spike', 0)) * abs(float(x.get('change_24h', 0))),
        reverse=True
    )[:5]
    for c in short_candidates:
        chg = float(c.get('change_24h', 0))
        if chg < -8.0:
            warnings.append(f"FUTURES_SHORT: {c['symbol']} chg24h={chg:.2f}% es caída sobreextendida, no rebote")
        if chg > 0:
            print(f"  ✅ SHORT bounce candidate: {c['symbol']} chg24h=+{chg:.2f}% trend={c.get('trend_4h')}")

elif regime == 'SPOT_BULL':
    regime_longs = [c for c in candidates if c.get('regime_adjusted_bias') == 'LONG']
    print(f"Régimen bias — LONG={len(regime_longs)}")
    long_candidates = sorted(
        [c for c in candidates if c.get('regime_adjusted_bias') == 'LONG'],
        key=lambda x: float(x.get('composite_score', 0)),
        reverse=True
    )[:5]
    for c in long_candidates:
        chg = float(c.get('change_24h', 0))
        if chg < 0:
            warnings.append(f"SPOT_BULL: {c['symbol']} chg24h={chg:.2f}% es negativo en top LONG")

# 3. Verificar top_bearish / top_bullish del screener backend si es accesible
try:
    from pair_screener import get_screened_pairs
    screened = get_screened_pairs(regime_mode=regime)
    meta = screened.get('_meta', {})
    print(f"\nScreener backend: spot={meta.get('spot_count')}, futures={meta.get('futures_count')}, regime={meta.get('regime_mode')}")
    top_bearish = meta.get('top_bearish', [])
    top_bullish = meta.get('top_bullish', [])
    print(f"Top bearish: {[(c['symbol'], c['chg_pct'], round(c['score'])) for c in top_bearish[:3]]}")
    print(f"Top bullish: {[(c['symbol'], c['chg_pct'], round(c['score'])) for c in top_bullish[:3]]}")
    if regime == 'FUTURES_SHORT':
        for c in top_bearish[:3]:
            if c['chg_pct'] < -8.0:
                warnings.append(f"Screener top_bearish: {c['symbol']} chg={c['chg_pct']:.2f}% es caída extendida")
except Exception as e:
    print(f"\nNo se pudo acceder al screener backend directamente: {e}")

print()
if errors:
    print("❌ ERRORES:")
    for e in errors:
        print(f"  - {e}")
if warnings:
    print("⚠️ WARNINGS:")
    for w in warnings:
        print(f"  - {w}")
if not errors and not warnings:
    print("✅ Validación OK: scanner y screener alineados con el régimen")
