---
title: "Calibración de Datos — Inventario, Calidad, Linaje y Contratos Analíticos"
type: canonical-memory
tags: ["data", "data-quality", "lineage", "historical", "mimic", "peaks", "backtest", "research", "range-scalp"]
created_at: 2026-07-23
updated_at: 2026-07-24
status: active
---

# Calibración de Datos — fuente canónica para análisis del sistema

Este documento es la referencia única de datos para análisis, calibración, entrenamiento, MIMIC, backtests y auditoría. Define qué fuente es apta para cada afirmación, dónde vive, qué representa y qué no debe concluirse de ella.

## 0. Acoplamiento canónico con calibración de entrada

- La arquitectura y responsabilidades del pipeline de calibración de entrada viven en la apertura de `canonical/calibracion_entradas-a20ab8-v3.md`.
- Este documento (`calibrar_datos.md`) no redefine flujo de módulos; solo define autoridad de datos, contratos de features, linaje y criterios de promoción.
- Si hay conflicto entre flujo operativo y evidencia de datos, se conserva la separación: `calibracion_entradas` decide operación, `calibrar_datos` decide validez analítica.

## 1. Principios no negociables

- **Producción es la verdad de PnL:** una afirmación de edge desplegable usa `trades_closed` con PnL neto y lifecycle real. Peaks, MIMIC y backtests son evidencia auxiliar.
- **No mezclar labels:** `realized_net_pnl_pct` de un cierre real, retorno forward de peak y resultado MIMIC son targets distintos. Un dataset debe declarar uno solo por fila.
- **No mezclar identidad:** el perfil de régimen A/B/C no es el tier de calidad de entrada `peak_quality_profile`. Ambos deben almacenarse por separado.
- **No usar futuro:** toda feature debe existir antes de la decisión. Features creadas con cierre, peak u horizonte posterior sólo se usan como labels.
- **No imputar silencio:** para decisión y entrenamiento los faltantes se preservan como `null` y se reportan; nunca se convierten en cero sin una imputación versionada y validada.
- **No promover por frecuencia:** más aperturas o mayor capital no son evidencia de edge. La promoción requiere PnL neto positivo, PF, DD, cobertura, muestra y estabilidad temporal.

## 2. Estado de evidencia al 2026-07-23

| Fuente / cohorte | Resultado | Lectura permitida |
|---|---:|---|
| `trades_closed`, últimos 60d | 1,124 cierres; -194.27 USDT; -0.080% medio; 50.2% WR | No hay edge agregado demostrable en producción. |
| RANGE_SCALP real, tiers de calidad etiquetados | A: 42/-82.40 USDT; B: 22/-50.54; C: 6/-7.53 | Ningún tier se puede promover. Muestra C insuficiente. |
| `range_scalp_walkforward_report.json` | 306 filas, OOF corr -0.0186, promoción false | El modelo RANGE_SCALP permanece `shadow`. |
| Features del walk-forward | 296/306 sin casi todo el schema histórico | El modelo anterior no representa el contrato live; no entrenar/promover hasta reparar cobertura. |
| Peaks | 1,638,639 registros, 43 símbolos | Fuente de oportunidades forward, no PnL ejecutado. |
| MIMIC / sweeps | algunos resultados positivos | Hipótesis; sólo sirve tras igualar ejecución, coste, selección y lifecycle con producción. |
| **Universal cross-symbol dataset** (s93) | 397,081 filas, 40 símbolos, features OHLCV + labels `forward_net_4h` | Entrenamiento y validación walk-forward purgada del modelo universal. |
| **Universal thresholds/model** (s93) | Walk-forward WR 69.7%, PF 4.55, cov 11.6%; MIMIC WR 66.2%, PF 2.90, cov 12.3% | **Desplegado en producción el 2026-07-23.** La verdad final sigue siendo `trades_closed` real. |

## 3. Mapa de almacenamiento y autoridad

### 3.1 PostgreSQL `crypto_agent` — producción y auditoría

