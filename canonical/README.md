# Documentación Canónica — `canonical/`

Este directorio contiene los **documentos canónicos** del sistema crypto-agent. Cada archivo es la referencia única de autoridad para su dominio. Ningún otro documento debe contradecirlos; si hay conflicto, el canónico prevalece.

## Inventario

| Archivo | Tipo | Dominio | Estado |
|---------|------|---------|--------|
| `calibracion_entradas-a20ab8-v3.md` | planning | Entradas, capital por modo, datos externos, maduración | active (s115) |
| `calibrar-ciclo-vida-52fe5d.md` | canonical-memory | Lifecycle de posiciones (apertura → monitoreo → cierre); incluye event stream, reconciliación Binance, drawdown monitor y profit erosion detection | active (s120b) |
| `calibrar_datos.md` | canonical-memory | Datos: inventario, calidad, linaje, contratos analíticos | active (s120b) |
| `hard_risk_breakeven_s99d.md` | canonical-memory | Breakeven en hard-risk monitor — protección de ganancias cada 2s | active |

---

## 1. `calibracion_entradas-a20ab8-v3.md`

**Título:** Calibración de Entradas — Producción Real, Capital por Modo, Datos de Terceros y Maduración del Sistema

**Propósito:** Registro maestro de la política de entradas. Define cómo se asigna capital por régimen activo, qué fuentes de datos externas se integran, y el cronograma de maduración operativa.

**Estructura (14 secciones):**

- **Apertura canónica (arquitectura):** el documento inicia con diagrama Mermaid del pipeline de calibración de entrada y tabla de responsabilidades por módulo (`market_regime`, `run_trades`, `autonomous_signals`, `entry_profile_gate`, `staged_entry`, `portfolio_selector`, `pnl_service`, `analysis_cycle`, `external_data_client`, `technical_graph_analyst`, `app`).

- **Estado del sistema (s94–s101):** Historial cronológico de cambios aplicados (Directional Arbiter, Hard-Risk Monitor, SR features, modelo universal, routing de capital con retry acotado y observabilidad completa de espera/ejecución). Cada estado incluye cambios, validación y rollback.
- **§1 Decisiones del usuario:** Capital modo-único, PRESERVATION más ofensivo, datos de terceros selectivos, maduración absoluta.
- **§2 Asignación de capital por modo:** SPOT_BULL (80-95%), RANGE_SCALP (70-90%), PRESERVATION (30-60%), FUTURES_SHORT (60-90%). Reglas duras: no cerrar posiciones por cambio de modo, reserva de comisiones intacta, leverage máx 2x.
- **§3 Datos de terceros:** Evaluación de 8 fuentes (CoinGlass ✅, Usenami ✅ free tier, MMT ✅ fase 2, resto descartado). Fase 1: funding rate, OI, liquidation clusters, L/S ratio, ETF flows.
- **§4 Cambios propuestos C1–C10:** Armonía de parámetros, propagación de perfil, recalibración thresholds, relajación B/C, FUTURES_SHORT en producción, PRESERVATION ofensivo, paralelización, datos externos, analista gráfico.
- **§5 Plan de trabajo:** 4 semanas (fundamentos → futuros → preservation/datos → madurez).
- **§6–9 Monitoreo, roles, riesgos, métricas:** Dashboard, alarmas, mitigaciones, objetivos 7d/30d.
- **§11 Fusión db0300:** Hallazgos de producción (19 jul): peak_quality_profile no propagado, umbrales degenerados, predictor optimista (corr -0.18), staged entry sin persistencia.
- **§12 Fundamento de datos:** Referencia a `calibrar_datos.md` como autoridad de linaje. Modelo RANGE_SCALP en shadow (OOF corr -0.0186).
- **§13 Discovery agnóstico:** `opportunity_shadow` en pair_screener para observación sin ejecución.
- **§14 Estado C7-C10 completado:** PRESERVATION ofensivo, paralelización del análisis, datos externos fase 1 y analista técnico gráfico.

