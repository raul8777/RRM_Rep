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

**🔴 Regla dura — cierres parciales:** todo cierre parcial (SSO, PRS, UG, PredExit) debe insertar un registro en `trades_closed` con `metadata.partial_close=True` y reducir `quantity` en `trades_open`. Nunca se limita a mutar `quantity` o metadata sin fila de cierre. El dashboard debe mostrar PnL realizado + no realizado + total por posición abierta.

**🔴 Regla dura — visibilidad de shadow evidence:** todo mecanismo que se despliegue "en observación"/shadow (no activo en producción real) debe ser visible en `/metrics/shadow-readiness` — agregar una entrada en `app.py::get_shadow_readiness` con `id`, `name`, `mode` (leído de la config real, no hardcodeado), `state`, `summary` con métricas reales, y `gates` con los umbrales de graduación a activo. No es aceptable dejar un flag `*_enabled=False`/modo shadow en el código sin su contraparte visible en el dashboard — la falta de visibilidad fue precisamente lo que permitió que el bug de `range_reversion_shadow`/`invalid_reversion_payoff` (ver s108/s115) pasara desapercibido durante semanas. Al implementar un mecanismo nuevo en shadow, agregar su entrada al dashboard es parte de la misma tarea, no un paso separado opcional.

**🔴 Regla dura — ejecución real con evidencia obligatoria:** cuando una calibración se despliega en `active` (ejecución real), también debe dejar evidencia en `/metrics/shadow-readiness` como control de calidad de rollout. En este caso la tarjeta debe mostrar explícitamente decisiones ejecutadas vs decisiones que se habrían diferido/cortado, con gates de muestra mínima para recalibrar umbrales. No se acepta un mecanismo activo sin telemetría de evidencia comparable.

**�🟡 Guía — limpieza de candidatos activos tras un fix de evaluación/priorización:** todo cambio que altere cómo se evalúan, confirman o priorizan candidatos (`staged_entry.py::_direction_transition`, `get_openable_candidates`, gates de ejecución en `run_trades.py`) debe cerrar con una revisión de `ready_candidates` en estado `ready`/`validated`: consultar `data.last_execution_gate`, `data.direction`, y el conteo de `risk_queue_selected`/`execution_attempt_blocked` en `ready_candidate_checks` por candidato, para detectar y descartar (`_discard_ready`) cualquiera que haya quedado en un estado inconsistente con la lógica nueva o arrastrando el patrón que motivó el fix (ver s110/s111, HEIUSDT/RIFUSDT monopolizando la cola). No dar el fix por cerrado sin este chequeo, aunque el resultado sea "nada que limpiar".

## s115 — revalidación de tesis S/R al ejecutar (activo, 5%) + visibilidad shadow obligatoria (ago 3 2026)

- **`_staged_sr_thesis_check` (`run_trades.py`):** antes de ejecutar un SHORT/LONG vía `STAGED_SHORT_DIRECTION`/`STAGED_FUTURES_LONG_DIRECTION`/`STAGED_SPOT_LONG_DIRECTION` (la ruta que bypassea el arbitraje una vez la FSM de dirección confirma el lado), revalida que el precio esté realmente cerca del nivel S/R de su propia tesis (`resistance_dist_pct`/`support_dist_pct` ≤ 5%, o una continuación de breakdown/breakout genuina). Config: `range_scalp_sr_thesis_gate_enabled` (default `True`), `range_scalp_sr_thesis_max_dist_pct` (default `5.0`). Activo, no shadow — backtest de 48h mostró +$212 de mejora neta incluso descontando el costo de oportunidad de ganadores reales bloqueados.
- **Regla de visibilidad shadow** (ver regla dura arriba): se agregaron a `/metrics/shadow-readiness` las entradas `direction_volume_gate` y `staged_immediate_entry` (mecanismos shadow de s113 que no tenían visibilidad hasta ahora) y `sr_thesis_gate` (este mecanismo, activo).
- Ver `canonical/calibracion_entradas-a20ab8-v3.md` §s115 para el detalle completo, incluida la simulación retrospectiva de 48h con el trade-off exacto (pérdidas evitadas vs. ganancias sacrificadas).
- Rollback: `range_scalp_sr_thesis_gate_enabled=false` revierte al comportamiento anterior sin tocar ningún otro mecanismo.

## s113 — plan completo de ciclo de entradas: concurrencia SPOT, escalonamiento, ATR, VSA, watcher universal, entrada inmediata (ago 3 2026)

- **Lock de concurrencia en SPOT (`position_manager.py`):** nuevo `self._close_lock` (RLock), mismo patrón que `futures_client.py` desde s105h — el ciclo normal y `monitor_spot_hard_stops_once` (2s) ya no pueden duplicar un cierre.
- **Escalonamiento sin descarte terminal:** `_symbol_has_open_futures_position(..., allow_scale_in_ready_id=X)` — una posición etiquetada con `scale_in_ready_id=X` no bloquea a su propio candidato agregando una segunda pierna.
- **Anclaje ATR al SL real (`_atr_anchor_sl_pct`):** ensancha (nunca aprieta) el SL de % fijo en SPOT y FUTUROS. El guardarrail `max_loss_usd_per_trade` sigue siendo el techo — esto solo corrige el punto de partida.
- **Corroboración de volumen (VSA/Wyckoff) en `_direction_transition`:** shadow por defecto (`direction_volume_gate_enabled=False`) — se mide, no se exige, hasta validar contra resultados reales.
- **`intra_mode="watcher"` en `FUTURES_SHORT`/`PRESERVATION`:** mismo fix de s106 ya validado en `RANGE_SCALP`, extendido a los otros 2 regímenes que comparten el mismo dispatch genérico.
- **Entrada inmediata escalonada (`maybe_open_immediate_leg_range_scalp_short`):** apagada por defecto (`staged_immediate_entry_enabled=False`) — el mecanismo de mayor riesgo del plan, valida con fracción pequeña antes de ampliar.
- Ver `canonical/calibracion_entradas-a20ab8-v3.md` §s113 para el detalle completo, datos que motivaron el plan, y pendientes.
- Rollback: cada mecanismo tiene su propio flag (`*_enabled`) independiente — se puede revertir uno sin afectar los demás. El lock de SPOT y el fix de escalonamiento son aditivos (sin flag, pero inertes si nada los ejercita).

## s111 — cola de riesgo consciente de viabilidad + confirmación de dirección anclada a geometría (ago 2 2026)

- **Prioridad de cola (`staged_entry.py::get_openable_candidates`):** un candidato cuyo último intento real de ejecución falló (`data.last_execution_gate.passed=False`) cae detrás de todos los que no han fallado, sin importar su score de EV/calidad. Medido en 48h: 93% de las selecciones de cola las consumían candidatos que nunca abrían, monopolizando `max_open_per_cycle` y bloqueando candidatos viables.
- **Confirmación de dirección (`staged_entry.py::_direction_transition`):** `RANGE_SCALP` ancla `direction_min_progress_bps`/`direction_max_adverse_bps` a un % de la distancia al stop real del trade (`direction_geometry_anchor_enabled`, opt-in por régimen) en vez de bps fijos idénticos para todo símbolo. Las degradaciones (`DIRECTION_RECOVERY_PENDING`/`DIRECTION_DRAWDOWN_SOFT`) decrementan `directional_confirmations` en vez de resetear a 0.
- Ver `canonical/calibracion_entradas-a20ab8-v3.md` §s111 para el detalle completo (causas raíz, datos de 48h, decisiones de diseño).
- Rollback: `direction_geometry_anchor_enabled=false` en el config de `RANGE_SCALP` revierte a bps fijos; revertir `priority()` a la tupla original de 4 elementos revierte la deprioritización por fallo de ejecución.

## s110 — gates finales de ejecución condicionados a demora real (ago 2 2026)

- **Regla de diseño (explícita del usuario):** la única razón legítima para volver a validar recompensa:riesgo o elegibilidad de un candidato `validated`/`direction_confirmed` es que haya esperado genuinamente por falta de capital o de slots disponibles. Fuera de ese escenario, debe abrir de inmediato al primer indicio de rompimiento hacia la dirección esperada — no re-derivar la tesis desde cero.
- `run_trades.py::_candidate_delayed_by_capital_or_slots()`: único punto de verdad para "¿esto esperó de verdad?" — capital (`data.capital_routing.retry_count`/`status`) o slots (`data.stage_reason in (RISK_BLOCKED, ROUTING_BLOCKED)`).
- `_revalidate_entry_before_submit` (R:R≥2.0, s109) y `futures_executable` (cacheado desde `signal_meta.directional_arbitration` en vez de re-derivar del top-N rotativo del screener) deben respetar esta condición — ver `canonical/calibracion_entradas-a20ab8-v3.md` §s110 para el detalle completo.
- Rollback: revertir `_candidate_delayed_by_capital_or_slots` a `return True` incondicional (comportamiento pre-s110) si se detectan aperturas prematuras sobre setups genuinamente agotados.

## s104 — disciplina documental architecture-first (jul 29 2026)

- El canónico `canonical/calibracion_entradas-a20ab8-v3.md` debe iniciar siempre con:
  - diagrama de arquitectura del pipeline de calibración de entrada,
  - descripción de módulos y límites operativos.
