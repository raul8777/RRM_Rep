# Plan de mitigación de la brecha EV esperado–PnL realizado

## Contexto

Los diagnósticos de Super MIMIC sugieren una brecha material entre el PnL contractual proyectado y el resultado realizado. Las cifras siguientes aparecen en el plan original, pero al 2026-09-21 no se localizaron el artefacto primario, hash y consulta reproducible que las originaron; por tanto su estado es `UNVERIFIED_PROVENANCE` y no pueden usarse para promoción ni estimación de ganancias:

```text
PnL contractual proyectado: +$1,039.67
PnL realizado:               -$516.20
Costes proyectados:           $129.09
```

La diferencia no se explica solamente por fees, slippage o funding. Hay descalibración entre:

- Probabilidad estimada.
- Evolución temporal de la tesis.
- Geometría de stop y target.
- Momento y motivo de invalidación.
- Resultado real por ventana y régimen.

Este plan no autoriza cambios de producción. Las diez ventanas OOS permanecen selladas y `apply_allowed=false`.

## Diagnóstico técnico

El contrato calcula actualmente:

```text
EV = p × recompensa − (1 − p) × riesgo − costes
```

La fórmula es correcta para un resultado binario target/stop, pero supone que:

1. `p` está calibrada.
2. Stop y target representan los desenlaces efectivos.
3. La posición permanece abierta hasta uno de esos desenlaces.
4. Lifecycle no introduce otra distribución de salidas.
5. Los costes proyectados representan los costes realizados.

En la práctica, Lifecycle introduce cierres por:

- `belief_exhausted`.
- `sr_path_stalled`.
- `sr_path_invalidated`.
- Trailing.
- Breakeven.
- Time stop.
- Hard SL/TP.
- Liquidación al final de ventana.

Por tanto, el EV binario no representa completamente la distribución real.

El preflight Lifecycle produjo:

```text
sr_path_stalled:      -$265.61
sr_path_invalidated:  -$120.61
hard SL short:         -$99.07
window_end:            -$42.42
belief_exhausted:      +$23.84
```

Esto no demuestra todavía que `belief_exhausted` sea robusto ni que `sr_path_stalled` sea perjudicial. Convierte ambos resultados en hipótesis diagnósticas que deben reconstruirse desde evidencia primaria y probarse con censura y contrafactuales.

Los resultados también prueban que SHORT no es universalmente perdedor:

```text
FUTURES_SHORT 3: resultado muy negativo
FUTURES_SHORT 1, 61 jobs Entry:
  SHORT: +$361.80
  LONG:  -$29.52
```

No debe introducirse una prohibición direccional global.

---

# Objetivo

Descomponer:

```text
EV contractual proyectado − PnL realizado
```

en:

```text
error de probabilidad
+ error de payoff/geometría
+ error temporal
+ efecto Lifecycle
+ error de costes
+ efecto ventana/régimen
```

La mitigación debe preservar:

- Evaluación independiente de LONG y SHORT.
- Misma base económica para ambas direcciones.
- Leverage máximo de `2×`.
- Contratos, cantidades y lineage reconciliados.
- OOS sellado.
- Deployment bloqueado.
- Ausencia del antiguo directional confirmation gate.

---

# Fase 1 — Observabilidad y reconciliación por posición

Antes de ajustar thresholds, registrar cada contrato y cierre mediante `position_id`.

## Registro requerido

```text
window_id
regime_at_entry
regime_at_close
profile
symbol
side
thesis_type

entry_timestamp
exit_timestamp
holding_hours

predicted_probability_raw
predicted_probability_calibrated

expected_target_price
expected_stop_price
expected_reward_usdt
expected_risk_usdt
expected_cost_usdt
expected_net_pnl_usdt

realized_exit_price
realized_gross_pnl
realized_fees
realized_slippage
realized_funding
realized_net_pnl

close_reason
lifecycle_state_at_close
belief_state
sr_path_state
MFE
MAE
target_reached_later
stop_reached_later
```

## Métricas

```text
forecast_error = realized_net_pnl − expected_net_pnl
```

Descomponer en:

```text
probability_error
payoff_error
cost_error
timing_error
lifecycle_intervention_delta
```

## Invariantes

El diagnóstico debe fallar si:

- Falta `position_id`.
- Un cierre no tiene contrato.
- Un contrato tiene múltiples cierres completos.
- La cantidad ejecutada difiere de la cantidad contractual.
- El lado seleccionado, ejecutado y cerrado difiere.
- No pueden atribuirse fees o funding.
- El EV se agrega dos veces por reutilización del símbolo.

---

# Fase 2 — Calibración de probabilidad

## Diagnóstico

Construir tablas independientes por:

```text
LONG / SHORT
SPOT / FUTURES
régimen
perfil A/B/C
thesis_type
horizonte de tenencia
```

Buckets iniciales:

```text
0.50–0.54
0.54–0.58
0.58–0.62
0.62–0.66
> 0.66
```

Métricas por bucket:

```text
predicted_probability_mean
observed_success_rate
trade_count
Brier score
log loss
expected calibration error
PnL neto
profit factor
```

## Definiciones de éxito

Mantener dos objetivos separados:

```text
p_target_before_stop
p_net_profitable_before_timeout
```

No calibrar exclusivamente contra `PnL > 0` ni asumir que alcanzar target es el único éxito válido.

## Mitigación

Usar solamente development:

- Isotonic regression cuando haya muestra suficiente.
- Beta calibration o Platt scaling con muestras menores.
- Shrinkage hacia la media global para cohortes pequeñas.
- No calibrar por símbolo cuando la muestra sea insuficiente.
- Conservar `p_raw` y `p_calibrated` para auditoría.

## Gate

La calibración avanza solo si:

- Disminuye Brier score.
- Disminuye ECE.
- Reduce la diferencia entre EV agregado y PnL realizado.
- No empeora materialmente LONG ni SHORT.
- Funciona en múltiples ventanas.

---

# Fase 3 — Evolución temporal de la tesis

La probabilidad de entrada no debe permanecer implícitamente constante durante toda la posición.

## Curva temporal

Registrar por ciclo:

```text
p(t)
expected_remaining_reward(t)
expected_remaining_risk(t)
remaining_cost(t)
remaining_EV(t)
```

Comparar:

```text
EV_hold(t)
EV_close_now(t)
```

Regla económica candidata:

```text
Cerrar si EV_hold(t) < EV_close_now(t) − uncertainty_buffer
```

Esta lógica no es un gate direccional previo a la entrada; es una comparación económica posterior a la ejecución.

## Features temporales

- Horas desde entrada.
- Progreso hacia target.
- Retroceso desde MFE.
- MAE.
- Velocidad de precio normalizada por ATR.
- Persistencia de `stalled`.
- Velas cerradas sin progreso.
- Cambio de probabilidad.
- Cambio de régimen.
- Coste restante estimado.

## Gate

Debe:

- Reducir pérdidas de cola.
- Evitar cortar excesivamente MFE.
- Mejorar expectancy neta.
- Superar al cierre fijo por tiempo.
- Superar al control sin intervención.

---

# Fase 4 — Recalibración de geometría

## Diagnóstico por trade

```text
target_distance_ATR
stop_distance_ATR
reward_risk_ratio
time_to_target
time_to_stop
time_to_MFE
time_to_MAE
actual_exit_distance_ATR
```

Segmentar por:

- Dirección.
- Régimen.
- Thesis type.
- Perfil.
- Volatilidad.
- Ventana.

## Contrafactuales

Reproducir cada trayectoria con:

```text
A. Contrato original sin Lifecycle
B. Lifecycle actual
C. Solo hard SL/TP
D. belief_exhausted
E. sr_path_invalidated
F. sr_path_stalled
G. Cierre por EV restante
```

## Mitigación

Calibrar geometría con distribuciones observadas:

```text
target = cuantil alcanzable de MFE
stop   = cuantil controlado de MAE
```

Preservar:

- Riesgo económico comparable para LONG y SHORT.
- Costes completos.
- Leverage máximo `2×`.
- Prohibición de mover el stop para acomodar sizing.

---

# Fase 5 — Invalidación y estancamiento

## Hipótesis sobre `sr_path_stalled`

1. Se activa demasiado pronto.
2. Confunde consolidación con invalidación.
3. No normaliza progreso por volatilidad.
4. No exige persistencia suficiente.
5. Cierra pérdidas antes de una recuperación.
6. Se comporta de forma distinta en LONG y SHORT.

## Estados propuestos

```text
on_track
slow_progress
stalled_unconfirmed
stalled_confirmed
invalidated
target_reached
```

Política candidata:

```text
slow_progress:
  HOLD

stalled_unconfirmed:
  HOLD o endurecer protección

stalled_confirmed:
  cerrar solo si EV restante <= 0

invalidated:
  cierre completo si la ruptura estructural está confirmada
```

## Persistencia

Evaluar:

```text
1, 2, 3 y 4 velas cerradas consecutivas
```

Reiniciar el contador si reaparece progreso.

## Contrafactual obligatorio

Para cada cierre `stalled` o `invalidated`, continuar virtualmente y medir:

```text
PnL real del cierre
PnL a 1 vela
PnL a 2 velas
PnL a 4 velas
PnL al target/stop natural
MFE sacrificado
drawdown evitado
```

Clasificar cada intervención como:

- Pérdida evitada.
- Ganador cortado prematuramente.
- Cierre sin efecto.
- Invalidación tardía.

---

# Fase 6 — Resultado por ventana

Analizar jerárquicamente:

```text
global
→ régimen
  → ventana
    → dirección
      → thesis_type
        → razón de cierre
```

Métricas:

- Media y mediana entre ventanas.
- Peor ventana.
- Ventanas rentables.
- Contribución de la mejor ventana.
- Dispersión.
- Intervalo de confianza.
- Leave-one-window-out.
- Sensibilidad a operaciones extremas.

Un candidato no avanza si su PnL global es positivo pero:

- Depende de una sola ventana.
- Se vuelve negativo al eliminar su mejor ventana.
- Tiene pérdida material en el intervalo de confianza.
- La mejora se debe únicamente a mayor exposición.

---

# Fase 7 — EV multicierre

Reemplazar progresivamente el modelo binario por:

```text
EV =
  p_target × payoff_target
+ p_stop × payoff_stop
+ p_belief × payoff_belief
+ p_stalled × payoff_stalled
+ p_invalidated × payoff_invalidated
+ p_timeout × payoff_timeout
− costes esperados
```

Con:

```text
Σ probabilidades = 1
```

Inicialmente usar frecuencias con shrinkage por:

- Régimen.
- Dirección.
- Thesis type.
- Geometría.
- Perfil.

