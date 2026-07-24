---
title: "Calibración de Entradas — Producción Real, Capital por Modo, Datos de Terceros y Maduración del Sistema"
type: planning
status: in-progress
created_at: 2026-07-22
merged_from: Calibracion_Entradas-a20ab8-v2.md
supersedes: Calibracion_Entradas-a20ab8-v2.md
---

# Plan a20ab8 v3: Producción real con rotación total de capital por modo, apertura controlada de FUTURES_SHORT/PRESERVATION, integración selectiva de datos de terceros y maduración operativa absoluta

> **Merge note:** `Calibracion_Entradas-a20ab8-v2.md` fue fusionado en este documento. Se conservan las decisiones de dirección del usuario, el análisis por modo y las reglas duras de v2; v3 añade la asignación de capital por modo, la evaluación de datos de terceros y el cronograma de maduración.

## Estado actual del sistema — jul 22 2026

La calibración agresiva ya está reflejada en la configuración runtime. Se corrigió el mecanismo por el cual los perfiles de régimen (almacenados en `trading_runtime_state`) revertían los valores globales a configuraciones más conservadoras de sweeps anteriores.

### Cambios aplicados en esta sesión

- **`docker-compose.override.yml`**: se añadieron todas las variables de entorno globales de la calibración agresiva para sobrescribir los valores conservadores de `docker-compose.yml`:
  - `KELLY_MAX_USDT=2500.0`, `KELLY_MAX_PCT_LIQUID=0.90`, `KELLY_DYNAMIC_BASE_PCT=0.22`, `KELLY_CONVICTION_MAX_PCT=0.22`
  - `FUTURES_LEVERAGE=2`, `FUTURES_KELLY_MAX_USDT=2500.0`, `FUTURES_KELLY_CAP_PCT=0.90`, `FUTURES_KELLY_BASE_USDT=200.0`, `FUTURES_KELLY_MAX_PCT_LIQUID=0.90`
  - `FUTURES_MIN_PROB=0.45`, `FUTURES_MAX_NEW_PER_CYCLE=3`
  - `MIN_SIGNAL_STRENGTH=0.40`, `MIN_SUCCESS_PROBABILITY=0.50`
  - `MAX_POSITIONS_SPOT_BULL=8`, `MAX_POSITIONS_RANGE_SCALP=6`, `MAX_POSITIONS_PRESERVATION=6`, `MAX_POSITIONS_FUTURES_SHORT=5`
  - `VOLSPIKE_OVERRIDE_ENABLED=false`
- **`config_helpers.py`**: se añadió `import os` para corregir el error `name 'os' is not defined` al cargar `kelly_config_override` desde la DB; el override de DB ahora respeta las variables de entorno.
- **`run_trades.py`**: se introdujo `_ENV_PROTECTED_SCALARS` en `_apply_regime_params()` para que los perfiles de régimen sigan pudiendo ajustar SL/TP/vol_spike/etc., pero **no puedan revertir los escalares globales de la calibración agresiva**.
- **Tests**: se actualizó `tests/test_profit_boost_entries.py` al nuevo default `futures_max_new_per_cycle=3`.
- **Runtime**: se limpió `kelly_config_override` y se recreó el contenedor `app` para aplicar las nuevas variables de entorno.

### Configuración runtime verificada

| Parámetro | Valor actual |
|-----------|---------------|
| `kelly_max_usdt` | 2500.0 |
| `kelly_max_pct_liquid` | 0.90 |
| `kelly_dynamic_base_pct` | 0.22 |
| `kelly_conviction_max_pct` | 0.22 |
| `max_positions_spot_bull` | 8 |
| `max_positions_range_scalp` | 6 |
| `max_positions_preservation` | 6 |
| `max_positions_futures_short` | 5 |
| `futures_leverage` | 2 |
| `futures_kelly_max_usdt` | 2500.0 |
| `futures_kelly_cap_pct` | 0.90 |
| `futures_kelly_base_usdt` | 200.0 |
| `futures_min_prob` | 0.45 |
| `futures_max_new_per_cycle` | 3 |
| `min_signal_strength` | 0.40 |
| `min_success_probability` | 0.50 |
| `futures_entry_max_rsi_4h` | 50.0 |
| `volspike_override_enabled` | False |

### Validación

