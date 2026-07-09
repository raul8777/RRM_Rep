# Crypto-Agent — System Context Rules

> **Memoria persistida del workspace:** este archivo es la referencia canónica que debe consultarse primero en futuras sesiones para mantener contexto, reglas y decisiones del sistema.

Estas reglas aplican automáticamente en toda sesión de trabajo con este workspace.
Son contexto arquitectónico y guía de diagnóstico, no valores hardcodeados.

**Jerarquía de reglas:**
- 🔴 **Regla dura** — nunca se viola (ej. persistir solo tras fill confirmado, PnL siempre neto)
- 🟡 **Guía** — aplica por defecto, puede tener excepciones justificadas (ej. verificar vol24h antes de nuevos activos)
- ⚪ **Contexto** — información arquitectónica de referencia, puede cambiar con el sistema

**Norma obligatoria para cambios relevantes** — responder antes de proponer cualquier modificación:
1. ¿Cómo impacta SPOT y FUTURES por separado?
2. ¿Cómo impacta perfiles A/B/C?
3. ¿Qué gate valida aceptación y cuál gatilla rollback?
4. ¿Qué estado se persiste y qué evento se audita en `trades_audit`?
5. ¿Cómo se mide contra baseline/changepoint (3y/60000)?
6. ¿Qué endpoint/dashboard mostrará la salud del cambio?

Si una propuesta no responde estos 6 puntos, no está lista para rollout.

---

## 1. ARQUITECTURA GENERAL

Bot de trading algorítmico Python/FastAPI corriendo en Docker, operando en Binance SPOT y FUTURES USDM. El orquestador central es `run_trades.py` con función `execute_once()` que conecta todos los módulos en cada ciclo.

**Flujo de datos por ciclo:**
```
market_regime.py              → detecta régimen (SPOT_BULL/RANGE_SCALP/PRESERVATION/FUTURES_SHORT)
    ↓
_apply_regime_params()        → carga overrides del modo activo desde DB → aplica a settings
_apply_mode_profile_harmony() → aplica multiplicadores de perfil A/B/C sobre el resultado anterior
    ↓
pair_screener.py              → candidatos técnicos del universo de análisis
autonomous_signals.py         → genera señales con indicadores técnicos por símbolo
                                 (boosts escalados por pesos del learning_engine — regime-aware)
learning_engine.py            → pesos por indicador (symbol, regime) → escala boosts en autonomous_signals
    ↓
_compute_profit_score()       → profit_score = ev_norm×0.40 + prob×0.30 + strength×0.20 + regime_bonus×0.10
    ↓
portfolio_selector.py         → QUBO: elige K señales ortogonales (maximiza EV, minimiza correlación)
    ↓
_kelly_position_size()        → sizing: base × regime_mult × profile_mult × conviction × concentration × F&G
    ↓
BinanceClient / FuturesClient → ejecuta orden → _verify_entry_filled() → persiste en DB
    ↓
[cada ciclo, sobre posiciones abiertas]
smart_scale_out.py            → SSO-1..7: partial/full exit por 7 triggers adaptativos
profit_realization_supervisor.py → PRS: timing de exit escalado por régimen y perfil
peak_capture_engine.py        → PredExit: advisory de captura de peak (solo en ganancia)
unified_governance_supervisor.py → UG: árbitro final — consolida advisories en acción única
```

**Tablas DB clave** (SQLite `trades.db`):
- `trades_open` — posiciones activas + metadata JSON (`_smart_meta`: trailing, breakeven, SSO state)
- `trades_closed` — historial con PnL neto, comisiones, `regime_at_entry`, `profile_at_entry`
- `trades_audit` — log inmutable de TODA decisión de riesgo (entry, exit, SSO, breakeven, governance)
- `trading_runtime_state` — estado restart-safe: modo activo, perfil, cooldowns, pause_new_entries
- `kelly_sizing_log` — trazabilidad de sizing con todos los multipliers descompuestos
- `regime_profiles` — 12 filas (4 modos × 3 perfiles A/B/C), JSON de overrides por modo/perfil

---

## 2. MÓDULOS — RESPONSABILIDADES Y LÍMITES

