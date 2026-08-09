# Plan de implementación: reconciliación y visibilidad bidireccional

## Objetivo

Convertir los cierres en una verdad canónica única para todos los modos, lados y venues soportados, con trazabilidad completa de intención, ejecución, reconciliación y PnL realizado.

El criterio de diseño es:
- simplificar el control plane;
- robustecer el data plane;
- minimizar hardcodes y heurísticas duplicadas;
- mantener paridad entre longs y shorts en futures, y entre las rutas equivalentes de spot cuando el venue lo permita.

## Decisión arquitectónica

No conviene eliminar supervisores de lifecycle. Conviene:
- unificar la clasificación y persistencia de cierres;
- centralizar la reconciliación Binance↔DB;
- hacer explícitos los estados de reconciliación;
- dejar a SSO/PRS/UG como consumidores de una verdad única, no como autores paralelos.

La simplificación correcta es eliminar duplicidad de lógica, no quitar capas funcionales que cubren problemas distintos.

## Principios de diseño

1. El cierre debe tener una sola representación canónica.
2. La intención de cerrar, la confirmación de Binance y la razón final no se mezclan.
3. La lógica debe ser side-agnostic por defecto: un lado se trata como `position_side`, no como un caso especial.
4. Los hardcodes deben reemplazarse por:
   - catálogos de razones;
   - mapeos de side/regime/config;
   - helpers puros reutilizables.
5. Cada ruta de cierre debe ser idempotente.
6. Los cierres sintéticos o reconciliados deben quedar marcados como no aptos para entrenamiento.

## Contrato bidireccional

El sistema debe comportarse así:

- Futures long y futures short usan exactamente el mismo pipeline, con inversión simétrica de signos, stop/target y reglas de protección.
- Spot usa el mismo contrato de evento y de reconciliación, aunque el adapter de venue valide qué lados son realmente ejecutables.
- Toda lógica de riesgo y lifecycle debe recibir:
  - `position_side` normalizado;
  - `trade_type`;
  - `regime_mode`;
  - `entry_context`;
  - `close_intent`;
  - `exchange_state`;
  - `reconciliation_state`.

## Qué simplificar

### 1. Unificar clasificación de cierre

La clasificación de `close_reason` hoy está repartida entre el cliente de futures, la reconciliación y las rutas de ghost/manual close. Eso debe reducirse a un único clasificador compartido.

Propuesta:
- mover la lógica de inferencia a un helper puro común;
- usarlo en `futures_client.py`, `position_manager.py` y `manual_position_closer.py`;
- dejar `binance_truth_guard.py` como auditor/reconciliador, no como segunda fuente de verdad.

### 2. Reducir duplicidad de ghost cleanup

El ghost cleanup no debe existir como lógica distinta para cada ruta. Debe convertirse en una sola máquina de estados con tres salidas:
- reconciliar y cerrar;
- reconciliar y conservar;
- marcar mismatch y diferir.

### 3. Unificar supervisión de profit preservation

SSO, PRS y UG comparten la misma intención funcional: preservar valor sin estrangular ganadores. No deben competir con reglas distintas para el mismo tramo del lifecycle.

La simplificación propuesta es:
- una política de preservación común;
- adaptadores por módulo para la mecánica concreta;
- decisiones finales centralizadas en UG.

## Qué robustecer

### 1. Cierre como evento canónico

Cada cierre debe persistirse con estos campos mínimos:
- `close_reason`
- `close_source`
- `close_intent`
- `close_state`
- `reconciliation_state`
- `close_group_id`
- `exchange_match_status`
- `ghost_reconciled`
- `synthetic`
- `learning_eligible`
- `realized_net_pnl`
- `commission_total`

### 2. Máquina de estados de reconciliación

Estados recomendados:
- `intent_created`
- `exchange_confirmed`
- `partial_reconciled`
- `fully_reconciled`
- `mismatch_detected`
- `orphaned`
- `synthetic_reconciled`

### 3. Idempotencia y lock de cierre

Toda ruta que cierre o reconcilié debe ser idempotente y serializada a nivel de símbolo/cierre global donde haga falta.

### 4. Observabilidad de cierre

El dashboard debe separar:
- decisión;
- ejecución;
- verdad Binance;
- reconciliación;
- PnL realizado;
- motivo final.

## Plan por módulo

### P0. Contrato y persistencia base

**Módulos:** `state_persistence.py`, `position_manager.py`, `futures_client.py`

**Cambios:**
- definir el modelo canónico de evento de cierre;
- normalizar `position_side` y `trade_type`;
- centralizar la escritura de estados de cierre;
- asegurar idempotencia.

**Prioridad:** máxima.

**Riesgo:** alto, porque toca el núcleo de verdad contable.

**Mitigación:** feature-flag de lectura/escritura dual, tests de regresión con cierres reales y synthetic/ghost.

### P1. Reconciliación Binance↔DB

**Módulos:** `binance_truth_guard.py`, `position_manager.py`, `futures_client.py`, `manual_position_closer.py`

**Cambios:**
- convertir `binance_truth_guard.py` en reconciliador puro con salida de eventos;
- eliminar inferencias distintas para el mismo caso;
- preservar trazabilidad de ghost y mismatches;
- distinguir `manual_close` real de `binance_unidentified_close` técnico.

**Prioridad:** máxima.

