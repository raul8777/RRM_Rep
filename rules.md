# Reglas de operación — crypto-agent

> Archivo vivo. Añadir patrones, decisiones de diseño y reglas de operación que afecten el comportamiento del sistema.

---

## 0. Arquitectura canónica de calibración de entrada

- El documento maestro `canonical/calibracion_entradas-a20ab8-v3.md` debe iniciar con el diagrama de arquitectura y la descripción de módulos del pipeline de entrada.
- Cualquier cambio de flujo en calibración de entrada debe actualizar primero ese bloque canónico y después reflejarse en este archivo y en `SYSTEM_REFERENCE.md`.
- Mapa operativo mínimo que debe mantenerse consistente:
  - `market_regime.py`: modo activo y contexto.
  - `pair_screener.py` + `autonomous_signals.py`: candidatos y features.
  - `support_resistance.py`: niveles SR sin look-ahead + eventos `sr_bounce`/`sr_breakout`/`sr_breakdown`/`sr_rejection` (s108).
  - `directional_arbiter.py`: arbitraje LONG/SHORT en RANGE_SCALP (`evaluate_range_scalp`) usando estructura SR + predicciones.
  - `entry_profile_gate.py`: clasificación A/B/C/discard.
  - `portfolio_selector.py`: selección priorizada.
  - `staged_entry.py`: lifecycle `ready -> validated -> opened/discarded`.
  - `pnl_service.py` + `entry_decision.py` + `pnl_predictor.py`: validación EV/probabilidad/target-stop (incl. `predict_range_reversion`, consciente de dirección desde s108).
  - `run_trades.py`: gates finales y ejecución.
  - `analysis_cycle.py`: refresco paralelo de señales (C8).
  - `external_data_client.py`: enriquecimiento externo no bloqueante (C9).
  - `technical_graph_analyst.py`: features OHLC y microestructura (C10).
  - `app.py`: hot-config y observabilidad.

### Contrato SR de espera y apertura

- La ruta canónica de entradas SR es: `setup_detected` -> `ready` -> re-check inter/intra ciclo -> `direction_confirmed` -> `validated` -> preflight -> orden -> `fill` confirmado -> `opened`.
- Un candidato SR en `ready` permanece en espera activa hasta confirmar rompimiento/progreso direccional; no se permite apertura directa por score, QUBO o predictor sin confirmación de dirección.
- Un SHORT no puede abrir a `<=0.5%` del soporte con RSI `<50` salvo que `support_breakdown_sell`/`sr_breakdown` confirme continuación. El gate usa `range_scalp_short_near_support_*`, es hot-configurable y falla abierto si faltan ambas features.
- Todo bloqueo en preflight o ejecución debe persistirse con razón canónica en `ready_candidates.data` y evento de auditoría correspondiente.
- No existe apertura válida sin confirmación de fill en exchange.

### Contrato de riesgo FUTURES

- El PnL contractual de FUTURES es `(mark-entry)*qty` para LONG y `(entry-mark)*qty` para SHORT; nunca se vuelve a multiplicar por leverage.
- `position_telemetry_watcher` debe leer `qty`, crear un snapshot estable por `symbol_side` y normalizar velocidad por precio antes de evaluar drawdown.
- `hard_risk_monitor` aplica `max_loss_usd_per_trade` también a FUTURES en su loop de 2s; el motivo canónico es `max_loss_usd_stop`.

### Protocolo recurrente AIC

- Toda revisión periódica de pérdidas usa **AIC — Análisis Inteligente de Cierres** (`canonical/analisis_inteligente_cierres.md`) y un ID `AIC-YYYYMMDD-NN`.
- Debe separar A) no abrir, B) proteger después de abrir y C) reparar/calibrar configuración existente antes de agregar lógica paralela.
- La unidad de comparación es la tesis `(symbol, entry_time)`, no cada cierre parcial.
- Ningún gate se promueve sin reportar pérdidas evitadas, ganancias sacrificadas, mejora neta, tamaño de muestra y limitaciones.

## 1. Staged entry lifecycle

- Las posiciones NO se abren inmediatamente tras el QUBO. El candidato pasa primero al estado `ready` en `ready_candidates`.
- Estados válidos: `ready`, `validated`, `opened`, `discarded`, `expired`.
- Un candidato `ready` debe acumular `required_positive_checks` positivas antes de pasar a `validated`.
- Los candidatos `validated` se seleccionan globalmente dentro del régimen/trade type activo, en orden de prioridad persistida por la política del régimen y limitados por `max_open_per_cycle`; no necesitan reaparecer en el QUBO del ciclo siguiente.
- La política se reevalúa antes de cada apertura; si falla, el candidato pasa a `discarded` con `discard_reason`.
- QUBO/diversificación es una guarda independiente: pasar la política no sustituye la selección QUBO.

## 2. Estados de presentación y observabilidad

- Los estados persistidos del lifecycle continúan siendo `ready`, `validated`, `opened`, `discarded` y `expired`; las etiquetas del dashboard son una proyección de sólo lectura y no modifican ejecución, ranking ni gates.
- `En validación` corresponde a `ready`.
- `Lista para abrir` corresponde exclusivamente a una fila `validated` del régimen activo, sin posición duplicada y dentro del ranking/cupo `max_open_per_cycle`.
- `Validada · en cola` corresponde a una fila `validated` fuera del cupo de apertura actual.
- `Bloqueada por régimen` corresponde a un candidato activo cuyo `regime_mode` no coincide con el régimen runtime actual.
- `Duplicada · posición abierta` corresponde a un símbolo que ya existe en posiciones abiertas.
- `Expirada`, `Rechazada · descartada` y `Aceptada · abierta` son estados finales de presentación.
- Una señal reciente sin decisión persistida se etiqueta `Sin decisión final`; no debe presentarse como orden pendiente. Una señal `pending` se etiqueta `En revisión` y una señal vencida por TTL se etiqueta `Expirada`.
- El contrato de `/signals`, `/signals/recent` y `/dashboard/selection-observability` expone `display_status`, `display_label` y `actionable`; el último endpoint expone además `display_counts`.

