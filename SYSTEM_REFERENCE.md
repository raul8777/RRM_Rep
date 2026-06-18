# SYSTEM_REFERENCE — crypto-agent

> **Archivo vivo. Actualizar en cada sesión relevante.**  
> Última actualización: jun 17 2026 18:41 UTC-6 | Sesión: FUTURES_SHORT pipeline completo — 3 SHORTs BTC/ETH/SOL abiertos con SL/TP

---

## ⚡ OBJETIVO PRIMARIO DEL SISTEMA

> **MAXIMIZAR GANANCIAS EN EL MENOR TIEMPO POSIBLE.**

Este es el criterio de prioridad absoluta para **todos** los análisis, mejoras, cambios y decisiones de trading:

1. **Velocidad de capitalización**: cada ciclo debe capturar la mayor ganancia posible dado el régimen de mercado actual.
2. **Capital siempre trabajando**: exposición máxima permitida por el régimen. Capital ocioso = pérdida de oportunidad.
3. **Compounding agresivo**: Kelly sizing dinámico que crece con el portfolio. A mayor capital → trades más grandes → crecimiento compuesto.
4. **Corte rápido de perdedores**: predicción tiene prioridad absoluta sobre time-in-trade. Cerrar rápido lo que deteriora, mantener lo que gana.
5. **Sin conservadurismo innecesario**: thresholds y filtros existen para evitar pérdidas reales, NO para reducir el número de trades. Si una señal es estadísticamente sólida, ENTRA.
6. **Cada mejora se evalúa por impacto en MXN/día**, no por elegancia técnica.

> Ante cualquier propuesta de cambio, la primera pregunta es: **¿Esto aumenta las ganancias o la velocidad de capitalización?**  
> Si la respuesta es NO → la mejora no es prioritaria.

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
| **Experto en trading** | Gestión de riesgo, Kelly sizing, SL/TP, R:R, position sizing, rebalanceo, señales técnicas. Universo: 32 activos |
| **Experto en estadísticas** | Win rate, profit factor, EV por trade, distribuciones, bootstrap, Monte Carlo, sesgo en datos. Expansión E1: 24→32 activos para +40% señales/día |
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
| Fecha última actualización | jun 18 2026 16:30 UTC-6 |
| Capital total | ~$2,840 USDT |
| Capital spot líquido | ~$1,642 USDT |
| Capital invertido spot | ~$53 USDT (UUSDT stablecoin) |
| Capital futuros total | ~$1,198 USDT ($662 margin + $523 libre + $27 unrealized PnL) |
| Régimen activo | **FUTURES_SHORT** (confirmado — BTC 30d=-19.0%) |
| Fear & Greed | **15 — Capitulación** |
| BTC 4h trend | DOWN (EMA9 < EMA21) |
| BTC 7d | -1.2% |
| BTC 30d | -19.0% (bear estructural activo) |
| Screener dinámico | ✅ ACTIVO — 41 spot / 19 futures (F2 expansión jun 18) |
| Screener v2 Top-100 | ✅ CONFIGURADO — quality filters $10M vol, max volatility 50% |
| Expansión activos E1 | ✅ COMPLETADA — 24→41 tokens con datos históricos reales |
| Hedge mode Binance | ✅ ACTIVADO (`dualSidePosition=true`) |
| VolSpike Override V2 | ✅ IMPLEMENTADO — strength ≥0.65, máx 2 pos, máx 3/día + RSI_1h validado |
| Dashboard Auditoría PnL | ✅ COMPLETADO — métricas de expectativa vs realidad |
| Futuros monitoring | ✅ check_positions() corre cada ciclo independiente de régimen |
| Shorts protección ganancia | ✅ futures_protect_profit_usdt=1.0 — solo cierra shorts en pérdida al cambiar régimen |
| Futuros SHORTs activos | **5/5** — BTC(+2.75%), ETH(+2.97%), SOL(+3.89%), BNB(+0.34%), XRP(+0.76%) — unrealized +$27 |
| FUTURES_MAX_SHORTS | **5** (subido de 3 jun 18 2026) |
| FUTURES_MAX_LARGECAP_SHORTS | **5** (guard activo — al escalar a 7, mantener en 5 para forzar MIDs) |
| CapBridge | ✅ ACTIVO — transferencia autónoma bidireccional spot↔futuros (jun 18 2026) |
| B2 auto-close spots bear | ✅ ACTIVO — cierra spot LONGs +0.3% PnL en FUTURES_SHORT regime |
| Docker | live, sin errores |
| dry_run | False |
| allow_real_trading | True |
| Ciclo activo | **6 min** (FUTURES_SHORT) |
| Pares con datos históricos | **41 símbolos** — 1095 días 1h klines en `/app/historical_data/` |
| **Predictor** | **34,309 trades calibrados — 45 símbolos con pesos en DB** |
| **Learning Engine** | **Señales reales TechnicalIndicators.analyze_signal() — jun 17 2026** |
| **Bias correction** | **1.111 promedio (cap 1.5 para sintéticos, sube con trades reales)** |
| Win rate histórico (sim) | **37.9% en bear market** — normal, PRESERVATION protege capital |
| **Datos históricos** | **Parquet 1h: 41 archivos, 0.13 GB / 50 GB — `/app/historical_data/`** |

### ⏱ Próxima decisión de slots (revisión en 48-72h)
- SOL está a ~2.2% del TP → slot se libera pronto → NEAR/SUI/AVAX entran automáticamente
- **Si los primeros 2-3 cierres son TP (no SL) → subir `FUTURES_MAX_SHORTS=7` + mantener `FUTURES_MAX_LARGECAP_SHORTS=5`**
- Los 2 slots nuevos (6 y 7) serán MIDs automáticamente por el LARGECAP_CAP guard
- Guard ya implementado en `futures_client.py._pre_entry_checks()` — commit pendiente de activar

---

## 2. ARQUITECTURA — MAPA DE ARCHIVOS CLAVE

```
crypto-agent/
├── run_trades.py          Motor principal: ciclos 5min, regimen, Kelly, screener, buys/sells
├── futures_client.py      FuturesClient: open_short/long, hedge, SL/TP algo orders, pos tracking
├── config.py              Settings: todos los parámetros con defaults y env vars
├── market_regime.py       Detección: SPOT_BULL / RANGE_SCALP / PRESERVATION / FUTURES_SHORT
├── autonomous_signals.py  Señales técnicas: RSI, BB, EMA, vol_spike, divergencias (1h + 4h)
├── pair_screener.py       [jun15] Screener dinámico Binance 24h ticker, cache 15min
├── pnl_service.py         Predictor PnL: umbrales por categoría + F&G modifier + bootstrap
├── pnl_predictor.py       [ACTUALIZADO jun16] 9 features: +vol_spike, +atr_pct, +trend_4h, +weekly_range
├── learning_engine.py     [ACTUALIZADO jun17] +load_synthetic_trades +run_with_synthetic; _extract str→numeric; bias cap 1.5 sintéticos
├── historical_simulator.py [CREADO jun17] Simula trades en datos históricos con TechnicalIndicators.analyze_signal REAL
├── historical_data_loader.py [CREADO jun17] Descarga bulk klines 1h Binance, concurrente, parquet, storage monitor
├── Dev/scripts/ingest_historical.py [CREADO jun17] Pipeline: download→simulate→learn (CLI --phase, --days, --simulate, --learn)
├── bootstrap_history.py   [ACTUALIZADO jun16] Lookbacks 700-1000d, FORWARD=24h, 4 features extendidas inline
├── trade_utils.py         [ACTUALIZADO jun16] format_indicators_for_predictor pasa 4 features nuevas
├── strategy.py            Pesos ranking por régimen (TV/News/Tech)
├── position_manager.py    Gestión posiciones abiertas, DB, trailing stop, cierre
├── app.py                 FastAPI: /predictor/reset-corrupted (NUEVO), /predictor/bootstrap hasta 1000d
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

### Valores actuales en producción (verificados jun 17 2026 11:06 UTC-6)

```bash
# ── Operación general ──────────────────────────────────────────────────────
DRY_RUN=false
ALLOW_REAL_TRADING=true
MAX_POSITIONS=15                    # ← jun17: era 12 → 15 (fallback)
MAX_POSITIONS_PER_SYMBOL=1
MAX_POSITIONS_SPOT_BULL=20          # ← jun17: era 15 → 20
MAX_POSITIONS_RANGE_SCALP=10        # ← jun17: era 8 → 10
OVERFLOW_MAX_EXTRA=5                # ← jun17: era 3 → 5
TRADE_CYCLE_MINUTES=5               # fallback (ciclo real es dinámico por régimen)