| Módulo | Responsabilidad | Límite importante |
|--------|----------------|-------------------|
| `market_regime.py` | Detecta modo con BTC (1d/4h/1h). Anti-whipsaw: requiere 2 ciclos consecutivos para confirmar cambio. | No lee DB, no modifica `settings` |
| `config.py` | Define `Settings` (pydantic-settings v2). Fuente de verdad de defaults. | No lee DB en runtime |
| `config_helpers.py` | `get_param(settings, key)` — acceso seguro con fallback a `_PARAM_DEFAULTS`. | No modifica `settings`; `_PARAM_DEFAULTS` debe ser espejo exacto de `config.py` |
| `run_trades.py` | Orquesta TODO el ciclo SPOT: regime → params → señales → sizing → exits. Módulo central del sistema. | No ejecuta órdenes directamente (delega a `BinanceClient`) |
| `futures_client.py` | Ciclo FUTURES completo: `check_positions()`, trailing, breakeven, SL/TP como algo orders, `_smart_meta` persistence. | No detecta régimen |
| `smart_scale_out.py` | Evalúa SSO-1..7, devuelve `SSOAction`. Lógica pura. | No ejecuta órdenes, no persiste nada |
| `profit_realization_supervisor.py` | PRS: timing de partial/full exit escalado por régimen y perfil. Solo advisory. | No ejecuta órdenes |
| `peak_capture_engine.py` | PredExit: ¿capturar ganancia ahora o seguir corriendo? Solo advisory, solo en PnL > 0. | No evalúa pérdidas, no overridea Hard SL/TP |
| `unified_governance_supervisor.py` | Árbitro final. Recibe advisories SSO + PRS + PredExit → decide acción única. | No genera señales de entrada |
| `binance_commission.py` | Fuente única de tasas de comisión SPOT/FUTURES. `net_pnl()` para calcular neto. | No persiste, no decide |
| `binance_truth_guard.py` | Paridad DB vs Binance real. Detecta: `SPOT_GHOST_IN_DB`, `SPOT_QTY_DRIFT`, `SPOT_ORPHAN_IN_BINANCE`. | No corrige automáticamente (requiere reconcile explícito) |
| `learning_engine.py` | Predictor ML con bias correction por símbolo+régimen. Pesos en DB (`indicator_weights`, clave `(symbol, regime)`). Expone `get_weights(symbol, regime)`. | No decide entradas — devuelve probabilidad y EV. Pesos son no-op en bootstrap (todos = defaults) |
| `portfolio_selector.py` | QUBO selector: cartera óptima de K candidatos. Penaliza correlación Pearson entre pares. | No filtra por régimen (eso lo hace `run_trades.py` antes) |
| `app.py` | API FastAPI: control, hot-config, dashboard, audit. Módulo de interfaz operativa. | No ejecuta ciclos de trading |

---

## 3. CICLO DE VIDA DE UNA POSICIÓN

**Entrada SPOT:**
1. `pair_screener` → candidato → señal en `signal_store`
2. `_buy_top_symbols()` aplica gates secuenciales: régimen confirmado → RSI por modo → filtros BTC → funding rate → predictor ML → entry_quality_gate (todos los sub-gates deben pasar)
3. VolSpike override: señal de volumen excepcional en NARRATIVE bypasa el predictor (pero no los filtros de régimen)
4. QUBO selector → Kelly sizing (`_kelly_position_size()`) → `BinanceClient.buy()` → `_verify_entry_filled()`
5. Solo si fill confirmado: INSERT `trades_open` con `regime_at_entry`, `profile_at_entry`, `config_snapshot_id`, comisión de entrada

**Vida activa (cada ciclo sobre posiciones abiertas):**
- `run_trades.py` itera `position_manager.get_open_positions()`
- Pipeline de exit en orden de precedencia: Hard SL/TP (Binance) → BreakEven → Trailing → SSO → PRS → UG → PredExit
- Hard SL/TP son órdenes algo activas en Binance — se ejecutan sin pasar por el bucle Python
- SSO/PRS/PredExit devuelven advisories; UG consolida en la acción final (nunca múltiples closes en el mismo ciclo)
- La escalación automática de parcial a cierre total está desactivada por defecto: un advisory SSO partial + PRS full no colapsa a 100% sin confirmación explícita de UG

**Salida:**
- Parcial: UPDATE `trades_open` (reduce qty, persiste `_smart_meta` actualizado)
- Total: DELETE `trades_open` + INSERT `trades_closed` (PnL neto, comisiones, motivo, trigger)
- Siempre: INSERT `trades_audit` con event, detail, pnl_pct, pnl_usdt, regime, profile

**Persistencia restart-safe:**
- Estado trailing/breakeven/SSO vive en `trades_open.metadata._smart_meta`
- Cooldowns (VolSpike, SL post-cooldown, mass-SL) en `trading_runtime_state` → recargados al arrancar via `_load_cooldowns_from_db()`
- Régimen + perfil activos en `trading_runtime_state` → sobreviven restart de Docker

**Entrada FUTURES SHORT:**
- Ciclo separado en `futures_client.check_positions()` cada `cycle_futures_short` minutos
- Requiere: modo FUTURES_SHORT activo + BTC sin momentum alcista en 4h/3h/1h + señal técnica SHORT + vela roja 5m confirmada
- SL/TP se colocan como algo orders en Binance (hedge mode, `position_side=SHORT`)
- `_verify_entry_filled()` confirma `positionAmt > 0` antes de persistir — si falla, aborta sin DB write
- Cooldown post-SL por símbolo; mass-SL (múltiples SL simultáneos en una misma ventana temporal) → pausa global configurable vía `trading_runtime_state`

---

