# Plan — Cierre de bypasses del pipeline de entrada

Fecha: 2026-09-28/29
Autor: Devin (integración de análisis propio + review externo)
Estado: propuesta para implementación por Copilot; verificación posterior por Devin
Alcance: **solo Entry**. No tocar lifecycle (SSO/PRS/UG/PredExit/trailing), leverage, ni producción.

## Contexto

El pipeline bilateral s139/s140 está arquitectónicamente correcto, pero el review externo identificó bypasses operativos que permiten que una entrada se ejecute sin pasar por las autoridades que el diseño exige (pausa DB, QUBO vigente, revalidación completa tras cambio de lado). Mientras existan esos bypasses, ninguna calibración de Entry es medible: la señal observada mezcla decisiones del árbitro con ejecuciones que lo eludieron.

Verificación de los hallazgos hecha por Devin en código staging (referencias exactas abajo).

## Hallazgos confirmados (verificados en código)

### H1 — FastPath ignora `pause_new_entries` de la base de datos

- `execute_once()` consulta `_is_new_entries_paused(_db_client_ref)` y degrada a `monitor_only` (`run_trades.py` ~13081).
- El watcher intra-ciclo solo consulta `orchestrator.can_execute_trades()`, que mira el estado en memoria (`orchestrator.py:362`, `orchestrator.py:882`).
- Consecuencia: una pausa persistida en `trading_runtime_state` no protege al FastPath. Una pausa manual de operador puede ser ignorada.
- **Fix requerido:** el watcher debe leer `trading_runtime_state` (mismo helper `_is_new_entries_paused`) antes de cualquier ejecución, no solo el estado en memoria del orquestador.

### H2 — `top_symbols_qubo` se recibe pero no se usa

- `staged_entry.py:2515` `get_openable_candidates()` acepta `top_symbols_qubo` pero el SQL (`WHERE status='validated' AND regime_mode=%s`) no lo aplica.
- El ciclo normal pasa el conjunto QUBO; el watcher lo llama sin filtro → puede abrir una `validated` antigua que ya no pertenece al QUBO actual.
- **Fix requerido:** si `top_symbols_qubo` no es `None`, filtrar por `symbol = ANY(%s)` (o equivalente). Además el watcher debe pasar el conjunto QUBO vigente; si no lo tiene, no ejecutar.

### H3 — Cambio de lado en FastPath sin revalidar perfil ni entry_decision

- `_revalidate_entry_policy_after_arbitration_change()` solo se invoca desde `_buy_top_symbols()` (`run_trades.py:1065`, `9969`, `1418`).
- El FastPath re-arbitra y puede cambiar SHORT→LONG o venue, pero no reejecuta `entry_profile_gate` ni `evaluate_entry_decision` para el lado/venue nuevo.
- Consecuencia: la entrada puede ejecutarse con perfil A/B/C y decisión del lado anterior.
- **Fix requerido:** tras cualquier cambio de lado/venue en FastPath, correr la misma revalidación de política que el ciclo normal (extraer a helper compartido).

### H4 — Renovación de evidencia incompleta

- El predictor fresco actualiza escalares (`expected_pnl`, probabilidad, tamaño) pero **no** `entry_decision_prediction`, `indicators` ni `range_reversion_prediction` (`staged_entry.py:509`, `1847`; `entry_decision.py:75` lee preferentemente `meta["entry_decision_prediction"]`).
- Consecuencia: la decisión final puede leer la predicción del snapshot anterior.
- **Fix requerido:** la renovación debe sustituir el bloque completo de evidencia (predicción por tesis, indicadores, decisión) o marcar el candidato como no reutilizable.

### H5 — Re-arbitraje bilateral con indicadores viejos

- `_range_scalp_direction_decision()` pasa `precomputed_signal={"indicators": metadata.get("indicators", {})}` para LONG, SHORT y Futures LONG (`run_trades.py` ~8344).
- Precio renovado pero features del snapshot anterior → el re-arbitraje es parcialmente histórico.
- **Fix requerido:** en re-arbitraje, recomputar `indicators` con datos frescos o invalidar el candidato.

### H6 — Tesis opuesta sin predictor específico