## 3. Re-chequeos

- `inter-cycle`: evaluación entre ciclos. Se ejecuta como parte de `run_trades.execute_once`.
- `intra-cycle`: validación dentro del mismo ciclo, controlada por el scheduler de `orchestrator.py`.
- Cada re-chequeo genera un registro en `ready_candidate_checks` con `check_type`, `positive`, `execution_ms` y features.
- El `price_source` puede ser `avg` (precio promedio) o `ticker` (último precio), configurable por régimen.

## 4. Configuración por régimen

- `staged_entry_enabled` activa/desactiva el lifecycle global.
- `staged_entry_regime_config` contiene por régimen: `enabled`, `required_positive_checks`, `checks_per_cycle`, `max_cycles`, `max_age_min`, `intra_mode`, `price_source`, `require_predictor_valid`, `min_expected_pnl`, `policy_version` y `max_open_per_cycle`.
- Los cuatro regímenes están activados con `max_open_per_cycle=2` para permitir captura múltiple de señales SR; las señales SR confirmadas reducen `required_positive_checks` y `direction_required_confirmations` a 1.
- Cada operación recibe su perfil A/B/C al momento de la entrada a través de `entry_profile_gate` (`peak_quality_profile`). No existe un "perfil activo global" en runtime.
- SPOT_BULL, FUTURES_SHORT y PRESERVATION requieren `peak_quality_profile` A/B/C, predictor válido y EV neto actual estrictamente mayor a `min_expected_pnl`.
- RANGE_SCALP requiere `peak_quality_profile` A/B/C y predictor íntegro, pero se prioriza por calidad de reversión; el signo del predictor long genérico no es un veto.
- `preservation_upgrade_to_futures=False` mantiene PRESERVATION independiente; solo se convierte en FUTURES_SHORT al activarlo explícitamente.
- `range_reversion_predictor_mode=shadow` registra EV, probabilidad, target y stop de reversión sin bloquear aperturas.
- `entry_decision_min_probability` default **0.50** en todos los regímenes para captura agresiva en soporte.

## 5. MIMIC

- `SimulationDatabaseClient` implementa `ready_candidates` y `ready_candidate_checks` en memoria vía `_StagedTables`.
- El sweep `Dev/scripts/mimic_staged_entry_sweep.py` fuerza un régimen y calibra `staged_entry_regime_config`.
- En MIMIC se usa `forced_regime` para garantizar que el lifecycle reciba candidatos; `entry_profile_gate` se simula con el perfil forzado.

## 6. Soporte / Resistencia (SR)

- `support_resistance.py` calcula niveles SR sin look-ahead: pivotes locales con ventana de confirmación y clustering de precios.
- `autonomous_signals.py` inyecta en cada análisis `support_level`, `resistance_level`, `nearest_support_dist_pct`, `nearest_resistance_dist_pct`, `sr_zone_position` y los alias `sr_support_dist_pct` / `sr_resistance_dist_pct`.
- Filtro de entrada SR: si un BUY está presionando la resistencia (`sr_zone_position > 0.85` o distancia a resistencia <= ATR%%), se puede sombrear o rechazar según `sr_entry_filter_mode` (`shadow` por defecto, `active`, `disabled`).
- `peak_capture_engine.py` incluye disparador de salida `profit_predexit_sr_resistance` (prioridad 91) cuando la posición está cerca de la resistencia y ya hay beneficio.
- `staged_entry.py` reduce fricción para señales SR confirmadas (`support_bounce_buy`, `sr_bounce`, `support_breakout_buy`, `sr_breakout`, `support_breakdown_sell`, `sr_breakdown`): `required_positive_checks=1`, `direction_required_confirmations=1`, `direction_min_progress_bps=1.0`, `max_cycles>=4`.
- `run_trades.py` evita el descarte inicial de perfil (`PEAK_QUALITY_DISCARDED`) para eventos SR confirmados y reenvía las SR features al pipeline.
- El modelo universal se recalibró sobre el dataset actual **con features SR** y los umbrales A/B/C quedaron separados.
- **(s108, ago 1 2026)** `support_resistance.py` expone `sr_rejection` / `resistance_bounce_sell`: vela roja confirmada cerca de resistencia, mirror exacto de `sr_bounce`/`support_bounce_buy` (que solo cubría el lado del soporte). Sin este evento, un SHORT cerca de resistencia no tenía ninguna confirmación de reversión real, solo el umbral estático de zona.
- **(s108)** `directional_arbiter.py::short_resistance_setup` ahora es `short_breakdown OR resistance_bounce_sell/sr_rejection OR zone >= short_resistance_threshold` (antes solo `short_breakdown OR zone>=threshold`, sin ninguna vía basada en vela/volumen). Cambio aditivo: no elimina ninguna vía existente (`SHORT_GEOMETRY_PRIORITY` sigue intacto).
- **(s108)** `pnl_predictor.py::predict_range_reversion` ahora recibe `trade_side` y evalúa la rama SHORT con lógica simétrica (target/stop/probabilidad anclados a resistencia, no a soporte). Antes evaluaba **siempre** la rama LONG sin importar el lado real del candidato — ver detalle en §8.