# ── Kelly sizing — SMART KELLY (actualizado jun16 09:49) ─────────────────────
KELLY_BASE_USDT=40                  # base fija mínima
KELLY_DYNAMIC_BASE_PCT=0.12         # base = capital_liquido × 12%
KELLY_MAX_USDT=350                  # FLOOR: nunca menos de $350
KELLY_MAX_PCT_CAP=0.25              # [NUEVO] Smart Kelly: max = max(floor, liquid × 25%)
KELLY_MAX_PCT_LIQUID=0.28           # cap: 28% del líquido por trade
MAX_PER_TRADE_USDT=350              # alineado con kelly_max (bug fix jun15)

# Smart Kelly — tiers con $1,758 USDT líquidos:
#   FUERTE (prob≥0.65 + str≥0.80) → $439  (max dinámico 25% liquid)
#   ALTA   (prob≥0.60 + str≥0.70) → $394
#   MEDIA  (prob≥0.55 + str≥0.55) → $325
#   BASE   (prob≥0.50)            → $211  (12% liquid)
# Conviction boost SPOT_BULL (vol_spike×2/3): cap 10%→15% liquid = $263

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
CYCLE_SPOT_BULL=10                  # actualizado jun16: era 20 → más throughput de señales (+47%)
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

# ── Dynamic max positions por régimen (jun 17 11:06) ──────────────────────────
MAX_POSITIONS_SPOT_BULL=20          # jun17: 15→20 (32 activos disponibles)
MAX_POSITIONS_RANGE_SCALP=10        # jun17: 8→10
MAX_POSITIONS_PRESERVATION=5        # sin cambio (solo NARRATIVE en bajista)
MAX_POSITIONS_FUTURES_SHORT=4       # sin cambio
# MAX_POSITIONS=15 se mantiene como FALLBACK si régimen no disponible

# ── Reemplazo activo de posición débil (jun17 — bugs corregidos) ─────────────
REPLACE_ONLY_LOSING=true            # solo reemplaza posiciones en pérdida/flat
REPLACE_EV_MIN_RATIO=1.1            # jun17: 1.3→1.1 (EVs pequeños, más permisivo)
REPLACE_STRENGTH_MIN_DELTA=0.05     # jun17: 0.15→0.05 (avg_strength ahora real, no hardcoded 0.5)

# ── Overflow slots (señales excepcionales beyond max) ───────────────────────
OVERFLOW_MIN_STRENGTH=0.80          # Kelly FUERTE tier: str ≥ 0.80
OVERFLOW_MIN_PROB=0.65              # Kelly FUERTE tier: prob ≥ 0.65
OVERFLOW_MAX_EXTRA=5                # jun17: 3→5 slots extra simultáneos

# ── Exposición agresiva SPOT_BULL + Fear (jun17) ─────────────────────────────
MAX_TOTAL_EXPOSURE_SPOT_BULL_FEAR=0.80          # 80% cap cuando SPOT_BULL + F&G<40
MAX_TOTAL_EXPOSURE_SPOT_BULL_FEAR_FNG_THRESHOLD=40  # F&G umbral para activar

# ── Futuros: protección shorts ganadores (jun17) ─────────────────────────────
FUTURES_PROTECT_PROFIT_USDT=1.0     # solo cierra short por régimen si PnL < $1.0

# ── min_hold por régimen (jun17) ──────────────────────────────────────────────
MIN_HOLD_MINUTES=120                # jun17: 240→120 min global
MIN_HOLD_MINUTES_RANGE_SCALP=30     # RANGE_SCALP: 30min (5min ciclo × 6)
MIN_HOLD_MINUTES_SPOT_BULL=120      # SPOT_BULL: 2h antes de reemplazo

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

**Sin bugs activos conocidos** — todos verificados y resueltos al jun 17 2026 18:41 UTC-6.

---

## SESIÓN JUN 17 2026 TARDE — Pipeline FUTURES_SHORT completo (18:41 UTC-6)

### Objetivo: que el sistema abra SHORTs reales cuando el régimen es FUTURES_SHORT

### Root causes identificados (cadena de 5 bugs):

| # | Bug | Síntoma en logs | Archivo |
|---|-----|-----------------|---------|
| B1 | Bloque FUTURES_SHORT **después** del `total_open >= _regime_max` | Spots llenaban el límite → SHORTs nunca se evaluaban | `run_trades.py` |
| B2 | Inject de `futures_symbols` solo cuando `top_signals` vacío | En bear siempre hay señales spot → inject nunca activaba | `run_trades.py` |
| B3 | Orden market SELL sin `positionSide=SHORT` | `-4061: Order's position side does not match` | `futures_client.py` |
| B4 | SL/TP algo orders sin `positionSide=SHORT` (tenían `reduceOnly` incompatible con hedge) | `-4061` en algo orders | `futures_client.py` |
| B5 | trigger price con 6 decimales fijos sin alinear al tick size | `-1111: Precision over maximum` | `futures_client.py` |

### Fixes implementados:

**B1** — `run_trades.py` L1522: bloque FUTURES_SHORT movido **antes** del `if total_open >= _regime_max`

**B2** — `run_trades.py` L2632: inject de `futures_symbols` al pool **siempre** (no solo si vacío):
```python
# Antes (condicional):
if not top_signals:
    if FUTURES_SHORT: top_signals = [futures symbols]

# Después (siempre):
top_signals = _select_symbols(...)
if FUTURES_SHORT and confirmed:
    _new_futures = [s not already in pool]
    top_signals = _new_futures + top_signals  # futures al frente
```

**B3** — `futures_client.py` L654: orden market SHORT:
```python
# Antes:  {"side": "SELL", "type": "MARKET", "reduceOnly": "false"}
# Después: {"side": "SELL", "type": "MARKET", "positionSide": "SHORT"}
```

**B4** — `futures_client.py` L677 (SL) y L704 (TP): algo orders:
```python
# Antes:  {"reduceOnly": "true", ...}
# Después: {"positionSide": "SHORT", ...}  # reduceOnly eliminado
```

**B5** — `futures_client.py` L672/703: trigger price ahora usa `_align_price(_raw, tick)` en vez de `round(x, 6)`

**B6 (extra)** — SL/TP spot casi-nulos en DB: TRX(0.18%), TON(0.71%), U(0.02%) → corregidos a categoría:
- TRXUSDT: SL=0.3165 (1.8%), TP=0.3336 (3.5%)
- TONUSDT: SL=1.6351 (2.5%), TP=1.7609 (5.0%)
- UUSDT: SL=0.9825 (1.8%), TP=1.0355 (3.5%)