## 3.5 SEMÁNTICA OPERATIVA MODO × PERFIL

Cada combinación Modo+Perfil tiene una orientación estratégica distinta. Usarlo para diagnosticar si el perfil activo es coherente con el estado real del mercado.

| Modo | Perfil | Orientación de mercado | Ventaja principal |
|------|--------|----------------------|-------------------|
| SPOT_BULL | A | Tendencia alcista fuerte | Mayor extracción de upside; SSO/PRS más paciente para dejar correr runners |
| SPOT_BULL | B | Alcista moderado | Mejor relación retorno/estabilidad; menos sobreajuste |
| SPOT_BULL | C | Alcista incierto o frágil | Reduce drawdown en alza débil sin apagar captura de tendencia |
| RANGE_SCALP | A | Lateral volátil con rebotes | Máxima velocidad de capital; stagnation/decay agresivos, BE temprano, RSI/BB flexible |
| RANGE_SCALP | B | Lateral normal | Buena tasa de captura sin sobre-trading |
| RANGE_SCALP | C | Lateral sucio o con ruido | Menos falsos cierres/entradas cuando el rango está degradado |
| PRESERVATION | A | Mercado bajista defensivo | Evita infraoperar cuando aparecen oportunidades selectivas válidas |
| PRESERVATION | B | Bajista / riesgo alto | Mejor control de pérdidas y supervivencia de capital |
| PRESERVATION | C | Bajista severo o incertidumbre extrema | Máxima protección de capital |
| FUTURES_SHORT | A | Tendencia bajista clara | Mayor extracción de alfa bajista cuando la caída es persistente |
| FUTURES_SHORT | B | Bajista mixto | Mejor consistencia entre captura y control operativo |
| FUTURES_SHORT | C | Bajista con rebotes peligrosos | Reduce daño por squeezes y whipsaws |

**Combinaciones más agresivas para velocidad de ganancia real:**
- `SPOT_BULL-A` en tendencia alcista limpia y sostenida
- `RANGE_SCALP-A` en lateral bien definido con rebotes confirmados
- `FUTURES_SHORT-A` solo con confirmación bajista y filtros operativos activos (funding rate, momentum BTC)

**Señal de desajuste:** si el perfil activo no coincide con la orientación de mercado actual → revisar si el selector automático está activo y con suficientes ciclos de historial para haber rotado. Ver `trading_runtime_state.smart_total_profile_selector_state_<REGIME>`.

**Lógica de exits por régimen (orientación estratégica):**
- `PredExit`: hold mínimo más corto en RANGE_SCALP y FUTURES_SHORT que en global — mejora timing de corte en mercados rápidos
- `SSO`: en RANGE_SCALP los triggers de stagnation/decay son estructuralmente más agresivos para maximizar rotación de capital; SSO-4 alcanzable con `sso_target_contribution_pct_range_scalp`
- `PRS`: en RANGE_SCALP usa ventanas más cortas para capturar rebotes antes de que reviertan
- `UG`: arbitra SSO + PRS; en modo `monitor_only` (entornos pausados/críticos) la rama defensiva evita closes que no corresponden

---

## 4. SISTEMA DE CONFIGURACIÓN — CAPAS DE PRIORIDAD

```
config.py Field(default=X)              ← baseline compilado en imagen Docker
    ↓ sobreescrito por
.env / variables de entorno             ← overrides de deploy (API keys, flags)
    ↓ sobreescrito por
trading_runtime_state (DB)              ← hot-config runtime (persiste reinicios)
    ↓ aplicado via
_apply_regime_params()                  ← overrides del modo activo desde regime_profiles DB
_apply_mode_profile_harmony()           ← multiplicadores del perfil A/B/C activo
```

**Acceso correcto:** siempre `get_param(settings, 'key')` desde `config_helpers.py`.
Nunca `getattr(settings, 'key', hardcode)` — el hardcode puede quedar stale y no lo detecta el audit.

**Hot-config:** `PATCH /config/kelly` → escribe en `trading_runtime_state`. Solo campos en `_KELLY_EDITABLE_FIELDS` (app.py). Operación es MERGE, nunca REPLACE.

**Nuevo campo:** requiere actualización en 4 lugares: `config.py` (Field) + `config_helpers._PARAM_DEFAULTS` + `run_trades._scalar_map` + `app._KELLY_EDITABLE_FIELDS` (si gobernable por hot-config). Además, si el campo está intencionalmente desviado del default: añadirlo a `_EXCLUDED` en `_check_config_drift` Y en `/config/verify._exclude` para no generar falsos positivos en drift-check y tests.

**`/config/apply` también hace MERGE** (desde S75b): lee `kelly_config_override` existente, mergea solo los campos del body, escribe el resultado. Esto evita que tests o llamadas operativas a `/config/apply` destruyan overrides activos de `PATCH /config/kelly`.