**Rollback:** `PATCH /config/kelly {"range_scalp_short_first_enabled": false, "hard_risk_monitor_enabled": false}`

**Actualización reciente (s115, ago 3 2026, revalidación de tesis S/R + regla de visibilidad shadow):** encontrada la causa raíz de `币安人生USDT`/`SYNUSDT` y 4 casos más en 48h — `_staged_range_scalp_decision` ejecutaba SHORT/LONG sin volver a revisar si el precio estaba realmente cerca del S/R de su propia tesis. Simulado contra 85 operaciones reales de 48h antes de implementar: con umbral 5%, el PnL neto habría sido +$31.28 en vez de -$180.92 (sacrificando 11 ganancias reales de +$122.18 para evitar 16 pérdidas de -$334.38). Implementado activo (no shadow) y simétrico para SHORT/LONG. Nueva regla dura: todo mecanismo shadow debe tener visibilidad en `/metrics/shadow-readiness` — aplicada retroactivamente a los mecanismos shadow de s113. 571/574 tests pasan. Ver "Estado del sistema — ago 3 2026 — s115".

**Actualización reciente (s113, ago 3 2026, plan completo de ciclo de entradas):** implementado el plan completo derivado del análisis de rompimientos favorables: (1) lock de concurrencia nuevo en `position_manager.py` (SPOT) — mismo patrón que `_close_lock` de futuros (s105h), nunca aplicado a SPOT antes; (2) escalonamiento sin descarte terminal (`allow_scale_in_ready_id`); (3) anclaje ATR al SL real en SPOT y FUTUROS (nunca aprieta, solo ensancha, el guardarrail USD sigue como techo); (4) corroboración de volumen tipo VSA/Wyckoff en la confirmación de dirección (modo shadow); (5) `intra_mode="watcher"` extendido a `FUTURES_SHORT`/`PRESERVATION` (mismo fix de s106 que ya se validó en `RANGE_SCALP`); (6) entrada inmediata escalonada ("pierna 1" en el toque de S/R, apagada por defecto). 563/566 tests pasan (3 fallas preexistentes no relacionadas). Ver "Estado del sistema — ago 3 2026 — s113" en el documento completo.

**Actualización reciente (s111, ago 2 2026, cola de riesgo monopolizada + confirmación con umbrales fijos):** continuación de s110 — con datos de 48h, se midió que **93% de todas las selecciones de cola de ejecución (2,171 de 2,335) las consumieron candidatos que nunca abrieron una posición** (60 candidatos, promedio 36.2 selecciones cada uno, vs. 2.9 promedio de los 37 que sí abrieron). Causa: la prioridad de `get_openable_candidates` no considera si el candidato puede ejecutarse realmente — `HEIUSDT`/`RIFUSDT` (los mismos de s110) monopolizaron los 2 cupos por ciclo durante 46 minutos seguidos, bloqueando 6 candidatos viables (confirmado con timestamps: al descartarlos, el siguiente abrió 8 segundos después). Además, se midió que la FSM de confirmación de dirección degrada casi tanto como confirma (1,772 `DIRECTION_CONFIRMED` vs. 1,044 degradaciones, 59%) porque sus umbrales (`direction_min_progress_bps=1.0`, `direction_max_adverse_bps=150`) son porcentajes fijos sin relación con la distancia real al stop de cada trade. Fixes: la prioridad ahora deprioriza candidatos cuyo último intento real de ejecución falló (`last_execution_gate.passed=False`), sin importar su score de EV; los umbrales de confirmación de `RANGE_SCALP` ahora se anclan a un % de la distancia al stop del propio trade (opt-in, otros regímenes sin cambios); las degradaciones de confirmación ahora decrementan en vez de resetear a 0. 177/180 tests pasan (3 fallos preexistentes no relacionados). Ver "Estado del sistema — ago 2 2026 — s111" en el documento completo.

