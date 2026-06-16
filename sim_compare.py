from init_components import get_components
comp = get_components()
bc = comp.binance_client
import statistics

MXN_RATE = 19.5
COMMISSION = 0.002

def calc_rsi(prices, n=14):
    if len(prices) < n+1: return 50.0
    gs = [max(0, prices[i]-prices[i-1]) for i in range(1,len(prices))]
    ls = [max(0, prices[i-1]-prices[i]) for i in range(1,len(prices))]
    ag = sum(gs[-n:])/n; al = sum(ls[-n:])/n
    return 100 - 100/(1+ag/al) if al > 0 else 100

def ema(data, p):
    k = 2/(p+1); e = data[0]
    for d in data[1:]: e = d*k + e*(1-k)
    return e

def calc_atr14(highs, lows, closes):
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, min(15, len(closes)))]
    return sum(trs)/len(trs)/closes[-1] if closes[-1] > 0 else 0.035

symbols_before = ['INJUSDT','WLDUSDT','NEARUSDT','FETUSDT','RENDERUSDT','TAOUSDT']
symbols_after  = symbols_before + ['ONDOUSDT','JUPUSDT','OPUSDT']

scenarios = [
    {
        'name': 'ANTES (F1-F4 solamente)',
        'symbols': symbols_before,
        'atr_sl': False,
        'sl_fixed': 0.035,
        'tp': 0.07,
        'trail_act': 0.05, 'trail_dist': 0.02,
        'vol_min': 1.5, 'rsi_lo': 38, 'rsi_hi': 58,
        'cooldown': 12,
        'cap_base': 150, 'cap_strong': 150,
        'breakout_boost': False,
    },
    {
        'name': 'DESPUES (M1-M5 implementados)',
        'symbols': symbols_after,
        'atr_sl': True,
        'sl_fixed': 0.035,
        'tp': 0.07,
        'trail_act': 0.05, 'trail_dist': 0.02,
        'vol_min': 1.5, 'rsi_lo': 38, 'rsi_hi': 58,
        'cooldown': 12,
        'cap_base': 150, 'cap_strong': 225,
        'breakout_boost': True,
    },
]

# Simular sobre 7d y 30d por separado
for period_name, limit in [('7 DIAS', 170), ('30 DIAS', 720)]:
    print('='*60)
    print('PERIODO: ' + period_name)
    print('='*60)

    for sc in scenarios:
        all_pnls = []; total_trades = 0; strong_trades = 0; atr_upgrades = 0; breakout_entries = 0

        for sym in sc['symbols']:
            try:
                klines = bc.client.get_klines(symbol=sym, interval='1h', limit=limit)
                closes = [float(k[4]) for k in klines]
                highs  = [float(k[2]) for k in klines]
                lows   = [float(k[3]) for k in klines]
                vols   = [float(k[5]) for k in klines]
                avg_vol_20 = [sum(vols[i-20:i])/20 for i in range(20, len(vols))]

                i = 20; cooldown = 0
                while i < len(closes) - 48:
                    if cooldown > 0:
                        cooldown -= 1; i += 1; continue
                    rsi = calc_rsi(closes[max(0,i-20):i+1])
                    ema9  = ema(closes[max(0,i-15):i+1], 9)
                    ema21 = ema(closes[max(0,i-25):i+1], 21)
                    vs = vols[i] / avg_vol_20[i-20] if i-20 < len(avg_vol_20) else 1.0

                    # M4: breakout_12h is a STRENGTH BOOST on existing BUY signals, not a standalone entry
                    is_breakout = False
                    if sc['breakout_boost'] and i >= 13:
                        recent_max = max(highs[i-13:i-1])
                        if closes[i] > recent_max and vs >= 2.0:
                            is_breakout = True  # will boost cap if normal_entry also qualifies

                    normal_entry = (vs >= sc['vol_min'] and sc['rsi_lo'] <= rsi <= sc['rsi_hi'] and ema9 > ema21)
                    if not normal_entry:
                        i += 1; continue
                    if is_breakout: breakout_entries += 1

                    if sc['atr_sl'] and i >= 15:
                        atr_pct = calc_atr14(highs[max(0,i-15):i+1], lows[max(0,i-15):i+1], closes[max(0,i-15):i+1])
                        sl_atr = max(0.01, min(0.08, atr_pct * 1.5))
                        sl = max(sc['sl_fixed'], sl_atr)
                        if sl > sc['sl_fixed']: atr_upgrades += 1
                    else:
                        sl = sc['sl_fixed']

                    # M3: conviction sizing; M4 breakout also triggers strong sizing
                    is_strong = (vs >= 2.0 and sc['rsi_lo']+5 <= rsi <= sc['rsi_hi']-5) or is_breakout
                    cap = sc['cap_strong'] if (is_strong and sc['cap_strong'] > sc['cap_base']) else sc['cap_base']
                    if is_strong: strong_trades += 1

                    entry = closes[i]; sl_p = entry*(1-sl); tp_p = entry*(1+sc['tp'])
                    peak = entry; trail_active = False; pnl_pct = None

                    for j in range(i+1, min(i+48, len(closes))):
                        h = highs[j]; l = lows[j]
                        if h > peak: peak = h
                        if (peak-entry)/entry >= sc['trail_act']: trail_active = True
                        trail_stop = peak*(1-sc['trail_dist']) if trail_active else 0
                        if l <= sl_p: pnl_pct = -sl; break
                        if trail_active and l <= trail_stop: pnl_pct = (trail_stop-entry)/entry; break
                        if h >= tp_p: pnl_pct = sc['tp']; break

                    if pnl_pct is None:
                        pnl_pct = (closes[min(i+47, len(closes)-1)]-entry)/entry
                    net = (pnl_pct - COMMISSION) * cap
                    all_pnls.append(net)
                    total_trades += 1
                    cooldown = sc['cooldown']
                    i += 1
            except Exception as e:
                pass

        n = len(all_pnls)
        if n == 0:
            print(sc['name'] + ': no trades')
            continue

        days = 7 if limit == 170 else 30
        total = sum(all_pnls)
        wins  = sum(1 for p in all_pnls if p > 0)
        wr    = wins/n*100
        wins_list   = [p for p in all_pnls if p > 0]
        losses_list = [p for p in all_pnls if p <= 0]
        avg_w = statistics.mean(wins_list)   if wins_list   else 0
        avg_l = statistics.mean(losses_list) if losses_list else 0
        pf = abs(avg_w)/abs(avg_l) if avg_l < 0 else 99
        pnl_day = total / days
        trades_day = n / days

        print('--- ' + sc['name'] + ' ---')
        print('Pares: ' + str(len(sc['symbols'])) + ' | Trades: ' + str(n) + ' (' + str(round(trades_day,1)) + '/dia) | Strong: ' + str(strong_trades) + ' | Breakouts: ' + str(breakout_entries) + ' | ATR upg: ' + str(atr_upgrades))
        print('WR=' + str(round(wr,1)) + '% | AvgW=' + str(round(avg_w,3)) + ' | AvgL=' + str(round(avg_l,3)) + ' | PF=' + str(round(pf,2)))
        print('PnL/dia=' + str(round(pnl_day,2)) + ' USDT = ' + str(round(pnl_day*MXN_RATE,0)) + ' MXN/dia')
        print('PnL total ' + str(days) + 'd = ' + str(round(total,2)) + ' USDT = ' + str(round(total*MXN_RATE,0)) + ' MXN')
        print()
