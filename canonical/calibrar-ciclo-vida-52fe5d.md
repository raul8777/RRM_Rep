---
title: "Calibración del ciclo de vida de una operación"
type: canonical-memory
tags: ["lifecycle", "exit_pipeline", "SSO", "PRS", "UG", "peak_capture", "trailing", "breakeven", "spot", "futures", "profit_preservation", "loss_control", "drawdown_monitor", "erosion_detection"]
created_at: 2026-07-23
updated_at: 2026-08-28 (staging Demo Mode + sweep multi-régimen)
source_file: "C:\\Users\\raul8\\.windsurf\\plans\\calibrar-ciclo-vida-52fe5d.md"
---

# Calibración del ciclo de vida de una operación — control de tareas 52fe5d

Documento vivo que consolida el diseño actual del ciclo de vida de una posición (apertura → monitoreo → cierre) y controla las tareas para calibrar la preservación de ganancias y el control de pérdidas en SPOT y FUTURES.

## Actualización (2026-08-28) — Validación lifecycle en staging Demo Mode + fixes de sweep MIMIC

### Validación en staging real

Tras la migración de staging a Binance Demo Mode (`demo-api.binance.com` spot, `testnet.binancefuture.com` futures), el ciclo de vida completo se validó contra el exchange con órdenes reales en el entorno demo:

- **Apertura**: `FuturesClient initialized — synced 2 open position(s)`; posiciones ETHFIUSDT LONG y ETCUSDT LONG sincronizadas desde Binance.
- **Monitoreo**: trailing stops actualizados (`Futures/Trail`), `hard_risk_monitor` sin degradación y `position_telemetry_watcher` de 1s operativo.
- **Salida**: cierre LINKUSDT SHORT con `profit_realization_superv`, PnL positivo.
- **Ejecución spot**: `bought_0.168000_bnb`, `Balance sync: no orphan assets found`, `Commission refresh: spot=0.001 futures=0.0004`.
- **Capital bridge**: `futures_avail=$4684`, `spot=$2611`, `regime=RANGE_SCALP`, `open_shorts=0/4`.
- **Screener**: 93 pares escaneados.

### Fixes del sweep multi-régimen con impacto en lifecycle

El sweep de 8 ventanas (ago 28 2026) corrigió defectos en el simulador que distorsionaban el lifecycle:

1. **Conteo de aperturas**: `entries_executed` → `len(entries)` en `run_multiregime_sweep.py`; evita subcontar entradas validadas.
2. **PnL esperado para SHORTs**: `expected_pnl` para señales SELL se niega correctamente en `simulation/mimic_lifecycle_adapter.py` (SHORT gana cuando el precio baja).
3. **Alineación de régimen**: `_regime_alignment` en `lifecycle/signal.py` pasa de 0.0 a 0.4 para SHORT en SPOT_BULL, evitando que el simulador elimine por completo la señal residual permitida para pruebas.
4. **Toggles de parámetros**: `shorts_enabled` y `gates_enhanced` se propagan explícitamente en el diccionario `params` del sweep.

### Archivos modificados

- `s:\Tools\AI\crypto-agent-staging\simulation\mimic_lifecycle_adapter.py`
- `s:\Tools\AI\crypto-agent-staging\lifecycle\signal.py`
- `s:\Tools\AI\crypto-agent-staging\run_multiregime_sweep.py`

### Pendiente

- El lifecycle de salida s130b (`position_exit_decision_loop_enabled=True`) sigue validándose en staging; sin regresiones observadas en la ventana de Demo Mode.
- Requerir ventanas bajistas/laterales para validar cambios de régimen en el lifecycle.

---

## Actualización s130b — Exit Lifecycle Refactor: detectar rápido, actuar rápido (2026-08-17)

### Origen — directiva explícita del usuario

Tras la auditoría s130, el usuario identificó el patrón **"detectar rápido, actuar lento"**: `position_telemetry_watcher.py` detectaba señales de salida en 1s (`belief_last`, PIM, drawdown, profit-guard), pero todo el ejecutor de cierres (SSO, PRS, UG, PeakCapture, PIM, SmartTP, trailing, breakeven, belief) seguía en `run_trades.py::execute_once()` con un ciclo de 3-45 min. La instrucción fue mover toda la decisión y ejecución de salida al loop de 1s, con un **corte limpio** del ciclo de trading y validación en staging.

### Cambios aplicados

1. **Fase 1** — `_check_sl_tp_positions()` ahora es invocable desde `position_telemetry_watcher.py`. Se mantiene en `run_trades.py` como función de módulo; el watcher lo importa y ejecuta con un lock dedicado `_exit_decision_lock`. `execute_once()` deja de llamarlo cuando `position_exit_decision_loop_enabled=True` (tanto en modo normal como `monitor_only`/PAUSED). Si el flag se desactiva, la llamada se reactiva automáticamente.
2. **Fase 2** — nuevo `_run_exit_evidence_refresh_cycle()` en `position_telemetry_watcher.py`: refresco throttled (default 15s) del predictor `success_probability` y de `peak_capture_engine.compute_prob_higher_peak()` para **SPOT y FUTURES**. Los valores se persisten en metadata y se consumen por `_run_belief_cycle()` — cerrando el gap documentado en s130 donde `evaluate_belief()` solo recibía 2 de sus 4 señales. Se añade `peak_capture_engine.compute_prob_higher_peak()` como API pública ligera.
3. **Fase 3** — `_run_exit_decision_cycle()` ejecuta `_check_sl_tp_positions()` y `futures_client.check_positions()` cada 1s, con TTL de 60s para el régimen y `sso7_candidate_cache=None` (Opportunity Collapse deshabilitado). Este ciclo es el ejecutor único cuando el flag está activo; `hard_risk_monitor` sigue como backstop de SL/TP duro.
4. **Fase 4** — nuevas flags hot-config (`position_exit_decision_loop_enabled`, `position_exit_evidence_refresh_enabled`, `position_exit_evidence_refresh_seconds`) en `config.py` y `config_helpers.py`. Tests nuevos: `tests/test_exit_evidence_refresh.py`, `tests/test_exit_decision_cycle.py`, `tests/test_peak_capture_engine.py`.

### Configuración y rollback

```json
PATCH /config/kelly {"position_exit_decision_loop_enabled": true}
```
Activa el loop de 1s como ejecutor único. `execute_once()` ya no duplica salidas. En staging este flag ahora está por defecto `True` tras validación.

```json
PATCH /config/kelly {"position_exit_decision_loop_enabled": false}
```
Fallback: `execute_once()` vuelve a llamar `_check_sl_tp_positions()` automáticamente.

```json
PATCH /config/kelly {"position_exit_evidence_refresh_seconds": 15.0}
```
Ajusta throttle de evidencia cara.

### Validación

- Suite completa: 516 passed, 6 fallos preexistentes no relacionados (mismos que s130), 0 nuevos.
- Tests nuevos: `test_exit_evidence_refresh.py` (throttle, SPOT+FUTURES context, predict mock), `test_exit_decision_cycle.py` (enabled/disabled, lock, snapshots), `test_peak_capture_engine.py` (paridad `compute_prob_higher_peak` vs `evaluate().prob_higher_peak`).

### Pendiente

- Validar en staging real con `position_exit_decision_loop_enabled=True` y posiciones abiertas menores a 10. Se recomienda una ventana de auditoría corta comparando lo que el nuevo loop de 1s cerraría vs. lo que habría cerrado el ciclo lento, antes de dejar el flag como default `True` en producción.

---

## Plano en el que estamos

**Fase:** calibración del ciclo de vida de posiciones abiertas.
**Objetivo:** documentar el pipeline actual de salida, diagnosticar por qué las últimas 48h generaron pérdidas grandes / ganancias mínimas, y proponer ajustes canónicos a los módulos de exit (SSO, PRS, UG, PredExit, trailing, breakeven, hard SL/TP) sin romper la arquitectura existente.
**Alcance:** SPOT y FUTURES, con paridad de criterios salvo que la mecánica de mercado justifique una diferencia documentada.

## Actualización s130 — Belief-Driven Exit Lifecycle: cierre guiado por creencia, no por tiempo/target-diario/intercambio (2026-08-16)

### Origen — directiva explícita del usuario

Tras revisar todo el historial de este documento (s107→s129), el usuario pidió una redefinición del criterio de cierre: *"el cerrado debe ser determinado por lo que pensamos que va a hacer una posición: si creemos que no va a ganar más, ciérrala; si creemos que estamos esperando un gran rompimiento a favor, mantenla"* — explícitamente **no** por límites de tiempo, **no** por progreso del target diario, y **no** por intercambio de oportunidad (cerrar una posición perezosa porque otro candidato promete más). Aplicado simétricamente a ganancia y pérdida, con la instrucción de no dejar nada en modo shadow: *"implementa de una vez y se verificará en staging su comportamiento"*, y al llegar a la fase de limpieza final: *"retíralos de todos modos y documéntalo"*.

### Diagnóstico — qué del stack existente ya hacía esto (y qué no)

El stack de salida ya documentado en este archivo (s107-s129: Intelligent Drawdown Monitor, Profit Erosion, PRS anti-estrangulación, UG breakout-follow) ya tenía la filosofía correcta de "nunca cerrar solo por marcador" para el lado de erosión de ganancia — pero:
- El invalidador "SR" de `position_telemetry_watcher.py` (`_evaluate_sr_invalidation_for_snapshot`) usaba conteo de señales autónomas opuestas, **no** los niveles S/R reales de la tesis de entrada, y vivía en shadow.
- `smart_scale_out.py` SSO-4 (Target Contribution) cerraba ganadores por progreso del **target diario del portafolio**, no por evidencia del activo.
- SSO-5 (Re-entry Upgrade) y la rama Opportunity Collapse de SSO-7 cerraban/colapsaban el SL de una posición propia únicamente porque **otro candidato** lucía mejor — comparación externa, no creencia sobre la posición misma.
- `profit_realization_supervisor.py` (PRS) seguía usando `partial_minutes`/`full_minutes` como piso de gracia antes de evaluar degradación — no cierra nunca por tiempo puro (verificado y bloqueado con test de regresión), pero tampoco tenía forma de **extender indefinidamente** la paciencia cuando la tesis seguía intacta.

### Cambios aplicados (6 fases, todas activas — sin etapa shadow)

1. **Fase 0** — `smart_scale_out.py::_check_sso4_target_contribution` (SSO-4) retirado de `SmartScaleOut.evaluate()`. El target diario deja de ser un mecanismo de cierre individual.
2. **Fase 1** — nuevo `position_thesis.py::build_thesis_snapshot()`: consolida `target_price`/`stop_price`/`support_level`/`resistance_level`/`entry_success_probability` de forma uniforme en los 4 regímenes (antes solo existía, disperso, para RANGE_SCALP vía `range_scalp_intelligent_hold.py`). Persistido en `metadata['_thesis_snapshot']` al abrir (`position_manager.py::add_position` spot; `futures_client.py::open_short`/`open_long` futures, ambos lados).
3. **Fase 2-3** — nuevo `position_belief.py::evaluate_belief()`: produce `belief_state ∈ {running_strong, exhausted, uncertain}` combinando momentum/aceleración de `position_telemetry_watcher.py::_analyze_drawdown_trend` (ya existente), probabilidad del predictor entrada-vs-ahora, señal autónoma actual y distancia al target/stop estructural de la tesis. Barra de confianza **asimétrica** (perdedor necesita ≥2 señales favorables sin ninguna adversa; ganador necesita ≥1) y cerca del stop estructural (<0.5%) exige señal de recuperación real, no solo ausencia de deterioro. **Piso estructural**: cruzar el stop de la tesis fuerza `exhausted` de inmediato, sin importar el momentum — este es el límite a la paciencia, no un timer ni un dólar fijo. Se ejecuta cada 1s en `position_telemetry_watcher.py::_run_belief_cycle` (nuevo), reutilizando el buffer de momentum ya existente (cero costo de API extra), persistido en `metadata['belief_last']`.
4. **Fase 4** — `unified_governance_supervisor.py::evaluate()` lee `belief_last` al inicio, antes de cualquier rama de ganancia/pérdida: `exhausted` → cierre inmediato (prioridad 99, por encima de SSO/PRS); `running_strong` → hold, suprimiendo todos los timers de PRS/SSO por debajo — **excepto** cuando el sistema está en `monitor_only` (modo degradado), que nunca se ve overrideado por creencia.
5. **Fase 5** — SSO-5 (`_check_sso5_reentry_upgrade`) retirado del pipeline de cierre; rama Opportunity Collapse de SSO-7 deshabilitada por default (`sso7_opp_enabled: True → False`) — ambas eran "intercambio de oportunidad" puro (cerrar/colapsar una posición propia por comparación con otro candidato). Se conserva el Stagnation Trailing de SSO-7 (solo aprieta SL por falta de progreso propio, nunca por comparación externa). Se auditó PRS línea por línea: no existe un cierre por tiempo puro sin evidencia de degradación (`held_min` es siempre un piso de gracia, nunca un disparador aislado) — documentado con test de regresión (`tests/test_profit_realization_supervisor.py::test_prs_never_closes_on_time_alone_without_degradation_evidence`) en vez de fabricar una remoción inexistente.

### Validación

- Suite completa: 495 passed, mismos 6 fallos preexistentes de antes de esta sesión (`test_config_governance`, `test_entry_quality_gate`×2, `test_portfolio_selector`, `test_position_manager`, `test_staging_compare`) — ninguno relacionado.
- 39 tests nuevos: `tests/test_position_thesis.py` (11), `tests/test_position_belief.py` (10, matriz running_strong/exhausted/uncertain × ganador/perdedor × cerca/lejos del stop), `tests/test_position_belief_ug_integration.py` (8, incluye el carve-out de `monitor_only`), 1 test de regresión en PRS, más ajuste de defaults en `test_smart_scale_out.py`.
- Nueva tarjeta `/metrics/shadow-readiness` (id `position_belief`, modo `active`) en `app.py` para observar la distribución real de `belief_state` en staging.

### Rollback

```json
PATCH /config/kelly {"position_belief_enabled": false}
```
UG vuelve a decidir 100% por PRS/SSO/PeakCapture como antes de s130 (sin tocar código). Para restaurar el intercambio de oportunidad de SSO-7 específicamente: `PATCH /config/kelly {"sso7_opp_enabled": true}`. SSO-4 (Fase 0) y SSO-5 (Fase 5) son remociones deliberadas y permanentes del pipeline — no tienen flag; su rollback es revertir el commit correspondiente en `smart_scale_out.py` (los métodos permanecen en el código como referencia histórica, solo se retiraron de la lista `checks` de `SmartScaleOut.evaluate()`).

### Pendiente explícito

- La Fase 5 se ejecutó de inmediato por directiva explícita del usuario, sin esperar la evidencia de staging que el diseño original de la fase pedía (N≥30 posiciones con `belief_state` registrado antes de retirar las redes de respaldo). Monitorear si posiciones `belief_state=uncertain` (evidencia mixta, o legacy sin `_thesis_snapshot`) quedan con holding times excesivos al perder SSO-5/SSO-7-collapse como red de rotación de capital — su única protección residual es `hard_risk_monitor` y el Intelligent Drawdown Monitor de s120.
- Plan de diseño completo: `C:\Users\raul8\.windsurf\plans\sr-exit-lifecycle-redesign-741a4c.md`.

### Auditoría punto a punto post-implementación (2026-08-16, misma sesión)

Por pedido explícito del usuario, se auditó todo el ciclo de vida (selección → apertura → monitoreo → cierre) buscando bugs/gaps introducidos por s130 o preexistentes no vistos.

**Bug encontrado y corregido — escritura a DB de `belief_last` sin throttle:**
- `_run_belief_cycle` evalúa `evaluate_belief()` cada 1s por posición (barato, en memoria), pero la persistencia a `metadata` **no tenía throttle**, a diferencia de `_persist_snapshots` (throttle 5s) que ya existía en el mismo archivo. Cada tick de 1s escribía la metadata completa a `trades_open` para **cada posición abierta**; en futures, `_persist_smart_meta` hace además un `SELECT` antes del `UPDATE` (2 round-trips por posición por segundo).
- Con los límites documentados en `rules.md §9` (hasta ~32 posiciones abiertas simultáneas entre los 4 regímenes), esto representaba decenas de escrituras/segundo innecesarias a Postgres, sin beneficio real: UG solo lee `belief_last` cada 3-5 min.
- **Fix**: nuevo `_should_persist_belief()` en `position_telemetry_watcher.py` — persiste inmediatamente en toda transición de `belief_state` (nunca se retrasa un `exhausted` nuevo), pero limita re-escrituras del mismo estado a como máximo una cada `position_belief_persist_throttle_seconds` (default 10.0, hot-config). Test de regresión: `tests/test_position_belief_persist_throttle.py` (5 tests).
- Se verificó que el flujo de lectura (`context.metadata` en `run_trades.py` → `UnifiedGovernanceContext`) sigue viendo `belief_last` fresco de inmediato tras cualquier escritura, tanto en spot (mismo objeto `Position` mutado in-place por `position_manager.update_position_metadata`) como en futures (mismo singleton `FuturesClient` vía `_get_futures_client`) — no hay staleness introducida por el throttle, solo se evitan re-escrituras redundantes del mismo valor.