**Actualización reciente (s110, ago 2 2026, ZAMAUSDT/RIFUSDT/HEIUSDT — gates finales sin condicionar a demora real):** 5 candidatos `validated` (`ZAMAUSDT`, `RIFUSDT`, `EULUSDT`, `HEIUSDT`, `ONDOUSDT`) se quedaron sin abrir mientras el precio se movía favorablemente a la tesis (hasta -3.6%), acumulando cientos/miles de rechequeos (`ZAMAUSDT`: 986 checks/3h24min). Causa: (1) el gate `_revalidate_entry_before_submit` (agregado en s109) se invocaba en **todo** intento de apertura, no solo cuando hubo demora real por capital/slots — al recomputar recompensa:riesgo contra el SR fijo con precio cada vez más tardío, un candidato que se movía correctamente hacia el objetivo terminaba auto-bloqueándose por haber acertado; (2) `futures_executable` se re-derivaba cada ciclo desde el top-N rotativo del screener (`screened_pairs_cache`), sin relación con si el candidato ya validado podía ejecutarse — `HEIUSDT` (símbolo `TRADING`/`PERPETUAL` todo el tiempo) cayó del top-67 y quedó bloqueado indefinidamente. Fixes: nuevo `_candidate_delayed_by_capital_or_slots()` condiciona el gate de re-validación solo a demora real (bloqueo de capital vía `capital_routing.retry_count`, o de slots vía `stage_reason=RISK_BLOCKED/ROUTING_BLOCKED`) — sin demora, abre de inmediato; `futures_executable` ahora confía en el valor ya cacheado en `signal_meta.directional_arbitration` al momento de la señal, con fallback a la verificación en vivo solo si no existe ese campo. 161/161 tests de la suite filtrada pasan. Se descartaron manualmente 4 candidatos ya demasiado rezagados para abrir con sentido. Ver "Estado del sistema — ago 2 2026 — s110" en el documento completo.

**Actualización reciente (s109, ago 1 2026, auditoría AVAXUSDT — precio/dirección/capital):** se auditó un trade real (`AVAXUSDT` SHORT) con brecha entre precio de detección (6.3460), aceptación (~6.312) y apertura real (6.3100): de todo el movimiento favorable ocurrido desde la señal, la posición solo capturó ~30%. Se encontraron y corrigieron 6 causas: (1) la validación `ready→validated` usaba un precio de hasta 8 min de antigüedad; (2) la FSM de confirmación de dirección no corría en estado `ready`, solo tras `validated`; (3) la confirmación nunca comparaba contra el precio de la señal, solo contra el tick anterior, descartando movimientos favorables ya ocurridos; (4) la reserva de capital era todo-o-nada (bloqueaba en vez de reducir el tamaño); (5) el cooldown de reintento tras bloqueo de capital era de 5 min fijos frente a un watcher de 10s; (6) el ciclo principal y el fast-path no compartían el estado de capital, con riesgo de doble-compromiso. Fixes: precio fresco siempre, FSM de dirección activa desde `ready` (descarta antes ante rotura adversa), confirmación temprana contra precio de señal, nuevo gate de re-validación final (R:R≥2.0 con SR ya calculado), sizing dinámico que escala al capital disponible en vez de bloquear, cooldown reducido a 30s, y reserva de capital compartida entre ambos caminos de ejecución. 518/521 tests pasan (3 fallos preexistentes no relacionados). Ver "Estado del sistema — ago 1 2026 — s109" en el documento completo.

