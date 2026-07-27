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

## Estado del sistema — jul 27 2026 — s100: RANGE_SCALP FUTURES_SHORT-first routing

### Política operativa

- `RANGE_SCALP` separa las señales antes del profile gate y QUBO:
  - `FUTURES_SHORT`: SELL/HOLD, breakdown, resistencia (`sr_zone_position >= 0.65`), banda superior o RSI >= 60; siempre se ordenan antes que la cohorte residual.
  - `FUTURES_LONG_ANALYSIS`: candidatas sin setup SHORT; solo se analizan como LONG futures y nunca se enrutan a SPOT.
- La cohorte SHORT usa predictor `trade_type=futures`, `trade_side=SHORT` y el tamaño `range_scalp_short_usdt_per_trade` antes de profile gate, profit score y QUBO.
- Se mantiene `range_scalp_long_entries_enabled=false`: los LONG pueden ser auditados, pero no se abren mientras no haya evidencia operativa de fills SHORT consistentes.

### Flujo y garantías

1. `autonomous_signals` genera BUY/SELL/HOLD y adjunta S/R sin look-ahead.
2. El ranking prioriza absolutamente la cohorte `FUTURES_SHORT`; soporte/reversión queda como fallback de análisis.
3. Profile A/B/C, predictor, QUBO y staging siguen siendo gates obligatorios; el cambio no altera Kelly, leverage, SL/TP ni umbrales de perfil.
4. El arbiter evalúa SHORT antes de cualquier LONG; la intención `FUTURES_LONG_ANALYSIS` evita `SPOT_LONG`.
5. Antes de la apertura se mantienen confirmación direccional, capacidad, preflight y verificación de Binance.
6. Solo tras fill confirmado se persiste `trades_open`, `ready_candidates.opened` y la auditoría de ejecución.

### Corrección crítica de persistencia

- Se corrigió `FuturesClient.open_short`: después de confirmar el fill de Binance, la persistencia de `regime_at_entry` referenciaba `settings` fuera de alcance y provocaba `NameError`.
- El error hacía que el ciclo reportara `RANGE_SCALP_SHORT_EXECUTION_FAILED` aunque Binance ya hubiera abierto la posición. La persistencia usa ahora la configuración del propio cliente.

### Observabilidad y aceptación

- `trades_audit.range_scalp_direction_arbitration` conserva `routing_intent`, modo seleccionado, elegibilidad, geometría y predicciones.
- Medir por side, perfil y cohorte: cobertura, fill confirmado, PnL neto, PF y drawdown frente a baseline/changepoint 3y/60000.
- Dashboard y `/metrics/entry-evidence` deben mostrar el funnel por `routing_intent`, `selected_mode`, razón de rechazo y resultado de ejecución.

### Rollback

```powershell
PATCH /config/kelly {"range_scalp_short_preferred": false}
```

- Revertir el cambio de ranking si la cohorte SHORT deteriora PnL neto, PF, cobertura o drawdown frente al baseline.
- Mantener `range_scalp_long_entries_enabled=false` independientemente del rollback del ranking hasta validar fills y persistencia SHORT.

---

## Estado del sistema — jul 26 2026 — s99d: FUTURES_SHORT Eligibility Threshold

### Cambio aplicado

- **Umbral independiente para FUTURES_SHORT** (`directional_arbiter.py`):
  - `range_scalp_short_min_probability=0.40` evalúa la elegibilidad de la predicción SHORT.
  - LONG spot y LONG futures conservan `range_scalp_direction_min_probability=0.55`.
  - Se mantiene el requisito de `expected_pnl > 0`, pertenencia al universo futures y todos los gates de ejecución existentes.
  - Motivo: RANGE_SCALP prioriza candidatos cerca de soporte; el predictor direccional penaliza correctamente los SHORT en esa geometría, por lo que el umbral LONG compartido de 0.55 impedía que un SHORT con EV positivo fuese elegible.

### Configuración y validación

- Default canónico: `range_scalp_short_min_probability=0.40` en `config.py` y `config_helpers.py`.
- Hot-config permitido: `PATCH /config/kelly {"range_scalp_short_min_probability": 0.40}`.
- Regresión: `tests/test_directional_arbiter.py` valida que SHORT con probabilidad `0.45` y EV positivo sea `FUTURES_SHORT`, mientras LONG con `0.54` no cumple su umbral de `0.55`.
- Validación ejecutada: `python -m pytest tests/test_directional_arbiter.py -q` → `8 passed`.