| Fuente | Tabla | Granularidad | Autoridad | Uso permitido |
|---|---|---|---|---|
| Posición activa | `trades_open` | posición abierta | operativa | exposición y contexto de entrada; no label final |
| Cierre ejecutado | `trades_closed` | cierre total o parcial | máxima para PnL | outcome neto, comisiones, motivo de salida, cohortes reales |
| Auditoría | `trades_audit` | evento inmutable | máxima para decisiones | reconstruir gates, exits, fills y decisiones |
| Selección previa | `entry_selection_candidates` + `entry_selection_cycles` | símbolo/ciclo/etapa | alta para funnel | candidatos rechazados, QUBO, razones; no fill |
| Staging | `ready_candidates` | candidata staged | alta para validación | signal snapshot, estado, checks, `opened_trade_id` |
| Checks staged | `ready_candidate_checks` | check de candidata | alta para trayectoria | dirección, precios, profile, features y decisión |
| Estado runtime | `trading_runtime_state` | key/value | operativa | régimen/perfil/config efectiva; no entrenamiento directo |
| Sizing | `kelly_sizing_log` | decisión de sizing | auditoría | exposición y multiplicadores; no label de señal |
| Telemetry snapshots | `position_telemetry` | 1s por posición (throttled 5s a DB) | operativa | velocidad/aceleración de precio en tiempo real; no label |
| Drawdown/erosion audit | `trades_audit` (event=`drawdown_monitor_*`) | por acción tomada | auditoría | decisiones de CUT/TIGHTEN_SL/PROTECT_PROFIT con trend data |

### 3.2 Históricos de mercado — repositorio

| Fuente | Ubicación | Formato / granularidad | Uso |
|---|---|---|---|
| OHLCV Binance | `historical_data/<SYMBOL>_<interval>.parquet` | parquet, principalmente 1h | backtests, features reproducibles, MIMIC y labels forward 2h/4h |
| Labels forward de oportunidad | `Dev/benchmarks/research_data/opportunity_forward_labels_1h.jsonl` | JSONL, 40 símbolos, 1h | retorno neto/MFE/MAE 2h/4h; oportunidad de mercado, no fill real |
| Manifests de descarga | `historical_data/manifest_*.json` | JSON | cobertura, periodos, símbolos y procedencia |
| Datos externos históricos | `historical_data/external/` | parquet + `manifest.json` | F&G, BTC dominance, funding, ticker; sólo si timestamp-alineado |
| Auditoría de cobertura | `Dev/benchmarks/mimic_data_audit.json` | JSON generado | gaps, NaN, barras y span por símbolo |

Los archivos `*.bak.*` en `historical_data` son restauraciones, no insumos de experimentos. Cualquier experimento debe usar únicamente el parquet activo nombrado `<SYMBOL>_<interval>.parquet` y declarar el hash del archivo.

### 3.3 Peaks — hipótesis forward de mercado

| Artefacto | Ubicación | Definición |
|---|---|---|
| Dataset principal | `Dev/benchmarks/peaks_dataset/peaks_all.parquet` | una fila por peak detectado |
| Export opcional | `Dev/benchmarks/peaks_dataset/peaks_all.jsonl` | mismo dataset para streaming |
| Metadata | `Dev/benchmarks/peaks_dataset/peaks_summary.json` | símbolos, tipos y recuentos |
| Distribuciones | `Dev/benchmarks/peaks_dataset/peaks_by_regime.json` | retorno por régimen/dirección |
| Modelos peaks | `Dev/benchmarks/peaks_dataset/peak_quality_model/` | modelos por régimen/lado + metadata |

Campos básicos: `symbol`, `direction`, `peak_type`, `peak_start_ts`, `peak_end_ts`, `peak_return`, `window_id`, `window_regime`. Las features de inicio usan prefijo `s_`; las de fin `e_`. `e_*` y cualquier retorno forward no se permiten para una decisión live.

### 3.4 MIMIC, Super MIMIC, sweeps y backtests