Rollback:
```
PATCH /config/kelly {"sr_entry_filter_mode": "disabled"}
# Restaurar umbrales anteriores desde entry_profile_thresholds.json.backup_*
# Revertir pnl_predictor.py, entry_profile_gate.py, staged_entry.py, run_trades.py y config.py para volver a defaults conservadores
```

## 7. Rollback inmediato

```
PATCH /config/kelly {"staged_entry_enabled": false}
```

## 8. Patrones de bug recurrentes

- **MAX_CYCLES_REACHED antes de validar**: asegurar `max_cycles >= required_positive_checks + 1` para dar al menos `required_positive_checks` oportunidades.
- **Ready candidate descartado tras validar**: `_maybe_expire` debe saltar filas ya `validated`.
- **DB no inicializada en simulación**: `analysis_cache` debe caer a memoria cuando `connection_pool` no existe.
- **RANGE_SCALP no abre posiciones (s93b — jul 24 2026)**:
  - `staged_entry.build_range_reversion_prediction` buscaba Bollinger Bands en `bollinger_bands.{lower,middle,upper}`, pero `analyze_signal` las expone como `lower_band`, `middle_band`, `upper_band`.
  - `pnl_predictor.predict_range_reversion` ponía el stop estructural exactamente en `lower`, rechazando entradas en soporte (`band_position ≈ 0`) con `invalid_reversion_payoff` → `DIRECTION_TARGET_MISSING`. Si el precio rompía ligeramente por debajo de `lower`, el stop estructural quedaba por encima del precio.
  - El gate caía al predictor LONG genérico (comportamiento SPOT_BULL) y `staged_entry` descartaba los candidatos.
  - Fix: soportar ambas formas de BB keys y seleccionar el stop más alto entre el estructural (`lower * (1 - configured_sl/1000)`) y el configurado (`price * (1 - configured_sl)`) que esté estrictamente por debajo del precio actual, coherente con el criterio canónico de comprar cerca del soporte y no en el techo.
  - Nuevo root cause (post-recalibración): `_normalize_autonomous_signal` en `run_trades.py` no reenviaba las features que el modelo universal espera (`range_position`, `rsi_1h`, `rsi_4h`, `trend_1h`, `trend_4h`). `_extract_universal_features` usaba proxies débiles, bajando el score de calidad y descartando todos los candidatos `RANGE_SCALP` con `PEAK_QUALITY_DISCARDED`. Fix: añadir esas keys al dict normalizado.
  - También se re-aplicaron overrides `category_max_positions_enabled=False` y `entry_quality_vol_spike_min_range_scalp=0.20`.
  - Calibración agresiva temporal (jul 24 2026) para desatascar aperturas en mercado de miedo/bajo vol:
    - `pnl_predictor.predict_range_reversion`: penalizaciones suavizadas para volumen bajo, RSI <35 (sobreventa) y tendencia bajista en soporte.
    - `entry_decision.py`: `entry_decision_min_probability` para `RANGE_SCALP` de 0.60 -> 0.50.
    - `config_helpers.py`: `required_positive_checks` 2->1, `direction_required_confirmations` 2->1, `direction_min_progress_bps` 3.0->1.0, `max_open_per_cycle` 1->2.
    - Resultado: múltiples candidatos ACCEPTED y `KAITOUSDT` abierto.
    - **Rollback**: revertir los tres bloques de código anteriores y reiniciar; esto vuelve a bloquear entradas en condiciones actuales.
- **Modelo de reversión y arbitraje direccional ciegos a estructura real de resistencia (s108 — ago 1 2026)**:
  - Trigger: auditoría del trade `DIAUSDT` `FUTURES_SHORT` (`trades_closed.id=1350`, `directional_arbitration.reason=SHORT_GEOMETRY_PRIORITY`, -37.19 USDT) — se abrió con `sr_zone_position=0.2676` (lado del soporte, no de resistencia) y el propio `range_reversion_prediction` lo marcó `invalid_reversion_payoff`.
  - Causa raíz 1: `pnl_predictor.py::predict_range_reversion` no recibía ni usaba `trade_side` — **siempre** calculaba target por encima del precio y stop por debajo (lógica exclusivamente LONG), sin importar si el candidato evaluado era un SHORT. Confirmado con datos: en 30d de `RANGE_SCALP`, SELL con `valid=true` promedió -2.27 USDT vs +0.58 con `valid=false` (peor cuando "confirma"); BUY con `valid=true` promedió -7.95 (peor bucket de todo el análisis).
  - Causa raíz 2: no existía ningún evento de "rechazo en resistencia" (vela roja + cerca de resistencia) simétrico a `sr_bounce`/`support_bounce_buy`. `directional_arbiter.py::short_resistance_setup` solo tenía `short_breakdown` (ruptura de **soporte**, geométricamente incompatible con un short en zona de resistencia) o el umbral estático `zone>=0.65`, sin confirmación de vela/volumen.
  - Causa raíz 3 (bug independiente encontrado al conectar la anterior): `autonomous_signals.py` promovía `support_breakdown_sell` a señal `SELL` solo si `sr_zone_position >= 0.55` — invertido, ya que un breakdown de soporte por definición ocurre en zona **baja**. Confirmado: 0/157 SHORT de `RANGE_SCALP` en la ventana analizada tuvieron `sr_breakdown=true`.
  - Fix: `predict_range_reversion(..., trade_side=...)` con rama SHORT simétrica; `support_resistance.py` agrega `sr_rejection`/`resistance_bounce_sell` (+ `resistance_strength`/`resistance_volume_ratio` mirror de los de soporte); `autonomous_signals.py` corrige el umbral a `<=0.45` para breakdown y agrega la promoción a SELL por `resistance_bounce_sell` con `>=0.55`; `directional_arbiter.py::short_resistance_setup` reconoce la nueva señal (aditivo). `futures_client.py::open_short/open_long` + `run_trades.py` persisten `directional_arbitration_reason`, `sr_zone_position_at_entry`, `range_reversion_valid_at_entry` en `smart_meta` para poder medir esto sin joins frágiles por ventana de tiempo entre `ready_candidates` y `trades_closed`.
  - Validación: 497 tests pasan (3 fallos preexistentes de `test_config_governance.py`, no relacionados — no importan ninguno de los módulos tocados). Tests nuevos: `test_pnl_predictor_side.py` (simetría SHORT), `test_support_resistance.py` (detección de `sr_rejection`), `test_directional_arbiter.py` (`resistance_bounce_sell` prioriza `SHORT_RESISTANCE_PRIORITY` bajo el umbral de zona).
  - **No se tocó** el umbral de confirmación direccional (`direction_min_progress_bps=1.0`) ni la calibración de umbrales del arbiter — queda pendiente para una siguiente iteración, a evaluar con los datos nuevos que ahora se persisten.
  - **Rollback**: revertir `pnl_predictor.py`, `staged_entry.py`, `support_resistance.py`, `autonomous_signals.py`, `directional_arbiter.py`, `futures_client.py` y `run_trades.py` a los commits previos a s108; no requiere cambio de configuración (todos los cambios son de código, aditivos o correctivos, sin nuevas flags).
