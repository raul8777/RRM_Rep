# SYSTEM_REFERENCE — crypto-agent

> **Archivo vivo. Actualizar en cada sesión relevante.**  
> Última actualización: jun 15 2026 21:05 UTC-6 | MC dashboard 3 escenarios A/B/C + profit_projection corregido (trades normales) | análisis oportunidades perdidas jun15 | commit 68b7124

---

## 0. ROLES Y CONTEXTO DE ANÁLISIS

> **Todo análisis, diseño e implementación en este proyecto debe adoptarse desde los siguientes roles simultáneamente:**

| Rol | Aplicación en este sistema |
|-----|---------------------------|
| **Experto en criptomonedas** | Conocimiento de mercados, tokenomics, ciclos macro BTC/alts, narrativas, liquidez |
| **Behavioral Finance** | F&G como señal de tamaño de posición: capitulación (+50% Kelly), greed extremo (-35% Kelly) |
| **Actuario / Riesgo sistémico** | Circuit breaker técnico (5 fallos API = pausa 10min), no bloquea por pérdidas de mercado |
| **Microestructura de mercados** | Filtro volumen 24h ≥$3M + slippage implícito ≤ 25% del TP antes de entrar |
| **Teoría de portafolios (correlaciones)** | Limite por bucket: LARGE_CAP≤3, MIDCAP≤5, NARRATIVE≤4, MEME≤2 — diversificación forzada |
| **On-chain / microestructura derivados** | Funding rate spot filter: si FR>0.08% = longs sobreapalancados = skip compra |
| **Experto en trading** | Gestión de riesgo, Kelly sizing, SL/TP, R:R, position sizing, rebalanceo, señales técnicas |
| **Experto en estadísticas** | Win rate, profit factor, EV por trade, distribuciones, bootstrap, Monte Carlo, sesgo en datos |
| **Experto en inversiones y tendencias** | Macro BTC dominance, Fear & Greed, momentum sectorial, rotación de capital |
| **Experto en hacking y seguridad** | Protección de API keys, validación de inputs, exposure de endpoints, secrets en Docker/env |
| **Experto en performance de BD** | Queries eficientes, índices, volumen de escrituras (ciclos cada 5-20min × 24h), conexiones pool |
| **Experto en investigación de operaciones** | Optimización de parámetros, simulación, modelado de sistemas, decisiones bajo incertidumbre |
| **Experto en Machine Learning e IA** | Modelos predictivos (PnL predictor), features engineering, bootstrap vs overfitting, validación de modelos, mejora continua con datos reales |
| **Diseñador Gráfico** | UI/UX del dashboard, jerarquía visual, colores de alerta, legibilidad de tablas y métricas, experiencia del usuario en tiempo real |

> Ante cualquier propuesta de cambio: evaluar impacto desde **todos** los roles antes de implementar.

---

## 1. ESTADO DEL SISTEMA

| Campo | Valor |
|-------|-------|
| Fecha última actualización | jun 15 2026 21:05 UTC-6 |
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
| Docker | live, sin errores — commit 68b7124 |
| dry_run | False |
| allow_real_trading | True |
| Posiciones abiertas spot (12) | FETUSDT, LTCUSDT, ZECUSDT, MEGAUSDT, TRUMPUSDT, WLDUSDT, XLMUSDT, ZROUSDT, JTOUSDT, SOLUSDT, BNBUSDT, AVAXUSDT + más |
| Ciclo activo | 20 min (SPOT_BULL dinámico) |
| Win rate DB | 28% (107 trades — WR real normales: 0%/15 trades; rebalanceos post-fix: WR 53.6%/pool B) |

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
├── app.py                 FastAPI: /dashboard/summary, /futures/positions, /health/status, MC 3 escenarios A/B/C
├── dashboard.js           Frontend: cards, tablas, Monte Carlo 3 escenarios, barras comparativas, badge ahorro
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

# ── Dynamic max positions por régimen (jun 15 12:56) ─────────────────────────
MAX_POSITIONS_SPOT_BULL=15          # default: 15 (amplio en bull + 46 pares disponibles)
MAX_POSITIONS_RANGE_SCALP=8         # default: 8  (scalping controlado)
MAX_POSITIONS_PRESERVATION=5        # default: 5  (solo NARRATIVE en bajista)
MAX_POSITIONS_FUTURES_SHORT=4       # default: 4  (alineado con FUTURES_MAX_SHORTS=3)
# MAX_POSITIONS=12 se mantiene como FALLBACK si régimen no disponible