### Rollback

```
PATCH /config/kelly {"range_scalp_short_min_probability": 0.55}
```

---

## Estado del sistema — jul 26 2026 — s99c: CapBridge Arbiter-Aware + Just-in-Time Capital Transfer + Kelly FUTURES_LONG

### Cambios aplicados

- **Fast-path arbiter fallback rules** (`run_trades.py`):
  - `FUTURES_LONG` falla → fallback a SPOT LONG (misma dirección, compatible).
  - `FUTURES_SHORT` falla → candidato bloqueado con gate `FUTURES_SHORT_NO_MARGIN` (SPOT solo opera LONG).
  - Root cause ACEUSDT: arbiter decidía `FUTURES_LONG` con wallet vacío → `amount < $10` → candidato atascado.

- **Kelly sizing completo para FUTURES_LONG** (`_open_arbitrated_range_scalp_long`):
  - Antes: tamaño fijo `futures_usdt_per_trade` ($150). Ahora: `_kelly_position_size()` con concentración, conviction, cap.
  - Fast-path: usa `policy.position_size_usdt` del staged (Kelly ya calculado en validación).
  - Path normal: calcula Kelly completo análogo a FUTURES_SHORT.

- **Just-in-time capital transfer** (`_open_arbitrated_range_scalp_long` y `_open_arbitrated_range_scalp_short`):
  - Si Kelly quiere $1500 y futures tiene $150: `check_and_fund($1502)` transfiere `shortfall × 1.1` de spot→futures.
  - Post-transfer: refresca balance y abre con el monto Kelly completo.
  - Si la transferencia falla: graceful degradation (usa lo disponible).
  - `check_and_fund` corregido: transfiere `shortfall × 1.1` (antes era `required × 2.0`).

- **CapBridge arbiter-aware funding** (`futures_client.py` + `run_trades.py`):
  - Nuevo parámetro `futures_arbiter_active` en `smart_rebalance_capital`.
  - **Case 1b**: si arbiter activo y `regime in (RANGE_SCALP, SPOT_BULL)`, asegurar `kelly_margin` en futures (pre-fondeo).
  - **Case 3 guard**: no drenar idle futures capital cuando arbiter está activo.
  - `_cb_kelly_margin` ahora usa `max(futures_usdt_per_trade, kelly_base_usdt, avail×cap)` en vez de solo $150.

- **Observabilidad**: `arbiter_mode` (SPOT_LONG/FUTURES_SHORT/FUTURES_LONG) visible en dashboard selection-observability.

### Flujo completo post-fix

```
CapBridge ciclo N: arbiter_active=True, futures=$10, kelly_margin=$200 → transfiere $190 spot→futures
Ciclo N+1: Candidato validado → arbiter FUTURES_LONG → Kelly=$1500
  → futures tiene $200, need $1500 → just-in-time: check_and_fund($1502)
  → transfiere $1430 de spot → futures=$1632 → abre con $1500 ✅
Si falla todo → fallback SPOT LONG (misma dirección)
```

### Rollback

```
# Revert CapBridge arbiter-aware:
PATCH /config/kelly {"range_scalp_short_first_enabled": false}
# Revert just-in-time (volver a cap fijo):
git revert <commit> -- run_trades.py futures_client.py
```

---

## Estado del sistema — jul 26 2026 — s99b: Direction Target/Stop Derivation Fix + Dashboard wslrelay Fix

### Cambios aplicados

- **Direction target/stop derivation fix** (`staged_entry.py`):
  - Bug: `stage_ready_candidates` usaba `policy_data.target_price` y `stop_price` del evaluador LONG genérico incluso cuando el arbiter decidía FUTURES_SHORT → `DIRECTION_TARGET_INVALID` (target arriba del precio para SHORT) → ningún candidato confirmaba dirección → 0 operaciones abrían.
  - Fix: nueva función `_build_initial_direction()` extrae target/stop de la predicción del arbiter según `selected_mode`:
    - SHORT: `target = price_range.min`, `stop = price_range.max`
    - LONG: `target = price_range.max`, `stop = price_range.min`
    - Sin arbiter: fallback a `policy_data` (comportamiento anterior preservado).
  - Verificado en producción: LDOUSDT abrió via `FAST_PATH_EXECUTED` en ~90s post-staging.
  - 3 tests de regresión añadidos en `test_staged_entry.py`.