- Cambios de flujo de entrada: primero se actualiza ese bloque canónico, después `rules.md` y `SYSTEM_REFERENCE.md`.
- Módulos que deben permanecer sincronizados en documentación:
  - `market_regime.py`, `pair_screener.py`, `autonomous_signals.py`, `entry_profile_gate.py`, `portfolio_selector.py`, `staged_entry.py`, `pnl_service.py`, `entry_decision.py`, `run_trades.py`, `analysis_cycle.py`, `external_data_client.py`, `technical_graph_analyst.py`, `app.py`.

### Contrato SR canónico (sin bypass)

- Para señales SR, el estado `ready` representa espera activa de rompimiento/progreso direccional; no autoriza apertura por sí solo.
- La secuencia obligatoria es: `ready` -> checks inter/intra -> `direction_confirmed` -> `validated` -> preflight -> ejecución -> fill confirmado -> `opened`.
- Si preflight o ejecución bloquean, se persiste razón terminal/no terminal en `ready_candidates.data` y se audita el evento.
- Queda prohibido abrir posición sin confirmación de fill Binance/futures.

## s93 — Despliegue de umbrales universal (jul 23 2026)

- `entry_profile_thresholds.json` activo con umbrales por **régimen + categoría** (LARGE_CAP, NARRATIVE, MIDCAP_ALT).
- `entry_profile_gate.py` aplica el modelo universal `quality_score_model_universal_proposal.pkl` cuando detecta el marcador `universal` en el archivo de thresholds.
- Backup de thresholds disponible en `entry_profile_thresholds_pre_universal_20260723.json`.
- Monitorización obligatoria: `score_source == "universal_model"`, cobertura real ~8-15%, PnL neto y win rate vs. proyecciones.
- Rollback: restaurar backup thresholds y `docker compose restart app`.

---

## 1. ARQUITECTURA GENERAL

Bot de trading algorítmico Python/FastAPI corriendo en Docker, operando en Binance SPOT y FUTURES USDM. El orquestador central es `run_trades.py` con función `execute_once()` que conecta todos los módulos en cada ciclo.

**Flujo de datos por ciclo (Peaks-first pipeline s79):**
```
market_regime.py              → detecta régimen (SPOT_BULL/RANGE_SCALP/PRESERVATION/FUTURES_SHORT)
    ↓
_apply_regime_params()        → carga overrides del modo activo desde DB → aplica a settings
_apply_mode_profile_harmony() → aplica multiplicadores de perfil A/B/C sobre el resultado anterior
    ↓
pair_screener.py              → candidatos técnicos del universo de análisis
autonomous_signals.py         → genera señales + asigna peak_quality_profile vía entry_profile_gate.classify
                                 (A/B/C/discard basado en expected_pnl/score calibrado)
entry_profile_gate.py         → score de calidad y umbrales A/B/C calibrados sobre outcomes reales
learning_engine.py            → pesos por indicador (symbol, regime) → escala boosts en autonomous_signals
    ↓
_filter_buy_candidates()      → ANTES de QUBO (s80): hard-reject cualquier perfil != A/B/C;
                                 legacy entry_quality_gate opcional y desactivado por defecto.
                                 entry_quality_gate_mode: "disabled"|"advisory"|"blocking"
    ↓
_compute_profit_score()       → pq_norm×0.40 + ev_norm×0.20 + prob×0.20 + strength×0.10 + regime_bonus×0.10
    ↓
portfolio_selector.py         → QUBO: revenue = 0.6×peak_quality + 0.4×predictor_pnl (Peaks dominates)
                                 Correlation penalty + cardinality + risk unchanged
    ↓
_buy_top_symbols():
  pnl_service.validate()      → predictor_gate_mode: "advisory" (Profile A/B bypass) | "blocking" | "disabled"
                                 gate_warnings persisted in trades_open metadata
  _kelly_position_size()      → sizing: base × regime_mult × profile_mult × conviction × concentration × F&G
    ↓
BinanceClient / FuturesClient → ejecuta orden → _verify_entry_filled() → persiste en DB (+ gate_warnings)
  ↓
[cada ciclo, sobre posiciones abiertas]
1s telemetry layer            → snapshots vivos que alimentan SSO/PRS/UG/PredExit y protegen profit/drawdown sin crear riesgo nuevo
smart_scale_out.py            → SSO-1..7: partial/full exit por 7 triggers adaptativos
profit_realization_supervisor.py → PRS: timing de exit escalado por régimen y perfil
peak_capture_engine.py        → PredExit: advisory de captura de peak (solo en ganancia)
unified_governance_supervisor.py → UG: árbitro final — consolida advisories en acción única
```

：**Feature flags (s80) — entry-profile gate redesign:**
- Ver detalle completo en `.windsurf/plans/calibracion_entradas-db0300.md`.
- Plan maestro de calibración agresiva actualizado: `.windsurf/plans/Calibracion_Entradas-a20ab8-v3.md` (merge con v2; v2 eliminado).
- `entry_profile_gate_enabled`: bool (default True) — gate A/B/C basado en umbrales calibrados sobre outcomes reales
- `entry_profile_gate_thresholds_path`: ruta a `Dev/benchmarks/entry_profile_calibration/entry_profile_thresholds.json`
- **SPOT_BULL quality-score model**: `Dev/benchmarks/entry_profile_calibration/quality_score_model_spot_bull.pkl` se carga lazy en `entry_profile_gate.compute_quality_score()` y predice `realized_net_pnl_pct` cuando todas las features requeridas están presentes. Si no, se usa fallback heurístico.
- **Umbrales v3 recalibrados post-sweep (realized_net_pnl_pct, costo 0.0025)**: `SPOT_BULL` A=0.6673, B=0.3361, C=0.2456; `RANGE_SCALP` A=B=C=0.0917.
- `peaks_first_enabled`: bool (default True) — Peaks profile drives selection priority
- `predictor_gate_mode`: "advisory"|"blocking"|"disabled" (default "blocking")
- `entry_quality_gate_mode`: "advisory"|"blocking"|"disabled" (default "disabled") — legacy behavior-first gate, superseded by entry_profile_gate
- `evidence_gate_enabled`: bool (default False) — legacy Bayesian posterior gate, superseded
- `volspike_override_enabled`: bool (default False) — override narrative desactivado bajo el nuevo gate
- Rollback inmediato: `PATCH /config/kelly {"entry_profile_gate_enabled": false, "entry_quality_gate_mode": "blocking", "predictor_gate_mode": "advisory"}`

**Datos de investigación y calidad (s91):**
- Autoridad canónica: `canonical/calibrar_datos.md`. Producción (`trades_closed` neto agrupado por posición) es la única fuente para afirmar edge desplegable; peaks/MIMIC/backtests son auxiliares y conservan `source` separado.
- Linaje mínimo: `entry_selection_candidates` → `ready_candidates` → `ready_candidate_checks` → `metadata.ready_candidate_id` en posiciones/cierres. No unir históricos por símbolo sin etiquetarlos `unlinked_legacy`/heurísticos.
- `range_scalp_live_v1` se captura antes de `entry_profile_gate` y se persiste como schema, values, missingness y completeness. No imputar un faltante a cero para decisión o entrenamiento.
- RANGE_SCALP permanece `shadow` hasta completeness >=95% y holdouts temporales con PnL neto >0, PF>=1.15, cobertura>=5%, DD no peor y n>=30 para tier A. Rollback: `range_scalp_quality_model_mode=shadow`; no toca posiciones, sizing ni lifecycle.
- Extractores reproducibles: `Dev/scripts/build_research_manifest.py` y `Dev/scripts/build_research_outcomes.py`; sus artefactos viven en `Dev/benchmarks/research_data/`.
- Observabilidad operativa: `GET /metrics/entry-evidence` alimenta la tarjeta Evidence Shadow con funnel, cobertura `range_scalp_live_v1`, lineage exacto/legacy/roto, outcomes netos y gates; es estrictamente read-only.
- Descubrimiento agnóstico: `pair_screener.opportunity_shadow` clasifica el ticker USDT en exclusión estructural, `OBSERVE` y `ELIGIBLE`; `GET /screener/opportunity-shadow` es lectura autenticada. El shadow no modifica universo de órdenes, QUBO, staging, Kelly, SL/TP ni routing.
- Labels de oportunidad: `opportunity_labels.py` y `Dev/scripts/build_opportunity_forward_labels.py` son la fuente reproducible de `forward_gross/net/mfe/mae_2h|4h`. Son oportunidad direccional con fricción versionada, no PnL/fill real; siempre separar LONG/SHORT, SPOT/FUTURES y coste 1x/1.5x/2x.
- Resolución RANGE_SCALP: setup primario `1h`, contexto `4h`; `range_scalp_timing_shadow` 15m es telemetría de timing sólo para candidatas finalistas. No se usa como feature requerida, ranking, gate o autorización de orden hasta completar comparación prospectiva/walk-forward purgado contra baseline 1h.

**Tablas DB clave** (PostgreSQL `crypto_agent`):
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
| `binance_truth_guard.py` | Paridad DB vs Binance real. Detecta: `SPOT_GHOST_IN_DB`, `SPOT_QTY_DRIFT`, `SPOT_ORPHAN_IN_BINANCE`. | Auto-fix corre ghosts/orphans >$5 en pretrade gate; dust < $5 requiere `/reconcile/cleanup-dust` |
| `learning_engine.py` | Predictor ML con bias correction por símbolo+régimen. Pesos en DB (`indicator_weights`, clave `(symbol, regime)`). Expone `get_weights(symbol, regime)`. | No decide entradas — devuelve probabilidad y EV. Pesos son no-op en bootstrap (todos = defaults) |
| `portfolio_selector.py` | QUBO selector: cartera óptima de K candidatos. Penaliza correlación Pearson entre pares. | No filtra por régimen (eso lo hace `run_trades.py` antes) |
| `app.py` | API FastAPI: control, hot-config, dashboard, audit. Módulo de interfaz operativa. | No ejecuta ciclos de trading |

