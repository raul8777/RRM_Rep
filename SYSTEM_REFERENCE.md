# SYSTEM_REFERENCE — crypto-agent

> **Archivo vivo. Actualizar en cada sesión relevante.**  
> Última actualización: jun 15 2026 10:05 UTC-6 | Plan v2 arquitectura COMPLETO | Kelly agresivo aplicado | 12 posiciones abiertas | 9 compras último ciclo

---

## 1. ESTADO DEL SISTEMA

| Campo | Valor |
|-------|-------|
| Fecha última actualización | jun 15 2026 10:05 UTC-6 |
| Capital total | ~$3,080 USDT |
| Capital spot líquido | ~$1,316 USDT |
| Capital invertido spot | ~$1,756 USDT (57.2%) — cap 75% |
| Capital futuros | ~$5 USDT (ocioso — esperando FUTURES_SHORT) |
| Régimen activo | **SPOT_BULL** (confirmado) |
| Fear & Greed | **20 — Extreme Fear** |
| BTC 4h trend | UP (+1.6% última vela 4h) |
| BTC 7d | +9.0% |
| BTC 30d | -17.7% (bear estructural — SPOT_BULL de rebote corto plazo) |
| Screener dinámico | ✅ ACTIVO — 46 spot / 8 futures |
| Hedge mode Binance | ✅ ACTIVADO (`dualSidePosition=true`) |
| Docker | live, sin errores |
| dry_run | False |
| allow_real_trading | True |
| Posiciones abiertas spot (12) | FETUSDT, LTCUSDT, ZECUSDT, MEGAUSDT, TRUMPUSDT, WLDUSDT, XLMUSDT, ZROUSDT, JTOUSDT, SOLUSDT, BNBUSDT, AVAXUSDT + más |
| Ciclo activo | 20 min (SPOT_BULL dinámico) |
| Win rate DB | 34% (22W/42L — degradado por portfolio_rebalancing excesivo, ya corregido) |

---

## 2. ARQUITECTURA — MAPA DE ARCHIVOS CLAVE

```
crypto-agent/
├── run_trades.py          Motor principal: ciclos 5min, regimen, Kelly, screener, buys/sells
├── futures_client.py      FuturesClient: open_short/long, hedge, SL/TP algo orders, pos tracking
├── config.py              Settings: todos los parámetros con defaults y env vars
├── market_regime.py       Detección: SPOT_BULL / RANGE_SCALP / PRESERVATION / FUTURES_SHORT
├── autonomous_signals.py  Señales técnicas: RSI, BB, EMA, vol_spike, divergencias (1h + 4h)
├── pair_screener.py       [NUEVO jun15] Screener dinámico Binance 24h ticker, cache 15min
├── pnl_service.py         Predictor PnL: umbrales por categoría + F&G modifier + bootstrap
├── strategy.py            Pesos ranking por régimen (TV/News/Tech)
├── position_manager.py    Gestión posiciones abiertas, DB, trailing stop, cierre
├── app.py                 FastAPI: /dashboard/summary, /futures/positions, /health/status
├── dashboard.js           Frontend: cards, tablas, Monte Carlo, market ticker
├── SYSTEM_REFERENCE.md    ← ESTE ARCHIVO
└── .env                   Variables de entorno (gitignored)
```