| Familia | Ubicación | Qué representa | Limitación |
|---|---|---|---|
| Dataset de perfil | `Dev/benchmarks/entry_profile_calibration/training_data_v2*.jsonl` | resultados de simulaciones para calibración | no es fill real |
| Dataset RANGE actual | `Dev/benchmarks/entry_profile_calibration/range_scalp_calibration_dataset.jsonl` | mezcla histórica actual de real + MIMIC | versión v1 tiene feature coverage insuficiente |
| Reporte walk-forward | `Dev/benchmarks/entry_profile_calibration/range_scalp_walkforward_report.json` | validación temporal del modelo RANGE | no promovible al 2026-07-23 |
| **Dataset universal** | `Dev/benchmarks/research_data/cross_symbol_dataset.jsonl` | features OHLCV genéricas + label `forward_net_4h`, 40 símbolos | entrenamiento del modelo universal (s93) |
| **Umbrales universal** | `Dev/benchmarks/research_data/universal_thresholds_proposal.json` | thresholds A/B/C por régimen + categoría | insumo para `entry_profile_thresholds.json` (s93) |
| **Modelo universal** | `Dev/benchmarks/research_data/quality_score_model_universal_proposal.pkl` | `HistGradientBoostingClassifier` por régimen/categoría | gate en producción desde s93; verdad final en `trades_closed` |
| Sweep por ventanas | `Dev/benchmarks/entry_profile_calibration/sweep_*` | sensibilidad de parámetros/ventanas | descartar `trades=0`, PF nulo y perfiles sin propagación |
| Super MIMIC | `Dev/benchmarks/super_mimic/` | simulación multi-régimen y predictive sweep | resultado auxiliar hasta alinear lifecycle |
| Lifecycle sweeps | `Dev/benchmarks/lifecycle_sweep*`, `mimic_sweep_peak_quality_lifecycle` | hipótesis de salida | comparar con los mismos labels netos de producción |
| Backtests generales | `Dev/benchmarks/*`, raíz `backtest_*.py` y `backtest_*.log` | pruebas históricas | registrar configuración, mercado y coste |

Los runners vigentes se alojan en `Dev/scripts/mimic_*.py`, `Dev/scripts/super_mimic_classify_windows.py`, `backtest_*.py`, `historical_simulator.py` y `simulation/`.

## 4. Clasificación de fuentes por uso

| Pregunta | Fuente primaria | Fuente secundaria | Prohibición |
|---|---|---|---|
| ¿Existe edge ejecutable? | `trades_closed` neto enlazado | shadow prospectivo | peaks/MIMIC solos |
| ¿La entrada tenía oportunidad forward? | peaks o velas timestamp-alineadas | candidatas no abiertas | inferir fill/coste real |
| ¿Por qué no abrió? | `entry_selection_candidates`, `ready_*`, `trades_audit` | signal store | `trades_closed` aislado |
| ¿Por qué perdió? | `trades_closed` agrupado por posición + `trades_audit` | MFE/MAE reconstruido | culpar entrada sin lifecycle |
| ¿Qué features eran disponibles? | snapshot `signal_meta`/`entry_model_features` | OHLCV a `entry_time` | `e_*`, cierre o peak futuro |
| ¿Qué configuración fue efectiva? | `trading_runtime_state` + config snapshot | `trades_audit` | defaults de código aislados |

## 5. Identidad, linaje y etiquetas canónicas

### 5.1 Cadena de decisión esperada

`entry_selection_candidates.cycle_id` → `ready_candidates.id` → `ready_candidate_checks.ready_candidate_id` → `trades_open.metadata.ready_candidate_id` → `trades_closed.metadata.ready_candidate_id`.

Hay cohortes históricas sin `ready_candidate_id`; se mantienen como `link_status="unlinked_legacy"`, jamás se enlazan por símbolo solamente. El match de fallback requiere símbolo, lado y ventana de tiempo explícita, se etiqueta `heuristic` y no se usa en entrenamiento de aceptación.

### 5.2 Agrupación de outcome real

Una posición puede producir varias filas por cierres parciales. El dataset de investigación agrupa por `position_key`:

1. `metadata.close_group_id` si existe;
2. si no, `entry_order_id + symbol + side + entry_time`;
3. como último fallback trazable, `symbol + side + entry_time`.