---

## 3. CICLO DE VIDA DE UNA POSICIÓN

**Entrada SPOT:**
1. `pair_screener` → candidato → señal en `signal_store`.
2. `autonomous_signals.py` asigna `peak_quality_profile` vía `entry_profile_gate.classify()` (A/B/C/discard). Discard = descarte inmediato.
3. `_filter_buy_candidates()` (s80) aplica únicamente el entry-profile gate: cualquier perfil distinto de A/B/C se descarta. El legacy behavior-first gate (`entry_quality_gate_mode`) está desactivado por defecto y solo se reactiva bajo análisis explícito.
4. `pnl_service.validate()` / predictor gate según `predictor_gate_mode` → QUBO selector → `staged_entry` persiste `ready`; re-chequeos inter/intra-ciclo lo convierten en `validated` tras los `required_positive_checks`.
5. **Regla dura de latencia:** una validación intra-ciclo activa `trade_cycle_wake_event`; el scheduler no envía órdenes. El único trade cycle despierta y recorre la ruta canónica `execute_once()` → `get_openable_candidates()` → `_buy_top_symbols()` sin esperar el siguiente intervalo.
6. La apertura sigue siendo fail-closed: ranking EV-first y `max_open_per_cycle`, régimen activo, ausencia de posición duplicada, capital, preflight, Kelly sizing y `_verify_entry_filled()` siguen siendo obligatorios. Las validaciones concurrentes se coalescen en un solo ciclo; nunca autorizan una orden directa por candidata.
7. **🔴 Confirmación direccional staged:** `direction_confirmation_mode="enforced"` es obligatorio en `SPOT_BULL`, `RANGE_SCALP`, `FUTURES_SHORT` y `PRESERVATION`. El stage registra el setup y `base_price`, pero no es una confirmación. La apertura requiere `EntryDecision` elegible y dos avances consecutivos de al menos `direction_min_progress_bps`.
8. LONG debe avanzar; SHORT debe descender; RANGE_SCALP además debe reducir distancia a un target válido. Perfil A/B/C, QUBO, EV y predictor no reemplazan esta condición.
9. Un único retroceso limitado cambia a `recovery_pending` sin sumar checks positivos ni autorizar una orden. Solo se recupera al recapturar el setup y completar otro avance consecutivo. Un segundo retroceso, stop/drawdown, precio/referencia ausente, target inválido o expiración descarta terminalmente.
10. Todo check persiste `ready_candidate_checks.input_features.direction`. Un descarte terminal tiene `decision="negative"`, razón `DIRECTION_*` y copia su contexto en `ready_candidates.data.last_rejection`; `recovery_pending` se registra como `decision="pending"`.
11. La posición abierta conserva `ready_candidate_id` y `staged_direction` en metadata para atribuir el PnL realizado a su cadena de validación.
12. Defaults activos: `direction_min_progress_bps=3.0`, `direction_max_adverse_bps=50.0`, `direction_required_confirmations=2`. En todo régimen de entrada, `direction_confirmation_mode="enforced"`; una candidata no puede permanecer `validated` sin `state="confirmed"` y dos confirmaciones. Ajustar los dos espejos canónicos (`config.py` y `config_helpers.py`) y comparar por régimen/ventana, no por una racha aislada.
13. La validación staged no reemplaza el preflight final. Cada rechazo final se persiste como `data.last_execution_gate`; dashboard muestra `Pendiente de preflight` o `Dirección sin confirmar`, nunca `Lista para abrir` en esos estados.

**Monitoreo obligatorio de dirección:** por cada ventana de 24 h, medir staged, `recovery_pending`, validadas, abiertas, descartadas, razones `DIRECTION_*`, cobertura de aperturas y PnL neto de posiciones con `ready_candidate_id`. Si el rechazo terminal supera 90% o la cobertura cae materialmente, analizar `progress_bps`, `base_progress_bps` y `target_progress` antes de cambiar umbrales o desactivar la guarda.

**Vida activa (cada ciclo sobre posiciones abiertas):**
- `run_trades.py` itera `position_manager.get_open_positions()`
- Pipeline de exit en orden de precedencia: Hard SL/TP (Binance) → BreakEven → Trailing → 1s telemetry snapshot → SSO → PRS → UG → PredExit
- Hard SL/TP son órdenes algo activas en Binance — se ejecutan sin pasar por el bucle Python
- SSO/PRS/PredExit devuelven advisories; el 1s telemetry layer también los recalcula sobre snapshots vivos, mientras UG consolida en la acción final (nunca múltiples closes en el mismo ciclo)
- La escalación automática de parcial a cierre total está desactivada por defecto: un advisory SSO partial + PRS full no colapsa a 100% sin confirmación explícita de UG
- Regla de stop inteligente 1s: no cortar por umbral fijo aislado. Al cruzar drawdown se abre ventana de rescate (`monitoring`/`recoverable`), y el corte duro se habilita cuando hay `failed_recovery` o `shock` adverso confirmado.

**Salida:**
- Parcial: INSERT `trades_closed` (PnL neto proporcional, comisiones, motivo, trigger, `metadata.partial_close=True`) + UPDATE `trades_open` (reduce qty, persiste `_smart_meta` actualizado)
- Total: DELETE `trades_open` + INSERT `trades_closed` (PnL neto, comisiones, motivo, trigger)
- Siempre: INSERT `trades_audit` con event, detail, pnl_pct, pnl_usdt, regime, profile
- Cierre manual desde dashboard: modal de posición abierta → preview (`GET /positions/{symbol}/close/preview`) → confirmación → `POST /positions/{symbol}/close`. Se audita como `manual_close` y se excluye de learning.

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
→ Revisar en orden: `trading_runtime_state.pause_new_entries` → max_positions por régimen → **`category_max_positions_enabled` + límite por bucket** (gate R4: log `LÍM_CATEGORÍA MIDCAP_ALT 2/2` bloquea TODAS las señales si el bucket está lleno) → `min_signal_strength`/`min_success_probability` → filtros BTC (dominance, momentum 4h/3h/1h) → `regime.confirmed` (necesita 2 ciclos) → `entry_profile_gate` (rechazo `PEAK_QUALITY_DISCARDED`) → `entry_quality_gate` (legacy, desactivado por defecto en s80) → QUBO resultado vacío.
Logs: `[BuyTop]`, `[FilterBuy]`, `[Screener]`, `[Kelly]`, `[QUBO]`.

**"Todas las señales son PEAK_QUALITY_DISCARDED" (entry_profile_gate s80)**
→ Revisar:
1. Que `entry_profile_gate_thresholds_path` exista y tenga el formato `{ "thresholds": { "SPOT_BULL": {"A":..., "B":..., "C":...}, ... } }`.
2. Que `entry_profile_gate_enabled=True` y que `entry_quality_gate_mode` no esté en `blocking` (legacy, desactivado por defecto).
3. Que `autonomous_signals.py` esté llamando `entry_profile_gate.classify()` y propagando `expected_pnl`/`success_probability`/`strength`/`vol_spike`.
4. Que el score (`peak_quality_score`) sea ≥ umbral C para el régimen activo. Validación histórica: `python Dev/scripts/validate_entry_profile_gate.py --limit 50000`.
Rollback: `PATCH /config/kelly {"entry_profile_gate_enabled": false, "entry_quality_gate_mode": "blocking"}`.


**"RANGE_SCALP no abre posiciones" (gates específicos s78)**
→ Si todos los candidatos son rechazados con `RANGE_VOL` o `RANGE_BB`, verificar los 3 parámetros calibrados en s78:
1. `entry_quality_vol_spike_min_range_scalp` (DB override) — debe ser ≤ 0.20 en F&G bajo. Valor 0.35+ bloquea todos los candidatos en mercados de Extreme Fear donde vol es estructuralmente bajo.
2. BB gate en `_check_entry_quality()` (`run_trades.py` ~línea 4699) — si `bb_position is None` y `not _bb_touch_ok` → skip gate (no rechazar). Señales autónomas pueden no incluir `bb_position`; dato faltante ≠ posición incorrecta.
3. `kelly_multiplier` RANGE_SCALP en `market_regime.py` línea 253 — debe ser 0.95 (no 0.70). Con caps de liquidez (90%) y concentration multiplier activos, ×0.70 aplasta el sizing innecesariamente.
⚠️ **Estos 3 parámetros fueron calibrados y validados en producción (jul 14 2026, 5/5 cierres positivos). No modificar sin análisis de impacto.**

