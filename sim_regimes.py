"""
sim_regimes.py — Simulación comparativa por régimen de mercado

Escenarios:
  A: Sistema actual M1-M5 (solo spot, sin futuros)
  B: M1-M5 + FUTURES_SHORT en bear (-7%/7d → shorts BTC/ETH/SOL)
  C: M1-M5 + FUTURES_LONG en bull (RSI<32 + BTC UP)
  D: M1-M5 + FUTURES_SHORT en bear + FUTURES_LONG en bull (completo)

Períodos: 7d (rebote actual) y 30d (bear -17% mayo-junio)
Capital base: $3,028 USDT | MXN rate: 19.5
"""
from init_components import get_components
import statistics

comp = get_components()
bc   = comp.binance_client

MXN_RATE   = 19.5
# C0: use centralized Binance commission module (defaults: spot 0.001, futures 0.0004)
from binance_commission import total_commission as _sr_total_commission, get_config as _sr_comm_config
COMMISSION = _sr_comm_config().rate_for('spot') * 2  # kept for backward compat (round trip)
FUTURES_COMMISSION = _sr_comm_config().rate_for('futures')  # kept as per-side rate (existing code multiplies by leverage)

# ── Indicadores ───────────────────────────────────────────────────────────────
def calc_rsi(prices, n=14):
    if len(prices) < n + 1: return 50.0
    gs = [max(0, prices[i] - prices[i-1]) for i in range(1, len(prices))]
    ls = [max(0, prices[i-1] - prices[i]) for i in range(1, len(prices))]
    ag = sum(gs[-n:]) / n; al = sum(ls[-n:]) / n
    return 100 - 100 / (1 + ag / al) if al > 0 else 100

def ema(data, p):
    k = 2 / (p + 1); e = data[0]
    for d in data[1:]: e = d * k + e * (1 - k)
    return e

def calc_atr14(highs, lows, closes):
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
           for i in range(1, min(15, len(closes)))]
    return sum(trs) / len(trs) / closes[-1] if closes[-1] > 0 else 0.035

def calc_btc_7d(closes_btc, i):
    """BTC 7d % change at bar i (using 168 1h bars = 7 días)"""
    if i < 168: return 0.0
    return (closes_btc[i] - closes_btc[i - 168]) / closes_btc[i - 168] * 100

# ── Símbolos ─────────────────────────────────────────────────────────────────
SPOT_SYMBOLS    = ['INJUSDT','WLDUSDT','NEARUSDT','FETUSDT','RENDERUSDT','TAOUSDT',
                   'ONDOUSDT','JUPUSDT','OPUSDT']
FUTURES_SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT']

# ── Parámetros M1-M5 (sistema actual) ─────────────────────────────────────
SPOT_SC = {
    'atr_sl': True, 'sl_fixed': 0.035, 'tp': 0.07,
    'trail_act': 0.05, 'trail_dist': 0.02,
    'vol_min': 1.5, 'rsi_lo': 38, 'rsi_hi': 58,
    'cooldown': 12,
    'cap_base': 150, 'cap_strong': 225,
    'breakout_boost': True,
}

# Futuros SHORT: parámetros del .env actual
FUT_SHORT = {
    'margin':    25.0,   # USDT de margen por trade
    'leverage':   2,     # 2x
    'sl_pct':    0.03,   # 3% sobre entry → 6% pérdida sobre margen
    'tp_pct':    0.06,   # 6% bajo entry → 12% ganancia sobre margen
    'max_shorts': 2,     # máx simultáneos
    'bear_7d_threshold': -7.0,  # BTC 7d < -7% para activar shorts
}

# Futuros LONG: parámetros del .env actual
FUT_LONG = {
    'margin':   25.0,
    'leverage':  2,
    'sl_pct':   0.025,   # 2.5% bajo entry → 5% pérdida sobre margen
    'tp_pct':   0.05,    # 5% sobre entry → 10% ganancia sobre margen
    'rsi_threshold': 32, # RSI < 32 para entrar
    'cooldown_h': 24,    # no re-entrar en mismo símbolo en 24 velas
}