**Gap documentado, no corregido (requiere trabajo adicional fuera de alcance de un fix rápido):**
- `evaluate_belief()` está diseñado para combinar hasta 4 fuentes de evidencia (momentum, probabilidad del predictor "ahora" vs. "al entrar", señal autónoma, EV de `peak_capture_engine`), pero el ciclo de 1s en `position_telemetry_watcher.py` solo alimenta 2 (`momentum` y `signal_direction/strength`). `current_success_probability` requeriría re-ejecutar el modelo ML cada 1s por posición (costoso, deliberadamente no wireado); `peak_capture_prob_higher_peak` se calcula en `peak_capture_engine.py` pero solo dentro del ciclo de 3-5min de `run_trades.py`, nunca en el watcher de 1s. Efecto práctico: un perdedor necesita `favor>=2` para ser `running_strong`, pero con máximo 2 señales disponibles, en la práctica requiere momentum favorable **y** señal alineada con fuerza ≥0.55 simultáneamente — más estricto de lo que el diseño original preveía, pero no incorrecto (nunca cierra ni sostiene sin evidencia real). Pendiente evaluar en una siguiente iteración si vale la pena exponer el `prob_higher_peak` ya calculado por el ciclo lento hacia el `belief_last` de la próxima ventana de 1s (cache de última lectura), en vez de recalcularlo.
- Se verificó que `evaluate_belief()` degrada correctamente a `uncertain` (nunca `exhausted` ni `running_strong` fabricado) cuando `thesis_snapshot=None` (posiciones legacy abiertas antes de Fase 1, o sin predictor) — no requiere fix.

**Resto del pipeline (selección → apertura → cierre):** no se encontraron regresiones nuevas fuera del árbitro de creencia; el resto del stack (staged_entry, entry_decision, hard_risk_monitor, position_intelligence, SSO/PRS remanente) permanece sin cambios de esta sesión y ya fue auditado en iteraciones previas (s76, s93b, s96b, s99, s108, s109 — ver arriba y `rules.md §8`).

Validación final: 500 passed (5 tests nuevos del fix de throttle), mismos 6 fallos preexistentes no relacionados.

---

## Actualización s129 — mitigación bidireccional de ahorcamiento de ganadores (2026-08-08)

### Objetivo

Reducir cierres prematuros de ganancia sin perder control de regresión, manteniendo simetría LONG/SHORT para SPOT y FUTURES.

### Cambios aplicados

- `profit_realization_supervisor.py::_sr_resistance_acceleration`
  - Pasa de sesgo LONG-only a lógica bidireccional LONG/SHORT en `RANGE_SCALP`.
  - SHORT usa zona de soporte + señales de hold/continuidad de soporte como espejo de LONG en resistencia.
  - Si falta `sr_zone_position`, el acelerador no se activa (evita falsos positivos).
- `unified_governance_supervisor.py` (arbitraje `SSO partial + PRS close`)
  - Nuevo guard de follow-through: bloquea escalación a full cuando señal y velocidad todavía favorecen la posición.
  - Nuevo cap configurable para cierre parcial combinado: `ug_partial_to_full_cap_pct`.
  - Conserva escalación a full cuando hay riesgo de regresión (señal opuesta fuerte o velocidad adversa).
- Forensia operativa
  - `tmp_early_close_strangle_analysis_today.py` ahora publica agregados `by_quadrant` y `by_reason_compact` en el JSON para comparar eficacia por lado/mercado y por motivo.

### Validación

- Tests verdes:
  - `tests/test_profit_realization_supervisor.py` (nuevo caso SHORT simétrico incluido).
  - `tests/test_unified_governance_profit_arbitration.py` (nuevo).
- Resultado: `8 passed` en suite focalizada.

### Rollback inmediato

```json
PATCH /config/kelly {
  "ug_allow_partial_to_full_escalation": false,
  "ug_partial_to_full_cap_pct": 1.0
}
```

Rollback de código: revertir los cambios de `profit_realization_supervisor.py` y `unified_governance_supervisor.py` introducidos en s129.

### Evidencia mínima para promoción sostenida

- Ventana 24h/96h con `strangled_count` decreciente.
- `by_quadrant[*].missed_to_realized` no debe subir en el cuadrante dominante de ejecución.
- No degradar `realized` total ni aumentar `adverse60` agregado frente al baseline previo.

## Actualización s121 — Follow inteligente UG activo en todos los modos + evidencia shadow-readiness (2026-08-06)

### Objetivo

Eliminar cierres tempranos de ganancia en los casos donde el rompimiento sigue vivo, sin abrir la puerta a regresiones grandes cuando el impulso se gira en contra.

### Cambios aplicados

- `unified_governance_supervisor.py` incorpora un filtro centralizado (all-modes) que intercepta cierres full de ganancia candidatos (`sso_target_contribution`, `sso_capital_rotation`, `sso_slow_crawl`, `profit_realization_supervisor_full`) y decide entre:
  - **deferir cierre** cuando la señal favorece la posición y la velocidad PnL 1h sigue positiva;
  - **mantener cierre** cuando hay riesgo de regresión (señal opuesta fuerte o velocidad adversa).
- El mecanismo corre en **ejecución real** (`ug_breakout_follow_mode=active`), no solo en shadow.
- Se conservan evidencias para calibración en `/metrics/shadow-readiness` con una tarjeta dedicada (`ug_breakout_follow`) para comparar defer vs close ejecutado y cuantificar riesgo de regresión.
- `sso_target_range_scalp_guard_mode` pasa a `active` por defecto para mantener consistencia con la instrucción de ejecución real.

### Parámetros nuevos (hot-config)

```
ug_breakout_follow_enabled: true
ug_breakout_follow_mode: active          # active|shadow|disabled
ug_breakout_follow_min_hold_minutes: 45
ug_breakout_follow_max_hold_minutes: 240
ug_breakout_follow_min_notional_usdt: 700
ug_breakout_follow_min_pnl_usdt: 2.0
ug_breakout_follow_max_pnl_pct: 0.80
ug_breakout_follow_min_signal_strength: 0.58
ug_breakout_follow_min_velocity_pct_h: 0.08
ug_breakout_follow_cut_signal_strength: 0.62
ug_breakout_follow_cut_velocity_pct_h: 0.12
```

### Rollback inmediato

```json
PATCH /config/kelly {
  "ug_breakout_follow_mode": "shadow"
}
```

Rollback duro:

```json
PATCH /config/kelly {
  "ug_breakout_follow_enabled": false
}
```

### Evidencia operativa mínima esperada

- `evaluated >= 20` y `would_defer >= 5` antes de recalibrar umbrales.
- Revisar split por `reason` y `regression_risk` para evitar sobre-extender en mercados de reversión rápida.

## Actualización s120/s120b — Intelligent Drawdown Monitor + Profit Erosion + Anti-estrangulación PRS (2026-08-03)

### Origen

Análisis cuantitativo de los últimos 14-30 días de producción reveló:
- Net PnL: -$2,138 (30d), -$1,277 (14d)
- Average Winner: +$5.35 vs Average Loser: -$9.17 (ratio 1:1.7)
- Losers held 33% más tiempo que winners
- **$700 left on table** por peak_protection prematura en 14 días
- **21 trades con peak avg +$18** antes de revertir a SL (-$493 total)
- Regime change closures: -$336 inútiles en 30 días

### Cambios aplicados — s120: Intelligent Drawdown Monitor

Nuevo subsistema dentro de `position_telemetry_watcher.py` que monitorea TODAS las posiciones a 1s usando el mismo ciclo de telemetry existente (zero API extra).

**Aclaración de responsabilidad:** esta capa no es "loss-only". Es una capa de vida activa y protección de capital que debe ver tanto pérdidas como ganancias. Su función es detectar pérdida acelerada para cortar/tighten y detectar ganancia que se erosiona para protegerla, pero nunca abrir nuevas posiciones ni arbitrar toda la cadena de exits por encima de SSO/PRS/UG.

**Coexistencia con SSO/PRS/UG:** la misma capa de 1s también debe alimentar y evaluar, sobre snapshots vivos, las señales de `smart_scale_out`, `profit_realization_supervisor` y `unified_governance_supervisor` para que parciales, timing de toma de ganancia y arbitraje final reaccionen sin esperar al siguiente ciclo de 5m. La diferencia con el ciclo principal no es qué observa, sino quién tiene la última palabra: el 1s observa y propone; SSO/PRS/UG siguen consolidando la decisión final.

**Arquitectura:**
```
1s tick → update buffer circular (60 entries) → analyze trend → decide action
                                                       ↓
                                            ┌─────────────────────┐
                                            │  Case A: PnL < -$10 │
                                            │  → CUT / TIGHTEN_SL │
                                            ├─────────────────────┤
                                            │  Case B: PnL > 0    │
                                            │  but eroding         │
                                            │  → PROTECT_PROFIT   │
                                            └─────────────────────┘
```

**Case A — Posición en pérdida (unrealized < threshold_usdt):**
- Trend analysis: velocity ($/s), acceleration, recovery signal, support proximity
- Decision: `CUT` solo cuando falla la ventana de rescate o cuando hay shock adverso claro (ruta rápida)
- Decision: `TIGHTEN_SL` gradual (default 5%; en fase recoverable se usa tighten más suave)
- Decision: `HOLD` → recovery detectada o near support, no actúa

**Ventana de rescate loss-side (implementada):**
- Al cruzar umbral de drawdown (`-5`/`-10` según trayectoria), el 1s no degolla por marcador: abre estado `monitoring` y evalúa si hay rebote real.
- Si aparece recuperación (recovery signal / recent velocity positiva) y mejora suficiente, pasa a `recoverable` y difiere el `CUT`.
- Si ese rebote se revierte con deterioro acelerado y giveback suficiente, pasa a `failed_recovery` y habilita de nuevo el corte duro.
- Si detecta shock adverso (recent velocity muy negativa + aceleración negativa, sin señal de recovery), puede saltar directo al camino de corte rápido.

**Case B — Posición en profit pero erosionándose (s120b):**
- Solo se activa si peak fue >= $5 (filtra ruido)
- Evalúa: velocity adversa sostenida >= 15s, aceleración, recovery signal last 10s
- `PROTECT_PROFIT`: lock 30% del peak (cuando velocidad negativa + acelerando)
- `PROTECT_PROFIT_GENTLE`: lock 15% del peak (cuando velocidad negativa pero decelerando)
- `HOLD`: si hay recovery, near resistance/support, o velocity neutral

**Caso de ganancia:** esta capa sí debe monitorizar ganancias en tiempo real para capturar el mejor tramo posible, pero lo hace de forma conservadora: protege fracción del peak con SL más ajustado, no fuerza una salida total salvo que la pérdida estructural ya esté confirmada por la rama de drawdown.

**SSO/PRS/UG en 1s:** sobre el mismo snapshot, el 1s debe poder recalcular candidatos de SSO/PRS/UG para adelantar parciales, proteger profit y preparar la decisión de arbitraje. Eso no significa que cierre por sí mismo la posición final en todos los casos; significa que la capa de 1s no es ajena a la lógica de salida ganadora.

**Garantías anti-estrangulación (Case B):**
1. **NUNCA cierra** — solo mueve SL para proteger una fracción del peak
2. Solo tightens, nunca loosens (SL solo sube/baja según side)
3. Nunca mueve SL arriba del 97% del precio actual (siempre deja 3% buffer)
4. Recovery signal (últimos 10s bouncing) → siempre HOLD
5. Near resistance/support → siempre HOLD (pullback esperado)
6. Min peak $5 antes de activar erosion detection (ignora ruido)
7. Lock fractions conservadores: max 30%, gentle 15%

### Cambios aplicados — s120: Anti-estrangulación PRS

**Peak protection momentum-aware:**
- Cuando `momentum_score >= 70`, peak_protection threshold se relaja a 0.50 (antes 0.65)
- Permite hasta 50% de giveback antes de cortar (antes 35%)
- Floor mínimo de peak subido a 2% cuando momentum activo (evita trigger en peaks triviales)
- Estimación de impacto: +$700 recuperados por no cortar runners prematuramente

### Cambios aplicados — s120: Hot-config de trailing/timing

| Parámetro | Antes | Ahora | Efecto |
|-----------|-------|-------|--------|
| `trailing_stop_distance_pct` | 1.0% | 1.4% | Más espacio para runners |
| `trailing_stop_activation_pct` | 2.0% | 2.5% | Trailing arranca más tarde |
| `peak_protection_spot_bull_pct` | 0.65 | 0.50 | Permite 50% giveback |
| `peak_protection_spot_bull_profile_a_pct` | 0.70 | 0.45 | Profile A más permisivo |
| `smart_tp_max_extensions` | 3 | 4 | Runners pueden extender más |
| `smart_tp_extend_score_threshold` | 2 | 1 | Más fácil extender |
| `smart_tp_max_pnl_pct` | 15% | 20% | Cap extensión más alto |
| `profit_realization_spot_bull_min_partial_minutes` | 90 | 120 | No cortar antes de 2h |
| `profit_realization_futures_partial_minutes` | 30 | 120 | Futures: no cortar antes de 2h |
| `lifecycle_structure_partial_close_pct` | 0.35 | 0.20 | Parciales más pequeños |

### Config nuevo (hot-configurable vía PATCH /config/kelly)

```
drawdown_intelligent_monitor_enabled: true    # master switch
drawdown_monitor_threshold_usdt: -10.0        # umbral Case A
drawdown_cut_velocity_threshold: -0.0003      # min velocity adversa para CUT
drawdown_cut_min_data_points: 20              # min data antes de decidir
drawdown_cut_min_consecutive: 3               # 3 CUT seguidos antes de actuar
erosion_min_peak_usdt: 5.0                    # min peak para activar Case B
erosion_lock_fraction: 0.30                   # lock 30% peak (PROTECT_PROFIT)
erosion_lock_fraction_gentle: 0.15            # lock 15% peak (PROTECT_PROFIT_GENTLE)
erosion_velocity_threshold: -0.00015          # min velocity adversa para erosion
```

### Rollback inmediato

```json
PATCH /config/kelly {
  "position_reaction_policy_mode": "telemetry_only",
  "drawdown_intelligent_monitor_enabled": false,
  "trailing_stop_distance_pct": 1.0,
  "trailing_stop_activation_pct": 2.0,
  "peak_protection_spot_bull_pct": 0.65
}
```

### Costos de recurso

- **API**: Zero extra — reutiliza precios del ciclo de telemetry (spot REST) y WebSocket mark prices (futures)
- **CPU**: ~60 operaciones float por posición por segundo — negligible
- **Memoria**: buffer circular de 60 entries/posición (~4KB/posición)
- **Rate limit**: con 12 posiciones spot = 720 weight/min (< 1200 limit Binance)

### Validación

- Module load test: OK
- Unit tests de trend analysis y erosion evaluator: OK
- Suite completa: pass (excepto 1 falla preexistente en `test_portfolio_selector`)
- Desplegado — `RUNNING_FULL`, 4 posiciones activas, sin errores en logs

### Decisiones de diseño

- El monitor evalúa TODAS las posiciones (profit y loss) porque los datos muestran que el mayor daño viene de posiciones que **estaban en profit y se revirtieron** ($493 de 21 trades con avg peak +$18), no de posiciones que entraron directamente en pérdida.
- La evaluación de erosión NUNCA cierra — solo protege una fracción del peak. Esto evita el "estrangulamiento" que fue el problema original (system cutting winners too early).
- La capa de 1s debe ser profit-aware: su trabajo es capturar y preservar ganancias tempranamente, no dejar que el runner regrese al stop por falta de monitoreo. La restricción es sobre la forma de actuar, no sobre el tipo de posición que observa.
- SSO/PRS/UG conviven con esta telemetría 1s: la capa observa, pre-computa y protege; el trade cycle 5m sigue siendo el punto donde se consolida la acción final cuando hay conflicto entre advisories.
- Se usa un buffer circular de 60s en vez de un modelo ML porque: (a) zero latencia adicional, (b) interpretable, (c) rollback trivial vía config, (d) no requiere training data.
- El threshold de CUT requiere 3 decisiones consecutivas (3 segundos) — evita flip-flopping por un solo tick adverso.
- Peak protection relajada con momentum porque los datos muestran correlación directa: posiciones cortadas por `prs_peak_protection` con momentum > 70 tenían avg peak $71 pero realized only $7.89 — el peak no era el techo real.

### Pendiente explícito

- Monitorear en producción la frecuencia de PROTECT_PROFIT vs HOLD decisions para calibrar `erosion_velocity_threshold` (puede necesitar ajuste según volatilidad del mercado actual).
- Evaluar si `erosion_lock_fraction` de 30% es demasiado conservador o agresivo con datos reales de las próximas 48h.
- Considerar incorporar ATR dinámico al threshold de erosion velocity (mercados más volátiles necesitan threshold más permisivo).
- Confirmar que `drawdown_cut_min_consecutive=3` es suficiente para evitar falsos positivos en flash crashes temporales.

---

## Actualización s118 — el circuit-breaker de portafolio de s117 pausaba "a ciegas"; corregido a pausa inteligente que lee la causa (2026-08-03)

### Origen

Feedback directo del usuario tras desplegar s117: *"espera porque vas a pausar el sistema ahora? que no esta ya arreglado el problema? dijiste que se habia estabilizado BTC. no hagas estupideces sin preguntar."* — y luego: *"no debes pausar solo por pausar, debe ser un pausado inteligente, leer la causa y revisar si conviene pausar o no."*

### Problema con el diseño original de s117

`_portfolio_drawdown_circuit_breaker` pausaba puramente por `SUM(pnl) <= -umbral` en una ventana fija de 120 min — un contador que **solo decae por tiempo**, sin mirar si la condición que causó la pérdida sigue activa. Efecto: aunque BTC ya se hubiera estabilizado (verificado: solo +0.20% en los 30 min previos al fix, muy por debajo de cualquier umbral de alerta), el sistema **habría permanecido pausado hasta 2 horas** después de la última pérdida grande, solo porque esa pérdida seguía "dentro de la ventana" de la suma.

### Fix — pausa en 2 pasos: síntoma + causa