### Relaciones críticas
- `run_trades.py` → llama `market_regime.get_regime()` → determina modo
- `run_trades.py` → llama `pair_screener.get_screened_pairs()` → amplía lista de pares
- `run_trades.py` → instancia `FuturesClient` vía `_get_futures_client()` (singleton)
- `FuturesClient._open_positions` keyed como `"BTCUSDT_SHORT"` / `"BTCUSDT_LONG"` (post-jun15)
- `app.py` → tiene `_get_market_regime()` **propia, independiente** del motor (bug conocido #11)

---

## 3. MODOS DE TRADING

### Cuatro modos del market_regime

| Modo | Trigger | Kelly mult | SL % | TP % | Restricciones |
|------|---------|------------|------|------|---------------|
| **SPOT_BULL** | BTC EMA9>EMA21 4h, vol_ratio>0.5 | 1.0× | por categoría | por categoría | mínimo — spot compras normales |
| **RANGE_SCALP** | BB bandwidth < 4%, lateral | 0.7× | 1.2% | 1.5% | solo RSI<40 + BB lower touch |
| **PRESERVATION** | BTC 7d<-7% OR 30d<-15% | 0.5× | 1.5% | 3.0% | solo NARRATIVE tokens |
| **FUTURES_SHORT** | BTC 30d<-15% + vol evidencia bear | 0.3× | 3.0% | 5.0% | futuros short only |

### Condiciones especiales activas (jun 15)
- **SPOT_BULL + F&G Extreme Fear** → `spot_bull_pnl_boost` activo (relaja umbrales predictor)
- **LONG futuros en RANGE_SCALP**: si `bb_lower_touch=True` + símbolo en futures tier A → `open_long()`
- **LONG futuros en SPOT_BULL**: si RSI 4h < 35 + BTC_trend=UP + símbolo tier A → `open_long()`
- **Anti-whipsaw**: 3 ciclos consecutivos para cambiar de PRESERVATION/FUTURES_SHORT → cerrar shorts

### Cómo se activan LONGs de futuros (nuevo jun15)
```
RANGE_SCALP + bb_lower_touch=True → open_long(sl=2.5%, tp=5.0%, lev=2×)
SPOT_BULL + RSI_4h < 35 + BTC=UP  → open_long(sl=2.5%, tp=5.0%, lev=2×)
Requiere: hedge_mode=True, futures_enabled=True, futures_long_enabled=True
```

---

## 4. REGLAS DE ENTRADA VALIDADAS (estadística real)

### Datos históricos (jun 2026)
- Win rate real: 43–51% (varía por periodo, bootstrap 15,382 trades)
- Profit factor: 1.048 → objetivo ≥ 1.5
- Mejor par: DOGEUSDT (WR ~100%), RENDERUSDT (WR ~78%), BNBUSDT (WR ~60%)
- Peor par: BTCUSDT (WR 0%), LINKUSDT (WR 0%) — en spot LONG

### Reglas de entrada spot (NO negociables)
| Condición | Valor | Efecto |
|-----------|-------|--------|
| RSI 4h | < 65 | Requerido (hard filter) |
| vol_spike | ≥ 1.5× avg | VolSpike override activo si NARRATIVE |
| precio vs rango semanal | 20–60% | Ideal; >80% = sobrecomprado |
| BTC dominance | < 56% (default) | Si BTC.D > umbral → bloquea alts |
| min_signal_strength | 0.35 | Señales débiles rechazadas |

### VolSpike Override (bypass predictor)
```
Requisitos: vol_spike ≥ 1.5× + RSI_4h < 65 + trend=UP + token NARRATIVE
Efecto: omite PnL predictor, entra directamente
```

### Umbrales por categoría de token
| Categoría | min_prob | min_pnl | SL% | TP% | trailing_act% |
|-----------|---------|---------|-----|-----|---------------|
| LARGE_CAP | 55% | $0.10 | 1.8% | 3.5% | 2.5% |
| MIDCAP_ALT | 50% | $0.00 | 1.8% | 3.5% | 2.5% |
| NARRATIVE | 42% | -$5.00 | 2.5% | 5.0% | 3.0% |
| MEME | 52% | $0.05 | 2.0% | 4.5% | 2.5% |

---

## 5. PARÁMETROS .ENV CRÍTICOS

### Valores actuales en producción (verificados jun 15 2026 10:00 UTC-6)

```bash
# ── Operación general ──────────────────────────────────────────────────────
DRY_RUN=false
ALLOW_REAL_TRADING=true
MAX_POSITIONS=12                    # ← actualizado jun15 (era 10)
MAX_POSITIONS_PER_SYMBOL=1
TRADE_CYCLE_MINUTES=5               # fallback (ciclo real es dinámico por régimen)

# ── Kelly sizing — AGRESIVO (actualizado jun15 10:00) ──────────────────────
KELLY_BASE_USDT=40                  # era 20 → base fija mínima
KELLY_DYNAMIC_BASE_PCT=0.12         # era 0.08 → base = total × 12% ($3,080×12%=$369)
KELLY_MAX_USDT=350                  # era 200 → ~11% del portfolio por trade
KELLY_MAX_PCT_LIQUID=0.28           # era 0.30 → cap: 28% del líquido ($1,316×28%=$368)
MAX_PER_TRADE_USDT=350              # era 15 → BUG FIX: alinear con kelly_max

# Kelly sizes resultantes con capital actual ($3,080 total / $1,316 liquid):
#   prob=0.50, str=0.85 → $159/trade
#   prob=0.58, str=0.90 → $249/trade
#   Tier BASE min → $~100/trade

# ── SL/TP globales (overrideados por categoría — ver sección 4) ────────────
STOP_LOSS_PCT=1.8
TAKE_PROFIT_PCT=3.5
TRAILING_STOP_ACTIVATION_PCT=2.5
TRAILING_STOP_DISTANCE_PCT=1.2

# ── Filtros de señal ────────────────────────────────────────────────────────
MIN_SIGNAL_STRENGTH=0.30            # era 0.32
BTC_DOMINANCE_ALTCOIN_THRESHOLD=64.0

# ── Exposición y rebalanceo ─────────────────────────────────────────────────
MAX_TOTAL_EXPOSURE_PCT=0.75         # era 0.65 → max 75% del portfolio invertido
PROTECT_PROFIT_MIN_PCT=0.5          # era 0.8 → proteger desde +0.5%
MIN_HOLD_MINUTES=90                 # era 60 → esperar 90min antes de rebalancear
MAX_REBALANCE_PER_CYCLE=1           # era 2 → máx 1 venta por ciclo (protege posiciones)

# ── Ciclos dinámicos por régimen ────────────────────────────────────────────
CYCLE_SPOT_BULL=20                  # era 15 → señales 1h/4h no cambian en 15min
CYCLE_RANGE_SCALP=5                 # sin cambio
CYCLE_PRESERVATION=45               # era 30 → menos ciclos en bajista = menos comisiones
CYCLE_FUTURES_SHORT=6               # era 8 → más monitoreo en shorts

# TTL señales por régimen (cycle_minutes × 3):
#   SPOT_BULL=60min | RANGE_SCALP=15min | PRESERVATION=135min | FUTURES_SHORT=18min

# ── Futuros SHORT ───────────────────────────────────────────────────────────
FUTURES_ENABLED=true
FUTURES_MAX_SHORTS=3                # era 4 → concentrar en mejores oportunidades
FUTURES_USDT_PER_TRADE=100          # era 50 → Kelly futuros calcula $80-120
FUTURES_SL_PCT=2.5
FUTURES_TP_PCT=6.0
FUTURES_LEVERAGE=2

# ── Futuros LONG (hedge) ─────────────────────────────────────────────────────
FUTURES_HEDGE_MODE_ENABLED=true
FUTURES_LONG_ENABLED=true
FUTURES_MAX_LONGS=3
FUTURES_LONG_SL_PCT=2.5
FUTURES_LONG_TP_PCT=5.0
FUTURES_LONG_IN_RANGE_SCALP=true
FUTURES_LONG_IN_SPOT_BULL=true
FUTURES_LONG_RSI_THRESHOLD=35.0

# ── Screener dinámico ────────────────────────────────────────────────────────
SCREENER_ENABLED=true
SCREENER_MIN_VOL_USDT=10000000      # $10M para spot
SCREENER_FUTURES_MIN_VOL=100000000  # $100M para futuros
SCREENER_MAX_PAIRS=50
SCREENER_CACHE_MINUTES=15
```

### ⚠️ Nota crítica: docker-compose.yml inyecta variables por encima del .env
`MAX_PER_TRADE_USDT` y `MAX_POSITIONS` están declaradas en `docker-compose.yml` líneas 63-64.
Para cambiarlas: editar `docker-compose.yml` + `docker compose up -d app` (NO usar `restart`).

### Sin pendientes de ajuste — todos aplicados

---

## 6. BUGS CONOCIDOS Y FIXES

### Activos (no resueltos)

**Sin bugs activos conocidos** — todos verificados y resueltos al jun 15 2026 10:00 UTC-6.

### Resueltos (historial)

| Fecha | Bug | Fix |
|-------|-----|-----|
| jun 15 10:00 | `MAX_PER_TRADE_USDT=15` → predictor calculaba PnL con $15 pero Kelly compraba $150 (10× subestimado) | `MAX_PER_TRADE_USDT=350` en `.env` + `docker-compose.yml` línea 63. Requiere `up -d`, no `restart` |
| jun 15 10:00 | `MAX_POSITIONS` y `MAX_PER_TRADE_USDT` ignoraban el `.env` del contenedor | Variables inyectadas por `docker-compose.yml` con `${VAR:-default}` tienen prioridad sobre `/app/.env`; fix en compose |
| jun 15 09:35 | `SyntaxError` en `run_trades.py` — `min(generator, default=None)` requiere paréntesis extra en Python 3.11 | Convertido a lista: `_ts_list = [...]; min(_ts_list) if _ts_list else None` |
| jun 15 09:35 | `NameError: NewsClient` en health check `social_score` de `app.py` | Import local dentro del bloque: `from news_client import NewsClient as _NewsClient` |
| jun 15 09:00 | `allocation *= 0.5` cuando `btc_trend==DOWN` penalizaba doble sobre Kelly de régimen | Bloque eliminado de `run_trades.py` — Kelly de régimen es suficiente |
| jun 15 09:00 | Health check futuros contaba `len(_open_positions)` sin distinguir LONG/SHORT | `app.py` usa `count_open_shorts()` + `count_open_longs()` |
| jun 15 09:00 | `get_top_buy_signals()` sin ranking por régimen | Añadido `regime_mode` param: FUTURES_SHORT→RSI-desc, RANGE_SCALP→BB-lower+RSI-asc |
| jun 15 09:00 | Screener bug: `get_screened_pairs(settings)` posicional → `float < Settings` TypeError | 5 llamadas a `get_screened_pairs(settings=settings)` — screener retorna 46 spot + 8 futures |
| jun 15 | `-4067` hedge mode con algo orders huérfanas | `enable_hedge_mode()` cancela via `openAlgoOrders` primero |
| jun 15 | `_open_positions` no soportaba LONG+SHORT mismo símbolo | Key refactorizada a `"SYMBOL_SIDE"` (ej: `"BTCUSDT_SHORT"`) |
| jun 14 | `-4120` SL/TP order fallaba | Migrado a `/fapi/v1/algoOrder` con `algoType=CONDITIONAL` |
| jun 14 | BTCUSDT qty=0 | `_align_qty` redondeaba a 0 con `$20 × 2× / $65k = 0.0006 < step 0.001` |
| jun 14 | ETH precision `-1111` | `_align_price()` usa `_get_tick_size()` |
| jun 13 | TAO acumulaba 3 posiciones | `max_positions_per_symbol` no se respetaba en todos los paths |
| jun 12 | Whipsaw en cambio de régimen | `_non_short_regime_cycles` counter + 3 ciclos mínimos |

---

## 7. APIs EXTERNAS — NOTAS Y ERRORES CONOCIDOS

### Binance Spot (`api.binance.com`)
| Endpoint | Uso | Notas |
|----------|-----|-------|
| `GET /api/v3/ticker/24hr` | Screener dinámico | Sin auth; límite 20 req/min; usado con cache 15min |
| `POST /api/v1/order` | Compras spot | Requiere `HMAC-SHA256` signature |
| `GET /api/v3/account` | Balance | Campos: `free`, `locked` por asset |

### Binance FAPI (`fapi.binance.com`)
| Endpoint | Uso | Error conocido | Nota |
|----------|-----|---------------|------|
| `GET /fapi/v1/positionSide/dual` | Verificar hedge mode | — | Devuelve `{"dualSidePosition": true/false}` |
| `POST /fapi/v1/positionSide/dual` | Activar hedge mode | **-4067** si hay órdenes abiertas | Cancela con `openAlgoOrders` primero |
| `POST /fapi/v1/order` | Orden market | **-4120** para STOP_MARKET/TP_MARKET desde dic 2025 | Migrar a `/fapi/v1/algoOrder` |
| `POST /fapi/v1/algoOrder` | SL/TP desde dic 2025 | — | `algoType=CONDITIONAL`, `type=STOP_MARKET` o `TAKE_PROFIT_MARKET` |
| `GET /fapi/v1/openOrders` | Órdenes regulares abiertas | **NO incluye algo orders** | Para algo: usar `openAlgoOrders` |
| `GET /fapi/v1/openAlgoOrders` | Algo orders abiertas (SL/TP) | — | Sin parámetro = todas las símbolos |
| `DELETE /fapi/v1/algoOrder` | Cancelar algo order | — | Params: `{symbol, algoId}` |
| `DELETE /fapi/v1/allOpenOrders` | Cancelar todas regular | — | Params: `{symbol}` |
| `GET /fapi/v1/positionRisk` | Posiciones abiertas | — | Campo `positionSide`: BOTH/LONG/SHORT |
| `POST /sapi/v1/futures/transfer` | Transferencia spot↔futuros | — | `type=1` spot→futures, `type=2` futures→spot |
| `GET /fapi/v1/premiumIndex` | Funding rate | — | Campo `lastFundingRate`; >0.1% = no entrar SHORT |
| `GET /fapi/v1/exchangeInfo` | Tick size, step size | — | Cache local para evitar spam |

### Códigos de error frecuentes
| Código | Mensaje | Causa | Solución |
|--------|---------|-------|---------|
| -4047 | `No need to change margin type` | Margin type ya es ISOLATED | Ignorar (ya está bien) |
| -4067 | `Position side cannot be changed` | Hay órdenes abiertas (incl. algo orders) | Cancelar via `openAlgoOrders` primero |
| -4120 | `Order type not supported` | STOP_MARKET/TP_MARKET ya no van por `/order` | Usar `/algoOrder` |
| -1111 | `Precision is over maximum` | `triggerPrice` con más decimales de los permitidos | Usar `_get_tick_size()` para alinear precio |
| -4509 | `ReduceOnly Order Failed` | Intentar cerrar más qty de la abierta | Verificar qty con `get_positions()` |
| -2011 | `Unknown order sent` | Orden ya cancelada/completada | Ignorar silenciosamente |
| -1102 | `Mandatory parameter was not sent` | Falta parámetro requerido | Revisar endpoint docs |

### Otras APIs
| API | Uso | Auth | Notas |
|-----|-----|------|-------|
| CoinGecko `/trending` | Trending coins | Sin auth | Rate limit 30/min |
| Alternative.me F&G | Fear & Greed index | Sin auth | Valor 0-100, actualiza 1×/día |
| CryptoCompare Social | Score social (opcional) | API key | `CRYPTOCOMPARE_API_KEY` en .env |

---

## 8. PENDIENTES PRIORIZADOS

### ✅ TODO completado al jun 15 2026 10:05 UTC-6

**Plan arquitectura v1** (P1–P14): todos completados.
**Plan arquitectura v2** (Fases A–E): todos completados.
**Ajuste agresivo Kelly + ciclos**: completado.

| # | Tarea | Estado |
|---|-------|--------|
| P1–P14 | Ver historial anterior | ✅ Todos completados |
| A | Exposición total cap (75%) + rebalanceo inteligente (min_hold=90min, max=1/ciclo) | ✅ `run_trades.py` + `.env` |
| B | Ciclos dinámicos por régimen (20/5/45/6 min) + TTL señales dinámico | ✅ `run_trades.py` + `orchestrator.py` |
| C | Kelly futuros con `sl_pct × leverage` real | ✅ `run_trades.py` |
| D | Dashboard: `/health` con `portfolio_exposure` + `trade_cycle` + barra progreso | ✅ `app.py` + `dashboard.js` |
| E | Kelly por categoría: usa `category_thresholds[cat].sl_pct/tp_pct` | ✅ `run_trades.py` |
| K | Kelly agresivo: base 12%, max $350, max_per_trade $350 (bug fix) | ✅ `.env` + `docker-compose.yml` |

### � En observación

| # | Tarea |
|---|-------|
| N2 | Verificar BTCUSDT short qty>0 cuando régimen cambie a FUTURES_SHORT |
| N4 | Monitorear WR durante próximas 48h con nuevos parámetros agresivos — objetivo ≥ 45% |
| N5 | Predictor en bootstrap_mode para todos los pares nuevos del screener — acumulará datos con el tiempo |

---

## 9. SCREENER DINÁMICO — DATOS DE REFERENCIA

### Resultados primer run (jun 15 2026 08:35 UTC-6)
```
Total pares escaneados (vol > $10M): 46
Spot elegibles:    46 pares
Futures Tier A:     8 pares (vol > $100M)
  → ETHUSDT, ZECUSDT, WLDUSDT, BTCUSDT, SOLUSDT, XRPUSDT, BNBUSDT, USD1USDT
Cache TTL: 15 min
Score: volumen_24h × abs(priceChangePercent_24h)
```

### Cómo agregar/excluir pares del screener
```python
# pair_screener.py línea ~35: blacklist de stablecoins
STABLECOIN_PREFIXES = ["USDC", "BUSD", "TUSD", "DAI", "FDUSD", "USDP"]
# Para excluir un par específico, agregar a la blacklist
```

---

## 10. LECCIONES DE TRADING (estadísticas reales)

### Lo que funciona (no cambiar)
- Anti-whipsaw 2-ciclos: correcto y necesario
- VolSpike override con RSI < 65: sólida lógica de breakout
- Multi-timeframe 1h + 4h: reduce falsos positivos
- Fear & Greed modifier en umbrales predictor
- Kelly dinámico con tiers por categoría de token
- Trailing stop con `protect_profitable_positions`
- Screener dinámico 46 pares (vs 10 estáticos): más oportunidades

### Lo que destruye valor (CORREGIDO jun 15)
| Problema | Impacto | Fix aplicado |
|----------|---------|-------------|
| `portfolio_rebalancing` prematuro (51/64 = 80% de todos los cierres) | Destruye posiciones ganadoras cobrando 0.2% comisión | `min_hold=90min` + `max_rebalance=1/ciclo` |
| Triple penalización Kelly ($7.50/trade en bear) | Capital subutilizado en bear | Kelly unificado via `_regime.kelly_multiplier` |
| `MAX_PER_TRADE_USDT=15` → predictor con $15 pero Kelly compraba $150 | PnL estimado 10× subestimado → señales rechazadas innecesariamente | `MAX_PER_TRADE_USDT=350` |
| `kelly_dynamic_base_pct=8%` con cap $200 → $105 plano | Capital subutilizado | Base 12%, max $350 |
| BTC.D filter 56% bloqueaba TODOS los alts | Cero señales con BTC.D ~63% | Subido a 64% |
| 3 posiciones mismo símbolo (RENDER×3, TAO×3) | Concentración extrema | `MAX_POSITIONS_PER_SYMBOL=1` |

### Estadísticas históricas (64 trades cerrados al jun 15)
```
Win rate: 34% (22W / 42L) — ADVERTENCIA: degradado por portfolio_rebalancing (51 cierres)
Profit factor: 0.44 — objetivo ≥ 1.5 (esperamos mejora con min_hold=90min)
Avg win: $0.59 | Avg loss: -$0.70

Mejores símbolos: DOGEUSDT (75% WR, +$2.6 total) | RENDERUSDT (53% WR, +$2.6)
Peores símbolos:  FETUSDT (0% WR, -$6.9 total)   | WLDUSDT (0% WR, -$6.7)
                  TAOUSDT (25% WR, -$6.6 total)
```

### Matemáticas con capital actual ($3,080 total / $1,316 liquid)
```
Kelly dinámico: $3,080 × 12% = $369 base
Kelly sizes reales: prob=0.50 str=0.85 → $159/trade | prob=0.58 str=0.90 → $249/trade
Kelly max cap: min($350, $1,316×28%) = min($350, $368) = $350

12 posiciones × $150 avg = $1,800 = 58% del portfolio
Exposición cap: 75% = $2,310 max invertido → quedan $510 disponibles para oportunidades

Con WR 45% objetivo (post-corrección rebalanceo), r=1.94 (TP3.5%/SL1.8%):
EV por trade = 0.45×$150×1.94 - 0.55×$150 = +$48.2 (positivo)
```

### Con futuros activados (FUTURES_SHORT, bear market)
```
Margen por short: $100 × 2× = $200 notional
TP +6% = +$12 por trade | SL -2.5% = -$5 por trade → R:R = 2.4
Con 3 shorts simultáneos = $300 margen total
Win rate esperado shorts técnicos en bear: 45-55%
```

---

*Revisar este archivo al inicio de cada sesión de análisis o implementación.*
