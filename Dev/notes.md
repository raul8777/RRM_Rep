# Notas de desarrollo

> ⚠️ **Diseño original (obsoleto desde jun 15 2026)** — el sistema opera en modo `auto` con trading real (`DRY_RUN=false`, `ALLOW_REAL_TRADING=true`). Las notas de implementación activas comienzan en las secciones de fecha.

- El agente debe priorizar datos de Binance y TradingView.
- Las noticias deben provenir de Coindesk y Reuters mediante RSS para asegurar información verificable.
- El modo inicial será `suggest`, con opción de `auto` más adelante. ← **SUPERADO: modo auto activo**
- Mantener la implementación privada y local.

---

## 18 Jun 2026 — Futures Expansion + CapBridge + LARGECAP_CAP Guard

### Sesión completa (commits 7c2ed20 → 002f0f4)

#### A — MAX_SHORTS 3→5 (`docker-compose.yml`, `config.py`)
- Hardcodeado en docker-compose para evitar override por host env
- Primer ciclo: BNB + XRP SHORT abiertos automáticamente
- 5/5 slots llenos en <10min, unrealized +$26.89 desde el primer día

#### B2 — Auto-close spot LONGs en FUTURES_SHORT regime (`run_trades.py`)
- Solo cierra posiciones con pnl_pct >= +0.3% (no cristaliza pérdidas)
- Salta stablecoins (precio 0.98-1.02)
- TON y TRX cerrados en primero ciclo; UUSDT skipped correctamente

#### C — Overflow thresholds bajados (`docker-compose.yml`, `config.py`)
- `FUTURES_OVERFLOW_MIN_STRENGTH`: 0.82 → 0.75
- `FUTURES_OVERFLOW_MIN_DROP_24H`: -5.0 → -3.0

#### CapBridge — Transferencia autónoma spot↔futuros (`futures_client.py`)
- `smart_rebalance_capital()`: método único que evalúa ambos wallets cada ciclo
- Case 1: FUTURES_SHORT + slots libres → transfiere de spot a futuros
- Case 2: SPOT_BULL + slots spot libres → transfiere de futuros a spot
- Case 3: 0 shorts abiertos + régimen no-bear → devuelve exceso a spot
- Mínimo transferencia $10, errores siempre logueados con [CapBridge] ERROR:
- Reemplaza `check_and_fund` + `return_idle_funds_to_spot` (se mantienen como fallback)

#### LARGECAP_CAP Guard (`futures_client.py._pre_entry_checks()`)
- Param: `FUTURES_MAX_LARGECAP_SHORTS=5` (default = actual = 5 LARGE_CAPs)
- Param: `FUTURES_LARGECAP_SYMBOLS=["BTCUSDT","ETHUSDT","BNBUSDT","XRPUSDT","SOLUSDT"]`
- **Efecto hoy**: ninguno (5/5 son LARGE_CAPs)
- **Efecto al escalar a 7**: slots 6 y 7 son forzados a ser MIDs (NEAR/SUI/AVAX/LINK/DOT)
- Activar: solo cambiar `FUTURES_MAX_SHORTS=7` en docker-compose + restart

### Estado al cierre de sesión (16:30 UTC-6)
| Par | PnL% | Dist TP |
|-----|------|---------|
| SOLUSDT SHORT | +3.89% | 2.20% ← más próximo al TP |
| ETHUSDT SHORT | +2.97% | 3.17% |
| BTCUSDT SHORT | +2.75% | 3.41% |
| XRPUSDT SHORT | +0.76% | 5.46% |
| BNBUSDT SHORT | +0.34% | 5.73% |
**Total unrealized: +$26.89 sobre $661.95 margin = +4.07%**

### ✅ Slots 6-7 activados — jun 19 2026 08:10 UTC-6
- SOL SHORT cerró en TP: entrada $72.26 → salida $68.02 = **+$15.22 USDT (+11.7% sobre margin)**
- `FUTURES_MAX_SHORTS=7` en docker-compose.yml + `docker-compose up -d app`
- Slots 6-7 = forzados a MIDs por LARGECAP_CAP guard (FUTURES_MAX_LARGECAP_SHORTS=5)
- Sistema reabrió SOLUSDT automáticamente en $68.02 (nuevo SHORT, en -$4.09 al momento del deploy)
- Próximos candidatos MIDs: NEAR, SUI, AVAX, LINK, DOT