- La preparación usa `if support_context ... elif resistance_context`: soporte especializa LONG bounce, resistencia especializa SHORT rejection. La alternativa llega con el predictor genérico (`run_trades.py` ~8390).
- **Fix requerido:** construir ambas tesis explícitamente — `LONG_RESISTANCE_BREAKOUT` y `SHORT_SUPPORT_BREAKDOWN` deben usar `predict_pnl` (momentum/direccional), no `predict_range_reversion` (que devuelve geometría inválida: target hacia la banda media por debajo del precio en breakout). Verificar en código que la rama correcta se invoca.

### H7 — `entry_flags`/`generation` no persistido en live

- `entry_flags` solo existe en `mimic_mode_profile_sweep.py:933`; la persistencia normal de candidatos/auditorías no lleva generación/hash de estrategia.
- Consecuencia: imposible separar `baseline`/`v2_entry_fix`/`s140` en datos live.
- **Fix requerido:** persistir `entry_generation` (o config/strategy hash) en `ready_candidates` y `trades_audit` por cada decisión.

### H8 — Min notional fijo $10 en el contrato

- `opportunity_contract.py` usa mínimo notional hardcoded; Binance aplica filtros dinámicos por símbolo/venue/orden (`MIN_NOTIONAL`, `LOT_SIZE`/`MARKET_LOT_SIZE`, `PRICE_FILTER.tickSize`, `PERCENT_PRICE`).
- Consecuencia: el árbitro puede rankear un contrato que el exchange rechazará — falsos ganadores y bloqueos downstream.
- **Fix requerido:** validar filtros de exchange (snapshot `exchangeInfo` por símbolo) dentro de `build_executable_opportunity` antes de declarar `executable=true`. Fail-closed si los filtros no están disponibles y el símbolo es de un venue live.

## Lo que NO toca este plan

- Lifecycle completo (SSO, PRS, UG, PredExit, trailing, break-even, hard-risk).
- Leverage (queda fijado en 2×), Kelly multipliers, caps de capital.
- `range_scalp_min_stop_atr_multiple`/`ranking_mode`/`rejection_breakout_penalty` ya activos — no cambiar valores en este plan; su calibración va después.
- Producción: ninguno de estos cambios se promueve sin la validación dirigida.

## Plan de implementación (orden estricto)

### Fase 1 — Cerrar bypasses (bloquea cualquier calibración)

1. **Helper único `fresh_arbitrate_and_revalidate()`**: extrae la secuencia completa de revalidación (precio fresco → indicadores frescos → predictor por tesis → perfil → entry_decision → contrato → árbitro) y la usan tanto el ciclo normal como el FastPath. Un solo camino de autoridad.
2. **Pausa DB en watcher**: `watcher`/FastPath consulta `_is_new_entries_paused(db)` antes de ejecutar; si activa → skip con log `PAUSED_DB` auditable.
3. **QUBO filter real**: `get_openable_candidates` aplica `top_symbols_qubo` en SQL; el watcher pasa el conjunto vigente; si no hay conjunto QUBO disponible, `top_symbols_qubo=None` debe significar "sin filtro explícito documentado" y registrarse — mejor: el watcher exige el conjunto y se abstiene si no lo tiene.
4. **Sustitución completa de evidencia** (H4+H5): al re-arbitrar, reemplazar `indicators`, `entry_decision_prediction`, `range_reversion_prediction` y el contrato; si cualquiera no puede renovarse → bloqueo auditable.

### Fase 2 — Simetría de tesis y conformidad de contrato

5. **Predictores por tesis**: mapear `LONG_SUPPORT_BOUNCE`/`SHORT_RESISTANCE_REJECTION` → `predict_range_reversion`; `LONG_RESISTANCE_BREAKOUT`/`SHORT_SUPPORT_BREAKDOWN` → `predict_pnl` (momentum). Eliminar el `if/elif` excluyente.
6. **Filtros Binance en el contrato** (H8): `exchangeInfo` snapshot por símbolo; validar notional mínimo real, qty/stepSize, tickSize dentro de `build_executable_opportunity`.
7. **Generación persistida** (H7): `entry_generation` + config hash en `ready_candidates`, `trades_audit`, `entry_selection_candidates`.

