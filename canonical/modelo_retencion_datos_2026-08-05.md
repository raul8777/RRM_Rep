---
title: "Modelo de Retencion y Purga de Datos"
type: canonical-memory
status: proposal
created_at: 2026-08-05
related:
  - canonical/calibrar_datos.md
  - canonical/calibracion_entradas-a20ab8-v3.md
  - canonical/calibrar-ciclo-vida-52fe5d.md
---

# Modelo de retencion y purga de datos (historico incluido)

## 1) Objetivo

Mantener maxima capacidad de decision diaria y de investigacion historica, reduciendo crecimiento de disco en Docker y workspace sin tocar el comportamiento del sistema de trading.

Este plan respeta tres reglas canonicas:

- Produccion (`trades_closed`) es la verdad final para edge.
- Datos de MIMIC/peaks/backtests son evidencia auxiliar y nunca sustituyen labels reales.
- Los historicos son obligatorios para calibracion y backtests, pero con capas de temperatura y retencion distinta.

## 2) Estado real medido (2026-08-05)

### Workspace de datos

- `historical_data/`:
  - 2,493 archivos
  - 305.34 MB total
  - 2,228 archivos `.bak.*` (227.69 MB)
  - 253 parquet activos (21.08 MB)
- `Dev/benchmarks/`:
  - 359 archivos
  - 9,023.69 MB
- logs/txt en repo:
  - 59 archivos
  - 1,746.53 MB

### Top consumidores fuera de DB

- `Dev/benchmarks/peaks_dataset/peaks_all_old.jsonl`: 5,702.14 MB
- `Dev/benchmarks/mimic_sweep_peak_quality_full.log`: 1,268.98 MB
- `Dev/benchmarks/peaks_dataset/peaks_all.parquet`: 388.87 MB
- `Dev/benchmarks/forced_regime_sweep/sweep.log`: 282.14 MB
- `Dev/benchmarks/research_data/cross_symbol_dataset.jsonl`: 244.28 MB
- `Dev/benchmarks/research_data/opportunity_forward_labels_1h.jsonl`: 193.37 MB

### Docker

- Images: 15.12 GB
- Local volumes: 155.8 GB
- Build cache: 28.06 GB (13.38 GB reclaimable)

### PostgreSQL (tabla por tamano)

- `backtest_trade`: 136 GB
- `market_data`: 8,180 MB
- `trades_audit`: 562 MB
- `ready_candidate_checks`: 242 MB
- `position_telemetry`: 23 MB
- `trades_closed`: 8.68 MB

## 3) Modelo de datos por capas (incluye historico)

### Capa A: Decision operacional (hot)

Retencion objetivo: 45 dias raw + 180 dias agregados diarios.

Fuentes:

- `trades_open`, `trades_closed`, `trades_audit`
- `entry_selection_candidates`, `ready_candidates`, `ready_candidate_checks`
- `trading_runtime_state`, `kelly_sizing_log`

Uso:

- Apertura/cierre y explicabilidad de por que abrio o no abrio.
- Monitoreo de degradacion y rollback rapido.

### Capa B: Calibracion de entrada/salida (warm)

Retencion objetivo: 180 dias raw + 24 meses agregados semanales/mensuales.

Fuentes:

- Outcomes agrupados por posicion (`trades_closed` grouped)
- Features disponibles al momento de decision (`range_scalp_live_v1` y lineage)
- Telemetria de gates y razones de rechazo

Uso:

- Recalibrar umbrales A/B/C, evaluar drift por regimen/perfil.

### Capa C: Investigacion historica/backtest/mimic (cold)

Retencion objetivo:

- Artefactos reproducibles activos: conservar
- Artefactos pesados intermedios: comprimir/archivar
- Raw de corridas masivas: purgar por antiguedad y mantener resumen

Fuentes:

- `historical_data/*.parquet` activos
- `Dev/benchmarks/research_data/*`
- `Dev/benchmarks/peaks_dataset/*`
- `backtest_trade`, `backtest_run`

Uso:

- Walk-forward, backtests comparativos, auditoria de hipotesis.

## 4) Cuanta informacion minima necesitamos para decidir bien

### 4.1 Muestra minima operacional

Regla canonicamente exigida: n>=30 en tier A para promocion por holdout.

Dato observado en produccion: 1,124 cierres en 60 dias (~18.7 cierres/dia).

Calculo de tiempo para muestra por cohorte:

- `dias_para_n = n_objetivo / (cierres_dia * fraccion_regimen * fraccion_tier)`

Ejemplo conservador:

- cierres_dia = 18.7
- fraccion_regimen = 0.35
- fraccion_tier = 0.25
- n_objetivo = 30