### Archivos modificados esta sesión (18 jun)
- `run_trades.py` — B2, CapBridge call, _get_futures_client sync
- `futures_client.py` — smart_rebalance_capital(), LARGECAP_CAP guard, max_largecap_shorts attr
- `config.py` — MAX_SHORTS default, overflow thresholds, bridge params, largecap params
- `docker-compose.yml` — MAX_SHORTS=5→7, overflow hardcoded, bridge + largecap params
- `SYSTEM_REFERENCE.md` — estado actualizado

---

## 19 Jun 2026 — Dashboard UX: modales, hover PnL, multimodal counter, señales

### D1 — Fix `entry_price null` en Futuros Cerrados (`futures_client.py`)
- `get_recent_trades()`: lookup entry_price desde `_open_positions` en memoria
- Fallback matemático: SHORT → `entry = exit + pnl/qty`; LONG → `entry = exit - pnl/qty`
- Agrega `sl_price` y `tp_price` al resultado de trades cerrados (referencia para modal)
- La tabla "Entrada → Salida" en futuros cerrados ahora muestra precio real de entrada

### D2 — Tooltip hover PnL interactivo en gráfica SVG (`dashboard.js`)
- `buildPriceRangeChart()` ahora genera SVG con `cursor:crosshair`
- Al mover el mouse sobre la barra: línea crosshair vertical + tooltip flotante
- Tooltip muestra: `Si precio: $X.XX` + `PnL estimado: ±X.XXXX USDT (±Y%)`
- Color verde si PnL positivo, rojo si negativo (SHORT-aware: sentido invertido)
- Funciona en modales de: spot abierto, spot cerrado, futuros abierto

### D3 — Contador multimodal de posiciones (`dashboard.js`)
- `renderKPIRow()`: reemplaza `maxPos=12` hardcodeado por `openPos.max_positions` dinámico
- En `FUTURES_SHORT`: barra estado = `Spot: N/12 | Fut: M/7` (verde normal, rojo si overflow)
- En otros modos: `Spot: N/max` solo
- KPI tiles legacy: valor = `Ns+Mf`, sub = `Spot: N/12 | Fut: M/7`

### D4 — Señales Recientes overflow fix tab Mercado (`styles.css`)
- `market-col-signals` ampliada: 280px → 340px en el grid
- `#signalsDataMarket .signal-row`: columnas compactas 6-col (85px 52px 72px 40px 1fr 38px)
- Columna DECISIÓN ocultada en tab Mercado (`nth-child(5) { display:none }`)

### D5 — Health Check panel (verificado)
- ✅ Ya implementado completamente en TAB 4 (Sistema) — botón `🩺 Full Health`
- No requirió cambios adicionales

### Archivos modificados (19 jun, sesión 1)
- `futures_client.py` — D1: entry_price fallback, sl_price/tp_price en trades cerrados
- `frontend/dashboard.js` — D2: tooltip SVG hover; D3: contador multimodal
- `frontend/styles.css` — D4: market-col-signals ancho + compact signal-row
- `docker-compose.yml` — MAX_SHORTS=7 (slots 6-7 activados)

---

## 19 Jun 2026 — Bug Fixes: Tooltip, SOL entry_price, Balance Total (sesión 2)

### B1 — Tooltip hover PnL no funcionaba en gráficas SVG
- **Causa raíz**: navegadores bloquean `<script>` insertado via `innerHTML` (seguridad DOM estándar)
- **Fix**: `buildPriceRangeChart()` ahora retorna `{html, uid, entry, isShort, rangeMin, rangeSpan, chartL, chartW}` (ya no emite `<script>`)
- Nueva función global `attachChartHover(uid, ...)` que adjunta eventos mousemove/mouseleave directamente al elemento SVG del DOM
- Callers (`showPositionDetailModal`, `showFuturesDetailModal`) ahora llaman `attachChartHover()` tras setear `innerHTML`