- **Precio obsoleto en re-chequeo + espera de confirmación consume el edge + contención de capital todo-o-nada (s109 — ago 1 2026)**:
  - Trigger: auditoría del trade `AVAXUSDT` `FUTURES_SHORT` — "Precio Inicial" 6.3460 (detección), "Aceptada" ~6.312 (última confirmación), "Abierta" 6.3100. De 22 minutos de espera, el sistema solo capturó ~30% del movimiento favorable ocurrido desde la señal; el resto se perdió esperando confirmación + 4 intentos de ejecución bloqueados por capital en ~10 minutos.
  - Causa raíz 1: `staged_entry.py::evaluate_inter_cycle` reutilizaba `meta.get("current_price")` (precio del último análisis de indicadores, hasta ~8 min viejo) en vez de pedir un precio fresco, siempre que el símbolo siguiera en el "top" del screening — usado para decidir la transición `ready→validated` con precio desactualizado.
  - Causa raíz 2: la máquina de estados de dirección (`_direction_transition`) solo corría para candidatos ya `validated`; en estado `ready` el watcher únicamente registraba el precio observado sin analizar dirección — ni para confirmar más rápido ni para descartar antes ante un movimiento adverso.
  - Causa raíz 3: la confirmación de dirección comparaba siempre contra el tick anterior (`progress_bps`), nunca contra el precio de la señal (`base_progress_bps`, ya calculado pero solo usado para el guard de drawdown adverso) — un movimiento favorable ya ocurrido entre la detección y la primera observación se descartaba por completo.
  - Causa raíz 4: `_reserve_capital_for_candidate` era todo-o-nada — un candidato confirmado que pedía $1000 con solo $400 disponibles se bloqueaba por completo (`CAPITAL_ROUTING_BLOCKED`) en vez de abrir con $400. El fast-path de SHORT además usaba `range_scalp_short_usdt_per_trade=1000` fijo, sin pasar por el sizing Kelly dinámico que el resto del sistema ya usa.
  - Causa raíz 5: el cooldown de reintento tras bloqueo de capital era fijo en 5 minutos, desproporcionado frente al watcher que ya re-chequea cada 10s.
  - Causa raíz 6: el ciclo principal (`_buy_top_symbols`) y el fast-path razonaban el capital disponible de forma completamente independiente (sin compartir reservas), pudiendo comprometer el mismo capital dos veces.
  - Fix: precio fresco siempre en `evaluate_inter_cycle`; `_watcher_process_row` corre la FSM de dirección también en `ready` (descarta ante rotura adversa, persiste confirmación temprana sin promover); `_direction_transition` acepta `base_progress_bps` como confirmación válida en la primera observación; nuevo gate `_revalidate_entry_before_submit` (R:R≥2.0 usando SR ya calculado como target + piso de distancia mínima 0.15%) justo antes de enviar la orden; `_reserve_capital_for_candidate` escala la posición al capital real disponible en vez de bloquear todo-o-nada (piso mínimo $15); cooldown de capital reducido a 30s configurables con `max_retries` escalado a 960 (mismo techo de 8h); ciclo principal y fast-path comparten la misma reserva de capital en memoria (`_CAPITAL_RESERVATIONS`). Además, nuevo `direction.first_confirmed_price`/`first_confirmed_at` persistido en `smart_meta` para medir el costo de espera sin reconstruir manualmente la línea de tiempo.
  - Verificado: 518 tests pasan (3 fallos preexistentes de `test_config_governance.py`, no relacionados).
  - **No se cambió**: los umbrales de `direction_min_progress_bps`/`direction_required_confirmations`, ni los valores de `range_scalp_short_usdt_per_trade`/límites de exposición (se prefirió sizing dinámico inteligente sobre adivinar un número nuevo).
  - **Rollback**: revertir `staged_entry.py` y `run_trades.py` a los commits previos a s109; no requiere cambio de configuración salvo opcionalmente restaurar `capital_routing_cooldown_seconds`/`capital_routing_max_retries` si se hubieran sobreescrito por hot-config.

