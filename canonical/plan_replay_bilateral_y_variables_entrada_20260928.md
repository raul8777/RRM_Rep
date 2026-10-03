# Plan de replay bilateral y variables de entrada

Fecha: 2026-09-28  
Alcance original: analisis offline de RANGE_SCALP; no modifica configuracion, no envia ordenes y no habilita Spot Margin.

**Aclaración operativa (2026-09-30):** el alcance anterior describe ese replay histórico y no el modo de Staging. Staging es el entorno activo sandbox más cercano a Producción: las hipótesis aprobadas se prueban con órdenes en Binance Spot Demo/Futures Testnet (`BINANCE_USE_TESTNET=true`, `DRY_RUN=false`). No se requiere una fase `shadow`/no-submit; Producción sigue intacta.

## Actualización s140 — implementación y límite de evidencia

La implementación bilateral quedó activa en Staging. Las candidatas `ready`/`validated` siguen siendo reutilizables por identidad y contexto S/R, pero cada reintento renueva precio, análisis, predictor, perfil, tesis, contrato, riesgo, costes y tamaño. `staged_entry.py` ya no pasa indicadores persistidos como `precomputed_signal` para la revalidación; guarda `fresh_signal_meta`/`fresh_prediction`, y FastPath/ciclo normal consumen ese snapshot antes de ejecutar. Si no hay evidencia o contrato vigente, se bloquea de forma auditable.

También se corrigió la precisión de `locked_position_size_usdt` y el FastPath no puede elevar el ticket por encima del margen contractual. La regresión de predictor fresco pasó junto con las suites focales; el despliegue fue solo en `crypto-agent-app_STG`, sin modificar producción.

Diagnóstico operativo previo al fix: cuatro ciclos completos en la última hora seleccionaron candidatos pero ejecutaron cero; 14 candidatos únicos acumularon `STALE_ENTRY_REWARD_RISK_BELOW_MIN`, uno `PRICE_REFRESH_FAILED`, tres `BILATERAL_NO_POSITIVE_EXECUTABLE_RETURN` y dos `SPOT_CONTRACT_STALE`. Estos conteos son bloqueos/reintentos, no oportunidades independientes. La exploración SL/TP de 93 eventos (`-$48.28` baseline simulado frente a `+$519.75` EV modelo) permanece in-sample y no autoriza promoción.

## Objetivo

Reevaluar cada iteracion de candidato como cuatro alternativas independientes: SPOT LONG, FUTURES LONG, SPOT MARGIN SHORT y FUTURES SHORT. Cada alternativa debe tener prediccion, tesis, geometria, costes, sizing y restricciones de venue propios. El sistema elige como maximo un contrato ejecutable por evento; no abre simultaneamente las cuatro alternativas.

El contraejemplo historico "SHORT perdido -> LONG espejo" se conserva como control sencillo, no como estimacion del rendimiento del router bilateral. En el corte anterior se observaron 600 filas candidato-ciclo, todas persistidas como `futures/SHORT`; solo 450 tenian cuatro horas completas al corte de 21:02 UTC. En el analisis contrafactual, 241 SHORT alcanzaron +0.5% antes del SL; entre 129 SHORT que tocaron SL antes de +0.5%, 94 LONG espejo alcanzaron +0.5% antes de su SL espejo. Ese 94 no es una probabilidad de LONG entrenada ni incluye el contrato bilateral real.

## Diagnostico de la ruta actual

El ciclo observado contiene 60 ciclos RANGE_SCALP y 600 filas `entry_selection_candidates`, todas con `trade_type=futures`, `expected_side=SHORT`, y ninguna guarda un objeto `directional_arbitration`.

En `run_trades.execute_once`, las predicciones genericas, `entry_profile_gate`, `entry_decision` y QUBO corren antes de `_prepare_range_scalp_staging_candidates`, que es donde se calcula el arbitraje bilateral. Por lo tanto, una alternativa LONG no participa en el perfil, la prediccion/decision inicial ni el QUBO cuando el candidato se crea como SHORT; puede quedar descartada antes de competir.

Dentro de `_range_scalp_direction_decision` se calculan predicciones para varios venues y lados, pero la prediccion estructural usa `if support_context ... elif resistance_context`: en soporte especializa el rebote LONG; en resistencia especializa el rechazo SHORT. El lado opuesto llega principalmente con la prediccion generica. El arbitro puede comparar los contratos que recibe, pero no repara el sesgo de etapas previas ni una tesis alternativa que nunca fue construida.