**"SPOT_BULL rechaza breakouts fuertes / rechaza por volúmen" (extensión s79)**
→ Si el modo SPOT_BULL sigue rechazando señales con RSI y volumen elevados, revisar la vía de breakout controlado en `_check_entry_quality()` (`run_trades.py`) y sus parámetros gobernables: `entry_quality_breakout_min_vol_spot_bull`, `entry_quality_breakout_rsi_max_spot_bull`, `entry_quality_breakout_min_strength_spot_bull`. La regla base sigue siendo pullback-first; la extensión solo evita rechazos automáticos de breakouts con convicción suficiente.
→ Para rechazos por volumen bajo (`SPOT_BULL_VOL`), los parámetros por perfil son: `entry_quality_vol_spike_min_spot_bull` (default 0.85) y `entry_quality_vol_spike_floor_spot_bull` (default 0.70). Perfil A actual: `min=0.50`, `floor=0.45` → umbral efectivo ~0.46. Perfil B: `min=0.50`, `floor=0.45` → umbral efectivo 0.50.

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

**"El sistema no abre posiciones tras docker compose up (reinicio post-stop)"**
→ Cuando el operador presiona DETENER en el dashboard, se persiste `pause_new_entries=True` con reason=`operator_manual_stop` en `trading_runtime_state`. Este flag persiste en DB incluso tras `docker compose down/up`. El sistema arranca normalmente (SL/TP funcionan) pero nunca evalúa entradas nuevas.
**Procedimiento de reanudación (aplicar desde dashboard o API):**
1. Dashboard: pestaña "Acciones sensibles" → botón **"▶ Reanudar"** → confirmar.
2. API: `DELETE /control/pause-new-entries` (elimina solo la pausa) o `POST /control/resume-all` (limpia pausa + capital_routing_guard + operator_alerts + inicia orchestrator si estaba detenido).
3. Verificar en logs: el mensaje `[execute_once] pause_new_entries=True (operator request)` debe desaparecer y el ciclo debe generar señales autónomas.
4. Si el CapBridge no transfiere capital a spot tras la reanudación (spot < $50 y futures > $1000 en SPOT_BULL), verificar que `futures_capital_bridge_enabled=True` y que el fix s119 está desplegado.
**Causa raíz del problema histórico (s119):** cuando `futures_arbiter_active=True` y el régimen es SPOT_BULL/RANGE_SCALP, el `smart_rebalance_capital` entraba en Case 1b (elif) que matcheaba el régimen pero no necesitaba transferir spot→futures — y al ser elif, nunca evaluaba Case 2 (futures→spot para spot). Fix: Case 1b ahora cae through a evaluar si spot necesita capital cuando futures no necesita más.
**Checklist completo de reinicio tras detener:**
- [ ] `pause_new_entries` limpio (via botón Reanudar o `DELETE /control/pause-new-entries`)
- [ ] `capital_routing_guard` inactivo (se limpia con `/control/resume-all`)
- [ ] No hay `operator_alerts` activas bloqueantes
- [ ] Health status = ok (no `critical`)
- [ ] CapBridge transfiere capital si spot < needed (visible en logs como `[CapBridge] ... → transfer $X futures→spot`)

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

- **Nuevo parámetro hot-config sin campo en `config.py`**: `run_trades._apply_regime_params()` usa `setattr(settings, ...)`. Si `Settings` (pydantic) tiene `extra='forbid'`, el `setattr` lanza `"Settings" object has no field ...`, el campo queda sin aplicar y el fallback hardcodeado en `_check_entry_quality` sigue activo. Solución: agregar el campo con `Field(default=...)` en `config.py` y asegurar que `_PARAM_DEFAULTS`/`_scalar_map` lo reflejen.

- **Persistencia antes de confirmación de fill**: INSERT en `trades_open` antes de `_verify_entry_filled()` → posición zombie si Binance no ejecutó la orden (`positionAmt == 0`).

- **PnL bruto en decisión de exit**: pasar `unrealized_pnl` sin procesar por `binance_commission.net_pnl()` → breakeven o trailing se activa en nivel incorrecto. Todo valor de PnL que alimente SSO/PRS/UG/PredExit/BreakEven/Trailing debe ser neto.

- **Perfil auto-selector sesgado por rank base**: si `profile_rank` no es igualitario entre A/B/C, el selector siempre elige el de mayor rank base ignorando el rendimiento real. El ranking debe emerger exclusivamente de metadata de rendimiento.

- **`_check_config_drift` y `/config/verify` con `_exclude` desincronizados**: si se añade un campo gobernable por hot-config a `_KELLY_EDITABLE_FIELDS` pero no a `_EXCLUDED` en ambos bloques de drift-check, el campo generará false positives (`critical drift`) cuando esté intencionalmente desviado del default. Regla: cualquier campo en `_KELLY_EDITABLE_FIELDS` que pueda diferir del default en producción debe estar en `_EXCLUDED` de `_check_config_drift` Y en `_exclude` de `/config/verify`.

- **Pesos de `indicator_weights` del régimen anterior servidos en el nuevo régimen**: antes de S75, la clave de `indicator_weights` era solo `(symbol)` — los pesos no se invalidaban al cambiar de régimen. Ahora la clave es `(symbol, regime)`. Si `_persist_weights` se llama sin pasar `regime`, guarda en `__any__` (backward-compatible). Aplicar migration `018_indicator_weights_regime.sql` antes de deploy en producción o el `ON CONFLICT(symbol, regime)` fallará.

- **`min_strength` hardcodeado en callers de `get_top_buy_signals`**: cualquier caller que pase `min_strength=0.6` explícitamente anula la resolución desde `settings` y el ajuste por perfil A/C. El caller correcto no pasa `min_strength` — deja que la función lo resuelva internamente.

- **Escalado de boosts con `_lw_scale` aplicado dos veces**: si otro módulo también carga pesos del learning_engine y los aplica sobre una señal que ya pasó por `analyze_symbol`, los boosts se duplican. `_lw_scale` solo se aplica dentro de `analyze_symbol` — no debe re-aplicarse externamente.

- **🔴 Comisión de fill no convertida a USDT (posición PEPE jul-2026)**: `verify_and_fetch_fills()` en `binance_client.py` debe convertir `commission` a USDT antes de retornar. Si `commissionAsset` es el base asset del par (ej. PEPE) y no es USDT/BNB, multiplicar `commission × fill_price`. De lo contrario la `entry_commission` persiste en unidades del asset (268K PEPE ≠ $0.71) → el `unrealized_pnl` calculado sobre la qty de DB resulta ficticio.

- **🔴 Ghost positions: cantidad a vender siempre desde balance real de Binance**: todo cierre SPOT (SL/TP, SSO, PRS, UG, PredExit, manual) debe calcular la qty con `trade_utils.get_binance_sell_qty(client, symbol, db_qty, price)` antes de enviar la orden. Nunca vender `db_qty × close_pct` sin verificar el balance real. Si `balance/db_qty < 0.001`, cerrar el registro DB como `ghost_balance_mismatch`.

- **🔴 Ghost positions: partial close persiste qty real vendida, no qty calculada**: después de `create_market_order()`, usar `verify_and_fetch_fills()` (SPOT) o `positionRisk` (FUTURES) para obtener la cantidad real ejecutada. `position_manager.close_position()` recibe `qty_closed=filled_qty`, no `qty_calculated`. Si no se confirma el fill, NO se actualiza `trades_open.quantity` ni se inserta en `trades_closed`.

- **🔴 FUTURES: no marcar cerrado si Binance aún tiene positionAmt > 0**: `futures_client.close_long/close_short` deben verificar `positionRisk.positionAmt == 0` antes de llamar `_save_futures_closed()`. Si aún hay posición residual, actualizar `pos.qty` al residual y seguir gestionando; nunca persistir cierre fantasma.

- **🔴 Reconcile preventivo cada ciclo**: `execute_once()` ejecuta `sync_with_exchange_balance()` + `evaluate_spot_parity()` antes de evaluar salidas. Cualquier `SPOT_GHOST_IN_DB` se cierra automáticamente; cualquier `SPOT_ORPHAN_IN_BINANCE > $5` se importa. Dust `< $5` se ignora en paridad y se limpia vía endpoint `/reconcile/cleanup-dust`.

- **⚠️ s78 RANGE_SCALP entry gates — NO MODIFICAR sin análisis**: tres fixes calibrados en jul 14 2026 que desbloquearon entradas en RANGE_SCALP. Validados con 5/5 cierres positivos (+$12.37, +$7.67, +$8.00, +$4.83, +$4.02). **No revertir individualmente** — los tres actúan como sistema:
  1. **BB gate skip (`run_trades.py` ~4699)**: `if bb_position is None and not _bb_touch_ok: pass` — evita rechazar señales válidas sin dato BB. Solo RANGE_SCALP.
  2. **Vol threshold 0.20 (`entry_quality_vol_spike_min_range_scalp`)**: DB override vía `PATCH /config/kelly`. Rollback: `PATCH /config/kelly {"entry_quality_vol_spike_min_range_scalp": 0.35}`.
  3. **Kelly regime mult 0.95 (`market_regime.py:253`)**: `kelly_mult=0.95` (era 0.70). Da ~$1,199/trade MEDIA. Rollback: editar línea + restart.
  Si el sistema vuelve a no abrir posiciones en RANGE_SCALP: verificar que estos 3 valores no hayan sido revertidos antes de diagnosticar cualquier otra causa.

- **🔴 Posición fantasma (ghost) por divergencia balance/qty > 1000×**: si `binance_balance / db_qty < 0.001`, la posición es candidata a ghost — el SL/TP se bloqueará para siempre porque el clamp deja `qty=0`. Detectar en dos puntos: (1) `position_manager.py` durante `sync_with_exchange_balance` → loguear CRITICAL; (2) `run_trades.py` en SL/TP clamp → auto-close del registro DB con `close_reason='ghost_balance_mismatch'`. Sin esta detección, el ciclo imprime `cannot sell` silenciosamente durante horas y el UG queda bloqueado con `watchdog_level` creciente.