## 9. Activación en producción

- `config.py` y `config_helpers.py` tienen `staged_entry_enabled=True` por defecto.
- SPOT_BULL y RANGE_SCALP: `enabled=True`, base `required_positive_checks=2`, `max_cycles=3`; señales SR confirmadas usan `required_positive_checks=1`, `max_cycles>=4`.
- FUTURES_SHORT y PRESERVATION: `enabled=True` con predictor válido, EV neto positivo y hasta dos aperturas por ciclo; sus políticas siguen en calibración.
- Límites agresivos por régimen: `max_positions_spot_bull=12`, `max_positions_range_scalp=8`, `max_positions_preservation=4`, `max_positions_futures_short=8`, `max_total_exposure_pct=0.95`, `category_max_positions_enabled=False`.
- Kelly multipliers: SPOT_BULL=1.05, RANGE_SCALP=1.0, PRESERVATION=0.7, FUTURES_SHORT base=0.6 con boost hasta 1.0 según confianza.
- El predictor de reversión de RANGE_SCALP permanece en sombra hasta superar al baseline fuera de muestra en EV neto y calibración de probabilidad.
- Hot-config disponible vía `PATCH /config/kelly` con `staged_entry_enabled` o `staged_entry_regime_config`.

## 10. Monitoreo en producción

Revisar al inicio de cada ciclo de monitoreo (24-48h):

```sql
-- Distribución de estados
SELECT regime_mode, status, COUNT(*) FROM ready_candidates GROUP BY regime_mode, status;

-- Últimos 50 re-chequeos
SELECT ready_candidate_id, regime_mode, check_type, positive, decision, discard_reason, created_at
FROM ready_candidate_checks
WHERE created_at >= NOW() - INTERVAL '24 hours'
ORDER BY created_at DESC LIMIT 50;

-- Candidatos validados pendientes
SELECT id, symbol, regime_mode, positive_checks, total_checks, ready_at, last_checked_at
FROM ready_candidates
WHERE status='validated';
```

Alertas manuales a vigilar:
- `discarded` > 90% del total sin `discard_reason` claro → revisar `entry_profile_gate` o predictor.
- `validated` crece pero no pasa a `opened` → revisar QUBO, capacidad y `get_openable_candidates`.
- Comparar `policy.range_reversion_shadow` con el PnL realizado de RANGE_SCALP antes de cambiar `range_reversion_predictor_mode` a bloqueo.
- Errores `[StagedEntry] staged entry error` en logs → revisar excepción y rollback temporal.

## 11. Refactor s95 — Perfil por operación (no global)

- A partir de esta versión no existe un `current_regime_profile_label` activo a nivel sistema.
- El perfil A/B/C de cada operación se determina en la entrada por `entry_profile_gate` y se guarda en el metadata de la posición como `peak_quality_profile`.
- Kelly sizing, universe caps, screener y supervisores de salida (PRS, UG, SSO, SmartTP, Peak Capture) usan el perfil de la posición/señal, no un perfil global.
- El dashboard muestra el perfil como "Per operation" y el perfil real de cada posición se visualiza en su metadata / tooltip.
- Transiciones de régimen ya no incluyen cambio de perfil activo; solo cambian parámetros por modo de mercado.
- La variable de configuración `current_regime_profile_label` permanece con valor default `B` por compatibilidad de serialización, pero el runtime no la consulta para decisiones.
- MIMIC/backtest asigna el `forced_profile` como `peak_quality_profile` de cada operación; no simula un perfil global.
- Defaults canónicos de Kelly reflejan calibración agresiva: `kelly_max_pct_liquid=0.90`, `futures_kelly_max_pct_liquid=0.90`, `kelly_dynamic_base_pct=0.22`.
- Verificación: `611 passed, 2 skipped, 0 failed` (`pytest -n 4`); `-n 10` puede ser intermitente por competencia entre workers.

## 12. SR Pool runtime visibility (s96 — jul 25 2026)

- Flags de runtime del SR Pool se controlan únicamente por hot-config vía `PATCH /config/kelly`:
  - `sr_pool_enabled`, `sr_pool_trade_enabled`, `sr_pool_intra_cycle_enabled`
  - `sr_pool_full_scan_minutes`, `sr_pool_lookback_minutes`, `sr_pool_max_open_per_cycle`
- Activación por defecto en esta sesión:
  ```powershell
  PATCH /config/kelly {
    "sr_pool_enabled": true,
    "sr_pool_trade_enabled": true,
    "sr_pool_intra_cycle_enabled": true,
    "sr_pool_full_scan_minutes": 5,
    "sr_pool_lookback_minutes": 150,
    "sr_pool_max_open_per_cycle": 2
  }
  ```
- Endpoint de estado: `GET /sr_pool/status` (requiere `X-API-Key`).
  - Devuelve flags, settings, timestamp del último full scan, tamaño del pool y últimas 20 señales.
- Panel del dashboard: **🌊 SR Pool** en la pestaña **Operaciones**, entre las tarjetas de Operaciones y Selección de candidatos.
  - Refresca cada 30 s y con el botón **↻ Actualizar**.
