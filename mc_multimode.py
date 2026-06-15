"""Monte Carlo multi-modo con distribuciones reales del sistema."""
import random
random.seed(42)
N = 10_000; MXN = 17.8; CAP = 342.0

MODES = {
    'E3-Anterior':  {'sp_wr':0.462,'sp_aw':0.42,'sp_al':-0.34,'sp_tpm':6.0, 'sh':False},
    'E4-Actual':    {'sp_wr':0.460,'sp_aw':0.60,'sp_al':-0.34,'sp_tpm':15.0,'sh':False},
    'E5-Futuros':   {'sp_wr':0.460,'sp_aw':0.60,'sp_al':-0.34,'sp_tpm':13.0,'sh':True,
                     'sh_wr':0.42,'sh_aw':1.50,'sh_al':-0.625,'sh_tpm':2.0},
}

print('=' * 88)
print('  MONTE CARLO MULTI-MODO (10,000 sims) | Dist. reales del sistema | $342 USDT')
print('  Horizonte: 30 y 90 dias | Compounding mensual incluido')
print('=' * 88)
print()
print('  %-22s %10s %9s %9s | %10s %9s %9s %6s' % (
    'Escenario','30d med','30d P5','30d P95','90d med','90d P5','90d P95','%+90d'))
print('  ' + '-' * 84)

for name, c in MODES.items():
    r30=[]; r90=[]
    for _ in range(N):
        cap=CAP; t90=0.0
        for mo in range(3):
            scale = cap / CAP
            mp = 0.0
            ns = max(1, int(random.gauss(c['sp_tpm'], c['sp_tpm']*0.30)))
            for _ in range(ns):
                mp += (c['sp_aw'] if random.random() < c['sp_wr'] else c['sp_al']) * scale
            if c['sh']:
                nh = max(0, int(random.gauss(c['sh_tpm'], c['sh_tpm']*0.5)))
                for _ in range(nh):
                    mp += (c['sh_aw'] if random.random() < c['sh_wr'] else c['sh_al']) * scale
            cap += mp; t90 += mp
            if mo == 0:
                r30.append(mp)
        r90.append(t90)
    r30.sort(); r90.sort()
    m30   = sum(r30)/N
    p5_30 = r30[int(.05*N)]; p95_30 = r30[int(.95*N)]
    m90   = sum(r90)/N
    p5_90 = r90[int(.05*N)]; p95_90 = r90[int(.95*N)]
    pp    = sum(1 for x in r90 if x > 0) / N * 100
    cap90 = CAP + m90

    print('  %-22s %+10.2f %+9.2f %+9.2f | %+10.2f %+9.2f %+9.2f %5.1f%%' % (
        name, m30, p5_30, p95_30, m90, p5_90, p95_90, pp))
    print('  %-22s %+10.1f %+9.1f %+9.1f | %+10.1f %+9.1f %+9.1f' % (
        '  -> MXN', m30*MXN, p5_30*MXN, p95_30*MXN, m90*MXN, p5_90*MXN, p95_90*MXN))
    print('  %-22s %22s   %10s %+9.2f' % ('  -> Cap final', '', 'USDT media:', cap90))
    print()

print()
print('  PARAMETROS USADOS:')
print('  E3: WR=46.2% | avg_win=$0.42 | avg_loss=-$0.34 |  6 trades/mes (historial real)')
print('  E4: WR=46.0% | avg_win=$0.60 | avg_loss=-$0.34 | 15 trades/mes (BTC.D 64% fix)')
print('       avg_win +43% por mejor R:R: TP 3.5%/SL 1.8% = 1.94x (antes 1.67x)')
print('  E5: Igual E4 en spot (13 t/mes) + 2 shorts/mes en FUTURES_SHORT')
print('       Short: TP 6% * 2x leverage = +$1.50/TP | SL 2.5% * 2x = -$0.625/SL')
print('       Short WR 42%: PRESERVATION > FUTURES en bear con rebotes frecuentes')
print()
print('  FUENTES:')
print('  - avg_win/loss/WR: 39 trades cerrados reales del sistema (base)')
print('  - avg_win E4/E5:   proyectado por mejora R:R (TP 3.5% vs 2.5% -> +40% win)')
print('  - short WR 42%:    backtest_multimode.py validado (bear -10% con rebotes)')
print('=' * 88)