- `/config/drift-check`: `drift_count=0`, `new_alerts=0`.
- `/config/verify`: `ok=True`, `mismatch_count=0`.
- `/health/status`: `overall=ok`, régimen activo `RANGE_SCALP`, leverage máximo 2x, `db_open_positions=0`.
- Suite completa de tests: **576 passed, 0 failed**.

### Estado de los cambios propuestos (C1-C10)

- **C1 — Armonía de parámetros y auditoría de overrides**: completado. Los valores agresivos están en `docker-compose.override.yml` y protegidos contra perfiles de régimen.
- **C2 — Propagar `peak_quality_profile`**: parcial; el `entry_profile_gate` ya usa el modelo de calidad entrenado (`quality_score_model_spot_bull.pkl`), pero falta verificar que el perfil real se propaga a `signal_meta` y a la metadata de posición.
- **C3 — Recalibrar thresholds RANGE_SCALP A/B/C**: pendiente. El archivo `entry_profile_thresholds.json` aún muestra A=1.5, B=1.0, C=0.9 con B/C sin datos (`n=0`).
- **C4/C5 — Relajar B/C y fricción staged entry**: parcial. `staged_entry_enabled=True`, `required_positive_checks=1`, `max_open_per_cycle=2` en SPOT_BULL/RANGE_SCALP; falta validar el bypass B/C con `confidence >= 0.8`.
- **C6 — Activar FUTURES_SHORT**: parcial. `futures_enabled=True`, `futures_leverage=2`, `max_positions_futures_short=5`, pero aún no se han abierto shorts reales y los thresholds de shorts no están recalibrados con outcomes reales.
- **C7 — PRESERVATION más ofensivo**: pendiente; aún no se actualizan `preservation_max_positions`, `preservation_min_vol_spike`, `preservation_max_rsi_entry`, `preservation_kelly_mult`.
- **C8 — Paralelización**: pendiente.
- **C9 — Datos de terceros (CoinGlass/Usenami)**: pendiente; no hay módulo `external_data_client.py` aún.
- **C10 — Analista Técnico Gráfico**: pendiente.

Este plan finaliza la estrategia: se activa cada modo con **todo el capital disponible del sistema** (respetando operaciones abiertas y reserva de comisiones), se seleccionan fuentes de datos externas de alto valor, y se establecen salvaguardas de madurez para que ningún cambio rompa armonía ni trazabilidad.

## 1. Decisiones del usuario (registro de dirección)

- **Aprobado** el plan v2.
- **Capital:** modo único activo a la vez; cuando un modo esté activo dispone de todo el capital excepto posiciones abiertas y reserva de comisiones.
- **PRESERVATION:** abrirlo más (más tokens NARRATIVE, umbrales ligeramente más permisivos, pero sin romper narrative_only).
- **Datos de terceros:** revisar mejores opciones, incluir solo lo estrictamente valioso, descartar lo de bajo valor.
- **Maduración:** la siguiente etapa debe demostrar madurez absoluta: configuración auditada, monitoreo completo, tests, documentación.

## 2. Principio de asignación de capital por modo (una sola dirección activa)

El sistema opera un único régimen confirmado por ciclo. La asignación objetivo es:

| Modo activo | Dirección | Objetivo de capital desplegado |
|-------------|-----------|--------------------------------|
| SPOT_BULL | Long spot | 80-95% del líquido disponible |
| RANGE_SCALP | Long spot | 70-90% del líquido disponible (rotación más rápida) |
| PRESERVATION | Long spot selectivo | 30-60% del líquido disponible |
| FUTURES_SHORT | Short futuros | 60-90% del margen disponible |

**Reglas duras de capital:**
- Nunca se cierran posiciones abiertas por un cambio de modo; solo nuevas entradas siguen el modo activo.
- Reserva de comisiones (`bnb_reserve`, `usdt_buffer`) se mantiene intacta.
- En FUTURES_SHORT: leverage máximo 2x por posición, max 5 shorts activos, nunca más del 90% del margen total para evitar liquidaciones de mantenimiento.
- En PRESERVATION: narrative_only=True, kelly_mult reducido, max_positions limitado.

## 3. Datos de terceros: evaluación y selección

### Fuentes evaluadas