- Los 6 flags SR-pool están excluidos de las verificaciones de config drift (`_check_config_drift` y `/config/verify`) porque se espera que difieran de los defaults canónicos.
- Los cambios hot-config se sincronizan inmediatamente al `settings` singleton de `app.py`; además, el endpoint `/sr_pool/status` lee directamente desde `components.settings` para reflejar overrides restaurados desde DB al reiniciar.
- Fix de race condition en `dashboard.js`: las llamadas que requieren autenticación esperan a que `/dashboard/config` entregue la API key antes de ejecutarse, evitando 401 en `/sr_pool/status`, `/range_scalp/dual_stats`, `/system/position-reconciliation`, etc.
- **Fix s96b — `NameError: name 'profile' is not defined`**: después del refactor s95, `universe_selector.select_analysis_universe` retornaba `"profile_label": profile` en lugar de `"profile_label": profile_label`; esto rompía el ciclo de análisis y de trading, impidiendo que el SR-pool generara señales y que se abrieran posiciones. Corregido a `profile_label` y agregado test de regresión en `tests/test_universe_selector_caps.py`.
- Verificación recomendada:
  ```powershell
  docker-compose exec app python -m pytest tests/test_config_governance.py tests/test_sr_pool_quality.py -q
  # 14 passed

  Invoke-RestMethod -Uri http://localhost:8000/sr_pool/status -Headers @{ 'X-API-Key' = 'DfJsbMHzq1gYZo6rvTFk7RXU9ht3xLauS2yVQleBjCdPW4KI' }
  # ok: true, flags activos
  ```

## 13. Staged entry fast-path execution (s98 — jul 26 2026)

- Cuando un candidato staged-entry pasa a `validated` + `direction_confirmed` en el intra-cycle check, el sistema ejecuta la compra **inmediatamente** desde el thread intra-cycle sin esperar al próximo ciclo de trading (~5 min).
- El fast-path bypassa los ~15 gates de mercado de `_buy_top_symbols` (BB position, RSI, funding rate, predictor PnL, entry-profile re-evaluation, BTC macro trend, WR screener, etc.) que son **redundantes** con la validación staged.
- **Gates de seguridad que se mantienen:**
  1. `staged_entry_fast_path_enabled` flag (hot-config)
  2. Circuit breaker (consecutive API failures)
  3. Global MAX_POSITIONS por régimen
  4. Per-symbol position limit
  5. Exposure cap (`max_total_exposure_pct`)
  6. USDT liquid balance suficiente para la orden
- Kelly sizing usa `position_size_usdt` de la política de validación staged; fallback a `_kelly_position_size()` con concentración.
- El trade cycle normal sigue como fallback: si el fast-path está deshabilitado o falla, el `trade_cycle_wake_event` despierta el ciclo normalmente.
- Race condition protegida con `_trade_cycle_lock` para serializar ejecuciones entre el thread intra-cycle y el trade cycle.
- Observabilidad: cada intento persiste `last_execution_gate` en `ready_candidates.data` con `passed`, `reason`, `timestamp`, `usdt_available`, `position_size_usdt` y `path=fast_path`.

Rollback instantáneo:
```
PATCH /config/kelly {"staged_entry_fast_path_enabled": false}
```

## 14. Directional Arbiter & Hard-Risk Monitor (s99 — jul 26 2026)

### Directional Arbiter (`range_scalp_short_first_enabled`)
- En RANGE_SCALP, **antes** de abrir spot long, el sistema evalúa si un futures short o futures long tiene mejor EV.
- `_range_scalp_direction_decision()` en `run_trades.py` invoca `directional_arbiter.evaluate_range_scalp()` comparando predicciones LONG (spot), SHORT (futures) y LONG (futures).
- Decisiones posibles: `SPOT_LONG`, `FUTURES_SHORT`, `FUTURES_LONG`, `ABSTAIN`.
- El arbiter se ejecuta tanto en fast-path como en `_buy_top_symbols`, con audit persistido en `trades_audit`.
- Config:
  - `range_scalp_short_first_enabled` (default `True`) — activa el arbiter
  - `range_scalp_short_preferred` (default `True`) — elige `FUTURES_SHORT` siempre que sea elegible (prob>0.40, EV>0) y el mercado sea ejecutable en futuros. Ignora comparación por score con LONG.
  - `range_scalp_short_priority_bonus` (default `0.05`) — bonus EV a SHORT cuando `short_preferred=False`
  - `range_scalp_long_superiority_delta` (default `0.10`) — delta mínimo para elegir LONG sobre SHORT cuando `short_preferred=False`
  - `range_scalp_direction_min_probability` (default `0.55`) — probabilidad mínima para LONG
  - `range_scalp_short_min_probability` (default `0.40`) — probabilidad mínima específica para FUTURES_SHORT
  - `range_scalp_short_sl_pct` (default `2.0`) / `range_scalp_short_tp_pct` (default `4.0`) — SL/TP de apertura para FUTURES_SHORT en RANGE_SCALP.

### Direction confirmation re-enforced para RANGE_SCALP (s99e)
- `config_helpers.py` fuerza `direction_confirmation_mode='enforced'` cuando `range_scalp_short_first_enabled=True`.
- `direction_required_confirmations=1` y `direction_min_progress_bps<=1.0` para RANGE_SCALP: una sola observación favorable confirma la dirección (bounces rápidos en SR).
- `direction_max_adverse_bps=150.0` — tolerancia amplia para señales en soporte.