Estado observado del contenedor: `directional_policy_mode=active`, `range_scalp_ranking_mode=ev_net`, `range_scalp_min_stop_atr_multiple=1.5`, `range_scalp_direction_min_probability=0.55`, `range_scalp_rejection_breakout_penalty=0.5`, `FUTURES_LEVERAGE=2`, `RANGE_REVERSION_PREDICTOR_MODE=enforced`, `RANGE_SCALP_LONG_ENTRIES_ENABLED` no estaba definido en environment y su default es true; `SPOT_MARGIN_SHORT_ENABLED=false`. El override DB incluye `max_positions_range_scalp=5` (antes 6) y `kelly_dynamic_base_pct=0.22`; no incluye los umbrales bilaterales principales.

`range_scalp_long_min_probability` y `range_scalp_long_min_expected_pnl` aparecen en config y hot-config, pero el arbitro activo no los lee. Este usa `range_scalp_direction_min_probability` y exige EV neta positiva. `range_scalp_short_min_expected_pnl/probability` y varios flags `futures_long_*` pertenecen a rutas legacy y no deben darse por autoridad de la ruta bilateral.

## Geometria de los cuatro contratos

La zona S/R define que tesis construir, no el lado ganador. Los cuatro routes tienen su propio coste y elegibilidad. Para cada soporte o resistencia realmente activo, se construye la geometria indicada abajo. Si el evento no esta confirmado, esa tesis no se marca ejecutable.

| Venue y lado | En soporte | En resistencia | Riesgo y coste especificos |
|---|---|---|---|
| SPOT LONG | `LONG_SUPPORT_BOUNCE`: entry tras rebote confirmado; stop bajo soporte con buffer medido; target en la primera resistencia/extension. | `LONG_RESISTANCE_BREAKOUT`: entry despues de ruptura confirmada o retest; stop bajo resistencia recuperada; target en la siguiente resistencia/extension. | Sin leverage ni funding; comision Spot, slippage, saldo Spot disponible, filtros Spot y max-loss en USDT. |
| FUTURES LONG | Mismo rebote LONG, con contrato, precio, qty, margen y fees Futures propios. | Mismo breakout LONG, con confirmacion/retetest, stop y target de continuacion propios. | Leverage permitido por bracket y configuracion (Staging actual 2x); funding, fees, slippage, margen, max-loss, filtros Futures y ordenes protectoras. |
| SPOT MARGIN SHORT | `SHORT_SUPPORT_BREAKDOWN`: exigir breakdown de soporte; stop sobre soporte/reclaim; target en el siguiente soporte inferior. | `SHORT_RESISTANCE_REJECTION`: exigir rechazo explicito, sin breakout pressure; stop sobre resistencia; target en soporte inferior. | No es Spot cash: requiere Margin, activo borrowable, limite de prestamo, interes, collateral/margin level, fees y reglas de repay. Hoy `spot_margin_short_enabled=false`, por lo que esta alternativa es solo shadow/feasibility. |
| FUTURES SHORT | Mismo breakdown SHORT con confirmacion; stop sobre soporte roto; target en soporte inferior siguiente. | Mismo rechazo SHORT con evidencia explicita; stop sobre resistencia; target en soporte inferior. | Leverage bracket, funding firmado, fees Futures, slippage, margen aislado, max-loss y filtros de orden. |

Los niveles soporte/resistencia, pivotes, SR paths y confirmaciones se calculan sin look-ahead. La distancia ATR del stop, max-loss en USD y costes fijan el tamano; no se elige el tamano primero para despues deformar el riesgo. Se conserva `next_levels` para medir extensiones y no confundir un target base con toda la oportunidad.

## Protocolo experimental

1. Congelar la cinta de 600 filas con `cycle_id`, `symbol`, timestamp, precio, features, lado original, motivo de filtro, stage, configuracion/modelo y version de los costes. Cada fila de cada ciclo cuenta como iteracion; aparte se reporta evento/simbolo unico y repetidos.
2. Corregir solo la preparacion offline: formar las cuatro alternativas antes de filtros que dependan del lado y antes del QUBO. Calcular tesis S/R de forma independiente, no con `if/elif` excluyente cuando haya evidencia de ambos contextos.
3. Generar prediccion LONG y SHORT propia por tesis y venue. Perfil/quality debe ser neutral al lado o calculado por contrato; no heredar el perfil de una fila SHORT para veto LONG. Guardar features faltantes sin imputarlos como si fueran reales.
4. Construir contratos netos con entry/target/stop, qty, notional, margen, leverage, comision, slippage, funding/interes, max-loss, ATR y restricciones Binance. Comprobar geometria y rounding antes de declarar `executable`.
5. Medir por fila: ambos contratos viables, LONG-only, SHORT-only, ambos, ninguno; EV neta por alternativa; arbitro ganador; razon de cada rechazo. Despues QUBO/riesgo de portfolio escoge candidatos, no lados. La apertura efectiva selecciona como maximo un contrato por evento, pasa fresh-price re-arbitration y requiere fill confirmado.
6. Replay con velas de un minuto dentro de cuatro horas. Calcular primero TP-before-SL; calcular ademas MFE >= 0.5% antes del SL, excluyendo los casos ambiguos de la misma vela. Medir fees/slippage/funding al precio y qty ejecutables; no llamar ganador a un MFE que ocurre despues del stop.
7. Mantener el SHORT observado, el LONG espejo simple y los cuatro contracts nuevos como brazos separados. El espejo usa geometria reflejada y es control, no sustituye prediccion LONG.
8. Evitar leakage: separar entrenamiento/holdout por tiempo y agrupar tambien por simbolo/evento para que repeticiones de HBAR u otro activo no aparezcan en train y test. Usar cohortes pre/post-changepoint y baseline 3y/60000 segun AIC.
9. Aceptar solo si mejora PnL neto, expectancy y profit factor sin degradar drawdown/cola, con cobertura por venue/side/perfil A/B/C y muestra suficiente. No cambiar umbrales live por el resultado de estas mismas 600 observaciones.