| Fuente | Datos clave | Costo aprox. | Valor para el sistema | Incluir |
|--------|-------------|--------------|-----------------------|---------|
| **CoinGlass API** | Funding rates, OI, liquidaciones, long/short ratio, ETF flows, whale alerts, exchange balances | Medio/Alto | **Muy alto** para FUTURES_SHORT y macro BTC | ✅ Sí |
| **MMT.gg** | Heatmaps, volume delta, OI, liquidation clusters, aggregated CEX data | Medio | Alto para exits y stop placement | ✅ Fase 2 |
| **Usenami** | Funding rates, OI, volume, arbitrage (free tier) | Bajo/Gratis | Alto como fallback y para validación | ✅ Sí (free tier) |
| **Loris Tools** | Perp futures cross-venue, options surfaces, RWA perps | Alto | Medio; overkill si solo operamos Binance | ❌ No |
| **DappLooker Perp Intelligence** | DEX perps, orderbook, funding, OI | Medio | Bajo; no operamos DEX perps | ❌ No |
| **Glassnode / Messari** | On-chain, macro, unlocks, research | Alto/Medio | Medio a largo plazo; no impacta ciclo 5-15 min | ❌ No ahora |
| **CoinGecko / CoinMarketCap** | Market cap, dominance, vol24h global | Gratis (limitado) | Bajo; ya tenemos dominance y vol24h de Binance | ❌ No |
| **TradingView (charts widget)** | Visualización OHLC, indicadores | Gratis/Pro | Medio para análisis humano; no genera señal directa | ❌ No ahora |

### Integración priorizada

**Fase 1 — obligatoria (valor alto, costo bajo):**
1. **Funding rate cross-exchange** (CoinGlass o Usenami): detectar cuando Binance funding es extremo vs otras venues → señal de squeeze/continuación para shorts.
2. **Open Interest Δ 1h/4h/24h** (CoinGlass): confirmar si una caída viene con descarga de apalancamiento o acumulación de shorts.
3. **Liquidation clusters / heatmap** (CoinGlass o MMT): ajustar SL/TP y trailing para evitar spikes.
4. **Long/Short ratio y top trader positions** (CoinGlass): contrarian/sentiment input.
5. **ETF net inflows / Coinbase Premium Index** (CoinGlass): macro BTC direction para SPOT_BULL/PRESERVATION.

**Fase 2 — valor medio:**
- Whale exchange inflow/outflow alerts.
- Stablecoin market cap delta (liquidez global cripto).

**No integrar ahora:**
- DEX perps, RWA perps, options surfaces, on-chain profundo, news NLP.

### Requisito de implementación

Todo dato de terceros debe:
- Tener fallback a datos de Binance si la API falla.
- No bloquear el ciclo: timeout ≤ 5s, cache TTL ≥ 15 min.
- Ser auditado en `trades_audit` event `EXTERNAL_DATA` con timestamp y fuente.
- No reemplazar el modelo interno; solo añadir features al `entry_profile_gate` y al predictor.

## 4. Cambios propuestos (versión final con capital total por modo)

### C1 — Armonía de parámetros y auditoría de overrides (Fase 0)

- Ejecutar `tmp_audit_config.py` y `/config/verify` antes de cualquier cambio.
- Alinear todos los defaults de `config.py`, `_PARAM_DEFAULTS`, `_scalar_map`, `_KELLY_EDITABLE_FIELDS`.
- Añadir validación en `_apply_regime_params()` que loguee cada override aplicado.
- **Madurez:** checklist de 4 lugares para todo parámetro nuevo.

### C2 — Propagar `peak_quality_profile` real

- Corregir `run_trades.py::_filter_buy_candidates` para escribir `peak_quality_profile` en `signal_meta[symbol]` y metadata de posición.
- Asegurar que `position_manager.add_position()` reciba y guarde el perfil del gate, no del régimen.
- **Madurez:** test `test_peak_quality_profile_propagation.py` nuevo.

### C3 — Recalibrar thresholds RANGE_SCALP A/B/C

- Usar `training_data_v2_bestexit.jsonl` + outcomes reales de los últimos 7 días.
- Separar A/B/C con mean_net > 0 en cada tier.
- **Madurez:** script `recalibrate_range_scalp_thresholds.py` con CI semanal.

### C4 — Permitir B/C en regímenes confirmados con confianza alta

- SPOT_BULL y RANGE_SCALP: si `regime.confirmed=True` y `confidence >= 0.8`, reducir umbral efectivo de B/C hasta el mínimo calibrado.
- PRESERVATION: si `regime.confirmed=True`, bajar `entry_quality_vol_spike_min_preservation` y subir `max_positions_preservation` de 2 a 4.
- **Madureg:** monitoreo semanal WR/PnL por perfil; rollback automático si WR<42% por 7 días.