- **Dashboard wslrelay fix** (`frontend/dashboard.js`):
  - Bug: `wslrelay.exe` (WSL2) interceptaba conexiones a `[::1]:8000` (IPv6 localhost) → el dashboard nunca recibía respuesta del API.
  - Fix: `API_BASE` ahora fuerza `127.0.0.1` cuando hostname es `localhost`, bypaseando wslrelay.
  - URL correcta: `http://127.0.0.1:8000/frontend/index.html`.

### Evidencia diagnóstica

| Candidato | Arbiter | direction.side | target (antes) | target (después) | Resultado |
|---|---|---|---|---|---|
| RIFUSDT id=1648 | FUTURES_SHORT | SHORT | 0.083 (LONG, > entry) | — | `DIRECTION_TARGET_INVALID` |
| RIFUSDT id=1661 | FUTURES_SHORT | SHORT | 0.079 (< entry ✓) | — | discarded (price no favoreció) |
| LDOUSDT id=1660 | FUTURES_SHORT | SHORT | 0.379 (< entry ✓) | — | **FAST_PATH_EXECUTED** ✅ |
| HBARUSDT id=1659 | FUTURES_SHORT | SHORT | 0.069 (< entry ✓) | — | **confirmed → validated** ✅ |

### Rollback

```
# Revert direction derivation (usar policy directamente):
git revert <commit> -- staged_entry.py
# Revert dashboard fix:
git revert <commit> -- frontend/dashboard.js
```

---

## Estado del sistema — jul 26 2026 — s99: Directional Arbiter, Hard-Risk Monitor, Fast-Path Allocation Fix

### Cambios aplicados

- **Directional Arbiter** (`range_scalp_short_first_enabled=True`):
  - Antes de abrir spot long en RANGE_SCALP, evalúa si futures SHORT o futures LONG tiene mejor EV.
  - Nuevo módulo `directional_arbiter.py` con `evaluate_range_scalp()` → decisiones `SPOT_LONG`, `FUTURES_SHORT`, `FUTURES_LONG`, `ABSTAIN`.
  - Integrado en fast-path y `_buy_top_symbols`; audit en `trades_audit`.
  - `_open_arbitrated_range_scalp_short()` y `_open_arbitrated_range_scalp_long()` en `run_trades.py`.
  - Nuevo flag `range_scalp_short_preferred` (default `True`): cuando `True`, el arbiter elige `FUTURES_SHORT` siempre que sea elegible (prob>0.40, EV>0) y el par tenga futuros disponible. Rollback: `PATCH /config/kelly {"range_scalp_short_preferred": false}`.
  - `pnl_predictor._calculate_expected_pnl()` ahora usa los porcentajes configurados de SL/TP como el payoff real de la posición (no solo como cap sobre el rango estimado). Para FUTURES_SHORT con `range_scalp_short_sl_pct=2.0` y `tp_pct=4.0`, EV>0 requiere `prob > 2/(2+4) ≈ 33%`, haciendo viables shorts con `prob` entre 0.40 y 0.50. `_calculate_risk_metrics()` también refleja el riesgo/recompensa real del SL/TP.
  - `_open_arbitrated_range_scalp_short()` aplica Kelly sizing y fondeo JIT desde spot igual que el path LONG, en lugar de usar `range_scalp_short_usdt_per_trade` fijo ($1000). Se alinearon los defaults de `futures_leverage` a `10` en ambos paths SHORT/LONG.
  - `config_helpers.get_staged_entry_regime_config()` fuerza `direction_required_confirmations=1` y `direction_min_progress_bps<=1.0` para RANGE_SCALP, permitiendo confirmación direccional en un solo ciclo.
  - Cuando activo, `_filter_buy_candidates` se salta para RANGE_SCALP (el arbiter filtra internamente).