**Perfiles en DB:** `regime_profiles` — 12 filas. Cada fila = JSON de overrides aplicados via MERGE sobre el baseline del modo. El selector de perfiles usa metadata de rendimiento real (`expected_return`, `sharpe`, `win_rate`) — ranking base igualitario (no ponderado por letra).

**Harmony** (`_apply_mode_profile_harmony`): aplica multiplicadores del perfil una sola vez. Si un check interno (como `_check_sso1_stagnant`) vuelve a llamar `_profile_scale()` sobre un valor que ya viene escalado de harmony → doble multiplicación.

---

## 5. GUÍA DE DIAGNÓSTICO — DÓNDE BUSCAR

**Al inicio de sesión sin síntoma concreto — checklist proactivo:**
1. `GET /health` → ¿algún check `critical` o `error`? Si sí → investigar antes de cualquier cambio
2. `GET /dashboard/summary` → bloque `pause_new_entries`: ¿activo sin razón vigente?
3. `SELECT event, count(*) FROM trades_audit WHERE ts > datetime('now','-24 hours') GROUP BY event` → ¿mass_sl, pause, circuit_breaker inesperados?
4. `GET /metrics/unified-governance` → ¿posiciones en pérdida bloqueadas sin cierre?
5. `docker exec app python /app/tmp_audit_config.py` → ¿`CRITICAL mismatch`? Si sí → fix antes de operar

**"El sistema no abre posiciones"**
→ Revisar en orden: `trading_runtime_state.pause_new_entries` → max_positions por régimen → **`category_max_positions_enabled` + límite por bucket** (gate R4: log `LÍM_CATEGORÍA MIDCAP_ALT 2/2` bloquea TODAS las señales si el bucket está lleno) → `min_signal_strength`/`min_success_probability` → filtros BTC (dominance, momentum 4h/3h/1h) → `regime.confirmed` (necesita 2 ciclos) → `entry_quality_gate` → QUBO resultado vacío.
Logs: `[BuyTop]`, `[Screener]`, `[Kelly]`, `[QUBO]`.

**Capital subinvertido (ej. $1k de $5k en posiciones):** verificar `screener_decisions` por rejections con razón `LIM_CATEGORIA` — indica `category_max_positions_enabled=True` y bucket lleno. Solución: `PATCH /config/kelly` con `{"category_max_positions_enabled": false, "max_positions_range_scalp": 6}`. El QUBO sigue minimizando correlación Pearson como diversificación real — no se pierde protección ante correlación.

**"Un exit (SSO/PRS/trailing/BE) no disparó cuando debía"**
→ Verificar que el PnL pasado al supervisor sea neto (via `binance_commission.net_pnl()`), no bruto. Revisar si profile multiplier se aplicó dos veces (harmony + check interno). Ver `trades_audit` para confirmar si el advisory se generó pero UG lo bloqueó.
Logs: `[SSO]`, `[PRS]`, `[UG]`, `[PredExit]`, `[Trailing]`.

**"Posición zombie / inconsistencia DB vs Binance tras restart"**
→ `binance_truth_guard.evaluate_spot_parity()` / `evaluate_futures_parity()` → compara DB vs Binance.
- `SPOT_GHOST_IN_DB`: posición en DB sin saldo real en Binance
- `SPOT_ORPHAN_IN_BINANCE`: saldo en Binance sin registro en DB
Fix: `auto_reconcile_spot()` / `auto_reconcile_futures()` o endpoint `/reconcile`.

**"Parámetro de config no toma efecto"**
→ Verificar presencia en los 4 lugares: `_PARAM_DEFAULTS`, `_scalar_map`, `_KELLY_EDITABLE_FIELDS`, `config.py`. `docker exec app python /app/tmp_audit_config.py` detecta `CRITICAL mismatch`.

**"Sizing incorrecto"**
→ Leer `kelly_sizing_log` para esa entrada: campo `multipliers` descompone cada factor: `base_pct`, `regime_mult`, `profile_mult`, `conviction_mult`, `concentration_mult`, `fng_mult`, `kpi_mult`. El factor más bajo es el cuello de botella.

**"Trailing/BreakEven no activó después de restart"**
→ Estado vive en `trades_open.metadata._smart_meta`. Para FUTURES: `_sync_smart_meta_from_db()` en `FuturesClient` reconstruye desde DB; verificar que `_persist_smart_meta()` se haya llamado en el ciclo anterior. Para SPOT: `run_trades.py` recarga metadata al inicio del ciclo.

**"Régimen oscila o cambia inesperadamente"**
→ Anti-whipsaw: necesita 2 ciclos consecutivos para confirmar cambio. Ver `_cache['pending_mode']` y `_cache['pending_count']` en `market_regime.py`. Con `regime.confirmed=False`, los parámetros se aplican pero el sizing puede reducirse.

**"El predictor rechaza señales buenas"**
→ Ver `indicator_weights` en DB para ese símbolo + régimen activo. Si `bias_correction` alto o `win_rate` bajo → predictor conservador. Cap de bias_correction: 1.5 con <20 trades reales, 2.0 con ≥20. Recalibrar con `POST /learning/run`.