**Actualización reciente (s108, ago 1 2026, auditoría DIAUSDT SHORT):** se auditó un trade real `FUTURES_SHORT` (`DIAUSDT`, `SHORT_GEOMETRY_PRIORITY`, -37.19 USDT) que resultó abierto lejos de resistencia (zona 0.27, lado del soporte), y un análisis cuantitativo de 313 aperturas `RANGE_SCALP` mostró que la relación "resistencia→short, soporte→long" no se sostenía tal como estaba implementada. Se encontraron y corrigieron **3 bugs independientes** (no descalibración): (1) `pnl_predictor.py::predict_range_reversion` siempre evaluaba la rama LONG sin importar el lado real del candidato; (2) no existía ningún detector de "rechazo en resistencia" simétrico a `sr_bounce`; (3) el umbral que promovía `support_breakdown_sell` a señal SELL estaba invertido (`>=0.55` en vez de `<=0.45`), por lo que nunca se activaba (0/157 casos medidos). Fixes aplicados de forma aditiva (ninguna vía de apertura existente se eliminó) en `pnl_predictor.py`, `support_resistance.py`, `autonomous_signals.py`, `directional_arbiter.py`, `staged_entry.py`, `futures_client.py` y `run_trades.py`, con persistencia nueva de `directional_arbitration_reason`/`sr_zone_position_at_entry`/`range_reversion_valid_at_entry` para medición futura sin joins frágiles. 497/500 tests pasan (3 fallos preexistentes no relacionados). La calibración del umbral de confirmación direccional y cualquier rediseño más amplio del arbitraje quedan pendientes a propósito, a la espera de datos reales con los fixes en producción. Ver sección "Estado del sistema — ago 1 2026 — s108" en el documento completo.

**Actualización reciente (s101):** cierre operativo del plan RANGE_SCALP en el alcance actual: retry acotado de `CAPITAL_ROUTING_BLOCKED` (cooldown + max retries + descarte terminal), y acciones de gráfico completas para filas en espera (`Ver gráfico`, `TV`, `BIN`) en dashboard.

**Actualización reciente (s105–s105c, jul 30-31, incidente "cero aperturas ~44h"):** diagnóstico exhaustivo con datos reales de producción (no sólo tests) que encontró **tres bugs independientes** que se enmascaraban entre sí en el funnel `staged_entry → fast-path/execute`:
- **s105:** candidatas `validated` esperando confirmación direccional morían por timeout de sólo 15-20 min (`MAX_AGE_EXPIRED`/`MAX_CYCLES_REACHED`) en vez de por invalidación estructural real. Fix: `max_age_min=480` (8h) en las 4 regímenes, `max_cycles=100` (desacoplado), nuevo terminal explícito `DIRECTION_SR_PROXIMITY_LOST` (pérdida real de cercanía al soporte/resistencia de referencia) como único camino de descarte anticipado junto con `DIRECTION_STOP_BREACHED`.
- **s105b:** segunda vía de muerte prematura independiente del TTL: `CAPITAL_ROUTING_BLOCKED_MAX_RETRIES` (3 reintentos × 5 min ≈ 15-20 min) alineada a 8h (96 reintentos). Guarda de posición duplicada (antes retornaba `None` en silencio) ahora descarta explícito con `DUPLICATE_POSITION_SKIP`. Se aplicó `migrations/022_ready_candidate_checks_writer.sql` (pendiente desde antes, rompía el audit trail de descartes por expiración) y se documentó la lección operativa: **un fix en el repo no está vigente hasta reiniciar el proceso** (Python no recarga módulos).
- **s105c (causa raíz real y más grave):** el gate de exposición (`_execute_validated_fast_path` y `_buy_top_symbols`) contaba el capital idle de la wallet de futuros (pre-fondeado por CapBridge, 0 posiciones abiertas) como "invertido", calculando ~92% de exposición cuando la real era ~5.8% — bloqueaba el 100% de las aperturas en el primer gate, sin dejar rastro por candidata. Fix: restar `futures_liquid` de `_invested` en ambos paths. **Verificado con datos reales:** primeras posiciones abiertas desde el 29 de julio (`ZAMAUSDT`, `LDOUSDT`) minutos después del restart post-fix.

