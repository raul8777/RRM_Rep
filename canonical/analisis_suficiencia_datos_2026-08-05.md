---
title: "Analisis de Suficiencia de Datos para Aperturas y Lifecycle"
type: canonical-memory
status: proposal
created_at: 2026-08-05
related:
  - canonical/calibrar_datos.md
  - canonical/calibracion_entradas-a20ab8-v3.md
  - canonical/calibrar-ciclo-vida-52fe5d.md
  - .windsurf/rules.md
---

# 1) Objetivo

Evaluar si la informacion que estamos guardando hoy es suficiente para:

- mejores aperturas,
- mejor ciclo de vida,
- mayores ganancias y menores perdidas,
- sin romper canones de calibracion_entradas, calibrar_datos y calibrar-ciclo-vida.

Este documento complementa el plan de retencion/purga con una matriz de suficiencia y faltantes.

## Actualizacion operativa (s116, 2026-08-05)

Se implemento observabilidad directa en dashboard para esta misma brecha de suficiencia:

- `/metrics/shadow-readiness` ahora incluye el componente `entry_close_lineage_instrumentation`.
- Se miden gates de cobertura para:
  - `decision_features_v1` en checks staged,
  - linaje `ready_candidate_id` en `trades_closed`,
  - contexto de entrada (`staged_direction`, `profile_at_entry`, `regime_at_entry`),
  - costo neto (`commission_bps_total`) y hints de friccion (`spread/slippage/funding`).

Implicacion: esta brecha pasa de "diagnostico estatico" a "control operativo continuo" visible en Evidencia Shadow.

## Actualizacion operativa (s121b, 2026-08-07)

Se completó y verificó en runtime la reorganización de Evidencia Shadow bajo estrategia padre SR:

- `/metrics/shadow-readiness` expone `strategy_groups` con grupo `sr_strategy`.
- El grupo SR se publica como `Estrategia de SR` y agrega sus subtareas/componentes hijos.
- Componentes SR vienen anotados con `strategy_parent_id` y `strategy_parent_name` para render agrupado en dashboard.
- Validación post-restart (runtime local):
  - `strategy_groups=1`
  - `first_group_id=sr_strategy`
  - `first_group_children=14`
  - `annotated_children=14`

Implicación: la lectura de readiness deja de ser una lista plana y pasa a una vista jerárquica operacional (estrategia padre -> mecanismos hijos), facilitando promoción/rollback por bloque funcional.

# 2) Criterio de evaluacion (alineado a rules y canones)

Una captura de datos es suficiente solo si permite responder de forma reproducible:

1. Por que se abrio esta posicion y no otra.
2. Que gate la dejo pasar o la bloqueo.
3. Que parte de la perdida/ganancia vino de entrada vs lifecycle.
4. Si un cambio mejora baseline en SPOT/FUTURES y perfiles A/B/C.
5. Si existe evidencia para promocion o rollback (gates con muestra suficiente).

# 3) Estado real medido en BD

## 3.1 Volumen por tabla de decision/lifecycle

- trades_closed: 1,585 filas
- trades_audit: 999,949 filas
- ready_candidates: 4,183 filas
- ready_candidate_checks: 134,361 filas
- entry_selection_candidates: 33,830 filas
- position_telemetry: 30,080 filas
- kelly_sizing_log: 4,690 filas

## 3.2 Cobertura de features de apertura (checks ultimos 14 dias)

Cobertura alta:

- sr_zone_position: 91.79%
- sr_resistance_dist_pct: 91.33%
- directional_shadow: 88.04%
- sr_support_dist_pct: 76.80%

Cobertura media/baja:

- expected_pnl: 14.53%
- success_probability: 14.53%
- signal_strength: 14.53%
- trend_4h/range_position/rsi_4h: ~14.4%
- usdt_available: 2.24%

Cobertura nula observada en checks (0% con esos nombres):

- spread_bps_estimate
- funding_rate_annualized
- oi_change_1h_pct
- atr (con esa key)
- bb_position
- fear_greed_value
- range_scalp_timing_shadow (en checks)

Nota: varias features pueden existir con otros nombres o en otras tablas, pero para decidir aperturas en tiempo real la cobertura en ready_candidate_checks es la referencia mas util.

## 3.3 Cobertura de trazabilidad en outcomes (trades_closed ultimos 60 dias)

Bien cubierto:

- profile_at_entry: 53.50%
- regime_at_entry: 53.50%
- sso_pnl_snapshot: 45.43%
- close_group_id: 33.56%

Bajo (limita atribucion robusta):

- ready_candidate_id: 8.39%
- staged_direction: 8.39%
- range_reversion_prediction: 7.95%
- effective_sl_price / effective_tp_price / sl_source: 6.69%
- gate_warnings: 4.10%
- entry_live_feature_completeness: 1.58%
- entry_live_feature_missing: 1.58%
- entry_model_features / entry_model_score: 1.58%

# 4) Diagnostico de suficiencia por dominio

## 4.1 Aperturas (entry quality)

Estado: parcialmente suficiente.

Fortalezas:

- Muy buena visibilidad SR y directional context en checks.
- Buen volumen de checks para analisis de colas y gates.

Brechas criticas:

1. Coverage baja de probabilidad/EV/fuerza en checks globales (~14.5%).
2. Falta observabilidad consistente de costos microestructura en decision:
   - spread estimado,
   - slippage ex-ante,
   - funding/OI intrahora en el punto de decision.
3. Nombres/contratos de features no totalmente estandarizados entre componentes.

Impacto en ganancias/perdidas:

- Dificulta distinguir si perdemos por mala señal, mala ejecucion o costo oculto.
- Reduce capacidad de recalibrar umbrales por evidencia homogénea.

## 4.2 Lifecycle (exit quality)

Estado: suficiente para operar, insuficiente para atribucion fina longitudinal.

Fortalezas:

- trades_audit muy rico y grande.
- telemetry y sso/prs/ug dejan huella util.

Brechas criticas:

1. Link entry->outcome bajo (`ready_candidate_id` 8.39% en cierres).
2. Cobertura baja de parametros efectivos de SL/TP al cierre real.
3. Falta estandar de campos obligatorios por motivo de cierre para analisis causal.

Impacto en ganancias/perdidas:

- Cuesta separar perdida de entrada vs perdida de gestion de salida.
- Menor precision para ajustar PRS/UG/Hard-risk por tipo de fallo.

## 4.3 Calibracion y promocion de modelos

Estado: canonicamente bien definido, operacionalmente con gaps de cobertura.

Fortalezas:

- Gates de promocion y rollback estan claros en canones.
- Separacion real/simulado bien establecida.

Brechas criticas:

1. `range_scalp_live_v1` no aparece con cobertura suficiente en cierres.
2. Persistencia baja de `entry_live_feature_completeness/missing` en outcomes.
3. Sin cobertura estable de costos de ejecucion por trade para labels netos ajustados.

# 5) Lo que SI nos hace falta (prioridad alta)

## P1. Contrato unico de features de decision en checks

Meta:

- 95%+ de checks con un bloque normalizado `decision_features_v1`.

Estado:

- Implementado en persistencia de checks staged.
- Pendiente de cierre: alcanzar y sostener cobertura objetivo en datos de produccion.

Campos minimos obligatorios:

- expected_pnl_pct, success_probability, signal_strength
- rsi, rsi_4h, vol_spike, range_position, bb_position, atr
- sr_zone_position, sr_support_dist_pct, sr_resistance_dist_pct
- spread_bps_estimate, slippage_bps_estimate
- funding_rate_annualized, oi_change_1h_pct (cuando aplique)
- fear_greed_value (nullable permitido)
- feature_completeness, feature_missing

## P1. Linkage obligatorio entry->close

Meta:

- >=90% de trades_closed con ready_candidate_id, staged_direction, profile_at_entry, regime_at_entry.

Estado:

- Implementado en rutas de cierre SPOT/FUTURES.
- Pendiente de cierre: verificar cobertura efectiva >=90% en ventana operacional.

Razon:

- Sin esto no hay atribucion confiable de apertura vs lifecycle.

## P1. Costos reales de ejecucion por trade

Meta:

- guardar en cierre:
  - spread_real_bps (si disponible),
  - slippage_real_bps,
  - commission_bps_total,
  - funding_cost_usdt (futuros).

Estado:

- `commission_bps_total` implementado y ya observado por shadow-readiness.
- `spread/slippage/funding` quedan como hints de costo con cobertura a monitorear y normalizar por origen.

Razon:

- El edge neto depende de friccion real, no solo de direccion correcta.

## P2. Estandar de eventos lifecycle para analisis causal

Meta:

- taxonomia fija de eventos en trades_audit.detail para:
  - trigger primario de cierre,
  - trigger secundario,
  - estado de trend/velocity al momento,
  - decision source (SSO/PRS/UG/HardRisk/Manual).

## P2. Muestra operativa minima por cohorte

Meta:

- dashboard que marque automaticamente cuando una cohorte NO es interpretable (n insuficiente), p.ej.:
  - n < 30 tier A,
  - n < 50 por regimen-side,
  - cobertura < 80% en campos obligatorios.

# 6) Matriz exhaustiva de gaps (dato, uso, impacto, accion)

1. Dato: spread_bps_estimate en checks.
- Uso: filtrar entradas donde el costo se come el EV.
- Estado: no observable en muestra reciente.
- Impacto: alto.
- Accion: instrumentar extractor pretrade y persistir en checks.

2. Dato: slippage ex-ante y slippage real.
- Uso: calibrar target/stop y sizing neto.
- Estado: incompleto/no estandar.
- Impacto: alto.
- Accion: guardar `slippage_bps_estimate` y `slippage_bps_realized`.

3. Dato: linkage ready_candidate_id en cierres.
- Uso: atribucion entrada->outcome.
- Estado: bajo.
- Impacto: alto.
- Accion: hard guard de persistencia al abrir/cerrar.

4. Dato: feature_completeness/missing en outcomes.
- Uso: promotion gates de calidad de datos.
- Estado: muy bajo.
- Impacto: alto.
- Accion: propagar desde entrada a metadata de cierre siempre.

5. Dato: external features (funding/OI/FG) en decision efectiva.
- Uso: sesgo macro y filtros de ruido.
- Estado: bajo/no consistente.
- Impacto: medio-alto.
- Accion: persistir nullables con bandera source/age_ms.

6. Dato: parametros efectivos de SL/TP y fuente al cierre.
- Uso: saber si perdidas vienen de setup o ejecucion de riesgo.
- Estado: bajo.
- Impacto: alto.
- Accion: estandarizar en metadata de cierre para 100% de trades.

7. Dato: taxonomia fija de close_reason enriquecida.
- Uso: analisis de patrones perdedores.
- Estado: parcial.
- Impacto: medio.
- Accion: tabla de mapeo canonical reason_family/reason_detail.

8. Dato: cobertura de decision_features por regimen/perfil.
- Uso: saber si un modo esta ciego de datos.
- Estado: no dashboardizado completo.
- Impacto: alto.
- Accion: endpoint y panel diario de cobertura por cohorte.

# 7) Cuanta informacion es necesaria (minimo viable para decidir bien)

Para operacion diaria:

- 100% de aperturas con:
  - regime, profile, side, signal snapshot, EV/probabilidad,
  - costos estimados (spread/slippage/funding segun modo),
  - decision gate trace.

Para calibracion semanal:

- >=80% cobertura de campos obligatorios por cohorte.
- >=30 casos tier A por cohorte de evaluacion.
- >=50 casos por regimen-side para ajustes sensibles.

Para promocion mensual:

- mantener gates canonicos (`PnL>0`, `PF>=1.15`, `DD no peor`, `cobertura>=5%`, `n>=30 A`) mas:
- feature completeness >=95% para schema objetivo.
- friccion real observada y estable en ventana.

# 8) Recomendacion ejecutiva

El sistema ya guarda mucho dato, pero aun no guarda con cobertura suficiente el conjunto minimo que mas impacta ganancias netas:

1. costo real de ejecucion,
2. linkage completo entrada->cierre,
3. completitud de features de decision en cada apertura.

Sin cerrar esos tres puntos, vamos a seguir pudiendo operar y purgar, pero no vamos a optimizar aperturas/lifecycle con la precision necesaria para subir ganancias y bajar perdidas de forma confiable.

# 9) Plan de implementacion recomendado (2 semanas)

Semana 1:

1. Crear `decision_features_v1` normalizado en `ready_candidate_checks`.
2. Forzar persistencia de `ready_candidate_id` y `staged_direction` en toda apertura/cierre.
3. Persistir `spread/slippage/funding` estimado en decision y realizado en cierre.

Semana 2:

1. Dashboard de cobertura por campo/regimen/perfil.
2. Alertas automaticas de cobertura insuficiente y muestra insuficiente.
3. Validacion contra baseline de 7 y 30 dias para confirmar mejora en explicabilidad y tuning.