### Etiquetas que deben reportarse

| Metrica | Definicion |
|---|---|
| TP-first | El target contractual se toca antes que el stop dentro del horizonte. |
| MFE +0.5 before SL | El precio cruza +0.5% a favor antes del stop, aunque cierre despues en perdida. |
| MFE +0.5 after SL | Movimiento posterior a un stop; no es ganador ejecutable. Se reporta aparte. |
| Ambiguo intraminuto | Target/stop o umbral/stop ocurren en la misma vela de un minuto; no se asigna ganador sin datos de mayor resolucion. |
| Ejecutable | Contrato pasa geometria, filtros de simbolo, notional/qty, riesgo, saldo/borrow y precio fresco. |

## Restricciones de Binance a incorporar

No hay un porcentaje TP/SL universal que Binance imponga. La estrategia puede proponer precios, pero el exchange aplica filtros y la cuenta aplica limites propios.

| Restriccion | Aplicacion experimental |
|---|---|
| `PRICE_FILTER`: min/max price y `tickSize` | Leer por simbolo en `exchangeInfo`; alinear target/stop en direccion conservadora y recalcular R/R despues de redondear. |
| `LOT_SIZE` y `MARKET_LOT_SIZE` | Validar por separado la qty de MARKET y de ordenes limit/condicionales; respetar `minQty`, `maxQty`, `stepSize`. |
| `MIN_NOTIONAL`/`NOTIONAL` | El minimo y si aplica a MARKET dependen de mercado/simbolo. El `exchangeInfo` Futures Demo consultado dio BTC $50, ETH $20 y HBAR/XRP $5; el testnet puede diferir de mainnet. |
| `PERCENT_PRICE` / filtros por lado | Precio limite/trigger dentro de la banda dinamica de precio de referencia. |
| Estado y limites de ordenes | Simbolo `TRADING`, max orders/algo orders, rate limits y estados de cuenta. |
| Futures leverage brackets | Max leverage/notional es por simbolo y cuenta; consultar `/fapi/v1/leverageBracket`, no asumir el max global. Staging usa leverage 2x. |
| Hedge mode / `positionSide` | Futures LONG/SHORT requiere side/positionSide consistente; respetar limites y modo de cuenta. |
| `triggerProtect` / working type | Aplicar cuando se utilicen triggers Mark Price/`priceProtect`; validar las reglas de orden condicional vigentes. |
| Spot Margin Short | Activo debe ser borrowable; validar max borrow, interes, collateral, margin level, isolated/cross y repay. No es un short Spot cash. |

La implementacion local asume minimo notional fijo de $10 en `opportunity_contract.py`/ejecutores, mientras `exchangeInfo` dinamico devuelve otros minimos. Futures usa `LOT_SIZE` para alinear qty; la orden MARKET tambien necesita cumplir `MARKET_LOT_SIZE`. Es un gate de conformidad a reparar antes de una calibracion de size, no un knob de estrategia.