**Actualización reciente (s106, jul 31, análisis de demora deteccion→apertura):** con las aperturas ya funcionando (post s105c), se cuantificó que en RANGE_SCALP el 96-98% del tiempo entre la detección SR y la apertura real es oscilación de la FSM de confirmación direccional (no espera genuina del mercado, ni ejecución lenta): mediana 13.6 min, máximo 4.8h, costo medido de -$460.61 USDT/24h. Dos causas raíz: (1) `intra_mode='predictor'` (150s/tick, recomputo de predictor) nunca fue `'watcher'` (10s/tick) en producción pese a que `config_helpers.py` ya tenía ese valor — `config.py::Settings` es la fuente viva y estaba desalineada; (2) la FSM (`_direction_transition`) resetea `directional_confirmations` ante cualquier tick no-favorable, sin distinguir ruido (27.9% de los ticks) de reversión real. Fix: `intra_mode`→`'watcher'` en `config.py` + nueva rama `DIRECTION_NOISE_HOLD` que preserva confirmaciones ante ruido, sin tocar ningún umbral de sensibilidad. Se agregó además trazabilidad por intento (`ready_candidate_checks`, `writer='fast_path_gate'`) para cada gate de ejecución, no solo el último. **Desplegado y verificado en config** (restart `2026-07-31T18:38:05Z`, `intra_mode='watcher'` confirmado en vivo); **medición de impacto real aún pendiente** porque `pause_new_entries` está activo desde antes del restart (`operator_manual_stop`, sin relación con este fix) — 0 candidatas nuevas en `RANGE_SCALP` hasta que se reanude. Ver detalle completo en el documento.

---

## 2. `calibrar-ciclo-vida-52fe5d.md`

**Título:** Calibración del Ciclo de Vida de una Operación

**Propósito:** Documento vivo del pipeline de salida (apertura → monitoreo → cierre). Diagnostica por qué las salidas generan pérdidas grandes y ganancias mínimas, y propone rediseños canónicos.

**Estructura (15+ secciones):**

- **Actualización s120/s120b (ago 3 2026):** Intelligent Drawdown Monitor + Profit Erosion Detection. Monitorea TODAS las posiciones a 1s: (Case A) posiciones en pérdida > $10 → análisis de tendencia → CUT/TIGHTEN_SL/HOLD; (Case B) posiciones en profit pero erosionándose → PROTECT_PROFIT (lock 30% peak) / PROTECT_PROFIT_GENTLE (15%) / HOLD. Anti-estrangulación PRS: peak_protection momentum-aware (relaja a 50% giveback cuando momentum >= 70). Hot-config de trailing ampliado (1.4% distance, 2.5% activation). Erosion NUNCA cierra, solo tightens SL. Zero API extra.
- **Actualizaciones s93–s118:** Fixes RANGE_SCALP, integración SR en salida, modelo universal, perfil por operación, event stream Binance, circuit-breaker de portafolio inteligente.
- **Pipeline de salida (precedencia canónica):**
  1. Hard SL/TP (órdenes algo en Binance)
  2. Dust/ghost cleanup
  3. Breakeven
  4. Trailing stop
  5. **Position Telemetry 1s: Profit Guard + Drawdown Monitor + Erosion Detection (s120)**
  6. Smart Scale-Out (SSO-1..7)
  7. Profit Realization Supervisor (PRS) — now momentum-aware peak protection
  8. Unified Governance Supervisor (UG)
  9. Peak Capture / PredExit
- **Módulos:** `futures_client.py`, `run_trades.py`, `smart_scale_out.py`, `profit_realization_supervisor.py`, `peak_capture_engine.py`, `unified_governance_supervisor.py`, `position_manager.py`, `state_persistence.py`, `position_telemetry_watcher.py`.
- **Análisis de daño real (§10):** 48h: -137.11 USDT (38.24% WR). UG loss explica -199.91 USDT (146% del daño). SELL: +670.08 (74% WR) vs BUY: -677.23 (48% WR) en 30d. Overtrading hasta 68 aperturas/día. Solo 3 BE activados, 0 trailing en 48h.
- **Diagnóstico por roles (§11):** Físico (mínimo local de parámetros), Estadístico (R:R invertido, sesgo de supervivencia), Técnico (señales lentas, sin estructura), Ingeniero (código 9000+ líneas, perfil no propagado).
- **Rediseño (§12):** Cap de pérdida por operación ($20 o 0.3% portfolio), UG loss rediseñado (eliminar fast_cut, peak_drawdown con vela confirmada), BE temprano (+0.15% RANGE_SCALP), PRS por niveles de estructura, `market_structure.py`, sizing ligado a calidad, FUTURES_SHORT como hedge automático (modo dual `*_WITH_HEDGE`).
- **Acciones inmediatas (§13):** Fase 0 hot-config de emergencia, Fase 1 cambios de código, Fase 2 validación con shadow log.