- **🟢 Evidence gate calibrado y habilitado (s79, jul 18 2026)** — **SUPERSEDIDO por entry_profile_gate (s80)**: `_evidence_gate_posterior` queda desactivado por defecto (`evidence_gate_enabled=False`). El nuevo `entry_profile_gate` (`entry_profile_gate.py`) asigna perfiles A/B/C usando `compute_quality_score` + umbrales calibrados sobre `signal_peak_outcomes.jsonl`, garantizando retorno neto positivo mínimo para perfil C y descartando todo lo demás. Rollback del nuevo gate: `PATCH /config/kelly {"entry_profile_gate_enabled": false, "entry_quality_gate_mode": "blocking"}`.

---

## 7. SKILLS — ROLES ACTIVOS POR ÁREA

Conocimiento experto que se activa automáticamente según el contexto de cada solicitud.

**Autonomía analítica total:** cada rol puede cuestionar libremente cualquier supuesto, parámetro, arquitectura, gate, modelo, prioridad o decisión histórica del sistema. La sección **En este sistema** describe evidencia y comportamiento vigente; nunca limita hipótesis, alternativas ni recomendaciones. Cada análisis debe separar hechos observados, incertidumbres, hipótesis, impacto SPOT/FUTURES y perfiles A/B/C, método de validación y rollback.

Las únicas invariantes son de ejecución, no de razonamiento: no bypass de preflight/fill Binance, PnL neto, paridad Binance↔DB, custodia de secretos, auditoría inmutable y validación fuera de muestra antes de modificar capital real.

---

**Experto en criptomonedas**
- *Principios:* Los mercados cripto siguen ciclos macro BTC (dominance, halvings, liquidez global). Las narrativas sectoriales (AI, RWA, DePIN, Gaming) tienen ventanas de 2-8 semanas. MEME requiere catalizador + volumen sostenido. Tokenomics (supply, vesting, circulación real) determina la liquidez efectiva de un par.
- *En este sistema:* BTC dominance y F&G son inputs al régimen y al sizing. Narrativas mapean a categorías de `universe_selector.py`. Al proponer nuevos activos, evaluar tokenomics + vol24h antes de incluirlos en el universo.

**Behavioral Finance**
- *Principios:* Los inversores son sistemáticamente irracionales en extremos de miedo/codicia. La capitulación genera oportunidades de compra con EV positivo; el greed extremo genera sobrecompra con reversión probable. El sesgo de recencia hace que los traders eleven SLs tras rachas ganadoras y los bajen tras perdedoras.
- *En este sistema:* F&G modifica Kelly en `_kelly_position_size()`: capitulación amplía base y greed extremo la reduce. Al analizar sizing anómalo, contrastar el multiplicador vigente con evidencia de resultados y con alternativas de sizing.

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
- *En este sistema:* `learning_engine.py` usa regresión logística con pesos por indicador y `bias_correction` compensa distribución sintéticos vs reales. Evaluar cualquier umbral de probabilidad contra EV neto, calibración, coste de falsos positivos y muestra fuera de entrenamiento. Features más predictivos actuales: `vol_spike` y `trend_4h`; su relevancia debe verificarse periódicamente.

**Diseñador / UX**
- *Principios:* La jerarquía visual determina qué información procesa primero el operador bajo presión. Las alertas críticas deben ser inmediatamente visibles sin acción del usuario. El color debe codificar estado (rojo=crítico, amarillo=warning, verde=ok) de forma consistente en toda la interfaz.
- *En este sistema:* El dashboard sirve para decisiones operativas en tiempo real. `operator_focus` prioriza: critical > warning > info. Jerarquía de información: estado del sistema → posiciones abiertas → métricas históricas. Badges de contexto (`LEGACY`/`ACTUAL`) son para trazabilidad — no deben dominar la vista principal.

**Experto en Física Cuántica**
- *Principios:* El modelo de Ising y la formulación QUBO (`min xᵀQx`) son la base matemática de los primeros problemas resueltos por computación cuántica (D-Wave). La superposición cuántica permite explorar simultáneamente exponencialmente más estados que cualquier algoritmo clásico. El colapso de función de onda por medición es análogo a la reducción de incertidumbre cuando se obtiene nueva información.
- *En este sistema:* El QUBO en `portfolio_selector.py` es exactamente esta formulación — la solución actual es una aproximación clásica greedy del óptimo cuántico real. Si el universo escala a N=100+ activos con restricciones complejas, esta es la base para migrar a un solver cuántico annealing. El anti-whipsaw de 2 ciclos consecutivos es el "operador de medición" que colapsa la superposición de regímenes a un estado definido.

---

## 6. MIMIC — CONCEPTOS, ARQUITECTURA Y SIMULADOR E2E

### Concepto

**MIMIC** (*Market-Informed Meta-Intelligence Calibration*) es el banco de pruebas histórico del sistema: ejecuta la lógica real de trading (`run_trades.execute_once()`) contra datos históricos locales (parquet), usando dinero virtual, los mismos detectores de régimen y los mismos módulos de salida que producción. En una frase, es **producción ejecutada contra el pasado**.

### Objetivo

- Validar configuraciones y fixes antes de que afecten capital real.
- Calibrar perfiles A/B/C por régimen y mantener actualizados sus meta-fields (`sharpe`, `win_rate_pct`, `max_drawdown_pct`, `expected_return_pct`).
- Descubrir qué parámetros dominan en cada tipo de mercado mediante barridos sistemáticos.
- Generar líneas base (`baseline`) y detectar regresiones (`pytest tests/test_mimic_regression.py`).

### Flujo de trabajo (fases del scheduler)

El trabajo de simulación se organiza en micro-universos (ventanas de fechas + símbolos + régimen objetivo) y progresa por fases:

1. **SELECT_UNIVERSE** — elige el siguiente micro-universo de la cola priorizada.
2. **AUDIT** — verifica disponibilidad de parquet para cada símbolo.
3. **DOWNLOAD** — ingesta datos externos (funding, F&G, dominancia BTC) si faltan.
4. **VALIDATE** — sanity-check de calidad de datos.
5. **MAP_REGIMES** — etiqueta cada punto histórico con el régimen detectado por BTC.
6. **CALIBRATE** — ejecuta simulaciones forzando perfiles A/B/C y variando parámetros.
7. **CONSOLIDATE** — agrega métricas (PnL, Sharpe, drawdown, win rate).
8. **CROSS_VALIDATE** — valida en ventanas no vistas.
9. **APPLY** — propone/escribe perfiles óptimos en `regime_profiles`.
10. **MONITOR** — observa comportamiento en producción real.

### Componentes principales

| Componente | Archivo | Responsabilidad |
|---|---|---|
| **Orchestrator CLI** | `simulation/mimic_orchestrator.py` | Punto de entrada por consola: `start`, `run`, `run-phase`, `pause`, `reset`, `status`. |
| **Scheduler** | `simulation/mimic_scheduler.py` | Máquina de estados con fases y persistencia en `mimic_state.json` + `trading_runtime_state`. |
| **Window controller** | `simulation/mimic_window_controller.py` | Decide cuándo MIMIC puede ejecutarse según horario y condiciones de mercado. |
| **Micro-universe selector** | `simulation/mimic_universe_selector.py` | Cola priorizada de micro-universos; prioriza cobertura, régimen faltante y oportunidad. |
| **Simulation runner** | `simulation/mimic_runner.py` | Ejecuta ciclo a ciclo la lógica real contra datos históricos, avanzando el reloj simulado. |
| **Historical data feed** | `simulation/mimic_feed.py` | Carga parquet de `historical_data/` y sirve velas 1h/4h/1d; re-muestrea a 5m/15m. |
| **Regime detector histórico** | `simulation/mimic_regime_detector.py` | Replica `market_regime.py` usando velas históricas de BTC. |
| **Simulation clients** | `simulation/mimic_clients.py` | Mocks de SPOT/FUTURES con balance virtual, comisiones, slippage y órdenes en memoria. |
| **Simulation DB** | `simulation/mimic_db.py` | DB en memoria que inyecta `regime_profile_*`, `regime_profile_active_*` e `indicator_weights`. |
| **Paper trader** | `simulation/mimic_paper_trader.py` | Ejecuta el ciclo contra precios live sin enviar órdenes reales; acumula PnL ficticio. |
| **Mode × Profile sweep** | `Dev/scripts/mimic_mode_profile_sweep.py` | Barre cuadrícula de parámetros sobre ventanas históricas, forzando perfil A/B/C. |
| **Window builder** | `Dev/scripts/mimic_build_window.py` | Construye manifests de ventanas continuas con metadata de régimen predominante. |

### Relación con perfiles A/B/C

MIMIC es el motor que alimenta al selector automático de perfiles:

- Cada modo tiene 3 perfiles: `A` (agresivo), `B` (balanceado), `C` (conservativo/defensivo).
- El sweep fuerza un perfil y mide su rendimiento en una ventana histórica pura de un régimen.
- Los resultados actualizan los meta-fields de `regime_profile_{REGIME}_{A/B/C}`.
- El selector de producción suma `profile_rank` + métricas reales + confianza del régimen y elige el perfil activo.
- `profile_rank` debe mantenerse asimétrico pero razonable (ej. `A=250, B=200, C=150`) para que A sea atractor sin bloquear rotación a B/C cuando el mercado lo requiera.