- **Direction confirmation re-enforced para RANGE_SCALP**:
  - `config_helpers.py` fuerza `direction_confirmation_mode='enforced'` cuando `range_scalp_short_first_enabled=True`.
  - `direction_required_confirmations=1`, `direction_max_adverse_bps=150.0`.
- **Hard-Risk Monitor**:
  - Thread independiente en `orchestrator.py` cada 2s ejecutando `hard_risk_monitor.monitor_spot_hard_stops_once()`.
  - Config: `hard_risk_monitor_enabled`, `hard_risk_monitor_interval_seconds`.
- **Fast-path allocation fix**:
  - Bug: `expected_pnl` (~$3) se usaba como `position_size_usdt` → `ALLOCATION_TOO_SMALL` para todos los candidatos.
  - Fix: `_policy_size = float(policy.get("position_size_usdt") or 0)` sin fallback a `expected_pnl`.
- **Screener futures ampliado**: `screener_futures_min_vol=$10M` (era $100M), `screener_max_futures_pairs=100`.
- **Dashboard**: endpoints `/price-history/{symbol}` acepta `trade_type` (spot/futures); posiciones cerradas incluyen `position_id`, `position_mode`.

### Validación

- Suite completa: **674 passed, 14 skipped**.
- Re-verificación focalizada jul 27 2026: `test_pnl_predictor_candles.py`, `test_pnl_predictor_side.py`, `test_directional_arbiter.py`, `test_range_scalp_bidirectional.py`, `test_pnl_service_side.py`, `test_staged_entry_fast_path.py`, `test_entry_quality_gate.py`, `test_config_governance.py` — pasados.
- Operaciones abiertas post-fix: EDUUSDT, QIUSDT ×3 via fast-path.

### Rollback

```
PATCH /config/kelly {"range_scalp_short_first_enabled": false, "hard_risk_monitor_enabled": false}
```

---

## Estado del sistema — jul 24 2026 — s94: Inyección completa de features SR, relajación de gates y suite 602 verde

### Cambios aplicados

- **SR features inyectadas en todo el pipeline**:
  - `pnl_predictor.py`: probabilidad de éxito y rango de precios anclados a SR para LONG/SHORT.
  - `entry_profile_gate.py`: features de distancia/posición de zona SR en el modelo universal y bonus heurístico por eventos.
  - `autonomous_signals.py`: extracción, reenvío y señales BUY/SELL por rebote/breakout/breakdown de SR; ranking por régimen bonificado.
  - `staged_entry.py`: fricción reducida para señales SR confirmadas y filtro por `top_symbols_qubo`.
  - `run_trades.py`: reenvío de SR features y bypass del descarte inicial de perfil para eventos SR confirmados.
- **Relajación de gates**:
  - `entry_decision.py`: `entry_decision_min_probability` default **0.50** para todos los regímenes.
  - `staged_entry.py`: `required_positive_checks=1`, `direction_required_confirmations=1`, `direction_min_progress_bps=1.0`, `max_cycles>=4` para señales SR confirmadas.
- **Configuración agresiva de exposición y Kelly**:
  - `config.py` / `config_helpers.py`: `max_positions_spot_bull=12`, `max_positions_range_scalp=8`, `max_positions_preservation=4`, `max_positions_futures_short=8`, `max_total_exposure_pct=0.95`, `max_positions=8`, `max_open_per_cycle=2`, `category_max_positions_enabled=False`.
  - `market_regime.py`: `kelly_mult` SPOT_BULL=1.05, RANGE_SCALP=1.0, PRESERVATION=0.7, FUTURES_SHORT base=0.6 con boost por confianza (1.0 si `confidence>=0.80`).
- **Alineación de defaults canónicos**:
  - `entry_decision_min_probability`, `category_max_positions_enabled`, `screener_max_pairs` sincronizados entre `config.py`, `config_helpers.py`, `run_trades.py` y `universe_selector.py`.
  - Tests actualizados: `test_staged_entry.py`, `test_system_health.py`, `test_range_scalp_entry_pipeline.py`, `test_entry_evidence.py`.
- **Calibración**: `recalibrate_thresholds_v2.py` y `build_universal_quality_model.py` incluyen features SR.

### Validación