**Riesgo:** alto por impacto directo en cierres automáticos y ghosts.

**Mitigación:** despliegue por shadow, luego active solo para observación, luego activa para persistencia.

### P2. Supervisión de lifecycle y profit preservation

**Módulos:** `smart_scale_out.py`, `profit_realization_supervisor.py`, `unified_governance_supervisor.py`, `peak_capture_engine.py`

**Cambios:**
- factorizar la lógica común de preservación;
- hacer la evaluación side-agnostic;
- asegurar que longs y shorts usen la misma semántica, invertida solo en el cálculo de signo;
- mantener UG como árbitro final.

**Prioridad:** alta.

**Riesgo:** medio-alto, porque una calibración agresiva puede estrangular winners.

**Mitigación:** rollback por hot-config, límites por régimen, y shadow readiness comparando defer vs close.

### P3. Telemetría 1s y hard-risk

**Módulos:** `position_telemetry_watcher.py`, `hard_risk_monitor.py`

**Cambios:**
- unificar las señales de pérdida y erosión en un único contrato de observabilidad;
- hacer que el monitor vea tanto profit como loss;
- mantener la disciplina de que el monitor propone y el lifecycle consolida.

**Prioridad:** alta.

**Riesgo:** medio, por posibles falsos positivos de reducción de SL.

**Mitigación:** thresholds conservadores, activación por persistencia temporal, pruebas con ventanas de mercado distintas.

### P4. Motor de entrada bidireccional

**Módulos:** `run_trades.py`, `staged_entry.py`, `directional_arbiter.py`, `support_resistance.py`, `autonomous_signals.py`

**Cambios:**
- eliminar if/else por lado donde un helper puro pueda decidir;
- parametrizar el lado como signo o estrategia, no como branch duplicada;
- alinear la validación de longs y shorts en futures;
- mantener consistencia del contrato entre SPOT y FUTURES.

**Prioridad:** media-alta.

**Riesgo:** medio, porque tocar la entrada puede alterar la tasa de apertura.

**Mitigación:** solo cambios con hipótesis falsable y replay contra cohortes etiquetadas.

### P5. Dashboard y auditoría

**Módulos:** `app.py`, `frontend/dashboard.js`, `frontend/styles.css`

**Cambios:**
- mostrar la secuencia completa de cierre;
- resaltar reconciliación pendiente o fallida;
- separar PnL realizado, unrealized y estado Binance;
- hacer visibles los cierres sintéticos, ghost y manuales reales.

**Prioridad:** media.

**Riesgo:** bajo.

**Mitigación:** cambios presentacionales y payload-only.

## Orden de despliegue recomendado

### Fase 0 — contrato y observabilidad

1. Introducir el esquema canónico de cierre.
2. Emitir eventos de reconciliación explícitos.
3. Añadir métricas y dashboard de estados de cierre.

### Fase 1 — reconciliación segura

1. Unificar clasificación de cierre.
2. Centralizar ghost cleanup.
3. Diferenciar un cierre real de Binance de un cierre inferido.

### Fase 2 — lifecycle y protección de capital

1. Factorizar SSO/PRS/UG.
2. Alinear la protección de profit en long/short.
3. Validar que el monitor de 1s alimenta, pero no reemplaza, el árbitro final.

### Fase 3 — entrada bidireccional

1. Reutilizar helpers para long/short.
2. Reducir hardcodes por rama.
3. Validar simetría por régimen.

### Fase 4 — hardening final

1. Replays y pruebas de regresión con cierres reales.
2. Shadow-to-active por cohortes.
3. Ajuste de umbrales con datos vivos.

## Riesgos principales

1. **Regresión de contabilidad.** Un cambio de cierre puede alterar PnL histórico o duplicar cierres si la idempotencia falla.
2. **Sobrecorrección.** Simplificar demasiado podría ocultar señales útiles de SSO/PRS/UG.
3. **Estrangulamiento de ganadores.** Los cambios de protección pueden cortar demasiado pronto si se calibran con pérdidas recientes solamente.
4. **Sesgo por régimen.** Un fix útil en RANGE_SCALP puede no transferirse igual a SPOT_BULL o FUTURES_SHORT.
5. **Hardcodes nuevos por prisa.** Si no se abstrae bien, se reemplaza un conjunto de `if`s por otro.

## Estrategia para evitar hardcodes

- Representar side como `LONG`/`SHORT` normalizados y usar helpers con signo.
- Usar catálogos de razones de cierre y estados de reconciliación.
- Inferir comportamiento por régimen desde configuración, no desde ramas embebidas.
- Mantener una sola política de lifecycle con parámetros por régimen y por side.
- Evitar mapeos literales repetidos en frontend/backend; compartir contratos serializados.

## Criterios de éxito

El rediseño se considera correcto si:
- disminuyen los cierres `binance_unidentified_close` y `ghost_balance_mismatch`;
- cada cierre deja un estado de reconciliación explícito;
- longs y shorts siguen la misma semántica en futures;
- spot conserva su contrato sin lógica paralela innecesaria;
- SSO/PRS/UG siguen funcionando, pero sobre una base contable única;
- el dashboard permite auditar por qué se cerró una posición sin leer logs.

## Próximo entregable

Convertir este plan en un mapa de implementación por archivo con cambios pequeños, empezando por el contrato canónico de cierre y la máquina de estados de reconciliación.