El label `realized_net_pnl_usdt` es la suma de todos los cierres del grupo; `realized_net_pnl_pct` es porcentaje ponderado por cantidad/valor cerrado. `partial_close` se preserva como detalle y no como una observación independiente de entrenamiento.

### 5.3 Labels oficiales

| Label | Origen | Significado |
|---|---|---|
| `realized_net_pnl_usdt` | grupo de `trades_closed` | PnL neto total ejecutado |
| `realized_net_pnl_pct` | grupo de `trades_closed` | retorno neto ponderado de la posición |
| `exit_reason_set` | grupo de `trades_closed.close_reason` | mecanismos de lifecycle aplicados |
| `mfe_*` / `mae_*` | velas posteriores versionadas | excursiones, no PnL realizado |
| `forward_return_*` | peaks/velas posteriores | oportunidad de mercado, no ejecución |
| `forward_gross_2h` / `forward_gross_4h` | OHLCV posterior | retorno direccional al cierre de horizonte, antes de fricción |
| `forward_net_2h` / `forward_net_4h` | OHLCV posterior + coste versionado | retorno direccional menos comisión/slippage de entrada/salida; no fill real |
| `forward_mfe_2h` / `forward_mfe_4h`, `forward_mae_2h` / `forward_mae_4h` | highs/lows OHLCV posteriores | excursión favorable/adversa direccional; no PnL realizado |
| `mimic_realized_net_pnl_pct` | MIMIC | target simulado, nunca fusionado como real |

## 6. Contrato `range_scalp_live_v1`

El contrato de entrada se captura al instante de señal, antes de `entry_profile_gate`. Cada campo tiene valor o `null`; se persisten `feature_schema_version`, `feature_missing` y `feature_completeness`.

| Feature | Fuente | Requerida para modelo compacto |
|---|---|---|
| `expected_pnl_pct` | predictor | sí |
| `success_probability` | predictor | sí |
| `strength` | señal | sí |
| `rsi` | indicador de señal | sí |
| `rsi_4h` | indicador multi-TF | sí |
| `vol_spike` | señal / OHLCV | sí |
| `range_position` | OHLCV / señal | sí |
| `bb_position` | OHLCV / señal | sí |
| `atr` | OHLCV | sí |
| `trend_4h` | OHLCV / régimen | sí |
| `peak_quality` | peak model live | opcional, nunca label |
| `composite_score` | señal | opcional |
| `fear_greed_value` | cache externa timestamp-alineada | opcional |
| `spread_bps_estimate` | book/ticker live | opcional hasta que exista extractor estable |
| `range_scalp_timing_shadow` | OHLCV 15m capturado tras setup 1h y antes de orden | telemetría opcional de timing; no interviene en gate, ranking ni orden |

El modelo anterior usa un schema más grande. No se vuelve a entrenar ni se hace `enforced` hasta que el schema real soporte completeness >=95% en la cohorte shadow y pase validación temporal.

## 7. Reglas de calidad y exclusión

Excluir del entrenamiento principal, manteniendo reporte separado:

- `is_synthetic=true`, `corrupt=true`, `is_corrupted=true`;
- `ghost_balance_mismatch`, dust, reconciliaciones, limpiezas y cierres manuales;
- observaciones sin `entry_time`, símbolo o PnL neto válido;
- filas MIMIC mezcladas con producción bajo un mismo label;
- filas con régimen forzado sin esa etiqueta;
- outcomes sin fill confirmado.

Clasificar, no excluir automáticamente, los cierres de SL/UG/replacement: son esenciales para atribuir el outcome al lifecycle versus entrada.

## 8. Artefactos reproducibles implementados

- `Dev/scripts/build_research_manifest.py`: inventario con paths, tamaños, hashes, metadata y schema de fuentes de investigación.
- `Dev/scripts/build_research_outcomes.py`: dataset de posición enlazado que agrupa parciales y preserva `link_status`, lifecycle y calidad de features.
- `Dev/scripts/build_range_scalp_calibration_dataset.py`: sólo debe consumir outcomes reales explícitamente etiquetados y MIMIC separado por `source`.
- `opportunity_labels.py` + `Dev/scripts/build_opportunity_forward_labels.py`: construyen labels LONG/SHORT de cierre, MFE y MAE para 2h/4h desde OHLCV 1h; deben declarar comisión, slippage, lado, intervalo y horizonte. La generación actual produjo 399,647 filas de 40 símbolos con coste SPOT-BNB round-trip de 0.204%.