- Suite completa: **602 passed, 0 failed**.
- `tmp_audit_config.py`: 0 critical mismatches.

### Monitorización

- Confirmar aperturas validadas en próximos ciclos con `sr_bounce`, `sr_breakout`, `sr_breakdown`.
- Auditar `sr_entry_shadow_rejected` para evitar sombrear oportunidades rentables.
- Seguir PnL/drawdown dados los nuevos límites de exposición y multiplicadores Kelly.

### Rollback

```powershell
PATCH /config/kelly {"staged_entry_enabled": false}
# o revertir los cambios en pnl_predictor.py, entry_profile_gate.py, staged_entry.py y config.py
```

---

## Estado del sistema — jul 24 2026 — s93b: Fix RANGE_SCALP e integración S/R

### Cambios aplicados

- **Fix RANGE_SCALP bloqueado** en `staged_entry.py` + `pnl_predictor.py`:
  - `staged_entry.build_range_reversion_prediction` lee Bollinger Bands de la forma plana (`lower_band`, `middle_band`, `upper_band`) que produce `analyze_signal`, además de la forma anidada.
  - `pnl_predictor.predict_range_reversion` ponía el stop estructural exactamente en `lower`, rechazando entradas legítimas sobre soporte (`band_position ≈ 0`) con `invalid_reversion_payoff` → `DIRECTION_TARGET_MISSING`. Si el precio rompía ligeramente por debajo de `lower`, el stop estructural quedaba por encima del precio de entrada.
  - Root cause combinado: la predicción de reversión fallaba o era inválida; el gate caía al predictor LONG genérico (comportamiento SPOT_BULL) y `staged_entry` descartaba con `DIRECTION_TARGET_MISSING`.
  - Se corrigió para leer ambas formas de BB keys y para elegir el stop más alto entre el estructural (`lower * (1 - configured_sl/1000)`) y el configurado (`price * (1 - configured_sl)`) que esté estrictamente por debajo del precio actual. Esto permite entradas en soporte/inferior a la banda inferior sin forzar operaciones en el techo del rango.
- **Fix RANGE_SCALP bloqueado en `entry_profile_gate`** en `run_trades.py`:
  - `_normalize_autonomous_signal` no reenviaba las features que el modelo universal espera (`range_position`, `rsi_1h`, `rsi_4h`, `trend_1h`, `trend_4h`).
  - Sin ellas, `_extract_universal_features` usaba proxies débiles (`bb_position` para `range_position`, `ema_cross` para trend, mismo RSI para 1h/4h), bajando sistemáticamente el score de calidad y descartando todos los candidatos `RANGE_SCALP` con `PEAK_QUALITY_DISCARDED`.
  - Se añadieron esas keys al dict normalizado; tras el fix un candidato `KAITOUSDT` pasó el gate y fue staged en el siguiente ciclo.
- **Calibración agresiva temporal para RANGE_SCALP** (para desatascar aperturas):
  - `pnl_predictor.py`: se suavizaron penalizaciones del predictor de reversión por volumen bajo, RSI en sobreventa (<35) y tendencia bajista cuando el precio está cerca de la banda inferior.
  - `entry_decision.py`: umbral mínimo de probabilidad para `RANGE_SCALP` de 0.60 -> 0.50.
  - `config_helpers.py`: `required_positive_checks` 2->1, `direction_required_confirmations` 2->1, `direction_min_progress_bps` 3.0->1.0, `max_open_per_cycle` 1->2.
  - Resultado: 5-7 candidatos ACCEPTED por ciclo y `KAITOUSDT` validado/abierto (`entry_price=0.9472`, size ~$1,970, unrealized -0.51%).
  - ⚠️ Experimental; rollback documentado.
- **Overrides runtime re-aplicados** vía `trading_runtime_state.kelly_config_override` y contenedor reiniciado:
  - `category_max_positions_enabled = False`
  - `entry_quality_vol_spike_min_range_scalp = 0.20`
- **Integración Soporte/Resistencia**:
  - `support_resistance.py`: cálculo sin look-ahead (pivotes locales + clustering).
  - `autonomous_signals.py`: inyección de niveles SR, distancias, `sr_zone_position` y aliases para el modelo.
  - Filtro de entrada SR (`shadow` por defecto) y disparador de salida SR en `peak_capture_engine.py` (`profit_predexit_sr_resistance`, prioridad 91).