### B2 — SOLUSDT mostraba entry_price = exit_price ($68.02 = $68.02)
- **Causa raíz confirmada**: SOL cerró en $68.02 y reabrió un nuevo SHORT en $68.02. El lookup de `_open_positions` encontraba el SHORT activo nuevo (entry=$68.02) y lo usaba como entry del trade cerrado
- **Fix**: `get_recent_trades()` ahora SIEMPRE calcula `entry_price` matemáticamente (`exit + pnl/qty` para SHORT) — nunca usa `_open_positions` para `entry_price`
- `sl_price`/`tp_price` siguen viniendo de memoria (solo referencia, no crítico)
- Verificado: SOLUSDT ahora muestra `entry_price: $72.26` → `exit_price: $68.02` ✅

### B3 — Balance Portfolio mostraba ~$677 USDT menos del real
- **Causa raíz**: `get_available_usdt()` retorna solo USDT libre en futuros (~$523). No incluye margin comprometido en posiciones abiertas (~$662)
- **Fix**: Nuevo método `get_total_wallet_usdt()` en `FuturesClient` usa `walletBalance` (margin + libre) en vez de `availableBalance`
- `/dashboard-data` ahora usa `get_total_wallet_usdt()` para el grand total
- Verificado: `total_mxn: 50,973 MXN` (vs esperado ~50,577 MXN ✅)

### Archivos modificados (19 jun, sesión 2)
- `futures_client.py` — B2: entry_price siempre math; B3: nuevo `get_total_wallet_usdt()`
- `app.py` — B3: usa `get_total_wallet_usdt()` en `/dashboard-data`
- `frontend/dashboard.js` — B1: `buildPriceRangeChart` retorna objeto; `attachChartHover()` global; callers actualizados
- `tests/test_system_health.py` — HEALTH_VERSION bump → "2026-06-19-v2"

---

## 19 Jun 2026 — SL Smart Enhancement & Tooltip PnL Fix (sesión 3)

### SL Smart — Trailing Stop mejorado para proteger ganancias adicionales
- **Problema identificado**: Trailing solo se activaba con nuevos mínimos, pero no inicializaba `trail_best_price` al alcanzar umbral de activación
- **Síntoma**: ETHUSDT con 2.96% ganancia tenía SL en entry ($1753.79) pero no podía moverse por encima para proteger ganancias
- **Fix implementado**: En `futures_client.py check_positions()`:
  - Inicializar `trail_best_price` cuando `pnl_pct >= activation_pct` por primera vez
  - Logs ahora muestran: `Trailing activated at 1702.0800 (PnL=2.95%)`
  - Una vez activado, trailing espera nuevos mínimos para mover SL > entry price
- **Comportamiento esperado**: Si ETHUSDT baja a $1690, SL se moverá a ~$1707 (protegiendo ~$46 de ganancia)

### Tooltip Hover — PnL real en USD corregido
- **Problema**: Tooltip mostraba solo diferencia de precios por unidad, no PnL real considerando cantidad
- **Ejemplo**: ETHUSDT mostraba +$52.61 (diferencia unitaria) en lugar de +$7.79 (0.148 × $52.61)
- **Fix implementado**:
  - `attachChartHover()` ahora recibe parámetro `qty`
  - Cálculo corregido: `pnlEst = pnlEstUnit * qty` (PnL real en USD)
  - Actualizados callers: spot positions pasa `position.quantity`, futures pasa `pos.qty`
  - Debugging agregado: console.log muestra qty para verificar
- **Resultado**: Tooltip ahora coincide con "PnL No Real" del dashboard

### Estado actual del sistema
- **ETHUSDT**: 2.95% ganancia, trailing activado, SL en $1753.79 (entry)
- **BTCUSDT**: 2.22% ganancia, trailing activado, SL en $64546.2 (entry)
- **App**: Corriendo con mejoras aplicadas, tooltip funcional con PnL real

### Archivos modificados (19 jun, sesión 3)
- `futures_client.py` — Trailing: inicialización de trail_best_price en activation
- `frontend/dashboard.js` — Tooltip: attachChartHover() recibe qty, cálculo PnL real actualizado

---

## 16 Jun 2026 — VolSpike Override V2 + Dashboard PnL Auditoría

### Problema identificado
Sistema comprando con PnL esperado negativo (NEAR accuracy -378%). VolSpike Override demasiado permisivo.

### Solución implementada
**VolSpike Override V2 — controles más selectivos:**
- `volspike_override_min_strength: 0.65` — Requerir alta convicción técnica
- `volspike_override_max_positions: 2` — Máximo 2 posiciones simultáneas  
- `volspike_override_daily_limit: 3` — Máximo 3 overrides por día