**Rollback:** Revertir hot-config a valores pre-cambio si PnL 48h empeora o WR cae >5pp.

**Actualización reciente (s105d, jul 31, caso ALLOUSDT/LAUSDT):** el "Ghost cleanup" de futuros (`futures_client.py` FIX-S4) borraba filas de `trades_open` sin posición en Binance **sin reconciliar el cierre real** — se perdía el PnL real (ganador o perdedor) de posiciones cerradas fuera del flujo normal de cierre. Fix: reconciliar contra `/fapi/v1/userTrades` (`_get_realized_pnl_from_trades`) e insertar en `trades_closed` (marcado `ghost_reconciled=true`, `learning_eligible=false`) antes de borrar. Ver detalle completo en el documento.

**Actualización reciente (s105e, jul 31):** el cierre `close_reason='regime_change'` (futuros SHORT) resultó ser un stop de tiempo mal calibrado — 34 cierres/0% WR/-275.50 USDT en 7 días, el peor `close_reason` de la ventana. Root cause: el guardia anti-whipsaw (`run_trades.py`, N35 jun-18) nunca incluyó `RANGE_SCALP` entre los regímenes "short-friendly" pese a que desde s99e (`range_scalp_short_preferred`) ese régimen abre y sostiene sus propios SHORT; como el sistema opera en `RANGE_SCALP` de forma casi continua, el contador global nunca se reseteaba y forzaba el cierre de cualquier SHORT nativo de `RANGE_SCALP` a los ~5 ciclos, sin que hubiera cambio de régimen real. Fix: se agregó `RANGE_SCALP` al conjunto de regímenes short-friendly en los 3 puntos donde se usaba la tupla — el auto-cierre por `regime_change` ahora sólo dispara ante una transición real hacia un régimen incompatible con SHORT (p. ej. `SPOT_BULL`).

**Actualización reciente (s105f, jul 31):** `close_reason='manual_close'` aparecía en cierres de FUTURES 100% automatizados sin que el usuario cerrara nada. Root cause: `futures_client.py::_classify_close_reason()` trataba cualquier orden Binance `type=MARKET` sin match de auditoría manual como prueba de cierre manual — pero `MARKET` es el tipo de orden que usa *todo* cierre automatizado del bot (regime_change, unified_governance, basket_reset_auto, gap protection). Se disparaba en la ruta de reconciliación (`-2022`/`ReduceOnly` en `close_long`/`close_short`, y la detección de "ghost" `FIX-T4` dentro de `check_positions()`, que corre cada ciclo). Fix: nuevo parámetro `fallback_reason` que preserva el motivo real del caller o usa la etiqueta neutral `binance_unidentified_close` en vez de adivinar "manual". De 17 cierres históricos `manual_close` en 7 días, 10 (todos FUTURES, sin audit real) se reclasificaron a `binance_unidentified_close`; los 6 SPOT genuinamente manuales no se tocaron.