# ── Reemplazo activo de posición débil ───────────────────────────────────────
REPLACE_ONLY_LOSING=true            # solo reemplaza posiciones en pérdida/flat
REPLACE_EV_MIN_RATIO=1.3            # nuevo EV debe ser ≥ peor_actual × 1.3
REPLACE_STRENGTH_MIN_DELTA=0.15     # nueva strength debe superar promedio open en +0.15

# ── Overflow slots (señales excepcionales beyond max) ───────────────────────
OVERFLOW_MIN_STRENGTH=0.80          # Kelly FUERTE tier: str ≥ 0.80
OVERFLOW_MIN_PROB=0.65              # Kelly FUERTE tier: prob ≥ 0.65
OVERFLOW_MAX_EXTRA=3                # máximo 3 slots extra simultáneos

# ── Behavioral Finance: F&G Kelly boost (R1) ─────────────────────────────────
FNG_KELLY_BOOST_ENABLED=true
FNG_CAPITULATION_THRESHOLD=15       # F&G ≤ 15 → capitulación total
FNG_EXTREME_FEAR_THRESHOLD=20       # F&G ≤ 20 → miedo extremo (actual: 20)
FNG_EXTREME_GREED_THRESHOLD=75      # F&G ≥ 75 → euforia peligrosa
FNG_CAPITULATION_MULTIPLIER=1.50    # +50% Kelly en capitulación
FNG_EXTREME_FEAR_MULTIPLIER=1.35    # +35% Kelly en miedo extremo
FNG_EXTREME_GREED_MULTIPLIER=0.65   # -35% Kelly en euforia
FNG_CAPITULATION_PNL_BOOST=-0.25    # relax min_pnl hasta -$0.25 en capitulación

# ── Microestructura (R3) ──────────────────────────────────────────────────────
LIQUIDITY_FILTER_ENABLED=true
MIN_LIQUIDITY_VOL_USDT=3000000      # $3M vol 24h mínimo para spot (todos los pares screener pasan)
MAX_SLIPPAGE_TP_RATIO=0.25          # spread implícito no puede consumir > 25% del TP

# ── Correlaciones por categoría (R4) ─────────────────────────────────────────
CATEGORY_MAX_POSITIONS_ENABLED=true
# category_max_positions: {"LARGE_CAP":3,"MIDCAP_ALT":5,"NARRATIVE":4,"MEME":2} — en config.py defaults

# ── Circuit breaker técnico (R5) ──────────────────────────────────────────────
CIRCUIT_BREAKER_ENABLED=true
CIRCUIT_BREAKER_CONSECUTIVE_FAILURES=5   # 5 errores API consecutivos → pausa
CIRCUIT_BREAKER_PAUSE_MINUTES=10         # pausa de 10 min (buys sólo, SL/TP no se ven afectados)