**"Las señales tienen fuerza distinta de la esperada tras cambio de régimen"**
→ Los boosts en `autonomous_signals.analyze_symbol` se escalan por `_lw_scale` cargado de `learning_engine.get_weights(symbol, regime)`. Verificar que `indicator_weights` tiene fila para `(symbol, régimen activo)` o al menos `__any__`. Si solo existe la fila del régimen anterior, los pesos quedan en `__any__` — correcto por diseño hasta que haya suficientes trades del nuevo régimen. Si todos los pesos coinciden con defaults → bootstrap mode, escalado es 1.0 (no-op).

**"profit_score no coincide con el ranking esperado"**
→ Log `[ProfitScore/{REGIME}]` muestra desglose de los 4 componentes. Verificar: `ev_norm` (depende de rango del batch, sensible a outliers de EV), `prob` (viene de `pnl_predictor`), `strength` (technical signal), `regime_bonus` (SPOT_BULL=4h trend, RANGE_SCALP=bb_position, FUTURES_SHORT=RSI). Si EV de todos los candidatos es 0 → ev_norm es 0 para todos, el score recae en prob+strength+regime_bonus.

---

## 6. PATRONES DE BUG RECURRENTES

- **Doble multiplicación de perfil**: harmony aplica el multiplicador de perfil a `sso_stagnant_min_hours_{regime}`. Si `_check_sso1_stagnant()` llama además a `_profile_scale()` internamente → el valor se multiplica dos veces. Buscar en el supervisor cualquier llamada a `_profile_scale()` sobre parámetros de timing.

- **Fallback stale en `getattr`**: `getattr(settings, 'param', OLD_VALUE)` con un valor hardcodeado que difiere del canónico. `tmp_audit_config.py` los detecta como `CRITICAL`. La corrección es usar `get_param(settings, 'param')` que usa `_PARAM_DEFAULTS` como fallback.

- **REPLACE en lugar de MERGE en DB**: cualquier operación que sobreescriba el JSON completo de `kelly_config_override` o `regime_profiles` destruye fixes manuales previos. Siempre hacer `json_merge_patch` o actualizar solo los campos afectados. **Desde S75b `/config/apply` también hace MERGE** — patrón correcto implementado en los 3 endpoints que escriben `kelly_config_override`: `PATCH /config/kelly`, `_apply_regime_params` y `/config/apply`.

- **Campo per-régimen sin equivalente global**: agregar `sso_stagnant_min_hours_range_scalp` sin agregar el fallback global correspondiente → `KeyError` o comportamiento indefinido en otros modos.

- **Persistencia antes de confirmación de fill**: INSERT en `trades_open` antes de `_verify_entry_filled()` → posición zombie si Binance no ejecutó la orden (`positionAmt == 0`).

- **PnL bruto en decisión de exit**: pasar `unrealized_pnl` sin procesar por `binance_commission.net_pnl()` → breakeven o trailing se activa en nivel incorrecto. Todo valor de PnL que alimente SSO/PRS/UG/PredExit/BreakEven/Trailing debe ser neto.

- **Perfil auto-selector sesgado por rank base**: si `profile_rank` no es igualitario entre A/B/C, el selector siempre elige el de mayor rank base ignorando el rendimiento real. El ranking debe emerger exclusivamente de metadata de rendimiento.

- **`_check_config_drift` y `/config/verify` con `_exclude` desincronizados**: si se añade un campo gobernable por hot-config a `_KELLY_EDITABLE_FIELDS` pero no a `_EXCLUDED` en ambos bloques de drift-check, el campo generará false positives (`critical drift`) cuando esté intencionalmente desviado del default. Regla: cualquier campo en `_KELLY_EDITABLE_FIELDS` que pueda diferir del default en producción debe estar en `_EXCLUDED` de `_check_config_drift` Y en `_exclude` de `/config/verify`.

- **Pesos de `indicator_weights` del régimen anterior servidos en el nuevo régimen**: antes de S75, la clave de `indicator_weights` era solo `(symbol)` — los pesos no se invalidaban al cambiar de régimen. Ahora la clave es `(symbol, regime)`. Si `_persist_weights` se llama sin pasar `regime`, guarda en `__any__` (backward-compatible). Aplicar migration `018_indicator_weights_regime.sql` antes de deploy en producción o el `ON CONFLICT(symbol, regime)` fallará.

- **`min_strength` hardcodeado en callers de `get_top_buy_signals`**: cualquier caller que pase `min_strength=0.6` explícitamente anula la resolución desde `settings` y el ajuste por perfil A/C. El caller correcto no pasa `min_strength` — deja que la función lo resuelva internamente.

- **Escalado de boosts con `_lw_scale` aplicado dos veces**: si otro módulo también carga pesos del learning_engine y los aplica sobre una señal que ya pasó por `analyze_symbol`, los boosts se duplican. `_lw_scale` solo se aplica dentro de `analyze_symbol` — no debe re-aplicarse externamente.