Gate contractual:

```text
multiexit_expected_net_pnl > 0
```

No basta con que el EV binario sea positivo.

---

# Fase 8 — Secuencia experimental

## Etapa 1: instrumentación

Usar:

- Los 61 jobs preservados de la ronda detenida.
- Preflights existentes.
- Resultados A+C conforme aparezcan.
- Solo ventanas development.

## Etapa 2: replay contrafactual

Reutilizar trayectorias existentes para comparar:

- Sin Lifecycle.
- Hard SL/TP.
- `belief_exhausted`.
- `stalled` actual.
- `stalled` persistente.
- `invalidated`.
- EV restante.

## Etapa 3: calibración

- Walk-forward dentro de development.
- La ventana evaluada no calibra su propio modelo.
- Leave-one-window-out.
- LONG y SHORT independientes.
- Mismos criterios económicos.

## Etapa 4: micro-preflight

```text
6 ventanas
10–20 paquetes
100 ciclos
máximo 12 símbolos
3 workers
apply_allowed=false
```

## Etapa 5: integración

Incorporar al siguiente E2E únicamente mecanismos que superen sus controles contrafactuales.

## OOS

No abrir las diez ventanas OOS hasta congelar:

1. Calibrador de probabilidad.
2. Geometría.
3. Política Lifecycle.
4. Candidatos finales.
5. Resultados development.

---

# Criterios de éxito

## Probabilidad

```text
ECE menor que baseline
Brier score menor
|EV proyectado − realizado| reducido al menos 40%
```

## Lifecycle

```text
expectancy superior al control sin Lifecycle
tail loss menor
MFE sacrificado dentro del límite
profit factor > 1
```

## Estabilidad

```text
PnL agregado > 0
mediana por ventana > 0
mínimo 8/14 ventanas rentables
resultado positivo leave-one-window-out
sin dependencia de una sola operación
```

## Contrato

```text
cantidades reconciliadas
costes realizados atribuibles
selected_side = executed_side = closed_side
leverage <= 2×
cero cierres sin contrato
```

---

# Rollback y seguridad

- Mantener la política actual como baseline inmutable.
- Ejecutar mitigaciones primero en replay y MIMIC.
- No aplicar cambios automáticamente al runtime.
- Cada variante debe tener fingerprint y artifact independiente.
- Si falla cualquier gate, conservar baseline y descartar la variante.
- No ajustar parámetros después de observar OOS.
- No reintroducir confirmation gates direccionales.

---

# Revisión adversarial multimodo y de trazabilidad — 2026-09-21

## Veredicto de implementación

El diseño conceptual es válido, pero la implementación futura debe permanecer bloqueada hasta resolver los P0 de esta sección. En el código actual es posible obtener un artefacto técnicamente `completed` que no represente el modo solicitado, mezcle unidades económicas, use una cohorte sintética, trate censura como pérdida y tenga rollback inefectivo. Cualquiera de esos defectos puede fabricar una mejora estadística o aplicar una configuración imposible de atribuir.

El resultado que se persigue no es maximizar EV proyectado, sino mejorar PnL neto realizado y reducir su error contra un forecast versionado:

```text
net_incremental_pnl
= pérdidas evitadas
+ ganancias adicionales capturadas
- ganancias sacrificadas
- fees, slippage, funding, borrow interest y costes de transición
```

Ninguna cifra de ganancia futura se publicará sin intervalo de confianza, baseline contemporáneo, denominador y cobertura de la matriz definida abajo.

## Universo completo que debe cubrirse

### Modos de régimen

```text
SPOT_BULL
RANGE_SCALP
PRESERVATION
FUTURES_SHORT
```

### Modos de ejecución

```text
SPOT_LONG
FUTURES_LONG
FUTURES_SHORT
SPOT_MARGIN_SHORT
ABSTAIN
```

`SPOT_MARGIN_SHORT` es un modo de ejecución, no un quinto régimen. `ABSTAIN` no produce una posición, pero debe conservar contrato alternativo, razones y coste de oportunidad para detectar sobrebloqueo. La implementación debe mantener separados `regime_mode`, `selected_execution_mode`, `trade_type` y `side`; nunca inferir uno de otro mediante strings libres.

### Perfiles

```text
A
B
C
```

Los perfiles son por operación, no un estado global. Debe persistirse `profile_at_signal`, `profile_at_arbitration`, `profile_at_fill` y la fuente de asignación. Un `peak_quality_profile` explícito no podrá saltarse silenciosamente los thresholds: el bypass, si sigue siendo contractual, debe quedar tipado, versionado y medido como cohorte independiente.

### Matriz obligatoria de cobertura

Cada experimento y reporte debe producir una fila por:

```text
regime_mode
× selected_execution_mode
× profile A/B/C
× side LONG/SHORT
× thesis_type
× close_cause
× window/changepoint
```

Las combinaciones no soportadas deben figurar como `NOT_APPLICABLE` con la regla que las excluye. Las soportadas sin muestra serán `INSUFFICIENT_DATA`, nunca cero. `ABSTAIN` se mide por oportunidades rechazadas, PnL contrafactual y razón; los demás modos por posiciones y fills. No se permite ocultar una regresión de una celda mediante el agregado global.

## Contrato de trazabilidad de punta a punta

La implementación reutilizará las identidades existentes de las migraciones 025/026 y las endurecerá. No se creará un identificador paralelo sin necesidad.