`_portfolio_drawdown_circuit_breaker` ahora funciona en 2 fases:
1. **Síntoma** (igual que s117): ¿el PnL realizado de la ventana sigue por debajo del umbral? Si no, no hay nada que evaluar.
2. **Causa** (nuevo): si el síntoma está presente, identifica qué lado domina la pérdida (`realized_short` vs `realized_long`, vía `side='SELL'/'BUY'` en `trades_closed`) y **revisa el momentum reciente de BTC** (`portfolio_drawdown_recheck_momentum_minutes`, default 30 min) en la dirección que importaría: si el libro perdió por SHORT, ¿BTC sigue subiendo ahora mismo? Si perdió por LONG, ¿BTC sigue bajando? Solo si la condición adversa **sigue activa** (`portfolio_drawdown_recheck_momentum_pct`, default 0.5%) se pausa. Si el mercado ya se estabilizó, **no pausa**, aunque la suma de la ventana siga negativa.
- Fail-safe conservador: si no hay `client` disponible para re-chequear el momentum, o la consulta de klines falla, pausa (mismo comportamiento que s117) — nunca falla hacia "no pausar" por un error técnico.
- Verificado con datos reales al momento del fix: PnL realizado seguía en -$184.74 (dominado 100% por SHORT), pero BTC solo se movió +0.20% en los 30 min previos (vs. umbral de 0.5%) — con la lógica nueva, el sistema **no pausa**, comportamiento correcto y verificado antes de desplegar.

### Decisiones de diseño

- No se eliminó el circuit-breaker, se corrigió su criterio de decisión — la protección contra pérdidas correlacionadas activas sigue intacta (ver test `test_portfolio_drawdown_pauses_when_loss_and_btc_still_adverse_for_shorts`).
- Se usó BTC como proxy de "condición de mercado adversa" en vez de recalcular el momentum de cada símbolo perdedor individualmente — consistente con el resto del sistema, que ya usa BTC como señal macro (`futures_no_short_btc_*_threshold`).

### Validación

- Tests actualizados/nuevos en `tests/test_s113_entry_pipeline.py`: pausa cuando la causa sigue activa (SHORT y LONG), **no pausa cuando el mercado ya se estabilizó** (el escenario exacto reportado por el usuario), fail-safe sin cliente.
- Suite completa: 590/593 passed (3 fallas preexistentes, independientes).
- Desplegado — `system_state=RUNNING`, 4 posiciones sobrevivieron el restart.

### Pendiente explícito

- Calibrar `portfolio_drawdown_recheck_momentum_minutes`/`_pct` con más datos de producción.
- Confirmar en logs de producción (`[Portfolio/Drawdown] ... NOT pausing (cause has passed)`) que el comportamiento en vivo coincide con lo verificado manualmente antes del despliegue.

---

## Actualización s117 — gate de BTC-momentum replicado en la ruta staged + circuit-breaker de PnL de portafolio (2026-08-03)

### Origen

Continuación directa de s116: reconstruí la línea de tiempo completa desde las 7am hora local (13:00 UTC) y encontré que el daño real fue mayor (12 cierres, 10 pérdidas/2 ganancias, -$221.77 neto) y que **el sistema siguió abriendo SHORTs nuevos activamente durante toda la cascada** (`GIGGLEUSDT` a las 14:14, en el mismo minuto que 3 pérdidas seguidas; `ICPUSDT`, `MIRAUSDT`, `ADAUSDT` después). Se cerró manualmente `INJUSDT` (única posición en riesgo inmediato real, a 0.38% de su SL) antes de implementar los fixes, con confirmación explícita del usuario.

### Diagnóstico — ninguno de los mecanismos existentes (ni los de s115/s116) ataca esta causa

- El gate de BTC-momentum (`BTC_MOMENTUM`/`BTC_1H_BOUNCE`/`BTC_3H_RALLY`) existe desde antes de esta sesión pero **solo en las rutas legacy** (`_buy_top_symbols`, `_maybe_open_range_scalp_shorts`, `_maybe_open_sr_pool_shorts`) — nunca en la ruta staged fast-path, que fue el origen de 6 de las 7 aperturas de la cascada de hoy.
- El fix de s116 (mass-SL) es **reactivo**: solo actúa después de 3+ pérdidas ya ocurridas. No hay ningún mecanismo **preventivo** que reaccione a "el mercado en general se está moviendo en contra" antes de que se acumule el daño.
- `circuit_breaker_enabled`/`_circuit_breaker_check` (el único circuit-breaker de propósito general que existía) reacciona **solo a fallas técnicas de API, explícitamente no a pérdidas de mercado** (así lo dice su propio docstring) — confirmando que nunca existió una red de seguridad basada en PnL real.

### Cambios aplicados

1. **`run_trades.py::_btc_momentum_blocks_new_shorts`** (nueva): replica los 3 chequeos de BTC-momentum de la ruta legacy, calculada una sola vez por lote (no por candidato) para eficiencia. Conectada en `_execute_validated_fast_path` justo antes de reservar capital para un SHORT. Config: `futures_staged_btc_momentum_gate_enabled` (default `True`), reutiliza los umbrales ya existentes (`futures_no_short_btc_4h_threshold`, `_1h_threshold`, `_3h_threshold`).
2. **`run_trades.py::_portfolio_drawdown_circuit_breaker`** (nueva): pausa **todas** las aperturas nuevas (SPOT + FUTUROS, todos los regímenes) si el PnL **realizado** del portafolio completo cayó más de `portfolio_drawdown_max_loss_usd` (default $150) dentro de una ventana móvil (`portfolio_drawdown_window_minutes`, default 120 min). Deliberadamente más simple que un cálculo de PnL no realizado en tiempo real (evita fetches de precio en vivo por cada posición abierta en el hot path) — captura directamente el síntoma que importa. Conectada como Gate 0 en `_execute_validated_fast_path` (staged) y al inicio de `_buy_top_symbols` (legacy) — cobertura completa de ambas rutas de apertura.
3. Verificado antes de desplegar: el PnL realizado real de los últimos 120 min al momento del despliegue era **-$184.74**, ya por debajo del umbral de -$150 — el circuit-breaker se activa inmediatamente al desplegar, pausando toda apertura nueva hasta que la ventana se recupere.

### Decisiones de diseño

- El circuit-breaker de portafolio usa solo PnL **realizado**, no marca-a-mercado — más simple, más rápido, sin dependencia de fetches de precio en vivo por posición, y ya suficiente para capturar el patrón real observado (una racha de cierres realizados negativos).
- Se aplicó a **ambas** rutas de apertura (staged y legacy) desde el principio, a diferencia del gate de BTC-momentum (que por ahora solo cubre la ruta staged, ya que la legacy ya lo tenía) — el circuit-breaker de portafolio es nuevo en ambos lados por igual.
- No se implementó cierre proactivo de posiciones ya abiertas al detectar el drawdown (alcance mayor, requiere diseño propio) — el circuit-breaker de esta sesión solo previene *nuevas* aperturas.

### Validación

- Tests nuevos en `tests/test_s113_entry_pipeline.py`: 11 casos (`_btc_momentum_blocks_new_shorts` con velas 4h/1h/3h reales y gate desactivado; `_portfolio_drawdown_circuit_breaker` con bloqueo/no-bloqueo/desactivado/sin-db/fallo-de-query).
- Suite completa: 587/590 passed (3 fallas preexistentes, independientes).
- Desplegado — `system_state=RUNNING`, 4 posiciones sobrevivieron el restart (`INJUSDT` cerrada manualmente antes del despliegue, -$14.19, mejor que dejarla llegar a su SL completo).

### Pendiente explícito

- Confirmar en los logs de producción que el circuit-breaker de portafolio efectivamente pausó las aperturas en el primer ciclo tras el despliegue (esperado dado que -$184.74 < -$150, pero no verificado en vivo dentro de esta sesión por la cadencia de ciclos).
- Calibrar `portfolio_drawdown_max_loss_usd`/`_window_minutes` con más datos de producción.
- Evaluar cierre proactivo de posiciones abiertas cuando se detecta un drawdown de portafolio (alcance mayor, pendiente).

---

## Actualización s116 — circuit-breaker Mass-SL nunca se disparó en una cascada real de 8 pérdidas/2h (BTC +2%) (2026-08-03)

### Origen

Usuario reportó "puras pérdidas miserables" desde las 7am hora local. Verificado: BTC subió de $62,670 a $63,956 (+2.05%) entre 13:00-14:45 UTC, arrastrando casi todos los SHORT del portafolio (100% del libro era SHORT). 8 de 10 cierres en esa ventana fueron pérdidas, -$183.36 neto.

### Causa raíz — el circuit-breaker existía pero nunca se activó, por 2 razones + 1 gap de cobertura

1. **Filtro de razón demasiado estricto**: `_sl_count_this_cycle` (PB2, `run_trades.py` ~línea 11800) solo contaba `reason == 'stop_loss'` exacto. De las 8 pérdidas, solo 2 (`LAUSDT`, `RIFUSDT`) tenían esa razón exacta — el resto (`stop_loss_binance` ×3, `binance_unidentified_close` ×2, `adverse_signal_exit` ×1) eran invisibles para el contador. **Mismo patrón de bug ya corregido en s110 para `_consecutive_sl_count` (por símbolo), nunca aplicado a este contador (de portafolio).**
2. **Ventana demasiado angosta**: el contador se reseteaba a 0 en cada ciclo (~3-5 min) — nunca acumulaba across una cascada que se desarrolló a lo largo de **2 horas**.
3. **Gap de cobertura**: aunque el cooldown se hubiera disparado correctamente, **la ruta staged fast-path (`_execute_validated_fast_path`, la dominante para RANGE_SCALP desde s110) nunca verificaba `_FUTURES_MASS_SL_COOLDOWN_UNTIL`** — solo las rutas legacy (`_buy_top_symbols`, `_maybe_open_range_scalp_shorts`, `_maybe_open_sr_pool_shorts`) lo hacían. 6 de las 7 posiciones de la cascada de hoy abrieron vía `STAGED_SHORT_DIRECTION` — la ruta sin protección.
4. Este riesgo ya estaba documentado sin resolver: hipótesis 6 de este mismo documento ("Correlación / mass-SL... la pausa global no es suficiente") y tarea C7 del checklist original ("si múltiples posiciones cierran SL en la misma **hora**, activar pausa global") — nunca completamente implementada; lo que existía (PB2) era una versión más débil (por ciclo, no por hora).

### Cambios aplicados

1. **`run_trades.py::_is_losing_close_dict`** (nueva, mirror de `_is_losing_close` de s110 pero para los dicts que retorna `check_positions()`): cualquier razón con PnL negativo cuenta, no solo `stop_loss` exacto. Conectada en el conteo por-ciclo existente.
2. **`run_trades.py::_mass_sl_rolling_window_count`** (nueva): cuenta pérdidas reales en `trades_closed` en una ventana móvil real (`futures_mass_sl_window_minutes`, default 75 min), no reseteada por ciclo. El circuit-breaker ahora dispara si **cualquiera** de los dos conteos (mismo ciclo O ventana móvil) alcanza `futures_mass_sl_threshold`.
3. **`_execute_validated_fast_path`**: agregado el chequeo de `_FUTURES_MASS_SL_COOLDOWN_UNTIL` (antes ausente) justo antes de reservar capital para un SHORT — cierra el gap de cobertura de la ruta staged.

### Decisiones de diseño

- Se mantuvo el conteo por-ciclo original (no se eliminó) — se usa como disparador OR junto con la ventana móvil, para no perder la detección rápida de un evento verdaderamente simultáneo (múltiples SL en el mismo tick de precio).
- No se tocó el gate de BTC-momentum (ausente en la ruta staged, identificado en el mismo análisis) — queda como tarea separada, de menor prioridad que este fix dado que el circuit-breaker de portafolio cubre el síntoma de forma más general (cualquier causa de pérdidas correlacionadas, no solo BTC).

### Validación

- Tests nuevos en `tests/test_s113_entry_pipeline.py`: 7 casos (`_is_losing_close_dict` con razones variadas incluyendo las de la cascada real, `_mass_sl_rolling_window_count` con conteo correcto/sin db/con error).
- Suite completa: 578/581 passed (3 fallas preexistentes, independientes).
- Desplegado — `system_state=RUNNING`, 5 posiciones sobrevivieron el restart.

### Pendiente explícito

- Calibrar `futures_mass_sl_window_minutes` (default 75 min) y `futures_mass_sl_threshold` (default 3) con más datos de producción — la calibración actual es un punto de partida razonable, no validado estadísticamente.
- Replicar el gate de BTC-momentum (`BTC_MOMENTUM`/`BTC_1H_BOUNCE`/`BTC_3H_RALLY`) en la ruta staged — mismo patrón de gap de cobertura, pendiente como mejora complementaria.
- Evaluar si el cooldown debería también cerrar proactivamente posiciones SHORT ya abiertas cuando se detecta una cascada (hoy solo bloquea nuevas aperturas) — alcance mayor, requiere diseño propio.

---

## Actualización s114 — fix real de la pierna fantasma SL=TP=entry (s112 quedó solo diagnosticado, no corregido) (2026-08-03)

### Origen

El usuario preguntó por qué `take_profit`/`sso_target_contribution` tomaban ganancias muy pequeñas en 24h. Medición: `take_profit` promedió solo 0.229%/$0.63 en 24h (5 cierres) — verificado que los 5 tenían `order_id` con prefijo `RECONCILE_` (la firma exacta del bug diagnosticado en s112 pero nunca corregido, porque la sesión pivotó al plan s113). Confirmado con un caso nuevo real: `EDENUSDT` cerró con **pérdida** (-0.08%) pero etiquetado como `take_profit` — prueba definitiva de que no es toma de ganancias real, es el artefacto de reconciliación.

### Fix implementado

`binance_truth_guard.py::auto_reconcile_futures` (rama `BINANCE_MISMATCH_RUNTIME_MISSING`):
- Reemplazado `_pos.get("stopLossPrice")`/`_pos.get("takeProfitPrice")` (campos inexistentes en `/fapi/v2/positionRisk`, siempre 0.0 → colapsaban SL/TP al precio de entrada) por: (1) valores por defecto sanos basados en el precio de entrada (3%/6%, mismas proporciones que `_sync_open_positions`), (2) sobrescritos por el trigger real de la orden algo (`STOP_MARKET`/`TAKE_PROFIT_MARKET`) vía `/fapi/v1/openAlgoOrders` cuando existe, igual que la ruta de sincronización ya correcta.
- **Guard defensivo nuevo**: si `sl`/`tp` terminan iguales entre sí o iguales al precio de entrada, la importación se descarta (`skipped`, razón `sl_tp_collapsed_to_entry_guard`) en vez de persistir una posición corrupta.
- `_smart_meta['learning_eligible'] = False` en las posiciones importadas por esta ruta — pero se encontró que `_save_futures_closed` **ignoraba ese campo y hardcodeaba `learning_eligible=True` siempre**; corregido para leer `pos._smart_meta.get('learning_eligible', True)`.

### Validación

- Tests nuevos en `tests/slow/test_position_reconciliation.py`: SL/TP nunca colapsan a la entrada (con o sin fallback), la orden algo real gana sobre el default cuando existe, dirección correcta (SL arriba/TP abajo para SHORT). 17/17 pasan (15 preexistentes + 2 nuevos).
- Suite completa: 563/566 (mismas 3 fallas preexistentes, independientes).
- Desplegado — `system_state=RUNNING`, 5 posiciones sobrevivieron el restart.

### Pendiente

- Medir en 7-14 días si el patrón `RECONCILE_` + cierre en segundos desaparece.
- El `meta_payload` de `_save_futures_closed` sí embebe `smart_meta` completo (incluyendo `synthetic`/`learning_eligible`) anidado bajo `metadata->'smart_meta'` — pero el caso auditado en s112 (`ONDOUSDT` id 1400) llegó por la ruta `ghost_reconciled_close`, que construye su metadata de forma distinta y no preservó `synthetic`. No se investigó esa ruta en esta sesión — queda pendiente si se sigue viendo el flag perdido en cierres futuros.

---

## Actualización s112 — auditoría de todos los cierres 48h: pierna fantasma SL=TP=entry (4 instancias) + duplicado de cierre por horas (3 instancias) (2026-08-02/03)

### Origen: auditoría solicitada de TODOS los cierres de las últimas 48h, tras encontrar el caso ONDOUSDT (ver s110/s111, sección de análisis SR)

El usuario pidió repetir el mismo análisis forense (aplicado inicialmente solo a `ONDOUSDT`) sobre **todos** los cierres de 48h, para ver si era un caso aislado. No lo fue.

### Hallazgo 1 — pierna fantasma con `sl_price == tp_price == entry_price` (4 instancias, no 1)

Mismo patrón exacto en `DOGEUSDT` (1-ago 19:49:43→19:49:46, +$1.34), `WIFUSDT` (1-ago 23:34:05→23:34:09, +$6.06), `TAOUSDT` (2-ago 23:39:44→23:39:47, +$0.19) y `ONDOUSDT` (2-ago 23:55:17→23:55:21, +$4.22): todas con `order_id` prefijo `RECONCILE_`, cerradas por `take_profit` en 2.7-4.0 segundos.

**Causa raíz** (`binance_truth_guard.py::auto_reconcile_futures`, rama `BINANCE_MISMATCH_RUNTIME_MISSING`, línea ~569-570):
```python
_sl = _safe_float(_pos.get("stopLossPrice"), 0.0) or _entry
_tp = _safe_float(_pos.get("takeProfitPrice"), 0.0) or _entry
```
El objeto de posición de Binance (`/fapi/v2/positionRisk`) no tiene campos `stopLossPrice`/`takeProfitPrice` (viven en órdenes algo separadas, `/fapi/v1/openAlgoOrders`) — la llamada siempre devuelve 0.0, y el `or _entry` de Python fija ambos al precio de entrada. La siguiente observación de precio en cualquier dirección satisface trivialmente "TP alcanzado". `_sync_open_positions` (la ruta "hermana" de sincronización al arrancar) ya resuelve esto correctamente leyendo `openAlgoOrders`; esta ruta de `binance_truth_guard.py` reimplementó el mismo problema sin reutilizar esa lógica ya probada — mismo patrón de "rutas de reconciliación independientes sin contexto compartido" documentado en s105f/s105h más abajo.