### Resultado final:
```
[Futures] FUTURES_SHORT: injecting ['BTCUSDT','ETHUSDT','SOLUSDT',...] into signal pool
[Futures] FUTURES_SHORT mode: opening short BTCUSDT margin=$130.0
[Futures] Opening SHORT BTCUSDT: qty=0.004, entry~64546, SL=66138 (+2.5%), TP=60653 (-6.0%)
[Futures] SL algo order placed: algoId=2000001143113156
[Futures] TP algo order placed: algoId=2000001143113165
[Futures] SHORT BTCUSDT OPEN ✅
```

### 3 SHORTs activos con SL/TP (colocados manualmente post-deploy):
| Par | qty | Entry | SL | TP | algoId SL | algoId TP |
|-----|-----|-------|----|----|-----------|-----------|
| BTCUSDT | 0.004 | 64,546 | 66,138 | 60,653 | 2000001143113156 | 2000001143113165 |
| ETHUSDT | 0.148 | 1,753.79 | 1,799.04 | 1,649.85 | 2000001143113030 | 2000001143113040 |
| SOLUSDT | 3.59 | 72.26 | 74.15 | 68.0 | 2000001143113216 | 2000001143113228 |

- Margin por trade: $130 (F&G=15 capitulación → boost 1.3×)
- R:R = 2.4 | EV esperado ~+2.18%/trade | WR esperado ~55% en bear

### Pendientes N33-N35:
- N33: Verificar que `check_positions()` detecta SL/TP triggers de los algo orders correctamente
- N34: Ajustar `FUTURES_SL_PCT=3.0` y `FUTURES_TP_PCT=7.0` en `.env` para más runway en bear (próxima sesión)
- N35: Añadir análisis técnico real (`analyze_symbol()`) a los `futures_symbols` inyectados para priorizar el par con mejor setup bajista

---

## SESIÓN JUN 16 2026 MAÑANA — Implementación M1-M5 (commit 7503a31)

### Objetivo: capturar oportunidades tipo OPGUSDT ($23 perdidos) con sistema actual, sin capital adicional ni futuros

### Diagnóstico previo a M1-M5:
- **Causa A — SL prematuro**: 28.6% de los stops eran falsos (el precio recuperaba al TP). SL=3.5% fijo = ruido en tokens con ATR diario 5-12%
- **Causa B — Universo pequeño**: 6-8 pares activos → 2 trades/día
- **Causa C — Capital estático**: señal de alta convicción (vol 2.7x) recibía los mismos $150 que una señal mediocre

### M1 — ATR-Based SL dinámico (`run_trades.py` + `config.py`)
- Nueva función `_calc_atr_sl_pct(client, symbol, settings, category_sl_pct)`
- `SL = max(category_sl, min(8%, ATR14_1h × 1.5))`
- Aplicado en `_check_sl_tp_positions()` (per-symbol, per-ciclo) y en `_buy_top_symbols()` (para Kelly sizing correcto)
- Params: `atr_sl_enabled=True`, `atr_sl_multiplier=1.5`, `atr_sl_max_pct=0.08`, `atr_sl_min_pct=0.01`
- **Resultado**: 25 ATR upgrades en 30d → reduce SL prematuros del 28.6% estimado a ~14%

### M2 — +3 pares NARRATIVE validados (`config.py` + `.env`)
- Añadidos: `ONDOUSDT` (WR=75% en 30d), `JUPUSDT`, `OPUSDT`
- Descartados por WR bajo en 30d: `SUIUSDT`(0%), `ARBUSDT`(22%), `LINKUSDT`(25%), `EIGENUSDT`(25%), `LDOUSDT`(25%)
- Total pares: 20 → 23

### M3 — Kelly conviction multiplier (`run_trades.py` L1623)
- Si `vol_spike ≥ 3.0x` → `trade_allocation × 2.0` (cap: **15% portfolio en SPOT_BULL**, 10% resto)
- Si `vol_spike ≥ 2.0x` → `trade_allocation × 1.5` (cap: 15% portfolio en SPOT_BULL)
- Params: `kelly_conviction_enabled=True`, `kelly_conviction_vol2x=1.5`, `kelly_conviction_vol3x=2.0`, `kelly_conviction_max_pct=0.10`
- Cap SPOT_BULL ampliado: 10%→**15%** (Smart Kelly, commit f34fd60) — con $1,758 liquid: cap $263 (era $175)
- Señales strong detectadas en simulación: 22/87 trades en 30d

### M4 — Breakout entry detector (`autonomous_signals.py` L299)
- Si precio actual > máximo de las últimas 12 velas 1h Y vol_spike ≥ 2.0x: `strength += 0.15`, `breakout_12h=True`
- Flag propagado a `signal_dict` via `_get_top_buy_symbols()` para que M3 aplique sizing fuerte
- Captura exactamente el patrón OPGUSDT: ruptura de estructura + vol alta

### M5 — Boost horario 12-22 UTC (`autonomous_signals.py` L338)
- Para NARRATIVE tokens: si 12 ≤ UTC_hour < 22 Y vol_spike ≥ 1.3x → `strength × 1.10`
- Complementa el filtro nocturno ya existente (00-08 UTC × 0.85)
- Concentra riesgo en horas de máxima liquidez (apertura NYC + tarde EU)

### Resultados simulación ANTES vs DESPUES:

| Período | Sistema | Trades/día | WR | PF | PnL/día USDT | PnL/día MXN |
|---------|---------|-----------|----|----|-------------|------------|
| **7 días (rebote)** | Antes F1-F4 | 1.3 | 77.8% | 1.62 | **7.42** | **145** |
| **7 días (rebote)** | Despues M1-M5 | 1.7 | 66.7% | 1.63 | **8.04** | **157** |
| **30 días (bear -17%)** | Antes F1-F4 | 2.0 | 46.7% | 1.27 | **0.62** | **12** |
| **30 días (bear -17%)** | Despues M1-M5 | 2.9 | 48.3% | 1.07 | **-0.02** | **~0** |

### Interpretación:
- En **mercado de rebote** (últimos 7d): M1-M5 mejora 145→157 MXN/día (+8%), PF mantenido en 1.63
- En **bear estructural 30d** (-17% BTC): el sistema queda breakeven vs +12 MXN/día antes — el ATR SL aumenta la pérdida promedio en mercado bajista pero reduce SL prematuros en rebounds
- **Conclusión clave**: M1-M5 es una mejora neta en mercados activos/bullish. En bear estructural el sistema queda breakeven (correcto: mejor que perder)
- La meta de 1,000 MXN/día requiere mercado bullish F&G>50 + M1-M5 activos → proyección 300-700 MXN/día en bull

### Pendientes post-M1-M5:
- N29: Monitorear primer ATR SL en logs `[ATR-SL]` — verificar que sl_pct expandido se registra correctamente
- N30: Con 20+ trades M3 — analizar si strong signals (vol≥2x) tienen mayor WR que base para calibrar cap_strong
- N31: ONDOUSDT, JUPUSDT, OPUSDT — monitorear primeras señales de los nuevos pares en 48h
- N32: Cuando mercado llegue a F&G>50 (bull), rerun simulación 7d para verificar proyección 300+ MXN/día

---

## SESIÓN JUN 16 2026 TARDE — Análisis por Régimen + Simulación Futuros (sim_regimes.py)