### C5 — Reducir fricción staged entry para perfil A

- `required_positive_checks=1` solo para perfil A en SPOT_BULL/RANGE_SCALP cuando `confidence >= 0.8`.
- B/C y PRESERVATION/FUTURES_SHORT mantienen 2.
- `direction_min_progress_bps` a 3.0 para SPOT_BULL; 3.0 para RANGE_SCALP.
- **Madurez:** métrica `validated_to_opened_ratio` en dashboard.

### C6 — Activar FUTURES_SHORT en producción con todo el capital del modo

- `futures_enabled=True`.
- `futures_max_shorts=5`.
- `futures_leverage=2`.
- Capital por short = `min(Kelly, margen_total_disponible / max_shorts)` con cap de riesgo: pérdida máxima del bucket ≤ 5% del portfolio total en 24h.
- Requisitos de entrada:
  - BTC 4h trend DOWN.
  - RSI 4h < 50.
  - Funding rate positivo (>0.01%) o al menos no negativo extremo.
  - Vela 5m roja confirmada.
  - Perfil A del `entry_profile_gate` (calibrar thresholds shorts con MIMIC + primeros outcomes reales).
- Mass-SL pause: si 2+ shorts cierran por SL en 15 min, pausar nuevos shorts 2h.
- **Madurez:** dashboard de futuros con funding, OI, unrealized PnL, paridad Binance↔DB.

### C7 — PRESERVATION más ofensivo

- `preservation_max_positions=4` (subir de 2).
- `preservation_min_vol_spike=0.8` (bajar de 1.0 si histórico lo soporta).
- `preservation_max_rsi_entry=70` (subir de 65) para capturar rebotes en bear.
- `preservation_narrative_only=True` (no se toca).
- `preservation_kelly_mult=0.6` (subir de 0.5 para desplegar más capital defensivo).
- **Madurez:** solo tokens NARRATIVE con vol24h ≥ $5M.

### C8 — Paralelizar señales con 10 workers

- `ThreadPoolExecutor(10)` en `autonomous_signals.py`.
- Semáforo de 20 req/s a Binance.
- Cache de klines por ciclo para evitar llamadas duplicadas.
- **Madurez:** log de latencia por ciclo; alarma si >10s.

### C9 — Integrar datos de terceros Fase 1

- Nuevo módulo `external_data_client.py` con adapters para CoinGlass y Usenami.
- Features inyectadas:
  - `funding_rate_annualized` y `funding_percentile_24h`.
  - `oi_change_1h_pct`, `oi_change_24h_pct`.
  - `liquidation_cluster_nearest_pct` (distancia al próximo cluster de liquidaciones long/short).
  - `etf_net_inflow_1d_usd` y `coinbase_premium_index`.
- Usar en `entry_profile_gate` para FUTURES_SHORT y en `pnl_predictor` para SPOT_BULL macro dirección.
- **Madurez:** test de fallback si API cae; no bloquea ciclo.

### C10 — Nuevo rol: Analista Técnico Gráfico (fase 1 sin dependencias externas)

- Extraer features de OHLC 5m/15m/1h:
  - Patrones: engulfing, doji, morning/evening star (simplificado).
  - Estructura: higher-highs/lower-lows, soporte/resistencia de corto plazo.
  - Volumen relativo en la vela de entrada.
- Añadir al predictor y al gate como features opcionales.
- **Madurez:** backtest A/B con/sin features; incluir solo si mejora WR > 2%.

## 5. Plan de trabajo (maduración absoluta)

### Semana 1 — Fundamentos y SPOT/RANGE

| Día | Tarea | Entregable |
|-----|-------|------------|
| 1 | C1: armonía de parámetros, corregir drift, migrar tablas staged | 0 mismatches, tablas ready_* en prod |
| 2 | C2: propagación peak_quality_profile | Test + dashboard mostrando perfil real |
| 3 | C3: recalibrar RANGE_SCALP thresholds | `entry_profile_thresholds.json` actualizado |
| 4 | C4/C5: relajar B/C y fricción A | Config deployada, métricas en dashboard |
| 5-7 | Monitoreo + ajustes menores | Reporte semanal WR/PnL por perfil |