Cada ejecución genera un manifest/version hash y reporta filas válidas, excluidas, enlaces exactos/heurísticos y faltantes por feature.

## 9. Gates de aceptación y rollback de datos

| Gate | Umbral | Acción si falla |
|---|---|---|
| Integridad del manifest | hash/schema fuente disponible | bloquear experimento |
| Label real | fill + PnL neto + grupo consistente | excluir de entrenamiento |
| Link candidato→trade | exacto por `ready_candidate_id` u orden | marcar legacy, no inferir |
| Completeness shadow | >=95% de features requeridas | mantener `shadow`; reparar extractor |
| Promoción modelo | PnL >0, PF>=1.15, DD no peor, cobertura>=5%, n>=30 A por holdout | no promover |
| Drift post-rollout | tier PnL<=0, PF<1 o pérdida >2× presupuesto | volver modelo/thresholds a `shadow` |

Rollback de datos no modifica órdenes, posiciones, SL/TP ni lifecycle. Restaura el manifest/dataset/modelo anterior y mantiene la auditoría de la decisión.

## 10. Operación de investigación

- Ejecutar procesos batch con máximo 10 workers, parquet streaming y resultados JSONL particionados.
- Todo runner declara intervalo temporal, símbolos, régimen detectado/forzado, versión de lifecycle, costes, hashes de inputs, semilla y parámetros.
- Se rechaza una conclusión de sweep si `trades=0`, PF nulo, la configuración no varía el resultado, o no se puede verificar la propagación del perfil.
- Walk-forward usa splits temporales purgados/embargados según horizonte; no se usa `KFold(shuffle=True)`.
- RANGE_SCALP conserva setup 1h y contexto 4h; `range_scalp_timing_shadow` 15m se compara prospectivamente contra el baseline antes de convertirse en feature o gate. No sustituye el contrato `range_scalp_live_v1`.
- Sensibilidad mínima: comisiones/fills reales y fricción 1x/1.5x/2x.

## 11. Observabilidad mínima

El dashboard/API de datos debe mostrar por periodo, régimen, lado, símbolo, tier de calidad y razón de salida:

- funnel candidato → gate → staged → QUBO → fill → cierre;
- coverage de schema y faltantes por feature;
- enlaces exactos, legacy y fallidos;
- PnL neto, PF, WR, DD, MFE/MAE y calibración score/outcome;
- distribución de `close_reason` y porcentaje de parciales;
- dataset/model/threshold/lifecycle version.
- `GET /metrics/entry-evidence` es la vista autenticada y de solo lectura para este avance: entrega funnel, cobertura, lineage, outcomes exactos y gates; no modifica configuración ni promoción.

## 12. Relación con los documentos hermanos

- `calibracion_entradas-a20ab8-v3.md` define la política de entrada y consume los datasets de este documento.
- `calibrar-ciclo-vida-52fe5d.md` define el lifecycle y consume los outcomes agrupados para atribución de exits.
- `.windsurf/rules.md` mantiene las reglas operativas; este documento es autoridad para ubicación, linaje y calidad de datos.

## 13. Checklist antes de cualquier análisis

1. ¿El manifest identifica exactamente los inputs y hashes?
2. ¿El target es real, forward o simulado y está separado?
3. ¿Las features existían antes del timestamp de decisión?
4. ¿Se agruparon cierres parciales por posición?
5. ¿Se separaron SPOT, FUTURES, régimen, side, profile de régimen y tier de entrada?
6. ¿Se incluyeron costes/lifecycle efectivos?
7. ¿El resultado usa holdout temporal y sensibilidad de fricción?
8. ¿Se puede reproducir sin cargar datasets completos en memoria?