**Impacto verificado con velas reales:** `TAOUSDT` siguió cayendo de 192.28 a 190.56 (-0.9% adicional) en la hora siguiente; `ONDOUSDT` de 0.378 a 0.3714 (-1.75% adicional) en 20 minutos. Las 4 cerraron "a favor" por casualidad (el precio ya venía moviéndose en la dirección correcta al crearse la pierna fantasma) — con la misma probabilidad podrían haber cerrado en pérdida instantánea, ya que el diseño no depende de nada real, solo de en qué dirección se movió el siguiente tick.

### Hallazgo 2 — duplicado de cierre por horas (variante distinta del caso HYPERUSDT de 47ms)

`ONDOUSDT` (ids 1398/1400, separados 32min), `WIFUSDT` (ids 1377/1387, separados 4h) y `DOGEUSDT` (ids 1372/1386, separados 3h44m) — cada uno cuenta el mismo PnL 2 veces en `trades_closed`. A diferencia de la duplicación de `HYPERUSDT` (2 hilos procesando la misma fila en 47ms, ya corregido con el claim atómico de s110), aquí la fila de `trades_open` huérfana (creada por el Hallazgo 1) sobrevive hasta el **siguiente restart del contenedor**, momento en que `_sync_open_positions`/Ghost-L1/L2 la encuentra de nuevo (ya no existe en Binance) y la cierra una segunda vez. El claim atómico de s110 no cubre este caso porque no es una carrera entre 2 procesos simultáneos — es la misma fila, procesada correctamente dos veces en momentos distintos porque nunca debió existir.

### Hallazgo 3 (confirmación retroactiva) — `binance_safety_close` con PnL=$0 enmascarado (ya corregido en s110, Fase 1)

`HYPERUSDT` (id 1389, -$29.05 real vs $0 registrado) y `ALLOUSDT` (id 1383, -$22.22 real vs $0 registrado) — ambos anteriores al fix de hoy. Sin acción adicional requerida; confirma que el fix era necesario.

### Cambios pendientes de implementar (no aplicados aún — pendiente confirmación del usuario)

1. `binance_truth_guard.py::auto_reconcile_futures` (rama `BINANCE_MISMATCH_RUNTIME_MISSING`): reemplazar el cálculo de `_sl`/`_tp` reutilizando la lógica de `openAlgoOrders` ya existente en `_sync_open_positions`, en vez de leer campos inexistentes del objeto de posición.
2. Guard defensivo: rechazar/alertar si se intenta persistir una `FuturesPosition` con `sl_price == tp_price` o con cualquiera de los dos igual a `entry_price` — nunca debe pasar silenciosamente.
3. Verificar que `learning_eligible=False` se propague para estos cierres sintéticos (igual que Ghost-L1/L2), para no contaminar entrenamiento de modelos con resultados de azar.
4. Confirmar si `_smart_meta["synthetic"]=True` (puesto al crear la pierna fantasma) sobrevive hasta `trades_closed.metadata` — en el caso auditado (`ONDOUSDT` id 1400) no apareció, solo `ghost_reconciled: true`.

### Validación pendiente

- Tests nuevos para `auto_reconcile_futures` con el fix de SL/TP real, y para el guard defensivo de `sl==tp`.
- Medir en 7-14 días si el patrón `RECONCILE_` + cierre en <10s desaparece por completo.

---

## AIC-20260807-01 — telemetría FUTURES y cap USD real

Esta sección es la rama B, **Proteger después de abrir**, de la primera ejecución del protocolo canónico **AIC — Análisis Inteligente de Cierres**. La rama A está documentada en `calibracion_entradas-a20ab8-v3.md`.

### Incidente y causas raíz

- Nueve cierres auditados cruzaron -$10 y seis cruzaron -$18, pero 0/9 tenían snapshots FUTURES en `position_telemetry`.
- `position_telemetry_watcher.py` buscaba un campo de cantidad incompatible con el snapshot FUTURES real (`qty`), por lo que el hilo seguía vivo mientras omitía silenciosamente esas posiciones.
- El PnL FUTURES calculado por telemetry volvía a multiplicarse por leverage, aunque `qty` ya expresa exposición contractual.
- La velocidad se medía en unidades crudas de precio, haciendo que el mismo umbral significara cosas distintas por símbolo.
- El loop independiente de 2s comprobaba SL/TP de FUTURES, pero no aplicaba el cap nominal `max_loss_usd_per_trade`.

### Correcciones

- Telemetry lee `qty`, usa fallback estable `SYMBOL_SIDE`, calcula PnL contractual sin multiplicar leverage y normaliza velocidad por precio.
- `hard_risk_monitor.py` cierra FUTURES al alcanzar `max_loss_usd_per_trade` con `close_reason=max_loss_usd_stop`.
- La capa de 1s decide rebote/drawdown; la capa independiente de 2s es el backstop duro en USD.

### Validación y límites

- Regresiones focales en `tests/test_position_telemetry_profit_guard.py` y `tests/test_futures_hard_sl.py`: 17 passed.
- Replay no-lookahead: `Dev/benchmarks/incident_preopen_1h_replay_20260807.json`.
- No se adopta un corte ciego a -$10: HOMEUSDT cruzó ese nivel y terminó +$7.81. El cap duro permanece en -$18 mientras el watcher decide antes cuando hay deterioro confirmado.
- Desplegado el 2026-08-07: `system_state=RUNNING`, hard-risk y telemetry sin errores, y 21 snapshots recientes para 3 posiciones FUTURES después del restart.
- Pendiente fuera de muestra: observar ejecuciones reales de `max_loss_usd_stop` y comparar excursión adversa/cierre durante 5-7 días.

## Actualización s107 — Hard SL/TP (Binance) vs System SL/TP (software): el margen de seguridad colapsaba en el primer trailing, y no había monitoreo rápido para futuros (2026-08-01)

### Contexto / pregunta del usuario

El operador preguntó, tras revisar dos cierres `binance_unidentified_close` reales (`ESPUSDT` +$12.33, `XPLUSDT` -$35.95, ambos 31-jul), si Binance "movía" el SL/TP por su cuenta, y planteó: *"de nada nos sirve tener un SL más holgado que Binance si al final Binance va a cerrar la posición"*. Terminología adoptada: **Hard SL/TP** = la orden algo real en Binance (lo único que puede ejecutar la posición); **System SL/TP** = lo que el software trackea (`pos.sl_price`/`pos.tp_price`, usado por `is_sl_hit()`/`is_tp_hit()`).

### Causa raíz 1 — no existía monitoreo rápido (2s) para futuros

`hard_risk_monitor.py` (loop de 2s) solo cubría **SPOT** (`monitor_spot_hard_stops_once`, fix de s99d para el mismo patrón: *"el hard-risk monitor revisa cada 2s pero el trade cycle corre cada 3-5 min; si el pico ocurre entre ciclos, no se protege"*). Ese mismo gap **nunca se extendió a futuros**: `FuturesClient.check_positions()` (que evalúa `is_sl_hit()`/`is_tp_hit()`) solo se llamaba dentro del ciclo principal de trading — cada 3 a 45 minutos según el régimen — mientras Binance vigila su orden algo en tiempo real.

### Causa raíz 2 (la más grave) — el margen de seguridad colapsaba en el primer trailing/breakeven

Al abrir una posición futures (`FIX-T2A`), el Hard SL se coloca deliberadamente 1.5x más ancho que el System SL (red de seguridad de último recurso). Pero en `FuturesClient.update_sl_tp()`, cada vez que trailing/breakeven/SSO movían el SL, la nueva orden en Binance se colocaba **exactamente** en el mismo precio que el System SL — sin volver a aplicar el margen. Es decir: desde el primer ajuste de trailing en adelante, Hard SL == System SL, y como el motor de Binance reacciona instantáneamente mientras el software revisa cada 3-45 min, **Binance ganaba la carrera casi siempre** — el System SL dejaba de ser el mecanismo real de cierre.

### Causa raíz 3 — atribución incorrecta de `close_reason` para cierres por algo-order

Cuando una orden STOP_MARKET/TAKE_PROFIT_MARKET (algo order) se dispara, Binance la ejecuta internamente como `MARKET`. Tanto la consulta REST (`/fapi/v1/order`) como el evento del user-data-stream describen el fill resultante con `type=MARKET`, nunca `STOP_MARKET`/`TAKE_PROFIT_MARKET` — por lo que `_classify_close_reason()` nunca podía mapear el tipo de orden correctamente para nuestras propias órdenes SL/TP, y caía al bucket genérico `binance_unidentified_close`.

### Tamaño del problema (datos reales, 7 días, futuros)

| close_reason | n | PnL promedio | PnL total |
|---|---|---|---|
| `binance_unidentified_close` | 23 | -$15.52 | **-$356.86** |
| `stop_loss_binance` | 6 | -$18.49 | -$110.96 |
| `stop_loss` (software) | 3 | -$15.56 | -$46.69 |

`binance_unidentified_close` era el segundo bucket más frecuente de todo el sistema en futuros y el de peor PnL promedio — casi con certeza compuesto en su mayoría por SL/TP reales de Binance mal etiquetados, varios potencialmente evitables si el software hubiera cerrado primero.

### Cambios aplicados

- **`futures_client.py`**: nueva constante `_SL_SAFETY_MARGIN_MULTIPLIER = 1.5` (antes hardcodeada como `1.5` solo en la apertura). `update_sl_tp()` ahora recibe `current_price` opcional y, en cada ajuste de SL (trailing/breakeven/SSO/SyncSL), coloca el Hard SL en Binance con el mismo margen relativo a precio actual usado al abrir, mientras `pos.sl_price` (software) se queda en el valor solicitado tal cual. Se persiste `pos._smart_meta['hard_sl_price']` en apertura y en cada ajuste, para trazabilidad y para la Opción C. TP nunca se ensancha (no hay concepto de red de seguridad para una salida favorable — Hard TP == System TP siempre, sin cambios).
- **`futures_client.py::_infer_close_reason_from_price()`** (nuevo): heurística de respaldo — compara el precio de cierre contra `pos.sl_price`/`pos._smart_meta['hard_sl_price']`/`pos.tp_price` con tolerancia de 1.5% (calibrada contra los casos reales `ESPUSDT`/`XPLUSDT`, que tuvieron ~0.02%-0.54% de slippage sobre el nivel). Conectada en `_classify_close_reason()` (antes del fallback genérico) y en el handler del evento de cierre del user-data-stream, vía nuevo parámetro `pos` propagado a través de `_get_realized_pnl_from_trades()`.
- **`hard_risk_monitor.py`**: nueva función `monitor_futures_hard_stops_once()`, paralela a `monitor_spot_hard_stops_once` (s99d) — revisa `is_sl_hit()`/`is_tp_hit()` de posiciones futures cada 2s y cierra proactivamente, sin duplicar la lógica pesada de trailing/SmartTP/SSO (que se queda en el ciclo principal).
- **`orchestrator.py::_hard_risk_monitor_loop`**: ahora llama a `monitor_futures_hard_stops_once(comp)` junto con la de spot.
- **No se tocó**: la Opción E del plan (verificación periódica de que las órdenes SL/TP siguen vivas en Binance, ante cancelación por liquidación/ADL) y la Opción D (alimentar el SL real al sizing/EV de Kelly) quedan pendientes para una sesión separada, dado su mayor alcance/impacto en capital arriesgado.

### Validación

- Tests nuevos: `tests/test_futures_hard_sl.py` (8 casos) — el margen se preserva en trailing SHORT/LONG, TP nunca se ensancha, la heurística de precio clasifica correctamente SL/TP/sin-match, el monitor rápido cierra en hit y no hace nada sin hit.
- Sanity check contra los dos casos reales que motivaron esta investigación: con la heurística y tolerancia final (1.5%), `ESPUSDT`/`XPLUSDT` se habrían clasificado como `take_profit_binance`/`stop_loss_binance` en vez de `binance_unidentified_close`.
- `pytest tests/ -q -m "not slow and not integration"`: **456 passed** (448 + 8 nuevos), mismas 3 fallas preexistentes de `test_config_governance.py` ya documentadas como independientes en s106.
- Restart de `crypto-agent-app` aplicado (`2026-08-01T00:38:35Z`), arranque limpio sin tracebacks ni errores nuevos en logs. Las alertas de operador activas al momento del restart (`binance_truth_critical`, `health_critical`, `loss_focus_zrousdt_short`) son previas al restart y no relacionadas con este cambio.
- **Pendiente**: no hubo ningún cierre real de futuros en la ventana inmediatamente posterior al restart para confirmar en producción que el monitor rápido/margen preservado reducen `binance_unidentified_close`. Re-medir la distribución de `close_reason` en futuros (7 días) una vez transcurrida una ventana representativa, y comparar contra el baseline de esta entrada.

### Rollback

Revertir `futures_client.py` (`update_sl_tp`, `_infer_close_reason_from_price`, `_classify_close_reason`), `hard_risk_monitor.py` (`monitor_futures_hard_stops_once`) y la línea añadida en `orchestrator.py::_hard_risk_monitor_loop`. Cambio aditivo — no se modificó ningún umbral de SL/TP software (`sl_pct`/`tp_pct`) ni la lógica de trailing/SSO/PRS/UG existente, solo el precio al que se coloca la orden real en Binance y la clasificación posterior del motivo de cierre.

## Actualización s105h — `binance_unidentified_close`: condición de carrera duplicaba cierres tras s105g (2026-07-31)

### Causa raíz

- El usuario pidió revisar `binance_unidentified_close`: 2ª peor categoría de pérdidas en 7 días (`trades_closed`, 18 cierres, -266.62 USDT, 5.6% WR, avg -14.81/cierre).
- Al analizar la muestra se encontró un bug independiente del problema de atribución: **dos pares de filas duplicadas** en `trades_closed` (mismo symbol/side/entry_time/exit_price/pnl, separadas por ~1 segundo) — `RIFUSDT`/`DOGEUSDT` el mismo día del despliegue de s105g. El bug se reprodujo en vivo durante el diagnóstico (`CRVUSDT`, ids 1321/1322, duplicado real detectado en logs de producción mientras se investigaba).
- Root cause: desde s105g, `binance_event_stream.py` ejecuta `_on_futures_message()` → `FuturesClient.handle_order_fill_event()` en el **hilo propio** de `ThreadedWebsocketManager`, en paralelo al ciclo de polling (`check_positions()` Ghost-L1/L2) y al manejo de `-2022`/`ReduceOnly` en `close_long()`/`close_short()`. Los tres caminos comparten el mismo patrón "leer `pos.closed` → reconciliar vía red (`_get_realized_pnl_from_trades`) → `_save_futures_closed()` → marcar `pos.closed=True`" **sin ningún lock** (`futures_client.py` no tenía un solo `threading.Lock`/`RLock`). Entre la lectura de `pos.closed` y las llamadas HTTP bloqueantes de en medio, dos hilos podían pasar la guarda antes de que cualquiera terminara, y ambos `INSERT` en `trades_closed` prosperaban (sin `ON CONFLICT` ni constraint único).
- Impacto medido: 3 pares duplicados totales (2 históricos + 1 en vivo durante el diagnóstico) en la ventana analizada. Las filas duplicadas ya tenían `learning_eligible=false` (no contaminan el pipeline de ML), pero sí inflaban `pnl_by_close_reason`/dashboard con pérdidas fantasma (-32.44 USDT extra en 7 días).

### Cambio aplicado

- `futures_client.py`: nuevo `self._close_lock = threading.RLock()` en `__init__` que serializa el ciclo completo "detectar cierre → reconciliar → persistir → marcar `pos.closed`" en los 4 puntos de entrada: `handle_order_fill_event()` (event stream), el bloque `FIX-T4-L1`/`FIX-T4-L2` de `check_positions()` (Ghost cleanup), y el handler `-2022`/`ReduceOnly` de `close_long()`/`close_short()`. Un lock global (no por símbolo) porque los cierres son eventos poco frecuentes por ciclo — el costo de latencia es despreciable frente a la seguridad de no reordenar nada.
- `_save_futures_closed()`: defensa en profundidad adicional a nivel DB — antes de insertar un cierre completo (no aplica a parciales), consulta si ya existe una fila con mismo `symbol`+`side`+`entry_time` cerrada en los últimos 5 minutos; si existe, salta el insert y audita `close_duplicate_skipped` en vez de duplicar. Cubre cualquier ruta futura que no pase por `_close_lock`.
- Corrección histórica: los 3 pares duplicados encontrados (ids 1309/1310 dobles de 1307/1308, e id 1322 doble de 1321) se marcaron `is_corrupted=TRUE, corrupt=TRUE` con `metadata.duplicate_of`/`duplicate_marked_at`/`duplicate_reason='s105h_event_stream_polling_race'` — mismo mecanismo que ya usa `app.py` para excluir cierres no-estratégicos de stats/aprendizaje. No se borraron filas (se preserva rastro de auditoría, igual que el criterio de s105f).

### Validación

- `pytest tests/test_futures_manual_close_parity.py tests/test_futures_ghost.py tests/test_binance_event_stream.py -o addopts=""`: 36/36 pasan.
- `pytest tests/ -o addopts="" -k "futures or event_stream or ghost"`: 88/88 pasan.
- `pytest tests/ -o addopts=""`: 443 passed, 3 failed — las 3 fallas (`test_config_governance.py`) son preexistentes y no relacionadas (drift/audit de config, sin tocar `futures_client.py`).
- Desplegado en producción (`docker compose restart app`, 2026-07-31 17:19 UTC): `/health` post-restart con `event_stream_connected=true`, `consecutive_cycle_failures=0`, `last_cycle_error=null`; el ciclo de trading abrió/gestionó posiciones normalmente inmediatamente después (XLMUSDT open, trailing stops en EULUSDT/VIRTUALUSDT/AVAXUSDT). Cero cierres duplicados nuevos en la ventana observada post-restart (pendiente de confirmación sostenida en 48-72h de operación real).

### Respuesta a la norma de 6 puntos (`.windsurf/rules.md`)