### Semana 2 — FUTURES_SHORT en producción real

| Día | Tarea | Entregable |
|-----|-------|------------|
| 1 | C6: activar futuros con 25% del capital del modo | 1-2 shorts abiertos, métricas OK |
| 2-3 | Escalar a 50% si WR>50% y DD<5% | 3-4 shorts |
| 4-5 | Escalar a 100% si métricas sostienen | 5 shorts, capital pleno desplegado |
| 6-7 | Recalibrar thresholds A/B/C de shorts con outcomes reales | `entry_profile_thresholds.json` actualizado |

### Semana 3 — PRESERVATION y datos externos

| Día | Tarea | Entregable |
|-----|-------|------------|
| 1-2 | C7: PRESERVATION más ofensivo | 4 posiciones narrative en bear |
| 3-4 | C9 Fase 1: integrar CoinGlass/Usenami funding+OI | `external_data_client.py` + tests |
| 5-7 | Validar que datos mejoran FUTURES_SHORT entry/exit | Métrica de mejora WR o EV |

### Semana 4 — Madurez operativa y gráfico

| Día | Tarea | Entregable |
|-----|-------|------------|
| 1-3 | C10: features de velas OHLC | Nuevo predictor feature set |
| 4-5 | C8: paralelización de señales | Latencia < 10s, 0 rate-limit errors |
| 6-7 | Documentación, runbooks, revisiones de seguridad | `SYSTEM_REFERENCE.md`, `.windsurf/rules.md`, runbook operativo |

## 6. Monitoreo y alarmas (madurez absoluta)

Cada cambio debe tener:

1. **Métrica objetivo** en dashboard.
2. **Umbral de éxito** a 7 días.
3. **Alarma automática** con rollback manual.
4. **Auditoría** en `trades_audit`.
5. **Nota en `SYSTEM_REFERENCE.md`**.

### Dashboard nuevo

- Capital desplegado por modo (% del líquido).
- Entradas por perfil A/B/C y modo.
- Win rate y PnL neto por perfil y modo (ventana 7d).
- FUTURES_SHORT: funding, OI, unrealized PnL, paridad DB↔Binance.
- Latencia de ciclo y rate-limit hits.
- Config drift status.

### Alarmas

| Alarma | Condición | Acción |
|--------|-----------|--------|
| WR perfil cae | WR<42% en 7d para cualquier perfil | Revertir thresholds de ese perfil |
| DD futuros | DD bucket futures >5% en 24h | Pausar nuevos shorts, revisar SL/TP |
| Latencia | Ciclo >20s por 3 ciclos consecutivos | Bajar workers o revisar DB |
| Ghost | Paridad Binance↔DB >0 | Ejecutar reconcile inmediatamente |
| Config drift | >0 mismatches | No operar hasta corregir |
| External data fail | API caída >15 min | Fallback a Binance, alertar |

## 7. Roles del sistema (revisión final)

Los roles de `rules.md` ya cubren casi todo. Se formalizan dos adicionales:

- **Analista Técnico Gráfico** (C10): patrones de velas, estructura de mercado, soporte/resistencia.
- **Experto en Ejecución y Microestructura**: slippage, colocación de órdenes, funding, OI, liquidaciones.
- **Macro / Liquidez Global**: ETF flows, M2, stablecoin supply, Coinbase Premium (ya cubierto parcialmente por Experto cripto).

No se requieren más roles. La maduración operativa viene de **aplicar disciplinadamente los 6 roles de cambio** de `rules.md` y de monitorear cada hipótesis.

## 8. Riesgos y mitigaciones actualizados

| Riesgo | Mitigación |
|--------|------------|
| FUTURES_SHORT consume todo el capital y squeeze alcista lo liquida parcialmente | Max 5 shorts, leverage 2x, mass-SL pause, DD bucket cap 5%/24h, funding/OI filter. |
| PRESERVATION más ofensivo entra en bear traps | narrative_only=True, vol24h ≥ $5M, max 4 posiciones, Kelly reducido. |
| Datos de terceros fallan y el ciclo se congela | Timeout 5s, fallback a Binance, cache 15min, no gate bloqueante en fase 1. |
| Paralelización excede rate limits de Binance | Semáforo 20 req/s, backoff exponencial, métrica de rate-limit hits. |
| Sobre-trading por relajar B/C | Rollback si WR<42% en 7d por perfil. |
| Pérdida de trazabilidad | Todo cambio en `trades_audit`, `SYSTEM_REFERENCE.md`, `.windsurf/rules.md`. |