# ─────────────────────────────────────────────────────────────────────────────
def sim_spot(klines_map, period_bars, sc=SPOT_SC):
    """Simula el sistema spot M1-M5. Retorna lista de PnL USDT."""
    all_pnls = []
    for sym, klines in klines_map.items():
        if sym not in SPOT_SYMBOLS: continue
        closes = [float(k[4]) for k in klines[-period_bars:]]
        highs  = [float(k[2]) for k in klines[-period_bars:]]
        lows   = [float(k[3]) for k in klines[-period_bars:]]
        vols   = [float(k[5]) for k in klines[-period_bars:]]
        n = len(closes)
        if n < 25: continue
        avg_vol_20 = [sum(vols[i-20:i]) / 20 for i in range(20, n)]
        i = 20; cooldown = 0
        while i < n - 48:
            if cooldown > 0: cooldown -= 1; i += 1; continue
            rsi   = calc_rsi(closes[max(0, i-20):i+1])
            ema9  = ema(closes[max(0, i-15):i+1], 9)
            ema21 = ema(closes[max(0, i-25):i+1], 21)
            vs    = vols[i] / avg_vol_20[i-20] if i-20 < len(avg_vol_20) else 1.0

            is_breakout = False
            if sc['breakout_boost'] and i >= 13:
                recent_max = max(highs[i-13:i-1])
                if closes[i] > recent_max and vs >= 2.0:
                    is_breakout = True

            normal_entry = (vs >= sc['vol_min'] and sc['rsi_lo'] <= rsi <= sc['rsi_hi'] and ema9 > ema21)
            if not normal_entry: i += 1; continue

            if sc['atr_sl'] and i >= 15:
                atr_pct = calc_atr14(highs[max(0,i-15):i+1], lows[max(0,i-15):i+1], closes[max(0,i-15):i+1])
                sl = max(sc['sl_fixed'], max(0.01, min(0.08, atr_pct * 1.5)))
            else:
                sl = sc['sl_fixed']

            is_strong = (vs >= 2.0 and sc['rsi_lo']+5 <= rsi <= sc['rsi_hi']-5) or is_breakout
            cap = sc['cap_strong'] if is_strong else sc['cap_base']

            entry = closes[i]; sl_p = entry*(1-sl); tp_p = entry*(1+sc['tp'])
            peak = entry; trail_active = False; pnl_pct = None
            for j in range(i+1, min(i+48, n)):
                h = highs[j]; l = lows[j]
                if h > peak: peak = h
                if (peak-entry)/entry >= sc['trail_act']: trail_active = True
                trail_stop = peak*(1-sc['trail_dist']) if trail_active else 0
                if l <= sl_p: pnl_pct = -sl; break
                if trail_active and l <= trail_stop: pnl_pct = (trail_stop-entry)/entry; break
                if h >= tp_p: pnl_pct = sc['tp']; break
            if pnl_pct is None:
                pnl_pct = (closes[min(i+47, n-1)] - entry) / entry
            all_pnls.append((pnl_pct - COMMISSION) * cap)
            cooldown = sc['cooldown']; i += 1
    return all_pnls