1. **SPOT/FUTURES:** solo FUTURES — el event stream futuros y `handle_order_fill_event` son los únicos con SL/TP algo-orders reconciliables; el stream SPOT es de solo auditoría.
2. **Perfiles A/B/C:** sin impacto — bug de concurrencia en persistencia, no de sizing/entrada.
3. **Gate/rollback:** gate = 0 pares duplicados nuevos en `trades_closed` en 48-72h. Rollback si aparece cualquier regresión: `PATCH /config/kelly {"binance_event_stream_enabled": false}` (vuelve a operar 100% por polling — con un solo camino activo, la duplicación deja de ser posible incluso sin el lock) o revertir el commit + `docker compose restart app`.
4. **Persistencia/auditoría:** nuevo evento `close_duplicate_skipped` en `trades_audit` cuando el guard de DB detecta un duplicado. Sin cambios de esquema (reutiliza `is_corrupted`/`corrupt`/`metadata`).
5. **Baseline/changepoint:** baseline = 3 pares duplicados detectados en la ventana de diagnóstico desde que existe el event stream (2026-07-31). Changepoint esperado: 0 duplicados nuevos sostenido.
6. **Dashboard:** `pnl_by_close_reason` deja de contar dos veces el mismo cierre; los duplicados históricos ya no aparecen en vistas que filtren `corrupt=false`.

### Alcance no cubierto en esta ronda

- La atribución real de los ~16 cierres restantes de `binance_unidentified_close` (no duplicados) sigue pendiente: son cierres `MARKET` cuyo `clientOrderId` no decodifica a un motivo `x-<reason>-<uuid>` (probablemente algo-orders o `MARKET` colocadas antes del despliegue de s105g, sin tag). Requiere una sesión de diagnóstico dedicada con más muestra una vez que el tráfico post-s105g/s105h sea mayoritario, siguiendo el mismo patrón que s105e/f/g.

---

## Actualización s105g — Binance como límite del ciclo de vida: client-order-ids estructurados + User Data Stream en tiempo real (2026-07-31)

### Motivación (pedido explícito del usuario)

El usuario reportó que "nos ha pasado muchas veces que tenemos posiciones abiertas en nuestro sistema que en Binance no existen" y pidió que el ciclo de vida de una posición **anticipe** el comportamiento de Binance en vez de descubrirlo después del hecho, manteniendo a Binance como fuente de verdad final pero con el control operativo de nuestro lado.

### Diagnóstico

- El ciclo principal corre cada **5 minutos** (`orchestrator.py::_trade_cycle_interval_minutes`). Las órdenes SL/TP de futuros son algo-orders nativas de Binance (`STOP_MARKET`/`TAKE_PROFIT_MARKET`) que Binance ejecuta de forma **autónoma**, sin avisar a nuestro proceso — entre que Binance cierra y que el siguiente ciclo lo nota, hay hasta 5 min de estado desincronizado ("ghost").
- Cero uso de websockets/`listenKey`/user data stream en todo el repo — toda la verdad viene de **polling REST**.
- La detección de "ghost" estaba duplicada en 5 lugares con lógica ligeramente distinta (`_sync_open_positions` FIX-S4, `check_positions()` FIX-T4-L1/L2, el manejo de `-2022` en `close_long`/`close_short`, `sync_manual_closes_from_audit`) — de ahí el bug s105f (`manual_close` mal etiquetado): la adivinanza existe porque nunca capturábamos el evento real en el momento en que ocurrió.
- Ninguna orden que el bot coloca (entrada, SL, TP, cierres) llevaba `newClientOrderId` — Binance no tenía forma de saber "por qué" hicimos algo; toda la atribución dependía de heurísticas post-hoc.

### Cambios aplicados

**Fase 1 — Client Order IDs estructurados** (`futures_client.py`):
- Nuevos helpers `_gen_client_order_id(kind, reason=None)` / `_slugify_reason()` / `_decode_close_reason_from_client_order_id()`. Esquema: `e-<uuid8>` (entradas), `sl-<uuid8>` / `tp-<uuid8>` (algo orders), `x-<reason_slug>-<uuid8>` (cierres/cierres parciales iniciados por el bot).
- `newClientOrderId` agregado a **todas** las órdenes que `futures_client.py` envía a Binance: entradas (`open_long`/`open_short`), SL/TP algo orders (apertura y `update_sl_tp`), cierres totales (`close_long`/`close_short`) y parciales (SSO, Unified Governance), y los cierres de limpieza por aborto de entrada.
- `_classify_close_reason()`: nuevo paso 0 (antes de la heurística de tipo de orden) que lee el `clientOrderId` real de Binance y, si es uno de los nuestros, usa el motivo tal cual — sin adivinar. El audit de cierre manual (paso 1) y el mapeo por tipo de orden (paso 2, s105f) se mantienen como defensa en profundidad para órdenes previas a este cambio.
- Como `manual_position_closer.py` también pasa por `close_long`/`close_short`, los cierres genuinamente manuales **también** quedan etiquetados automáticamente (`x-manual_close-...`) sin depender del match de auditoría por ventana de tiempo.

**Fase 2 — Binance User Data Stream en tiempo real** (`binance_event_stream.py`, nuevo módulo):
- Usa `python-binance` (ya en `requirements.txt`) `ThreadedWebsocketManager` — maneja creación/keepalive de `listenKey` internamente, sin necesidad de implementarlo a mano ni agregar dependencias.
- Futuros: escucha `ORDER_TRADE_UPDATE`; cuando `X=FILLED` y `rp` (realized profit) ≠ 0, llama a `FuturesClient.handle_order_fill_event()` — nuevo método que reconcilia el cierre **en segundos** en vez de esperar al próximo ciclo, reutilizando `_get_realized_pnl_from_trades`/`_classify_close_reason` ya probados (el stream reduce la *latencia* de detección, no duplica la lógica de cálculo de PnL).
- Spot: escucha `executionReport`; cada fill real se audita de inmediato (`event_stream_spot_execution`) para darle a `binance_truth_guard.evaluate_spot_parity()` un rastro en el mismo segundo. SPOT no tiene SL/TP en Binance (es 100% local), así que aquí el valor es visibilidad temprana, no mutación de estado autónoma — se mantiene deliberadamente conservador para no ampliar el radio de riesgo de este cambio.
- Idempotente: si la posición ya está cerrada (carrera ganada por el polling existente), el evento es un no-op silencioso. Ningún callback puede tumbar el hilo del socket — cualquier excepción se atrapa y registra.
- `orchestrator.py`: `_start_event_stream()`/`_stop_event_stream()` con el mismo patrón que `_start_hard_risk_monitor()`; se arranca junto con el trade cycle y se detiene en `stop_all_cycles()`. Nuevos campos de salud en `get_health_status()`/`/health`: `event_stream_running`, `event_stream_connected`, `event_stream_last_event_at`, `event_stream_last_futures_event_at`, `event_stream_last_spot_event_at`, `event_stream_last_error`.
- Nuevos flags en `config.py`/`config_helpers.py`/`app.py` `_KELLY_EDITABLE_FIELDS`: `binance_event_stream_enabled` (default `True`), `binance_event_stream_futures_enabled`, `binance_event_stream_spot_enabled`.
- **El polling cada 5 min y las 5 rutinas de reconciliación existentes (FIX-S4, FIX-T4-L1/L2, `-2022` handler, `sync_manual_closes_from_audit`) no se tocan** — siguen siendo la red de seguridad para cuando el stream se desconecta. Sin credenciales reales o en `dry_run`, el stream simplemente no arranca y el sistema opera exactamente igual que antes.

### Alcance no cubierto en esta ronda (hoja de ruta)

- **Fase 3 (consolidación):** reemplazar las 5 rutinas de reconciliación dispersas por una única máquina de estados (`position_lifecycle.py`) alimentada primero por el stream y en su defecto por polling. Requiere tocar código central con mucha superficie de tests — se deja para una sesión dedicada.
- **Fase 4 (idempotencia de intención):** persistir un `close_intent` *antes* de emitir cualquier orden de cierre, para que carreras entre rutas internas nunca produzcan ambigüedad ni reintentos duplicados.
- Client-order-id estructurado para **SPOT** (`run_trades.py` tiene decenas de sitios que colocan órdenes de venta por distintos motivos) — se dejó fuera para no ampliar el radio de riesgo; el stream SPOT de esta ronda es solo de auditoría/visibilidad.
- ~~Actualizar `python-binance`~~ — hecho en esta misma sesión (`1.0.16` → `1.0.37`, con `websockets` pineado a `16.1.1`), ver "Verificado en producción" abajo.

### Validación

- `pytest tests/test_futures_manual_close_parity.py tests/test_futures_ghost.py tests/test_binance_event_stream.py tests/test_config_governance.py tests/ -q`: sin fallos.
- `tmp_audit_config.py` (vía `test_config_governance.py`): 0 mismatches críticos (se corrigieron dos fallbacks `getattr` que no coincidían con el default canónico: `binance_use_testnet` y `binance_event_stream_spot_enabled`).
- **Verificado en producción (2026-07-31, real, con credenciales reales):**
  - **Incompatibilidad de dependencia confirmada Y corregida de raíz:** `python-binance==1.0.16` (pineado en `requirements.txt`) usaba un endpoint de `listenKey` de spot obsoleto (`/api/v1/userDataStream` → `410 Gone`) y su capa de websockets no era compatible con `websockets>=16`, causando además que `stop()` pudiera colgarse indefinidamente (`AttributeError` en el teardown async). **Fix: actualizado `python-binance` a `1.0.37`** (`requirements.txt`) — resuelve ambos problemas en la librería misma, no son workarounds. Verificado: spot ya no da `410 Gone`, `stop()` completa en ~3s.
  - **`websockets` pineado explícitamente a `16.1.1`:** durante la verificación, `websockets==17.0.1` (resuelto automáticamente en un rebuild sin pin) rompió la inicialización del socket manager de `python-binance` 1.0.37 (`"Binance Socket Manager failed to initialize after 5 seconds"`) — regresión de una librería de rápido movimiento. Se fijó `websockets==16.1.1` en `requirements.txt` (versión verificada funcionando) para evitar que un futuro rebuild sin pin reintroduzca el problema.
  - **Contención de arranque descubierta y corregida:** incluso con las versiones correctas, `_start_event_stream()` fallaba consistentemente (mismo error de timeout de 5s) cuando se invocaba sincrónicamente dentro de `start_trade_cycle()` en el arranque de la app, compitiendo por el GIL con el trabajo síncrono pesado del primer ciclo (sync de posiciones de futuros, detección de régimen, llamadas de red). En una prueba aislada (`docker exec` sin esa contención) conectaba sin problema. Fix: `orchestrator._delayed_start_event_stream()` — el arranque del stream ahora ocurre en un hilo separado con un delay de 15s, después de que el arranque pesado inicial se asiente.
  - **Bug de truthiness corregido:** `start_futures_user_socket()`/`start_user_socket()` devuelven un identificador de conexión que puede ser un string "falsy" incluso en éxito — el estado `running`/`connected` ahora se determina con flags explícitos seteados solo si la llamada no lanzó excepción, nunca por `bool(valor_de_retorno)`.
  - **Visibilidad:** los mensajes de `binance_event_stream.py`/`orchestrator.py::_start_event_stream` usan `print()` en vez de `logging` — se confirmó que el logger de `orchestrator.py` no llega a `docker logs` en este proyecto (comportamiento preexistente, no introducido por este cambio), mientras que `print()` sí.
  - Se confirmó que `orchestrator.pause()`/`orchestrator.stop()` (los endpoints `/control/pause`/`/control/stop` que usa el operador) **no invocan `stop_all_cycles()`** — solo cambian flags de estado; los hilos daemon (incluido el del event stream) siguen vivos hasta que el proceso completo termina. El timeout en `stop()` (ya no necesario para el bug original, corregido en la librería) se mantiene como salvaguarda defensiva.
  - **Resultado final confirmado en `/health` tras el despliegue completo (rebuild + recreate):** `event_stream_running=true`, `event_stream_connected=true`, `event_stream_last_error=null`, **`event_stream_last_futures_event_at` poblado** (evento real recibido de Binance vía websocket), sostenido 60s+ sin errores ni spam de reconexión; spot conectado también, pendiente su primer evento real. `consecutive_cycle_failures=0` en todo momento — el trading nunca se vio afectado durante las pruebas.
- Pendiente: monitorear ausencia de errores las próximas 24-48h; confirmar que `event_stream_last_spot_event_at` se puebla en el primer fill spot real.

### Respuesta a la norma de 6 puntos (`.windsurf/rules.md`)

1. **SPOT/FUTURES:** Fase 1 (client-order-id) es solo FUTURES. Fase 2 (stream) cubre ambos, pero con profundidad distinta: FUTURES reconcilia estado automáticamente, SPOT solo audita en tiempo real.
2. **Perfiles A/B/C:** sin impacto — ninguno de los cambios depende de perfil.
3. **Gate/rollback:** gate = `event_stream_connected=true` sostenido en producción sin incremento de `event_stream_last_error`; el `close_reason` de nuevos cierres automatizados debe coincidir con el `clientOrderId` tageado. Rollback: `PATCH /config/kelly {"binance_event_stream_enabled": false}` (el sistema vuelve a operar 100% por polling, sin tocar `futures_client.py`) o revertir el commit completo + `docker compose restart app`.
4. **Persistencia/auditoría:** nuevo evento `event_stream_close_detected` (futuros, `binance_confirmed=true`) y `event_stream_spot_execution` (spot) en `trades_audit`. Sin cambios de esquema.
5. **Baseline/changepoint:** baseline = latencia de detección de cierre actual (hasta 5 min, ventana del ciclo). Changepoint esperado: la mayoría de los cierres automatizados deberían quedar registrados en segundos vía `event_stream_close_detected` en vez de en el siguiente ciclo de polling.
6. **Dashboard/endpoint:** `/health` expone el estado del stream; recomendable agregar un indicador visual en el dashboard operativo en una iteración futura.

---

## Actualización s105f — `close_reason='manual_close'` en cierres 100% automatizados de futuros (2026-07-31)

### Causa raíz

- El usuario reportó cierres `manual_close` en FUTURES sin haber cerrado nada manualmente. Verificado con datos reales: de 17 cierres `manual_close` en `trades_closed`, **6 eran manuales genuinos** (SPOT, vía dashboard, jul-27/29, con audit `manual_position_close_start`/`manual_position_close_binance_filled`) y **10 eran cierres 100% automatizados de FUTURES mal etiquetados** — todos precedidos 1-15s antes por un audit `BINANCE_MISMATCH_GHOST_RUNTIME` (`binance_truth_guard.py`), es decir, la posición ya no existía en Binance cuando el ciclo la revisó.
- Root cause en `futures_client.py::_classify_close_reason()`: cuando no hay match de auditoría de cierre manual (`manual_position_futures_close_done`), la función consulta el tipo de orden real en Binance y trata `type=MARKET` como evidencia de cierre manual. **Pero `MARKET` es el tipo de orden que usa absolutamente todo cierre automatizado del bot** (`close_long()`/`close_short()` en sí — línea ~2574/2696 `"type": "MARKET"` —, los cierres parciales de `unified_governance_supervisor` dentro de `check_positions()` — línea ~3924 —, y los callers `regime_change`, `regime_change_to_short`, `regime_futures_short`, `basket_reset_auto` en `run_trades.py`). Solo las órdenes algo nativas de Binance (`STOP_MARKET`/`TAKE_PROFIT_MARKET`/`TRAILING_STOP_MARKET`) son evidencia inequívoca; `MARKET` es ambiguo y el código lo resolvía siempre a favor de "manual" sin evidencia real.
- Esta función de clasificación sólo se invoca desde la ruta de **reconciliación** (`_get_realized_pnl_from_trades`), que se dispara en: (a) el manejo de error `-2022`/`ReduceOnly` en `close_long()`/`close_short()` cuando Binance ya cerró la posición antes que nuestro intento, y (b) la detección de "ghost" (FIX-T4, `Ghost-L1`/`Ghost-L2`) dentro de `check_positions()`, que corre **cada ciclo** (no solo al reiniciar) y reconcilia posiciones que desaparecieron de Binance sin pasar por nuestro flujo normal de cierre. En ambos casos se descartaba el motivo real (el `reason` que el caller original tenía, o simplemente "no lo sabemos") y se sustituía por `"manual_close"` sin evidencia.

### Cambio aplicado

- `futures_client.py::_classify_close_reason()` y `_get_realized_pnl_from_trades()`: nuevo parámetro `fallback_reason`. Cuando no hay match de audit manual y la orden es `MARKET` (o se cae al heurístico de signo), ya no se asume `"manual_close"`:
  - Si el caller conocía su intención real (p. ej. `close_long()`/`close_short()` en el manejo de `-2022`, que ahora pasa su propio `reason` como `fallback_reason`) → se preserva ese motivo real.
  - Si no hay contexto (ghost cleanup en `_sync_open_positions()`, `Ghost-L1`/`Ghost-L2` en `check_positions()`) → se usa la etiqueta neutral explícita `"binance_unidentified_close"`, nunca `"manual_close"` sin evidencia.
- `frontend/dashboard.js`: agregado `binance_unidentified_close` a `_CLOSE_REASON_MAP` (❓ Cierre no identificado).
- Tests nuevos en `tests/test_futures_manual_close_parity.py`: MARKET sin audit → `binance_unidentified_close` (sin fallback) o el `fallback_reason` (con); heurístico de signo también respeta `fallback_reason`; regresión específica del caso real (`close_short` pierde la carrera contra Binance, debe conservar el motivo original, nunca `manual_close`).

### Corrección histórica (aprobada por el usuario)

- Se identificaron los 10 cierres de FUTURES mal etiquetados (`ids 1236,1237,1245,1253,1254,1260,1270,1277,1289,1305` — SYNUSDT, EULUSDT, LINKUSDT, UNIUSDT, ZAMAUSDT, KAITOUSDT, ESPUSDT×3, MMTUSDT) mediante el criterio verificable: `close_reason='manual_close'` AND sin match de `trades_audit.event='manual_position_futures_close_done'` en una ventana de ±25min/2min alrededor de `exit_time`.
- Se reclasificaron a `close_reason='binance_unidentified_close'` preservando PnL/precios/comisiones intactos, con `metadata.close_reason_corrected_from='manual_close'` y `close_reason_corrected_at` para trazabilidad del propio fix.
- Los 6 cierres SPOT genuinamente manuales (WLDUSDT, ZAMAUSDT, BTCUSDT, LTCUSDT, ENAUSDT, PENGUUSDT, DIAUSDT) **no se tocaron** — audit real confirmado.