**Actualización reciente (s105g, jul 31):** a pedido del usuario, rediseño para que el ciclo de vida **anticipe** a Binance en vez de descubrir su estado después del hecho (causa raíz de "posiciones abiertas en el sistema que en Binance no existen"). Fase 1: todas las órdenes que `futures_client.py` envía (entradas, SL/TP, cierres) ahora llevan `newClientOrderId` estructurado (`x-<reason>-<uuid8>` para cierres) — Binance mismo carga nuestra intención, eliminando la mayoría de las adivinanzas de `_classify_close_reason`. Fase 2: nuevo módulo `binance_event_stream.py` que consume el User Data Stream de Binance (futuros y spot, vía `python-binance` `ThreadedWebsocketManager`, ya en `requirements.txt`) — los cierres de futuros ahora se detectan y reconcilian en segundos (`FuturesClient.handle_order_fill_event`) en vez de esperar hasta 5 min al siguiente ciclo de polling; spot audita cada fill real en tiempo real. El polling y las rutinas de reconciliación existentes se mantienen intactas como red de seguridad. Health expuesto en `/health` (`event_stream_running`, `event_stream_connected`, etc.). Fases 3-4 (máquina de estados única, idempotencia de intención) quedan documentadas como hoja de ruta. **Verificado y corregido de raíz en producción real:** se actualizó `python-binance` de `1.0.16` a `1.0.37` (resuelve el endpoint `listenKey` obsoleto de spot y el riesgo de bloqueo indefinido en `stop()`), se fijó `websockets==16.1.1` en `requirements.txt` (una versión más nueva sin pin rompía la inicialización del socket manager), y se descubrió/corrigió una contención de arranque (el stream ahora arranca con 15s de delay en un hilo separado para no competir por el GIL con el sync pesado de posiciones al boot). Resultado confirmado: `event_stream_running=true`, `event_stream_connected=true`, eventos reales recibidos de Binance, sin afectar el trading en ningún momento.

**Actualización reciente (s105h, jul 31):** a pedido del usuario, diagnóstico de `binance_unidentified_close` (2ª peor categoría de pérdidas en 7 días: 18 cierres, -266.62 USDT, 5.6% WR) reveló un bug independiente introducido por s105g: el hilo del event stream (`handle_order_fill_event`) y las rutas de polling (Ghost-L1/L2 en `check_positions()`, el handler `-2022` de `close_long`/`close_short`) podían pasar la guarda `pos.closed` de forma concurrente y duplicar el mismo cierre en `trades_closed` — reproducido en vivo durante el diagnóstico (`CRVUSDT`). Fix: `threading.RLock` (`_close_lock`) serializando "detectar → reconciliar → persistir → marcar cerrado" en los 4 puntos de entrada, más un guard de duplicado a nivel DB en `_save_futures_closed()` como defensa en profundidad. 3 filas duplicadas históricas marcadas `corrupt=true` (sin borrar, con `metadata.duplicate_of` para trazabilidad). Desplegado y verificado en producción (`docker compose restart app`, health OK, trading normal post-restart). La atribución real de los cierres `binance_unidentified_close` no duplicados queda como diagnóstico pendiente para una sesión dedicada.

**Actualización reciente (s105i, ago 1):** fix para el error recurrente en logs de Docker `Error receiving message: Read loop has been closed`. `python-binance` cierra el read loop del websocket en determinadas condiciones; `binance_event_stream.py` ahora detecta payloads de error en los callbacks (`_is_socket_error_payload`), programa reconexión automática (`_reconnect`) con cooldown de 60s, re-suscribe mark-price streams y expone nuevas métricas en `/health/status`. Un watchdog de respaldo reconecta si no llega ningún evento en 30 min. Se mantiene el polling/reconciliación existente como red de seguridad. Tests: `tests/test_binance_event_stream.py` 19 passed. Requiere reiniciar el contenedor para activar el módulo modificado.

---

## 3. `calibrar_datos.md`

**Título:** Calibración de Datos — Inventario, Calidad, Linaje y Contratos Analíticos

**Propósito:** Referencia única de datos para análisis, calibración, entrenamiento, MIMIC, backtests y auditoría. Define qué fuente es apta para cada afirmación.

**Estructura (13 secciones):**

- **§1 Principios no negociables:** Producción es la verdad de PnL, no mezclar labels, no mezclar identidad (perfil régimen ≠ tier calidad), no usar futuro, no imputar silencio, no promover por frecuencia.
- **§2 Estado de evidencia:** `trades_closed` 60d: 1,124 cierres, -194.27 USDT, 50.2% WR. Modelo universal desplegado s93 (WR 69.7%, PF 4.55). Modelo RANGE_SCALP en shadow.
- **§3 Mapa de almacenamiento:**
  - **PostgreSQL:** `trades_open`, `trades_closed`, `trades_audit`, `entry_selection_candidates`, `ready_candidates`, `ready_candidate_checks`, `trading_runtime_state`, `kelly_sizing_log`.
  - **Históricos:** `historical_data/*.parquet` (OHLCV 1h), `opportunity_forward_labels_1h.jsonl`, manifests, datos externos (F&G, BTC dominance, funding).
  - **Peaks:** `peaks_all.parquet` (1,638,639 registros, 43 símbolos). Features `s_*` (inicio) vs `e_*` (fin, no permitidas para decisión live).
  - **MIMIC/backtests:** Datasets de calibración, walk-forward, universal, sweeps, super_mimic, lifecycle.