```text
suite_id / experiment_id / stage_id / job_id
window_id / data_snapshot_hash / code_commit
ready_candidate_id / signal_id
thesis_id / position_id / close_group_id
entry_order_id / exit_order_id / exchange_trade_id
contract_id / contract_version / contract_hash
model_id / calibration_id / config_fingerprint
```

### Reglas de identidad

1. `position_id` identifica una posición económica; `thesis_id` identifica la tesis y `close_group_id` agrupa fills de cierre.
2. Múltiples fills no son múltiples tesis ni múltiples éxitos. El PnL se agrega primero por posición/tesis.
3. Toda relación crítica debe tener columna tipada, constraint o tabla puente; metadata JSONB es copia de auditoría, no clave primaria.
4. El contrato congelado al arbitraje y el contrato revalidado pre-submit se guardan por separado con hashes. El fill no sobrescribe ninguno.
5. Cada valor económico lleva `value`, `currency`, `unit`, `basis` y `source`. Como mínimo se distinguen USD/USDT, porcentaje decimal/porcentaje 0–100, margin/notional y bruto/neto.
6. Los timestamps son UTC aware y se separan `observed_at`, `computed_at`, `decision_at`, `submitted_at`, `filled_at` y `persisted_at`.
7. La procedencia incluye query/script versionado, inputs, hashes, row counts, exclusiones y razón de exclusión.

### Estados mínimos del forecast

```text
p_raw
p_calibrated
probability_target_definition
expected_gain_distribution
expected_loss_distribution
expected_cost_breakdown
expected_net_pnl_usdt
expected_return_on_risk
forecast_horizon
model/config/data fingerprints
```

No se reutilizará `expected_pnl` sin unidad. Los nombres canónicos serán, como mínimo, `expected_pnl_usdt` y `expected_return_pct`; los aliases legacy solo se aceptarán mediante adaptadores que validen rango y fuente.

## Protocolo obligatorio de excepciones del predictor

El retorno `valid=false` es una barrera de seguridad, no la solución definitiva. Toda excepción del predictor abre un incidente de datos/modelo y debe terminar en la reparación de la causa propietaria. No se considera resuelto un caso únicamente porque la oportunidad haya sido bloqueada.

### Taxonomía inicial de causas

Cada fallo se clasifica en una etapa y una causa estable, sin depender del texto libre de Python:

```text
INPUT_CONTRACT
  missing_required_field
  invalid_type
  invalid_range
  unit_mismatch
  non_finite_value
  stale_or_misaligned_timestamp

LEARNED_WEIGHTS
  missing_or_corrupt_weights
  invalid_weight_type_or_range
  incompatible_model_schema
  database_read_failure

INDICATORS
  malformed_indicator_shape
  missing_nested_value
  contradictory_support_resistance
  invalid_price_or_atr

OHLCV
  unequal_array_lengths
  empty_or_insufficient_window
  NaN_or_infinite_candle
  non_monotonic_timestamp
  invalid_high_low_close

PROBABILITY
  calculation_domain_error
  non_finite_probability
  probability_out_of_range

PRICE_RANGE
  invalid_or_inverted_range
  support_resistance_clamp_inversion
  numeric_overflow

EXPECTED_PNL
  invalid_notional_or_price
  unit_or_side_mismatch
  non_finite_expected_pnl

COST_MODEL
  unavailable_commission_config
  unsupported_trade_type
  invalid_fee_slippage_funding

RISK_METRICS
  invalid_stop_target_geometry
  non_finite_risk_metric

INTERNAL
  unexpected_programming_error
```

La taxonomía puede ampliarse, pero nunca se reemplaza por `unknown` permanente. Un error inicialmente `INTERNAL` debe reclasificarse después del diagnóstico.

### Instrumentación mínima por fallo

Persistir un evento `predictor_failed` con:

```text
error_id
error_stage
error_code
exception_class
sanitized_message
stack_fingerprint
symbol
regime_mode
execution_mode
trade_type
requested_side
profile_source/profile_at_signal
model_id/model_schema_version
weights_id/weights_hash
input_schema_version/input_fingerprint
market_data_snapshot_id
config_fingerprint
observed_at/computed_at
correlation_id/signal_id/ready_candidate_id
```

El evento no guarda secretos ni payloads completos. Debe adjuntar un fixture mínimo saneado y reproducible o una referencia inmutable al snapshot de entrada. Los campos sensibles se redactan antes de persistir. `stack_fingerprint` agrupa recurrencias sin perder el traceback completo en el sistema de errores autorizado.

### Prevención antes del cálculo

1. Validar un schema tipado en la frontera del predictor: precio y notional positivos, side y trade type soportados, probabilidad/unidades conocidas, timestamps UTC y valores numéricos finitos.
2. Validar OHLCV como conjunto: longitudes iguales, orden temporal, ventana mínima, precios positivos y `low <= open/close <= high`.
3. Validar indicadores y S/R antes de acceder a estructuras anidadas; distinguir `missing`, `not_applicable` y cero real.
4. Validar pesos/modelo al cargarlos y poner en cuarentena versiones corruptas o incompatibles. El fallback a pesos default solo se permite si está declarado como `bootstrap_mode`, no por excepción silenciosa.
5. Validar la salida de cada etapa (`probability`, `price_range`, `expected_pnl`, costes y risk metrics) antes de alimentar la siguiente. `NaN`, infinito, geometría invertida o unidad ambigua fallan en la etapa propietaria.
6. Sustituir progresivamente el `except Exception` monolítico por errores tipados y contexto de etapa. El boundary exterior conserva un último fail-closed para errores inesperados, pero nunca oculta su traceback o procedencia.