### Validación

- `pytest tests/test_futures_manual_close_parity.py tests/test_futures_ghost.py tests/ -q`: sin fallos.
- Query de verificación (repetible): `SELECT close_reason, COUNT(*) FROM trades_closed tc WHERE close_reason='manual_close' AND NOT EXISTS (SELECT 1 FROM trades_audit ta WHERE ta.symbol=tc.symbol AND ta.event='manual_position_futures_close_done' AND ta.ts BETWEEN tc.exit_time - INTERVAL '25 minutes' AND tc.exit_time + INTERVAL '2 minutes') GROUP BY close_reason;` — debe tender a 0 tras el fix.

### Respuesta a la norma de 6 puntos (`.windsurf/rules.md`)

1. **SPOT/FUTURES:** solo aplica a FUTURES (la reconciliación de `_classify_close_reason` sólo se usa en `futures_client.py`); SPOT no se toca.
2. **Perfiles A/B/C:** sin impacto — la clasificación no depende de perfil.
3. **Gate/rollback:** gate = 0 nuevos `manual_close` sin match de audit real en 48-72h. Rollback: revertir el fallback_reason (dejar que `_classify_close_reason` vuelva a inferir `manual_close` por MARKET) y `docker compose restart app`.
4. **Persistencia/auditoría:** sin cambios de esquema; se preserva PnL/precios en la corrección histórica, solo se corrige `close_reason` + metadata de trazabilidad.
5. **Baseline/changepoint:** baseline = 10/17 cierres `manual_close` de 7 días eran falsos positivos (58.8%). Changepoint: la proporción de `manual_close` sin audit real debe caer a ~0.
6. **Dashboard:** `pnl_by_close_reason` y la tabla de cierres ahora deberían mostrar `binance_unidentified_close` (❓) en vez de `manual_close` para estos casos.

---

## Actualización s105e — `close_reason='regime_change'` era un stop de tiempo mal calibrado, 0% WR en 7 días (2026-07-31)

### Causa raíz

- El usuario reportó sospecha de que el cierre tipo `regime_change` (futuros SHORT) sólo generaba pérdidas en los últimos días. Verificado con datos reales (`trades_closed`, 7 días): **34 cierres, 0 ganadores, 34 perdedores, -275.50 USDT, 0% WR**, el peor `close_reason` de la ventana (peor que `hard_risk_stop` -231.39 y `stop_loss_binance` -110.96). Sólo el 2026-07-31 generó -204.62 USDT en 21 cierres.
- Root cause en `run_trades.py` (bloque `# A3: Auto-close ALL shorts...`, línea ~10935): el guardia anti-whipsaw (`_non_short_regime_cycles`, introducido N35 jun-18) incrementa un contador **global** cada ciclo en que `_regime_mode not in ("PRESERVATION", "FUTURES_SHORT")`, y fuerza el cierre de **todos** los SHORT abiertos cuando el contador alcanza `min_cycles_before_close_shorts` (default 5), salvo que ya estén en ganancia por encima de `futures_protect_profit_usdt` (-5 USD).
- Este guardia se diseñó cuando los SHORT sólo se abrían en régimen `FUTURES_SHORT`, así que "régimen ≠ FUTURES_SHORT/PRESERVATION" equivalía correctamente a "el régimen cambió y ya no toca tener shorts". Desde s99e (`range_scalp_short_preferred`), **`RANGE_SCALP` abre y sostiene sus propios SHORT como estrategia legítima**, pero nadie agregó `RANGE_SCALP` a la tupla de regímenes "short-friendly". Como el sistema opera en `RANGE_SCALP` de forma prácticamente continua (`regime_confidence.mode='RANGE_SCALP'` sostenido, sin transición real registrada en `regime_transition_state`), el contador **nunca se resetea** y termina cerrando cualquier SHORT de `RANGE_SCALP` a los ~5 ciclos de vida (2–90 min observados), **sin que haya ocurrido ningún cambio de régimen real**. Confirmado con `metadata.smart_meta.regime_at_entry='RANGE_SCALP'` y `metadata.smart_meta.unified_governance_loss_state.regime_applied='RANGE_SCALP'` en los cierres `regime_change` más recientes — el régimen al abrir y al cerrar es el mismo.
- Efecto: un stop de tiempo ciego (no depende de estructura, SL/TP, ni señal de reversión) etiquetado engañosamente como "cambio de régimen", que corta posiciones apenas entran en pérdida leve (avg -0.13% a -0.5%) antes de que puedan revertir, con 0% de tasa de acierto histórica en la ventana analizada.

### Cambio aplicado

- `run_trades.py`: se agregó `RANGE_SCALP` al conjunto de regímenes "short-friendly" (`_short_friendly_regimes = ("PRESERVATION", "FUTURES_SHORT", "RANGE_SCALP")`) usado en el contador `_non_short_regime_cycles` y en el gate de auto-cierre por `regime_change` (dos sitios) y en el mensaje informativo de pre-switch. Efecto: el auto-cierre por `regime_change` sólo se activa ante una transición real hacia un régimen que no admite SHORT (p. ej. `SPOT_BULL`), y deja de disparar contra los SHORT nativos de `RANGE_SCALP` mientras el régimen no cambie de verdad.
- No se tocó `futures_protect_profit_usdt` ni `min_cycles_before_close_shorts`: siguen protegiendo shorts ganadores/cerrando en una transición real a `SPOT_BULL`.
- No se escribió un test unitario aislado: la lógica vive inline dentro del monolito `execute_once()` (9000+ líneas) sin punto de extracción sencillo; se validó con la suite completa (`pytest tests/ -q`, sin regresiones) y con evidencia directa de producción (query SQL de 7 días antes/después del fix).

### Validación

- `pytest tests/ -q`: sin fallos (mismo resultado que baseline pre-cambio).
- Pendiente: confirmar en producción tras reiniciar `crypto-agent-app` que ya no aparezcan cierres `regime_change` con `regime_at_entry == regime_at_close == 'RANGE_SCALP'`; sólo deberían seguir ocurriendo ante una transición real de régimen (`regime_transition_state.active=true` con `to_mode` fuera de `PRESERVATION/FUTURES_SHORT/RANGE_SCALP`).

### Respuesta a la norma de 6 puntos (`.windsurf/rules.md`)

1. **SPOT/FUTURES:** sólo aplica a FUTURES (el guardia sólo opera sobre `_open_short_keys`, futuros). SPOT no se toca.
2. **Perfiles A/B/C:** sin impacto — el guardia no distingue perfil, sólo régimen y PnL no realizado.
3. **Gate de aceptación / rollback:** gate = 0 cierres `regime_change` con `regime_at_entry='RANGE_SCALP'` sin transición real en las siguientes 48-72h. Rollback: revertir el commit (quitar `RANGE_SCALP` de `_short_friendly_regimes`) y `docker compose restart app`.
4. **Persistencia/auditoría:** no se modificó el esquema de `trades_closed`/`trades_audit`; los cierres reales por transición a `SPOT_BULL` se seguirán registrando igual como `regime_change`.
5. **Baseline/changepoint:** baseline = 34 cierres/0% WR/-275.50 USDT en 7 días previos (query documentada arriba). Changepoint a monitorear: conteo de `regime_change` con `regime_at_entry==regime_at_close` debe caer a ~0 tras el fix.
6. **Dashboard/endpoint:** `GET /dashboard/summary` → `pnl_by_close_reason` (ya expone `regime_change`); verificar que su frecuencia y PnL neto mejoren en los próximos días.

---

## Actualización s105d — Ghost cleanup de futuros borraba PnL real (ganador y perdedor) sin registrar el cierre (2026-07-31)

### Causa raíz (caso real: ALLOUSDT +$74.11, LAUSDT -$9.53)

- Investigando un reporte de "ALLOUSDT marcaba >100 de PnL no real y de pronto desapareció" del dashboard, se confirmó con el historial real de Binance (`FuturesClient._get_realized_pnl_from_trades`, `/fapi/v1/userTrades`) que la posición **sí se cerró con una ganancia real neta de +$74.11** (bruto +$77.65, comisión -$3.54, exit≈0.27324). Una posición hermana, `LAUSDT`, cerrada en la misma ventana, tuvo una **pérdida real neta de -$9.53**.
- Ninguna de las dos quedó jamás en `trades_closed`: el mecanismo **"Ghost cleanup" (FIX-S4)** en `futures_client.py._sync_open_positions()` detecta filas de `trades_open` sin posición correspondiente en Binance y las **borra directamente** (`DELETE FROM trades_open`) sin reconciliar el resultado real. Esto ocurre cuando una posición se cierra por una vía que no pasa por el flujo normal de cierre (`close_position`/`_save_futures_closed`) — p. ej. una orden MARKET clasificada como `"manual_close"` por `_classify_close_reason`.
- Impacto: pérdida silenciosa de trazabilidad de PnL real (ganador o perdedor) cada vez que se dispara esta ruta — las estadísticas, `daily_pnl`, y el dashboard nunca se enteran de esos cierres, aunque el dinero sí se movió en la cuenta real.

### Cambio aplicado

- `futures_client.py`, bloque FIX-S4 (dentro de `_sync_open_positions`): antes de borrar una fila fantasma de `trades_open`, se llama a `_get_realized_pnl_from_trades(symbol, side, opened_at=...)` (método ya existente, usado por la recuperación normal de SL/TP perdidos) para reconstruir el cierre real desde Binance. Si se obtiene un `exit_price` válido, se inserta un registro completo en `trades_closed` (entry/exit price real, PnL neto real, `close_reason` real, comisiones, `pnl_prediction`/`indicators_at_entry` heredados de la fila original) con `metadata.ghost_reconciled=true` y `learning_eligible=False` (excluido de entrenamiento por ser una reconciliación reactiva, conforme a las reglas de exclusión de `calibrar_datos.md`). Se audita el evento (`ghost_reconciled_close`, `binance_confirmed=true`). Si Binance no devuelve historial reconciliable, se preserva el comportamiento anterior (sólo `DELETE`, sin insertar nada falso).
- Tests: `tests/test_futures_ghost.py::test_ghost_cleanup_reconciles_real_close_before_deleting` y `test_ghost_cleanup_deletes_without_reconciling_when_no_binance_history`.

### Validación

- `pytest tests/test_futures_ghost.py tests/test_futures_manual_close_parity.py tests/test_close_persistence_hardening.py tests/slow/test_futures_smart_exits.py tests/ -q`: todo passed.
- Desplegado en producción (reinicio de `crypto-agent-app`); pendiente confirmar en las próximas horas que un ghost-cleanup real efectivamente inserte en `trades_closed` (`metadata->>'ghost_reconciled'='true'`).

### Nota de auditoría de esta sesión

- El diagnóstico requirió instanciar `FuturesClient` en el contenedor para consultar el historial real de Binance; su constructor ejecuta la misma sincronización/ghost-cleanup como efecto secundario, por lo que las filas ya-fantasma de `ALLOUSDT` (ids 1394, 1395) y `LAUSDT` (id 1390) se borraron en ese momento — **antes** de que este fix estuviera desplegado, por lo que esos dos cierres reales quedaron sin reconciliar. Ningún registro de `trades_open` se cerró/canceló por esta investigación; ambas posiciones ya estaban cerradas en Binance cuando se consultaron.

---

## Actualización s93b — Fix RANGE_SCALP + trigger SR en salida (2026-07-24)

### Fix de aperturas RANGE_SCALP

- `staged_entry.build_range_reversion_prediction` esperaba Bollinger Bands en `bollinger_bands.{lower,middle,upper}`, pero `analyze_signal` las expone como `lower_band`, `middle_band`, `upper_band`. Eso hacía que la predicción de reversión fuera inválida (`missing_bollinger_bands`) y `entry_decision` cayera al predictor LONG genérico (comportamiento SPOT_BULL), que no genera `target_price`/`stop_price`, por lo que `staged_entry` descartaba con `DIRECTION_TARGET_MISSING`.
- `pnl_predictor.predict_range_reversion` ponía el stop estructural exactamente en `lower`. Cuando el precio tocaba o rompía ligeramente la banda inferior (`band_position ≈ 0`), el stop quedaba en o por encima del precio, invalidando el payoff (`invalid_reversion_payoff`) y terminando en `DIRECTION_TARGET_MISSING`.
- Se corrigieron ambos problemas: lectura plana/anidada de BB y selección del stop más alto entre el estructural (`lower * (1 - configured_sl/1000)`) y el configurado (`price * (1 - configured_sl)`) que esté estrictamente por debajo del precio actual, coherente con el criterio canónico de comprar cerca del soporte y no forzar trades en el techo del rango.
- Nuevo cuello de botella detectado: `_normalize_autonomous_signal` en `run_trades.py` no reenviaba las features que el modelo universal espera (`range_position`, `rsi_1h`, `rsi_4h`, `trend_1h`, `trend_4h`). `_extract_universal_features` caía en proxies débiles, bajando el score de calidad y descartando todos los candidatos `RANGE_SCALP` con `PEAK_QUALITY_DISCARDED`. Se añadieron esas keys al dict normalizado y el siguiente ciclo ya staged a `KAITOUSDT`.
- Se re-aplicaron overrides runtime:
  - `category_max_positions_enabled = False`
  - `entry_quality_vol_spike_min_range_scalp = 0.20`

### Integración de Soporte/Resistencia en salida

- `peak_capture_engine.py` ahora incluye el trigger `profit_predexit_sr_resistance` (prioridad 91) entre `regime_fear` (92) y `capture` (85).
- Se dispara cuando la posición está cerca de la resistencia (`sr_zone_position > 0.85` o distancia ≤ ATR%%) y ya hay beneficio.
- Esto añade una razón de cierre adicional al análisis lifecycle; los dashboards y queries deben agrupar también por `profit_predexit_sr_resistance`.

### Monitorización requerida

- Confirmar aperturas validadas `RANGE_SCALP` en las próximas 6-12h.
- Revisar si `sr_entry_shadow_rejected` sombrea señales rentables; considerar pasar a `active` o ajustar umbrales tras 48h de datos.

---

## Actualización s94 — Pipeline SR completo y gates relajados (2026-07-24)

### Inyección de features SR en aperturas y cierres

- `pnl_predictor.py` ajusta probabilidad de éxito y rango de precios usando `sr_zone_position`, `sr_bounce`, `sr_breakout`, `sr_breakdown` y distancias SR.
- `entry_profile_gate.py` consume distancias/posición de zona SR en el modelo universal y bonifica el score heurístico por eventos SR.
- `autonomous_signals.py` extrae, reenvía y genera señales BUY/SELL a partir de rebote de soporte, breakout y breakdown; el ranking por régimen bonifica eventos SR confirmados.
- `staged_entry.py` reduce fricción para señales SR confirmadas: `required_positive_checks=1`, `direction_required_confirmations=1`, `direction_min_progress_bps=1.0`, `max_cycles>=4`. También filtra `get_openable_candidates` por `top_symbols_qubo`.
- `run_trades.py` reenvía SR features y evita el descarte inicial de perfil (`PEAK_QUALITY_DISCARDED`) para eventos SR confirmados.

### Relajación de gates y configuración agresiva

- `entry_decision.py`: `entry_decision_min_probability` default **0.50** para todos los regímenes.
- `config.py` / `config_helpers.py`: `max_positions_spot_bull=12`, `max_positions_range_scalp=8`, `max_positions_preservation=4`, `max_positions_futures_short=8`, `max_total_exposure_pct=0.95`, `max_positions=8`, `max_open_per_cycle=2`, `category_max_positions_enabled=False`.
- `market_regime.py`: `kelly_mult` SPOT_BULL=1.05, RANGE_SCALP=1.0, PRESERVATION=0.7; FUTURES_SHORT base 0.6 con boost hasta 1.0 según confianza.
- `universe_selector.py`: default `screener_max_pairs=100` alineado a config.

### Validación

- Suite completa: **602 passed, 0 failed**.
- `tmp_audit_config.py`: 0 critical mismatches.

### Implicaciones para el lifecycle

- Las aperturas ahora dependen más de eventos SR confirmados; los cierres siguen incluyendo el disparador `profit_predexit_sr_resistance`.
- Los re-chequeos staged serán más permisivos para candidatos con metadatos `support_bounce_buy`, `sr_bounce`, `support_breakout_buy`, `sr_breakout`, `support_breakdown_sell` o `sr_breakdown`.
- El riesgo agregado sube con mayor exposición y Kelly; se debe monitorear drawdown y correlación de cierres.

---

## Actualización s93 — Entrada universal desplegada (2026-07-23)

El `entry_profile_gate` ahora aplica el **modelo universal** (`quality_score_model_universal_proposal.pkl`) con thresholds por régimen + categoría. Esto afecta el ciclo de vida porque:

- El perfil de calidad de entrada (`peak_quality_profile`) y el score (`peak_quality_score`) se generan a partir del modelo universal; `score_source` en `entry_profile_gate_info` debe ser `"universal_model"`.
- La propagación de `peak_quality_profile` al metadata de `trades_open` es crítica para que SSO/PRS/UG escaleen paciencia y riesgo por calidad real de entrada. El runtime ya no consulta un `current_regime_profile_label` global.
- Los cierres reales bajo este gate deben agruparse por `score_source` y `profile_label` para comparar lifecycle (giveback, parciales, SL/TP) contra baseline y contra proyecciones walk-forward/MIMIC.
- Rollback de entrada no altera SL/TP ni lifecycle; solo cambia el conjunto de señales que llegan al pipeline de salida.

### Monitorización requerida