Fuentes oficiales: [Spot symbol filters](https://github.com/binance/binance-spot-api-docs/blob/master/filters.md), [Futures Exchange Information](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Exchange-Information), [Futures New Order](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Order), [Notional and Leverage Brackets](https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Notional-and-Leverage-Brackets). Los filtros de exchangeInfo son dinamicos; guardar snapshot por simbolo y fecha del replay.

## Inventario de variables

El inventario enumera Settings referenciados por los modulos del pipeline de entrada y de su seleccion/ejecucion. `Hot config` indica presencia en la whitelist `_KELLY_EDITABLE_FIELDS`, no que sea seguro ajustarla ni que se use en la ruta activa. `Env` indica que el contenedor puede leer la variable de entorno. `No patch` requiere cambio de deploy/codigo o mecanismo aparte. Variables supersedidas o sin lecturas activas se marcan en notas.

| Grupo | Variables | Estado de calibracion |
|---|---|---|
| Universo, liquidez y latencia | `market_regime_enabled`, `cycle_range_scalp`, `analysis_symbols`, `futures_symbols`, `screener_enabled`, `screener_min_vol_usdt`, `screener_futures_min_vol`, `screener_max_pairs`, `screener_max_futures_pairs`, `screener_cache_minutes`, `screener_merge_with_static`, `screener_top_n_enabled`, `screener_top_n_count`, `screener_quality_min_vol_usdt`, `screener_quality_max_volatility`, `screener_quality_min_price_usdt`, `screener_quality_min_age_days`, `liquidity_filter_enabled`, `min_liquidity_vol_usdt`, `min_liquidity_vol_usdt_range_scalp`, `max_slippage_tp_ratio` | Calibrables con universo/segmento congelado; liquidez y price band deben ser prerequisitos de ejecucion, no optimizar sobre winners. |
| Snapshot/predictor | `predictor_use_real_candles`, `predictor_interval`, `predictor_horizon_minutes`, `trade_horizon_minutes`, `predictor_gate_mode`, `min_signal_strength`, `min_success_probability`, `min_signal_strength`, `min_success_probability`, `futures_min_prob`, `futures_vol_spike_min`, `vol_spike_min`, `futures_entry_max_rsi_4h`, `range_reversion_predictor_mode`, `range_scalp_quality_model_mode`, `range_scalp_quality_model_path`, `entry_profile_gate_enabled`, `entry_profile_gate_thresholds_path` | Medir por lado/tesis, calibracion y cohortes OOS; comprobar artefactos disponibles. No contar fallback como modelo. |
| Features/pesos ML | `learning_engine` weights para `rsi`, `macd`, `bb`, `ema`, `volume`, `vol_spike`, `atr_pct`, `trend_4h`, `weekly_range`, `sr_zone_position`, `sr_bounce`, `sr_breakout`, `sr_breakdown`, `support_strength`, `bias_correction`; features universales `rsi_1h`, `rsi_4h`, `bb_position`, `atr_pct`, `vol_spike`, `range_position`, `trend_1h`, `trend_4h`, distancias/posicion SR | Variables de modelo y datos, no umbrales libres; medir cobertura, missingness, drift y calibracion por side/venue. |
| Perfil/EV/probabilidad | `entry_decision_enabled`, `entry_decision_policy_version`, `entry_decision_regime_mode`, `entry_decision_min_expected_pnl`, `entry_decision_min_probability`, `entry_quality_gate_enabled`, `entry_quality_min_vol_spike`, `entry_quality_min_strength`, `entry_quality_max_failures`, `entry_quality_vol_spike_min_spot_bull`, `entry_quality_vol_spike_floor_spot_bull`, `entry_quality_rsi_min_spot_bull`, `entry_quality_rsi_max_spot_bull`, `entry_quality_breakout_min_vol_spot_bull`, `entry_quality_breakout_rsi_max_spot_bull`, `entry_quality_breakout_min_strength_spot_bull`, `entry_quality_max_failures_spot_bull/range_scalp/preservation/futures_short` | Parte del legacy entry-quality gate esta supersedida. `entry_decision_min_*` no es hot-patch editable. Separar per-contract score antes del arbiter. |
| Descubrimiento S/R | `sr_pool_enabled`, `sr_pool_trade_enabled`, `sr_pool_full_scan_minutes`, `sr_pool_lookback_minutes`, `sr_pool_max_open_per_cycle`, `sr_pool_intra_cycle_enabled`, `sr_pool_interval`, `sr_pool_kline_limit`; nested `sr_lookback_bars`, `sr_pivot_order`, `sr_near_band_atr_mult`, `sr_near_band_price_pct`, `sr_pool_zone_support_max`, `sr_pool_zone_resistance_min`, `sr_pool_min_strength` | `sr_pool_trade_enabled=false` por diseno; SR Pool solo descubre y alimenta ruta comun. Geometria sin look-ahead. |
| Tesis/direccion | `directional_policy_mode`, `range_scalp_bidirectional_enabled`, `range_scalp_long_entries_enabled`, `range_scalp_long_min_probability`, `range_scalp_long_min_expected_pnl`, `range_scalp_long_support_zone_max`, `range_scalp_long_superiority_delta`, `range_scalp_direction_mode`, `range_scalp_direction_min_probability`, `range_scalp_short_resistance_threshold`, `range_scalp_short_first_enabled`, `range_scalp_short_preferred`, `range_scalp_short_priority_bonus`, `directional_unconfirmed_countertrend_margin`, `range_scalp_structure_override_margin`, `range_scalp_short_min_expected_pnl`, `range_scalp_short_min_probability` | Shared `range_scalp_direction_min_probability` is read by arbiter. Long-specific min probability/EV and several preference fields are currently unused by that arbiter; do not infer behavior from config presence. |
| Contratos, geometria y perdida maxima | `stop_loss_pct`, `take_profit_pct`, `range_scalp_sl_pct`, `range_scalp_tp_pct`, `range_scalp_short_sl_pct`, `range_scalp_short_tp_pct`, `futures_sl_pct`, `futures_tp_pct`, `futures_long_sl_pct`, `futures_long_tp_pct`, `range_scalp_min_stop_atr_multiple`, `range_scalp_short_atr_sl_anchor_enabled`, `range_scalp_short_atr_sl_multiplier`, `spot_atr_sl_anchor_enabled`, `spot_atr_sl_multiplier`, `atr_sl_enabled`, `atr_sl_multiplier`, `atr_sl_min_pct`, `atr_sl_max_pct`, `max_loss_usd_per_trade`; hardcoded preflight `min_reward_risk_ratio=2.0` and `min_remaining_pct=0.15` | SL/TP calibrables, pero limitados por max-loss, ATR floor, geometry, min RR, remaining room, fees y filtros Binance. Noise floor activo en contenedor por env=1.5; no es hot-patch editable. |
| Costs/venue | `opportunity_contract_futures_entry_fee_rate`, `opportunity_contract_futures_exit_fee_rate`, `opportunity_contract_spot_entry_fee_rate`, `opportunity_contract_spot_exit_fee_rate`, `slippage_pct`, `funding_fee_guard_mode`, `funding_fee_guard_canary_fraction`, `funding_fee_guard_fail_closed`, `funding_guard_warning_fee_usdt`, `funding_guard_block_fee_usdt`, `funding_guard_warning_rate_pct`, `funding_guard_block_rate_pct`, `funding_rate_spot_filter_enabled`, `funding_rate_spot_max`, `funding_rate_cache_minutes`, `spot_margin_short_enabled`, `spot_margin_isolated_only`, `range_scalp_short_instrument` | Preferir fills reales/commission endpoints a defaults. Futures funding e intereses Margin dependen de hora/simbolo/cuenta; snapshot en cada contrato. Spot Margin esta apagado en runtime actual. |
| Tamano/capital/riesgo cartera | `max_per_trade_usdt`, `max_loss_usd_per_trade`, `kelly_enabled`, `kelly_base_usdt`, `kelly_max_usdt`, `kelly_max_pct_cap`, `kelly_max_pct_liquid`, `kelly_min_usdt`, `kelly_dynamic_base_pct`, `futures_kelly_base_usdt`, `futures_kelly_max_usdt`, `futures_kelly_max_pct_liquid`, `futures_kelly_cap_pct`, `futures_kelly_min_usdt`, `futures_usdt_per_trade`, `range_scalp_short_usdt_per_trade`, `futures_leverage`, `futures_enabled`, `futures_max_shorts`, `futures_max_longs`, `futures_max_largecap_shorts`, `futures_max_new_per_cycle`, `max_positions`, `max_positions_per_symbol`, `max_positions_spot_bull/range_scalp/preservation/futures_short`, `max_total_exposure_pct`, `max_total_exposure_spot_bull_fear`, `futures_symbols`, `futures_largecap_symbols` | Derivar size de riesgo fijo por contrato; no subir el ticket para forzar el 0.5%. Leverage bracket y wallets reservados imponen techos. |
| Concentracion/QUBO | `portfolio_concentration_enabled`, `portfolio_concentration_alpha`, `portfolio_concentration_min_mult`, `portfolio_concentration_max_mult`, `portfolio_concentration_pressure_start`, `portfolio_concentration_pressure_boost`, `portfolio_concentration_baseline_spot_bull/range_scalp/preservation/futures_short`, `category_max_positions_enabled`, `category_max_positions`, `qubo_enabled`, `qubo_lambda_corr`, `qubo_lambda_size`, `qubo_lambda_risk`, `qubo_solver`, `qubo_population_size`, `qubo_iterations`, `qubo_seed`, `qubo_min_candidates`, `qubo_include_open_positions` | QUBO selecciona cartera/eventos despues de arbitrar; evaluar candidatos LONG/SHORT simetricos y mantener comparacion sin QUBO. Muchos campos no son hot-config editables. |
| Staged lifecycle/cola | `staged_entry_enabled`, `staged_entry_fast_path_enabled`, `staged_entry_price_source`, `staged_entry_persist_checks`, `staged_hold_policy_enabled`, `staged_hold_soft_ttl_min`, `staged_hold_hard_ttl_min`, `staged_hold_require_block_reason`, `staged_hold_allowed_block_reason_tokens`; nested por regimen `enabled`, `checks_per_cycle`, `required_positive_checks`, `max_cycles`, `max_age_min`, `intra_mode`, `watcher_interval_seconds`, `price_source`, `require_inter_positive`, `require_predictor_valid`, `min_expected_pnl`, `max_open_per_cycle`, `cooldown_min`, `sr_side_guard_mode/canary_fraction/max_dist_pct/breakout_min_pressure/buffer_pct/max_away_pct`, `direction_volume_gate_enabled/canary_fraction/volume_anomaly_multiplier`, `direction_geometry_anchor_enabled/min_progress_stop_fraction/max_adverse_stop_fraction` | No cambiar a la vez con arbitraje/modelo. Conservar razones por candidato; no dejar slots ocupados por filas que fallan repetidamente. |
| Filtros macro/tecnicos de Shorts | `futures_no_short_btc_4h_threshold`, `futures_no_short_btc_3h_threshold`, `futures_no_short_btc_1h_threshold`, `futures_confirm_red_candle`, `futures_min_btc_7d_drop`, `futures_min_inject_strength`, `futures_entry_max_rsi_4h`, `staged_short_risk_guard_enabled/canary_fraction/max_resistance_dist_pct/min_risk_flags/volume_anomaly_multiplier`, `range_scalp_short_near_support_gate_enabled/max_dist_pct/max_rsi`, `range_scalp_sr_thesis_gate_enabled/max_dist_pct` | Separar riesgo macro real de sesgo side-specific. Registrar bloqueos para los cuatro contratos antes de calibrar thresholds. |
| Parametros derivados no obvios | `range_scalp_rejection_breakout_penalty`, `range_scalp_short_max_per_cycle`, `range_scalp_short_execution_enabled`, `spot_bull_pnl_boost_*`, `futures_category_thresholds`, `category_thresholds`, `token_categories`, `peak_quality_profile`, Fear & Greed Kelly thresholds/multipliers | Multiplicadores/categoria solo si el codigo de esa ruta los lee; varios son legacy, desconectados o solo para otra etapa/regimen. |

### Edicion vs. autoridad efectiva

- AST local identifico 206 Settings en el perimetro de modulos de entrada/seleccion/execution. La tabla anterior agrupa los knobs mas relevantes; la lista no sustituye call-graph por ruta.
- `PATCH /config/kelly` solo acepta `_KELLY_EDITABLE_FIELDS`; env vars pueden overridear defaults; `kelly_config_override` de DB mergea campos. El valor efectivo debe congelarse desde `/config/kelly`, `/config/verify`, `/config/drift` y snapshot de Settings por cada replay.
- Hot-patch puede permitir cambiar una variable inactiva. Confirmar siempre llamada de codigo. En el estado auditado, `range_scalp_long_min_probability/min_expected_pnl` no gobiernan el arbiter; `range_scalp_ranking_mode` y el noise floor no estan en la whitelist, aunque el contenedor los recibe por environment.
- El entorno auditado tenia `FUTURES_LEVERAGE=2`, `MAX_LOSS_USD_PER_TRADE=18`, `MAX_PER_TRADE_USDT=1500`, `FUTURES_USDT_PER_TRADE=150`, `KELLY_BASE_USDT=150`, `KELLY_MAX_USDT=2500`, `KELLY_MAX_PCT_LIQUID=0.90`, `KELLY_DYNAMIC_BASE_PCT=0.22`; el override DB tenia `max_positions_range_scalp=5` y `kelly_dynamic_base_pct=0.22`. Registrar estos valores como estado historico, no como nuevos defaults.

## Replay SL/TP inicial - solo exploratorio

Ejecucion 2026-09-28, corte fijo 21:02:40 UTC. Se unieron filas `qubo_selected` con el arbitraje mas cercano del mismo simbolo a <=5 segundos; 93 eventos maduros, 42 simbolos. Se evaluaron los contratos Spot LONG, Futures LONG y Futures SHORT presentes en la auditoria; Spot Margin no participo porque estaba deshabilitado en Staging. Resultado completo: `Dev/benchmarks/bilateral_sl_tp_grid_20260928.json`.

Se probaron 12 pares de multiplicadores sobre las distancias SL/TP registradas por contrato (SL 0.75/1.0/1.25/1.5; TP 0.5/0.75/1.0). Se mantuvieron fijos prediccion/probabilidad, venues/costes registrados, QUBO cohort, leverage y max-loss de $18. Se preservaron el piso ATR 1.5x y filtros publicos actuales de qty/notional/tick. Las probabilidades no se recalibraron con el resultado futuro.

| SL x contrato | TP x contrato | Seleccionadas | Abstenciones | TP-first | SL-first | MFE +0.5 antes SL | Neto simulado USDT | EV modelo USDT |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1.00 | 1.00 (baseline) | 52 | 41 | 9 | 14 | 45 | -48.28 | 519.75 |
| 1.00 | 0.50 | 42 | 51 | 21 | 9 | 36 | 31.56 | 115.43 |
| 1.25 | 0.50 | 34 | 59 | 18 | 5 | 29 | 59.28 | 55.10 |
| 1.50 | 0.50 | 19 | 74 | 12 | 0 | 18 | 90.69 | 27.33 |

El punto 1.50/0.50 es el mejor neto in-sample, pero con solo 19 selecciones; 1.25/0.50 da mas cobertura y menor neto. En el baseline el EV del modelo (+$519.75) difiere mucho del replay (-$48.28), por lo que el predictor/probabilidad no esta calibrado para estas trayectorias. Ningun punto se aprueba para activacion.

Limitaciones: 93 de 128 filas QUBO-selected tienen arbitraje enlazado y 4h maduras; las filas QUBO-excluded y los candidatos descartados antes de QUBO carecen de alternativas historicas completas. El QUBO se congelo al conjunto baseline y no se rerankeo por grid; repeticiones de simbolo se cuentan por iteracion y no se simulo competencia temporal de slots/posiciones. La cinta de velas es public Futures 1m; filtros actuales, no snapshot historico; costes se infieren de contratos, no de fills. Es un sensitivity replay, no una comparacion causal ni un resultado deployable.

## Gate de aprobacion

No promover ningun set de calibracion con solo este corte. Requerir replay determinista, side/venue-specific outcomes netos, limite de perdida, filtros exchange, holdout temporal y por simbolo, y control de multiplicidad de iteraciones repetidas. Rollback por restauracion de snapshot de config/version/hash; runtime permanece sin cambios hasta aprobacion y canary en Staging con health, pausa, capital y fill trazables.

<!-- DEVIN_REVIEW_START: 2026-09-28 -->
## [DEVIN REVIEW 2026-09-28] Clasificación de variables para la calibración de Entry

Revisión del inventario completo con la pregunta operativa: *¿esta variable pertenece al espacio de calibración del Entry, debe congelarse como prerequisito, o está fuera del alcance?* Tag de mis comentarios: **[D]**.

### A. INCLUIR en el espacio de calibración de Entry — son knobs de decisión direccional/geométrica

| Variable | Por qué entra | [D] Nota |
|---|---|---|
| `range_scalp_min_stop_atr_multiple` | Es el gate probado — calibrar con grid {1.0, 1.5, 2.0}; mis datos dicen que incluso 1.5× puede ser insuficiente | **prioridad 1** |
| `range_scalp_ranking_mode` | `ror` vs `ev_net` — es el defecto de incentivo medido | **prioridad 1** |
| `range_scalp_rejection_breakout_penalty` | Grid {0.3, 0.5, 0.7}; depende de que `breakout_pressure` se pueble en el momento de entrada — verificar primero | prioridad 2 |
| `range_scalp_direction_min_probability` | Umbral único que SÍ lee el árbitro activo | prioridad 2 |
| `range_scalp_sl_pct` / `tp_pct` / `range_scalp_short_sl_pct`/`tp_pct` | Geometría contractual — mis datos muestran que el stop estructural domina sobre `sl_pct` (min() gana); calibrar juntos | prioridad 2 |
| `futures_sl_pct` / `futures_tp_pct` | Lo mismo para la ruta directa FUTURES_SHORT | prioridad 2 |
| `range_scalp_short_atr_sl_anchor_*` / `spot_atr_sl_*` / `atr_sl_*` | Anclaje estructural del stop — interactúa con noise floor | prioridad 2 |
| `min_signal_strength`, `min_success_probability`, `futures_min_prob` | Filtros upstream que deciden qué llega al árbitro — medir sensibilidad conjunta | prioridad 3 |
| `entry_time_stop_hours` | Horizonte de decisión de la entrada | prioridad 3 |
| `max_positions_range_scalp`, `futures_max_shorts`, `max_open_per_cycle` | Cobertura — calibrar SOLO después de que la calidad direccional esté validada | prioridad 4 |
| `futures_no_short_btc_*h_threshold`, `futures_confirm_red_candle`, `staged_short_risk_guard_*` | Filtros macro side-biased — medir bloqueos primero; calibrar después solo si siguen siendo relevantes | prioridad 4 |

### B. CONGELAR como prerequisito — no son knobs de calibración, son el entorno

| Grupo | [D] Por qué congelar |
|---|---|
| Screener/universo/liquidez (`screener_*`, `liquidity_*`, `analysis_symbols`) | Cambiar el universo contamina la comparación A/B — cada candidato diferente es una muestra distinta |
| Costes (`opportunity_contract_*_fee_rate`, `slippage_pct`, `funding_*`) | Son realidad del exchange, no decisión. Snapshot por fecha, no calibración |
| Filtros Binance (min notional, tick, lot size, brackets) | Conformidad — se verifica una vez, se congela |
| `futures_leverage` | Fijado en 2× por política; no es knob de Entry |
| `max_loss_usd_per_trade`, `max_per_trade_usdt` | Límites de riesgo duros — no se calibran contra PnL in-sample |
| QUBO (`qubo_*`, `portfolio_concentration_*`) | Selección de cartera posterior al arbitraje — distinta etapa, distinto objetivo |
| `staged_entry_*` (TTL, checks, hold_policy) | Lifecycle de la cola — separado de la decisión direccional |

### C. EXCLUIR del alcance Entry — fuera de esta fase

| Variable(s) | [D] Por qué fuera |
|---|---|
| Lifecycle: SSO/PRS/UG/PredExit/trailing/breakeven (`position_*`, `exit_*`, `close_*`) | El usuario delimitó: "en estos momentos nos estamos concentrando en los entrys" |
| `spot_margin_short_enabled`, `spot_margin_isolated_only` | `false` en runtime — no hay venue Margin que calibrar; shadow/feasibility solamente |
| CapBridge / BNB transfer / `capital_transfer_*` | Allocator operativo — autorizado JIT post-contrato, no es decisión de dirección |
| `predictor_*` de datos/candles (`predictor_use_real_candles`, `predictor_interval`) | Infraestructura del predictor — cambiarla invalida toda comparación histórica |
| `learning_engine` weights/features | Modelo de datos — su recalibración es otra fase y otro contrato |
| Variables supersedidas/sin lecturas activas (`range_scalp_long_min_probability`, `range_scalp_long_min_expected_pnl`, `futures_long_*`, varios `range_scalp_short_*` legacy) | La doc misma lo marca: "no deben darse por autoridad de la ruta bilateral" — presencia en config ≠ comportamiento. NO incluir en grid porque el sweep no las leería |

### D. Advertencias operativas que el listado no cubre

1. **`pause_new_entries` está ACTIVO** (`RUNNING_ENTRIES_PAUSED`, 180min TTL, motivo `allocator_deploy_and_balance_error_investigation`): DNS `demo-api.binance.com` + rechazos de balance en testnet. **Ninguna calibración de Entry live puede observarse hasta resolver esto.** El sweep MIMIC sigue corriendo (datos históricos — no afectado).

2. **Marca de generación del sweep queda desactualizada:** `entry_flags.generation` distingue `baseline` vs `v2_entry_fix`, pero Copilot desplegó s139/s140 posteriormente (allocator JIT, evidencia fresca en revalidación). Los jobs del resume post-s140 son realmente `v3` — si el análisis necesita separarlos, hay que usar timestamp de la fila o actualizar la lógica de `generation` para incluir el hash de código.

3. **`predict_range_reversion` sigue siendo mean-reversion only.** s139 dice que breakout/breakdown usan "predicción direccional de continuación" — verificar que esa rama existe y se invoca con `predict_pnl` (momentum), no que `predict_range_reversion(LONG)` se llama contra una resistencia (devuelve target inválido hacia la media). La tabla del doc lo describe conceptualmente; la implementación debe validarse por código, no por config.

4. **`sr_pool_trade_enabled=false`** según el inventario — el doc describe SR Pool como generador de candidatos. Si en la ruta activa los candidatos vienen del pair_screener bilateral, el SR Pool no aporta flujo nuevo; si sí aporta, el gate de `stop_noise_gate` que cableé en su ruta SHORT sigue siendo necesario pero es sobre la ruta secundaria.

5. **El grid inicial recomendado** (para la dirigida A/B, solo variables de prioridad 1+2):

```text
min_stop_atr_multiple:  {1.0, 1.5, 2.0}
ranking_mode:           {ror, ev_net}
breakout_penalty:       {1.0, 0.5, 0.3}
range_scalp_sl_pct:     {0.8, 1.2, 1.6}
entry_time_stop_hours:  {4, 8, 24}
→ 3×2×3×3×3 = 162 celdas × ventanas; con 8 workers y ~14min/job, ~2 días
```

6. **Reutilización del trabajo previo:** las medidas ya hechas (noise floor fix, ev_net ranking, breakout penalty, SL/TP intrabar + time stop en el runner, marca `entry_flags`, cobertura de rutas directas) **siguen siendo válidas y aplicadas** — forman parte del contrato s139. Lo que cambia es que s139/s140 trajeron otras piezas (allocator, fresh evidence) que no estaban en mi scope y que interactúan con la reutilización de candidatos entre ciclos. El experimento dirigido debe decidir si compara `baseline → v2+mios` o `baseline → s139+v2` — son tesis diferentes y la marca de generación lo soporta.

<!-- DEVIN_REVIEW_END: 2026-09-28 -->