### Source of truth — simulador determinista

**Propósito:** Simulador determinista que ejecuta la lógica de producción `run_trades.execute_once()` contra datos históricos reales, sin tocar Binance ni PostgreSQL. Sirve para validar cambios, calibrar parámetros y detectar regresiones antes de deploy.

**Componentes técnicos:**
- `simulation/mimic_protocols.py` — `SimulationClock` (tiempo controlado).
- `simulation/mimic_clients.py` — mocks de SPOT/FUTURES sobre feed histórico.
- `simulation/mimic_feed.py` — carga parquet de `historical_data/`.
- `simulation/mimic_news_client.py` — F&G y noticias deterministas.
- `simulation/mimic_db.py` — DB en memoria; soporta transacciones múltiples como el DB client real.
- `simulation/mimic_runner.py` — orquesta ciclos con inyección de dependencias.
- `Dev/scripts/mimic_smoke_test.py` — validación de ciclo completo.
- `Dev/scripts/mimic_parameter_sweep.py` — sweep de parámetros con reporte JSON.
- `tests/test_mimic_regression.py` — test de regresión pytest contra baseline.

**🔴 Reglas duras del simulador:**
1. Nunca duplicar lógica de trading en el simulador — reusar `run_trades.py` con DI.
2. Nunca hardcodear comportamiento del mercado en mocks — derivar de datos históricos.
3. Siempre invalidar cachés globales (señales autónomas, etc.) al inicio de cada ciclo simulado para evitar contaminación entre pasos.
4. Si se agrega un método usado por producción en `BinanceClient`/`FuturesClient`/`NewsClient`, el mock correspondiente debe implementarlo.

**Guías de uso:**
- En simulación deshabilitar `screener_enabled` y usar `analysis_symbols` estáticos; el screener real consume API pública de Binance.
- El predictor PnL en simulación reproduce el comportamiento de producción: con umbrales altos rechaza la mayoría de señales.
- Antes de cambiar parámetros de producción: correr sweep y verificar que no haya regresión (`pytest tests/test_mimic_regression.py`).
- Para actualizar baseline: `pytest tests/test_mimic_regression.py --mimic-update-baseline`.

**Comandos:**
```powershell
# Smoke test
$env:BINANCE_API_KEY="dummy"; $env:BINANCE_API_SECRET="dummy"; $env:TRADINGVIEW_WEBHOOK_SECRET="dummy"; $env:PYTHONIOENCODING="utf-8"; python Dev/scripts/mimic_smoke_test.py

# Calibración de 4 configs sobre 20 ciclos
$env:BINANCE_API_KEY="dummy"; $env:BINANCE_API_SECRET="dummy"; $env:TRADINGVIEW_WEBHOOK_SECRET="dummy"; $env:PYTHONIOENCODING="utf-8"; python Dev/scripts/mimic_calibration_run.py

# Sweep (puede ser lento; reducir grid/ventana si es necesario)
$env:BINANCE_API_KEY="dummy"; $env:BINANCE_API_SECRET="dummy"; $env:TRADINGVIEW_WEBHOOK_SECRET="dummy"; $env:PYTHONIOENCODING="utf-8"; python Dev/scripts/mimic_parameter_sweep.py

# Regresión
$env:BINANCE_API_KEY="dummy"; $env:BINANCE_API_SECRET="dummy"; $env:TRADINGVIEW_WEBHOOK_SECRET="dummy"; $env:PYTHONIOENCODING="utf-8"; pytest tests/test_mimic_regression.py -v
```

### Lifecycle Kernel MIMIC

**Propósito:** Validar el kernel declarativo de ciclo de vida (`lifecycle/`) dentro del simulador MIMIC, independiente de la lógica legacy de `run_trades.py`.

**Componentes:**
- `lifecycle/base.py` — contratos (`Signal`, `EntryDecision`, `ExitDecision`, `Position`, policies)
- `lifecycle/signal.py` — `SignalPipeline`
- `lifecycle/entry.py` — `EntryGate`
- `lifecycle/sizing.py` — `PositionSizer`
- `lifecycle/exit.py` — `ExitEngine` (hard SL/TP, trailing, breakeven, partial, momentum, stagnation)
- `lifecycle/runner.py` — `LifecycleRunner`
- `simulation/mimic_lifecycle_adapter.py` — `ExecutionAdapter` que conecta el kernel a `SimulationRunner`

**Reglas duras:**
1. El adapter nunca debe duplicar lógica de negocio — solo traduce entre el kernel y los mocks de simulación.
2. El cost basis debe reconstruirse de los fills del sim client (`_avg_entry_price`), no del precio actual.
3. Los perfiles A/B/C del lifecycle deben provenir de resultados de sweep, no de valores arbitrarios.
4. `execute_exit` debe reportar `realized_pnl_usdt` para poder medir win/loss y profit factor.
5. El runner debe reportar `exit_reasons` para poder comparar qué mecanismos de salida están activos.

**Anti-patrón detectado (jul 2026):**
- Si dos perfiles distintos (p. ej. B y C) producen métricas idénticas en validación, el cuello de botella no es el tuning de parámetros sino la **fidelidad/cantidad de señales generadas**. No seguir tuneando parámetros; mejorar el signal pipeline primero.

**Comandos:**
```powershell
# Validación multi-window, multi-perfil
$env:BINANCE_API_KEY="dummy"; $env:BINANCE_API_SECRET="dummy"; $env:TRADINGVIEW_WEBHOOK_SECRET="dummy"; python tmp_lifecycle_multi_window.py --profile all

# Tests del lifecycle
python -m pytest tests/test_lifecycle_adapters.py tests/test_lifecycle_exit.py tests/test_lifecycle_runner.py -q
```

**Artifacts:**
- `Dev/benchmarks/lifecycle_multi_window_profile_{A,B,C}_20260713.json`
- `Dev/lifecycle_kernel_validation_20260713.md`

---

## 7. MIMIC AUTÓNOMO — EJECUCIÓN CONTINUA POR MICRO UNIVERSOS

**Propósito:** Ejecutar MIMIC de forma autónoma, acumulando micro universos en el dataset global y mejorando la cobertura de regímenes/símbolos sin intervención manual.

**Componentes:**
- `simulation/mimic_scheduler.py` — máquina de estados con fases: `SELECT_UNIVERSE → AUDIT → DOWNLOAD → VALIDATE → MAP_REGIMES → CALIBRATE → CONSOLIDATE → CROSS_VALIDATE → APPLY → MONITOR`.
- `simulation/mimic_universe_selector.py` — cola priorizada de micro universos (symbol window × regime).
- `simulation/mimic_window_controller.py` — ventanas de ejecución programables y condiciones de mercado.
- `simulation/mimic_orchestrator.py` — CLI para operación manual/programada.
- `Dev/scripts/mimic_data_audit.py` — audita datos históricos y genera cola.
- `Dev/scripts/download_historical_data.py` — descarga klines para micro universos.

**Persistencia:**
- `Dev/benchmarks/mimic_state.json` — estado del scheduler.
- `Dev/benchmarks/mimic_universe_queue.json` — cola de micro universos.
- `Dev/benchmarks/mimic_dataset_progress.json` — log de resultados acumulados.
- `Dev/benchmarks/mimic_schedule.json` — configuración de ventanas.
- `trading_runtime_state` (DB) — estado restart-safe.

**API endpoints** (`app.py`): `/mimic/status`, `/mimic/universes`, `/mimic/select-universe`, `/mimic/start`, `/mimic/run`, `/mimic/pause`, `/mimic/resume`, `/mimic/retry`, `/mimic/reset`, `/mimic/schedule`, `/mimic/report`.

**Dashboard:** Tab "MIMIC" en `frontend/index.html` + `frontend/dashboard.js` con estado, cola, programador de ventanas, cobertura acumulada y acciones.

**Reglas duras:**
1. Cada micro universo es idempotente: reiniciar reintenta desde la fase actual sin duplicar resultados.
2. Cross-validation (`pytest tests/test_mimic_regression.py`) se dispara solo cuando hay datos suficientes acumulados.
3. El operador puede forzar/pausar/reiniciar desde dashboard u orchestrator; la ejecución automática respeta la ventana programada.
4. Descargas deben cubrir la ventana del micro universo, no solo los últimos días.
5. El auto-run worker del backend revisa el horario cada 5 minutos; si se cambia el horario en el dashboard, el worker lo recarga desde disco en la siguiente verificación.

**Comandos:**
```powershell
# Generar cola y ejecutar un ciclo completo
$env:PYTHONIOENCODING="utf-8"; python -m simulation.mimic_orchestrator reset
$env:PYTHONIOENCODING="utf-8"; python -m simulation.mimic_orchestrator run-phase --phase audit --window-days 1
$env:PYTHONIOENCODING="utf-8"; python -m simulation.mimic_orchestrator run --force
```

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

---

## 9. MIMIC — VENTANAS DE SIMULACIÓN DISPONIBLES

Directorio: `/app/Dev/benchmarks/windows/`
Parquets 4h y 1d de BTC **deben existir** — generados desde `BTCUSDT_1h.parquet` con timestamps en ms (bug fix jul 2026, ver snippet abajo).