---

## 7. SKILLS — ROLES ACTIVOS POR ÁREA

Conocimiento experto que se activa automáticamente según el contexto de cada solicitud.
Cada rol tiene dos niveles: **Principios** (dominio general, siempre válido, permite razonar más allá del código actual) y **En este sistema** (cómo aplica hoy, como referencia, no como límite).

---

**Experto en criptomonedas**
- *Principios:* Los mercados cripto siguen ciclos macro BTC (dominance, halvings, liquidez global). Las narrativas sectoriales (AI, RWA, DePIN, Gaming) tienen ventanas de 2-8 semanas. MEME requiere catalizador + volumen sostenido. Tokenomics (supply, vesting, circulación real) determina la liquidez efectiva de un par.
- *En este sistema:* BTC dominance y F&G son inputs al régimen y al sizing. Narrativas mapean a categorías de `universe_selector.py`. Al proponer nuevos activos, evaluar tokenomics + vol24h antes de incluirlos en el universo.

**Behavioral Finance**
- *Principios:* Los inversores son sistemáticamente irracionales en extremos de miedo/codicia. La capitulación genera oportunidades de compra con EV positivo; el greed extremo genera sobrecompra con reversión probable. El sesgo de recencia hace que los traders eleven SLs tras rachas ganadoras y los bajen tras perdedoras.
- *En este sistema:* F&G modifica Kelly en `_kelly_position_size()`: capitulación amplía base, greed extremo la reduce. No suprimir este mecanismo — es la señal de sizing más robusta conductualmente. Al analizar sizing anómalo: verificar que el multiplicador F&G está activo y su valor actual.

**Actuario / Riesgo sistémico**
- *Principios:* El riesgo técnico (fallo de infraestructura) y el riesgo de mercado (pérdidas normales) requieren respuestas distintas. Pausar operaciones por pérdidas de mercado introduce sesgo de timing y destruye el edge estadístico. Los circuit breakers deben responder a fallos del sistema, no a resultados del mercado.
- *En este sistema:* `pause_new_entries` solo se activa ante: errores API consecutivos, mass-SL en ventana temporal corta, o health `critical`. Una racha de pérdidas sola no justifica pausa automática. Al proponer nuevos circuit breakers: clasificar explícitamente si responden a riesgo técnico o de mercado.

**Microestructura de mercados**
- *Principios:* La liquidez real de un par determina el slippage real de ejecución. Vol24h bajo relativo al notional del sistema = spread efectivo alto = R:R real peor que el modelado. Los filtros de liquidez deben ser conservadores porque los backtests no capturan slippage de mercado profundo.
- *En este sistema:* Gate de vol24h ≥ $3M activo en `universe_selector.py`. Slippage está implícitamente modelado en la diferencia señal/precio de ejecución. Al proponer nuevos activos o ampliar el universo: verificar liquidez antes del backtest, no después.

**Teoría de portafolios**
- *Principios:* La diversificación óptima no es máximo número de posiciones, sino máxima ortogonalidad entre ellas. Activos correlacionados en el mismo bucket amplían el riesgo sin ampliar el retorno esperado. La frontera eficiente de Markowitz maximiza retorno por unidad de riesgo dado un nivel de correlación.
- *En este sistema:* Límites por bucket (LARGE_CAP, MIDCAP_ALT, NARRATIVE, MEME) fuerzan ortogonalidad entre categorías. El QUBO en `portfolio_selector.py` minimiza correlación Pearson entre candidatos. Al expandir el universo o aumentar max_positions: evaluar el impacto en correlación efectiva del portafolio, no solo en cantidad de señales.

**On-chain / microestructura de derivados**
- *Principios:* El funding rate perpetuo es la tasa que los longs pagan a los shorts para mantener posiciones abiertas. Funding positivo alto = longs sobreapalancados = presión de liquidación hacia abajo si el precio cae. Open interest creciente con precio estancado = acumulación de apalancamiento frágil.
- *En este sistema:* Gate de funding rate activo en `run_trades.py` para entradas SPOT. Para FUTURES_SHORT: funding rate positivo = carry a favor del short (ingreso adicional al PnL). Al analizar un par que no entra: verificar funding rate como posible gate activo.

**Experto en trading**
- *Principios:* El edge de un sistema es `EV = WR × TP_net − (1−WR) × SL_gross`. Sin EV positivo, más trades = más pérdidas. El R:R real incluye comisiones en ambos lados. El break-even WR = SL_gross / (TP_net + SL_gross). Un sistema con WR=40% puede ser muy rentable con R:R=2:1.
- *En este sistema:* Universo activo: 32 activos. Al proponer cambios de SL/TP: recalcular break-even WR y comparar contra WR histórico del régimen antes de aprobar. `binance_commission.net_pnl()` es la función canónica para PnL neto.