## 9. Métricas de éxito a 30 días

| Métrica | Objetivo 7d | Objetivo 30d |
|---------|-------------|--------------|
| Entradas SPOT_BULL + RANGE_SCALP | ≥ 10 | ≥ 60 |
| Win rate neto SPOT | ≥ 48% | ≥ 52% |
| PnL neto SPOT | > $20 | > $150 |
| Shorts FUTURES abiertos (al menos 1 ciclo) | ≥ 3 | ≥ 20 |
| PnL neto FUTURES | > -$5 | > $50 |
| PRESERVATION entradas en bear | ≥ 2 | ≥ 10 |
| Latencia ciclo señales | < 10s | < 8s |
| Config drift / ghost / paridad | 0 | 0 |
| Uptime del sistema | > 99% | > 99% |

## 10. Próximos pasos inmediatos

1. **Aprobar plan v3**.
2. **Confirmar API keys** para CoinGlass y/o Usenami (o autorizar uso de free tier de Usenami + MMT trial).
3. **Comenzar Fase 0** (armonía de parámetros) sin tocar capital real.
4. **Definir capital inicial para FUTURES_SHORT** en Semana 2: ¿todo el margen disponible desde el día 1, o despliegue escalonado 25% → 50% → 100% en 3 días?

---

**Reglas duras respetadas:** PnL neto en decisiones, persistencia solo tras fill confirmado, `min_signal_strength >= 0.40`, no forzar trades sin convicción, paridad Binance↔DB, auditoría inmutable en `trades_audit`, no hardcodear parámetros, documentación canónica actualizada.

**Nota de almacenamiento:** este archivo es el registro maestro de `Calibracion_Entradas` para futuro estudio. Se actualizará tras cada fase con resultados reales.

---

## 11. Fusión con `calibracion_entradas-db0300.md` (2026-07-23)

El documento `calibracion_entradas-db0300.md` fue fusionado en este archivo maestro. A continuación los hallazgos críticos de esa sesión que quedan vigentes y que alimentan los items C2-C5 y el diagnóstico de salidas:

### Hallazgos de producción (19 jul 2026)

- **Posiciones abiertas:** 4 (BTC, ONDO, SOL, XRP), todas `RANGE_SCALP`, `entry_profile_label=A` (perfil de régimen), `peak_quality_profile=EMPTY`.
- **Últimas 200 cerradas:** win-rate 51%, PnL total **-306.33 USDT**, promedio -1.53 USDT/trade (-0.15%).
- **Pérdidas principales:** `stop_loss` (-333.61, 38 trades, 0 wins), cortes `unified_governance_loss_*` (-216.14, 23 trades), `active_replacement` (-53.82).
- **Ganancias principales:** `prs_peak_protection` (+206.42, 47/47 wins), `profit_predexit_capture` (+108.03, 13/13 wins), `sso_target_contribution` (+27.34).
- **Perfil predominante A:** no es calidad de entrada, es el perfil de régimen activo.

### Problemas raíz confirmados

1. **`peak_quality_profile` no se propaga:** `_filter_buy_candidates` escribe el resultado en una copia local de `meta`, no en `cand` ni `signal_meta[symbol]`. El sizing y salidas usan perfil de régimen A en lugar del perfil real de calidad.
2. **Umbrales `RANGE_SCALP` degenerados:** `entry_profile_thresholds.json` tenía A=B=C=0.0917, por lo que el gate no diferenciaba tiers de riesgo.
3. **Predictor de entrada optimista:** `entry_pred_pnl` promedio +2.51 USDT (174/197 positivos), correlación con PnL real: **-0.18**. Los SL agresivos convierten entradas predichas positivas en pérdidas.
4. **Staged entry no persistía en producción:** faltaban tablas `ready_candidates`/`ready_candidate_checks`; fallback compraba inmediatamente sin re-validación.
5. **`entry_quality_gate` en modo advisory:** pasaba señales a pesar de warnings (`RANGE_VOL`, `SPOT_BULL_4H_DOWN`, `SPOT_BULL_VOL`).

### Acciones derivadas vigentes