### Inventario (12 ventanas — jul 2026)

| ID | Período | Dominant | RS% | SB% | Símbolos | Uso recomendado |
|----|---------|----------|-----|-----|----------|-----------------|
| `win_20250116_20250131` | 16–31 ene 2025 | SPOT_BULL | 46% | 54% | 27 | Mixta — transiciones RS↔SB |
| `win_20250525_20250608` | 25 may – 8 jun 2025 | **RANGE_SCALP** | **95%** | 5% | 28 | **Sweep RANGE_SCALP puro** |
| `win_20250718_20250807` | 18 jul – 7 ago 2025 | **RANGE_SCALP** | **90%** | 10% | 29 | **Sweep RANGE_SCALP puro** |
| `win_20260303_20260317` | 3–17 mar 2026 | SPOT_BULL | 0% | 100% | 44 | Sweep SPOT_BULL |
| `win_20260317_20260331` | 17–31 mar 2026 | SPOT_BULL | 0% | 100% | 45 | Sweep SPOT_BULL |
| `win_20260401_20260415` | 1–15 abr 2026 | SPOT_BULL | 0% | 100% | 45 | Sweep SPOT_BULL |
| `win_20260415_20260430` | 15–30 abr 2026 | SPOT_BULL | 0% | 100% | 47 | Sweep SPOT_BULL |
| `win_20260501_20260515` | 1–15 may 2026 | SPOT_BULL | 0% | 100% | 44 | Sweep SPOT_BULL |
| `win_20260515_20260531` | 15–31 may 2026 | SPOT_BULL | 0% | 100% | 44 | Sweep SPOT_BULL |
| `win_20260601_20260620` | 1–20 jun 2026 | SPOT_BULL | 8% | 92% | 44 | Sweep SPOT_BULL reciente |
| `win_20260620_20260705` | 20 jun – 5 jul 2026 | SPOT_BULL | 16% | 84% | 43 | Más reciente — RS emergente |
| `win_20260628_20260630` | 28–30 jun 2026 | SPOT_BULL | 0% | 100% | 4 | Micro-ventana / pruebas |

> **Pendientes:** ventanas PRESERVATION y FUTURES_SHORT (datos nov-2025 / abr-2025 disponibles para construirlas).

### Bug fix — parquets BTC 4h/1d (jul 2026)

`mimic_feed.py` espera `open_time` en **ms** enteros. Reconstruir si se pierden:

```python
import pandas as pd, os
df = pd.read_parquet('historical_data/BTCUSDT_1h.parquet')
df = df.sort_values('open_time').drop_duplicates('open_time').set_index('open_time')
for iv, rule in [('4h', '4h'), ('1d', 'D')]:
    rs = df.resample(rule).agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna().reset_index()
    rs['open_time'] = rs['open_time'].apply(lambda x: int(x.timestamp() * 1000))
    rs[['open_time','open','high','low','close','volume']].to_parquet(
        f'historical_data/BTCUSDT_{iv}.parquet', index=False)
```

### Construir nuevas ventanas

```bash
# Paso 1: verificar qué símbolos tienen cobertura para el período deseado
docker exec crypto-agent-app python -c "
import os, pandas as pd
data_dir = '/app/historical_data'
target = pd.Timestamp('YYYY-MM-DD', tz='UTC')
ok = sorted(f.replace('_1h.parquet','') for f in os.listdir(data_dir)
            if f.endswith('_1h.parquet') and
            pd.read_parquet(os.path.join(data_dir,f))['open_time'].min() <= target)
print(','.join(ok))
"

# Paso 2: construir la ventana con esos símbolos
docker exec crypto-agent-app python /app/Dev/scripts/mimic_build_window.py \
    --start YYYY-MM-DD --end YYYY-MM-DD \
    --symbols SYM1,SYM2,...
```

### Workers óptimos para sweep (i9-12900H / 20 lógicos / 15 GB RAM)

`mimic_mode_profile_sweep.py` usa `ThreadPoolExecutor` — workers son threads, GIL se libera en pandas/numpy.

| `max_workers` | ETA sweep full 5 184 jobs (max_cycles=10) | Notas |
|---------------|------------------------------------------|-------|
| 2 | ~2.4 h | default conservador |
| 4 | ~1.2 h | — |
| **6** | **~50 min** | **recomendado** — P-cores sin saturar |
| 8 | ~37 min | sistema host puede volverse lento |

Llamada recomendada para sweep full baseline RANGE_SCALP:

```json
POST /mimic/sweep
{
  "windows": ["win_20250525_20250608", "win_20250718_20250807"],
  "strategy": "full",
  "max_cycles": 10,
  "max_workers": 6,
  "clear_results": true
}
```

---

## 10. SELECTOR AUTOMÁTICO DE PERFIL — MECÁNICA Y CONFIGURACIÓN

### Función y flujo

El selector (`_run_regime_profile_auto_selector` en `run_trades.py`) corre **cada ciclo** cuando el régimen está `confirmed=True`. Lee todos los perfiles del régimen activo desde DB, los puntúa, aplica hysteresis y activa el mejor si supera el umbral.

**Flujo por ciclo:**
```
1. Leer perfiles regime_profile_{REGIME}_{A/B/C} desde trading_runtime_state
2. Puntuar cada uno con _score_profile_candidate()
3. Comparar mejor candidato vs perfil activo
4. Si delta > min_score_delta Y ciclos >= min_hold_cycles → switch
5. Escribir decisión en trades_audit (event: regime_profile_selector_decision)
6. Si switch → _apply_regime_params() aplica los parámetros del nuevo perfil
```

### Fórmula de scoring

```python
score = rank_base                          # desde profile_rank en selector-config
      + meta.score * 1.0                   # override directo (raramente usado)
      + meta.expected_return_pct * 1.50    # retorno esperado
      + meta.expected_pnl_usd * 0.01       # PnL absoluto (peso bajo)
      + meta.win_rate_pct * 0.40           # tasa de éxito
      + meta.sharpe * 25.0                 # calidad risk-adjusted
      - abs(meta.max_drawdown_pct) * 0.80  # penalización por drawdown
      + regime.confidence * 5.0            # boost por confianza del régimen
```

**Si los meta-fields están vacíos (0), el score es solo `rank_base + confidence_boost`.**  
→ Mantener meta-fields actualizados con datos del último sweep.

### Configuración activa (jul 11 2026)

```
GET/POST /regime/profiles/selector-config
profile_rank:             {A: 250, B: 200, C: 150}   ← A es el atractor
profile_min_hold_cycles:  4                            ← ~3h mínimo antes de cambiar
profile_min_score_delta:  0.05                         ← filtro anti-ruido
profile_selector_enabled: true
audit_every_cycle:        true
```

### Diseño: A como atractor permanente (estado de mínima energía)

El ranking **asimétrico** 250/200/150 garantiza que A siempre tenga ventaja estructural:

| Perfil | Score base (sin meta) | Con meta sweep jul11 |
|--------|----------------------|----------------------|
| A | 254.0 | **330.5** |
| B | 204.0 | 259.1 |
| C | 154.0 | 196.6 |

**Delta A→B = 71.4 pts** → B solo gana si win_rate real de A cae >15% ó sharpe cae >1.1  
**Retorno a A:** automático tras 4 ciclos de hold mínimo en cuanto el delta desaparece

**Por qué NO usar ranking igualitario (200/200/200):** sin gravedad, B/C pueden quedarse indefinidamente aunque el mercado mejore. El sistema debe siempre tender a A.

**Por qué NO usar A=1000 (estado anterior corrupto):** B/C nunca activarían aunque el mercado los requiera → perfiles inútiles.

### Meta-fields — cómo actualizar tras sweep

Después de cada sweep con suficientes ciclos (`max_cycles≥50`):

```json
POST /regime/profiles
{
  "regime": "RANGE_SCALP",
  "profile": "A",
  "params": { ... (mantener params actuales) ... },
  "meta": {
    "expected_return_pct": <pnl_pct promedio top-25%>,
    "win_rate_pct": <buy_ratio * 100>,
    "sharpe": <pnl_avg / pnl_std>,
    "max_drawdown_pct": <max DD promedio>,
    "source": "sweep_<fecha>"
  }
}
```

### Diagnóstico del selector

```
GET /regime/profiles/selector-decisions   ← últimas N decisiones con scores y razones
GET /regime/profiles/selector-config      ← config activa + estado por régimen
GET /regime/profiles?regime=RANGE_SCALP   ← perfiles + meta-fields actuales
```

**"Selector lleva muchos ciclos sin cambiar"** → verificar:
1. `profile_rank` en selector-config — si A tiene ventaja muy alta (>500), B/C nunca ganarán
2. `meta-fields` de perfiles — si están vacíos, el score es solo rank_base
3. `cycles_since_switch` en selector state — si es miles, el selector está bloqueado

**"Selector cambió a B/C pero el mercado mejoró"** → verificar:
1. `profile_min_hold_cycles` — después de esos ciclos el retorno a A es automático si score A > score B+delta
2. Forzar retorno manual: `POST /regime/profiles/activate {"regime":"RANGE_SCALP","profile":"A","apply_now":true}`

### Semántica A/B/C para RANGE_SCALP (resumen)