- **§4 Clasificación por uso:** Tabla de fuente primaria/secundaria/prohibición para 6 preguntas analíticas.
- **§5 Linaje:** Cadena `entry_selection_candidates` → `ready_candidates` → `ready_candidate_checks` → `trades_open` → `trades_closed`. Agrupación por `position_key` (close_group_id → entry_order_id → fallback). Labels oficiales: `realized_net_pnl_usdt`, `forward_net_4h`, `mimic_realized_net_pnl_pct`, etc.
- **§6 Contrato `range_scalp_live_v1`:** 14 features capturadas antes del gate. Completeness ≥95% requerido para promoción.
- **§7 Reglas de exclusión:** Sintéticos, ghost, dust, sin fill confirmado, MIMIC mezclado con producción.
- **§8 Artefactos reproducibles:** `build_research_manifest.py`, `build_research_outcomes.py`, `build_range_scalp_calibration_dataset.py`, `opportunity_labels.py`.
- **§9 Gates de aceptación:** Integridad manifest, label real, link candidato→trade, completeness shadow, promoción modelo, drift post-rollout.
- **§10–11 Operación y observabilidad:** 10 workers máx, walk-forward temporal, `GET /metrics/entry-evidence`.
- **§12 Relación con hermanos:** Este doc es autoridad de datos; `calibracion_entradas` consume datasets; `calibrar-ciclo-vida` consume outcomes agrupados.
- **§13 Checklist pre-análisis:** 8 preguntas obligatorias antes de cualquier análisis.

---

## 4. `hard_risk_breakeven_s99d.md`

**Título:** Breakeven en Hard-Risk Monitor (s99d)

**Propósito:** Documentar el fix que mueve el SL a net breakeven desde el hard-risk monitor cada 2s, evitando que picos de ganancia entre ciclos de trading se pierdan cuando el precio cae.

**Estructura (6 secciones):**

- **§1 Problema observado:** Caso BANKUSDT +$10 de ganancia no realizada, BE nunca activado, cerrado por `hard_risk_stop` a -$20.22.
- **§2 Fix implementado:** `_try_breakeven` en `hard_risk_monitor.py`, corre antes del breach check, mueve SL a `entry * (1 + 2*commission + slippage)`.
- **§3 Relación con pipeline de salida canónico:** Refina la etapa de Hard SL/TP del documento `calibrar-ciclo-vida-52fe5d.md`.
- **§4 Rollback:** `PATCH /config/kelly {"hard_risk_breakeven_enabled": false}`.
- **§5 Validación:** `tests/test_hard_risk_monitor.py` 6/6 passed; 526 tests focalizados sin fallos.
- **§6 Notas operativas:** Complementa, no reemplaza, el BE del trade cycle. Threshold de 0.15 % es el más agresivo entre regímenes.

---

## Relación entre documentos

```
calibrar_datos.md (autoridad de datos)
        │
        ├── feeds ──→ calibracion_entradas-a20ab8-v3.md (política de entrada)
        │
        └── feeds ──→ calibrar-ciclo-vida-52fe5d.md (política de salida)
                            │
                            └── refina ──→ hard_risk_breakeven_s99d.md
                                           (protección de ganancias en 2s)
```

- **Datos** define qué es apto para afirmar edge.
- **Entradas** define qué señales entran y cómo se asigna capital.
- **Lifecycle** define cómo se monitorean y cierran las posiciones.
- `.windsurf/rules.md` mantiene reglas operativas; los canónicos son autoridad de dominio.