### Flujo de resolución de causa raíz

```text
predictor_failed
→ bloquear oportunidad y preservar contrato alternativo/ABSTAIN
→ agrupar por stack_fingerprint + input_schema + model/config hash
→ reproducir con fixture saneado
→ identificar productor propietario del dato o etapa defectuosa
→ corregir productor, parser, schema, modelo o cálculo
→ añadir regresión con el fixture mínimo
→ ejecutar matriz régimen × ejecución × A/B/C × LONG/SHORT afectada
→ verificar ausencia de recurrencia en staging
→ cerrar incidente con evidencia y versión reparada
```

No se corrige una causa rellenando el campo con cero, `0.5`, una geometría genérica o capturando una excepción más abajo. Cuando el dato es obligatorio se repara el productor; cuando es opcional se define semántica explícita de ausencia y comportamiento contractual.

### SLO y gate de cierre

- Cobertura de eventos: 100% de excepciones generan `predictor_failed` y una razón de rechazo enlazada.
- Tasa de error: cero excepciones conocidas recurrentes en la ventana mínima de staging definida para la versión; cualquier recurrencia reabre el incidente.
- Causas `INTERNAL`: cero pendientes para promoción.
- Reproducción: 100% de causas promovidas a fix tienen fixture y test de regresión.
- Integridad: ningún payload inválido alcanza profile gate, arbitraje, staged entry, sizing o ejecución.
- Recuperación: una entrada válida equivalente sigue produciendo forecast válido; el fix no puede convertir el fallo en sobrebloqueo general.

El gate económico permanece bloqueado mientras exista una familia de excepción recurrente sin causa raíz reparada, aunque todas sus oportunidades hayan sido rechazadas de forma segura.

## Registro de gaps y bugs previsibles

### P0 — bloquean cualquier certificación o deploy

1. **Modo solicitado no controla necesariamente el sweep.** `DesignSpec.mode` existe, pero `run_sweep()` solo transmite `forced_regime`; si este es nulo, un experimento etiquetado con un modo puede observar otro. Corrección: resolver un único `requested_regime`, pasarlo obligatoriamente y comparar distribución observada. Test: cada uno de los cuatro modos debe observar el régimen solicitado dentro de tolerancia preregistrada o fallar `REGIME_MISMATCH`.
2. **Lifecycle hardcodeado a `RANGE_SCALP`.** `_base_policy()` y `MarketContext` fijan ese régimen, aunque el diseño declare otro. Corrección: propagar régimen y perfil desde el job/cohorte; prohibir defaults silenciosos. Test parametrizado 4 regímenes × 3 perfiles.
3. **Fallback sintético contaminando certificación.** Sin cohorte Entry, lifecycle usa hasta cinco símbolos y alterna LONG/SHORT por índice. Esto inventa lado, entrada y notional. Corrección: en certification/economic mode, ausencia o pérdida de cohorte es error; el fallback sintético se permite solo en smoke tests rotulados `SYNTHETIC_NON_ECONOMIC`.
4. **Unidad incorrecta en expected-gain TP.** El replay guarda un precio absoluto en `expected_peak_return`, mientras `ExitEngine` lo compara como retorno fraccional y además vuelve a multiplicar por `expected_gain_mult`. Corrección: schema inequívoco (`expected_peak_return_pct` o `expected_peak_price`) y conversión única. Test LONG/SHORT con target conocido.
5. **Liquidación de fin de ventana tratada como cierre económico.** `_replay_policy()` calcula PnL y win/loss para `window_end`. Corrección: outcome `RIGHT_CENSORED`, excluir de PF/WR/calibración y reportar MTM por separado. Tests de invariancia al extender el fin administrativo.
6. **Rollback capturado después de aplicar.** El endpoint aplica `_apply_kelly_config_patch()` y luego `apply_recommended_config()` lee el runtime para construir `rollback_payload`; puede guardar la configuración nueva como rollback. Corrección: snapshot transaccional previo, persistirlo antes del apply, verificar fingerprint posterior y compensar si falla auditoría. Test E2E old → new → rollback = old exacto.
7. **Gate económico insuficiente.** `DeployGate` puede aprobar con tres trades y no exige provenance, régimen, censura, PF/Sharpe válidos, OOS sellado, estabilidad, matriz completa ni aprobación humana. Corrección: gate compuesto fail-closed con certificaciones técnica/económica/OOS/gobernanza independientes. Cualquier `null`, `unknown` o cobertura faltante bloquea.
8. **Contrato no siempre se recalcula con precio fresco.** La revalidación pre-submit ocurre solo si el candidato esperó por capital/slots y una distancia de riesgo cero retorna `ZERO_RISK_DISTANCE_FAIL_OPEN`. Corrección: reprice y reconstrucción completa siempre antes de submit; cero riesgo, precio stale o feed divergente fallan cerrado.
9. **Unidades incompatibles bajo `expected_pnl`.** El predictor produce USDT; discovery puede guardar distancia porcentual bajo el mismo nombre; profile scoring multiplica ese campo como magnitud homogénea. Corrección: migrar a campos tipados y rechazar aliases ambiguos. Test metamórfico: escalar notional cambia USDT, no porcentaje ni perfil estructural.
10. **Costos incompletos de SPOT margin.** `_contract_costs()` trata `spot_margin` como SPOT y no modela borrow interest, repay, duración ni comisiones específicas. Corrección: modelo de costes por venue/instrumento, con interés dependiente del tiempo y verdad de transacciones margin. Sin cobertura, `SPOT_MARGIN_SHORT` no puede certificarse económicamente.
11. **Unicidad de candidatos sin lado.** El índice activo usa `(symbol, trade_type, regime_mode)`; LONG y SHORT del mismo símbolo/venue/régimen pueden colisionar o sobrescribirse conceptualmente. Corrección: incluir execution mode/side/thesis identity y migrar con detección previa de duplicados.
12. **Sweep/lifecycle SPOT_BULL aplicado a otros modos.** El grid contiene parámetros `max_positions_spot_bull` y los adaptadores construyen `spot_bull_config_from_params`; esto puede simular otros regímenes con política equivocada. Corrección: factories y grids explícitos por régimen, con reporte de parámetros activos/inertes.