### Predictor SL/TP cap para RANGE_SCALP (s99e)
- Bug: `pnl_predictor._calculate_expected_pnl()` usaba el rango completo de Bollinger/velas/SR para calcular EV, sin reflejar el SL/TP real de la posición. Para SHORT esto exigía `prob>0.5` en rangos simétricos y rechazaba shorts viables con `prob` entre 0.40 y 0.50.
- Fix en `pnl_predictor.py`:
  - `_calculate_expected_pnl()` acepta `settings` y `trade_type` y usa los porcentajes configurados de SL/TP como el payoff real de la posición, en lugar del rango estimado.
  - Nuevo helper `_get_sl_tp_pct()` lee `range_scalp_short_sl_pct`/`tp_pct` (futures short), `futures_long_sl_pct`/`tp_pct` (futures long) o `stop_loss_pct`/`take_profit_pct` (spot).
  - Para FUTURES_SHORT: ganancia máxima = caída del precio a `current*(1 - tp_pct/100)`, pérdida máxima = subida a `current*(1 + sl_pct/100)`.
  - `_calculate_risk_metrics()` ahora también recibe `settings`/`trade_type`/`trade_side` y reporta `max_loss_usdt`, `percentage_risk` y `risk_reward_ratio` a partir del SL/TP real.
  - Con `range_scalp_short_sl_pct=2.0` y `range_scalp_short_tp_pct=4.0`, el EV positivo solo requiere `prob > 2/(2+4) ≈ 33%`; combinado con `range_scalp_short_min_probability=0.40`, más shorts son elegibles sin degradar el modelo.
  - Si el SL/TP configurado es inválido, se conserva el fallback al rango estimado (Bollinger/velas/SR).

### Kelly sizing para FUTURES_SHORT (s99e)
- Bug: `_open_arbitrated_range_scalp_short()` abría con `range_scalp_short_usdt_per_trade=1000` fijo, sin Kelly sizing y sin intentar fondear el wallet de futuros. Además, varios `getattr(settings, 'futures_leverage', 2)` usaban default `2` en lugar del canónico `10`.
- Fix en `run_trades.py`: `_open_arbitrated_range_scalp_short()` ahora:
  1. Calcula tamaño vía `_kelly_position_size()` usando `short_prediction`, `strength`, concentración de shorts abiertos y parámetros futures (`futures_kelly_*`).
  2. Llama `fc.check_and_fund()` para transferir spot→futures si el wallet es insuficiente.
  3. Ajusta el monto post-transfer y abre con la cantidad real disponible.
  4. Todos los `getattr(settings, 'futures_leverage', ...)` en los paths SHORT/LONG de RANGE_SCALP ahora usan default `10`, alineado con `config.py`.

### Rollback
```
PATCH /config/kelly {"range_scalp_short_preferred": false}
```
- Thread independiente en `orchestrator.py` que monitorea posiciones spot cada 2s buscando hard stops breached.
- Módulo: `hard_risk_monitor.py` → `monitor_spot_hard_stops_once(comp)`.
- Config:
  - `hard_risk_monitor_enabled` (default `True`)
  - `hard_risk_monitor_interval_seconds` (default `2.0`)
- Estado visible en `GET /system/status` → `hard_risk_monitor_running`, `hard_risk_monitor_last_at`, `hard_risk_monitor_last_error`.

### Fast-path allocation fix
- Bug: `_policy_size` usaba `policy.expected_pnl` (~$3 profit) como tamaño de posición → `ALLOCATION_TOO_SMALL`.
- Fix: `_policy_size = float(policy.get("position_size_usdt") or 0)` — sin fallback a `expected_pnl`.
- Todas las entradas sin `position_size_usdt` explícito ahora usan Kelly sizing (~$200-$1200).

### Screener futures
- `screener_futures_min_vol` reducido a `$10M` (era `$100M`) para ampliar universo futures.
- `screener_max_futures_pairs=100` (nuevo campo).

### Direction target/stop derivation fix (s99b)
- Bug: `stage_ready_candidates` stored LONG policy `target_price`/`stop_price` in `direction` even when arbiter decided SHORT → `DIRECTION_TARGET_INVALID` (target above entry for SHORT).
- Fix: `_build_initial_direction()` in `staged_entry.py` derives target/stop from arbiter's prediction for the selected mode:
  - SHORT: target = `price_range.min`, stop = `price_range.max`
  - LONG: target = `price_range.max`, stop = `price_range.min`
  - Fallback to policy values when no arbiter decision exists.
- Verified: LDOUSDT opened via FAST_PATH_EXECUTED within 90s of staging.

### Fast-path arbiter fallback rules (s99c)
- Cuando el arbiter decide `FUTURES_LONG` y la ejecución falla:
  - Si `routing_intent == FUTURES_LONG_ANALYSIS`, **NO hay fallback a SPOT** (futures-only) y el candidato se bloquea con gate `FUTURES_LONG_NO_MARGIN`.
  - Si el intent no es futures-only, puede caer a SPOT LONG (misma dirección, compatibilidad legacy).
- Cuando el arbiter decide `FUTURES_SHORT` y falla, **NO hay fallback**: SPOT solo opera LONG. El candidato se bloquea con gate `FUTURES_SHORT_NO_MARGIN`.
- Root cause del caso ACEUSDT: screener dinámico detectaba futuros para ACEUSDT en Binance → arbiter decidía `FUTURES_LONG` → wallet futures con ~$10 → `amount < $10` → `None` → candidato atascado sin `continue` ni fallback.
- Fix: `FUTURES_SHORT` falla → `continue` (bloqueado). `FUTURES_LONG` falla → bloqueado sin fallback cuando el intent exige futures-only.