def sim_futures_short(btc_klines, eth_klines, sol_klines, period_bars, fsc=FUT_SHORT):
    """
    Simula futuros SHORT en bear market.
    Entra en short cuando BTC 7d < threshold.
    SL/TP en sentido inverso (short gana cuando precio baja).
    """
    all_pnls = []
    for sym_klines in [btc_klines, eth_klines, sol_klines]:
        klines = sym_klines[-period_bars:]
        closes = [float(k[4]) for k in klines]
        highs  = [float(k[2]) for k in klines]
        lows   = [float(k[3]) for k in klines]
        n = len(closes)
        if n < 170: continue

        i = 168; cooldown = 0  # necesita 168 velas 1h para calcular 7d
        while i < n - 24:
            if cooldown > 0: cooldown -= 1; i += 1; continue

            # Calcular BTC 7d change local (aproximado con este símbolo)
            btc_7d = (closes[i] - closes[i-168]) / closes[i-168] * 100
            if btc_7d >= fsc['bear_7d_threshold']:
                i += 1; continue  # mercado no es bajista → no short

            # Filtro adicional: no abrir short si última vela +2% (momentum alcista)
            last_chg = (closes[i] - closes[i-1]) / closes[i-1] * 100 if i > 0 else 0
            if last_chg >= 2.0: i += 1; continue

            entry = closes[i]
            sl_p  = entry * (1 + fsc['sl_pct'])   # SL arriba del entry (short)
            tp_p  = entry * (1 - fsc['tp_pct'])   # TP abajo del entry (short)
            margin = fsc['margin']
            lev    = fsc['leverage']
            pnl_pct = None

            for j in range(i+1, min(i+48, n)):
                h = highs[j]; l = lows[j]
                if h >= sl_p: pnl_pct = -fsc['sl_pct'] * lev; break   # SL hit (precio sube)
                if l <= tp_p: pnl_pct = fsc['tp_pct'] * lev; break     # TP hit (precio baja)

            if pnl_pct is None:
                pnl_pct = (entry - closes[min(i+47, n-1)]) / entry * lev  # mark-to-market

            net = (pnl_pct - FUTURES_COMMISSION * lev) * margin
            all_pnls.append(net)
            cooldown = 24; i += 1

    return all_pnls


def sim_futures_long(klines_map, btc_klines_full, period_bars, flc=FUT_LONG):
    """
    Simula futuros LONG en SPOT_BULL cuando RSI < threshold.
    Solo entra si BTC 7d > 0 (mercado alcista).
    """
    all_pnls = []
    for sym in FUTURES_SYMBOLS:
        if sym not in klines_map: continue
        klines = klines_map[sym][-period_bars:]
        closes = [float(k[4]) for k in klines]
        highs  = [float(k[2]) for k in klines]
        lows   = [float(k[3]) for k in klines]
        n = len(closes)
        if n < 170: continue

        i = 168; cooldown = 0
        while i < n - 24:
            if cooldown > 0: cooldown -= 1; i += 1; continue

            # Solo en bull: BTC 7d > 0
            btc_7d = (closes[i] - closes[i-168]) / closes[i-168] * 100
            if btc_7d < 0: i += 1; continue

            rsi = calc_rsi(closes[max(0, i-20):i+1])
            if rsi >= flc['rsi_threshold']: i += 1; continue  # RSI no suficientemente bajo

            entry  = closes[i]
            sl_p   = entry * (1 - flc['sl_pct'])    # SL debajo del entry (long)
            tp_p   = entry * (1 + flc['tp_pct'])    # TP arriba del entry (long)
            margin = flc['margin']
            lev    = flc['leverage']
            pnl_pct = None

            for j in range(i+1, min(i+48, n)):
                h = highs[j]; l = lows[j]
                if l <= sl_p: pnl_pct = -flc['sl_pct'] * lev; break
                if h >= tp_p: pnl_pct = flc['tp_pct'] * lev; break

            if pnl_pct is None:
                pnl_pct = (closes[min(i+47, n-1)] - entry) / entry * lev

            net = (pnl_pct - FUTURES_COMMISSION * lev) * margin
            all_pnls.append(net)
            cooldown = flc['cooldown_h']; i += 1

    return all_pnls