### P1 — pueden sesgar ganancias, cohortes o atribución

13. **Selección de perfil incorrecta.** `_best_recommendation()` devuelve la primera recomendación si no encuentra el perfil solicitado. Debe devolver `NO_CANDIDATE_PROFILE_MISMATCH`.
14. **`design.profile` y `design.profiles` pueden divergir.** El CLI solo recibe `--profile` cuando `profiles` tiene exactamente un elemento; artifacts y UI pueden etiquetar un perfil distinto al barrido. Debe existir una lista canónica y cada resultado debe declarar perfil efectivo.
15. **Win rate usa escalas distintas.** Lifecycle devuelve 0–100; otros componentes y UI esperan 0–1 y multiplican por 100. Normalizar almacenamiento a fracción `[0,1]`, validar rango y formatear solo en presentación.
16. **Sampling no estratificado por defecto.** Un cap pequeño puede eliminar modos/perfiles/ventanas y aun generar recomendación. En certificación, sampling debe estratificar por todos los ejes y publicar coverage ratio; una celda requerida vacía bloquea.
17. **Jobs no equivalen a muestra independiente.** Variantes reutilizan posiciones y ventanas. Reportar `n_jobs`, `n_positions_unique`, `n_theses_unique`, `n_windows_independent` y effective sample size; los IC deben agrupar por tiempo/posición.
18. **Intrabar insuficiente.** Window certification acepta fidelidad `low`; con velas 1h no se conoce el orden stop/target y el simulador adopta stop-first. Lifecycle/ejecución no puede certificarse económicamente sin 1m/5m o análisis de bounds conservador.
19. **Universo lifecycle truncado.** `_load_window()` usa los primeros cinco símbolos, no una selección estratificada y documentada. Puede ocultar concentración y cambiar con el orden del manifest.
20. **Costos potencialmente inconsistentes.** El predictor descuenta comisión round-trip y el contrato vuelve a construir fees desde geometry. Aunque el contrato no reutiliza `pred.expected_pnl`, reportes que comparen ambos pueden interpretar una doble resta o bases diferentes. Persistir `gross_model_ev`, `cost_model_version` y `net_contract_ev` y reconciliar la transformación.
21. **Bias multiplicativo sobre EV neto.** `bias_correction` multiplica ganancias y pérdidas netas después de fees; un factor aprendido puede distorsionar costos. Calibrar probabilidad/payoffs por separado o aplicar corrección antes del coste fijo.
22. **Fallback probabilístico no empírico.** Discovery usa el threshold mínimo del régimen como “probabilidad” cuando `n < 20`. Debe marcarse `prior/fallback`, no observación calibrada, y no entrar como label independiente.
23. **Excepciones del predictor contenidas pero no reparadas.** El fail-closed evita operar con `p=0.5`, pero un `except Exception` monolítico puede seguir ocultando datos mal tipados, pesos corruptos, OHLCV inconsistente, geometría inválida o fallos de costes/risk metrics. Aplicar el protocolo obligatorio anterior: etapa y código tipados, fixture reproducible, reparación en el productor propietario, regresión y ausencia de recurrencia en staging. Bloquear promoción ante cualquier familia recurrente no resuelta.
24. **Profile override salta evaluación.** `peak_quality_profile` retorna antes de todos los thresholds. Persistir `profile_assignment_source`; medir override y classifier por separado; validar que el override procede de modelo certificado.
25. **Identidades sin integridad referencial completa.** Migraciones 025/026 agregan IDs e índices, pero no enlazan todas las órdenes/fills/cierres mediante FK o constraints de cardinalidad. Añadir auditor de huérfanos antes de imponer constraints gradualmente.
26. **Una posición puede producir varias filas de cierre.** La unicidad de `idempotency_key` evita duplicados exactos, no garantiza una sola tesis económica. Agregar por `position_id/close_group_id`; validar suma de qty cerrada, fees y PnL antes del label.
27. **Scale del contrato no contempla filtros de exchange.** `scale_contract()` escala qty linealmente pero no tick/step size, min notional, fill VWAP ni nuevo riesgo tras redondeo. Crear `submitted_contract` y `filled_contract` derivados, nunca sobrescribir el locked contract.
28. **Funding/interest dependen del tiempo.** Un único `funding_bps` al entry no representa número de cobros, signo ni duración; el EV temporal debe modelar schedule y reconciliar cargos reales.
29. **Cambio de régimen durante la posición.** Segmentar solo por entry o close genera atribución contradictoria. Persistir trayectoria/changepoints y preregistrar si el estimando es policy-at-entry o policy-dynamic.
30. **Causas de cierre no mutuamente excluyentes.** Prioridades del motor eligen una causa aunque varias se activen. Persistir `eligible_close_causes`, `winning_priority` y política; competing risks usa causa observada y análisis de sensibilidad.
31. **Hard-risk y soft-exit pueden competir.** Una orden FOK pendiente no debe bloquear MARKET de emergencia ni causar doble close. Requiere state machine/idempotencia por `close_group_id`, cancelación confirmada y reconciliación de fills parciales internos aunque la decisión sea full-size.
32. **Leakage temporal en pesos/features.** Pesos aprendidos y datos externos deben tener `trained_through` anterior a cada decisión. El hash del parquet no basta si contiene observaciones futuras.