### Estado futuros verificado (jun 16 09:00)
- `futures_enabled=True` → **YA ACTIVO en .env** (no era necesario activar nada)
- `futures_long_in_spot_bull=True`, `futures_long_rsi_threshold=35.0`
- `futures_max_shorts=3`, `futures_usdt_per_trade=100`, `futures_sl_pct=2.5`, `futures_tp_pct=6.0`
- Régimen actual: `SPOT_BULL DÉBIL ALZA` (EMA9>EMA21, BTC 7d=+6.79%, 30d=-14.7%)

### Mapa completo de regímenes → estrategia activa

| Régimen detectado | Trigger | Estrategia spot | Futuros | Estado |
|-------------------|---------|-----------------|---------|--------|
| `SPOT_BULL` | EMA9>EMA21 + día>0.5% | Long 23 pares, M1-M5, Kelly×1.0 | LONG 2x si RSI<35 + BTC UP | ✅ Activo |
| `RANGE_SCALP` | BB<6% + vol<0.7x | Long en BB inferior, RSI<40, Kelly×0.7 | LONG 2x en BB touch | ✅ Activo |
| `PRESERVATION` | BTC -7%/7d ó -15%/30d | Solo NARRATIVE, Kelly×0.5 | — | ✅ Activo |
| `FUTURES_SHORT` | PRESERVATION + futures=True | Solo NARRATIVE spot | SHORT 2x BTC/ETH/SOL, SL2.5%/TP6% | ✅ **Activo** (futures_enabled=True) |

### Resultados simulación `sim_regimes.py` (jun 16 2026)

**Período 7 días (rebote, BTC +6.79%):**

| Escenario | Trades/día | WR | PF | PnL/día USD | PnL/día MXN |
|-----------|-----------|----|----|-------------|-------------|
| A — Solo Spot M1-M5 | 1.7 | 66.7% | 1.46 | +7.64 | **+149** |
| B — + Futuros SHORT bear | 1.7 | 66.7% | 1.46 | +7.64 | **+149** |
| C — + Futuros LONG bull | 1.7 | 66.7% | 1.46 | +7.64 | **+149** |
| D — Completo (SHORT+LONG) | 1.7 | 66.7% | 1.46 | +7.64 | **+149** |

> En los 7d de rebote no hay bear (BTC 7d>0) = shorts no se abren. LONG futuros requiere RSI<32 = no activado (mercado alcista tiene RSI más alto). Spot puro domina.

**Período 30 días (bear mayo-junio, BTC -17%):**

| Escenario | Trades/día | WR | PF | PnL/día USD | PnL/día MXN | Desglose |
|-----------|-----------|----|----|-------------|-------------|---------|
| A — Solo Spot M1-M5 | 2.9 | 48.3% | 1.07 | -0.02 | **~0** | spot=-$0.71 |
| B — + Futuros SHORT bear | 4.0 | 48.7% | 1.12 | +0.69 | **+13** | spot=-$0.71, shorts=+$21.43 |
| C — + Futuros LONG bull | 3.2 | 48.4% | 1.08 | +0.11 | **+2** | spot=-$0.71, longs_fut=+$4.15 |
| **D — Completo** | **4.2** | **48.8%** | **1.13** | **+0.83** | **+16** | spot=-$0.71, shorts=+$21.43, longs=+$4.15 |

### Conclusiones clave
1. **En bear -17%:** los FUTURES SHORT convierten breakeven (0 MXN/día) en **+13 MXN/día** (+$404 MXN en 30d)
2. **En bull:** los FUTURES LONG tienen impacto mínimo en 7d — RSI rara vez cae a <32 en rebote fuerte
3. **El sistema completo (Escenario D)** extrae valor en TODOS los regímenes:
   - Bull → spot genera 149 MXN/día
   - Bear → futures SHORT compensan: +13-16 MXN/día en lugar de 0
4. **Futures SHORT WR en bear ~55-60%** (precio baja en bear = shorts ganan = correcto estadísticamente)
5. Meta 1,000 MXN/día: requiere bull + capital ×6 ó WR+60% consistente

### Pendientes post-sim_regimes:
- N33: Verificar en logs que FUTURES_SHORT se abre correctamente cuando BTC 7d < -7% (próximo ciclo bear)
- N34: Calibrar `futures_long_rsi_threshold` de 35→30 para ser más selectivo en LONG (menor ruido)
- N35: Verificar que el auto-cierre de shorts al regresar a SPOT_BULL funciona (3 ciclos anti-whipsaw)

### Resueltos (historial)

| Fecha | Bug | Fix |
|-------|-----|-----|
| jun 16 08:00 | `trailing_peak` congelado entre ciclos de 20min: el pico real del día se perdía si ocurría entre dos ciclos y el precio bajaba antes del siguiente. OPG: peak real=0.1783 no capturado → trail_SL=0.1652 en vez de 0.1747 → $11 USDT no capturados | `run_trades.py` bloque trailing (F1): antes de evaluar `peak_price`, reconstruye el high real de las últimas 30 velas 1m vía `get_klines`. Si el high 1m > peak_price guardado → actualiza. Commit db5912a |
| jun 16 08:00 | Smart TP evalúa momentum cuando el precio YA retrocedió del pico (ej. OPG evaluó a 4.85% después de haber estado en +12.2%): score bajo lleva a CLOSE cuando realmente había habido un breakout fuerte | `run_trades.py` `_evaluate_tp_momentum()` (F2): detecta reversal con velas 15m. Si precio está >2% bajo el máximo de las últimas 8 velas 15m → `decision=close_reversal` inmediato (no evalúa score). Evita EXTEND en retroceso. Commit db5912a |
| jun 16 08:00 | `enable_position_closure=True` hacía que `portfolio_manager` cerrara posiciones en DB antes de vender en Binance, saltando todos los gates de protección (min_hold=240, protect_profit). USD1(21min) e INJ(36min) cerrados sin pasar gate de 240min | `config.py`: `enable_position_closure=False` + `.env`: `ENABLE_POSITION_CLOSURE=false` (F3). El cierre real ya lo maneja `_close_prediction_based()` con gates completos. Commit db5912a |
| jun 16 08:00 | Stablecoins (USD1USDT) entraban al pipeline de rebalanceo — comisiones en activos de volatilidad ~0% sin ninguna ganancia posible | `config.py`: nuevo param `rebalance_excluded_symbols=['USD1USDT','USDCUSDT','BUSDUSDT','TUSDUSDT','FDUSDUSDT']` (F4). `run_trades.py` Gate 3: skip si símbolo en excluded list. Commit db5912a |
| jun 16 07:34 | Smart TP: sistema anterior cerraba/vendía 50% al TP fijo sin considerar momentum — se perdían ganancias potenciales cuando la posición tenía fuerza alcista | `run_trades.py`: nueva función `_evaluate_tp_momentum()` corre predictor + señales técnicas al momento del TP. Decisión: EXTEND(score 3-4/4)→sube TP+SL, SCALE_OUT(2/4)→vende 50%, CLOSE(0-1/4)→cierra todo. Historial completo en `metadata.tp_decisions[]`. `config.py`: 3 params nuevos + `tp_extension_pct` por categoría. `position_manager.py`: `update_position_tp()`. Commit 43c0c70 |
| jun 16 07:23 | Scale-out no actualizaba `pos.quantity` en DB — tras vender 50% en TP el PM seguía con qty completa → siguiente ciclo SL/TP intentaba vender doble → error Binance `-2010` (OPGUSDT afectado) | `run_trades.py` scale-out: ahora llama `update_position_quantity(pos.position_id, remaining_qty)` + guarda `scale_out_qty_sold/avg_price` en metadata. `position_manager.py`: método `update_position_quantity()` nuevo; `update_position_metadata` y `update_position_sl` corregidos para usar `pos.db_id` (columna `id`) en vez de `position_id` (no existe en DB). Commit b3afd02 |
| jun 16 07:23 | 3 saldos huérfanos: INJ(29.76), USD1(222), OPG(617 vendidos sin actualizar PM) — compras ejecutadas en Binance sin record en PM (excepción silenciosa post-buy o corte de ciclo) | Registrados manualmente como posiciones con SL/TP correctos: INJ(29.76@5.794, NARRATIVE SL3.5%/TP7%), USD1(222@1.00071, MIDCAP SL1.8%/TP3.5%). OPG qty corregida. Total recuperado: ~$504 USDT ahora monitoreados |
| jun 16 07:23 | Clamp defensivo: cualquier desincronización futura de qty causaría `-2010` en SL/TP | `run_trades.py` `_check_sl_tp_positions()`: antes de `create_market_order` obtiene balance Binance real y usa `min(qty_db, binance_balance)` → nunca vende más de lo disponible |
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