Resultado:

- dias_para_n ~= 30 / (18.7*0.35*0.25) ~= 18.3 dias

Conclusiones:

- 45 dias hot cubre validacion de ciclo corto.
- 180 dias warm es suficiente para robustez por regimen/perfil y estacionalidad corta.

### 4.2 Minimo historico para backtest y mimic

- 12 meses de OHLCV 1h por simbolo para estabilidad multi-regimen.
- 24 meses recomendado para comparar ciclos y evitar sobreajuste de ventana.
- Mantener siempre manifests y hashes de input para reproducibilidad.

## 5) Politica de purga segura (sin afectar comportamiento)

## Fase 1 (impacto bajo, ahorro inmediato)

1. Eliminar o mover a archivo frio `historical_data/*.bak.*`.
2. Rotar/comprimir logs >30 dias en `Dev/benchmarks/*.log` y `*.txt`.
3. Limpiar build cache Docker no usado (`docker builder prune`) en ventana de mantenimiento.
4. Revisar `peaks_all_old.jsonl`:
   - si no se usa en pipelines activos, mover a almacenamiento externo comprimido.

## Fase 2 (impacto medio, controlado)

1. `backtest_trade`:
   - conservar raw 90 dias
   - exportar >90 dias a parquet particionado por `run_id` y mes
   - borrar de DB solo tras validar export + hash + conteos
2. `market_data`:
   - conservar raw 180 dias
   - agregar tabla de resumen diario para horizontes largos
3. `ready_candidate_checks` y `entry_selection_candidates`:
   - conservar raw 120 dias
   - materializar agregados diarios por regimen/symbol/gate reason

## Fase 3 (gobernanza continua)

1. Job diario de snapshot de tamano por tabla/directorio.
2. Job semanal de propuesta de purge en modo dry-run.
3. Job mensual de archivo frio y verificacion de restore (muestreo).

## 6) Guardrails obligatorios antes de borrar

1. Nunca purgar `trades_closed` ni `trades_audit` sin export versionado y validacion de restore.
2. Nunca borrar `historical_data/<SYMBOL>_<interval>.parquet` activos sin manifest de reemplazo.
3. Cualquier borrado debe generar:
   - conteos antes/despues
   - checksum de archivo exportado
   - reporte de dif con filas y periodos
4. Purga primero en dry-run y luego en apply.
5. Ventana de cambio fuera de ciclo de trading activo.

## 7) Presupuesto objetivo de almacenamiento

Objetivo inicial (30 dias):

- Reducir `Dev/benchmarks` de ~9.0 GB a <=4.0 GB
- Reducir logs de ~1.7 GB a <=0.3 GB
- Reducir build cache Docker en >=10 GB
- Reducir `postgres_data` de 155.8 GB a <=60 GB, atacando `backtest_trade`

Objetivo sostenido (90 dias):

- Crecimiento neto mensual <= +5 GB en total (DB + filesystem + Docker cache)

## 8) Plan de ejecucion recomendado

Semana 1:

1. Implementar auditor de almacenamiento (solo lectura) y reporte diario.
2. Ejecutar Fase 1 completa.
3. Definir politica SQL exacta para archivar `backtest_trade` por fecha/run.

Semana 2:

1. Ejecutar archivo + purge parcial de `backtest_trade` en lote pequeno.
2. Validar consultas de analytics y backtest post-purge.
3. Ajustar TTL de `market_data` y checks de entrada.

Semana 3:

1. Activar automatizacion semanal dry-run + mensual apply.
2. Publicar dashboard interno de salud de almacenamiento.

## 9) Riesgos y mitigaciones

- Riesgo: perder reproducibilidad de un experimento antiguo.
  - Mitigacion: export con hash + manifest + metadata de corrida antes de borrar.
- Riesgo: degradar investigacion de largo plazo.
  - Mitigacion: conservar agregados mensuales + parquet frio por run critico.
- Riesgo: afectar runtime de trading por mantenimiento.
  - Mitigacion: ejecutar purga fuera de ventana de trading y sin tocar tablas hot durante ciclo.

## 10) Decision ejecutiva

El problema principal de espacio no esta en datos historicos activos de mercado (que son pequenos), sino en:

1. `backtest_trade` en Postgres (136 GB)
2. artefactos y logs pesados de benchmark (especialmente `peaks_all_old.jsonl` y logs de sweep)
3. build cache Docker acumulado

Se puede reducir mas del 50% del consumo total sin perder capacidad de decision diaria ni historico util, si se aplica retencion por capas y archivo versionado en vez de borrado ciego.