**Experto en estadísticas**
- *Principios:* Las conclusiones requieren muestras suficientes. Con N<30, el intervalo de confianza para win_rate es tan amplio que cualquier conclusión es ruido. El bootstrap CI para WR con N=100 es ±5-8%. El overfitting en backtests ocurre cuando los parámetros se optimizan sobre la misma ventana que se evalúa.
- *En este sistema:* El `bias_correction` del `learning_engine` compensa el overfitting de datos sintéticos. Cap 1.5 con <20 trades reales, cap 2.0 con ≥20. Antes de proponer cambios basados en live data: pedir N de trades en la cohorte post-changepoint. El baseline es 3y/60000 (Monte Carlo) — no comparar contra muestras de días.

**Experto en inversiones y tendencias**
- *Principios:* El capital en cripto fluye en ciclos predecibles: BTC → ETH → L1s → alts → memes → vuelta a BTC. BTC dominance creciente = rotación hacia BTC = presión bajista en alts. El momentum sectorial tiene memoria de 2-6 semanas. Las narrativas macro (tipos de interés, ETFs, regulación) impactan la correlación del mercado cripto con activos tradicionales.
- *En este sistema:* BTC dominance es input al detector de régimen. Momentum sectorial explica por qué NARRATIVE tokens pueden tener TP más alto que LARGE_CAP en el mismo régimen. Al analizar por qué una narrativa funciona o falla: ubicarla en el ciclo de capital y su fase actual.

**Experto en hacking y seguridad**
- *Principios:* Los secretos en código fuente son comprometidos permanentemente (git history). Los endpoints sin auth en producción son vectores de ataque. Las validaciones de inputs previenen inyección y comportamientos inesperados. La superficie de ataque crece linealmente con el número de endpoints expuestos.
- *En este sistema:* API keys solo en `.env` + `docker-compose.yml`. Endpoints con efectos mutantes requieren `_require_api_key()`. Al agregar endpoints en `app.py`: verificar auth antes de merge. `CORS_ORIGINS` limitado a localhost. Secrets nunca en logs ni en responses de API.

**Experto en performance de BD**
- *Principios:* SQLite es single-writer. Las lecturas pueden bloquearse si hay escrituras frecuentes sin WAL mode. Los índices compuestos en columnas de filtro frecuente (`symbol`, `ts`, `event`) reducen full-table scans. El costo de un INSERT en hot path es lineal con el tamaño de la tabla sin índice.
- *En este sistema:* El sistema escribe a SQLite cada ciclo. `trades_audit` es append-only — no leer en hot path. Índices críticos en `(symbol, ts)` y `(event, ts)`. Al proponer nuevos INSERTs o tablas: evaluar volumen × frecuencia de ciclo. Lentitud de ciclo → buscar full-table scans en `trades_open` o `trading_runtime_state`.

**Experto en investigación de operaciones**
- *Principios:* La optimización combinatoria NP-hard requiere aproximaciones cuando el espacio de búsqueda es grande. Monte Carlo sobre el espacio de parámetros es más robusto que grid search determinístico para encontrar regiones de alta performance. El profit_factor y max_drawdown son más informativos que el PnL absoluto para comparar configuraciones bajo distintas condiciones de mercado.
- *En este sistema:* El QUBO es una aproximación clásica greedy del óptimo real. Con K candidatos de N=32, el espacio es manejable. El backtest 3y/60000 es Monte Carlo sobre parámetros. Al proponer cambios al selector de portafolio: evaluar complejidad computacional del nuevo espacio de búsqueda.

**Experto en ML/IA**
- *Principios:* Un modelo que siempre predice "sí" con umbral bajo puede tener alta accuracy pero EV negativo. El overfitting en datos sintéticos se detecta por la diferencia entre performance en train vs live. Los features más predictivos deben mantenerse estables entre ciclos de reentrenamiento — si cambian mucho, el dataset es insuficiente.
- *En este sistema:* `learning_engine.py` usa regresión logística con pesos por indicador. `bias_correction` compensa distribución sintéticos vs reales. **🔴 Nunca bajar umbrales de probabilidad para forzar más trades** — eso aprueba trades con EV negativo. Features más predictivos: `vol_spike` y `trend_4h`. Recalibrar con `POST /learning/run` cuando hay ≥50 trades reales cerrados.

**Diseñador / UX**
- *Principios:* La jerarquía visual determina qué información procesa primero el operador bajo presión. Las alertas críticas deben ser inmediatamente visibles sin acción del usuario. El color debe codificar estado (rojo=crítico, amarillo=warning, verde=ok) de forma consistente en toda la interfaz.
- *En este sistema:* El dashboard sirve para decisiones operativas en tiempo real. `operator_focus` prioriza: critical > warning > info. Jerarquía de información: estado del sistema → posiciones abiertas → métricas históricas. Badges de contexto (`LEGACY`/`ACTUAL`) son para trazabilidad — no deben dominar la vista principal.