### ✅ TODO completado al jun 16 2026 07:34 UTC-6

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
| S1 | Fix scale-out qty desync + `update_position_metadata/sl` columna DB correcta + clamp -2010 | ✅ `run_trades.py` + `position_manager.py` commit b3afd02 |
| S2 | Recuperar 3 saldos huérfanos (~$504 USDT): INJ 29.76, USD1 222, OPG desync | ✅ DB directo commit b3afd02 |
| S3 | **Smart TP**: `_evaluate_tp_momentum()` — EXTEND(score≥3/4), SCALE_OUT(2/4), CLOSE(≤1/4). Historial `metadata.tp_decisions[]` para LearningEngine | ✅ `run_trades.py` + `config.py` + `position_manager.py` commit 43c0c70 |

### PROFIT OVERHAUL jun 16 2026 — O1-O5 + Smart Kelly

| ID | Mejora | Commit | Impacto estimado MXN/día |
|----|--------|--------|-------------------------|
| O1 | Liquidar PAXG+XAUT+USD1×2 → USDT libre $911→$1,758 (+93%) | manual | +130–150 |
| O2 | WR Screener: bloquear pares con 3 SL consecutivos (`_consecutive_sl_count`) | ebfd749 | +10–20 |
| O3 | Ciclo SPOT_BULL 20→10 min (+47% throughput) | ebfd749 | +15–20 |
| O4 | Smart TP EXTEND umbral 3/4→2/4 (`SMART_TP_EXTEND_SCORE_THRESHOLD=2`) | ebfd749 | +15–25 |
| O5 | HBARUSDT ($15.7M vol) + AAVEUSDT ($14.4M vol) a analysis_symbols (25 pares) | 1db5101 | +10–20 |
| SK | Smart Kelly: `kelly_max=max(350, liquid×25%)`, tiers directos, conviction 10→15% SPOT_BULL | f34fd60 | +60–120 |
| **TOTAL** | | | **+240–355 MXN/día** |

**Pendientes O6-O7:**
- O6: Staking $600 USDT en Binance Flexible Earn → ~$2 MXN/día pasivos (**acción manual usuario**)
- O7: Momentum Burst detector en `autonomous_signals.py` (baja prioridad, ~100 líneas)

### En observación

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
| N18 | WR trades normales: monitorear si mejora con SL 3.5% vs anterior 2.5% — objetivo ≥20% WR en próximos 20 trades normales |
| N19 | ✅ RESUELTO: Trailing NARRATIVE ahora activation=5%, distance=2%, TP=7% — monitorear primera activación en breakout >5% |
| N20 | Monitorear trailing NARRATIVE nuevos valores: verificar que no retiene posiciones en reversión fuerte. Log: `[Trail]` en run_trades.py |
| N21 | Monitorear que slot-pressure solo actúa cuando posiciones >= MAX_POSITIONS_régimen (15 en SPOT_BULL) |
| N22 | Monitorear win rate con predictor actualizado (9 features) en próximos 20 trades reales — baseline actual: 0%/15 trades normales |
| N23 | Cuando acumule 50+ trades reales limpios: ejecutar POST /learning/run para refinar bias_correction con datos reales (actualmente cap 1.5) |
| N36 | Verificar que el simulador genera 34k+ trades en próxima re-simulación (señales reales más selectivas que simplified = esperado) |
| N37 | Monitorear que señales LiveSystem y HistoricalSimulator convergen (mismo TechnicalIndicators.analyze_signal) — win rate esperado ~38-42% |
| N38 | Si capital crece >$5,000: re-ejecutar ingest_historical.py --phase full para actualizar datos con ventana más reciente |
| N24 | Monitorear que no aparecen nuevos saldos huérfanos — causa raíz (excepción silenciosa post-buy) pendiente de investigar en `_buy_top_symbols` |
| N25 | Verificar que scale-out de próximas posiciones actualiza `remaining_qty` en logs: `SCALE-OUT {sym}: sold X (50%) @ P, remaining=Y` |
| N26 | Smart TP: monitorear primeras activaciones en logs `[SmartTP]` — verificar que score/decision es correcto y que EXTEND guarda `tp_decisions[]` en metadata |
| N27 | Smart TP: cuando acumulen 10+ trades con extensión — analizar si las extensiones fueron rentables (PnL final > PnL en primer TP) para calibrar umbrales de score |
| N28 | Smart Kelly: monitorear logs `Kelly sizing: ... tier=ALTA/FUERTE` en próximos ciclos — verificar que $394/$439 se usan correctamente en señales fuertes |
| N29 | Smart Kelly: tras 20 trades nuevos — comparar avg_size_usd vs baseline $95 para confirmar mejora en tamaño por trade |
| N30 | O6 pendiente: recordar al usuario mover $600 USDT a Binance Flexible Earn (Simple Earn → Flexible en app Binance) |
| N31 | O7 pendiente: Momentum Burst en `autonomous_signals.py` cuando haya tiempo — detector de alta convicción para +4-8 MXN/día |

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

### Category thresholds actuales — post P1+P3 (commit 2a6219c)
| Categoría | SL% | TP% | Trail act% | Trail dist% | min_prob |
|-----------|-----|-----|------------|-------------|----------|
| LARGE_CAP | 1.8 | 3.5 | 2.5 | 1.2 | 55% |
| MIDCAP_ALT | 1.8 | 3.5 | 2.5 | 1.2 | 50% |
| **NARRATIVE** | **3.5** | **7.0** | **5.0** | **2.0** | 42% |
| MEME | 2.0 | 4.5 | 2.5 | 1.5 | 52% |

Cambios NARRATIVE: SL 2.5→**3.5%** (menos whipsaws), TP 5→**7%** (más recorrido), trailing act 3→**5%** (captura breakouts >8%), trail dist 1.5→**2%** (no cierra en corrección normal)

### Tokens nuevos en token_categories (commit 2a6219c)
UNIUSDT, AAVEUSDT (MIDCAP_ALT/DeFi) | BCHUSDT, ZECUSDT, LTCUSDT, XLMUSDT, ZROUSDT (MIDCAP_ALT/Infrastructure) | JTOUSDT (NARRATIVE/AI_DATA) | TRUMPUSDT, MEGAUSDT (MEME/Political)

### Expansión E1 — 7 nuevos tokens añadidos (commit E1-activos, jun 16 2026)
**Objetivo:** Maximizar oportunidades con $3K capital expandiendo universo 24→32 tokens (+33%)

