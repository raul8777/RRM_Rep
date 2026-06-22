# SAFEPOINT v1 — Sistema Productivo (jun 22 2026)

> Marca de referencia: el sistema comenzó a ser consistentemente productivo.
> Usar para comparar rendimiento futuro y restaurar comportamiento si el sistema decae.

---

## Git Tag de Restauración

```bash
# Ver estado exacto de este punto
git show safepoint-v1-jun22-2026

# Restaurar solo docker-compose.yml (config)
git checkout safepoint-v1-jun22-2026 -- docker-compose.yml
docker-compose up -d --force-recreate app

# Restaurar código completo
git checkout safepoint-v1-jun22-2026
docker-compose up -d --force-recreate app
```

**Commit base**: `b078c03` — `feat: Maximize futures slot utilization`
**Tag**: `safepoint-v1-jun22-2026`

---

## Estado del Sistema al Momento del Safepoint

### Régimen y Contexto de Mercado
- **Modo**: `FUTURES_SHORT` — Bear estructural confirmado
- **BTC**: 30d = -17.0%, 7d = -2.6%, EMA9 > EMA21
- **F&G Index**: 20 (Extreme Fear) — contexto ideal para shorts
- **USDT disponible**: ~$1,737 spot + margen futures separado

### Posiciones FUTURES Activas (11 SHORTs)

| Symbol | Entry | SL | TP | SL% |
|--------|-------|----|----|-----|
| TAOUSDT | 231.4000 | 231.4000 | 216.3300 | 7% |
| FILUSDT | 0.7830 | 0.8370 | 0.7360 | 7% |
| DOTUSDT | 0.9320 | 0.9980 | 0.8820 | 7% |
| BCHUSDT | 199.4960 | 199.5000 | 185.5200 | 7% |
| XRPUSDT | 1.1322 | 1.2116 | 1.0599 | 7% |
| LINKUSDT | 7.8520 | 8.4030 | 7.3370 | 7% |
| SOLUSDT | 72.0100 | 77.0500 | 68.7600 | 7% |
| AVAXUSDT | 6.1850 | 6.3980 | 5.7940 | 7% |
| UNIUSDT | 3.0040 | 3.0040 | 2.8340 | 7% |
| DOGEUSDT | 0.0829 | 0.0829 | 0.0771 | 7% |
| ADAUSDT | 0.1574 | 0.1686 | 0.1517 | 7% |

---

## Configuración Exacta (Parámetros Clave)

### FUTURES
| Parámetro | Valor | Notas |
|-----------|-------|-------|
| FUTURES_MAX_SHORTS | **12** | subido de 9 en esta sesión |
| FUTURES_OVERFLOW_MAX_EXTRA | 3 | → 15 total máximo |
| FUTURES_OVERFLOW_MIN_STRENGTH | **0.55** | bajado de 0.65 |
| FUTURES_SL_PCT | 7.0% | subido de 5% (SL-OPT) |
| FUTURES_TP_PCT | 7.0% | simétrico con SL |
| FUTURES_LEVERAGE | 2x | hard cap |
| FUTURES_USDT_PER_TRADE | 50.0 | piso mínimo |
| FUTURES_KELLY_CAP_PCT | 0.20 | 20% del margen disponible |
| FUTURES_SL_COOLDOWN_HOURS | 4.0 | pausa post-SL por símbolo |
| FUTURES_MASS_SL_COOLDOWN_HOURS | 4.0 | pausa global si 3+ SL simultáneos |
| FUTURES_MAX_NEW_PER_CYCLE | 3 | stagger: máx 3 nuevos/ciclo |
| FUTURES_SYMBOLS | 19 pares | incluye 1000PEPEUSDT (fix) |
| FUTURES_LARGECAP_SYMBOLS | BTC/ETH/BNB/XRP/SOL | |
| FUTURES_MAX_LARGECAP_SHORTS | 5 | diversificación forzada |

### SPOT
| Parámetro | Valor | Notas |
|-----------|-------|-------|
| STOP_LOSS_PCT | 2.5% | subido de 1.8% (SL-OPT) |
| TAKE_PROFIT_PCT | 3.5% | |
| ATR_SL_ENABLED | True | |
| ATR_SL_MULTIPLIER | 1.5 | |
| ATR_SL_MIN_PCT | 1.5% | floor subido de 1.0% |
| MAX_POSITIONS_SPOT_BULL | 20 | |
| MAX_POSITIONS_RANGE_SCALP | 10 | |
| OVERFLOW_MAX_EXTRA | 5 | overflow SPOT |
| MAX_PER_TRADE_USDT | 350.0 | |