**Experto en Física Cuántica**
- *Principios:* El modelo de Ising y la formulación QUBO (`min xᵀQx`) son la base matemática de los primeros problemas resueltos por computación cuántica (D-Wave). La superposición cuántica permite explorar simultáneamente exponencialmente más estados que cualquier algoritmo clásico. El colapso de función de onda por medición es análogo a la reducción de incertidumbre cuando se obtiene nueva información.
- *En este sistema:* El QUBO en `portfolio_selector.py` es exactamente esta formulación — la solución actual es una aproximación clásica greedy del óptimo cuántico real. Si el universo escala a N=100+ activos con restricciones complejas, esta es la base para migrar a un solver cuántico annealing. El anti-whipsaw de 2 ciclos consecutivos es el "operador de medición" que colapsa la superposición de regímenes a un estado definido.

---

## 8. WOLF HUNTER — PEAK CAPTURE ENGINE

**Filosofía:** No vender en el primer TP fijo — capturar el pico real de ganancia antes de que revierta (optimal stopping aplicado a exits). Motor activo por defecto (`profit_predexit_enabled=True`). Implementado en `peak_capture_engine.py`.

**PredExit tiene DOS ramas independientes bajo el bloque C1 ("Re-evaluación predictiva"):**

- **Rama pérdidas** (`pnl_net < 0`) → `prediction_exit_deteriorated`: cierra cuando EV < umbral mínimo por categoría AND prob < mínimo AND tendencia DOWN o FLAT extremo. Propósito: cortar posiciones que estadísticamente no van a recuperar.
- **Rama ganancias** (`pnl_net > 0`) → Wolf Hunter (`PeakCaptureEngine.evaluate()`): decide si capturar ahora o esperar el pico mayor. Propósito: maximizar extracción de upside.

Ambas ramas requieren que la posición supere el hold mínimo por régimen antes de activarse:
- RANGE_SCALP: `min_hold_for_prediction_exit_range_scalp` (20 min — bounces son cortos)
- FUTURES_SHORT: `min_hold_for_prediction_exit_futures_short` (30 min)
- Global: `min_hold_for_prediction_exit` (90 min)

**Lógica del motor Wolf Hunter (rama ganancias):**
1. Reconstruye `peak_pnl_pct` real: `metadata.trailing_peak` + max de últimas N velas 1m (`peak_reconstruction_lookback_candles`) → persiste en `metadata.peak_pnl_pct_real`
2. Calcula `capture_margin` (umbral de paciencia): base por perfil A < B < C, ajustado por tamaño de posición relativo, convicción (signal_strength × prob), F&G, y régimen
3. Calcula `EV_esperar = prob_higher_peak × upside − (1−prob) × downside + pred_ev`
4. Calcula `giveback_penalty`: penalización si el precio ya cedió parte del peak (cuánto se devolvió desde el máximo)
5. Decisión: si `current_pnl > EV_esperar × capture_margin − giveback_penalty` → capturar; si no → hold

**Capture margin por régimen (factor multiplicador sobre base del perfil):**
- RANGE_SCALP: ×1.05 — más ágil, scalps revierten rápido
- SPOT_BULL: ×0.92 — más paciente, dejar correr runners en tendencia
- PRESERVATION: ×1.20 — tomar cualquier ganancia, modo defensivo
- FUTURES_SHORT: ×1.10 — proteger ganancias por apalancamiento

**Cierre dinámico según `prob_higher_peak`:**
- < 35% → cierre total (100%)
- 35–60% → cierre parcial (50%), deja correr el resto con trailing
- ≥ 60% → hold completo

**Triggers de salida forzada (priority sobre capture normal):**
- `momentum_exhausted` (RSI≥70 en long / RSI≤30 en short) → priority 93, cierre 100%
- `trend_reversal` o F&G≤15 → priority 92, cierre 100%
- Capture normal basado en valor → priority 85

**Integración:**
- Advisory va a UG — nunca overridea Hard SL/TP ni BreakEven
- Auditado en `trades_audit.event='profit_predexit_decision'` cada ciclo donde PnL > 0 y posición pasó hold mínimo
- Peak persistido restart-safe en `metadata.peak_pnl_pct_real` y `metadata.last_peak_price`
- Logs: `[PredExit/Profit]` (rama ganancias) · `[PredExit]` (rama pérdidas) · `[PredEval]` (hold)

**Al diagnosticar Wolf Hunter:**
- Si no dispara en posiciones en ganancia → verificar `profit_predexit_enabled`, hold mínimo por régimen, y si `pnl_usdt_net` supera `profit_predexit_absolute_min_usd`
- Si captura demasiado pronto → revisar `capture_margin` del perfil activo y `profit_predexit_conviction_sensitivity`
- Si captura demasiado tarde → revisar `giveback_penalty_weight` y si `peak_pnl_pct_real` se está reconstruyendo correctamente desde velas 1m