- Fracción de posiciones abiertas con `score_source == "universal_model"`.
- PnL neto, win rate, giveback y razón de parciales por `peak_quality_profile` (A/B/C) y categoría.
- Comparar contra cohortes previas (`score_source != "universal_model"` o trades anteriores).

---

## Actualización s95 — Perfil por operación consolidado (2026-07-24)

El refactor s95 elimina el perfil activo global del runtime. Implicaciones para el ciclo de vida:

- Cada posición lleva su propio `peak_quality_profile` generado por `entry_profile_gate` en el momento de la entrada. Este perfil viaja en el metadata de `trades_open` y es la única fuente para decisiones de sizing, salida y monitorización.
- Los supervisores de salida (PRS, UG, SSO, SmartTP, Peak Capture) deben leer el perfil de la posición, no de `settings.current_regime_profile_label`. Si el metadata carece de `peak_quality_profile`, el fallback canónico es `'B'`.
- Los caps del `universe_selector` son por régimen únicamente; todos los perfiles comparten el mismo universo candidato, y la calidad de entrada decide cuáles entran.
- El dashboard no presenta un "perfil activo global"; muestra "Per operation" y, en el detalle de cada posición, su `peak_quality_profile` real.
- MIMIC/backtest asigna el perfil forzado por operación (`peak_quality_profile`), no un perfil global de simulación.
- Verificación: suite completa `611 passed, 2 skipped, 0 failed` (`pytest -n 4`).

---

## Estado actual del pipeline de salida

Precedencia canónica (mayor → menor autoridad):

1. **Hard SL/TP** — órdenes `algo` activas en Binance; se ejecutan fuera del bucle Python.
2. **Dust / ghost / balance mismatch cleanup** — cierre automático cuando la posición DB no tiene respaldo en Binance o el remanente es invendible.
3. **Breakeven (BE)** — mueve SL al precio de entrada cuando se alcanza el umbral de ganancia.
4. **Trailing stop** — SL sigue al precio favorable a distancia dinámica.
5. **Smart Scale-Out (SSO-1..7)** — 7 triggers adaptativos de salida parcial/total.
6. **Profit Realization Supervisor (PRS)** — timing de salida escalado por régimen/perfil y peak protection.
7. **Unified Governance Supervisor (UG)** — árbitro final que consolida SSO+PRS+PredExit en una sola acción.
8. **Peak Capture / PredExit (Wolf Hunter)** — decide capturar ganancia ahora o esperar un pico mayor.

Flujos separados:

- **SPOT:** `run_trades.py::_check_sl_tp_positions()` itera posiciones, aplica BE/trailing/SSO/PRS/UG/PredExit y ejecuta cierres vía `position_manager`.
- **FUTURES:** `futures_client.py::check_positions()` aplica BE/trailing/SmartTP, luego SSO/PRS/UG/PredExit, y ejecuta cierres parciales/totales.

## Módulos y responsabilidades

| Módulo | Responsabilidad | Límite importante |
|--------|-----------------|-------------------|
| `futures_client.py` | Ciclo FUTURES completo: BE, trailing, SmartTP extend, SL/TP `algo`, ghost/sync, evaluación SSO/PRS/UG, ejecución de cierres. | No detecta régimen; depende de `settings` pasados. |
| `run_trades.py::_check_sl_tp_positions()` | Ciclo SPOT: BE, trailing, SSO/PRS/UG/PredExit, ejecución de cierres. | No maneja FUTURES. |
| `smart_scale_out.py` | 7 triggers de salida: decay, slow crawl, stagnant, momentum, multi-level ATR, target contribution, re-entry upgrade, bounce rescue, stagnation trailing. | Solo advisory; no ejecuta órdenes ni persiste. |
| `profit_realization_supervisor.py` | Timing de salida escalado por régimen/perfil, momentum score, peak protection. | Solo advisory; no ejecuta órdenes. |
| `peak_capture_engine.py` | PredExit: ¿capturar ganancia ahora o seguir corriendo? | Solo en PnL>0; advisory; no overridea hard SL/TP. |
| `unified_governance_supervisor.py` | Árbitro final: ramas profit, loss, fast-cut, peak-drawdown; consolida advisories. | No genera señales de entrada. |
| `position_manager.py` | Persistencia SPOT: cierres total/parcial, reducción de `quantity`, `close_group_id`, metadata `_smart_meta`. | No sabe de FUTURES. |
| `state_persistence.py` | Persistencia restart-safe de cooldowns, mass-SL breaker y contadores volatile. | Solo estado; no decide. |

## Problema reciente: últimas 48h — pérdidas grandes, ganancias mínimas

Hipótesis iniciales a validar con datos:

1. **Trailing / BE no se activan a tiempo:** en `RANGE_SCALP` y `FUTURES_SHORT` el precio oscila y revierte antes de que el trailing proteja ganancias.
2. **SSO / PRS / UG disparan cierres tardíos o nunca:** posiciones ganadoras no se cierran hasta que se convierten en perdedoras.
3. **Peak Capture no captura picos:** hold mínimo muy largo o `prob_higher_peak` sobreestimado; se devuelve ganancia del pico.
4. **SL/TP con R:R desfavorable:** `stop_loss_pct` / `take_profit_pct` actuales generan muchos stops y pocos TPs.
5. **Perfil por operación:** sizing y salidas usan el `peak_quality_profile` de cada posición. Si el metadata de una posición no tiene `peak_quality_profile`, los supervisores fallback a 'B'.
6. **Correlación / mass-SL:** múltiples posiciones cierran en SL simultáneo por alta correlación; la pausa global no es suficiente.
7. **FUTURES_SHORT inactivo o mal calibrado:** ausencia de shorts en caídas deja el portfolio expuesto al lado largo.
8. **Comisiones + slippage en parciales:** cierres parciales frecuentes erosionan PnL neto si no se contabilizan correctamente.

## Métricas y diagnóstico requerido

Recolectar por ventanas de 24h y 48h antes de cualquier cambio:

- Número de cierres por `close_reason`: `stop_loss`, `take_profit`, `sso_*`, `prs_*`, `unified_governance_*`, `profit_predexit_*`, `manual_close`, `ghost_*`, `dust_*`.
- PnL **neto** agrupado por reason, régimen, `peak_quality_profile` de la posición y tipo (SPOT/FUTURES).
- Tiempo medio en trade hasta el cierre, por reason.
- `peak_pnl_pct` vs `pnl_pct` al cierre: % devuelto del pico (`giveback`).
- Tasa de activación de BE y trailing: % de posiciones que llegaron a BE, % que activaron trailing.
- Correlación de cierres: cuántos SL en la misma vela/hora.
- Comparativa SPOT vs FUTURES: trades, PnL, win-rate, drawdown.
- `ready_candidate_id` no nulo: tasa de aperturas validadas vs no, y PnL neto por trayectoria `staged`.

## Tareas de análisis y calibración

### A. Diagnóstico del daño reciente
- [ ] **A1.** Query `trades_closed` últimas 48h: PnL neto por `close_reason`, símbolo, `regime_at_entry`, `entry_profile_label`.
- [ ] **A2.** Query `trades_open` actual: posiciones en pérdida, `peak_pnl_pct`, horas sostenidas, BE/trailing activo, `entry_profile_label`.
- [ ] **A3.** Correlar con `trades_audit`: eventos `unified_governance_decision`, `sso_*`, `profit_predexit_*`, `profit_realization_supervisor_*`.
- [ ] **A4.** Reconstruir para cada posición cerrada: pico máximo alcanzado y % devuelto al cierre.
- [ ] **A5.** Identificar posiciones que pasaron de ganancia a pérdida (crossed from + to -) antes del cierre.

### B. Revisión de parámetros de salida
- [ ] **B1.** Auditar `stop_loss_pct`, `take_profit_pct`, `futures_sl_pct`, `futures_tp_pct` y los valores por categoría/régimen.
- [ ] **B2.** Auditar `trailing_stop_activation_pct`, `trailing_stop_distance_pct`, `futures_trailing_activation_pct`, `futures_trailing_distance_pct` y distancia dinámica por tiers de PnL.
- [ ] **B3.** Auditar `sso_*` gates: decay, slow crawl, stagnant, flat stagnation timeout, stagnation trailing, bounce rescue.
- [ ] **B4.** Auditar `profit_realization_*`: hold mínimo, partial/full minutes, `peak_protection_*` por régimen/perfil.
- [ ] **B5.** Auditar `unified_governance_*`: fast loss threshold, near-SL hold, loss escalation, F&G modifiers.

### C. Ajustes / rediseño canónico propuestos
- [ ] **C1. Regla de oro del SL:** SL nunca retrocede. Documentar y reforzar en `position_manager.update_position_sl()` y `futures_client.update_sl_tp()`.
- [ ] **C2. BE más temprano en modos rápidos:** activar BE a menor ganancia en `RANGE_SCALP` y `FUTURES_SHORT` para proteger capital en mercados de reversión rápida.
- [ ] **C3. Peak protection más sensible:** si `peak_pnl_pct >= 2 × min_profit_pct` y `current_pnl < peak × protect_pct`, disparar cierre con prioridad 95; ajustar `protect_pct` por **perfil de calidad real de entrada**, no por perfil de régimen.
- [ ] **C4. SSO-1 decay más sensible:** reducir `sso_decay_min_profit_usd` para posiciones grandes o en FUTURES_SHORT; mantener floor dinámico basado en wallet.
- [ ] **C5. Cierre parcial canónico:** todo parcial debe insertar fila en `trades_closed` con `partial_close=True`, reducir `quantity` en `trades_open`, y resetear `peak_pnl_pct` baseline para la porción restante. Reforzar regla dura de `.windsurf/rules.md` §3.
- [ ] **C6. Unificación SPOT/FUTURES de lógica de salida:** extraer helper común que construya `SSO/PRS/UG/PredExit` context para ambos modos; evitar divergencia de criterios.
- [ ] **C7. F&G y correlación en salida:** si F&G <25 o >75 ajustar paciencia/peak-protection; si múltiples posiciones cierran SL en la misma hora, activar pausa global de re-entrada.
- [x] **C8. Propagación del perfil de calidad:** `entry_profile_gate.classify()` ya escribe `peak_quality_profile`, `peak_quality_score`, `entry_profile_gate_info` (incluyendo `score_source`, `model_version`, `category`) en `signal_meta`; `_filter_buy_candidates` y `staged_entry` propagan estos valores al metadata de la posición. Pendiente: que SSO/PRS/UG los consuman para escalar paciencia/riesgo.
- [ ] **C9. Comisiones netas en decisión de parcial:** incluir comisión + slippage estimado en el cálculo de umbral de cierre parcial para evitar que parciales frecuentes erosionen PnL.
- [ ] **C10. Dashboard de ciclo de vida:** añadir vista de "vida activa" con BE, trailing, peak, advisories, próximo trigger estimado y giveback actual.

### D. Validación y tests
- [ ] **D1.** Tests de unidad para cada advisory (SSO/PRS/UG/PredExit) con escenarios de pico/reversión.
- [ ] **D2.** Tests de integración MIMIC con métricas de ciclo de vida: win-rate, drawdown, tiempo medio en trade, % devuelto del pico.
- [ ] **D3.** Backtest out-of-sample con cambios propuestos antes de aplicar en producción.
- [ ] **D4.** Modo sombra (shadow): ejecutar nuevo pipeline de salida en paralelo durante 24h sin ejecutar órdenes, comparar decisiones vs las ejecutadas.

## Criterios de aceptación para cualquier cambio

Antes de aprobar un cambio debe responder las 6 preguntas de `.windsurf/rules.md`:

1. ¿Cómo impacta SPOT y FUTURES por separado?
2. ¿Cómo impacta los perfiles A/B/C de calidad de entrada y de régimen?
3. ¿Qué gate valida la aceptación y cuál gatilla rollback?
4. ¿Qué estado se persiste y qué evento se audita en `trades_audit`?
5. ¿Cómo se mide contra baseline / changepoint?
6. ¿Qué endpoint/dashboard mostrará la salud del cambio?

Además:

- Todo cierre parcial debe persistir correctamente en DB (regla dura).
- Ningún cambio puede hacer retroceder el SL.
- SPOT y FUTURES deben tener paridad de criterios salvo diferencia documentada.
- Antes de rollout: 48h de shadow log + suite completa verde.

## Rollback por defecto

Si después de aplicar ajustes el PnL neto de 48h empeora o el win-rate cae más de 5 puntos porcentuales respecto al baseline:

```
PATCH /config/kelly {
  "sso_enabled": true,
  "profit_predexit_enabled": true,
  "unified_governance_enabled": true,
  "trailing_stop_activation_pct": <valor pre-cambio>,
  "peak_protection_spot_bull_pct": <valor pre-cambio>,
  "sso_decay_min_profit_usd": <valor pre-cambio>
}
```

## 10. Análisis de daño real — últimas 48h y 7 días

Datos extraídos directamente de `trades_closed` y `trades_audit` el 2026-07-23.

### 10.1 Resumen por periodo

| Periodo | Cierres | Wins | Losses | PnL neto | Win-rate | Prom/trade |
|---------|---------|------|--------|----------|----------|------------|
| 48h     | 34      | 13   | 21     | **-137.11** | 38.24% | -4.03 |
| 7d      | 226     | 103  | 123    | **-488.34** | 45.58% | -2.16 |

### 10.2 Cierres por mecanismo (48h)

| Reason | N | Wins | Losses | Total PnL | Avg | WR |
|---|---|---|---|---|---|---|
| `unified_governance_soft_supervisor_escalated` | 10 | 0 | 10 | **-109.16** | -10.92 | 0% |
| `unified_governance_peak_drawdown_cut` | 4 | 0 | 4 | **-48.04** | -12.01 | 0% |
| `unified_governance_loss_fast_cut` | 2 | 0 | 2 | **-32.30** | -16.15 | 0% |
| `unified_governance_loss_defensive_close` | 1 | 0 | 1 | **-10.41** | -10.41 | 0% |
| `prs_peak_protection` | 3 | 3 | 0 | +20.57 | +6.86 | 100% |
| `profit_predexit_capture` | 1 | 1 | 0 | +12.35 | +12.35 | 100% |
| `pretrade_ghost_auto_close` | 1 | 1 | 0 | +13.11 | +13.11 | 100% |
| `sso_partial_close` | 4 | 2 | 2 | +5.57 | +1.39 | 50% |
| `profit_realization_supervisor_partial` | 4 | 4 | 0 | +3.96 | +0.99 | 100% |
| `profit_realization_supervisor_full` | 1 | 1 | 0 | +0.11 | +0.11 | 100% |
| `ghost_balance_mismatch` | 3 | 1 | 2 | +7.11 | +2.37 | 33% |

**Conclusión:** los mecanismos de `unified_governance_*` explican **-199.91 USDT (146%)** del daño total. PRS/SSO/PredExit aportan ~+55 USDT. El sistema cobra primas pequeñas y paga primas masivas en cierres por pérdida.

### 10.3 Señal histórica: trades tipo SELL vs BUY (30d)

| Side | Cierres | Wins | PnL neto |
|---|---|---|---|
| SELL | 180 | 133 (74%) | **+670.08** |
| BUY | 721 | 348 (48%) | **-677.23** |

`SELL` (que incluye shorts de futuros y ventas SPOT) ha sido fuertemente positivo. `BUY` (entradas SPOT) ha sido el lado perdedor. Esto indica que la dirección corta/venta ha tenido ventaja estadística reciente, y que un hedge SHORT podría haber compensado parte del drawdown SPOT.

### 10.4 Sizing y pérdidas en %

Ejemplos de cierres por UG loss en 48h:

| Symbol | Qty | Entry | Exit | PnL USD | PnL % |
|---|---|---|---|---|---|
| OPNUSDT | 16,255 | 0.0690 | 0.0678 | -21.17 | -1.89% |
| NEARUSDT | 1,100 | 1.9650 | 1.9540 | -15.34 | -0.71% |
| OPNUSDT | 21,101 | 0.0694 | 0.0688 | -14.85 | -1.01% |
| KITEUSDT | 11,270 | 0.1198 | 0.1188 | -13.29 | -0.98% |
| WLDUSDT | 5,661 | 0.3777 | 0.3761 | -12.23 | -0.57% |

Las pérdidas porcentuales son pequeñas (-0.5% a -1.9%), pero **grandes en USD por sizing masivo**.

### 10.5 Overtrading

| Día | Aperturas | Símbolos distintos | Cierres | PnL día |
|---|---|---|---|---|
| 2026-07-23 | 16 | 6 | 16 | -70.09 |
| 2026-07-22 | 3 | 3 | 5 | +9.73 |
| 2026-07-21 | 37 | 22 | 35 | -124.55 |
| 2026-07-20 | 25 | 11 | 26 | -22.76 |
| 2026-07-19 | 68 | 19 | 70 | -27.17 |
| 2026-07-18 | 62 | 20 | 62 | -177.50 |
| 2026-07-17 | 12 | 7 | 12 | -75.99 |

Hasta 68 aperturas en 19 símbolos en un solo día indica re-entradas repetidas sin esperar dirección clara.

### 10.6 Breakeven y trailing

En 48h solo hubo **3 eventos `breakeven_activated`** y **0 `trailing_stop_triggered`**. La mayoría de las posiciones nunca alcanzan el umbral de BE/trailing. Esto invalida la premisa de "dejar correr ganancias protegidas".

## 11. Diagnóstico por roles

### 11.1 Físico / Optimización cuántica
- El sistema está atrapado en un **mínimo local del landscape de parámetros**: más sizing para compensar pérdidas, lo que genera pérdidas aún mayores.
- UG loss branch actúa como un **operador de truncamiento no unitario**: corta colas de pérdida, pero también destruye valor esperado al cerrar en momentos de reversión.
- La medición de `peak_pnl_pct` (reconstruida desde velas 1m) tiene lag; en altcoins el verdadero pico puede estar entre velas.
- Los thresholds fijos en % no escalan con la volatilidad del asset ni con el sizing.