| Perfil | Escenario | Activación automática |
|--------|-----------|----------------------|
| A | Lateral puro (99%+ RS, rebotes claros) | Estado base — atractor |
| B | Lateral mixto (~87% RS, ruido de régimen) | Solo si win_rate A cae >15% |
| C | Lateral degradado/sucio (~80% RS, bajo vol) | Solo si win_rate B cae adicionalmente |

### s76: Kelly uniforme para RANGE_SCALP (jul 12 2026)

**Principio**: Los perfiles diferencian riesgo via entry gates y exit timers, NO via Kelly sizing.
- A: prob≥0.44, strength≥0.37 (más permisivo)
- B: prob≥0.46, strength≥0.43 (neutral)
- C: prob≥0.48, strength≥0.49 (más estricto)

Si un perfil aprobó un trade, ese trade ya pasó sus filtros — merece el mismo capital.
Kelly uniforme: $2500 max, KD=0.26, max_pct_liquid=0.90, conviction_max=0.30, MP=5.

**Post-sweep analysis**: Usar `/post_sweep_analysis` workflow después de cada sweep completo.

---

## 6. Peak MIMIC y `peak_quality` (jul 16 2026)

### Componentes

- `peak_quality.py`: carga de modelos `RandomForest` por régimen/dirección, imputación de medianas, asignación A/B/C/discarded.
- `peak_signal_source.py`: lectura por lotes de `peaks_all.parquet`, predicción vectorizada con `n_jobs=12`, schedule de señales por `window_id`.
- `peak_mimic_validator.py`: simulación directa (abre en `peak_start_ts`, cierra en `peak_end_ts`).
- `simulation/mimic_lifecycle_adapter.py`: inyección de `peak_entry_schedule` en `run_lifecycle_simulation`.
- `lifecycle/signal.py` + `lifecycle/profile_classifier.py`: passthrough de `peak_quality_profile` a `Signal.features` y uso como override del clasificador A/B/C.

### Reglas de uso

- **Perfiles A/B/C vienen del predictor, no del clasificador por thresholds técnicos**, cuando `Signal.features["peak_quality_profile"]` está presente.
- **Inyección en MIMIC**: `--peak-signal-mode` en `mimic_mode_profile_sweep` fuerza `lifecycle=True` y un solo worker (`max_workers=1`) para evitar problemas de estado.
- **Memoria**: `peak_signal_source` predice por batches de 100k-200k filas; no cargar todo `peaks_all.parquet` en memoria.
- **Caveat**: el validador directo cierra en `peak_end_ts`, por lo que sus métricas son un **techo teórico/in-sample**. El lifecycle sweep mide la captura real con `ExitEngine`.

### Tuning de `ExitPolicy` (resultado final jul 16 2026)

- Config recomendada en `Dev/benchmarks/peak_lifecycle_tuning/best_exit_policy.json`.
- Métrica objetivo: `pnl_per_day_usdt` medio sobre 12 ventanas, con deploy gate (`max_drawdown_pct < 15%`, `trades ≥ 3`, `profit_factor ≥ 1.0`).
- Mejor config (fingerprint `1befaf86143c8295`): **PnL/día $20.18**, retorno **10.08%**, max DD **6.32%**, PF **1.33**.
- Reglas nuevas implementadas: `use_expected_gain_for_tp` y `time_stop_hours` (perfil C lo usa; A y B principalmente confían en hard TP/trailing).
- En `tune_peak_exit_policy.py`: default `--max-workers 10`; modo `--refine-from` para perturbaciones locales desde `tuning_report.json`.

### Decisiones de rollout

1. No activar en producción hasta que el lifecycle sweep pase el deploy gate en ventanas representativas (`drawdown < 15%`, `PnL/día > 0`, `trades ≥ 3`).
2. Antes de live, computar features `s_*` en `analyze_symbol` o re-entrenar con el subconjunto disponible.
3. Si se usa, mantener `_check_entry_quality` como piso duro; `peak_quality` actúa como filtro adicional de rentabilidad esperada.

---

## 7. Dashboard UI — Perfil, Modo y Peak MIMIC ExitPolicy (jul 16 2026)

### Columnas en posiciones abiertas y cerradas
- **Perfil**: badge visual (`A`/`B`/`C`) derivado de `entry_profile_label_for_display` → usa `peak_quality_profile` si existe, sino `profile_at_entry`. A=verde, B=azul, C=rojo.
- **Modo**: badge/texto del modo al abrir (`SPOT_BULL`, `RANGE_SCALP`, `PRESERVATION`, `FUTURES_SHORT`) con icono y color.
- **Peak MIMIC**: badges `expected_gain` (🎯EG×N) y `time_stop` (⏱TSh). Muestran si el `ExitPolicy` del perfil los usa (`use_expected_gain_for_tp`).

### Filtros
- Cada tabla (abiertas/cerradas) tiene filtros desplegables por **Perfil** (`All`, `A`, `B`, `C`) y **Modo** (`All`, `SPOT_BULL`, `RANGE_SCALP`, `PRESERVATION`, `FUTURES_SHORT`).
- Estado de filtros vive en `_openProfileFilter`, `_openModeFilter`, `_closedProfileFilter`, `_closedModeFilter` y se re-renderiza desde `_lastOpenData` / `_lastClosedData`.

### Modal de detalle
- Muestra Perfil, Modo, Contexto en sección "📍 Entrada".
- Sección "🟠 Peak MIMIC":
  - Perfil de la señal `entry_peak_quality_profile` + score.
  - Perfil activo usado por el trade (`entry_profile_label_for_display`).
  - `expected_gain` multiplier y si es TP activo.
  - `time_stop_hours`.

### Backend asociado
- `app.py` enriquece posiciones con `exit_policy_params` y expone `exit_policies` top-level en `/dashboard/summary`.
- `position_manager.py`, `run_trades.py`, `futures_client.py` persisten `peak_quality_profile`/`peak_quality_score` en metadata.

---

## 8. DB client lazy init + rango de posiciones cerradas (jul 16 2026)

### Reglas
- **`DatabaseClient`** no debe romper en `__init__` si Postgres aún no responde. Debe crear el objeto, guardar el error e intentar `_initialize_pool` lazy en `get_connection`.
- **`init_components`** no debe dejar `c.db_client = None` por un fallo transitorio de conexión. Asigna siempre el `DatabaseClient`; `PositionManager`/`Orchestrator` lo reciben y se recuperan cuando la DB vuelva.
- **`/health/status`** debe manejar `db_client is None` explícitamente sin propagar `AttributeError`.
- **Dashboard**: `/dashboard/summary` acepta `days` (1–365, default 7) para cerradas. El frontend persiste la selección en `localStorage` y la envía en cada fetch.

### Checks operativos
- Si `db_client` es `None`, reiniciar el contenedor `app` después de confirmar que Postgres está healthy.
- Si las posiciones cerradas no cargan, verificar que `PositionManager` tenga `db_client` con pool (no `None`) y que `get_closed_positions_since` use `exit_time >= since`.

---

## 9. Reanálisis de calibración de entradas — s88 (jul 22 2026)

### Scope

Cierre de la revisión de entry calibration con énfasis en `range_reversion_predictor_mode` y alineación completa de defaults auditables.

### Decisiones clave

- **RANGE_SCALP shadow fallback**: `entry_decision.py` no rechaza `RANGE_MODEL_INVALID` cuando `range_reversion_predictor_mode="shadow"`. Se conserva el predictor long genérico y se persiste `range_prediction={"valid": False, "reason": "shadow_skipped"}`.
- **SPOT_BULL direccional**: `direction_min_progress_bps` pasa de `3.0` a `5.0`; `direction_required_confirmations` se mantiene en `2`.
- **Defaults canónicos**: todos los `getattr(settings, ..., default)` para parámetros críticos ahora coinciden con `_PARAM_DEFAULTS` en `config_helpers.py`.

### Archivos modificados

- `entry_decision.py`: lógica condicional de `RANGE_MODEL_INVALID` y defaults canónicos.
- `config.py` / `config_helpers.py`: umbrales de confirmación SPOT_BULL y defaults de staged/entry decision.
- `staged_entry.py`: defaults de `entry_profile_gate_thresholds_path` y `range_reversion_predictor_mode`.
- `run_trades.py`: defaults de `staged_entry_enabled`, `entry_decision_enabled`, `volspike_override_enabled`.
- `pnl_predictor.py`: defaults de `take_profit_pct` y `range_scalp_sl_pct`.
- `tests/test_entry_decision.py`, `tests/test_range_scalp_entry_pipeline.py`, `tests/test_entry_quality_gate.py`, `tests/test_open_positions_detail.py`, `Dev/benchmarks/mimic_baseline.json`.
- `tmp_audit_config.py`: parser tolerante a constantes de módulo y comas finales.

### Validación

- **576 passed, 0 failed** en suite completa.
- **0 critical mismatches** en audit de `getattr`.
- Runtime activo y healthy tras reinicio de `crypto-agent-app`.

### Rollback

- Para volver al comportamiento anterior de `RANGE_SCALP`: `PATCH /config/apply {"range_reversion_predictor_mode": "blocking"}`.
- Para restaurar SPOT_BULL 3 bps: editar `SPOT_BULL.direction_min_progress_bps` en `config.py` y `config_helpers.py`, rebuild/restart.
- Para reactivar `volspike_override_enabled`: `PATCH /config/apply {"volspike_override_enabled": true}` (no recomendado bajo entry-profile gate).