### Fase 3 — Regresiones obligatorias (tests)

- Pausa DB activa mientras el watcher intenta FastPath → no ejecuta.
- Candidata `validated` fuera del QUBO actual → no ejecuta.
- Cambio SHORT→LONG en FastPath → reclasifica perfil A/B/C y reevalúa `entry_decision` del lado nuevo.
- Renovación sustituye `entry_decision_prediction`/`indicators`/contrato completos.
- `LONG_RESISTANCE_BREAKOUT` usa predictor momentum (verificar rama, no solo config).
- Filtros dinámicos Binance dentro del contrato → contrato no ejecutable rankeado igual que uno ejecutable.
- `entry_generation` persistido por decisión.
- Fallo de persistencia post-fill → sin mutación de quantity sin fila de cierre (ya cubierto por anti-ghost, mantener).

### Fase 4 — Después de los fixes (no antes)

- Corrida dirigida Entry A/B en ventanas SHORT (`cal_futures_short_*`, `cal_preservation_*`), mismas seeds que el baseline persistido.
- Grid calibración (solo variables de decisión Entry): `min_stop_atr_multiple {1.0,1.5,2.0}` × `ranking_mode {ror,ev_net}` × `rejection_breakout_penalty {1.0,0.5,0.3}` × `range_scalp_sl_pct {0.8,1.2,1.6}` × `entry_time_stop_hours {4,8,24}`.
- Dedupe por entrada única `(symbol, minuto)` — factor ~7.2× de inflación medido.
- OOS permanece sellado; producción sin tocar.

## Verificación posterior (rol de Devin)

Al recibir aviso: revisar diffs de `staged_entry.py`, `orchestrator.py`, `run_trades.py`, `opportunity_contract.py`, `entry_decision.py`; correr la suite focal; verificar en live que `pause_new_entries` bloquea también el FastPath (test: pausar → observar 1 ciclo watcher → cero submits); revisar que `entry_generation` aparece en `trades_audit`.

## Estado operativo al escribir este plan

- `pause_new_entries` liberado manualmente por Devin (DNS `demo-api.binance.com` resuelto, ping 200). Vigilar rechazos de balance testnet — si reaparecen, la pausa debe activarse y **también cubrir el FastPath** (H1).
- Sweep MIMIC `sm_20260925_130743_6ac94e` corriendo ~123/280; resultados post-s140 son efectivamente generación `v3` (la marca `entry_flags` del sweep no distingue s140 — usar timestamp de fila).

<!-- COPILOT_PROPOSAL_START: 2026-09-28 -->
## Propuesta de implementación de Copilot

### Veredicto

El plan queda aprobado como base de implementación, pero no debe ejecutarse literalmente sin estas precisiones. Los bypasses H1-H8 están confirmados; mientras existan, la calibración Entry mezcla decisiones del árbitro con rutas que no respetan la autoridad bilateral.

El alcance permanece limitado a Entry. No se modifican lifecycle, leverage, Kelly, SL/TP, producción ni valores de calibración.

### Contrato de autoridad s141

Antes de abrir una orden deben existir simultáneamente:

```text
entry_execution_authorized = true
pause_snapshot = leído correctamente y no pausado
qubo_authority = ciclo QUBO vigente + símbolo incluido
fresh_snapshot = precio + indicadores + predicciones por tesis/venue
policy_snapshot = perfil + entry_decision del lado/venue seleccionado
contract_snapshot = contrato redondeado a filtros exchange y todavía ejecutable
capital_snapshot = reserva/JIT autorizado y balance refrescado
```

La ausencia o error de cualquier autoridad bloquea la entrada y persiste una razón canónica. Ningún `None`, excepción de lectura o snapshot anterior se interpreta como autorización.

### Fase 0 — Contratos internos y fuente de autoridad