# ── Función de reporte ────────────────────────────────────────────────────────
def report(name, pnls_spot, pnls_f_short, pnls_f_long, days):
    all_pnls = pnls_spot + pnls_f_short + pnls_f_long
    n = len(all_pnls)
    if n == 0:
        print(f'  {name}: sin trades')
        return

    total    = sum(all_pnls)
    wins     = sum(1 for p in all_pnls if p > 0)
    wr       = wins / n * 100
    wins_l   = [p for p in all_pnls if p > 0]
    losses_l = [p for p in all_pnls if p <= 0]
    avg_w    = statistics.mean(wins_l)   if wins_l   else 0
    avg_l    = statistics.mean(losses_l) if losses_l else 0
    pf       = abs(avg_w) / abs(avg_l)   if avg_l < 0 else 99.0
    pnl_day  = total / days
    t_day    = n / days

    spot_tot   = sum(pnls_spot)
    fshort_tot = sum(pnls_f_short)
    flong_tot  = sum(pnls_f_long)

    print(f'  {"─"*55}')
    print(f'  {name}')
    print(f'  Trades: {n} ({t_day:.1f}/día) | WR: {wr:.1f}% | PF: {pf:.2f}')
    print(f'  AvgW: ${avg_w:.3f} | AvgL: ${avg_l:.3f}')
    print(f'  PnL/día: {pnl_day:.2f} USDT = {pnl_day*MXN_RATE:.0f} MXN/día')
    print(f'  PnL {days}d:  ${total:.2f} USDT = ${total*MXN_RATE:.0f} MXN')
    if pnls_f_short or pnls_f_long:
        print(f'  Desglose: spot=${spot_tot:.2f} | shorts=${fshort_tot:.2f} | longs_fut=${flong_tot:.2f}')
    print()


# ── Main ──────────────────────────────────────────────────────────────────────
print()
print('=' * 62)
print('  SIMULACIÓN POR RÉGIMEN — Sistema M1-M5 + Futuros')
print('=' * 62)
print()

# Descargar klines una sola vez
print('Descargando klines...')
all_symbols = list(set(SPOT_SYMBOLS + FUTURES_SYMBOLS))
klines_map  = {}
for sym in all_symbols:
    try:
        klines_map[sym] = bc.client.get_klines(symbol=sym, interval='1h', limit=750)
        print(f'  {sym}: {len(klines_map[sym])} velas OK')
    except Exception as e:
        print(f'  {sym}: ERROR {e}')

btc_klines = klines_map.get('BTCUSDT', [])
eth_klines = klines_map.get('ETHUSDT', [])
sol_klines = klines_map.get('SOLUSDT', [])

for period_label, period_bars in [('7 DÍAS (rebote actual)', 170), ('30 DÍAS (bear mayo-junio)', 720)]:
    days = 7 if period_bars == 170 else 30
    print()
    print('━' * 62)
    print(f'  PERÍODO: {period_label}')
    print('━' * 62)
    print()

    # Escenario A: solo spot M1-M5
    sp_a = sim_spot(klines_map, period_bars)
    report('A — Solo Spot M1-M5 (sin futuros)', sp_a, [], [], days)

    # Escenario B: spot + FUTURES_SHORT en bear
    fs_b  = sim_futures_short(btc_klines, eth_klines, sol_klines, period_bars)
    sp_b  = sim_spot(klines_map, period_bars)
    report('B — Spot M1-M5 + Futuros SHORT (bear)', sp_b, fs_b, [], days)

    # Escenario C: spot + FUTURES_LONG en bull (RSI<32)
    fl_c  = sim_futures_long(klines_map, btc_klines, period_bars)
    sp_c  = sim_spot(klines_map, period_bars)
    report('C — Spot M1-M5 + Futuros LONG bull (RSI<32)', sp_c, [], fl_c, days)

    # Escenario D: spot + SHORT en bear + LONG en bull (completo)
    sp_d  = sim_spot(klines_map, period_bars)
    report('D — Completo: Spot + SHORT bear + LONG bull', sp_d, fs_b, fl_c, days)

print('=' * 62)
print('  FIN DE SIMULACIÓN')
print('=' * 62)