- **Modelo universal recalibrado** sobre `cross_symbol_dataset.jsonl` actual (397.081 filas); thresholds A/B/C separados.

### Validación

- Tests relevantes: **63 passed**.
- Smoke test: `entry_decision.evaluate(..., regime='RANGE_SCALP', profile='A')` retorna `eligible=True` usando el predictor de reversión.
- Config cargada y verificada tras restart:
  - `category_max_positions_enabled = False`
  - `entry_quality_vol_spike_min_range_scalp = 0.2`

### Monitorización

- Confirmar en las próximas 6-12h que `ready_candidates` deja de descartar por `DIRECTION_TARGET_MISSING` y aparecen `validated`/`opened` en RANGE_SCALP.
- Auditar logs `sr_entry_shadow_rejected` para no bloquear oportunidades rentables.
- Comparar cohortes con `score_source == "universal_model"`.

### Rollback

```powershell
# Revertir staged_entry.py
PATCH /config/kelly {"staged_entry_enabled": false}
```

---

## Estado del sistema — jul 23 2026 — s93: Umbrales universales desplegados

El Escenario 2 del plan de universalización está **activo en producción**.

### Cambios aplicados

- **`entry_profile_thresholds.json`**: reemplazado por los umbrales universales por **régimen + categoría** (`SPOT_BULL`, `RANGE_SCALP`, `LARGE_CAP`, `NARRATIVE`, `MIDCAP_ALT`). Backup: `entry_profile_thresholds_pre_universal_20260723.json`.
- **`entry_profile_gate.py`**:
  - Detecta formato universal por `meta.version` o claves `regime__categoria`.
  - Carga `quality_score_model_universal_proposal.pkl`.
  - Extrae features genéricas (`rsi_1h`, `rsi_4h`, `bb_position`, `atr_pct`, `vol_spike`, `range_position`, `trend_1h`, `trend_4h`) de `signal_meta`, `indicators` e `indicators_at_entry`.
  - Predice probabilidad de `forward_net_4h > 0` y aplica el umbral más específico.
  - Mantiene compatibilidad con el formato legacy de thresholds y el pipeline SPOT_BULL/RANGE_SCALP anterior.
- **Tests**: `tests/test_entry_quality_gate.py` actualizado para usar thresholds pre-universales en assertions legacy; suite relevante pasa.
- **Runtime**: `docker compose restart app` aplicado.

### Validación previa al despliegue

- Walk-forward purgado (76 ventanas): WR medio 69.7%, PF medio 4.55, cobertura 11.6%.
- MIMIC retrospective sweep ligero (3 ventanas): WR medio 66.2%, PF medio 2.90, cobertura 12.3%.

### Monitorización obligatoria

- Verificar `score_source == "universal_model"` en `entry_profile_gate_info` de `ready_candidates` y `trades_open`.
- Comparar cobertura real (~8-15%) y PnL neto con proyecciones.
- Si el PnL real no confirma en 1-2 semanas: restaurar backup thresholds y reiniciar.

### Rollback inmediato

```powershell
Copy-Item Dev/benchmarks/entry_profile_calibration/entry_profile_thresholds_pre_universal_20260723.json Dev/benchmarks/entry_profile_calibration/entry_profile_thresholds.json -Force
# o dentro del contenedor:
docker compose exec app cp Dev/benchmarks/entry_profile_calibration/entry_profile_thresholds_pre_universal_20260723.json Dev/benchmarks/entry_profile_calibration/entry_profile_thresholds.json
docker compose restart app
```

---

## Estado anterior del sistema — jul 22 2026

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
- **C2 — Propagar `peak_quality_profile`**: completado en s93. El `entry_profile_gate` registra `score_source`, `category`, `model_version` y `peak_quality_profile` en `signal_meta` y metadata de posición.
- **C3 — Recalibrar thresholds RANGE_SCALP A/B/C**: completado en s93 mediante el modelo universal. El archivo `entry_profile_thresholds.json` ahora contiene thresholds por régimen + categoría y el gate aplica el modelo universal `quality_score_model_universal_proposal.pkl`.
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