### P2 — observabilidad, operación y gobernanza

33. Dashboard y API pueden discrepar por aliases, escala de WR y fallback frontend. El frontend no calcula gates.
34. `completed`, `certified`, `economically_certified`, `oos_passed`, `approved` y `deployed` deben ser estados distintos y monotónicos.
35. Cache/resume necesita incluir código, dataset, régimen, perfil, modelo de costes y schema version; de lo contrario reutiliza jobs stale.
36. Cancelación o crash debe cerrar manifest como incompleto; ninguna etapa downstream consume resultados parciales.
37. El metamodelo no debe considerar “winner” un deploy técnico con muestra mínima ni aprender de un rollback.
38. El rollback por merge puede dejar campos nuevos no presentes en el snapshot viejo. Debe restaurar el documento efectivo completo o ejecutar un inverse patch verificado.
39. Todos los cambios de runtime requieren actor autenticado, razón, ticket/evidence IDs y separación entre quien propone y quien aprueba.
40. Retención y redacción deben conservar hashes y métricas sin almacenar secretos, API keys o payloads sensibles en artifacts/logs.

## Arquitectura de gates revisada

```text
G0 provenance + schema + suite lineage
G1 mode/profile/execution coverage
G2 accounting and identity reconciliation
G3 censoring and label validity
G4 predictor root-cause closure + technical reproducibility and parameter sensitivity
G5 economic sufficiency and baseline improvement
G6 temporal/OOS validation
G7 shadow/staging reconciliation
G8 manual approval + atomic deploy/rollback
```

Cada gate devuelve `PASS`, `FAIL` o `INSUFFICIENT_DATA`; solo `PASS` avanza. No existe conversión automática de `INSUFFICIENT_DATA` a warning.

## Pruebas bloqueantes añadidas

1. Matriz parametrizada de cuatro regímenes × tres perfiles × modos de ejecución soportados; verifica requested/observed, side, venue, costos y config efectiva.
2. Contract tests para `SPOT_LONG`, `FUTURES_LONG`, `FUTURES_SHORT`, `SPOT_MARGIN_SHORT` y `ABSTAIN`, incluyendo simetría LONG/SHORT y leverage máximo 2x.
3. Test de unidad/currency que rechaza USDT en campos pct, porcentajes 0–100 en probabilidades y margin confundido con notional.
4. Test de lifecycle sin cohort path que debe fallar en certification mode; synthetic fallback solo pasa como smoke no económico.
5. Test expected peak LONG/SHORT que alcanza exactamente el target y cierra una vez.
6. Test de censura: mover `window_end` sin evento natural no cambia WR/PF ni labels, solo MTM/exposure.
7. Test de rollback atómico con fallo inyectado antes/después de apply y audit; el fingerprint final siempre es old o new, nunca mezcla.
8. Test de pre-submit reprice en rutas inmediata y demorada; stale/zero-risk/feed mismatch bloquean.
9. Test de colisión simultánea LONG/SHORT para el mismo símbolo en RANGE_SCALP y migración de candidatos existentes.
10. Test de fills múltiples/duplicados: una tesis, qty reconciliada, PnL/fees exactos e idempotencia tras replay.
11. Test SPOT margin con borrow, interés acumulado, repay parcial/final, fees y cierre de emergencia.
12. Test stop y target dentro de la misma vela con bounds; low-fidelity no puede producir economic certification.
13. Test all-negative/null/one-trade/profile-mismatch que devuelve `NO_CANDIDATE`.
14. Test cache poisoning: cambiar commit, data hash, cost model o regime invalida resume.
15. Test frontend/API: UI solo refleja gate backend y nunca habilita deploy con estado incompleto.
16. Golden test desde fills de Binance hasta waterfall EV-realized y dashboard, con tolerancia contable definida.
17. Tests parametrizados del predictor para campos ausentes, tipos/unidades inválidos, `NaN`/infinito, precio/notional no positivo, side/trade type desconocido y timestamps stale; cada caso produce `valid=false`, `error_stage/error_code` estable y evento enlazado.
18. Tests OHLCV para longitudes desiguales, ventana insuficiente, timestamps desordenados y OHLC imposible; ninguno alcanza cálculo de probabilidad.
19. Tests de pesos aprendidos corruptos, schema incompatible y fallo de lectura; verifican cuarentena/versionado y que bootstrap explícito no se confunda con recuperación de error.
20. Failure injection por etapa (`probability`, `price_range`, `expected_pnl`, `cost_model`, `risk_metrics`) para comprobar trazabilidad, redacción de secretos y preservación del traceback/fingerprint.
21. Test de reparación por fixture: reproduce cada familia real detectada, demuestra que falla antes del fix, pasa después y conserva forecasts válidos equivalentes en los cuatro regímenes, modos soportados, A/B/C y ambos lados.
22. Test de recurrencia/SLO en staging: ninguna familia marcada como reparada reaparece; `INTERNAL`, evento sin fixture o excepción sin clasificación bloquean economic certification.