1. Crear un `FreshEntrySnapshot` interno, sin cambiar todavía el esquema SQL, con:
	- `snapshot_id`, `entry_generation`, `code_hash`, `config_hash`, `model_hash`.
	- precio y timestamp/source.
	- indicadores crudos y formateados frescos.
	- predicciones LONG/SHORT por tesis y venue.
	- `entry_profile`, `entry_decision`, arbitraje, contrato seleccionado y alternativa.
	- `qubo_cycle_id` y `qubo_selected_symbols`/hash del conjunto.

2. Separar la pureza del contrato económico de la lectura de Binance. `opportunity_contract.py` no debe hacer llamadas de red. `run_trades.py` o un proveedor de filtros obtiene un snapshot de `exchangeInfo` por símbolo/venue y lo inyecta en el builder.

3. La generación no se escribirá manualmente como una etiqueta suelta. Se derivará de una versión/hash de Entry, configuración efectiva y modelo, y se persistirá en JSONB/auditoría sin migración si los campos existentes lo permiten.

### Fase 1 — Cerrar ejecución bypass

1. Extraer `fresh_arbitrate_and_revalidate()` como única autoridad usada por ciclo normal y FastPath. Debe:
	- leer pausa DB y autoridad QUBO;
	- obtener precio e indicadores frescos;
	- formar las cuatro tesis/venues aplicables;
	- ejecutar predictor específico por tesis;
	- volver a clasificar A/B/C;
	- recalcular `entry_decision`;
	- construir y redondear contratos;
	- comparar EV neta;
	- devolver snapshot completo o bloqueo.

2. Mover la comprobación de pausa a una utilidad compartida entre `run_trades.py` y `orchestrator.py`. La lectura DB debe ser fail-closed para nuevas entradas:
	- pausa activa: `PAUSED_DB`;
	- lectura fallida: `PAUSE_STATE_UNAVAILABLE`;
	- pausa expirada: reanudar solo después de persistir el evento de auto-resume.

3. Persistir la autoridad QUBO por ciclo. `get_openable_candidates()` debe filtrar por ella cuando se solicita. El watcher no tiene permiso para ejecutar si no recibe un `qubo_cycle_id` vigente y un conjunto de símbolos seleccionado; no se usará `None` como bypass implícito.

4. Reemplazar atómicamente la evidencia anterior. El snapshot fresco debe sustituir, no complementar, `indicators`, `entry_decision_prediction`, `range_reversion_prediction`, side, thesis, venue, selected contract y tamaño. El snapshot original queda únicamente en historial/linaje.

5. Comparar side, thesis, venue, contrato, generación de modelo y config. Si cualquiera cambia, la política se recalcula incluso cuando el side textual siga siendo igual.

### Fase 2 — Simetría de tesis y filtros exchange

1. Eliminar la preparación estructural excluyente. Las rutas deben ser explícitas:

| Tesis | Predictor |
|---|---|
| `LONG_SUPPORT_BOUNCE` | reversión LONG |
| `SHORT_RESISTANCE_REJECTION` | reversión SHORT |
| `LONG_RESISTANCE_BREAKOUT` | continuación LONG |
| `SHORT_SUPPORT_BREAKDOWN` | continuación SHORT |

Cada predicción debe producir target/stop orientados, probabilidad, EV, side, venue y tamaño propios. El árbitro puede abstenerse si una tesis carece de evidencia o contrato, pero no debe inventar la alternativa.

2. Validar filtros Binance antes de declarar un contrato ejecutable:
	- `minQty`, `stepSize`, `MARKET_LOT_SIZE`;
	- `minNotional`/`NOTIONAL`;
	- `tickSize` y redondeo conservador de target/stop;
	- bandas de precio y restricciones por venue.

Después del redondeo se recalculan qty, notional, riesgo, reward, fees y EV. Si faltan filtros para una ruta live, esa ruta queda inexecutable.

### Fase 3 — Regresiones y aceptación

Además de las pruebas de Devin, son obligatorias:

- pausa activa, pausa expirada y error de lectura DB durante watcher: cero submits;
- QUBO ausente, QUBO vencido y candidata fuera del QUBO: cero submits;
- cambio SHORT→LONG, LONG→SHORT y venue sin cambio de side: perfil y `entry_decision` nuevos;
- cambio de features con el mismo side/thesis: snapshot, contrato y target/stop nuevos;
- `entry_decision_prediction` vieja nunca mezclada con predictor fresco;
- breakout/resistance y breakdown/support ejecutan predictor direccional;
- filtros exchange inválidos o ausentes producen `CONTRACT_EXCHANGE_FILTER_BLOCKED`;
- generación/config/model hash persistidos en candidate, check, audit y apertura;
- carrera watcher/ciclo normal: como máximo un submit por `decision_id`;
- fill confirmado con persistencia fallida: auditoría de recuperación completa y ningún `opened` falso.

### Criterio de despliegue

1. Ejecutar la suite focal de Entry y diagnósticos estáticos.
2. Desplegar únicamente `crypto-agent-app_STG` con DB/Redis intactos.
3. Verificar hashes, `/status`, `/control/status`, pausa DB, autoridad QUBO, cero posiciones inesperadas y auditorías.
4. Ejecutar primero un canary con nuevas entradas pausables y observar un ciclo completo.
5. Confirmar que cada intento contiene snapshot fresco, `entry_generation`, `qubo_cycle_id`, contrato y razón final.
6. Solo después de una muestra post-fix estable ejecutar la calibración A/B propuesta por Devin. OOS permanece sellado y producción fuera de alcance.

### Riesgos aceptados y no objetivos

- Puede disminuir temporalmente el número de aperturas porque una candidata sin evidencia fresca o sin QUBO vigente se abstendrá.
- No se relajan EV, probabilidad, ATR, riesgo, capital ni filtros exchange para recuperar cobertura.
- La cuadrícula SL/TP y cualquier cambio de sizing permanecen fuera de esta implementación.

<!-- COPILOT_PROPOSAL_END: 2026-09-28 -->

## Estado de implementación s141 — 2026-09-29

### Fases completadas

- **Fase 0:** snapshot de generación/hash de Entry, autoridad de pausa, autoridad QUBO y contrato de evidencia fresca.
- **Fase 1:** pausa DB fail-closed en ciclo normal y watcher; filtro QUBO real; helper común `fresh_arbitrate_and_revalidate()` usado por ciclo normal y FastPath; sustitución de side/thesis/venue/profile/entry decision/contrato frescos.
- **Fase 2:** preparación bilateral explícita para bounce/rejection y breakout/breakdown; filtros exchange por venue inyectados al contrato; redondeo conservador de precios; validación dinámica de qty/notional/tick; `entry_generation`, `entry_code_hash`, `entry_config_hash` y `entry_model_hash` persistibles en evidencia.
- **Fase 3:** regresiones de pausa/QUBO, revalidación, contratos, generación y pipeline cubiertas por la suite focal.

### Validación de código y despliegue

- Suite focal final: **177 passed, 1 skipped, 15 warnings**.
- Pylance: sin errores en `run_trades.py`, `staged_entry.py`, `orchestrator.py`, `directional_arbiter.py`, `opportunity_contract.py` y `binance_client.py`.
- Solo `crypto-agent-app_STG` fue reiniciado; DB/Redis y producción no se tocaron.
- Host/contenedor tienen hashes coincidentes de los seis módulos actualizados.
- Staging: `RUNNING_FULL`, Binance testnet, salud `ok`, ciclo sin fallos consecutivos, cero posiciones Spot/Futures, logs nuevos sin traceback ni errores de margen.

### Pendiente antes de declarar 100% operativo

- Observar al menos un ciclo completo post-s141 y comprobar en `ready_candidate_checks`/`trades_audit` que:
	- el watcher bloquea con pausa DB;
	- ningún candidato fuera del QUBO vigente llega a submit;
	- cada intento contiene snapshot fresco, `entry_generation`, `qubo_cycle_id` y contrato exchange-validado;
	- un cambio de side/venue deja evidencia de nuevo perfil y `entry_decision`;
	- un fill confirmado queda enlazado a persistencia y auditoría.
- La calibración A/B y el grid SL/TP siguen bloqueados hasta completar ese canary; OOS continúa sellado.

**Conclusión:** implementación de código y despliegue de s141 completados; certificación operativa del plan aún pendiente de la evidencia del primer ciclo post-restart.