# ── On-chain funding rate (R6) ────────────────────────────────────────────────
FUNDING_RATE_SPOT_FILTER_ENABLED=true
FUNDING_RATE_SPOT_MAX=0.0008        # > 0.08% = longs sobreapalancados → skip spot buy
FUNDING_RATE_CACHE_MINUTES=10       # cache funding rate 10min (fapi.binance.com)
```

### ⚠️ Nota crítica: docker-compose.yml inyecta variables por encima del .env
`MAX_PER_TRADE_USDT` y `MAX_POSITIONS` están declaradas en `docker-compose.yml` líneas 63-64.
Para cambiarlas: editar `docker-compose.yml` + `docker compose up -d app` (NO usar `restart`).
Los nuevos params D1–D4 (MAX_POSITIONS_SPOT_BULL, etc.) solo tienen defaults en `config.py`.
Para sobreescribir desde Docker: agregar al `docker-compose.yml` como `- VAR=${VAR:-default}`.

### Sin pendientes de ajuste — todos aplicados

---

## 6. BUGS CONOCIDOS Y FIXES

### Activos (no resueltos)

**Sin bugs activos conocidos** — todos verificados y resueltos al jun 15 2026 20:40 UTC-6.

### Resueltos (historial)

| Fecha | Bug | Fix |
|-------|-----|-----|
| jun 15 20:40 | `_sell_holdings_not_in_top()` vendía TODO lo que no estaba en `top_symbols` del ciclo de 20min → win rate rebalanceo 18% (49/60 pérdidas = -$63.56 en un día). Root cause: criterio "no está en top" es ruido, no señal de salida | **Opción B**: rebalanceo solo si RSI1h>60 O EMA9 descendente + min_hold=240min. Nuevo param `rebalance_rsi_overbought=60`. `run_trades.py` + `config.py` commit ffc5a75 |
| jun 15 20:40 | Timezone bug en `min_hold_minutes`: `entry_time` naive datetime vs `datetime.now(timezone.utc)` aware → comparación lanzaba excepción silenciosa (try/except pasaba), protección ignorada completamente → posiciones de 9-20min se rebalanceaban | Fix A: `_entry_ts.replace(tzinfo=timezone.utc)` si `tzinfo is None`. `run_trades.py:745` commit 4639c2c |
| jun 15 20:40 | Posiciones en pérdida cerca del SL cerradas por rebalanceo antes de que SL checker actuara (competencia entre dos mecanismos) | Fix D SL-Delegated: si `abs(pnl_pct) >= SL × 0.75` → diferir al SL checker, no rebalancear. `run_trades.py:771` commit 4639c2c |
| jun 15 13:47 | Dashboard mostraba 0 posiciones cerradas — `close_position()` guardaba `exit_time` como datetime naive (sin UTC) vs DB que retorna aware; comparación en `calculate_performance_metrics` y `get_closed_positions` lanzaba `TypeError: can't compare offset-naive and offset-aware datetimes`. `_get_monte_carlo_projection` pasaba objetos `ClosedPosition` a función que esperaba dicts `t["pnl"]` | Fix: `datetime.now(timezone.utc).isoformat()` en `position_manager.py:447` + conversión a dicts en `app.py:741`. `83 trades` ahora visibles. |
| jun 15 11:41 | `_STALE_CYCLE_MINUTES=15` hardcoded → falso positivo con SPOT_BULL 15min; warning spameaba error_collector en cada poll | Fix: umbral dinámico `orchestrator.current_cycle_minutes × 1.5` + dedup por `last_cycle` timestamp. `app.py` línea ~1500 |
| jun 15 11:41 | `portfolio_manager.get_position_predictions()` usaba `trade_cycle_minutes=5` fijo → predicción a 5min en vez del ciclo real 15min SPOT_BULL → sesgo contra posiciones abiertas en prediction-reallocation | Fix: usa `orchestrator.current_cycle_minutes` con fallback a config. `portfolio_manager.py` línea ~351 |
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

### ✅ TODO completado al jun 15 2026 12:56 UTC-6

**Plan arquitectura v1** (P1–P14): todos completados.
**Plan arquitectura v2** (Fases A–E): todos completados.
**Ajuste agresivo Kelly + ciclos**: completado.
**Dashboard redesign v18**: completado.
**Smart exit re-evaluation**: completado.
**Dynamic max positions + replacement + overflow**: completado.
**Nuevos roles R1-R6 (Behavioral Finance, Microestructura, Correlaciones, Circuit Breaker, On-chain)**: completado.