## Criterio económico y de cobertura final

Una candidata solo puede declarar mejora si:

- mejora PnL neto incremental contra baseline concurrente y su IC preregistrado excluye el umbral mínimo de materialidad;
- reduce error EV-realized sin conseguirlo mediante simple reducción desproporcionada de exposición;
- no empeora drawdown, expected shortfall, concentración ni costes de ejecución fuera de tolerancia;
- mantiene resultados aceptables en cada celda material de régimen/ejecución/perfil, o declara explícitamente `INSUFFICIENT_DATA` y no aplica allí;
- demuestra estabilidad por ventana/changepoint, leave-one-window-out y OOS sellado;
- reconcilia el 100% de las posiciones incluidas con fills, cantidades, fees, funding/interés e identidades;
- tiene rollback atómico probado y aprobación manual trazable.

El rollout puede ser segmentado: una candidata aprobada para `RANGE_SCALP × SPOT_LONG × B` no queda aprobada para otros modos o perfiles. El runtime debe aplicar exactamente el scope certificado y usar el baseline vigente fuera de él.

---

# Orden de implementación recomendado

1. Instrumentar reconciliación por posición.
2. Construir contrafactuales Lifecycle.
3. Analizar `sr_path_stalled` e `invalidated`.
4. Calibrar probabilidades por régimen y dirección.
5. Recalibrar stop/target mediante MFE/MAE.
6. Introducir EV multicierre.
7. Validar con leave-one-window-out.
8. Ejecutar micro-preflight.
9. Integrar solamente mejoras demostradas.
10. Mantener OOS sellado y deployment bloqueado.

## Prioridad inmediata

No comenzar cambiando thresholds. Primero construir la atribución por posición y los contrafactuales. Sin esa capa, cualquier modificación de probabilidad, ATR, `stalled` o `invalidated` mezclaría causas y podría mejorar una ventana mientras empeora silenciosamente otras.

---

# Registro de implementación y certificación staging — 2026-09-26

## Cambios desplegados

- Predictor fail-closed con `valid=false`, `prediction_status=error`, etapa, código, clase de excepción, mensaje saneado y `stack_fingerprint` estable.
- Validación preventiva de precio, notional, horizonte, side, tipos numéricos, finitud y geometría/longitud OHLCV.
- Procedencia explícita del fallback de pesos y logging `predictor_weights_fallback`.
- `pnl_service.validate()` rechaza explícitamente toda predicción con `valid=false`.
- Productor inicial y periódico de `dashboard_summary_materialized_1d/7d`; los requests stale disparan refresh no bloqueante.
- Health y reconciliación SPOT usan `walletBalance` como la misma fuente de verdad.
- Stablecoins de caja (`USDC`, `BUSD`, `FDUSD`, `TUSD`) quedan fuera de la detección de posiciones huérfanas.
- Logging SR Pool tolera score ausente sin interrumpir el ciclo.
- En staging, el user-data WebSocket autenticado queda deshabilitado y se conserva polling canónico porque Binance testnet rechazaba ese canal con `-2015` aunque REST funcionaba.
- Migración `027_ready_candidates_execution_identity.sql` aplicada a staging.

## Evidencia de schema

```text
ready_candidates migradas: 7059
columnas execution_mode/side NOT NULL: 2/2
constraints de identidad: 2/2
índices requeridos: 2/2
filas con identidad nula: 0
colisiones activas derivadas: 0
identidades derivadas inválidas: 0
```

Backup previo verificado: `S:\Tools\AI\staging_pre_027_20260926.dump`.

## Evidencia ejecutable

```text
suite bloqueante: 98 passed
errores del editor en archivos modificados: 0
environment: staging
Binance: testnet
agent mode: auto
health overall: ok
trade cycle: completed
consecutive failures: 0
control: RUNNING_FULL
can_open_new_entries: true
position sync: ok
spot parity: ok, mismatch_count=0
API principal /dashboard/summary: disponible
API read-only :8002/dashboard/summary: disponible
```

## Resultado del gate

`STAGING_OPERATIONAL_CERTIFICATION=PASS` para el código y schema desplegados en esta ejecución.

Este PASS certifica arranque, schema, contratos locales, ciclo runtime, health, materialización del dashboard y reconciliación de staging. No equivale a `ECONOMIC_CERTIFICATION`, no abre OOS y no autoriza producción. G5–G8 continúan sujetos a suficiencia económica, validación temporal/OOS, ventana de observación de excepciones del predictor y aprobación manual.

Observación no bloqueante de staging: el mantenimiento automático de BNB intentó comprar con saldo spot USDT insuficiente (`-2010`) y desactivó el descuento BNB para ese ciclo. No produjo fallo de ciclo, divergencia de posiciones ni error de health; debe tratarse como limitación de fondos del entorno testnet, no como evidencia económica.