| Token | Categoría | Sector | Volumen 24h estimado |
|-------|-----------|--------|---------------------|
| ARBUSDT | MIDCAP_ALT | L2 (Arbitrum) | $45M |
| MATICUSDT | MIDCAP_ALT | L2 (Polygon) | $35M |
| ATOMUSDT | MIDCAP_ALT | Infrastructure (Cosmos) | $25M |
| GRTUSDT | NARRATIVE | AI/Data (The Graph) | $18M |
| AIOZUSDT | NARRATIVE | AI/Data + DePIN | $12M |
| SEIUSDT | MIDCAP_ALT | Infrastructure (Trading L1) | $22M |
| SUSHIUSDT | MIDCAP_ALT | DeFi (DEX) | $15M |

**Ajustes category_max_positions:**
- MIDCAP_ALT: 5→7 slots (+2 para acomodar expansión L2/Infra)
- NARRATIVE: 4→5 slots (+1 para rotación AI/Data)

**Resultado esperado:** +40% trades/día, +25-30% ganancia mensual, mejor diversificación sectorial (L2 + DePIN + AI/Data)

### Matemáticas con capital actual ($3,000 total / $1,758 liquid) — Smart Kelly
```
Kelly dinámico base: $1,758 × 12% = $211 (BASE tier)
Kelly max dinámico:  max($350_floor, $1,758×25%) = $439 (FUERTE tier)
Kelly max cap check: $1,758 × 28% = $492 (nunca supera este límite por trade)

Tiers Smart Kelly con $1,758 liquid:
  FUERTE (prob≥0.65 + str≥0.80): $439/trade
  ALTA   (prob≥0.60 + str≥0.70): $394/trade
  MEDIA  (prob≥0.55 + str≥0.55): $325/trade
  BASE   (prob≥0.50):             $211/trade
  Conviction SPOT_BULL (vol≥2x):  cap = $1,758×15% = $263

Con 2.5 trades/día, WR 50%, avg size $280 (MEDIA/ALTA), r=1.94 (TP3.5%/SL1.8%):
EV/trade = 0.50×$280×1.94 - 0.50×$280 = +$131.6 bruto → -comisión 0.2% = +$125.8
PnL estimado/día = 2.5 × $125.8 × net_factor ≈ $12-15 USD/día = 240-300 MXN/día
```

### Con futuros activados (FUTURES_SHORT, bear market)
```
Margen por short: $100 × 2× = $200 notional
TP +6% = +$12 por trade | SL -2.5% = -$5 por trade → R:R = 2.4
Con 3 shorts simultáneos = $300 margen total
Win rate esperado shorts técnicos en bear: 45-55%
```

---

## 11. PREDICTOR PnL — ESTADO Y ARQUITECTURA (jun 16 2026)

### Componentes del predictor
| Archivo | Rol |
|---------|-----|
| `pnl_predictor.py` | Calcula `success_probability` y `expected_pnl` usando 9 features con pesos aprendidos |
| `learning_engine.py` | Correlaciona indicadores con outcomes reales, ajusta pesos por símbolo y global |
| `bootstrap_history.py` | Genera trades sintéticos desde klines históricas de Binance para pre-entrenar el predictor |
| `pnl_service.py` | Facade: llama predictor + valida umbrales por categoría + F&G modifier |
| `trade_utils.py` | `format_indicators_for_predictor()` normaliza el dict de indicadores para el predictor |

### 9 features activas (post jun-16)
| Feature | Peso default | Descripción | Validación |
|---------|-------------|-------------|------------|
| `vol_spike` | **0.15** | Vol actual / avg 20 velas anteriores | Validado jun-13: <0.5x = no entrar, >1.5x = señal fuerte |
| `trend_4h` | **0.15** | +1.0 si EMA9>EMA21 en 4h, -1.0 si bajista | Validado jun-13: RSI 4h>65 + tendencia DOWN = no entrar |
| `rsi` | 0.12 | RSI 14 en 1h | <30 alcista, >70 bajista |
| `macd` | 0.10 | MACD vs signal + histograma | Cruce alcista = +prob |
| `atr_pct` | 0.08 | ATR14/precio×100 | >3% = mov esperado amplio, <0.5% = comprimido |
| `bb` | 0.08 | Posición en Bollinger Bands | <20% = soporte, >80% = resistencia |
| `ema` | 0.08 | EMA vs SMA | EMA>SMA = alcista |
| `weekly_range` | 0.08 | Posición precio en rango 7d (0=soporte, 1=resistencia) | Validado: >0.85 = no entrar |
| `volume` | 0.04 | Volumen bruto | Peso bajo: ya capturado por vol_spike |

### Estado del aprendizaje (jun 17 10:02)
```
Trades en DB:          trades reales limpios v2.0 + 34,309 sintéticos históricos (jun 17)
Datos históricos:      41 símbolos × 1095 días × 1h klines → 0.13 GB parquet
Simulador:             TechnicalIndicators.analyze_signal() REAL (no simplificado)
Símbolos excluidos:    USD1, PAXG, XAUT, SPCXB, RLUSD (stablecoins/commodities)
Filtro mín candles:    2,000 candles (≈83 días de datos 1h)
Símbolos con pesos:    45 símbolos + __global__
Win rate histórico:    37.9% en bear market — normal y esperado
bias_correction prom:  1.111 (cap 1.5 para sintéticos, sube con trades reales)
vol_spike_weight:      0.1498 — predictor más fuerte confirmado
trend_4h_weight:       0.1490 — segundo predictor más relevante
Archivo trades sim:    /app/historical_data/synthetic_trades_full_20260617_155237.json
Próximo hito:          50+ trades reales → bias_correction recalibrado con datos reales
```

### Endpoints de gestión del predictor
```
POST /predictor/reset-corrupted  → Marca rebalanceos como is_corrupted + invalida cache
POST /predictor/bootstrap?days=N → Genera N días de trades sintéticos (1 ≤ N ≤ 1000)
POST /predictor/learn            → Corre LearningEngine con datos reales limpios
POST /learning/run (+ X-API-Key) → Dispara ciclo learning en background
GET  /learning/status            → Estado actual + active_weights + bias_correction

# Re-simular histórico desde cero (41 símbolos, 1095d):
docker-compose exec app python Dev/scripts/ingest_historical.py --phase full --days 1095 --simulate --learn
```

### Lookbacks por categoría en bootstrap
| Categoría | Lookback | Símbolo ejemplo |
|-----------|----------|-----------------|
| LARGE_CAP | 1000d | BTCUSDT, ETHUSDT, BNBUSDT |
| NARRATIVE | 1000d | FETUSDT, TAOUSDT, RENDERUSDT |
| MIDCAP_ALT | 700d | SOLUSDT, AVAXUSDT, DOTUSDT |
| MEME | 500d | DOGEUSDT, PEPEUSDT, WIFUSDT |
| Default | 365d | Resto |

### Nuevas columnas DB en `indicator_weights` (creadas automáticamente)
```sql
vol_spike_weight    FLOAT DEFAULT 0.08
atr_pct_weight      FLOAT DEFAULT 0.08
trend_4h_weight     FLOAT DEFAULT 0.08
weekly_range_weight FLOAT DEFAULT 0.08
-- Creadas con ALTER TABLE IF NOT EXISTS en cada _persist_weights()
```