| # | Tarea | Estado |
|---|-------|--------|
| P1–P14 | Ver historial anterior | ✅ Todos completados |
| A | Exposición total cap (75%) + rebalanceo inteligente (min_hold=90min, max=1/ciclo) | ✅ `run_trades.py` + `.env` |
| B | Ciclos dinámicos por régimen (20/5/45/6 min) + TTL señales dinámico | ✅ `run_trades.py` + `orchestrator.py` |
| C | Kelly futuros con `sl_pct × leverage` real | ✅ `run_trades.py` |
| D | Dashboard: `/health` con `portfolio_exposure` + `trade_cycle` + barra progreso | ✅ `app.py` + `dashboard.js` |
| E | Kelly por categoría: usa `category_thresholds[cat].sl_pct/tp_pct` | ✅ `run_trades.py` |
| K | Kelly agresivo: base 12%, max $350, max_per_trade $350 (bug fix) | ✅ `.env` + `docker-compose.yml` |
| U1 | Dashboard redesign v18: Resumen Ejecutivo, KPI row, scroll errores, colapsables | ✅ `index.html` + `dashboard.js` + `styles.css` |
| U2 | Stale cycle warning: umbral dinámico × 1.5 + dedup una vez por ciclo | ✅ `app.py` |
| U3 | Smart exit — Fix A: anti-wick SL (confirma último candle 1m antes de cerrar) | ✅ `run_trades.py` + `config.py` |
| U4 | Smart exit — Fix B: horizonte predicción dinámico en prediction-reallocation | ✅ `portfolio_manager.py` |
| U5 | Smart exit — Fix C: rebalanceo consciente del predictor (epnl>0 + prob≥0.55 → hold) | ✅ `run_trades.py` + `config.py` |
| D1 | Dynamic max positions: `_get_max_positions_for_regime()` — SPOT_BULL=15, RANGE_SCALP=8, PRESERVATION=5, FUTURES_SHORT=4 | ✅ `run_trades.py` + `config.py` |
| D2 | Selector buffer +5: siempre evalúa candidatos de reemplazo aunque límite lleno | ✅ `run_trades.py` |
| D3 | Reemplazo activo `_try_replace_weakest_position()`: cierra peor EV si nueva señal supera ratio 1.3× + solo en pérdida | ✅ `run_trades.py` |
| D4 | Overflow slots `_is_overflow_eligible()`: abre slot extra si strength≥0.80 + prob≥0.65 + epnl>0 (max 3) | ✅ `run_trades.py` |
| D5 | `orchestrator.current_regime_mode` sincronizado por ciclo; `/health/status` expone `max_positions_current` + `overflow_slots_used` | ✅ `orchestrator.py` + `app.py` |
| D6 | Dashboard: badge ⚡OVF naranja en posiciones overflow; health `trade_cycle` muestra régimen + max + slots | ✅ `dashboard.js` + `styles.css` |
| R1 | F&G Kelly boost: Capitulacion(≤15)×1.50, Extreme Fear(≤20)×1.35, Extreme Greed(≥75)×0.65 | ✅ `run_trades.py` + `config.py` |
| R2 | Capitulacion PnL boost: `pnl_service.validate()` ya usa `fng_modifier` agresivo de `news_client` | ✅ `pnl_service.py` (preexistente + config) |
| R3 | Microestructura: `_check_liquidity_and_slippage()` — vol_24h<$3M o spread>25% TP → skip | ✅ `run_trades.py` + `config.py` |
| R4 | Correlaciones: `_check_category_limit()` — LARGE_CAP:3, MIDCAP_ALT:5, NARRATIVE:4, MEME:2 | ✅ `run_trades.py` + `config.py` |
| R5 | Circuit breaker técnico: 5 fallos API consecutivos → pausa 10min (no bloquea por pérdidas) | ✅ `run_trades.py` + `app.py` + `dashboard.js` |
| R6 | On-chain funding rate: `_get_funding_rate()` cacheado 10min, skip si FR>0.08% en SPOT_BULL | ✅ `run_trades.py` + `config.py` |

### � En observación

| # | Tarea |
|---|-------|
| N2 | Verificar BTCUSDT short qty>0 cuando régimen cambie a FUTURES_SHORT |
| N4 | Monitorear WR durante próximas 48h con nuevos parámetros agresivos — objetivo ≥ 45% |
| N5 | Predictor en bootstrap_mode para todos los pares nuevos del screener — acumulará datos con el tiempo |
| N6 | Monitorear que nuevo rebalanceo quirúrgico (RSI>60 O EMA9↓) no retiene posiciones en caída real sostenida — revisar logs `[Rebalance] Technical sell confirmed` vs `Holding: no technical sell signal` |
| N7 | Monitorear que Fix A (anti-wick candle 1m) no retrase SL en caídas reales > 2min |
| N8 | Primera activación de reemplazo activo: verificar logs `[Replace]` con ratio correcto y que no vende posiciones en profit |
| N9 | Primera activación overflow: verificar logs `[Overflow]` con strength/prob correctos; monitorear si las 3 posiciones extra son rentables |
| N10 | SPOT_BULL=15 posiciones: verificar que `max_total_exposure_pct=0.75` actúa como verdadero techo; Kelly sizes deben ser ~$80-100 con 15 posiciones |
| N11 | Primera activación F&G Kelly boost: con F&G=20 (Extreme Fear actual) debería boost ×1.35 en cada buy |
| N12 | Filtro liquidez R3: verificar que no bloquea pares del screener (todos deben tener vol>$3M) |
| N13 | Filtro categorias R4: con 12 posiciones abiertas verificar distribución MEME≤2 / NARRATIVE≤4 |
| N14 | Circuit breaker: verificar en logs que solo se activa por errores API, NO por pérdidas de mercado |
| N15 | Verificar win rate real con 83 trades en dashboard — actualmente 29.5% (bajo por rebalanceos). Monitorear si sube con nuevos filtros R1-R6 |
| N16 | Monitorear rebalanceos próximas 24h: objetivo reducir de 60/día → 5-8/día. Log clave: `[Rebalance] Holding: no technical sell signal` |
| N17 | Verificar que posiciones antiguas (>4h) con RSI>60 sí se rebalancean correctamente cuando aparece nueva señal de mayor EV |
| N18 | WR trades normales (SL/TP) es 0% en 15 trades — investigar si SL está demasiado ajustado o entries en mal timing. Candidato: ATR-based SL para NARRATIVE |
| N19 | Trailing stop poco agresivo para breakouts grandes: JTOUSDT +29% requería activación +5%/trail 2%. Revisar `trailing_activation_pct` NARRATIVE (actual +3%) |

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