### 11.2 Estadístico / Ciencia de datos
- **Win-rate 38-45% con R:R invertido**: ganancias promedio pequeñas (+0.99 en PRS parciales) vs pérdidas grandes (-10.92 en UG loss). Esperanza negativa incluso con WR del 50%.
- **Sesgo de supervivencia**: parámetros ajustados en backtests cortos donde UG loss no se activaba tanto. En vivo, la correlación entre posiciones (caídas de mercado) dispara muchos UG loss simultáneos.
- **Predictor de entrada optimista**: `entry_pred_pnl` promedio +2.51, correlación con PnL real **-0.18**. No discrimina trades perdedores. Necesita features de salida y estado de mercado.
- **Falta de validación out-of-sample**: ajustes vía hot-config sin 48h forward-test.

### 11.3 Analista técnico / Gráficos
- Las señales de 1h/4h son demasiado lentas para un ciclo de 5-15 minutos.
- El sistema no lee estructura de mercado: soporte/resistencia, rangos, tendencias corto plazo.
- Cierra por "velocidad negativa" sin verificar si el precio está en soporte.
- PRS hace parciales a 14-40 minutos, ignorando niveles de oferta/demanda.
- BE/trailing requiere `pnl_pct_net >= trail_activate` por categoría. Para `RANGE_SCALP` ese umbral es inalcanzable en movimientos de 1-2%.

### 11.4 Ingeniero de sistemas
- `run_trades.py` tiene 9000+ líneas; UG, SSO, PRS, PredExit, SmartTP, BE y trailing están anidados y difíciles de auditar.
- `peak_quality_profile` del `entry_profile_gate` **no llega al ciclo de vida**. UG usa `current_regime_profile_label=A` para todo, sin discriminar calidad real.
- `unified_governance_loss_min_usdt=1.50` es bajo; UG interviene en pérdidas pequeñas.
- `fast_loss_threshold` fijo (-0.75% en RANGE_SCALP, -1.5% otros) no considera volatilidad ni sizing.
- Los parciales PRS a 60% erosionan comisiones y dejan la posición residual expuesta sin BE.

## 12. Rediseño del ciclo de vida

### 12.1 Principio rector: "Primero no perder en promedio"
El objetivo de "ganancias extremas" solo es sostenible si primero se elimina la asimetría negativa. Orden de prioridades:

1. **Perder poco**: cada operación debe tener pérdida máxima en USD acotada.
2. **No perder nunca después de BE**: tras ganancia mínima, SL nunca debe volver a negativo.
3. **Ganar mucho**: dejar correr con trailing dinámico cuando la estructura lo permita.

### 12.2 Cambios canónicos propuestos

#### A. Cap de pérdida por operación (sin tocar Kelly sizing)
- `max_loss_usd_per_trade = min(20, portfolio_total * 0.003)`.
- El sizing se mantiene igual (Kelly). Lo que se ajusta es el **SL efectivo**: `sl_effective_pct = min(category_sl_pct, max_loss_usd / position_value)`.
- Ejemplo: posición de $2,000 con SL 3% → pérdida potencial $60. Con cap de $20 el SL efectivo se reduce a 1.0%.
- Aplicar a SPOT y FUTURES. Documentar en `.windsurf/rules.md`.
- **Por qué no reducir Kelly:** queremos capturar las ganancias masivas que Kelly proporciona cuando el trade funciona; el problema es dónde cortamos cuando no funciona.

#### B. UG loss branch rediseñado
1. **Eliminar `loss_fast_cut` por PnL fijo**. Reemplazar por SL dinámico ATR o ruptura de soporte confirmada.
2. **`peak_drawdown_cut`**: requerir pico real >= 1.5× ATR 1h y giveback confirmado por cierre de vela, no tick.
3. **`soft_supervisor_escalated`**: nunca cerrar en pérdida solo porque SSO/PRS lo pidan. En zona de pérdida SSO/PRS solo pueden proponer ajuste de SL, no cierre.
4. Pausa de re-entrada de 4h tras cierre con pérdida para el mismo símbolo.

#### C. BE/trailing temprano y dinámico
1. **BE móvil**: tras +0.15% de ganancia neta en `RANGE_SCALP` (o +0.25% en `SPOT_BULL`), mover SL a `entry + 1.5× comisión`. Esto asegura no perder dinero real.
2. **Trailing** sigue al precio con distancia escalada por ATR 15m, no por categoría fija.
3. **SL nunca retrocede**.

#### D. PRS reformulado
1. Parciales por niveles de estructura (resistencia inmediata, R1/R2 diario) en lugar de minutos transcurridos.
2. Desactivar "daily target contribution" cuando el PnL diario acumulado es negativo; forzar cierres para cumplir meta mientras se pierde es contraproducente.
3. Peak protection mínimo: requiere pico >= 1× ATR 1h para evitar ruido.

#### E. Integrar dirección y estructura de gráfico
1. Nuevo módulo `market_structure.py`: soporte/resistencia 1h/4h, tendencia corto plazo, distancia a soportes, RSI en timeframe relevante.
2. UG recibe `distance_to_nearest_support_pct` y `distance_to_nearest_resistance_pct`.
3. No cerrar long en pérdida si el precio está en soporte y la señal sigue favorable.
4. No dejar correr ganancia si toca resistencia sin volumen.

#### F. Sizing ligado a calidad de entrada
1. Propagar `peak_quality_profile` y `peak_quality_score` al metadata de `trades_open`.
2. Perfil A: más paciencia, trailing más ancho, BE algo más tarde. Perfil C: menos sizing, BE más temprano, SL más ajustado.
3. UG loss patience se escala por perfil de calidad, no por perfil de régimen.

#### G. FUTURES_SHORT como hedge automático
1. En caídas de mercado generalizadas (BTC 1h -0.5% o más), abrir shorts automáticamente como hedge del portfolio SPOT.
2. El sizing del hedge se calcula para neutralizar beta del portfolio, no para maximizar ganancia.
3. Esto convierte pérdidas de drawdown en ganancias en caídas.

### 12.3 FUTURES_SHORT — cómo funciona hoy y modo dual

#### Estado actual
- El detector de régimen en `market_regime.py` elige **un único modo**: `SPOT_BULL`, `RANGE_SCALP`, `PRESERVATION`.
- Si `futures_enabled=True` y `preservation_upgrade_to_futures=True`, `PRESERVATION` se convierte en `FUTURES_SHORT`.
- En `FUTURES_SHORT` se abren shorts sobre los símbolos de `futures_symbols` que den señal `SELL` o `HOLD` sobrecomprado, con filtros de:
  - tendencia 4h DOWN,
  - RSI 4h < 50,
  - vela 5m roja,
  - BTC no en rebote 1h/3h,
  - funding rate < 0.05%,
  - cooldown por SL y mass-SL.
- El sizing usa Kelly real sobre margen de futuros (`sl_pct=futures_sl/leverage`, `tp_pct=futures_tp/leverage`), con cap por margen disponible y correlación entre shorts.
- Tras salir del modo bajista, los shorts se cierran con whipsaw guard.

#### Limitación actual
No se pueden tener dos modos **oficialmente** al mismo tiempo. Si el mercado está en `SPOT_BULL` pero hay una corrección intradía, el sistema no abre shorts; solo deja de comprar SPOT si cambia el modo.

#### Propuesta: modo dual `*_WITH_HEDGE`
Introducir sub-modos concurrentes en `market_regime.py` y `run_trades.py`:

| Modo principal | Sub-modo hedge | Condición de activación | Qué hace |
|---|---|---|---|
| `SPOT_BULL` | `FUTURES_SHORT_HEDGE` | BTC 1h cae >= 0.5% y confianza bajista >= 0.55 | Sigue comprando SPOT, pero abre 1-2 shorts como seguro |
| `RANGE_SCALP` | `FUTURES_SHORT_HEDGE` | BTC 4h DOWN y precio cerca de resistencia | Aprovecha el lado corto mientras hace scalp long |
| `PRESERVATION` | `FUTURES_SHORT` (ya existe) | Bear estructural confirmado | Modo totalmente corto |

En la práctica esto requiere:
1. Que `get_regime()` devuelva `(primary_mode, hedge_mode)` en lugar de un solo `mode`.
2. Que `run_trades.py` ejecute ambas ramas: entradas SPOT según modo principal y entradas SHORT según hedge_mode.
3. Que el capital de futuros esté disponible y separado del spot.
4. Que el sizing del hedge sea fijo o por beta, nunca Kelly agresivo (su objetivo es proteger, no generar ganancias extremas por sí solo).

## 13. Acciones inmediatas propuestas

### Fase 0 — Hot-config de emergencia (ahora)
Aplicar vía `PATCH /config/kelly` **sin reiniciar** y monitorear 24h. **No se toca Kelly**.

```json
{
  "sl_effective_max_loss_usd": 20.0,
  "unified_governance_loss_min_usdt": 25.0,
  "unified_governance_loss_fast_cut_enabled": false,
  "unified_governance_peak_drawdown_cut_enabled": false,
  "profit_realization_spot_partial_minutes_range_scalp": 120.0,
  "profit_realization_spot_full_minutes_range_scalp": 240.0,
  "range_scalp_breakeven_pct": 0.15,
  "spot_bull_breakeven_pct": 0.25,
  "symbol_cooldown_after_loss_minutes": 240,
  "sso_target_daily_usd": 0.0,
  "futures_enabled": true,
  "preservation_upgrade_to_futures": true,
  "futures_hedge_spot_bull_enabled": true,
  "futures_hedge_btc_1h_drop_pct": 0.5,
  "futures_hedge_max_shorts": 2
}
```

**Rationale:** mantiene Kelly intacto para ganancias masivas; acota pérdidas vía SL efectivo; desactiva los gatillos UG más destructivos; retrasa parciales; activa BE temprano; pausa re-entradas; desactiva SSO-4 "daily target" mientras se pierde; y activa FUTURES_SHORT como hedge (incluyendo en SPOT_BULL cuando BTC cae 0.5% en 1h).

### Fase 1 — Cambios de código (48h)
1. `position_manager.py` + `run_trades.py`: propagar `peak_quality_profile` al metadata de `trades_open` y al contexto de UG/PRS/SSO.
2. `run_trades.py`: implementar `sl_effective_max_loss_usd` — recalcular SL efectivo por posición sin cambiar Kelly.
3. `unified_governance_supervisor.py`: loss branch basado en estructura de mercado + ATR; eliminar `loss_fast_cut` fijo y `soft_supervisor_escalated` en pérdida.
4. `smart_scale_out.py`: desactivar `SSO-LOSS` por PnL fijo; solo triggers de momentum/estructura.
5. `profit_realization_supervisor.py`: parciales por niveles de resistencia (nuevo input de `market_structure.py`).
6. Nuevo `market_structure.py`: soporte/resistencia 1h/4h, distancia a soportes.
7. `market_regime.py` + `run_trades.py`: implementar modo dual `SPOT_BULL_WITH_HEDGE` / `RANGE_SCALP_WITH_HEDGE`.

### Fase 2 — Validación
1. Shadow log 48h con Fase 0.
2. MIMIC sweep con Fase 1 en ventanas recientes.
3. Activar Fase 1 solo si shadow log muestra PnL neto 48h >= baseline y < -30 USD.

### Rollback
Si después de Fase 0 el PnL neto de 48h empeora respecto al baseline (-137 USD) o el win-rate cae >5 pp, revertir hot-config a valores pre-cambio.

## 14. Conclusión

El problema no es un módulo aislado, sino una **asimetría sistémica en las salidas**: entradas grandes (deseables para ganancias masivas) se cortan por un `UnifiedGovernance` hiperactivo en pérdidas moderadas-pero-masivas-en-USD, mientras los mecanismos de ganancia (PRS/SSO/PredExit) capturan primas pequeñas. La solución no es reducir Kelly, sino **proteger el capital con SL efectivo, BE temprano y dirección de mercado**. Además, los datos de 30 días muestran que el lado corto ha sido rentable, por lo que activar FUTURES_SHORT como hedge (y eventualmente en modo dual) puede convertir drawdowns SPOT en ganancias. La dirección y la estructura de precios deben reemplazar a los parámetros fijos como principal input de salida.

## Referencias

- `.windsurf/rules.md` §3 (ciclo de vida), §3.5 (modo × perfil), §6 (patrones de bug recurrentes).
- `SYSTEM_REFERENCE.md` estados s86–s90.
- Plan de calibración de entradas: `c:\Users\raul8\.windsurf\plans\calibracion_entradas-a20ab8-v3.md`.
- Módulos clave: `run_trades.py`, `futures_client.py`, `smart_scale_out.py`, `profit_realization_supervisor.py`, `peak_capture_engine.py`, `unified_governance_supervisor.py`, `position_manager.py`, `state_persistence.py`.
- Datos del análisis: queries a `crypto_agent.trades_closed` y `crypto_agent.trades_audit` del contenedor `crypto-agent-db`.

## 15. Fix paridad DB/Binance para cierres manuales de futures — cc1b8f

**Fecha:** 2026-07-28
**Símbolo ejemplo:** DIAUSDT SHORT

### 15.1 Problema

El cierre manual de un future SHORT persistió en `trades_closed` con:
- `exit_price = entry_price` → PnL bruto 0
- `pnl = -comisiones` (−$8.55)
- Poco después apareció un segundo registro con `close_reason = trailing_sl_binance` y `pnl = +$124.28`, duplicando el cierre sin existir una segunda operación real en Binance.

El PnL real reportado por `userTrades` fue +$128.41, neto ~+$125.45.

### 15.2 Root cause

| # | Causa | Ubicación | Efecto |
|---|---|---|---|
| A | `pos.exit_price` y `pos.pnl` se asignaban **después** de `_save_futures_closed()` | `futures_client.py` `close_long`/`close_short` | El cierre se persistía con `exit_price = entry_price` y PnL ≈ −comisiones. |
| B | `_get_realized_pnl_from_trades()` usaba heurística por signo del PnL para etiquetar la razón | `futures_client.py` | Un cierre manual positivo se etiquetaba como `trailing_sl_binance`. |
| C | El ciclo principal no conocía cierres manuales hechos desde otro proceso | `run_trades.py` / `manual_position_closer.py` | `check_positions()` regeneraba el cierre como SL/TP de Binance. |

### 15.3 Cambios

1. **`futures_client.py`**
   - `_average_fill_price(order, fallback)`: precio real del fill (fills → avgPrice → fallback).
   - `close_long()` / `close_short()`: asignan `pos.exit_price` y `pos.pnl` **antes** de persistir.
   - `_classify_close_reason(symbol, order_id, realized_pnl)`: prefiere `manual_close` por `trades_audit`, luego el tipo de orden de Binance, y solo al final recurre a signo.
   - `sync_manual_closes_from_audit()`: lee `manual_position_futures_close_done` de los últimos 20 min y marca posiciones en memoria como cerradas.

2. **`run_trades.py`**
   - `_fc.sync_manual_closes_from_audit()` se ejecuta antes de `_fc.check_positions()` cada ciclo.

3. **`manual_position_closer.py`**
   - El audit `manual_position_futures_close_done` guarda `order_id` y `client_order_id`.

### 15.4 Validación

- `tests/test_futures_manual_close_parity.py`: 5/5 passed.
- Regresión futures: 112/112 passed.
- `auto_reconcile_futures(dry_run=True)`: `mismatch_count: 0`.
- Contenedor `crypto-agent-app` reiniciado; health `overall: ok`; solo queda `WLDUSDT SHORT`.

### 15.5 Rollback

Revertir los commits que toquen `futures_client.py`, `run_trades.py`, `manual_position_closer.py` y el test; reiniciar el contenedor; ejecutar `auto_reconcile_futures(dry_run=False)` si aparecen discrepancias.

---

## 16. Reconexión automática del WebSocket de Binance — s105i (2026-08-01)

### Problema

Logs de Docker repetían: `Error receiving message: Read loop has been closed, please reset the websocket connection and listen to the message error.` El websocket del User Data Stream de Binance moría y `BinanceEventStream` no reconectaba, dejando al sistema dependiente del polling de 5 min.

### Causa

`python-binance` cierra el read loop del websocket ante determinadas condiciones de error. `BinanceEventStream` iniciaba la conexión pero no escuchaba mensajes de error del websocket ni reconectaba automáticamente.

### Cambios aplicados

- `binance_event_stream.py`:
  - `_is_socket_error_payload()` detecta strings/dicts con "read loop has been closed", "error receiving message", "connection closed" o "websocket error".
  - `_on_futures_message` / `_on_spot_message` detectan el error, registran `last_error` y llaman `_schedule_reconnect()`.
  - `_reconnect()` destruye el `ThreadedWebsocketManager` muerto, crea uno nuevo, reinicia los user data streams de futuros y spot, y re-suscribe los mark-price streams activos.
  - `_watchdog_loop()` actúa como red de seguridad: 30 min sin ningún evento dispara reconexión.
  - Cooldown de 60s entre reconexiones para evitar loops.
  - `health()` expone `event_stream_reconnect_count`, `event_stream_last_reconnect_at` y `event_stream_pending_reconnect`.
- Tests añadidos en `tests/test_binance_event_stream.py`.

### Relación con el pipeline de salida

El event stream acelera la detección de fills/cierres (segundos en vez de minutos), pero el polling y la reconciliación existentes (`FIX-S4`, `FIX-T4-L1/L2`) siguen siendo la red de seguridad. El fix no modifica la lógica de cierre, solo mejora la disponibilidad del canal de eventos.

### Rollback / desactivación

- Deshabilitar websocket: variable de entorno `BINANCE_EVENT_STREAM_ENABLED=False` (o flag equivalente en config) y reiniciar el contenedor.
- Forzar reconexión manual: `event_stream.force_reconnect()` o reiniciar el proceso (Python no recarga módulos en caliente).

### Validación

- `python -m pytest tests/test_binance_event_stream.py -q` => `19 passed`.
- Requiere reiniciar el contenedor para que Python cargue el módulo modificado.