**Dashboard PnL Visibilidad:**
- Tablas con "Esperado al Entrar", "Progreso", "Actual vs Esperado"
- Card "🎯 Expectativa vs Realidad" — métricas comparativas
- Card "⚡ Auditoría Override" — tracking VolSpike con win rate y recomendaciones
- Modal de detalle por posición — click en fila muestra: entrada, predicción, indicadores, targets, resultado

### Archivos modificados
- `config.py` — Nuevos parámetros de control
- `run_trades.py` — Validaciones V2 + tracking métricas
- `app.py` — Endpoint `/metrics/override-performance`
- `dashboard.js` — Funciones de auditoría y expectativa vs realidad
- `index.html` — Nuevas cards
- `styles.css` — Estilos de auditoría y progreso

### Criterios de éxito (48h)
- [ ] NEAR no genera más overrides hasta que accuracy mejore
- [ ] <3 posiciones PnL negativo simultáneas  
- [ ] Win rate override ≥ 50%

### Documentación
- `SYSTEM_REFERENCE.md` — Secciones 9 y 10 agregadas
- Memoria persistente creada: VolSpike Override V2 y Dashboard PnL Auditoría
- Archivos en `Dev/`:
  - `volspike-ajuste-selectivo-completado.md`
  - `dashboard-pnl-visibilidad-completado.md`
  - `expansion-e1-completada.md`

---

## 03 Jul 2026 — SMART Total Fase 2 (selector dual)

### Resumen
- Implementado selector de modo SMART Total con persistencia dedicada y auditoría por ciclo.
- Desacoplado selector de perfil para usar histéresis independiente por régimen.
- Integrado orden de decisión en `execute_once`: modo efectivo primero, luego perfil del modo.

### Claves de estado (runtime)
- `smart_total_mode_selector_state`
- `smart_total_active_mode`
- `smart_total_profile_selector_state_<REGIME>`
- Mirror de compatibilidad: `regime_profile_selector_state_<REGIME>`

---

## 03 Jul 2026 — Cierre implementación integral F1-F6

### Resumen
- Se completó hardening del plan en flujo core + dashboard sin reemplazar motores inteligentes.
- Se dejó trazabilidad explícita en runtime state para modo/perfil/params efectivos por ciclo.
- Se reforzó protección operativa para degradación crítica de health y para predicciones degeneradas.

### Cambios clave
1. `run_trades.py`
- Snapshot atómico por ciclo: `effective_runtime_selection_snapshot` con selector+apply_result.
- Inyección `FUTURES_SHORT` ahora exige señal `SELL` y análisis fresco; elimina inyecciones `HOLD/no alineadas`.
- Gate de frescura reforzado + helper de edad de cache.
- Filtro de calidad screener con relajación adaptativa controlada para preservar cobertura operable.
- Safety gate: si `health_results_latest` está `critical/error`, ciclo pasa a `monitor_only`.

2. `pnl_service.py`
- Detección de salida degenerada (`expected_pnl≈0` y probabilidad neutra) fuera de bootstrap.
- Rechazo explícito de esas predicciones para evitar entrada con EV artificialmente plano.

3. `app.py` + frontend
- Nuevo bloque `operator_focus` en `/dashboard/summary`.
- Panel `Operator Focus` en TAB Sistema con cola priorizada por severidad y acción sugerida.

### Verificación rápida
- Archivos modificados revisados sin errores de análisis estático en editor.
- Runner enfocado `Dev/focused_validation_runner.py` reportado `Exit Code: 0`.

### Eventos de auditoría
- `smart_total_mode_selector_decision`
- `regime_profile_selector_decision`

### Config nueva (selector dual)
- Modo: `smart_total_enabled`, `mode_selector_enabled`, `mode_require_confirmed`, `mode_min_hold_cycles`, `mode_min_confidence_delta`, `mode_audit_every_cycle`
- Perfil: `profile_selector_enabled`, `profile_min_hold_cycles`, `profile_min_score_delta`

### Validación rápida ejecutada
- `pytest -q tests/test_health_audit.py -k selector_config_normalization_supports_smart_total_mode_fields` → 1 passed
- `pytest -q tests/test_system_health.py -k SmartTotalSelector` → 2 passed