### Nuevas protecciones implementadas (jun 15 2026 11:41)
| Mejora | Descripción | Archivo |
|--------|-------------|--------|
| **Anti-wick SL** | Antes de cerrar por SL, verifica último candle 1m. Si cerró sobre el SL → wick → no vende. Param: `sl_confirmation_candles=1` | `run_trades.py` |
| **Horizonte dinámico predictor** | `get_position_predictions()` usa `orchestrator.current_cycle_minutes` (15min SPOT_BULL) en vez del fijo 5min de config | `portfolio_manager.py` |
| **Rebalanceo consciente** | Si predictor dice `epnl > 0 AND prob ≥ 0.55` → no vende en rebalanceo aunque no esté en top-N. Param: `rebalance_skip_if_positive_prediction=True` | `run_trades.py` |
| **Stale cycle dedup** | Warning `TRADING_CYCLE` se registra una sola vez por ciclo estancado, no en cada poll del health (era 15×/ciclo) | `app.py` |

### Lo que destruye valor (CORREGIDO jun 15)
| Problema | Impacto | Fix aplicado |
|----------|---------|-------------|
| `portfolio_rebalancing` quirúrgico — Opción B (jun 15 20:40) | 60 rebalanceos/día, WR=18%, -$63.56 en un día. Criterio "no está en top_symbols" es ruido de 20min | **Rebalanceo solo si: RSI1h>60 O EMA9↓ + min_hold=240min (4h)**. 5-8 rebalanceos/día esperados. `run_trades.py` commit ffc5a75 |
| `portfolio_rebalancing` prematuro (51/64 = 80% de todos los cierres) | Destruye posiciones ganadoras cobrando 0.2% comisión | `min_hold=90→120→240min` + `max_rebalance=1/ciclo` |
| Triple penalización Kelly ($7.50/trade en bear) | Capital subutilizado en bear | Kelly unificado via `_regime.kelly_multiplier` |
| `MAX_PER_TRADE_USDT=15` → predictor con $15 pero Kelly compraba $150 | PnL estimado 10× subestimado → señales rechazadas innecesariamente | `MAX_PER_TRADE_USDT=350` |
| `kelly_dynamic_base_pct=8%` con cap $200 → $105 plano | Capital subutilizado | Base 12%, max $350 |
| BTC.D filter 56% bloqueaba TODOS los alts | Cero señales con BTC.D ~63% | Subido a 64% |
| 3 posiciones mismo símbolo (RENDER×3, TAO×3) | Concentración extrema | `MAX_POSITIONS_PER_SYMBOL=1` |

### Estadísticas históricas (107 trades cerrados al jun 15 21:05)
```
Win rate global: 28% (30W/77L) — DEGRADADO por 92 rebalanceos (WR=32.6%)
Win rate trades normales (SL/TP/predictor): 0% / 15 trades — requiere ajuste SL
PnL del día: -$74.38 (portfolio_rebalancing -$59.17 + normales -$15.20)
Profit factor global: 0.18 — objetivo ≥ 1.5

Impacto post-fix inmediato: 55 rebalanceos → 5 en primera hora tras apply
Oportunidades perdidas por rebalanceo PRE-fix:
  JTOUSDT vendido → +29.4% el día   | XLMUSDT vendido → +10.7%
  ZROUSDT vendido → +10.7%           | WLDUSDT vendido 4x → +6.5%
  INJUSDT vendido → +7.3%
~70% de rebalanceos habrían sido bloqueados por Opción B
```

### Monte Carlo 90 días — 3 escenarios (1,000 sims, capital $3,080)
```
Escenario A — Sin fixes (histórico):   P50=$2,755  return=-10.5%  WR=28%
Escenario B — Con Opción B (activo):   P50=$3,066  return= -0.4%  WR~54%
Escenario C — Optimista (WR 45%):      P50=$3,246  return= +5.4%  WR=45%

Diferencia A→B: +$311 en 90 días = +$38/día de ahorro por rebalanceo quirúrgico
Para llegar a C: SL calibrado ATR + trailing agresivo (+5%/2%) + predictor con más datos
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