- C2: propagar `peak_quality_profile` a metadata de posición.
- C3: recalibrar thresholds A/B/C de `RANGE_SCALP` con datos reales.
- C4/C5: relajar B/C solo en regímenes confirmados con alta confianza y reducir fricción del staged entry.
- Auditoría de salidas: las pérdidas grandes vienen de SL/UG, no de entradas excesivas; esto justifica el plan hermano de **calibración del ciclo de vida** (`calibrar-ciclo-vida-52fe5d.md`).

## 12. Fundamento de datos y condición de no-promoción — 2026-07-23

- La autoridad de fuentes, el linaje y el contrato de features viven en `calibrar_datos.md`.
- Se implementaron `build_research_manifest.py` y `build_research_outcomes.py`: el primero versiona inputs y hashes; el segundo agrupa cierres parciales por posición y enlaza `ready_candidate_id` cuando existe.
- Se generó `executed_position_outcomes_v1`: 1,038 posiciones de 180 días; 814 elegibles para investigación, 10 con enlace exacto staged y 1,028 legacy sin enlace. Los históricos no enlazados no se usan para demostrar aceptación staged.
- Se introdujo el contrato `range_scalp_live_v1` antes del gate, persistido en señal, staging y metadata de fill. La telemetría conserva valores nulos y completeness; no imputa cero.
- El modelo RANGE_SCALP continúa en `shadow`: el walk-forward actual reporta OOF corr -0.0186, 306 registros y ninguna threshold A elegible. No se relajan thresholds, Kelly, B/C ni capital por esta evidencia.
- Próximo gate: completeness >=95% en cohorte shadow, outcome neto agrupado, walk-forward temporal purgado, PnL neto >0, PF>=1.15, cobertura>=5%, DD no peor que baseline y n>=30 para A en cada holdout.

## 13. Descubrimiento agnóstico de activos 2h/4h — 2026-07-24

- Se implementó un `opportunity_shadow` en `pair_screener.py`. Consume todo el ticker SPOT-USDT crypto disponible, clasifica activos `OBSERVE` o `ELIGIBLE` por liquidez y conserva un ranking barato de hasta 80 oportunidades por volatilidad/movimiento y volumen.
- Esta capa es estrictamente de observación: cada candidata lleva `execution_enabled=false`, el meta-reporta `execution_enabled=0` y no se modifica el universo de órdenes, QUBO, staging, Kelly, SL/TP ni routing existente.
- El snapshot restart-safe `trading_runtime_state.screener_snapshot` ya conserva embudo `ticker_rows → structural_excluded → observe → liquid_eligible → futures_volume_eligible → shadow_ranked` y `GET /screener/opportunity-shadow` expone la lectura autenticada.
- No existe todavía predicción ejecutable de retorno neto 2h/4h. Antes de promoción se requieren labels separados por horizonte/mercado/lado, costes y fricción, validación temporal purgada, paper/prospectivo y los gates de la sección 12. Los activos `OBSERVE` no pueden abrir operaciones.

## 14. Resolución temporal RANGE_SCALP — 2026-07-24

- El timeframe primario existente ya es `1h`, por debajo de 2h; el contexto macro usa `4h`. Cambiar toda la estrategia a `15m` sin recalibrar invalidaría los periodos de RSI/BB/volumen, aumentaría ruido y no está justificado por el horizonte objetivo de 2–4h.
- Se adopta arquitectura multi-timeframe: `1h` conserva contexto y generación de setup; `4h` conserva contexto de mercado; `15m` queda como telemetría de timing de entrada para candidatas RANGE_SCALP ya seleccionadas. No modifica ranking, profile gate, staging ni órdenes.
- `AutonomousSignalGenerator` adjunta `range_scalp_timing_shadow` con RSI, posición BB, spike de volumen y retornos 15m/1h para el conjunto final. Esta muestra permite comprobar de forma prospectiva si el timing 15m mejora retorno neto o sólo añade ruido.
- `opportunity_labels.py` y `Dev/scripts/build_opportunity_forward_labels.py` producen labels reproducibles `forward_gross/net/mfe/mae_2h` y `_4h` desde OHLCV 1h. El coste redondo descuenta comisión y slippage de entrada/salida; LONG y SHORT se etiquetan de forma distinta.
- No promover `15m` a gate de entrada hasta comparar walk-forward purgado por cohorte contra baseline 1h, con PnL neto, PF, MAE, DD, cobertura y sensibilidad a 1x/1.5x/2x de fricción.