### SSO (Smart Scale-Out)
| Parámetro | Valor | Trigger |
|-----------|-------|---------|
| SSO_DECAY_THRESHOLD_PCT | 0.004 (0.4%) | SSO-1b |
| SSO_DECAY_MIN_PROFIT_USD | $2.0 | SSO-1b |
| SSO_DECAY_MIN_HOURS | 1.5h | SSO-1b |
| SSO_SLOW_VELOCITY_THRESHOLD | 0.001 (0.1%/h) | SSO-1c T1 |
| SSO_SLOW_MIN_PROFIT_USD | $1.0 | SSO-1c |
| SSO_SLOW_MIN_HOURS | 2.0h | SSO-1c T1 |
| SSO_SLOW_TIER2_HOURS | 4.0h | SSO-1c T2: 0.15%/h |
| SSO_SLOW_TIER3_HOURS | 6.0h | SSO-1c T3: 0.35%/h |
| SSO_SLOW_TIER4_HOURS | 8.0h | SSO-1c T4: 0.50%/h |

---

## Sistemas y Features Activos en Este Punto

| Sistema | Estado | Descripción |
|---------|--------|-------------|
| SSO-1b Decay | ✅ Activo | Cierra si profit cae 0.4% desde peak |
| SSO-1c Slow Crawl | ✅ Activo | 4 tiers de velocidad mínima |
| SSO-1 Stagnant | ✅ Activo | velocity≤0 por 2h+ |
| SSO-2/3/4/5 | ✅ Activo | Momentum, ATR multilevel, target, upgrade |
| Binance Order Verify | ✅ Activo | 9 paths SPOT + 4 FUTURES |
| Futures Overflow | ✅ Simplificado | Solo strength + chg24 (sin vol_spike/BTC gate) |
| Kelly Sizing | ✅ Activo | Dinámico por tier convicción |
| Mass SL Circuit Breaker | ✅ Activo | Cooldown global si 3+ SL |
| SL Cooldown por Símbolo | ✅ Activo | 4h pausa post-SL |
| Largecap Diversification | ✅ Activo | Máx 5 large-cap shorts |
| PB1 Stagger | ✅ Activo | Máx 3 nuevos shorts/ciclo |
| PB3 Red Candle | ✅ Activo | Confirma vela roja antes de SHORT |
| PB5 BTC 3h Momentum | ✅ Activo | No short si BTC subió 1%+ en 3h |
| _verify_position_closed | ✅ Activo | Confirma cierre en Binance antes de DB |

---

## Resultados SSO Realizados (Primeras Horas)

| Símbolo | PnL | Trigger |
|---------|-----|---------|
| SUIUSDT | +$3.77 | SSO-1b profit_decay |
| ETHUSDT | +$1.75 | SSO-1c slow_crawl T2 |
| AVAXUSDT | +$1.74 | SSO-1c slow_crawl T2 |
| UNIUSDT | +$2.18 | SSO-1b profit_decay |
| UNIUSDT | +$1.05 | SSO-1 sso_slow_crawl |
| **TOTAL** | **~$10.49** | 5 cierres, 5 slots liberados |

---

## Baseline de Rendimiento (Pre-Safepoint)

Datos del análisis de 19 jun 2026 (pre-SSO):
- **Win Rate**: 20% (2/8 trades recientes)
- **PnL últimos 10**: -$38.17
- **Avg SL hit**: -$3.76 / -2.64% en SPOT
- **35 SL = -$120.91** = 53% de todas las pérdidas

**Expectativa post-SSO**: Win Rate ≥ 40%, avg PnL/trade > $0, menos SL (2.5% vs 1.8%)

---

## Señales de Degradación (Cuándo Comparar vs Este Safepoint)

| Señal | Umbral de Alerta | Acción |
|-------|-----------------|--------|
| Win Rate futures | < 40% en 48h | Revisar signal injection + overflow |
| Avg PnL/trade | < $0 (5+ trades) | Comparar config vs este safepoint |
| SL hits en 24h | > 3 SL simultáneos | Circuit breaker activo, revisar max_shorts |
| F&G > 60 en FUTURES_SHORT | — | Revisión urgente del regime engine |
| SSO firing sin profit | > 5 veces sin PnL | Calibrar SSO thresholds |
| Slots siempre llenos sin TP | > 24h sin un TP | Revisar TP%, SL%, leverage |

---

## Tests en Este Punto
- **98 passed, 0 failures**
- `tests/test_futures_smart_exits.py` (72 tests)
- `tests/test_profit_boost_entries.py` (26 tests)

```bash
# Comando para verificar integridad
docker-compose exec app python -m pytest tests/test_futures_smart_exits.py tests/test_profit_boost_entries.py -q
```

---

## Archivos Clave (Últimas Sesiones Pre-Safepoint)

| Archivo | Cambio Principal |
|---------|-----------------|
| `smart_scale_out.py` | SSO engine completo (5 triggers) |
| `futures_client.py` | _verify_position_closed, SSO integration, DB persist fix |
| `binance_client.py` | verify_order_filled (SPOT verification) |
| `run_trades.py` | 9 SPOT verify paths, overflow simplificado, SSO spot |
| `config.py` | 22+ nuevos params SSO, overflow, SL-OPT |
| `docker-compose.yml` | Todos los env vars — snapshot de referencia |
| `SYSTEM_REFERENCE.md` | Historial completo de sesiones |