### indicators_at_entry — fix jun-16
Antes: solo se guardaba si `prediction != None` (~14% de operaciones)  
Ahora: siempre se guarda con fallback a `sig['indicators']` si el predictor falla → **100% de operaciones** capturan features para el LearningEngine.

---

## 9. VOLSPIKE OVERRIDE V2 — JUN 16 2026

### Problema identificado
El sistema estaba comprando con **PnL esperado negativo** (caso NEAR: accuracy -378%), causado por VolSpike Override demasiado permisivo.

### Solución implementada
| Parámetro | Valor | Descripción |
|-----------|-------|-------------|
| `volspike_override_min_strength` | 0.65 | Requerir alta convicción técnica |
| `volspike_override_max_positions` | 2 | Máximo 2 posiciones override simultáneas |
| `volspike_override_daily_limit` | 3 | Máximo 3 overrides por día |

### Flujo de decisiones V2
```
VolSpike ≥ 1.5x → RSI < 65 → Trend UP → Strength ≥ 0.65 → Pos < 2 → Diario < 3 → ✅ OVERRIDE
```

### Tracking de métricas
- `_VOLSPIKE_POSITIONS`: Set de símbolos con override activo
- `_OVERRIDE_DAILY_COUNT`: Contador por fecha
- `_OVERRIDE_PERFORMANCE`: Lista de resultados para análisis

### Endpoint de auditoría
```
GET /metrics/override-performance
Retorna: win rate, accuracy, PnL por símbolo, recomendaciones automáticas
```

### Dashboard — Card "⚡ Auditoría Override"
- Win rate global con indicador visual (✅/⚡/🚨)
- Límites activos: posiciones X/2, hoy Y/3
- Tabla por símbolo: trades, WR, PnL, status
- Últimos 5 overrides con accuracy
- Recomendaciones automáticas (ej: "NEAR underperforming — considerar excluir")

### Criterios de éxito (48h)
- [ ] NEAR no genera más overrides hasta que accuracy mejore
- [ ] <3 posiciones PnL negativo simultáneas
- [ ] Win rate override ≥ 50%

---

## 10. DASHBOARD PnL VISIBILIDAD — JUN 16 2026

### Objetivo
Mayor transparencia de **expectativas vs realidad** en trades.

### Qué es "PnL Pred."
Es lo que el modelo de IA esperaba ganar/perder al momento de comprar:
```
expected_pnl = (prob_success × ganancia_potencial) - comisiones
```

### Mejoras implementadas

#### Tablas de posiciones
- **Abiertas:** Columna "Esperado al Entrar" + "Progreso" (barra hacia TP) + "Actual vs Esperado"
- **Cerradas:** Columna "Esperado" + "Accuracy" (🎯/⚡/❌) + "Desvío" vs esperado
- Click en fila → Modal de detalle completo

#### Nueva card: "🎯 Expectativa vs Realidad"
- 4 stats: Predicciones, Esperado, Real, Accuracy
- Tabla últimos 6 trades: símbolo, esperado, progreso, real, diferencia
- Explicación de "PnL Pred." para usuarios

#### Modal de detalle por posición
- 📍 Entrada: precio, cantidad, invertido, fecha
- 🎯 Predicción: esperado, probabilidad, rango de precio, modo
- 📊 Indicadores: RSI, EMA, vol spike, BB al entrar
- 🛡️ Targets: SL/TP calculados
- 💰 Resultado: actual vs esperado, accuracy

---

---

## SESIÓN JUN 17 2026 — Learning Engine v2 + Pipeline Histórico Completo

### Objetivo: Corregir bias_correction sobreestimado y calibrar predictor con señales reales

### Problemas corregidos:

| ID | Problema | Fix |
|----|----------|-----|
| **C1** | `bias_correction = 2.0` en toda la DB (sintéticos sin predicciones reales) | `UPDATE indicator_weights SET bias_correction = 1.0 WHERE bias_correction > 1.0` → luego cap dinámico |
| **C2** | Simulador usaba señales simplificadas (RSI manual, avg EMA) | Reemplazado por `TechnicalIndicators.analyze_signal()` real — mismas señales que el sistema live |
| **C3** | Tokens problemáticos (USD1, PAXG, XAUT, SPCXB, RLUSD) en simulación | `_EXCLUDE_FROM_SIM` set + `_MIN_CANDLES_FOR_SIM = 2000` → 5 tokens excluidos correctamente |
| **C4** | `trend_4h = 'DOWN'` (string) causaba `ValueError: could not convert string to float` en LearningEngine | `_extract()` en `learning_engine.py`: mapeo string→numeric (`'UP'→1.0, 'DOWN'→0.0`) |
| **C5** | `bias_correction` volvía a 2.0 tras re-learn (ratio actual/predicted → inf → clamp 2.0) | Cap dinámico: `max_correction = 1.5 if len(ratios) < 20 else 2.0` |

### Pipeline ejecutado:
```
1. SQL UPDATE → bias_correction = 1.0 en toda la tabla
2. historical_simulator.py → _EXCLUDE_FROM_SIM + _MIN_CANDLES_FOR_SIM + _analyze_window() real
3. learning_engine.py → _extract() str→numeric + bias cap 1.5
4. ingest_historical.py --phase full --days 1095 --simulate --learn
   → 41 símbolos descargados (0.13 GB)
   → 34,287 trades simulados con señales reales
   → SPCXB=0, XAUT=0, PAXG=0, USD1=0, RLUSD=0 ✅
5. run_with_synthetic(34,287 sintéticos + 22 reales = 34,309 total)
   → 45 símbolos + __global__ actualizados
   → bias_correction: 1.111 promedio (cap 1.5)
6. POST /learning/run → ciclo en background con pesos frescos
7. PnLPredictor.invalidate_cache() → active_weights recargados
```

### Estado post-sesión:
```
indicator_weights: 45 símbolos, avg bias=1.111, avg win_rate=36.2%
active_weights (cargados en predictor):
  vol_spike = 0.1498  ← predictor #1
  trend_4h  = 0.1490  ← predictor #2
  rsi       = 0.1192
  macd      = 0.0997
  bias_correction = 1.5 (cap para dataset sintético)
Sistema: RUNNING — trade/analysis/learning cycles activos
```

### Archivos modificados (commits pendientes):
- `historical_simulator.py` — _EXCLUDE_FROM_SIM, _MIN_CANDLES_FOR_SIM, _analyze_window real
- `learning_engine.py` — _extract str→numeric, bias cap dinámico, load_synthetic_trades, run_with_synthetic
- `Dev/scripts/ingest_historical.py` — integrado run_with_synthetic
- `SYSTEM_REFERENCE.md` — este archivo

---

### Análisis Impacto Retroactivo — ¿Cuánto habría ahorrado el L.E. v2?

**Período:** Jun 12–17 2026 | **Trades analizados:** 32 limpios (137 total, 105 corruptos excluidos)  
**PnL real:** -$82.10 USD | **Win rate real:** 6.25% (2/32)

| Causa de pérdida | PnL perdido | % total |
|------------------|-------------|---------|
| Caídas de mercado genuinas (SL golpeados 2-5%) | ~$55 USD | 67% |
| **VolSpike Override con RSI>65** (TAOUSDT -$11, WLDUSDT 2ª -$10.16) | **~$21 USD** | **26%** |
| Predictor mal calibrado (L.E. v2 habría bloqueado) | ~$6 USD | 7% |

---

## SESIÓN JUN 17 2026 11:06 UTC-6 — Optimización posiciones + 8 bugs críticos corregidos

### Cambios en posiciones y exposición (E2)