### Kelly sizing + just-in-time capital transfer para FUTURES (s99c)
- Bug: `_open_arbitrated_range_scalp_long` usaba tamaño fijo `futures_usdt_per_trade` ($150) en vez de Kelly sizing, y ambas funciones SHORT/LONG se limitaban al saldo disponible en futures sin intentar fondearlo.
- Consecuencia: posiciones FUTURES eran sub-dimensionadas. Si Kelly quería $1500 pero futures tenía $150, abría con $150 o fallaba.
- Fix — **Just-in-time capital transfer** (`_open_arbitrated_range_scalp_long` y `_open_arbitrated_range_scalp_short`):
  1. Calcular el monto Kelly deseado (o `usdt_override` del staged policy en fast-path).
  2. Si `amount > avail_net`: llamar a `fc.check_and_fund(amount)` que transfiere `shortfall × 1.1` de spot→futures.
  3. Refrescar balance post-transfer y usar el monto real disponible.
  4. Si la transferencia falla, usar lo que hay (graceful degradation).
- `check_and_fund` ahora transfiere `shortfall × 1.1` (antes era `required × 2.0`).
- Flujo ejemplo: Kelly=$1500, futures=$150 → shortfall=$1350 → transfiere $1485 desde spot → abre con $1500.
- **LONG**: Kelly completo con `_kelly_position_size()`, concentración, cap. Fast-path usa `policy.position_size_usdt`.
- **SHORT**: `usdt_override` o `range_scalp_short_usdt_per_trade` fijo + just-in-time transfer.

### CapBridge arbiter-aware funding (s99c)
- Bug: `smart_rebalance_capital` solo transfería spot→futures cuando `regime == "FUTURES_SHORT"`. En RANGE_SCALP con arbiter activo, el Case 2 intentaba drenar futures→spot y el Case 3 devolvía "idle" capital a spot, dejando el wallet de futures vacío (~$10).
- Consecuencia: cualquier decisión del arbiter `FUTURES_LONG` o `FUTURES_SHORT` fallaba por falta de margen. Solo se manifestaba cuando la exposición no era >90% (cuando hay capital líquido en spot pero nada en futures).
- Fix en `futures_client.py` (`smart_rebalance_capital`):
  - Nuevo parámetro `futures_arbiter_active` (bool).
  - **Case 1b**: si `futures_arbiter_active=True` y `regime in (RANGE_SCALP, SPOT_BULL)`, asegurar al menos `kelly_margin` disponible en futures (transfiere spot→futures si hay shortfall).
  - **Case 3 guard**: no drenar idle futures capital cuando el arbiter está activo.
- Fix en `run_trades.py` (`execute_once`): calcula `_cb_arbiter_active = range_scalp_short_first_enabled AND regime in (RANGE_SCALP, SPOT_BULL)` y lo pasa al CapBridge.
- Efecto: el CapBridge ahora transfiere ~$140 (kelly_margin - futures_avail) de spot→futures en el ciclo previo a la apertura, garantizando que el arbiter siempre tenga capital para ejecutar.

### Hard-risk breakeven activation (s99d)
- Bug: posición `BANKUSDT` llegó a +$10 de ganancia no realizada (~+0.68%) pero el breakeven nunca se activó. El precio se derrumbó y el `hard_risk_monitor` cerró por `hard_risk_stop` a -$20.22 (-1.37%).
- Root cause: el `hard_risk_monitor` revisa SL cada 2s pero no movía SL a breakeven. El BE solo se activaba en el trade cycle (`_check_sl_tp_positions`), que corre cada 3-5 min. Si el pico de ganancia ocurre entre ciclos, el SL se queda en el original y el hard stop cierra a pérdida.
- Fix en `hard_risk_monitor.py`:
  - Nueva función `_try_breakeven()` que corre antes del SL breach check.
  - Si `pnl_pct_net >= 0.15%` (umbral más agresivo entre regímenes), mueve el SL a `entry * (1 + 2*commission + slippage)`.
  - Usa `PositionManager.update_position_sl()`; respeta la regla de oro "SL nunca empeora".
  - Audita en `trades_audit` con evento `hard_risk_breakeven`.
- Configuración: `hard_risk_breakeven_enabled=True` en `config.py`, `config_helpers.py` y `_KELLY_EDITABLE_FIELDS` de `app.py` (hot-config).
- Tests: `tests/test_hard_risk_monitor.py` 6/6 passed; 526 tests focalizados sin fallos.
- Rollback: `PATCH /config/kelly {"hard_risk_breakeven_enabled": false}`.

### Rollback
```
PATCH /config/kelly {"range_scalp_short_first_enabled": false, "hard_risk_monitor_enabled": false}
PATCH /config/kelly {"hard_risk_breakeven_enabled": false}
```

---

## 10. WebSocket / Event Stream (s105i)

- `binance_event_stream.py` es la única fuente de suscripción a Binance User Data Streams (futuros y spot) y a mark-price streams.
- Todo error del websocket (`"Read loop has been closed"`, `"connection closed"`, `"error receiving message"`, `"websocket error"`) debe disparar reconexión automática. No se debe ignorar ni intentar reconexión manual desde fuera del módulo.
- El módulo mantiene polling/reconciliación existente (`FIX-S4`, `FIX-T4-L1/L2`) como red de seguridad; el event stream es un acelerador de latencia, no un reemplazo.
- Si `event_stream_reconnect_count` crece sin parar (>5 en pocos minutos), investigar conectividad/credenciales/testnet. Mientras tanto, desactivar el websocket con `BINANCE_EVENT_STREAM_ENABLED=False` (o flag equivalente en config) y reiniciar el contenedor.
- Para forzar una reconexión manual: `event_stream.force_reconnect()` o reiniciar el proceso (Python no recarga módulos en caliente).
- Health expone `event_stream_reconnect_count`, `event_stream_last_reconnect_at` y `event_stream_pending_reconnect`; usar `/health/status` para monitoreo.