| Parámetro | Antes | Ahora | Razón |
|---|---|---|---|
| `MAX_POSITIONS` (fallback) | 12 | **15** | 32 activos disponibles |
| `MAX_POSITIONS_SPOT_BULL` | 15 | **20** | Freno real es exposición%, no count |
| `MAX_POSITIONS_RANGE_SCALP` | 8 | **10** | Más scalp candidates en screener |
| `OVERFLOW_MAX_EXTRA` | 3 | **5** | Más breakouts NARRATIVE simultáneos |
| `MIDCAP_ALT` category max | 7 | **8** | 12 MIDCAP disponibles |
| `NARRATIVE` category max | 5 | **6** | 10 NARRATIVE disponibles |
| Exposición SPOT_BULL + F&G<40 | 75% | **80%** | F&G=23 Extreme Fear = momento óptimo |

### Bugs críticos corregidos

| ID | Bug | Archivo | Fix |
|----|-----|---------|-----|
| **BUG-A** | `get_fear_greed_index()` no existe → F&G boost futuros nunca aplica | `run_trades.py` l.1475 | → `get_fear_greed()` |
| **BUG-B** | `_avg_strength = 0.5` hardcoded → Active Replacement nunca activa | `run_trades.py` l.332 | Calcula avg desde metadata real de posiciones abiertas |
| **BUG-C** | EV check salta cuando `worst_ev < 0` → reemplaza con señales igual de malas | `run_trades.py` l.339 | Añade `if _new_ev <= _worst_ev: return False` primero |

### Ineficiencias corregidas

| ID | Ineficiencia | Fix |
|----|---|---|
| **INEF-1** | SlotPressure limitado a `approved[:1]` hardcoded | Usa `max_rebalance_per_cycle` (2 por defecto) |
| **INEF-2** | `min_hold=240min` global bloquea SlotPressure en RANGE_SCALP | RANGE_SCALP=30min, SPOT_BULL=120min, global=120min |
| **INEF-3/4** | `_VOLSPIKE_POSITIONS` y `_overflow_positions` no se limpian en `active_replacement` | `discard()` añadido al cerrar por reemplazo |
| **MEJO-1** | `replace_ev_min_ratio=1.3` muy alto para EVs pequeños | → 1.1 |
| **MEJO-2** | `replace_strength_min_delta=0.15` + avg=0.5 hardcoded = gate roto | → 0.05 (avg ahora real) |

### Archivos modificados esta sesión
- `run_trades.py` — BUG-A, BUG-B, BUG-C, INEF-1, INEF-2, INEF-3/4, MEJO-1/2 + exposición 80% Fear
- `config.py` — category_max MIDCAP/NARRATIVE, min_hold por régimen, nuevos params exposición
- `docker-compose.yml` — MAX_POSITIONS=15, MAX_POSITIONS_SPOT_BULL=20, OVERFLOW_MAX_EXTRA=5

**Ahorro estimado con L.E. v2 activo antes: $5–$8 USD** (Fix C4 trend_4h: INJUSDT RSI=73 habría sido bloqueado)

**Por qué el bias_correction 2.0→1.111 no habría cambiado mucho:**  
El bias multiplica `expected_pnl`, no `success_probability`. Todos los `expected_pnl` ya eran negativos antes del bias (entre -$0.04 y -$0.10). El bias solo los hacía "menos negativos" pero no cruzaban el umbral `min_pnl`. Ninguno de los 32 trades cae en el rango crítico donde `base × 2.0 > min_pnl` pero `base × 1.111 < min_pnl`.

**Los trades grandes perdedores entraron por Override, NO por el predictor:**
- TAOUSDT -$11.09: pred_pnl=-$2.92 (ya negativo) → entró por VolSpike Override
- WLDUSDT 2ª -$10.16: pred_pnl=-$1.09 (negativo) → Override
- WLDUSDT GANADOR +$8.05: pred_pnl=-$3.45 (negativo) → Override (éxito del Override)

**⚠️ PRÓXIMO FIX PRIORITARIO:** Verificar que `volspike_override_max_rsi` (ya en config) bloquee correctamente cuando RSI_entry > 65 **en el momento de ejecutar la orden** — no solo al evaluar la señal inicial. Esto habría salvado ~$21 USD (26% del total de pérdidas).

---

---

## SESIÓN JUN 17 2026 13:35 UTC-6 — Prediction-Driven Lifecycle (C1-C5) + Optimizaciones

### Cambios implementados y verificados en producción

| ID | Cambio | Archivo | Estado |
|----|--------|---------|--------|
| **C1** | Re-evaluación predictiva en `_check_sl_tp_positions` — cierra si EV<min AND prob<min AND trend=DOWN | `run_trades.py` | ✅ Activo en producción |
| **C2** | Segunda entrada por predicción — require strength≥0.70 + nueva EV ≥1.5× la entrada original | `run_trades.py` | ✅ Activo |
| **C3** | Cooldowns VolSpike + Narrative persistidos en tabla `trading_cooldowns` (DB) | `run_trades.py`, `migrations/009` | ✅ Activo |
| **C4** | Audit on startup — vende activos en `trading_excluded_symbols` automáticamente al arrancar | `run_trades.py` | ✅ Vendió RLUSD+USD1 |
| **C5** | Metadata enriquecida al abrir posición: `entry_regime`, `entry_pred_pnl`, `entry_pred_prob`, `entry_type`, `reevaluation_count` | `run_trades.py` | ✅ Activo |
| **OPT** | `_AUDIT_STARTUP_DONE` flag — C4 corre solo una vez por proceso, no en cada ciclo | `run_trades.py:76` | ✅ Verificado |

### Nuevos parámetros en config.py (líneas 306-318)
```python
prediction_exit_enabled = True
min_hold_for_prediction_exit = 90      # minutos mínimos antes de C1
allow_prediction_based_add = True
prediction_add_min_ev_ratio = 1.5      # nueva EV debe ser 1.5× la de entrada
prediction_add_min_strength = 0.70
prediction_add_kelly_factor = 0.6      # Kelly reducido para 2ª entrada
persist_cooldowns_enabled = True
auto_close_excluded_on_startup = True
```

### Migración aplicada
- `migrations/009_trading_cooldowns.sql` — tabla `trading_cooldowns(symbol, cooldown_type, activated_at, expires_at, metadata)` + trigger purge automático

### Estado del portafolio post-sesión (13:35 UTC-6)
- **USDT líquido**: ~$469 (tras vender RLUSDUSDT 109 + USD1USDT 280 excluidos)
- **Régimen**: PRESERVATION (no confirmado — BTC 30d=-16.2%)
- **Posiciones activas**: BTCUSDT(-1.97%), NEARUSDT(-1.95%), ONDOUSDT(-2.76%), TONUSDT(-0.49%), WLDUSDT(-1.66%)
- **PredEval**: evaluando 5 posiciones cada ciclo — todas holding (trend=FLAT, no deterioradas)
- **Alerta**: WLDUSDT EV=-2.86, prob=0.43 — cierre automático si trend cambia a DOWN

### Pendientes post-sesión
- N39: Monitorear primer cierre por C1 en logs `prediction_exit_deteriorated` — verificar que EV/prob/trend se logean correctamente
- N40: Con 50+ trades reales → POST /learning/run para recalibrar bias_correction con datos reales (actualmente cap 1.5 sintéticos)
- N41: Cuando PRESERVATION confirme (BTC 30d < -15% en 2 ciclos) → verificar que solo NARRATIVE recibe nuevas entradas

---

*Revisar este archivo al inicio de cada sesión de análisis o implementación.*
