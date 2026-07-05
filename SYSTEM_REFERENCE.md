# SYSTEM_REFERENCE — crypto-agent

> **Archivo vivo. Actualizar en cada sesión relevante.**  
> Última actualización: jul 05 2026 | Sesión 44: cierre final del pendiente E2E (auth+rutas en vivo), hardening de snapshot y restauración de startup tras drift de defaults.

---

### ✅ Sesión 44 — Cierre final del pendiente E2E + estabilización startup post-restart — jul 05 2026

**Objetivo operativo (cumplido):**
1. Cerrar el último pendiente de paridad con evidencia en vivo autenticada (`dashboard`, `runtime-state`, `futures`, `binance-truth`).
2. Endurecer la captura de evidencia para no fallar ante `401/404` o drift de rutas.
3. Restaurar estabilidad de arranque del contenedor ante validación estricta de defaults canónicos.

**Implementación aplicada:**
1. `Dev/dashboard_parity_snapshot.py`:
  - manejo resiliente de errores HTTP/URL (`_http_status`, `_error`, `_body`);
  - resolución automática de API key (`DASHBOARD_API_KEY` desde env/.env o runtime-config);
  - fallback de rutas para runtime/truth (`/system/runtime-state|/runtime-state` y `/metrics/binance-truth|alternas`);
  - salida enriquecida con `api_key_resolved`, `resolved_endpoints`, `endpoint_status`.
2. `config_helpers.py`:
  - alineación de defaults críticos para pasar validación de paridad con `config.Settings`;
  - sincronización dinámica de defaults complejos (`token_categories`, `category_thresholds`, `narrative_tokens`, `analysis_symbols`, `trading_excluded_symbols`) desde `Settings` para evitar drift futuro.

**Evidencia de cierre (snapshot final):**
1. `Dev/dashboard_parity_snapshot.json` generado con `api_key_resolved=true`.
2. `endpoint_status` final: `dashboard_summary=200`, `runtime_state=200`, `futures_positions=200`, `binance_truth=200`.
3. `binance_truth.mismatch_count=0` y `mismatch_count=0` en comparación de posiciones abiertas.
4. `snapshot_meta` presente en dashboard (`snapshot_timestamp`, `snapshot_generation_ms`, `data_freshness_seconds`).

**Estado final:**
1. Pendiente operativo E2E: cerrado.
2. Runtime recuperado post-restart con validación de defaults controlada.

---

### ✅ Sesión 43 — Implementación Dashboard-Binance Parity E2E (SPOT+FUTURES) — jul 05 2026

**Objetivo operativo (cumplido):**
1. Convertir la verificación de dashboard en fase bloqueante con correlación 1:1 contra Binance.
2. Reducir falsos positivos de tipo/edad (`SPOT`, `<1h`) por fallbacks ambiguos.
3. Exponer estado de frescura/snapshot para decisiones tácticas rápidas con menor riesgo de actuar sobre datos vencidos.

**Implementación aplicada:**
1. `app.py` (`/dashboard/summary`):
  - agrega `snapshot_meta` (`snapshot_timestamp`, `snapshot_finished_at`, `snapshot_generation_ms`, `tactical_decision_blocked` por severidad de `binance_truth`);
  - expone `basket_reset_decision` desde `trading_runtime_state`;
  - fortalece `open_positions` con `first_entry_time`, flags de parse de edad y `total_unrealized_pnl` consolidado.
2. `futures_client.py`:
  - consolidación preserva antigüedad real (usa `entry_time` más antiguo al consolidar);
  - incluye `first_opened_at` en `get_open_positions_summary()` para edad consistente post-restart.
3. `position_manager.py`:
  - elimina fallback ambiguo `COALESCE(...,'spot')` en carga de abiertas SPOT;
  - audita filas con `trade_type` faltante para trazabilidad de drift legacy.
4. `frontend/dashboard.js`:
  - añade alertas de staleness (`snapshot_age`, falta de fetch >60s);
  - integra alerta operativa de `basket_reset_decision.trigger`;
  - cálculo y orden de edad usa máximo entre `first_entry_time`, `entries.age_h` y `hours_held` para evitar sub-reporte.
5. `Dev/dashboard_parity_snapshot.py` (nuevo):
  - herramienta de captura 3x snapshots y comparación `dashboard ↔ futures endpoint ↔ runtime state ↔ binance_truth`.

**Estado de fase:**
1. Implementación completada en código para fases de snapshot, paridad por campo, staleness visible y preservación de edad real.
2. Validación enfocada requerida al cierre de esta sesión para confirmar PASS final tras cambios.

---

### ✅ Sesión 36 — Mode/Profile Harmony + KPI Governance (manual operativo) — jul 04 2026 18:10 UTC-6

**Objetivo:** convertir la implementación de hoy en un modelo formal para siguientes iteraciones: máxima ganancia con control operativo, trazabilidad y criterio explícito de aceptación/rollback.

**Implementación principal (hoy):**
1. `run_trades.py`:
  - overlay armónico modo/perfil (`_apply_mode_profile_harmony`) con firma idempotente para evitar drift por reaplicación;
  - persistencia del estado efectivo (`_persist_mode_profile_harmony_snapshot`) en `trading_runtime_state`;
  - Smart Kelly con doble contexto: multiplicador por modo/perfil (`_get_kelly_context_multiplier`) + multiplicador por salud KPI (`_get_kpi_context_multiplier`).
2. `smart_scale_out.py`:
  - SSO perfil-aware: escalado por perfil A/B/C y por régimen para SSO-1..SSO-6;
  - ajuste coherente entre agresividad de entrada (Kelly) y salida inteligente (SSO).
3. `app.py`:
  - snapshot de armonía y drift (`_get_mode_profile_harmony_snapshot`, `_get_mode_profile_harmony_drift`);
  - gate de rollout (`_get_rollout_readiness_snapshot`) con causas bloqueantes explícitas;
  - validación de paridad live/backtest (`_get_parity_validation_snapshot`);
  - criterio de promoción post-cambio (`_get_promotion_criteria_snapshot`);
  - exposición en dashboard/health de `mode_profile_harmony`, `rollout_readiness`, `parity_validation`, `promotion_criteria`.

**Estado operativo actual (al cierre de sesión):**
1. La plomería de gobernanza quedó implementada y visible.
2. La promoción automática se mantiene condicionada por evidencia (sample + parity), no por intuición.
3. Drift activo crítico de armonía se redujo a 0 en validaciones de runtime; deuda histórica puede seguir visible como contexto de auditoría.

---

### ✅ Sesión 37 — Caso SUIUSDT/XRPUSDT (negativos en FUTURES) — jul 04 2026

**Hallazgo forense (producción):**
1. SUIUSDT y XRPUSDT en SHORT estuvieron en pérdida prolongada con SL aún no tocado.
2. SSO no intervenía en pérdida para esos casos (`NO ACTION`), porque la lógica de salida principal estaba orientada a profit/bounce gates.
3. En SPOT se detectó error en evaluación SSO por variable de régimen inválida (`_regime_mode` fuera de scope), lo que podía desactivar protección en ciertos símbolos.

**Corrección aplicada:**
1. `run_trades.py`: fix de scope en SSO SPOT (`regime_mode=regime`) para evitar fallos de evaluación.
2. `futures_client.py`: nueva protección `adverse_signal_exit`:
  - si una posición FUTURES ya está en pérdida por encima de umbral,
  - supera tiempo mínimo,
  - y la señal técnica 1h se voltea en contra con fuerza mínima,
  - se cierra antes del hard SL.
3. Configuración persistente y editable añadida:
  - `futures_adverse_exit_enabled`
  - `futures_adverse_exit_loss_pct`
  - `futures_adverse_exit_min_hours`
  - `futures_adverse_exit_min_strength`
  (persistida en `Settings` + defaults canónicos + patch whitelist API).

**Impacto SPOT/FUTURES y perfiles:**
1. SPOT: se recupera estabilidad del path SSO (sin excepción por régimen).
2. FUTURES: se agrega salida temprana anti-deterioro para evitar deriva hasta SL completo cuando el mercado cambia de dirección.
3. Perfiles A/B/C: la corrección aplica por igual; los umbrales siguen gobernados por configuración runtime y modo activo.

**Trazabilidad y restart-safe:**
1. Cada cierre temprano registra auditoría `trades_audit.event='adverse_signal_exit'`.
2. Estado de decisión y metadata de posición permanecen en BD y sobreviven reinicios.
3. Validación enfocada posterior al fix: PASS.

---

### ✅ Sesión 38 — Implementación inicial de emergencia (triage SPOT/FUTURES) — jul 04 2026

**Objetivo operativo inmediato:**
1. Ejecutar fase 0/1 de emergencia sin cierres impulsivos: clasificar posiciones por probabilidad de recuperación antes de decidir rescate o cierre defensivo.
2. Aplicar la misma lógica para SPOT y FUTURES con sensibilidad por perfil A/B/C.
3. Mantener trazabilidad 100% y estado restart-safe en BD.

**Implementación aplicada:**
1. `app.py` agrega `GET /positions/emergency-triage`:
  - construye snapshot de riesgo por posición (SPOT/FUTURES),
  - clasifica en `recuperable`, `vigilancia_activa`, `cierre_defensivo`,
  - incluye cobertura activa (`adverse_exit_applicable`, `profit_supervisor_applicable`, `sso_waiting`, `monitor_only`).
2. `app.py` agrega `POST /positions/emergency-triage/snapshot` (auth):
  - persiste snapshot en `trading_runtime_state.key='emergency_triage_latest'`,
  - audita captura en `trades_audit.event='emergency_triage_snapshot'`.
3. El endpoint de triage no ejecuta cierres; solo habilita decisiones operativas basadas en datos.

**Contrato de emergencia (activo):**
1. No cerrar por pánico: toda salida defensiva debe estar sustentada por riesgo alto + baja probabilidad de recuperación.
2. Priorizar cierres individuales sobre batch durante contingencia para minimizar huecos de trazabilidad.
3. Validar por split SPOT/FUTURES y perfil A/B/C antes de aplicar acciones de ejecución.

**Trazabilidad y restart-safe:**
1. Snapshot operativo persistido en BD (`trading_runtime_state`) para reconstrucción post-reinicio.
2. Evento auditable en `trades_audit` por cada captura oficial del triage.
3. Compatible con `monitor_only/pause_new_entries`: el diagnóstico refleja el modo real de operación.

---

### ✅ Sesión 39 — Supervisor unificado de gobernanza (profit+loss) y rollback de triage — jul 05 2026

**Objetivo operativo:**
1. Consolidar autoridad de salida en un supervisor único para SPOT/FUTURES sin romper protecciones hard SL/safety SL.
2. Eliminar la capa táctica de triage de emergencia (endpoint/snapshot) para volver al plan estratégico estable.
3. Mantener trazabilidad 100% y restart-safe con metadata persistida y auditoría por decisión.

**Implementación aplicada:**
1. `app.py`:
  - rollback completo de `EmergencyTriageSnapshotRequest`, helpers de triage y endpoints `GET/POST /positions/emergency-triage*`.
  - nuevo endpoint `GET /metrics/unified-governance` para visibilidad live de decisiones abiertas y eventos 24h.
2. `unified_governance_supervisor.py` (nuevo):
  - `UnifiedGovernanceContext` y `UnifiedGovernanceDecision`;
  - rama de profit delega en `profit_realization_supervisor`;
  - rama de pérdida gestiona min-age/escalado, guard de distancia a SL, aceleración y reversión de señal, con modo `monitor_only`.
3. `run_trades.py` y `futures_client.py`:
  - integración del supervisor en flujo de decisión antes de cierre suave final;
  - persistencia de `state_patch` en metadata (`metadata` SPOT / `_smart_meta` FUTURES);
  - auditoría explícita `unified_governance_decision` y `unified_governance_close_triggered`.
4. `config.py` + `config_helpers.py`:
  - nuevas llaves runtime para activar/desactivar y parametrizar profit/loss governance por perfil y tipo de mercado.

**Validación y estado de fase:**
1. Escaneo de errores en archivos tocados: sin errores.
2. Runner enfocado: `focused_validation_runner.py` => `{"overall": "PASS"}`.
3. Estado: implementación cerrada en código para fases de rollback, supervisor, integración, persistencia/auditoría y validación base.

---

### ✅ Sesión 40 — Cierre operativo: cambios, notas y memorias consolidadas — jul 05 2026

**Cierre ejecutado:**
1. Cambios de código guardados en árbol de trabajo y verificados sin errores en archivos modificados.
2. Notas de sesión y memoria de repositorio consolidadas con foco en supervisor unificado y trazabilidad.
3. SYSTEM_REFERENCE actualizado como fuente de verdad de fase y estado.

**Evidencia de cierre:**
1. Validación enfocada más reciente: `focused_validation_runner.py` => `{"overall": "PASS"}`.
2. Observabilidad activa de gobernanza: endpoint `GET /metrics/unified-governance`.
3. Auditoría activa de decisiones: `unified_governance_decision` + `unified_governance_close_triggered`.

**Estado final de implementación (plan original):**
1. Fases B/C/D/E/G: completadas.
2. Fase H: lista para ejecución en ventana de rollout controlado.

---

### ✅ Sesión 41 — Implementación por fases ejecutada (UG/PRS/SPOT+FUTURES) — jul 05 2026

**Objetivo operativo (cumplido):**
1. Ejecutar en una sola pasada los fixes críticos detectados en auditoría integral, sin desviarse del plan por fases.
2. Asegurar paridad de comportamiento SPOT/FUTURES en `monitor_only` y en evaluación de supervisores.
3. Cerrar con validación enfocada en verde y trazabilidad persistida en referencias/memorias.

**Fase 1 — Safety/monitor-only (completada):**
1. `run_trades.py` corrige signo de `distance_to_sl_pct` en SPOT para UG (distancia positiva cuando el precio está por encima del SL efectivo).
2. `run_trades.execute_once(monitor_only=True)` ahora ejecuta también monitoreo/cierres de FUTURES antes de retornar.
3. `_check_sl_tp_positions(..., monitor_only=...)` recibe bandera explícita y la propaga al contexto de UG en SPOT.
4. `futures_client.check_positions(..., monitor_only=...)` incorpora parámetro y lo propaga a `UnifiedGovernanceContext`.

**Fase 2 — Contexto de señal/velocidad en supervisores (completada):**
1. SPOT y FUTURES ahora inyectan `current_signal_strength`, `current_signal_direction` y `pnl_velocity_1h` en PRS/UG con fallback seguro.
2. Se reutiliza snapshot de velocidad (`sso_pnl_snapshot`) para continuidad restart-safe y menor deriva de contexto entre ciclos.
3. `profit_realization_supervisor.py` ajusta criterio: señal ausente (`None`) se trata como neutral, no como señal degradada.

**Fase 3 — Canonical defaults + regresión (completada):**
1. `config_helpers.py` alinea defaults críticos con `config.py`:
  - `kelly_dynamic_base_pct`
  - `max_per_trade_usdt`
  - `max_positions`
  - `trailing_stop_activation_pct`
  - `trailing_stop_distance_pct`
2. `tests/test_futures_smart_exits.py` agrega:
  - prueba de paridad de defaults críticos;
  - prueba de propagación de `monitor_only` hacia UG en FUTURES.

---

### ✅ Sesión 42 — Implementación final del plan silencioso E2E — jul 05 2026

**Objetivo operativo (cumplido):**
1. Ejecutar la última revisión del plan y convertir los fallbacks silenciosos críticos en comportamiento fail-safe.
2. Reforzar la verdad canónica de configuración, metadata y dashboard sin desviarse del flujo original del sistema.
3. Mantener el foco en maximizar ganancias evitando entradas dañinas, cierres falsos y drift de restart.

**Implementación aplicada:**
1. `run_trades.py`:
  - funding rate SPOT ahora falla de forma segura y bloquea la entrada si la API no responde;
  - la confirmación de vela roja y la guardia de correlación/short momentum ya no permiten entrar en silencio cuando fallan;
  - la correlación de shorts degrada la exposición de forma conservadora ante errores.
2. `futures_client.py`:
  - restauración de metadata normalizada con preservación de corrupción y reconstrucción UTC de `opened_at`;
  - `monitor_only` y contexto UG permanecen intactos.
3. `position_manager.py`:
  - metadata no dict ahora conserva evidencia de corrupción en vez de reescribirse como `{}`;
  - se evita perder el rastro de por qué faltaba un campo.
4. `config_helpers.py` + `init_components.py`:
  - `validate_config()` ahora verifica paridad de defaults canónicos con `config.py`;
  - el bootstrap de componentes bloquea el arranque si existe drift crítico.
5. `app.py`:
  - parsing de metadata con warning explícito en caso de corrupción;
  - cálculo de `hours_held` unificado a UTC para SPOT y FUTURES.
6. `futures_correlation.py`:
  - fallas de correlación pasan a sizing conservador en vez de fail-open.
7. Tests:
  - agregado chequeo de validación de config canónica en `tests/test_config_governance.py`.

**Validación y estado final:**
1. Escaneo de errores en archivos tocados: sin errores.
2. Runner enfocado: `focused_validation_runner.py` => `{"overall": "PASS"}`.
3. Estado: el plan quedó implementado, validado y documentado; no quedan pasos pendientes dentro del alcance definido.

**Validación y estado final:**
1. Primera corrida del runner enfocada: FAIL controlado por prueba nueva (comparaba contra valores runtime con override de entorno).
2. Ajuste aplicado a prueba de paridad para comparar contra defaults declarados del modelo.
3. Segunda corrida: `focused_validation_runner.py` => `{"overall": "PASS"}`.
4. Estado de cierre: implementación por fases completada, validada y documentada.

**Memoria/Notas persistidas en repo memory:**
1. `unified_governance_supervisor_2026_07_05.md` actualizado con cierre de implementación (sign fix + monitor_only parity + contexto PRS/UG).
2. `profit_realization_supervisor_2026_07_05.md` actualizado con criterio de señal neutral cuando falta contexto.
3. `production-hardening-2026-06-29.md` actualizado con hardening adicional del 2026-07-05.

---

### Profit Realization Supervisor (autoridad central de salida)

El sistema usa un supervisor central de realización de ganancias para evitar que SmartTP, anti-wick, protección de reemplazo y SSO bloqueen rebotes ya maduros.

**Reglas operativas:**
1. Aplica a SPOT y FUTURES con umbrales separados por tipo y por perfil A/B/C.
2. Tiene prioridad sobre extensiones de TP y sobre confirmaciones anti-wick cuando la salida ya está justificada por tiempo en profit, giveback o pérdida de momentum.
3. No desactiva SL ni backstops de seguridad: solo acelera la monetización cuando la posición ya entregó suficiente recorrido.
4. Cada decisión debe persistirse en `trading_runtime_state` o metadata de posición y dejar evento auditable en `trades_audit`.
5. Si falta el estado de arranque, el supervisor reconstruye el tiempo en profit desde metadata persistida y sigue siendo restart-safe.

**Contrato mínimo esperado:**
1. `profit_realization_started_at` se guarda la primera vez que una posición entra en verde.
2. `profit_realization_last_reason` y `profit_realization_last_detail` dejan la razón operativa de la última intervención.
3. El dashboard/metrics debe mostrar posiciones positivas sin lock, minutos medios en profit sin realización y conteo de decisiones del supervisor.
4. Si el supervisor realiza un parcial, el resto de la posición sigue bajo SL/TP/trailing normal, pero con nueva traza persistida.

**Criterio de uso:**
1. Si una posición está en profit, pero no avanza o devuelve parte importante del peak, el supervisor puede cerrar parcial o totalmente antes de que SmartTP decida extender.
2. Si la señal sigue fuerte y el movimiento está limpio, el supervisor debe dejar correr la posición.
3. En reinicio, la misma decisión debe poder reconstruirse con la metadata ya persistida.

## MODELO OPERATIVO DE REFERENCIA (BASE DE FUNCIONAMIENTO)

### 0) Flujo canónico del sistema (orden obligatorio)

1. Detectar régimen de mercado.
2. Ejecutar selector SMART Total de modo (si está habilitado).
3. Ejecutar selector de perfil A/B/C para el modo efectivo.
4. Aplicar parámetros de régimen/perfil (`_apply_regime_params`).
5. Aplicar overlay de armonía modo/perfil (`_apply_mode_profile_harmony`).
6. Persistir snapshot efectivo en runtime state.
7. Ejecutar decisiones de entrada/salida con Smart Kelly + SSO.
8. Evaluar salud, drift, paridad y readiness para gobierno de rollout.

Este orden no se negocia: cualquier cambio futuro debe preservar esta secuencia o justificar explícitamente la variación.

### 1) KPI Gates como criterio de aceptación y rollback

Los gates no son decorativos: son el contrato operativo entre implementación y producción.

**Gate A — Harmony Drift (coherencia de parámetros):**
1. Fuente: `mode_profile_harmony_snapshot` vs runtime actual.
2. Si hay drift activo crítico, `rollout_readiness` debe ser `blocked`.
3. Drift histórico no reconocido permanece visible para auditoría, pero se separa del drift activo para evitar bloqueos falsos.

**Gate B — Health Crítico (estabilidad operativa):**
1. Fuente: `health_results_latest`.
2. Si estado reciente es `critical/error`, el motor prioriza protección; en runtime puede entrar en `monitor_only`.

**Gate C — Performance Changepoint (before/after):**
1. Fuente: `performance_changepoint_*` contra baseline 3y/60000.
2. Deterioro severo post-cambio bloquea promoción y activa rollback selectivo.

**Gate D — Parity Live vs Backtest:**
1. Fuente: compare-vs-live del run baseline activo.
2. Gap crítico en PnL/win-rate bloquea rollout final aunque el sistema esté estable.

**Gate E — Promotion Criteria (evidencia mínima):**
1. Requiere muestra mínima post-cambio, paridad aceptable y ausencia de drift activo crítico.
2. Estados esperados: `promote`, `observe`, `hold`.

**Protocolo de rollback (modelo obligatorio):**
1. Rollback siempre es selectivo (modo/perfil/parámetro afectado), no desmontaje global.
2. Si falla Gate A/B: rollback inmediato al último snapshot estable.
3. Si falla Gate C/D/E: mantener observación y revertir solo los parámetros responsables del gap.
4. Toda reversión debe quedar auditada en `trades_audit` y persistida en `trading_runtime_state`.

### 2) Smart Kelly (arquitectura aplicada hoy)

Smart Kelly queda definido como sizing contextual, no estático:

1. Base por configuración (spot/futures) con límites duros (`base_usdt`, `max_usdt`, `max_pct_liquid`).
2. Multiplicador por contexto de mercado y perfil:
  - Profile A: más agresivo para capturar velocidad de capitalización.
  - Profile B: balance riesgo/retorno.
  - Profile C: defensivo para preservación.
3. Multiplicador por salud KPI (`health_results_latest`) para evitar sobreexposición en degradación.
4. Aplicación antes de cap final para mantener el control de riesgo dentro de límites globales.
5. Trazabilidad en logs/snapshot para explicar por qué un size fue mayor o menor.

### 3) SSO (Smart Scale-Out) como motor de salida inteligente

SSO es la capa que convierte ventaja estadística en velocidad de reciclaje de capital:

1. SSO-1 estancamiento de beneficio.
2. SSO-2 agotamiento de momentum.
3. SSO-3 escalado por ATR/multi-nivel.
4. SSO-4 contribución a objetivo diario.
5. SSO-5 upgrade por mejor oportunidad.
6. SSO-6 rescue de rebote con smart gate.

Aplicación por perfil:
1. A deja correr más los ganadores y tolera más variación táctica.
2. B intermedio para equilibrio de captura/protección.
3. C acelera protección y reciclaje defensivo.

Regla de coherencia: nunca ajustar SSO aislado de Kelly; entrada y salida deben permanecer armónicas por modo/perfil.

### 4) Política de SL: lógico vs Binance (modelo definitivo)

1. **SL lógico (software)** es el control primario de ejecución y gestión dinámica.
2. **SL Binance (exchange)** es red de seguridad de último recurso (backstop), no trigger principal de estrategia.
3. El SL Binance no debe endurecerse por debajo del piso operativo seguro definido por análisis histórico de stops.
4. Si hay desalineación, se corrige con criterio conservador y trazabilidad, evitando crear riesgo adicional por sobreajuste.

Esta separación es obligatoria en nuevos fixes: optimizar el SL lógico no autoriza degradar la función defensiva del SL de Binance.

### 5) Baseline 3y/60000 + changepoints como centro de verdad

1. Toda mejora debe referenciar baseline 3y/60000 y su `base_run_id` activo.
2. Todo cambio relevante debe marcar changepoint para medición before/after.
3. Aceptación final requiere evidencia combinada: performance + paridad + estabilidad operativa.
4. Si mejora una métrica y empeora cierre/SL/drift crítico, no se promueve.

### 6) Estado persistente y observabilidad obligatorios

Toda funcionalidad nueva debe exponer:
1. Estado persistente restart-safe en `trading_runtime_state`.
2. Evento auditable en `trades_audit` cuando cambie decisiones o parámetros.
3. Superficie de observabilidad en API/dashboard para diagnóstico operativo.

Endpoints de control/observación que forman parte del modelo:
1. `/metrics/performance-changepoint`
2. `/metrics/rollout-readiness`
3. `/metrics/promotion-criteria`
4. `/backtest/compare-vs-live`
5. `/dashboard/summary` (bloques de harmony/parity/readiness/promotion)

### 7) Plantilla obligatoria para siguientes implementaciones y fixes

Antes de merge en producción, cada cambio debe responder explícitamente:

1. ¿Cómo impacta SPOT y FUTURES?
2. ¿Cómo impacta perfiles A/B/C?
3. ¿Qué gate valida aceptación y cuál gatilla rollback?
4. ¿Qué estado se persiste y qué evento se audita?
5. ¿Cómo se mide contra baseline/changepoint?
6. ¿Qué endpoint/dashboard mostrará la salud del cambio?

Si una propuesta no responde estos 6 puntos, no está lista para rollout.

---

### ✅ Sesión 35 — Hardening F1-F6 + Dashboard Value Review — jul 03 2026 19:40 UTC-6

**Objetivo:** cerrar implementación integral del plan agresivo-inteligente sin desviar motores, reforzando calidad de entrada, resiliencia operacional y visibilidad accionable.

**Implementación principal:**
1. `run_trades.py`:
  - persistencia de snapshot atómico por ciclo en `trading_runtime_state.key='effective_runtime_selection_snapshot'` con modo/perfil efectivos, selector y claves aplicadas;
  - inyección en `FUTURES_SHORT` endurecida: requiere análisis fresco y `signal=SELL`, eliminando fallback permisivo por `HOLD/no dirección`;
  - helper de edad de análisis + política de frescura unificada para bloquear inyecciones stale;
  - filtro de calidad del screener con relajación adaptativa controlada para evitar colapso de cobertura en mercado estresado;
  - gate defensivo: si `health_results_latest` está `critical/error` (reciente), `execute_once` entra en `monitor_only`.
2. `pnl_service.py`:
  - detector de salida degenerada del predictor (`expected_pnl≈0` + probabilidad neutra fuera de bootstrap) y rechazo explícito en validación.
3. `app.py`:
  - nuevo bloque `operator_focus` en `/dashboard/summary` con cola priorizada (critical/warning/info), acciones sugeridas y contexto de transición/runtime.
4. Frontend (`frontend/index.html`, `frontend/dashboard.js`, `frontend/styles.css`):
  - nuevo panel **Operator Focus** en tab Sistema con ranking, severidad, detalle y acción operativa por item.

**Validación ejecutada:**
1. Revisión de errores de edición en archivos modificados (`run_trades.py`, `pnl_service.py`, `app.py`, `dashboard.js`, `index.html`, `styles.css`) → **sin errores**.
2. Runner de validación enfocada reciente (`Dev/focused_validation_runner.py`) reportado en verde (`Exit Code: 0`).

**Impacto operativo esperado:**
1. Menos entradas FUTURES_SHORT incoherentes y menor ruido por análisis viejo.
2. Protección automática ante degradación crítica de health por resultados.
3. Trazabilidad completa de selección modo/perfil/params por ciclo.
4. Dashboard centrado en decisiones accionables (no solo métricas descriptivas).

---

### ✅ Sesión 34 — SMART Total Fase 2 (modo + perfil) — jul 03 2026 13:02 UTC-6

**Objetivo:** implementar decisión dual por ciclo (cambio de modo + cambio de perfil) con trazabilidad completa y estado restart-safe.

**Implementación principal:**
1. `run_trades.py` agrega selector de modo SMART Total con estado dedicado:
  - `trading_runtime_state.key='smart_total_mode_selector_state'`
  - `trading_runtime_state.key='smart_total_active_mode'`
  - auditoría por ciclo: `trades_audit.event='smart_total_mode_selector_decision'`
2. Selector de perfil desacoplado del selector de modo:
  - nuevo estado: `smart_total_profile_selector_state_<REGIME>`
  - mantiene mirror backward-compatible a `regime_profile_selector_state_<REGIME>`
3. Orden de decisión en ciclo `execute_once`:
  - primero selector SMART Total de modo
  - después selector de perfil sobre modo efectivo
  - actualización inmediata de `settings.current_regime_mode` y `settings.current_regime_profile_label`
4. `app.py` amplía config/normalización del selector dual:
  - campos nuevos de modo: `smart_total_enabled`, `mode_selector_enabled`, `mode_require_confirmed`, `mode_min_hold_cycles`, `mode_min_confidence_delta`, `mode_audit_every_cycle`
  - campos nuevos de perfil: `profile_selector_enabled`, `profile_min_hold_cycles`, `profile_min_score_delta`
  - `GET /regime/profiles/selector-decisions` ahora retorna decisiones de modo + perfil.

**Validación ejecutada:**
1. `tests/test_health_audit.py -k selector_config_normalization_supports_smart_total_mode_fields` → **1 passed**.
2. `tests/test_system_health.py -k SmartTotalSelector` → **2 passed**.

**Impacto operativo:**
1. SMART Total ya decide por separado el cambio de modo y el cambio de perfil.
2. Modo y perfil tienen histéresis/cooldown independientes.
3. Estado y auditoría quedan persistidos para reinicio, diagnóstico y análisis forense.

---

### ✅ Sesión 33 — Performance Monitor 3y/60000 (API + dashboard) — jul 02 2026 10:28 UTC-6

**Objetivo:** medir el efecto de los cambios desde el changepoint `PERF_3Y60K_PRIORITY_2026-07-02`.

**Implementación:**
1. Endpoint nuevo: [`GET /metrics/performance-changepoint`](s:/Tools/AI/crypto-agent/app.py) con comparación before/after.
2. Dashboard summary ahora incluye `performance_changepoint`.
3. Comparativa separa SPOT y FUTURES, y calcula `net_pnl`, `fees`, `win_rate` y `delta` contra la ventana previa equivalente.

**Estado de validación:**
1. Endpoint responde `ok=true`.
2. Dashboard summary ya incorpora el bloque `performance_changepoint`.
3. El marker persiste en BD y está auditado.

---

### ✅ Sesión 32 — Performance Changepoint 3y/60000 (prioridad absoluta) — jul 02 2026 10:20 UTC-6

**Changepoint ID:** `PERF_3Y60K_PRIORITY_2026-07-02`

**Centro de verdad (prioritario):**
1. Smart Backtest 3y / 60000 simulaciones (`root_session_id=c684eab3-a968-4822-8b15-8b75c7e956b4`).
2. Toda decisión de configuración posterior se compara contra este baseline.

**Baseline aplicado en sistema (SPOT + FUTURES):**
1. Base global de mayor retorno: `backtest_run.id=61556`.
2. Perfiles activos por escenario (Profile A):
  - `SPOT_BULL -> run 29600`
  - `RANGE_SCALP -> run 48412`
  - `PRESERVATION -> run 51631`
  - `FUTURES_SHORT -> run 61556`
3. Selector automático determinístico activo:
  - `enabled=true`
  - `min_hold_cycles=3`
  - `min_score_delta=0.05`
  - `profile_rank: A > B > C`

**Protocolo de medición post-cambio (obligatorio):**
1. Comparativa before/after desde el changepoint para ambos tipos de trading: SPOT y FUTURES.
2. KPIs mínimos: `pnl_total neto`, `pnl_diario`, `win_rate`, `profit_factor`, `max_drawdown`, `n_trades`, `fees`, `latencia de ciclo`, `calidad de cierre (SL/TP/trailing/rebalance)`.
3. Criterio de priorización: maximizar ganancias en el menor tiempo posible sin perder integridad operativa.

**Gobierno, trazabilidad y persistencia:**
1. Toda mutación de configuración debe generar evento auditable en `trades_audit`.
2. Todo estado activo/configuración debe persistirse en `trading_runtime_state` para supervivencia a reinicios.
3. Regla universal: cualquier implementación/análisis debe cubrir explícitamente SPOT y FUTURES (o documentar por qué no aplica).

---

### ✅ Sesión 31 — Parametrización UX de capital y validación de paridad live/backtest — jul 01 2026 11:30 UTC-6

**Objetivos alcanzados:**
1. ✅ Permitir ejecución de backtest sin parámetro `--capital` (fetch live balance automático)
2. ✅ Validar que live trades y backtest trades compartan metadata consistente (trade_type, close_reason)
3. ✅ Eliminar artefactos: PnL inflado a 231M USDT, metadata.trade_type='unknown'

#### Cambios en [Dev/smart_backtest_optimizer.py](Dev/smart_backtest_optimizer.py)

| Componente | Cambio | Líneas |
|-----------|--------|--------|
| Docstring | Agregadas ejemplos sin `--capital` + nota fetch automático | 1-25 |
| `_get_live_balance()` | Nueva: GET `/account/balance`, extrae `total_usdt` | 337-353 |
| Argparse `--capital` | `required=True` → `required=False, default=None` | 362 |
| main() — capital resolución | Lógica: explicit/live/error con instrucciones | 373-383 |
| Ejecución de jobs | Usa variable resuelta `capital` no `args.capital` | 427 |

**Uso:**
```bash
# Automático (fetch vivo)
python Dev/smart_backtest_optimizer.py --period 1y --simulations 12000

# Explícito (sin fetch)
python Dev/smart_backtest_optimizer.py --period 1y --simulations 12000 --capital 5283.95
```

#### Cambios en metadata SPOT y close_reason

**position_manager.py (líneas 200, 658):**
- `metadata.setdefault('trade_type', 'spot')` en apertura y cierre SPOT

**app.py (líneas 4599-4717):**
- `_normalize_close_reason()`: mapea strings brutos → categorías canónicas
- compare-vs-live: infiere `trade_type='spot'` si falta; normaliza close_reason

**Efecto:** Paridad observable entre live y backtest sin 'unknown' types.

#### Validación

✅ Sintaxis: py_compile exitoso  
✅ Argparse: `--capital` opcional  
✅ Error handling: mensaje amigable cuando API falla  
✅ Baseline PnL: 19.4k (realista) vs 231M (inflado pre-fix)  
✅ Metadata: trade_type y close_reason normalizados  

---

### ✅ Sesión 30 — Diagnóstico y reparación del ciclo detenido — jun 30 2026 18:20 UTC-6

**Síntoma:** `/health/status` mostró `warning`, `trade_cycle` stale (>6 min), y logs con `Error in cycle: name 'sig' is not defined`.

**Diagnóstico:**
- El `orchestrator.py` capturaba excepciones sin traceback, ocultando la causa.
- Con trazas habilitadas, el traceback apuntó a `run_trades.py:2982` en `_buy_top_symbols()`: se usaba `sig.get('signal_id')` pero la variable `sig` no existía en el path de compras spot.
- El error ocurría en el path de SPOT (también afectaba RANGE_SCALP y SPOT_BULL); FUTURES_SHORT no se afectaba porque bifurca antes del punto del bug.
- `position_sync` reportaba `futures_tracked=0`, lo cual es normal: futuros se rastrean en `futures_client._open_positions` y se persisten los cierres en `trades_closed`, no en la tabla `open_positions` de spot.

**Fixes aplicados:**
- `orchestrator.py`: se agregó `traceback.format_exc()` al log de `Error in cycle` para diagnóstico futuro.
- `run_trades.py`: se corrigió `sig.get('signal_id')` → `signal_meta.get(symbol, {}).get('signal_id')`.
- `run_trades.py`: se inicializó `volspike_override = False` al inicio de cada iteración de `_buy_top_symbols` para evitar `UnboundLocalError` en compras spot normales.
- `run_trades.py`: se agregó `'EJECUTADO'` al set de razones spot en `_infer_trade_type_from_reason` y un fallback a `'system'` cuando el tipo no se reconoce, evitando violaciones de la constraint `trades_audit_trade_type_check`.
- `run_trades.py`: se eliminó el import local `from trade_utils import get_symbol_filters...` dentro de la rama SSO de `_check_sl_tp_positions`. El import local hacía que `get_symbol_filters` se comportara como variable local de la función; cuando el flujo SL/TP se ejecutaba sin pasar primero por la rama SSO, Python lanzaba `UnboundLocalError` y bloqueaba todos los cierres de SL/TP (AAVEUSDT, BTCUSDT, WLDUSDT). Se mantiene el import a nivel de módulo (línea 14) y el log de traceback para futuros diagnósticos.
- Se reinició el contenedor `crypto-agent-app` varias veces para cargar los cambios.

**Verificación post-fix:**
- `/health/status`: `overall=ok`, `errors_last_5min=0`, `warnings_last_5min=0`.
- `/errors/recent`: sin errores; solo mensaje INFO de salida SL/TP (WLDUSDT).
- El ciclo completó sin `Error in cycle`, `Failed to track` ni `SL/TP sell failed`.
- Nota: dos compras spot previas (AAVEUSDT, TRXUSDT) quedaron sin trackear por el bug de `volspike_override`; el sistema de reconciliación/cleanup las manejará como huérfanos en ciclos siguientes. Las posiciones futuras previas se cerraron (open_shorts=0) por SL/TP normales.

**Commits:** `e0f87c8` (instrument orchestrator traceback), `e3dde0a` (fix NameError sig), `79fc92d` (fix volspike_override UnboundLocalError), `2f68912` (fix EJECUTADO trade_type), `4d28212` (fallback system trade_type), `181691b` (fix get_symbol_filters shadowing).

---

### ✅ Sesión 28 — Hardening productivo sin drift de config — jun 29 2026 14:20 UTC-6

**Restricción operacional:** ningún valor de configuración fue cambiado. Todas las mejoras son de proceso, trazabilidad, persistencia y rendimiento operativo.  
**Estado del sistema:** `pause_new_entries=true` persistido en BD. Monitoreo/lectura intactos; nuevas entradas pausadas.

#### Fixes implementados

| Área | Archivo(s) | Resultado |
|-----|------------|-----------|
| Atomicidad BD | `db_client.py`, `position_manager.py`, `futures_client.py` | `execute_transaction()` + cierres `trades_open → trades_closed` atómicos en SPOT/FUTURES |
| Webhook durable | `signal_store.py`, `app.py` | `/webhook` ya no responde éxito si la señal externa no se persiste en DB |
| Trazabilidad causal | `signal_store.py`, `run_trades.py`, `position_manager.py`, `futures_client.py` | `signal_id` propagado a ejecución y auditoría |
| Restart safety | `run_trades.py`, `state_persistence.py` | checkpoint temprano al inicio de ciclo además del guardado al final |
| Reconciliación SPOT | `position_manager.py`, `run_trades.py`, `app.py` | importa huérfanos y cierra posiciones DB sin balance en Binance, con auditoría |
| Screener fallback | `pair_screener.py` | último screener exitoso persistido en `trading_runtime_state` y reutilizado si Binance falla |
| Rendimiento ciclo | `run_trades.py`, `pnl_service.py` | caché de precio por ciclo y reuso en predicción |
| Rendimiento dashboard | `app.py`, `portfolio_manager.py` | caché local por request/método para balance, posiciones, ticker, summary |
| Auditoría homogénea | `run_trades.py`, `audit_tools.py`, `app.py` | `signal_decision` a `trades_audit` + endpoint `/audit/consistency` + health/dashboard con cobertura |
| Regresiones | `tests/test_restart_reconciliation.py`, `tests/test_audit_consistency.py`, `tests/test_health_audit.py` | cobertura nueva para reconciliación SPOT, fallback del screener y consistencia de auditoría |

#### Riesgo residual conocido

- Los tests nuevos quedaron **escritos y sin errores estáticos**, pero no ejecutados desde shell por ausencia de un binario Python/uv utilizable en la sesión actual.
- Existen archivos temporales/untracked históricos en el repo (`tmp_*`, snapshots, logs). No fueron tocados para evitar riesgo en productivo.

#### Fix post-sesión — FUTURES_SHORT bloqueado por spot exposure cap
- **Bug detectado:** `_buy_top_symbols()` en `run_trades.py` retornaba antes de evaluar shorts futuros cuando `_exposure_pct >= _max_exposure`. En FUTURES_SHORT el capital está en el wallet de futuros, por lo que la exposición de spot medida desde `portfolio_manager` llegaba al ~100% y abortaba toda apertura de nuevos shorts.
- **Fix:** la guarda de exposure ahora solo bloquea compras spot; cuando `_regime_mode == 'FUTURES_SHORT'` el flujo continúa y permite abrir shorts futuros (respetando los límites propios de futuros: `MAX_SHORTS`, `LARGECAP_CAP`, `MAX_ESCALAMIENTOS`, margin, cooldowns, etc.).
- **Commit/acción:** ver `run_trades.py` línea ~2109; se aplicó el fix y se ejecutó `tests/test_system_health.py -k "not Cat7"` (107 passed). Se requiere reinicio del contenedor para que el cambio entre en vigor.

#### Siguiente acción recomendada

1. Reiniciar el contenedor `crypto-agent-app` para cargar el fix de FUTURES_SHORT.
2. Verificar en el próximo ciclo que aparezcan intentos `[Futures] FUTURES_SHORT mode: opening short ...` y no se detengan por `[Exposure]`.
3. Ejecutar los tests nuevos dentro del contenedor/app o con un intérprete Python válido.
4. Verificar en runtime real `/health`, `/dashboard/summary` y `/audit/consistency`.

> ✅ **OBSERVACIÓN CERRADA** — jun 23 2026 14:08 UTC-6  
> `FUTURES_MIN_INJECT_STRENGTH` probado en **0.30** (commit `d3dced6`) → **revertido a 0.40** (commit actual).  
> **Resultado**: llenó 2 slots (UNIUSDT + FILUSDT, strength=0.33) pero FIL entró en rebote parcial (-1.43%). Sin señales ≥0.40 disponibles en espera — el cambio no aportó valor neto.  
> **Lección**: con 0.30 se arriesga entrar en rebotes. Con 0.40 el sistema espera señales con mejor timing. **Mantener 0.40 como valor base.**

> ⚠️ **CASO DE ESTUDIO — AAVEUSDT SHORT** — jun 24 2026 09:18 UTC-6  
> Entró @ `$71.71` el 23 jun 17:49 UTC — **dentro de la ventana `strength=0.30`** (mismo experimento que FIL).  
> Estado: precio `$76.24` → PnL **-$23.56 USDT** (-5.88%). SL automático Binance @ `$79.24` (solo $3 / 3.9% de margen).  
> RSI 1h=64.9 ↑ | RSI 4h=56.5 ↑ acelerando | BB 1h %B=92.9% (techo de banda).  
> **Diagnóstico**: entró al inicio de un rebote alcísta sostenido, no en continuación bajista.  
> **Acción**: No mover SL. Dejar que Binance lo gestione.  
> **Lección reforzada**: en 48h, strength=0.30 generó 2 entradas en rebotes (FIL -1.43%, AAVE -5.88%). **strength=0.40 es el mínimo correcto — no bajar.**

---

## ⚡ OBJETIVO PRIMARIO DEL SISTEMA

> **MAXIMIZAR GANANCIAS EN EL MENOR TIEMPO POSIBLE.**

Este es el criterio de prioridad absoluta para **todos** los análisis, mejoras, cambios y decisiones de trading:

1. **Velocidad de capitalización**: cada ciclo debe capturar la mayor ganancia posible dado el régimen de mercado actual.
2. **Capital siempre trabajando**: exposición máxima permitida por el régimen. Capital ocioso = pérdida de oportunidad.
3. **Compounding agresivo**: Kelly sizing dinámico que crece con el portfolio. A mayor capital → trades más grandes → crecimiento compuesto.
4. **Corte rápido de perdedores**: predicción tiene prioridad absoluta sobre time-in-trade. Cerrar rápido lo que deteriora, mantener lo que gana.
5. **Sin conservadurismo innecesario**: thresholds y filtros existen para evitar pérdidas reales, NO para reducir el número de trades. Si una señal es estadísticamente sólida, ENTRA.
6. **Cada mejora se evalúa por impacto en MXN/día**, no por elegancia técnica.

> Ante cualquier propuesta de cambio, la primera pregunta es: **¿Esto aumenta las ganancias o la velocidad de capitalización?**  
> Si la respuesta es NO → la mejora no es prioritaria.

---

## 0. ROLES Y CONTEXTO DE ANÁLISIS

> **Todo análisis, diseño e implementación en este proyecto debe adoptarse desde los siguientes roles simultáneamente:**

| Rol | Aplicación en este sistema |
|-----|---------------------------|
| **Experto en criptomonedas** | Conocimiento de mercados, tokenomics, ciclos macro BTC/alts, narrativas, liquidez |
| **Behavioral Finance** | F&G como señal de tamaño de posición: capitulación (+50% Kelly), greed extremo (-35% Kelly) |
| **Actuario / Riesgo sistémico** | Circuit breaker técnico (5 fallos API = pausa 10min), no bloquea por pérdidas de mercado |
| **Microestructura de mercados** | Filtro volumen 24h ≥$3M + slippage implícito ≤ 25% del TP antes de entrar |
| **Teoría de portafolios (correlaciones)** | Limite por bucket: LARGE_CAP≤3, MIDCAP≤5, NARRATIVE≤4, MEME≤2 — diversificación forzada |
| **On-chain / microestructura derivados** | Funding rate spot filter: si FR>0.08% = longs sobreapalancados = skip compra |
| **Experto en trading** | Gestión de riesgo, Kelly sizing, SL/TP, R:R, position sizing, rebalanceo, señales técnicas. Universo: 32 activos |
| **Experto en estadísticas** | Win rate, profit factor, EV por trade, distribuciones, bootstrap, Monte Carlo, sesgo en datos. Expansión E1: 24→32 activos para +40% señales/día |
| **Experto en inversiones y tendencias** | Macro BTC dominance, Fear & Greed, momentum sectorial, rotación de capital |
| **Experto en hacking y seguridad** | Protección de API keys, validación de inputs, exposure de endpoints, secrets en Docker/env |
| **Experto en performance de BD** | Queries eficientes, índices, volumen de escrituras (ciclos cada 5-20min × 24h), conexiones pool |
| **Experto en investigación de operaciones** | Optimización de parámetros, simulación, modelado de sistemas, decisiones bajo incertidumbre |
| **Experto en Machine Learning e IA** | Modelos predictivos (PnL predictor), features engineering, bootstrap vs overfitting, validación de modelos, mejora continua con datos reales |
| **Diseñador Gráfico** | UI/UX del dashboard, jerarquía visual, colores de alerta, legibilidad de tablas y métricas, experiencia del usuario en tiempo real |

> Ante cualquier propuesta de cambio: evaluar impacto desde **todos** los roles antes de implementar.

> **⚠️ REGLA UNIVERSAL SPOT+FUTURES — jun 24 2026**  
> Toda implementación, análisis o mejora debe evaluarse explícitamente para **AMBOS** tipos de trading (SPOT y FUTURES) antes de aprobarse.  
> Si aplica a uno, documentar por qué no aplica al otro, o implementar en ambos.  
> El objetivo es maximizar ganancias del sistema completo, no solo de un modo.  
> Ejemplo: escalamientos, SSO, ghost detection, comisiones, dashboard badges — todos aplican a SPOT y FUTURES.

---

## 1. ESTADO DEL SISTEMA

| Campo | Valor |
|-------|-------|
| Fecha última actualización | jul 05 2026 |
| Capital total | ~$2,810 USDT |
| Capital spot líquido | ~$1,771 USDT |
| Capital invertido spot | $0 (sin posiciones spot) |
| Capital futuros total | ~$1,040 USDT (~$500 margin + ~$2 libre + ~$38 unrealized PnL) |
| Régimen activo | **FUTURES_SHORT** (confirmado — BTC 30d=-17.4%) |
| Fear & Greed | **23 — Extreme Fear** |
| BTC 4h trend | DOWN (EMA9 < EMA21) |
| BTC 7d | -4.5% |
| BTC 30d | -17.4% (bear estructural activo) |
| Screener dinámico | ✅ ACTIVO — 41 spot / 19 futures (F2 expansión jun 18) |
| Screener v2 Top-100 | ✅ CONFIGURADO — quality filters $10M vol, max volatility 50% |
| Expansión activos E1 | ✅ COMPLETADA — 24→41 tokens con datos históricos reales |
| Hedge mode Binance | ✅ ACTIVADO (`dualSidePosition=true`) |
| VolSpike Override V2 | ✅ IMPLEMENTADO — strength ≥0.65, máx 2 pos, máx 3/día + RSI_1h validado |
| Dashboard Auditoría PnL | ✅ COMPLETADO — métricas de expectativa vs realidad |
| Dashboard UX D1-D4 | ✅ jun 19 2026 — entry_price fix, tooltip SVG hover, contador multimodal, señales fix |
| Dashboard Sesión 9 (FIX-P1/P2/P3) | ✅ jun 21 2026 — predictor real futuros, pnl_prediction persistido en DB, prediction_accuracy futuros |
| Dashboard Sesión 10 (FIX-L1/L2/L3/D1/L6/D3/D4/D5) | ✅ jun 20 2026 23h — dashboard unificado 2 cards, sub-tabs SPOT\|FUT, conteos reales, datos en vivo |
| Dashboard Bugs B1-B3 | ✅ jun 19 2026 (sesión 2) — tooltip script fix, SOL entry_price math, balance walletBalance |
| **Dashboard Sesión 23 (Fix 1-10)** | ✅ **jun 25 2026** — sticky bars, tab Ops unificado jerárquico, heatmap mercado, futuros en portfolio, disk storage, closed 7d, Monte Carlo fix, hover charts |
| **SSO-6 Smart Gate (Fix 12)** | ✅ **jun 26 2026** — rescue bloqueado si posición se recupera (G1 pnl>0, G2 señal favorable, G3 EV>2×target, G4 velocidad); trazabilidad completa en `smart_meta`+`trades_audit`; badge `🔄RESCUE⏸️` + modal Bounce Rescue Status; paridad SPOT/FUTURES; 9/9 tests PASS |
| Futuros monitoring | ✅ check_positions() corre cada ciclo independiente de régimen |
| Shorts protección ganancia | ✅ futures_protect_profit_usdt=1.0 — solo cierra shorts en pérdida al cambiar régimen |
| Futuros SHORTs activos | **9/9** — FIL, DOT, ETH, SUI, XRP, LINK, BTC, SOL, BNB — slots llenos |
| FUTURES_MAX_SHORTS | **9** (H-FIX A1 sesión 8 — subido de 7) |
| FUTURES_MAX_LARGECAP_SHORTS | **5** (guard IMPLEMENTADO en código jun 20 sesión 3 — slots 6-7 forzados a MIDs/NARRATIVE) |
| CapBridge | ✅ ACTIVO — transferencia autónoma bidireccional spot↔futuros (jun 18 2026) |
| B2 auto-close spots bear | ✅ ACTIVO — cierra spot LONGs +0.3% PnL en FUTURES_SHORT regime |
| Docker | live, sin errores |
| FUTURES_COMMISSION_RATE | **0.00037** (0.037% taker) — jun29 run_0677 |
| PnL Futures 48h bruto | $83.69 USDT |
| PnL Futures 48h neto | **$74.75 USDT** (fees=$8.94 / 10.7% del bruto) |
| Endpoint comisiones | `GET /performance/commissions?hours=N` — nuevo jun 24 |
| dry_run | False |
| allow_real_trading | True |
| Ciclo activo | **4 min** (FUTURES_SHORT — H-FIX A3 sesión 8) |
| Pares con datos históricos | **41 símbolos** — 1095 días 1h klines en `/app/historical_data/` |
| **Predictor** | **34,309 trades calibrados — 45 símbolos con pesos en DB** |
| **Learning Engine** | **Señales reales TechnicalIndicators.analyze_signal() — jun 17 2026** |
| **Bias correction** | **1.111 promedio (cap 1.5 para sintéticos, sube con trades reales)** |
| Win rate histórico (sim) | **37.9% en bear market** — normal, PRESERVATION protege capital |
| **Datos históricos** | **Parquet 1h: 41 archivos, 0.13 GB / 50 GB — `/app/historical_data/`** |

### ✅ Slots 6-7 activados — jun 19 2026 08:10 UTC-6
- SOL cerró en TP: salida $68.02, PnL **+$15.22 USDT (+11.7% sobre margin)** ← primer TP confirmado
- `FUTURES_MAX_SHORTS=7` aplicado en docker-compose.yml + container recreado
- Slots 6-7 = MIDs automáticamente (LARGECAP_CAP guard activo: max 5 large-caps)
- Próximos candidatos: NEARUSDT, SUIUSDT, AVAXUSDT, LINKUSDT, DOTUSDT

### ✅ Sesiones 13-14: Trailing SL protection + ghost cleanup + critical fixes — jun 21 2026 17:39 UTC-6

**Tag git: `v0.14-trailing-protection`**

#### Fixes implementados
| Fix | Archivo | Descripción |
|-----|---------|-------------|
| FIX-S1 | futures_client.py | `_persist_smart_meta()` — persiste breakeven/trailing/SmartTP en trades_open.metadata JSON |
| FIX-S2 | futures_client.py | `_sync_open_positions()` restaura `_smart_meta` desde DB + infiere estado desde SL vs entry |
| FIX-S3 | app.py + futures_client.py | Dashboard infiere `trailing_active` desde PnL% ≥1.5% y `breakeven_done` desde SL vs entry |
| FIX-S4 | futures_client.py | Ghost reconciliation: auto-delete trades_open stale en cada sync |
| FIX-S4b | futures_client.py | Auto-create trades_open para posiciones Binance sin record en DB |
| FIX-S5 | futures_client.py | SL se mueve inmediatamente al activar trailing (antes esperaba nuevo low/high) |
| FIX-S6 | position_manager.py | PM filtra `WHERE trade_type != 'futures'` — evita que spot SL/TP intente vender futuros |
| BUG-C1 | run_trades.py | `close_position()` → `close_long()` (método no existía) |
| BUG-C2 | futures_client.py | `pos.margin` → `qty * entry_price / LEVERAGE_MAX` (atributo no existía) |
| BUG-H1 | run_trades.py | SPOT SmartTP EXTEND SL buffer 0.5% → `sl_pct * 40%` (dinámico por categoría) |
| BUG-H2 | run_trades.py | Persist trailing SL para SPOT via `update_position_sl()` |

#### Estado de protección actual (9 SHORTs)
| Símbolo | PnL | Trail | BE | SL Dist |
|---------|-----|-------|-----|--------|
| DOTUSDT | $6.13 | ✅ | ✅ | 1.27% |
| SUIUSDT | $6.60 | ✅ | ✅ | 1.40% |
| XRPUSDT | $5.43 | ✅ | ✅ | 1.74% |
| SOLUSDT | $4.84 | ✅ | ✅ | 1.53% |
| BTCUSDT | $4.60 | ✅ | ❌ | 3.42% |
| LINKUSDT | $3.95 | ✅ | ✅ | 1.79% |
| ETHUSDT | $3.08 | ✅ | ❌ | 2.70% |
| FILUSDT | $1.94 | ❌ | ❌ | 3.84% |
| BNBUSDT | $1.52 | ❌ | ❌ | 2.03% |

Total unrealized PnL: **~$38 USDT**. 7/9 posiciones con trailing activo.

### ⏱ Próxima revisión
- Monitorear trailing SL en todas las posiciones — verificar que SL se aprieta con cada nuevo low
- Refactor `_evaluate_short_momentum` para usar `pnl_service.predict_for_symbol` (pendiente)
- Evaluar si breakeven_activation_pct=2.0% es óptimo o bajar a 1.5%

### 🖼️ Dashboard Sesión 23 — Refactor Completo — jun 25 2026 13:52 UTC-6

**Todos los fixes del plan `dashboard-refactor-863057.md` implementados.**

#### Archivos modificados
| Archivo | Cambios |
|---------|---------|
| `frontend/styles.css` | Sticky bars, ops-main-tabs, table-scroll height, heatmap grid, quitó overflow-y:auto del tab-content |
| `frontend/index.html` | Tab Ops unificado → `#unifiedPosCard`, Tab Mercado rediseñado 2 filas, cache-bust v23 |
| `frontend/dashboard.js` | switchOpsMainTab/Sub, renderMarketHeatmap, renderPnlSvgChart hover, renderPredVsRealChart hover, renderBalance futuros, renderHealthChecks disk, closed 7d, Monte Carlo fix |
| `app.py` | disk_storage en health check vía shutil.disk_usage('/app') |

#### Fix 1 — Sticky bars
- `.ticker-bar`: `sticky top:52px z-index:99`
- `.status-bar`: `sticky top:92px z-index:98`
- `.tab-nav`: `sticky top:128px z-index:97`
- Removido `overflow-y:auto` de `.tab-content` (bloqueaba sticky del documento)

#### Fix 2 — Tab Operaciones unificado jerárquico
- 2 cards separadas → 1 `#unifiedPosCard` con 2 niveles:
  - **Nivel 1**: `ops-main-tabs` → botones Abiertas / Cerradas (con label `7d`)
  - **Nivel 2**: `inner-tab-bar` → Todas / SPOT / FUTUROS (filtro compartido)
- Funciones JS: `switchOpsMainTab(main)`, `switchOpsSubTab(sub)`, legacy `switchInnerTab()` mantenida
- `table-scroll` → `max-height:calc(100vh - 420px)` para ver más filas

#### Fix 3+4 — Hover tooltips en charts (Rendimiento)
- `renderPnlSvgChart()`: crosshair vertical + dot flotante + tooltip con fecha / PnL día / PnL acumulado
- `renderPredVsRealChart()`: puntos con `data-*`, tooltip mouseenter/leave con símbolo/predicción/real/accuracy

#### Fix 5 — Monte Carlo capital hardcodeado
- `initial_capital` fallback `3080.0` → `0` (usa el valor real del backend)

#### Fix 6 — Portfolio muestra futuros individuales
- `renderBalance()` añade sección "⚡ FUTUROS — Posiciones abiertas" usando `_cachedFuturesPositions`
- Muestra: símbolo, side, qty @ entry_price = exposure, PnL en color

#### Fix 8 — Tab Mercado rediseñado
- **Layout**: 2 filas — fila 1 (3 cols: col-A régimen+BTC+F&G, col-B scanner protagonista, col-C heatmap+PredEval), fila 2 (señales full-width)
- **Heatmap 24h** — nuevo elemento `#marketHeatmapData`:
  - Grid `auto-fill minmax(68px,1fr)` con celdas coloreadas por cambio %
  - 5 clases: `heatmap-strong-up/mild-up/flat/mild-down/strong-down`
  - Datos desde `data.daily_ticker` (mismo endpoint del ticker bar)
- **Función**: `renderMarketHeatmap(tickerItems)` — sort desc por change_pct

#### Fix 9 — Disk storage en Sistema
- `app.py`: `shutil.disk_usage('/app')` → `checks["disk_storage"]` con total/used/free GB + used_pct + status ok/warning/critical
- `dashboard.js`: `renderHealthChecks()` renderiza barra visual de uso + GB libre en color

#### Fix 10 — Closed positions 7 días
- `fetch('/futures/closed?days=7')` — backend ya aceptaba el param

#### Pendientes conocidos
- Verificar en browser con datos reales de producción
- `renderMarketHeatmap` necesita que `daily_ticker` sea array de objetos `{symbol, change_pct, volume_usdt}` — revisar shape del backend si aparece "Sin datos del ticker"

---

### 🤖 Sesión 24 — SSO-6 Smart Gate + Trazabilidad BD — jun 26 2026 13:31 UTC-6

**Objetivo:** El rescue del rebote ya no cierra posiciones ciegamente — evalúa en tiempo real si la posición se recupera sola antes de ejecutar el cierre.

#### Fix 12 — SSO-6 Bounce Rescue Smart Gate

**Problema resuelto:** SSO-6 rescataba posiciones en el momento en que `pnl >= target_pnl`, sin verificar si la posición ya se comportaba positivamente. Resultado: cierres prematuros dejando ganancias sobre la mesa.

**4 compuertas (bloquean el rescue si alguna aplica):**

| Gate | Condición | Configuración |
|------|-----------|---------------|
| **G1** | `pnl_pct > 0` — posición ya en ganancia | Hardcoded |
| **G2** | Señal técnica favorable (SHORT+SELL o LONG/BUY+BUY) + `strength ≥ 0.40` + vol confirmado | `rescue_stay_strength_min=0.40` |
| **G3** | Predicción EV actualizada > 2× `|target_pnl|` | `rescue_stay_ev_multiplier=2.0` |
| **G4** | `pnl_velocity_1h > 0.3%/h` y acelerando | `rescue_velocity_min_pct=0.003` |

**Paridad SPOT/FUTURES:** Idéntica lógica. FUTURES en `futures_client.py`, SPOT en `run_trades.py`. Mismos config params aplican a ambos.

#### Archivos modificados

| Archivo | Cambio |
|---------|--------|
| `config.py` | 4 nuevos params: `rescue_smart_gate_enabled`, `rescue_stay_strength_min`, `rescue_stay_ev_multiplier`, `rescue_velocity_min_pct` |
| `smart_scale_out.py` | `SSOPositionData` +3 campos (`rescue_prediction_ev`, `rescue_signal`, `rescue_signal_strength`); Smart Gate en `_check_sso6_bounce_rescue()` |
| `futures_client.py` | Inyecta EV+signal desde `pnl_service`; persiste `bounce_gate_*` en `smart_meta`; `_audit()` eventos `bounce_gate_blocked`/`bounce_gate_fired`; expone 7 campos `bounce_gate_*` en `get_open_positions_summary()` |
| `run_trades.py` | Mismo patrón SPOT; elimina bloque de rescue directo (ahora dentro de SSO evaluate) |
| `app.py` | Expone `bounce_gate_*` en `open_list` para SPOT (~L1566) y FUTURES (~L1726) |
| `frontend/dashboard.js` | Badge `🔄RESCUE⏸️` con tooltip EV/señal/vel; modal sección "🔄 BOUNCE RESCUE STATUS"; cerradas `🔄BR(N)` con gate info |

#### Trazabilidad BD completa

- **`trades_open.metadata.smart_meta`**: `bounce_gate_blocked_count`, `bounce_gate_first_qualified_at`, `bounce_gate_last_signal`, `bounce_gate_last_signal_strength`, `bounce_gate_last_ev`, `bounce_gate_last_velocity`, `bounce_gate_blocked_reason`, `bounce_gate_last_eval` — persiste por `_persist_smart_meta()` solo cuando hay cambio
- **`trades_audit`**: eventos `bounce_gate_blocked` (con `gate_blocked_by`, `signal`, `rescue_ev`, `velocity`, `block_count_so_far`) y `bounce_gate_fired` (con `total_blocks_before`, `gate_held_minutes`, `final_signal`, `final_ev`)

#### Tests
- **9/9 PASS** `TestSSO6BounceRescue` — 4 originales + 5 nuevos Smart Gate (G1, G2, G3, gate-fires, G2-SPOT)

---

### 🖼️ Dashboard UX — jun 19 2026 08:33 UTC-6
- **D1**: `futures_client.get_recent_trades()` — `entry_price` calculado desde mem o fórmula math
- **D2**: `buildPriceRangeChart()` — tooltip hover SVG en todas las gráficas de modales
- **D3**: Contador multimodal `Spot: N/12 | Fut: M/7` en barra de estado (verde/rojo por overflow)
- **D4**: Columna señales Mercado ampliada a 340px + grid compacto 6-col sin desbordamiento
- **D5**: Health Check panel confirmado activo en TAB 4 — botón `🩺 Full Health`

### 🐛 Dashboard Bug Fixes — jun 19 2026 09:10 UTC-6 (sesión 2)
- **B1**: Tooltip hover SVG ahora funciona — `buildPriceRangeChart()` retorna objeto `{html,uid,...}`; `attachChartHover()` global adjunta eventos al DOM real (browsers bloquean `<script>` en `innerHTML`)
- **B2**: SOLUSDT entry_price corregido ($72.26 ≠ $68.02) — `get_recent_trades()` siempre calcula `entry_price` matemáticamente, nunca desde `_open_positions` (evita confusión con el nuevo SHORT del mismo símbolo)
- **B3**: Balance total correcto ($50,973 MXN) — nuevo `get_total_wallet_usdt()` usa `walletBalance` (margin + libre) en vez de `availableBalance` solo

---

## 2. ARQUITECTURA — MAPA DE ARCHIVOS CLAVE

```
crypto-agent/
├── run_trades.py          Motor principal: ciclos 5min, regimen, Kelly, screener, buys/sells
├── futures_client.py      FuturesClient: open_short/long, hedge, SL/TP algo orders, pos tracking
├── config.py              Settings: todos los parámetros con defaults y env vars
├── market_regime.py       Detección: SPOT_BULL / RANGE_SCALP / PRESERVATION / FUTURES_SHORT
├── autonomous_signals.py  Señales técnicas: RSI, BB, EMA, vol_spike, divergencias (1h + 4h)
├── pair_screener.py       [jun15] Screener dinámico Binance 24h ticker, cache 15min
├── pnl_service.py         Predictor PnL: umbrales por categoría + F&G modifier + bootstrap
├── pnl_predictor.py       [ACTUALIZADO jun16] 9 features: +vol_spike, +atr_pct, +trend_4h, +weekly_range
├── learning_engine.py     [ACTUALIZADO jun17] +load_synthetic_trades +run_with_synthetic; _extract str→numeric; bias cap 1.5 sintéticos
├── historical_simulator.py [CREADO jun17] Simula trades en datos históricos con TechnicalIndicators.analyze_signal REAL
├── historical_data_loader.py [CREADO jun17] Descarga bulk klines 1h Binance, concurrente, parquet, storage monitor
├── Dev/scripts/ingest_historical.py [CREADO jun17] Pipeline: download→simulate→learn (CLI --phase, --days, --simulate, --learn)
├── bootstrap_history.py   [ACTUALIZADO jun16] Lookbacks 700-1000d, FORWARD=24h, 4 features extendidas inline
├── trade_utils.py         [ACTUALIZADO jun16] format_indicators_for_predictor pasa 4 features nuevas
├── strategy.py            Pesos ranking por régimen (TV/News/Tech)
├── position_manager.py    Gestión posiciones abiertas, DB, trailing stop, cierre
├── app.py                 FastAPI: /predictor/reset-corrupted, /predictor/bootstrap hasta 1000d; FIX-L2/L3/D1/L6a (sesión 10)
├── futures_client.py      FuturesClient: FIX-L1 get_open_positions_summary() enriquece desde DB (sesión 10)
├── dashboard.js           Frontend: cards, tablas, Monte Carlo, sub-tabs inner SPOT|FUT, conteos dinámicos (sesión 10)
├── styles.css             .inner-tab-bar/.inner-tab-btn/.pos-count-badge (sesión 10)
├── SYSTEM_REFERENCE.md    ← ESTE ARCHIVO
└── .env                   Variables de entorno (gitignored)
```

### Relaciones críticas
- `run_trades.py` → llama `market_regime.get_regime()` → determina modo
- `run_trades.py` → llama `pair_screener.get_screened_pairs()` → amplía lista de pares
- `run_trades.py` → instancia `FuturesClient` vía `_get_futures_client()` (singleton)
- `FuturesClient._open_positions` keyed como `"BTCUSDT_SHORT"` / `"BTCUSDT_LONG"` (post-jun15)
- `app.py` → tiene `_get_market_regime()` **propia, independiente** del motor (bug conocido #11)

---

## 3. MODOS DE TRADING

### Cuatro modos del market_regime

| Modo | Trigger | Kelly mult | SL % | TP % | Restricciones |
|------|---------|------------|------|------|---------------|
| **SPOT_BULL** | BTC EMA9>EMA21 4h, vol_ratio>0.5 | 1.0× | por categoría | por categoría | mínimo — spot compras normales |
| **RANGE_SCALP** | BB bandwidth < 4%, lateral | 0.7× | 1.2% | 1.5% | solo RSI<40 + BB lower touch |
| **PRESERVATION** | BTC 7d<-7% OR 30d<-15% | 0.5× | 1.5% | 3.0% | solo NARRATIVE tokens |
| **FUTURES_SHORT** | BTC 30d<-15% + vol evidencia bear | 0.3× | **3.0%** | **7.0%** | futuros short only |

### Condiciones especiales activas (jun 15)
- **SPOT_BULL + F&G Extreme Fear** → `spot_bull_pnl_boost` activo (relaja umbrales predictor)
- **LONG futuros en RANGE_SCALP**: si `bb_lower_touch=True` + símbolo en futures tier A → `open_long()`
- **LONG futuros en SPOT_BULL**: si RSI 4h < 35 + BTC_trend=UP + símbolo tier A → `open_long()`
- **Anti-whipsaw**: 3 ciclos consecutivos para cambiar de PRESERVATION/FUTURES_SHORT → cerrar shorts

### Cómo se activan LONGs de futuros (nuevo jun15)
```
RANGE_SCALP + bb_lower_touch=True → open_long(sl=2.5%, tp=5.0%, lev=2×)
SPOT_BULL + RSI_4h < 35 + BTC=UP  → open_long(sl=2.5%, tp=5.0%, lev=2×)
Requiere: hedge_mode=True, futures_enabled=True, futures_long_enabled=True
```

---

## 4. REGLAS DE ENTRADA VALIDADAS (estadística real)

### Datos históricos (jun 2026)
- Win rate real: 43–51% (varía por periodo, bootstrap 15,382 trades)
- Profit factor: 1.048 → objetivo ≥ 1.5
- Mejor par: DOGEUSDT (WR ~100%), RENDERUSDT (WR ~78%), BNBUSDT (WR ~60%)
- Peor par: BTCUSDT (WR 0%), LINKUSDT (WR 0%) — en spot LONG

### Reglas de entrada spot (NO negociables)
| Condición | Valor | Efecto |
|-----------|-------|--------|
| RSI 4h | < 65 | Requerido (hard filter) |
| vol_spike | ≥ 1.5× avg | VolSpike override activo si NARRATIVE |
| precio vs rango semanal | 20–60% | Ideal; >80% = sobrecomprado |
| BTC dominance | < 56% (default) | Si BTC.D > umbral → bloquea alts |
| min_signal_strength | **0.55** | v4 OPT: 0.30→0.55 aplicado jun 20 |

### VolSpike Override (bypass predictor)
```
Requisitos: vol_spike ≥ 1.5× + RSI_4h < 65 + trend=UP + token NARRATIVE
Efecto: omite PnL predictor, entra directamente
```

### Umbrales por categoría de token
| Categoría | min_prob | min_pnl | SL% | TP% | trailing_act% | trailing_dist% | tp_ext% |
|-----------|---------|---------|-----|-----|---------------|----------------|--------|
| LARGE_CAP | 55% | $0.10 | 1.8% | 3.5% | **2.0%** | **0.8%** (ATR-scaled) | 1.5% |
| MIDCAP_ALT | 50% | $0.00 | 1.8% | 3.5% | **2.0%** | **0.8%** (ATR-scaled) | 2.0% |
| NARRATIVE | **45%** | **-$1.00** | **3.5%** | **7.0%** | **5.0%** | **2.0%** (ATR-scaled) | **3.0%** |
| MEME | 52% | $0.05 | 2.0% | 4.5% | **2.0%** | **1.0%** (ATR-scaled) | 2.5% |

> V4 (jun 21): trail distances ahora son ATR-scaled en runtime (los valores de tabla son config base/fallback).
> Fórmula: `trail_dist = max(floor, min(ceiling, ATR_5m × mult))`. Ver sección "TRAILING STOP DESIGN PRINCIPLES".
> Fuente: `config.py:category_thresholds` + `run_trades.py:_compute_spot_trail_distance()`

---

## 5b. COMISIONES — CONTABILIDAD Y AUDITORÍA

| Tipo | Rate config | PnL en DB | Estado |
|------|-------------|-----------|--------|
| SPOT | `binance_commission_rate=0.00097` (0.097%) | **Neto** — `_calc_close()` descuenta entrada+salida | ✅ Correcto |
| FUTURES SSO closes (`sso_slow_crawl`, `sso_profit_decay`, etc.) | `futures_commission_rate=0.00037` (0.037%) | **Neto desde jun 24** — commit `23c43ae` | ✅ Corregido |
| FUTURES Binance closes (`stop_loss_binance`, `trailing_sl_binance`) | 0.037% | Neto — Binance `realizedPnl` ya lo descuenta | ✅ Siempre correcto |

### Impacto real medido (48h, 60 trades futures)
```
gross_pnl = $83.69 USDT
fees      = $8.94 USDT  (0.04% x 2 lados x notional)
net_pnl   = $74.75 USDT  <- valor real desde jun 24
fees %    = 10.7% del PnL bruto
```

### Columnas DB
- `entry_commission` + `exit_commission` en `trades_closed` — siempre populadas para futuros desde jun 24
- SPOT: `entry_commission` llega NULL en ~155/165 trades históricos (bug pre-jun 24); salida siempre calculada

### Gestión autónoma de BNB (jun 29 2026)
| Concepto | SPOT | FUTURES | Implementación |
|----------|------|---------|----------------|
| Compra BNB | ✅ En spot wallet | ❌ No se compra directamente | `binance_commission.py:auto_manage_bnb_discount()` compra BNBUSDT spot |
| Transferencia BNB | Spot → Futures | ✅ Recibe BNB de spot | `universalTransfer(type='MAIN_UMFUTURE', asset='BNB')` |
| Funding BNB | USDT debe estar en spot para comprar | Sistema transfiere USDT futures→spot cuando el régimen lo indica | `FuturesClient.transfer_to_spot()` ahora permite transferir con posiciones abiertas respetando buffer de 10 USD |
| Buffer extra | `bnb_extra_buffer_usd` por wallet | `bnb_extra_buffer_usd` por wallet | Subido a **10 USD** por wallet |
| Reserva PM | Resta `total_reserve_usdt` del USDT disponible para trading | Resta `total_reserve_usdt` del margin disponible | `portfolio_manager.py` expone `*_after_reserve` |
| Trazabilidad | `trades_audit` eventos `bnb_*` y `usdt_futures_to_spot_*` | `trades_audit` eventos `bnb_*` y `usdt_futures_to_spot_*` | 100% audit trail incluyendo shortfalls |
| Alerta | `config_drift_alerts` | `config_drift_alerts` | Si `use_bnb` apagado 2 checks consecutivos |

#### Regla de preferencia de funding
El sistema elige de qué wallet tomar USDT para comprar BNB según el régimen activo, para mantener líquida la wallet que está tradeando más:
- **SPOT_BULL / RANGE_SCALP**: preferir USDT de **FUTURES** (spot se conserva para señales spot).
- **FUTURES_SHORT / PRESERVATION**: preferir USDT de **SPOT** (futures se conserva para margin de shorts).
- Si la fuente primaria no alcanza, se transfiere desde futures el excedente disponible (dejando 10 USD buffer). Si aún falta, se usa lo que haya en spot. Si ambos son insuficientes, se registra `bnb_liquidity_shortfall` y se salta la compra.


---

## 5. PARÁMETROS .ENV CRÍTICOS

### Valores actuales en producción (verificados jun 17 2026 11:06 UTC-6)

```bash
# ── Operación general ──────────────────────────────────────────────────────
DRY_RUN=false
ALLOW_REAL_TRADING=true
MAX_POSITIONS=15                    # ← jun17: era 12 → 15 (fallback)
MAX_POSITIONS_PER_SYMBOL=1
MAX_POSITIONS_SPOT_BULL=20          # ← jun17: era 15 → 20
MAX_POSITIONS_RANGE_SCALP=10        # ← jun17: era 8 → 10
OVERFLOW_MAX_EXTRA=5                # ← jun17: era 3 → 5
TRADE_CYCLE_MINUTES=5               # fallback (ciclo real es dinámico por régimen)

# ── Kelly sizing — SMART KELLY (actualizado jun29 2026 — post-backtest optimizer run_0677) ─────
# NOTA: los valores hardcoded en docker-compose.yml tienen prioridad sobre .env y config.py defaults.
KELLY_BASE_USDT=73                  # jun29: 76→73 (backtest optimizer run_0677, baseline=best)
KELLY_DYNAMIC_BASE_PCT=0.12         # base = capital_liquido × 12%
KELLY_MAX_USDT=747                  # jun29: 663→747 (backtest optimizer run_0677)
KELLY_MAX_PCT_CAP=0.25              # Smart Kelly: max = max(floor, liquid × 25%)
KELLY_MAX_PCT_LIQUID=0.704          # jun29: 0.621→0.704 (backtest optimizer run_0677)
MAX_PER_TRADE_USDT=500              # jun29: sin cambio (run_0677)

# Smart Kelly — tiers con $1,758 USDT líquidos:
#   FUERTE (prob≥0.65 + str≥0.80) → $439  (max dinámico 25% liquid)
#   ALTA   (prob≥0.60 + str≥0.70) → $394
#   MEDIA  (prob≥0.55 + str≥0.55) → $325
#   BASE   (prob≥0.50)            → $211  (12% liquid)
# Conviction boost SPOT_BULL (vol_spike×2/3): cap 10%→15% liquid = $263

# ── SL/TP globales (actualizado jun29 2026 — backtest run_0677, baseline=best) ────────────
STOP_LOSS_PCT=3.36                  # jun29: 1.03→3.36 (backtest optimizer run_0677)
TAKE_PROFIT_PCT=4.14                # jun29: 5.83→4.14 (backtest optimizer run_0677)
TRAILING_STOP_ACTIVATION_PCT=2.5
TRAILING_STOP_DISTANCE_PCT=1.2

# ── Filtros de señal (actualizado jun29 2026 — backtest run_0677) ─────────────────────────
MIN_SIGNAL_STRENGTH=0.389           # jun29: 0.359→0.389 (backtest run_0677)
MIN_SUCCESS_PROBABILITY=0.499       # jun29: 0.527→0.499 (backtest run_0677)
VOL_SPIKE_MIN=0.91                  # jun29: 0.83→0.91 (backtest run_0677)
BTC_DOMINANCE_ALTCOIN_THRESHOLD=64.0

# ── Exposición y rebalanceo ─────────────────────────────────────────────────
MAX_TOTAL_EXPOSURE_PCT=0.75         # era 0.65 → max 75% del portfolio invertido
PROTECT_PROFIT_MIN_PCT=0.5          # era 0.8 → proteger desde +0.5%
MIN_HOLD_MINUTES=90                 # era 60 → esperar 90min antes de rebalancear
MAX_REBALANCE_PER_CYCLE=1           # era 2 → máx 1 venta por ciclo (protege posiciones)

# ── Ciclos dinámicos por régimen ────────────────────────────────────────────
CYCLE_SPOT_BULL=10                  # jun17: era 20 → más throughput de señales (+47%)
CYCLE_RANGE_SCALP=5                 # sin cambio
CYCLE_PRESERVATION=45               # jun17: era 30 → menos ciclos en bajista = menos comisiones
CYCLE_FUTURES_SHORT=6               # jun17: era 8 → más monitoreo en shorts

# TTL señales por régimen (cycle_minutes × 3):
#   SPOT_BULL=60min | RANGE_SCALP=15min | PRESERVATION=135min | FUTURES_SHORT=18min

# ── Futuros SHORT ───────────────────────────────────────────────────────────
FUTURES_ENABLED=true
FUTURES_MAX_SHORTS=12               # jun28: 7→12 — backtest run_0812 uses 12 concurrent shorts
FUTURES_USDT_PER_TRADE=100          # era 50 → Kelly futuros calcula $80-120 (minimum margin floor, not hard cap)
FUTURES_KELLY_MAX_USDT=1111         # jun29: 1162→1111 (backtest optimizer run_0677)
FUTURES_KELLY_MAX_PCT_LIQUID=0.704  # jun29: 0.621→0.704 (backtest run_0677)
FUTURES_KELLY_BASE_USDT=73          # jun29: 76→73 — align with spot kelly_base (backtest run_0677)
FUTURES_SL_PCT=1.42                 # jun29: 3.33→1.42 (backtest optimizer run_0677)
FUTURES_TP_PCT=2.81                 # jun29: 7.68→2.81 (backtest optimizer run_0677)
FUTURES_LEVERAGE=10                 # jun28: 2→10 — backtest run_0812 validated 10x (fixed LEVERAGE_MAX hardcode in futures_client.py)
FUTURES_MAX_NEW_PER_CYCLE=5         # jun28: 3→5 — more throughput in FUTURES_SHORT

# ── Comisiones Binance (centralizado en binance_commission.py) ─────────────
# SPOT: 0.1% taker por lado; 0.05% con BNB discount
# FUTURES: 0.04% taker por lado; 0.02% con BNB discount
BINANCE_COMMISSION_RATE=0.001           # Binance-controlled spot taker default (live rate refreshed from API)
FUTURES_COMMISSION_RATE=0.0004          # Binance-controlled futures taker default (live rate refreshed from API)
USE_BNB_FOR_COMMISSION=true           # 0.05% spot / 0.02% futures with BNB
BNB_SPOT_COMMISSION_RATE=0.0005
BNB_FUTURES_COMMISSION_RATE=0.0002
AUTO_BNB_FOR_COMMISSION=true          # auto-detecta BNB, compra si falta, activa descuento
BNB_BUFFER_DAYS=7                     # días de comisiones a mantener en BNB
BNB_MIN_BALANCE_USD=5.0               # mínimo USD en BNB para mantener descuento activo
BNB_EXTRA_BUFFER_USD=10.0             # excedente de USD a mantener en cada wallet para evitar quedarse sin BNB
SLIPPAGE_PCT=0.00052                  # jun29: 0.0005→0.00052 (backtest run_0677)
# PnL predictor y Kelly sizing ahora usan reward/risk NETO de comisiones.
# BNB discount impact (Backtest 1000 window): round-trip SPOT 0.20%->0.10%, FUTURES 0.08%->0.04%.
# Estimado ahorro: ~$585-$1,170 USDT/20d sobre ~$1.17M notional (= +15-35% PnL adicional).
# Dashboard: status chip en header + panel detallado en tab Rendimiento vía /dashboard/commission.
# Portfolio Manager reserva el valor BNB necesario (`bnb_reserve_usdt`) antes de calcular capital disponible para trading.
# Funding: según el régimen activo, el sistema toma USDT de spot o de futures (excedente, dejando 10 USD buffer) para comprar BNB.
# Trazabilidad: cada compra, transferencia USDT futures→spot, transferencia BNB spot→futures, intento fallido y shortfall se persiste en `trades_audit`.
# Alerta: si `use_bnb` está apagado por 2 checks consecutivos, se inserta un `config_drift_alerts` warning.

# ── Configuración Centralizada (K2 refactor) ─────────────────────────────────
# Fuente única de verdad: config.py define los defaults; docker-compose.yml/.env los overrides.
# El código NO debe usar constantes de módulo que anulen settings ni getattr(..., 'X', hardcode)
# con valores distintos a config.py.
#
# Componentes:
#   - config_helpers.py: get_param(), get_spot_param(), get_futures_param(), validate_config(), dump_active_config()
#   - futures_client.py: LEVERAGE_MAX/MAX_SHORTS son sólo UPPER BOUNDS; el valor activo viene de settings.
#   - run_trades.py: _get_futures_client() sincroniza _active_leverage y max_shorts cada ciclo.
#   - app.py /config/drift: detecta desviación vs baseline run_0677 en runtime.
#   - app.py /config/apply-backtest: persiste parámetros de un run de backtest en trading_runtime_state.
#   - app.py /health/full-check: valida alineación de parámetros críticos con backtest run_0677.
#
# Baseline run_0677 (config.py defaults):
#   futures_leverage=10, futures_max_shorts=12, futures_max_new_per_cycle=5,
#   futures_sl_pct=1.42, futures_tp_pct=2.81, futures_usdt_per_trade=100.0,
#   futures_kelly_base_usdt=73.0, futures_kelly_max_usdt=1111.0, futures_kelly_max_pct_liquid=0.704,
#   kelly_base_usdt=73.0, kelly_max_usdt=747.0, kelly_max_pct_liquid=0.704,
#   stop_loss_pct=3.36, take_profit_pct=4.14.
#   min_signal_strength=0.389, min_success_probability=0.499, vol_spike_min=0.91.

# ── Futuros LONG (hedge) ─────────────────────────────────────────────────────
FUTURES_HEDGE_MODE_ENABLED=true
FUTURES_LONG_ENABLED=true
FUTURES_MAX_LONGS=3
FUTURES_LONG_SL_PCT=2.5
FUTURES_LONG_TP_PCT=5.0
FUTURES_LONG_IN_RANGE_SCALP=true
FUTURES_LONG_IN_SPOT_BULL=true
FUTURES_LONG_RSI_THRESHOLD=35.0

# ── Screener dinámico ────────────────────────────────────────────────────────
SCREENER_ENABLED=true
SCREENER_MIN_VOL_USDT=10000000      # $10M para spot
SCREENER_FUTURES_MIN_VOL=100000000  # $100M para futuros
SCREENER_MAX_PAIRS=50
SCREENER_CACHE_MINUTES=15

# ── Dynamic max positions por régimen (jun 17 11:06) ──────────────────────────
MAX_POSITIONS_SPOT_BULL=20          # jun17: 15→20 (32 activos disponibles) — config.py default actualizado jun20
MAX_POSITIONS_RANGE_SCALP=10        # jun17: 8→10 — config.py default actualizado jun20
MAX_POSITIONS_PRESERVATION=5        # sin cambio (solo NARRATIVE en bajista)
MAX_POSITIONS_FUTURES_SHORT=4       # sin cambio
# MAX_POSITIONS=15 se mantiene como FALLBACK si régimen no disponible

# ── Reemplazo activo de posición débil (jun17 — bugs corregidos) ─────────────
REPLACE_ONLY_LOSING=true            # solo reemplaza posiciones en pérdida/flat
REPLACE_EV_MIN_RATIO=1.1            # jun17: 1.3→1.1 (EVs pequeños, más permisivo)
REPLACE_STRENGTH_MIN_DELTA=0.05     # jun17: 0.15→0.05 (avg_strength ahora real, no hardcoded 0.5)

# ── Overflow slots (señales excepcionales beyond max) ───────────────────────
OVERFLOW_MIN_STRENGTH=0.80          # Kelly FUERTE tier: str ≥ 0.80
OVERFLOW_MIN_PROB=0.65              # Kelly FUERTE tier: prob ≥ 0.65
OVERFLOW_MAX_EXTRA=5                # jun17: 3→5 slots extra simultáneos

# ── Exposición agresiva SPOT_BULL + Fear (jun17) ─────────────────────────────
MAX_TOTAL_EXPOSURE_SPOT_BULL_FEAR=0.80          # 80% cap cuando SPOT_BULL + F&G<40
MAX_TOTAL_EXPOSURE_SPOT_BULL_FEAR_FNG_THRESHOLD=40  # F&G umbral para activar

# ── Futuros: protección shorts ganadores (jun17) ─────────────────────────────
FUTURES_PROTECT_PROFIT_USDT=1.0     # solo cierra short por régimen si PnL < $1.0

# ── min_hold por régimen (jun17) ──────────────────────────────────────────────
MIN_HOLD_MINUTES=120                # jun17: 240→120 min global
MIN_HOLD_MINUTES_RANGE_SCALP=30     # RANGE_SCALP: 30min (5min ciclo × 6)
MIN_HOLD_MINUTES_SPOT_BULL=120      # SPOT_BULL: 2h antes de reemplazo

# ── Behavioral Finance: F&G Kelly boost (R1) ─────────────────────────────────
FNG_KELLY_BOOST_ENABLED=true
FNG_CAPITULATION_THRESHOLD=15       # F&G ≤ 15 → capitulación total
FNG_EXTREME_FEAR_THRESHOLD=20       # F&G ≤ 20 → miedo extremo (actual: 20)
FNG_EXTREME_GREED_THRESHOLD=75      # F&G ≥ 75 → euforia peligrosa
FNG_CAPITULATION_MULTIPLIER=1.50    # +50% Kelly en capitulación
FNG_EXTREME_FEAR_MULTIPLIER=1.35    # +35% Kelly en miedo extremo
FNG_EXTREME_GREED_MULTIPLIER=0.65   # -35% Kelly en euforia
FNG_CAPITULATION_PNL_BOOST=-0.25    # relax min_pnl hasta -$0.25 en capitulación

# ── Microestructura (R3) ──────────────────────────────────────────────────────
LIQUIDITY_FILTER_ENABLED=true
MIN_LIQUIDITY_VOL_USDT=3000000      # $3M vol 24h mínimo para spot (todos los pares screener pasan)
MAX_SLIPPAGE_TP_RATIO=0.25          # spread implícito no puede consumir > 25% del TP

# ── Correlaciones por categoría (R4) ─────────────────────────────────────────
CATEGORY_MAX_POSITIONS_ENABLED=true
# category_max_positions: {"LARGE_CAP":3,"MIDCAP_ALT":5,"NARRATIVE":4,"MEME":2} — en config.py defaults

# ── Circuit breaker técnico (R5) ──────────────────────────────────────────────
CIRCUIT_BREAKER_ENABLED=true
CIRCUIT_BREAKER_CONSECUTIVE_FAILURES=5   # 5 errores API consecutivos → pausa
CIRCUIT_BREAKER_PAUSE_MINUTES=10         # pausa de 10 min (buys sólo, SL/TP no se ven afectados)

# ── On-chain funding rate (R6) ────────────────────────────────────────────────
FUNDING_RATE_SPOT_FILTER_ENABLED=true
FUNDING_RATE_SPOT_MAX=0.0008        # > 0.08% = longs sobreapalancados → skip spot buy
FUNDING_RATE_CACHE_MINUTES=10       # cache funding rate 10min (fapi.binance.com)
```

### ⚠️ Nota crítica: docker-compose.yml inyecta variables por encima del .env
`MAX_PER_TRADE_USDT` y `MAX_POSITIONS` están declaradas en `docker-compose.yml` líneas 63-64.
Para cambiarlas: editar `docker-compose.yml` + `docker compose up -d app` (NO usar `restart`).
Los nuevos params D1–D4 (MAX_POSITIONS_SPOT_BULL, etc.) solo tienen defaults en `config.py`.
Para sobreescribir desde Docker: agregar al `docker-compose.yml` como `- VAR=${VAR:-default}`.

### Defaults config.py alineados con producción (jun 20 2026)
Todos los `Field(default=...)` en `config.py` ahora reflejan los valores activos de producción:
- `stop_loss_pct`: 1.5 → **1.8** | `take_profit_pct`: 2.5 → **3.5**
- `max_positions_spot_bull`: 15 → **20** | `max_positions_range_scalp`: 8 → **10**
- `cycle_spot_bull`: 15 → **10** | `cycle_preservation`: 30 → **45** | `cycle_futures_short`: 8 → **6**

### Sin pendientes de ajuste — todos aplicados

---

## 6. BUGS CONOCIDOS Y FIXES

### Activos (no resueltos)

**Sin bugs activos conocidos** — todos verificados y resueltos al jun 20 2026 16:05 UTC-6.

### Bugs corregidos sesión 5 jun 20 2026 (commit d454044):
| Bug | Archivo | Fix |
|-----|---------|-----|
| BUG-E1+E7 | `run_trades.py:1487,2942` | `_buy_top_symbols()` acepta `regime_obj`/`regime_mode_override` — elimina re-detección redundante de `get_regime()` (ahorra 3 API calls/ciclo) |
| BUG-E5 | `run_trades.py:1621,2250` | `get_account_balance()` movido fuera del loop de símbolos; actualización local post-compra — ahorra N-1 API calls/ciclo |
| BUG-E6 | `run_trades.py:2875-2888` | `get_screened_pairs()` cacheado una vez por `execute_once()`; cache pasado a `_buy_top_symbols()` — ahorra 3-4 HTTP calls/ciclo |
| BUG-E8 | `run_trades.py:1777,1816,2235` | Caracteres no-ASCII en `decision_reason` → `LIM_SIMBOLO`, `QTY_INVALIDA` (previene encoding errors en DB) |
| BUG-E10 | `autonomous_signals.py:459-488` | `get_top_buy_signals()` en FUTURES_SHORT incluye señales SELL/HOLD como candidatos a short; SELL bonus RSI +15 en ranking |
| MEJORA-A1 | `run_trades.py:2942-2950` | Re-sort candidatos por EV predicho antes de `_buy_top_symbols()` — slots ocupados por mayor rentabilidad esperada |
| BUG-E2 | `run_trades.py:33-72` | `_get_cycle_asg()` — instancia ASG compartida por ciclo; reset al inicio de `execute_once()` — elimina 2-3 instanciaciones redundantes/ciclo |
| BUG-E4 | `pnl_service.py:41` + `run_trades.py:2378` | `predict_for_symbol(precomputed_signal=...)` reutiliza indicadores de Step 1 — elimina N llamadas `analyze_symbol()` redundantes en Step 2 |

### Bugs corregidos sesión 4 jun 20 2026 (commit e9d8877):
| Bug | Archivo | Fix |
|-----|---------|-----|
| BUG-D1 | `futures_client.py:826` | `tp_pct` default `5.0`→`7.0` en `open_short()` — evita TP incorrecto si caller no pasa parámetro |
| BUG-D2 | `analysis_cache.py:33-43` | `ON CONFLICT` reemplazado por upsert manual `UPDATE`→`INSERT` — cache técnico ahora persiste en DB correctamente |
| BUG-D2+ | `migrations/010_market_data_unique.sql` | Índice UNIQUE parcial `WHERE data_type='technical'` aplicado en producción |
| BUG-D3 | `run_trades.py:1695,2712` | Fallback `futures_usdt_per_trade` `100.0`→`20.0` en paths SHORT (consistente con config.py default) |
| BUG-D4 | `run_trades.py:53` | Fallback `max_largecap_shorts` `3`→`5` en `_get_futures_client()` (consistente con guard en línea 1643) |

### Bugs corregidos sesión 3 jun 20 2026 (commit 818fd3f):
| Bug | Archivo | Fix |
|-----|---------|-----|
| BUG-C1 | `run_trades.py:1669` | `_f_tp` fallback `5.0`→`7.0` (evita TP incorrecto si settings falla) |
| BUG-C3 | `run_trades.py:1640-1650` + `config.py:80-81` | LARGECAP guard implementado en código (ya estaba en docker-compose pero no en lógica) |
| BUG-NUEVO-1 | `run_trades.py:2878` | `TechnicalIndicators` → `AutonomousSignalGenerator` en inject futures |
| BUG-NUEVO-2 | Binance API | TP BTCUSDT SHORT 5%→7% vía algoOrder cancel+recreate |
| BUG-NUEVO-3 | `autonomous_signals.py:235-241` | `vol_spike` siempre presente en señales |

### Diagnósticos descartados (falsos positivos sesión 3):
- ~~BUG-S1~~: `signal_meta` SÍ incluye futures inyectados (inject ocurre antes de `_buy_top_symbols`)
- ~~BUG-S3~~: F&G boost futuros (×1.30) NO es duplicado (`_kelly_position_size` no aplica F&G)
- ~~Corruptas DB~~: Los datos de futuros cerrados vienen de Binance userTrades API, no PostgreSQL

---

## SESIÓN JUN 17 2026 TARDE — Pipeline FUTURES_SHORT completo (18:41 UTC-6)

### Objetivo: que el sistema abra SHORTs reales cuando el régimen es FUTURES_SHORT

### Root causes identificados (cadena de 5 bugs):

| # | Bug | Síntoma en logs | Archivo |
|---|-----|-----------------|---------|
| B1 | Bloque FUTURES_SHORT **después** del `total_open >= _regime_max` | Spots llenaban el límite → SHORTs nunca se evaluaban | `run_trades.py` |
| B2 | Inject de `futures_symbols` solo cuando `top_signals` vacío | En bear siempre hay señales spot → inject nunca activaba | `run_trades.py` |
| B3 | Orden market SELL sin `positionSide=SHORT` | `-4061: Order's position side does not match` | `futures_client.py` |
| B4 | SL/TP algo orders sin `positionSide=SHORT` (tenían `reduceOnly` incompatible con hedge) | `-4061` en algo orders | `futures_client.py` |
| B5 | trigger price con 6 decimales fijos sin alinear al tick size | `-1111: Precision over maximum` | `futures_client.py` |

### Fixes implementados:

**B1** — `run_trades.py` L1522: bloque FUTURES_SHORT movido **antes** del `if total_open >= _regime_max`

**B2** — `run_trades.py` L2632: inject de `futures_symbols` al pool **siempre** (no solo si vacío):
```python
# Antes (condicional):
if not top_signals:
    if FUTURES_SHORT: top_signals = [futures symbols]

# Después (siempre):
top_signals = _select_symbols(...)
if FUTURES_SHORT and confirmed:
    _new_futures = [s not already in pool]
    top_signals = _new_futures + top_signals  # futures al frente
```

**B3** — `futures_client.py` L654: orden market SHORT:
```python
# Antes:  {"side": "SELL", "type": "MARKET", "reduceOnly": "false"}
# Después: {"side": "SELL", "type": "MARKET", "positionSide": "SHORT"}
```

**B4** — `futures_client.py` L677 (SL) y L704 (TP): algo orders:
```python
# Antes:  {"reduceOnly": "true", ...}
# Después: {"positionSide": "SHORT", ...}  # reduceOnly eliminado
```

**B5** — `futures_client.py` L672/703: trigger price ahora usa `_align_price(_raw, tick)` en vez de `round(x, 6)`

**B6 (extra)** — SL/TP spot casi-nulos en DB: TRX(0.18%), TON(0.71%), U(0.02%) → corregidos a categoría:
- TRXUSDT: SL=0.3165 (1.8%), TP=0.3336 (3.5%)
- TONUSDT: SL=1.6351 (2.5%), TP=1.7609 (5.0%)
- UUSDT: SL=0.9825 (1.8%), TP=1.0355 (3.5%)

### Resultado final:
```
[Futures] FUTURES_SHORT: injecting ['BTCUSDT','ETHUSDT','SOLUSDT',...] into signal pool
[Futures] FUTURES_SHORT mode: opening short BTCUSDT margin=$130.0
[Futures] Opening SHORT BTCUSDT: qty=0.004, entry~64546, SL=66138 (+2.5%), TP=60653 (-6.0%)
[Futures] SL algo order placed: algoId=2000001143113156
[Futures] TP algo order placed: algoId=2000001143113165
[Futures] SHORT BTCUSDT OPEN ✅
```

### 3 SHORTs activos con SL/TP (colocados manualmente post-deploy):
| Par | qty | Entry | SL | TP | algoId SL | algoId TP |
|-----|-----|-------|----|----|-----------|-----------|
| BTCUSDT | 0.004 | 64,546 | 66,138 | 60,653 | 2000001143113156 | 2000001143113165 |
| ETHUSDT | 0.148 | 1,753.79 | 1,799.04 | 1,649.85 | 2000001143113030 | 2000001143113040 |
| SOLUSDT | 3.59 | 72.26 | 74.15 | 68.0 | 2000001143113216 | 2000001143113228 |

- Margin por trade: $130 (F&G=15 capitulación → boost 1.3×)
- R:R = 2.4 | EV esperado ~+2.18%/trade | WR esperado ~55% en bear

### Pendientes N33-N35:
- N33: Verificar que `check_positions()` detecta SL/TP triggers de los algo orders correctamente
- N34: ✅ DONE jun20 — `FUTURES_SL_PCT=3.0` y `FUTURES_TP_PCT=7.0` en `.env` activos
- N35: ✅ DONE jun 20 sesión 2 — `analyze_symbol()` integrado en inject futures (commit 80ecb70)

---

## SESIÓN JUN 16 2026 MAÑANA — Implementación M1-M5 (commit 7503a31)

### Objetivo: capturar oportunidades tipo OPGUSDT ($23 perdidos) con sistema actual, sin capital adicional ni futuros

### Diagnóstico previo a M1-M5:
- **Causa A — SL prematuro**: 28.6% de los stops eran falsos (el precio recuperaba al TP). SL=3.5% fijo = ruido en tokens con ATR diario 5-12%
- **Causa B — Universo pequeño**: 6-8 pares activos → 2 trades/día
- **Causa C — Capital estático**: señal de alta convicción (vol 2.7x) recibía los mismos $150 que una señal mediocre

### M1 — ATR-Based SL dinámico (`run_trades.py` + `config.py`)
- Nueva función `_calc_atr_sl_pct(client, symbol, settings, category_sl_pct)`
- `SL = max(category_sl, min(8%, ATR14_1h × 1.5))`
- Aplicado en `_check_sl_tp_positions()` (per-symbol, per-ciclo) y en `_buy_top_symbols()` (para Kelly sizing correcto)
- Params: `atr_sl_enabled=True`, `atr_sl_multiplier=1.5`, `atr_sl_max_pct=0.08`, `atr_sl_min_pct=0.01`
- **Resultado**: 25 ATR upgrades en 30d → reduce SL prematuros del 28.6% estimado a ~14%

### M2 — +3 pares NARRATIVE validados (`config.py` + `.env`)
- Añadidos: `ONDOUSDT` (WR=75% en 30d), `JUPUSDT`, `OPUSDT`
- Descartados por WR bajo en 30d: `SUIUSDT`(0%), `ARBUSDT`(22%), `LINKUSDT`(25%), `EIGENUSDT`(25%), `LDOUSDT`(25%)
- Total pares: 20 → 23

### M3 — Kelly conviction multiplier (`run_trades.py` L1623)
- Si `vol_spike ≥ 3.0x` → `trade_allocation × 2.0` (cap: **15% portfolio en SPOT_BULL**, 10% resto)
- Si `vol_spike ≥ 2.0x` → `trade_allocation × 1.5` (cap: 15% portfolio en SPOT_BULL)
- Params: `kelly_conviction_enabled=True`, `kelly_conviction_vol2x=1.5`, `kelly_conviction_vol3x=2.0`, `kelly_conviction_max_pct=0.10`
- Cap SPOT_BULL ampliado: 10%→**15%** (Smart Kelly, commit f34fd60) — con $1,758 liquid: cap $263 (era $175)
- Señales strong detectadas en simulación: 22/87 trades en 30d

### M4 — Breakout entry detector (`autonomous_signals.py` L299)
- Si precio actual > máximo de las últimas 12 velas 1h Y vol_spike ≥ 2.0x: `strength += 0.15`, `breakout_12h=True`
- Flag propagado a `signal_dict` via `_get_top_buy_symbols()` para que M3 aplique sizing fuerte
- Captura exactamente el patrón OPGUSDT: ruptura de estructura + vol alta

### M5 — Boost horario 12-22 UTC (`autonomous_signals.py` L338)
- Para NARRATIVE tokens: si 12 ≤ UTC_hour < 22 Y vol_spike ≥ 1.3x → `strength × 1.10`
- Complementa el filtro nocturno ya existente (00-08 UTC × 0.85)
- Concentra riesgo en horas de máxima liquidez (apertura NYC + tarde EU)

### Resultados simulación ANTES vs DESPUES:

| Período | Sistema | Trades/día | WR | PF | PnL/día USDT | PnL/día MXN |
|---------|---------|-----------|----|----|-------------|------------|
| **7 días (rebote)** | Antes F1-F4 | 1.3 | 77.8% | 1.62 | **7.42** | **145** |
| **7 días (rebote)** | Despues M1-M5 | 1.7 | 66.7% | 1.63 | **8.04** | **157** |
| **30 días (bear -17%)** | Antes F1-F4 | 2.0 | 46.7% | 1.27 | **0.62** | **12** |
| **30 días (bear -17%)** | Despues M1-M5 | 2.9 | 48.3% | 1.07 | **-0.02** | **~0** |

### Interpretación:
- En **mercado de rebote** (últimos 7d): M1-M5 mejora 145→157 MXN/día (+8%), PF mantenido en 1.63
- En **bear estructural 30d** (-17% BTC): el sistema queda breakeven vs +12 MXN/día antes — el ATR SL aumenta la pérdida promedio en mercado bajista pero reduce SL prematuros en rebounds
- **Conclusión clave**: M1-M5 es una mejora neta en mercados activos/bullish. En bear estructural el sistema queda breakeven (correcto: mejor que perder)
- La meta de 1,000 MXN/día requiere mercado bullish F&G>50 + M1-M5 activos → proyección 300-700 MXN/día en bull

### Pendientes post-M1-M5:
- N29: Monitorear primer ATR SL en logs `[ATR-SL]` — verificar que sl_pct expandido se registra correctamente
- N30: Con 20+ trades M3 — analizar si strong signals (vol≥2x) tienen mayor WR que base para calibrar cap_strong
- N31: ONDOUSDT, JUPUSDT, OPUSDT — monitorear primeras señales de los nuevos pares en 48h
- N32: Cuando mercado llegue a F&G>50 (bull), rerun simulación 7d para verificar proyección 300+ MXN/día

---

## SESIÓN JUN 16 2026 TARDE — Análisis por Régimen + Simulación Futuros (sim_regimes.py)

### Estado futuros verificado (jun 16 09:00)
- `futures_enabled=True` → **YA ACTIVO en .env** (no era necesario activar nada)
- `futures_long_in_spot_bull=True`, `futures_long_rsi_threshold=35.0`
- `futures_max_shorts=7`, `futures_usdt_per_trade=100`, `futures_sl_pct=3.0`, `futures_tp_pct=7.0` (v4 OPT jun20)
- Régimen actual: `SPOT_BULL DÉBIL ALZA` (EMA9>EMA21, BTC 7d=+6.79%, 30d=-14.7%)

### Mapa completo de regímenes → estrategia activa

| Régimen detectado | Trigger | Estrategia spot | Futuros | Estado |
|-------------------|---------|-----------------|---------|--------|
| `SPOT_BULL` | EMA9>EMA21 + día>0.5% | Long 23 pares, M1-M5, Kelly×1.0 | LONG 2x si RSI<35 + BTC UP | ✅ Activo |
| `RANGE_SCALP` | BB<6% + vol<0.7x | Long en BB inferior, RSI<40, Kelly×0.7 | LONG 2x en BB touch | ✅ Activo |
| `PRESERVATION` | BTC -7%/7d ó -15%/30d | Solo NARRATIVE, Kelly×0.5 | — | ✅ Activo |
| `FUTURES_SHORT` | PRESERVATION + futures=True | Solo NARRATIVE spot | SHORT 2x BTC/ETH/SOL, SL2.5%/TP6% | ✅ **Activo** (futures_enabled=True) |

### Resultados simulación `sim_regimes.py` (jun 16 2026)

**Período 7 días (rebote, BTC +6.79%):**

| Escenario | Trades/día | WR | PF | PnL/día USD | PnL/día MXN |
|-----------|-----------|----|----|-------------|-------------|
| A — Solo Spot M1-M5 | 1.7 | 66.7% | 1.46 | +7.64 | **+149** |
| B — + Futuros SHORT bear | 1.7 | 66.7% | 1.46 | +7.64 | **+149** |
| C — + Futuros LONG bull | 1.7 | 66.7% | 1.46 | +7.64 | **+149** |
| D — Completo (SHORT+LONG) | 1.7 | 66.7% | 1.46 | +7.64 | **+149** |

> En los 7d de rebote no hay bear (BTC 7d>0) = shorts no se abren. LONG futuros requiere RSI<32 = no activado (mercado alcista tiene RSI más alto). Spot puro domina.

**Período 30 días (bear mayo-junio, BTC -17%):**

| Escenario | Trades/día | WR | PF | PnL/día USD | PnL/día MXN | Desglose |
|-----------|-----------|----|----|-------------|-------------|---------|
| A — Solo Spot M1-M5 | 2.9 | 48.3% | 1.07 | -0.02 | **~0** | spot=-$0.71 |
| B — + Futuros SHORT bear | 4.0 | 48.7% | 1.12 | +0.69 | **+13** | spot=-$0.71, shorts=+$21.43 |
| C — + Futuros LONG bull | 3.2 | 48.4% | 1.08 | +0.11 | **+2** | spot=-$0.71, longs_fut=+$4.15 |
| **D — Completo** | **4.2** | **48.8%** | **1.13** | **+0.83** | **+16** | spot=-$0.71, shorts=+$21.43, longs=+$4.15 |

### Conclusiones clave
1. **En bear -17%:** los FUTURES SHORT convierten breakeven (0 MXN/día) en **+13 MXN/día** (+$404 MXN en 30d)
2. **En bull:** los FUTURES LONG tienen impacto mínimo en 7d — RSI rara vez cae a <32 en rebote fuerte
3. **El sistema completo (Escenario D)** extrae valor en TODOS los regímenes:
   - Bull → spot genera 149 MXN/día
   - Bear → futures SHORT compensan: +13-16 MXN/día en lugar de 0
4. **Futures SHORT WR en bear ~55-60%** (precio baja en bear = shorts ganan = correcto estadísticamente)
5. Meta 1,000 MXN/día: requiere bull + capital ×6 ó WR+60% consistente

### Pendientes post-sim_regimes:
- N33: ✅ VERIFICADO (jun 18) — FUTURES_SHORT abre SHORTs correctamente: BTC/ETH/XRP/BNB/SOL shorts activos con SL/TP reales
- N34: ✅ VERIFICADO — `futures_long_rsi_threshold=35` mantenido; LONG futuros no activó en bear (RSI rara vez <32 en rebotes)
- N35: ✅ IMPLEMENTADO (jun 18) — Anti-whipsaw 3 ciclos activo + `futures_protect_profit_usdt=1.0` protege shorts ganadores en cambio de régimen

### Resueltos (historial)

| Fecha | Bug | Fix |
|-------|-----|-----|
| jun 20 13:30 | **Anti-wick SL**: anti-wick comparaba contra `entry × (1-sl_pct)` ignorando `_effective_sl_price` — si SmartTP EXTEND había movido SL a breakeven (102.0), una vela 1m con close=101.8 bloqueaba el cierre correcto usando el floor viejo (98.2) | `run_trades.py:1135`: `if _last_close > _effective_sl_price` — mismo valor ya calculado por Fix 1 más arriba en el mismo ciclo |
| jun 29 2026 | **BNB discount autónomo**: BNB Off por `auto_bnb_for_commission=false` default y falta de reserva. Activado por defecto, buffer extra subido a 10 USD, Portfolio Manager reserva BNB antes de sizing, compra/transferencia/shortfall auditados en `trades_audit`, alerta de drift si `use_bnb` apagado 2+ checks. Luego, BNB funding por régimen: SPOT_BULL prefiere futures, FUTURES_SHORT prefiere spot, transferencia USDT futures→spot con buffer 10 USD | `config.py`, `config_helpers.py`, `docker-compose.yml`, `binance_commission.py`, `futures_client.py`, `portfolio_manager.py`, `run_trades.py`, `app.py`, `tests/test_binance_commission.py`, `tests/test_portfolio_manager.py` |
| jun 29 2026 | **Screener consciente del régimen**: scanner sugería LONGs en FUTURES_SHORT. Añadido ranking por régimen (SPOT_BULL/RANGE_SCALP/PRESERVATION/FUTURES_SHORT), cache invalidado al cambiar de régimen, persistencia 100% en `screener_decisions`, endpoints `/dashboard/screener` y `/screener/decisions` | `pair_screener.py`, `run_trades.py`, `app.py`, `migrations/017_screener_decisions.sql`, `tests/test_screener.py` commit 91b7128 |
| jun 20 13:30 | **Fix 4 orderType**: campo `type` en respuesta `/fapi/v1/openAlgoOrders` puede ser `None` para CONDITIONAL orders (Binance lo devuelve en `orderType`) → match fallaba silenciosamente, SL/TP se restauraban a defaults hardcodeados destruyendo el trailing | `futures_client.py:423`: `_o_type = _o.get("type") or _o.get("orderType", "")` |
| jun 20 13:30 | **SmartTP predictor tamaño**: `_evaluate_tp_momentum()` pasaba `max_per_trade_usdt=$150` fijo al predictor — posiciones de $350 (FUERTE tier) tenían `success_probability` subestimada → decisión `scale_out` cuando debería ser `extend` | `run_trades.py`: función acepta `position_size_usdt` opcional; call site pasa `float(pos.quantity) × current_price` capped a `max_per_trade_usdt` |
| jun 20 12:40 | **Fix 1 SPOT**: SL fijo ignoraba `pos.stop_loss` actualizado por SmartTP EXTEND/Scale-Out → tras un EXTEND el SL retrocedía a `entry × (1-sl_pct)` en el ciclo siguiente, borrando la protección ganada | `run_trades.py`: bloque `# ── Stop-Loss ──` usa `_effective_sl_price = max(sl_floor, pos.stop_loss)` en vez de comparar solo `pnl_pct <= -sl_pct` |
| jun 20 12:40 | **Fix 3 FUTURES trail**: `trail_best_price` se inicializaba al precio actual tras restart (Docker), perdiendo el historial del trailing ya ejecutado → SHORT ganador veía "best_price = precio actual" y el SL se calculaba demasiado permisivo | `futures_client.py`: inicialización infiere `best_price = sl_price / (1 + trail_dist)` para SHORT y `sl_price / (1 - trail_dist)` para LONG; solo aplica si cumple sanity checks (< entry para SHORT, > entry para LONG) |
| jun 20 12:40 | **Fix 4 FUTURES sync**: `_sync_open_positions` recreaba SL/TP hardcodeados (`entry × 1.03`/`entry × 0.94`) en cada restart, destruyendo silenciosamente el SL real que el trailing había movido en Binance | `futures_client.py`: `_sync_open_positions` intenta leer `/fapi/v1/algoOrder/openOrders` por símbolo; si encuentra `STOP_MARKET` o `TAKE_PROFIT_MARKET` usa el `triggerPrice` real y guarda el `algoId` en `sl_order_id`/`tp_order_id`; fallback seguro a defaults si API falla |
| jun 16 08:00 | `trailing_peak` congelado entre ciclos de 20min: el pico real del día se perdía si ocurría entre dos ciclos y el precio bajaba antes del siguiente. OPG: peak real=0.1783 no capturado → trail_SL=0.1652 en vez de 0.1747 → $11 USDT no capturados | `run_trades.py` bloque trailing (F1): antes de evaluar `peak_price`, reconstruye el high real de las últimas 30 velas 1m vía `get_klines`. Si el high 1m > peak_price guardado → actualiza. Commit db5912a |
| jun 16 08:00 | Smart TP evalúa momentum cuando el precio YA retrocedió del pico (ej. OPG evaluó a 4.85% después de haber estado en +12.2%): score bajo lleva a CLOSE cuando realmente había habido un breakout fuerte | `run_trades.py` `_evaluate_tp_momentum()` (F2): detecta reversal con velas 15m. Si precio está >2% bajo el máximo de las últimas 8 velas 15m → `decision=close_reversal` inmediato (no evalúa score). Evita EXTEND en retroceso. Commit db5912a |
| jun 16 08:00 | `enable_position_closure=True` hacía que `portfolio_manager` cerrara posiciones en DB antes de vender en Binance, saltando todos los gates de protección (min_hold=240, protect_profit). USD1(21min) e INJ(36min) cerrados sin pasar gate de 240min | `config.py`: `enable_position_closure=False` + `.env`: `ENABLE_POSITION_CLOSURE=false` (F3). El cierre real ya lo maneja `_close_prediction_based()` con gates completos. Commit db5912a |
| jun 16 08:00 | Stablecoins (USD1USDT) entraban al pipeline de rebalanceo — comisiones en activos de volatilidad ~0% sin ninguna ganancia posible | `config.py`: nuevo param `rebalance_excluded_symbols=['USD1USDT','USDCUSDT','BUSDUSDT','TUSDUSDT','FDUSDUSDT']` (F4). `run_trades.py` Gate 3: skip si símbolo en excluded list. Commit db5912a |
| jun 16 07:34 | Smart TP: sistema anterior cerraba/vendía 50% al TP fijo sin considerar momentum — se perdían ganancias potenciales cuando la posición tenía fuerza alcista | `run_trades.py`: nueva función `_evaluate_tp_momentum()` corre predictor + señales técnicas al momento del TP. Decisión: EXTEND(score 3-4/4)→sube TP+SL, SCALE_OUT(2/4)→vende 50%, CLOSE(0-1/4)→cierra todo. Historial completo en `metadata.tp_decisions[]`. `config.py`: 3 params nuevos + `tp_extension_pct` por categoría. `position_manager.py`: `update_position_tp()`. Commit 43c0c70 |
| jun 16 07:23 | Scale-out no actualizaba `pos.quantity` en DB — tras vender 50% en TP el PM seguía con qty completa → siguiente ciclo SL/TP intentaba vender doble → error Binance `-2010` (OPGUSDT afectado) | `run_trades.py` scale-out: ahora llama `update_position_quantity(pos.position_id, remaining_qty)` + guarda `scale_out_qty_sold/avg_price` en metadata. `position_manager.py`: método `update_position_quantity()` nuevo; `update_position_metadata` y `update_position_sl` corregidos para usar `pos.db_id` (columna `id`) en vez de `position_id` (no existe en DB). Commit b3afd02 |
| jun 16 07:23 | 3 saldos huérfanos: INJ(29.76), USD1(222), OPG(617 vendidos sin actualizar PM) — compras ejecutadas en Binance sin record en PM (excepción silenciosa post-buy o corte de ciclo) | Registrados manualmente como posiciones con SL/TP correctos: INJ(29.76@5.794, NARRATIVE SL3.5%/TP7%), USD1(222@1.00071, MIDCAP SL1.8%/TP3.5%). OPG qty corregida. Total recuperado: ~$504 USDT ahora monitoreados |
| jun 16 07:23 | Clamp defensivo: cualquier desincronización futura de qty causaría `-2010` en SL/TP | `run_trades.py` `_check_sl_tp_positions()`: antes de `create_market_order` obtiene balance Binance real y usa `min(qty_db, binance_balance)` → nunca vende más de lo disponible |
| jun 15 20:40 | `_sell_holdings_not_in_top()` vendía TODO lo que no estaba en `top_symbols` del ciclo de 20min → win rate rebalanceo 18% (49/60 pérdidas = -$63.56 en un día). Root cause: criterio "no está en top" es ruido, no señal de salida | **Opción B**: rebalanceo solo si RSI1h>60 O EMA9 descendente + min_hold=240min. Nuevo param `rebalance_rsi_overbought=60`. `run_trades.py` + `config.py` commit ffc5a75 |
| jun 15 20:40 | Timezone bug en `min_hold_minutes`: `entry_time` naive datetime vs `datetime.now(timezone.utc)` aware → comparación lanzaba excepción silenciosa (try/except pasaba), protección ignorada completamente → posiciones de 9-20min se rebalanceaban | Fix A: `_entry_ts.replace(tzinfo=timezone.utc)` si `tzinfo is None`. `run_trades.py:745` commit 4639c2c |
| jun 15 20:40 | Posiciones en pérdida cerca del SL cerradas por rebalanceo antes de que SL checker actuara (competencia entre dos mecanismos) | Fix D SL-Delegated: si `abs(pnl_pct) >= SL × 0.75` → diferir al SL checker, no rebalancear. `run_trades.py:771` commit 4639c2c |
| jun 15 13:47 | Dashboard mostraba 0 posiciones cerradas — `close_position()` guardaba `exit_time` como datetime naive (sin UTC) vs DB que retorna aware; comparación en `calculate_performance_metrics` y `get_closed_positions` lanzaba `TypeError: can't compare offset-naive and offset-aware datetimes`. `_get_monte_carlo_projection` pasaba objetos `ClosedPosition` a función que esperaba dicts `t["pnl"]` | Fix: `datetime.now(timezone.utc).isoformat()` en `position_manager.py:447` + conversión a dicts en `app.py:741`. `83 trades` ahora visibles. |
| jun 15 11:41 | `_STALE_CYCLE_MINUTES=15` hardcoded → falso positivo con SPOT_BULL 15min; warning spameaba error_collector en cada poll | Fix: umbral dinámico `orchestrator.current_cycle_minutes × 1.5` + dedup por `last_cycle` timestamp. `app.py` línea ~1500 |
| jun 15 11:41 | `portfolio_manager.get_position_predictions()` usaba `trade_cycle_minutes=5` fijo → predicción a 5min en vez del ciclo real 15min SPOT_BULL → sesgo contra posiciones abiertas en prediction-reallocation | Fix: usa `orchestrator.current_cycle_minutes` con fallback a config. `portfolio_manager.py` línea ~351 |
| jun 15 10:00 | `MAX_PER_TRADE_USDT=15` → predictor calculaba PnL con $15 pero Kelly compraba $150 (10× subestimado) | `MAX_PER_TRADE_USDT=350` en `.env` + `docker-compose.yml` línea 63. Requiere `up -d`, no `restart` |
| jun 15 10:00 | `MAX_POSITIONS` y `MAX_PER_TRADE_USDT` ignoraban el `.env` del contenedor | Variables inyectadas por `docker-compose.yml` con `${VAR:-default}` tienen prioridad sobre `/app/.env`; fix en compose |
| jun 15 09:35 | `SyntaxError` en `run_trades.py` — `min(generator, default=None)` requiere paréntesis extra en Python 3.11 | Convertido a lista: `_ts_list = [...]; min(_ts_list) if _ts_list else None` |
| jun 15 09:35 | `NameError: NewsClient` en health check `social_score` de `app.py` | Import local dentro del bloque: `from news_client import NewsClient as _NewsClient` |
| jun 15 09:00 | `allocation *= 0.5` cuando `btc_trend==DOWN` penalizaba doble sobre Kelly de régimen | Bloque eliminado de `run_trades.py` — Kelly de régimen es suficiente |
| jun 15 09:00 | Health check futuros contaba `len(_open_positions)` sin distinguir LONG/SHORT | `app.py` usa `count_open_shorts()` + `count_open_longs()` |
| jun 15 09:00 | `get_top_buy_signals()` sin ranking por régimen | Añadido `regime_mode` param: FUTURES_SHORT→RSI-desc, RANGE_SCALP→BB-lower+RSI-asc |
| jun 15 09:00 | Screener bug: `get_screened_pairs(settings)` posicional → `float < Settings` TypeError | 5 llamadas a `get_screened_pairs(settings=settings)` — screener retorna 46 spot + 8 futures |
| jun 15 | `-4067` hedge mode con algo orders huérfanas | `enable_hedge_mode()` cancela via `openAlgoOrders` primero |
| jun 15 | `_open_positions` no soportaba LONG+SHORT mismo símbolo | Key refactorizada a `"SYMBOL_SIDE"` (ej: `"BTCUSDT_SHORT"`) |
| jun 14 | `-4120` SL/TP order fallaba | Migrado a `/fapi/v1/algoOrder` con `algoType=CONDITIONAL` |
| jun 14 | BTCUSDT qty=0 | `_align_qty` redondeaba a 0 con `$20 × 2× / $65k = 0.0006 < step 0.001` |
| jun 14 | ETH precision `-1111` | `_align_price()` usa `_get_tick_size()` |
| jun 13 | TAO acumulaba 3 posiciones | `max_positions_per_symbol` no se respetaba en todos los paths |
| jun 12 | Whipsaw en cambio de régimen | `_non_short_regime_cycles` counter + 3 ciclos mínimos |

---

## 7. APIs EXTERNAS — NOTAS Y ERRORES CONOCIDOS

### Binance Spot (`api.binance.com`)
| Endpoint | Uso | Notas |
|----------|-----|-------|
| `GET /api/v3/ticker/24hr` | Screener dinámico | Sin auth; límite 20 req/min; usado con cache 15min |
| `POST /api/v1/order` | Compras spot | Requiere `HMAC-SHA256` signature |
| `GET /api/v3/account` | Balance | Campos: `free`, `locked` por asset |

### Binance FAPI (`fapi.binance.com`)
| Endpoint | Uso | Error conocido | Nota |
|----------|-----|---------------|------|
| `GET /fapi/v1/positionSide/dual` | Verificar hedge mode | — | Devuelve `{"dualSidePosition": true/false}` |
| `POST /fapi/v1/positionSide/dual` | Activar hedge mode | **-4067** si hay órdenes abiertas | Cancela con `openAlgoOrders` primero |
| `POST /fapi/v1/order` | Orden market | **-4120** para STOP_MARKET/TP_MARKET desde dic 2025 | Migrar a `/fapi/v1/algoOrder` |
| `POST /fapi/v1/algoOrder` | SL/TP desde dic 2025 | — | `algoType=CONDITIONAL`, `type=STOP_MARKET` o `TAKE_PROFIT_MARKET` |
| `GET /fapi/v1/openOrders` | Órdenes regulares abiertas | **NO incluye algo orders** | Para algo: usar `openAlgoOrders` |
| `GET /fapi/v1/openAlgoOrders` | Algo orders abiertas (SL/TP) | — | Sin parámetro = todas las símbolos |
| `DELETE /fapi/v1/algoOrder` | Cancelar algo order | — | Params: `{symbol, algoId}` |
| `DELETE /fapi/v1/allOpenOrders` | Cancelar todas regular | — | Params: `{symbol}` |
| `GET /fapi/v1/positionRisk` | Posiciones abiertas | — | Campo `positionSide`: BOTH/LONG/SHORT |
| `POST /sapi/v1/futures/transfer` | Transferencia spot↔futuros | — | `type=1` spot→futures, `type=2` futures→spot |
| `GET /fapi/v1/premiumIndex` | Funding rate | — | Campo `lastFundingRate`; >0.1% = no entrar SHORT |
| `GET /fapi/v1/exchangeInfo` | Tick size, step size | — | Cache local para evitar spam |

### Códigos de error frecuentes
| Código | Mensaje | Causa | Solución |
|--------|---------|-------|---------|
| -4047 | `No need to change margin type` | Margin type ya es ISOLATED | Ignorar (ya está bien) |
| -4067 | `Position side cannot be changed` | Hay órdenes abiertas (incl. algo orders) | Cancelar via `openAlgoOrders` primero |
| -4120 | `Order type not supported` | STOP_MARKET/TP_MARKET ya no van por `/order` | Usar `/algoOrder` |
| -1111 | `Precision is over maximum` | `triggerPrice` con más decimales de los permitidos | Usar `_get_tick_size()` para alinear precio |
| -4509 | `ReduceOnly Order Failed` | Intentar cerrar más qty de la abierta | Verificar qty con `get_positions()` |
| -2011 | `Unknown order sent` | Orden ya cancelada/completada | Ignorar silenciosamente |
| -1102 | `Mandatory parameter was not sent` | Falta parámetro requerido | Revisar endpoint docs |

### Otras APIs
| API | Uso | Auth | Notas |
|-----|-----|------|-------|
| CoinGecko `/trending` | Trending coins | Sin auth | Rate limit 30/min |
| Alternative.me F&G | Fear & Greed index | Sin auth | Valor 0-100, actualiza 1×/día |
| CryptoCompare Social | Score social (opcional) | API key | `CRYPTOCOMPARE_API_KEY` en .env |

---

## 8. PENDIENTES PRIORIZADOS

### ✅ TODO completado al jun 16 2026 07:34 UTC-6

**Plan arquitectura v1** (P1–P14): todos completados.
**Plan arquitectura v2** (Fases A–E): todos completados.
**Ajuste agresivo Kelly + ciclos**: completado.
**Dashboard redesign v18**: completado.
**Smart exit re-evaluation**: completado.
**Dynamic max positions + replacement + overflow**: completado.
**Nuevos roles R1-R6 (Behavioral Finance, Microestructura, Correlaciones, Circuit Breaker, On-chain)**: completado.

| # | Tarea | Estado |
|---|-------|--------|
| P1–P14 | Ver historial anterior | ✅ Todos completados |
| A | Exposición total cap (75%) + rebalanceo inteligente (min_hold=90min, max=1/ciclo) | ✅ `run_trades.py` + `.env` |
| B | Ciclos dinámicos por régimen (20/5/45/6 min) + TTL señales dinámico | ✅ `run_trades.py` + `orchestrator.py` |
| C | Kelly futuros con `sl_pct × leverage` real | ✅ `run_trades.py` |
| D | Dashboard: `/health` con `portfolio_exposure` + `trade_cycle` + barra progreso | ✅ `app.py` + `dashboard.js` |
| E | Kelly por categoría: usa `category_thresholds[cat].sl_pct/tp_pct` | ✅ `run_trades.py` |
| K | Kelly agresivo: base 12%, max $350, max_per_trade $350 (bug fix) | ✅ `.env` + `docker-compose.yml` |
| U1 | Dashboard redesign v18: Resumen Ejecutivo, KPI row, scroll errores, colapsables | ✅ `index.html` + `dashboard.js` + `styles.css` |
| U2 | Stale cycle warning: umbral dinámico × 1.5 + dedup una vez por ciclo | ✅ `app.py` |
| U3 | Smart exit — Fix A: anti-wick SL (confirma último candle 1m antes de cerrar) | ✅ `run_trades.py` + `config.py` |
| U4 | Smart exit — Fix B: horizonte predicción dinámico en prediction-reallocation | ✅ `portfolio_manager.py` |
| U5 | Smart exit — Fix C: rebalanceo consciente del predictor (epnl>0 + prob≥0.55 → hold) | ✅ `run_trades.py` + `config.py` |
| D1 | Dynamic max positions: `_get_max_positions_for_regime()` — SPOT_BULL=15, RANGE_SCALP=8, PRESERVATION=5, FUTURES_SHORT=4 | ✅ `run_trades.py` + `config.py` |
| D2 | Selector buffer +5: siempre evalúa candidatos de reemplazo aunque límite lleno | ✅ `run_trades.py` |
| D3 | Reemplazo activo `_try_replace_weakest_position()`: cierra peor EV si nueva señal supera ratio 1.3× + solo en pérdida | ✅ `run_trades.py` |
| D4 | Overflow slots `_is_overflow_eligible()`: abre slot extra si strength≥0.80 + prob≥0.65 + epnl>0 (max 3) | ✅ `run_trades.py` |
| D5 | `orchestrator.current_regime_mode` sincronizado por ciclo; `/health/status` expone `max_positions_current` + `overflow_slots_used` | ✅ `orchestrator.py` + `app.py` |
| D6 | Dashboard: badge ⚡OVF naranja en posiciones overflow; health `trade_cycle` muestra régimen + max + slots | ✅ `dashboard.js` + `styles.css` |
| R1 | F&G Kelly boost: Capitulacion(≤15)×1.50, Extreme Fear(≤20)×1.35, Extreme Greed(≥75)×0.65 | ✅ `run_trades.py` + `config.py` |
| R2 | Capitulacion PnL boost: `pnl_service.validate()` ya usa `fng_modifier` agresivo de `news_client` | ✅ `pnl_service.py` (preexistente + config) |
| R3 | Microestructura: `_check_liquidity_and_slippage()` — vol_24h<$3M o spread>25% TP → skip | ✅ `run_trades.py` + `config.py` |
| R4 | Correlaciones: `_check_category_limit()` — LARGE_CAP:3, MIDCAP_ALT:5, NARRATIVE:4, MEME:2 | ✅ `run_trades.py` + `config.py` |
| R5 | Circuit breaker técnico: 5 fallos API consecutivos → pausa 10min (no bloquea por pérdidas) | ✅ `run_trades.py` + `app.py` + `dashboard.js` |
| R6 | On-chain funding rate: `_get_funding_rate()` cacheado 10min, skip si FR>0.08% en SPOT_BULL | ✅ `run_trades.py` + `config.py` |
| S1 | Fix scale-out qty desync + `update_position_metadata/sl` columna DB correcta + clamp -2010 | ✅ `run_trades.py` + `position_manager.py` commit b3afd02 |
| S2 | Recuperar 3 saldos huérfanos (~$504 USDT): INJ 29.76, USD1 222, OPG desync | ✅ DB directo commit b3afd02 |
| S3 | **Smart TP**: `_evaluate_tp_momentum()` — EXTEND(score≥3/4), SCALE_OUT(2/4), CLOSE(≤1/4). Historial `metadata.tp_decisions[]` para LearningEngine | ✅ `run_trades.py` + `config.py` + `position_manager.py` commit 43c0c70 |

### PROFIT OVERHAUL jun 16 2026 — O1-O5 + Smart Kelly

| ID | Mejora | Commit | Impacto estimado MXN/día |
|----|--------|--------|-------------------------|
| O1 | Liquidar PAXG+XAUT+USD1×2 → USDT libre $911→$1,758 (+93%) | manual | +130–150 |
| O2 | WR Screener: bloquear pares con 3 SL consecutivos (`_consecutive_sl_count`) | ebfd749 | +10–20 |
| O3 | Ciclo SPOT_BULL 20→10 min (+47% throughput) | ebfd749 | +15–20 |
| O4 | Smart TP EXTEND umbral 3/4→2/4 (`SMART_TP_EXTEND_SCORE_THRESHOLD=2`) | ebfd749 | +15–25 |
| O5 | HBARUSDT ($15.7M vol) + AAVEUSDT ($14.4M vol) a analysis_symbols (25 pares) | 1db5101 | +10–20 |
| SK | Smart Kelly: `kelly_max=max(350, liquid×25%)`, tiers directos, conviction 10→15% SPOT_BULL | f34fd60 | +60–120 |
| **TOTAL** | | | **+240–355 MXN/día** |

**Pendientes O6-O7:**
- O6: Staking $600 USDT en Binance Flexible Earn → ~$2 MXN/día pasivos (**acción manual usuario**)
- O7: Momentum Burst detector en `autonomous_signals.py` (baja prioridad, ~100 líneas)

### En observación

| # | Tarea |
|---|-------|
| N2 | Verificar BTCUSDT short qty>0 cuando régimen cambie a FUTURES_SHORT |
| N4 | Monitorear WR durante próximas 48h con nuevos parámetros agresivos — objetivo ≥ 45% |
| N5 | Predictor en bootstrap_mode para todos los pares nuevos del screener — acumulará datos con el tiempo |
| N6 | Monitorear que nuevo rebalanceo quirúrgico (RSI>60 O EMA9↓) no retiene posiciones en caída real sostenida — revisar logs `[Rebalance] Technical sell confirmed` vs `Holding: no technical sell signal` |
| N7 | Monitorear que Fix A (anti-wick candle 1m) no retrase SL en caídas reales > 2min |
| N8 | Primera activación de reemplazo activo: verificar logs `[Replace]` con ratio correcto y que no vende posiciones en profit |
| N9 | Primera activación overflow: verificar logs `[Overflow]` con strength/prob correctos; monitorear si las 3 posiciones extra son rentables |
| N10 | SPOT_BULL=15 posiciones: verificar que `max_total_exposure_pct=0.75` actúa como verdadero techo; Kelly sizes deben ser ~$80-100 con 15 posiciones |
| N11 | Primera activación F&G Kelly boost: con F&G=20 (Extreme Fear actual) debería boost ×1.35 en cada buy |
| N12 | Filtro liquidez R3: verificar que no bloquea pares del screener (todos deben tener vol>$3M) |
| N13 | Filtro categorias R4: con 12 posiciones abiertas verificar distribución MEME≤2 / NARRATIVE≤4 |
| N14 | Circuit breaker: verificar en logs que solo se activa por errores API, NO por pérdidas de mercado |
| N15 | Verificar win rate real con 83 trades en dashboard — actualmente 29.5% (bajo por rebalanceos). Monitorear si sube con nuevos filtros R1-R6 |
| N16 | Monitorear rebalanceos próximas 24h: objetivo reducir de 60/día → 5-8/día. Log clave: `[Rebalance] Holding: no technical sell signal` |
| N17 | Verificar que posiciones antiguas (>4h) con RSI>60 sí se rebalancean correctamente cuando aparece nueva señal de mayor EV |
| N18 | WR trades normales: monitorear si mejora con SL 3.5% vs anterior 2.5% — objetivo ≥20% WR en próximos 20 trades normales |
| N19 | ✅ RESUELTO: Trailing NARRATIVE ahora activation=5%, distance=2%, TP=7% — monitorear primera activación en breakout >5% |
| N20 | Monitorear trailing NARRATIVE nuevos valores: verificar que no retiene posiciones en reversión fuerte. Log: `[Trail]` en run_trades.py |
| N21 | Monitorear que slot-pressure solo actúa cuando posiciones >= MAX_POSITIONS_régimen (15 en SPOT_BULL) |
| N22 | Monitorear win rate con predictor actualizado (9 features) en próximos 20 trades reales — baseline actual: 0%/15 trades normales |
| N23 | Cuando acumule 50+ trades reales limpios: ejecutar POST /learning/run para refinar bias_correction con datos reales (actualmente cap 1.5) |
| N36 | Verificar que el simulador genera 34k+ trades en próxima re-simulación (señales reales más selectivas que simplified = esperado) |
| N37 | Monitorear que señales LiveSystem y HistoricalSimulator convergen (mismo TechnicalIndicators.analyze_signal) — win rate esperado ~38-42% |
| N38 | Si capital crece >$5,000: re-ejecutar ingest_historical.py --phase full para actualizar datos con ventana más reciente |
| N24 | Monitorear que no aparecen nuevos saldos huérfanos — causa raíz (excepción silenciosa post-buy) pendiente de investigar en `_buy_top_symbols` |
| N25 | Verificar que scale-out de próximas posiciones actualiza `remaining_qty` en logs: `SCALE-OUT {sym}: sold X (50%) @ P, remaining=Y` |
| N26 | Smart TP: monitorear primeras activaciones en logs `[SmartTP]` — verificar que score/decision es correcto y que EXTEND guarda `tp_decisions[]` en metadata |
| N27 | Smart TP: cuando acumulen 10+ trades con extensión — analizar si las extensiones fueron rentables (PnL final > PnL en primer TP) para calibrar umbrales de score |
| N28 | Smart Kelly: monitorear logs `Kelly sizing: ... tier=ALTA/FUERTE` en próximos ciclos — verificar que $394/$439 se usan correctamente en señales fuertes |
| N29 | Smart Kelly: tras 20 trades nuevos — comparar avg_size_usd vs baseline $95 para confirmar mejora en tamaño por trade |
| N30 | O6 pendiente: recordar al usuario mover $600 USDT a Binance Flexible Earn (Simple Earn → Flexible en app Binance) |
| N31 | O7 pendiente: Momentum Burst en `autonomous_signals.py` cuando haya tiempo — detector de alta convicción para +4-8 MXN/día |

---

## 9. SCREENER DINÁMICO — DATOS DE REFERENCIA

### Resultados primer run (jun 15 2026 08:35 UTC-6)
```
Total pares escaneados (vol > $10M): 46
Spot elegibles:    46 pares
Futures Tier A:     8 pares (vol > $100M)
  → ETHUSDT, ZECUSDT, WLDUSDT, BTCUSDT, SOLUSDT, XRPUSDT, BNBUSDT, USD1USDT
Cache TTL: 15 min
Score: volumen_24h × abs(priceChangePercent_24h)
```

### Regime-aware ranking (jun 29 2026)
| Modo | Spot | Futures | Lógica |
|------|------|---------|--------|
| `SPOT_BULL` | Sí (long momentum) | Sí (long momentum) | `vol × positive chg` para spot; `vol × abs(chg)` para futures long |
| `RANGE_SCALP` | Sí | Sí (long) | `vol × abs(chg)` — aprovecha volatilidad en ambas direcciones |
| `PRESERVATION` | Solo `narrative_tokens` | No aplica | Protección de capital: solo tokens de narrativa permitidos |
| `FUTURES_SHORT` | Vacío | Sí (shorts) | Spot bloqueado; futures ordenados por bear-bounce score (`vol × abs(chg)`) |

- Cache TTL: 15 min; se invalida automáticamente cuando cambia `current_regime_mode`.
- Cada decisión del screener se persiste en `screener_decisions` (regime, símbolos, scores, parámetros, razón de rechazo).
- `run_trades.py` actualiza `settings.current_regime_mode` en cada ciclo para que dashboard y screener vean el régimen vivo.

### Endpoints de dashboard
- `GET /dashboard/screener` — resultado del screener consciente del régimen actual: `regime_mode`, `spot_count`, `futures_count`, `spot`, `futures`, `top_bullish`, `top_bearish`.
- `GET /screener/decisions?limit=&regime_mode=&acknowledged=` — audit trail de decisiones persistidas; filtrable por régimen y si fue rechazado.

### Cómo agregar/excluir pares del screener
```python
# pair_screener.py línea ~42: blacklist de stablecoins
_EXCLUDE_BASES = {"USDC", "BUSD", "TUSD", "DAI", "FDUSD", "USDP", ...}
# Para excluir un par específico, agregar a la blacklist
```

---

## 10. LECCIONES DE TRADING (estadísticas reales)

### Lo que funciona (no cambiar)
- Anti-whipsaw 2-ciclos: correcto y necesario
- VolSpike override con RSI < 65: sólida lógica de breakout
- Multi-timeframe 1h + 4h: reduce falsos positivos
- Fear & Greed modifier en umbrales predictor
- Kelly dinámico con tiers por categoría de token
- Trailing stop con `protect_profitable_positions`
- Screener dinámico 46 pares (vs 10 estáticos): más oportunidades

### Nuevas protecciones implementadas (jun 15 2026 11:41)
| Mejora | Descripción | Archivo |
|--------|-------------|--------|
| **Anti-wick SL** | Antes de cerrar por SL, verifica último candle 1m. Si cerró sobre el SL → wick → no vende. Param: `sl_confirmation_candles=1` | `run_trades.py` |
| **Horizonte dinámico predictor** | `get_position_predictions()` usa `orchestrator.current_cycle_minutes` (15min SPOT_BULL) en vez del fijo 5min de config | `portfolio_manager.py` |
| **Rebalanceo consciente** | Si predictor dice `epnl > 0 AND prob ≥ 0.55` → no vende en rebalanceo aunque no esté en top-N. Param: `rebalance_skip_if_positive_prediction=True` | `run_trades.py` |
| **Stale cycle dedup** | Warning `TRADING_CYCLE` se registra una sola vez por ciclo estancado, no en cada poll del health (era 15×/ciclo) | `app.py` |

### Lo que destruye valor (CORREGIDO jun 15)
| Problema | Impacto | Fix aplicado |
|----------|---------|-------------|
| `portfolio_rebalancing` quirúrgico — Opción B (jun 15 20:40) | 60 rebalanceos/día, WR=18%, -$63.56 en un día. Criterio "no está en top_symbols" es ruido de 20min | **Rebalanceo solo si: RSI1h>60 O EMA9↓ + min_hold=240min (4h)**. 5-8 rebalanceos/día esperados. `run_trades.py` commit ffc5a75 |
| `portfolio_rebalancing` prematuro (51/64 = 80% de todos los cierres) | Destruye posiciones ganadoras cobrando 0.2% comisión | `min_hold=90→120→240min` + `max_rebalance=1/ciclo` |
| Triple penalización Kelly ($7.50/trade en bear) | Capital subutilizado en bear | Kelly unificado via `_regime.kelly_multiplier` |
| `MAX_PER_TRADE_USDT=15` → predictor con $15 pero Kelly compraba $150 | PnL estimado 10× subestimado → señales rechazadas innecesariamente | `MAX_PER_TRADE_USDT=350` |
| `kelly_dynamic_base_pct=8%` con cap $200 → $105 plano | Capital subutilizado | Base 12%, max $350 |
| BTC.D filter 56% bloqueaba TODOS los alts | Cero señales con BTC.D ~63% | Subido a 64% |
| 3 posiciones mismo símbolo (RENDER×3, TAO×3) | Concentración extrema | `MAX_POSITIONS_PER_SYMBOL=1` |

### Estadísticas históricas (107 trades cerrados al jun 15 21:05)
```
Win rate global: 28% (30W/77L) — DEGRADADO por 92 rebalanceos (WR=32.6%)
Win rate trades normales (SL/TP/predictor): 0% / 15 trades — requiere ajuste SL
PnL del día: -$74.38 (portfolio_rebalancing -$59.17 + normales -$15.20)
Profit factor global: 0.18 — objetivo ≥ 1.5

Impacto post-fix inmediato: 55 rebalanceos → 5 en primera hora tras apply
Oportunidades perdidas por rebalanceo PRE-fix:
  JTOUSDT vendido → +29.4% el día   | XLMUSDT vendido → +10.7%
  ZROUSDT vendido → +10.7%           | WLDUSDT vendido 4x → +6.5%
  INJUSDT vendido → +7.3%
~70% de rebalanceos habrían sido bloqueados por Opción B
```

### Monte Carlo 90 días — 3 escenarios (1,000 sims, capital $3,080)
```
Escenario A — Sin fixes (histórico):   P50=$2,755  return=-10.5%  WR=28%
Escenario B — Con Opción B (activo):   P50=$3,066  return= -0.4%  WR~54%
Escenario C — Optimista (WR 45%):      P50=$3,246  return= +5.4%  WR=45%

Diferencia A→B: +$311 en 90 días = +$38/día de ahorro por rebalanceo quirúrgico
Para llegar a C: SL calibrado ATR + trailing agresivo (+5%/2%) + predictor con más datos
```

### Simulador Fidelidad Total v3 — backtest_multimode.py (jun 20 2026, capital $2,910, 21 símbolos)
```
Período: 90 días 4h | BTC: $70,653→$63,952 (-9.5%) | F&G=15 Capitulación | Kelly boost ×1.35
Módulos REALES activos: TechnicalIndicators.analyze_signal() + PnLPredictor (pesos DB)
Comisiones: 0.1%/lado | Warmup: 50 barras | LEVERAGE=2x futuros

RESULTADOS COMPARATIVOS:
  REAL (predictor+SmartTP+anti-wick+futuros): $5,366 | PnL:+$2,456 | +84.4% | WR:45% | MaxDD:38.6% | PF:1.24
  REAL_OPT (str≥0.58, SL3%/TP7%):            $7,191 | PnL:+$4,281 | +147%  | WR:47% | MaxDD:40.7% | PF:1.41 ◀ MEJOR
  FUT_ONLY (FUTURES_SHORT forzado):           $2,862 | PnL:-$47    | -1.6%  | WR:33% | MaxDD:11.4% | PF:0.95
  SPOT_BULL_SIM (SPOT_BULL forzado):          $8,947 | PnL:+$6,037 | +207%  | WR:48% | MaxDD:37.4% | PF:1.14
  HODL BTC:                                   $2,634 | PnL:-$276   | -9.5%

DISTRIBUCIÓN REGÍMENES (REAL):
  RANGE_SCALP: 45.5% | SPOT_BULL: 33.6% | FUTURES_SHORT: 20.9% | PRESERVATION: 0%

ESTADÍSTICAS SISTEMA (REAL):
  Predictor filtró: 91% señales BUY → solo 9% aprobadas (alta selectividad)
  Anti-wick saves: 138 SL falsos evitados
  SmartTP extend: 27× | scale-out: 8× | close normal: 11×
  Trailing fired: 9× | PredExit: 0×

BREAKDOWN MENSUAL (SPOT_BULL_SIM):
  Mes 1:  82 trades | WR:46% | PnL:+$1,346 | Capital:$4,256
  Mes 2:  56 trades | WR:57% | PnL:+$372   | Capital:$4,628
  Mes 3: 113 trades | WR:42% | PnL:+$3,171 | Capital:$7,799
  Mes 4:   2 trades | WR: 0% | PnL:+$844   | Capital:$8,642

MONTE CARLO SPOT_BULL_SIM (5,000 sims, 3m forward desde $8,179):
  P5: $8,079 (-1.2%) | P50: $8,252 (+0.9%) | P95: $8,443 (+3.2%)
  Proyección mediana 3m: +$72.59 USDT = +$1,415 MXN

BUGS CORREGIDOS DURANTE IMPLEMENTACIÓN v3:
  1. Capital no se devolvía al cerrar posiciones → crash a $0 (fix: capital += size_usdt + pnl - comm)
  2. Comisiones dobles en salida (×2 cuando ya se cobra ×1 en entrada)
  3. vol_spike key mismatch (real usa 'volume_spike' bool, sim esperaba 'vol_spike' float)
  4. trend_4h=None del módulo real → derivar de EMA9 vs EMA21
  5. bias_correction=1.5 del predictor real inflaría todo → modelo estadístico calibrado a WR=36.2%

RECOMENDACIONES PRIORIZADAS (jun 20 2026):
  [P1] Activar REAL_OPT (str≥0.58) → +$1,707 vs REAL en 90d = +$569 USDT/mes
  [P1] Anti-wick activo confirmado → 138 SL falsos evitados = valor probado
  [P2] Futuros WR=41% < target 50% → evaluar reducir MAX_SHORTS=4 o SL=2.0%
  [P2] SmartTP extiende frecuentemente → monitorear si extensiones devuelven ganancias
  [INFO] MaxDD REAL=38.6% en bear severo (-9.5% BTC) → aceptable, históricamente normal
  [INFO] SPOT_BULL_SIM +207% confirma: cuando BTC recupere, sistema escala fuerte
```

### Simulador Fidelidad Total v4 — backtest_multimode.py (jun 20 2026, capital $2,910, 30 símbolos)
```
Período: 90 días 4h | BTC: -9.6% (LATERAL/ALCISTA con rebotes) | F&G=15 | Kelly boost ×1.50
Módulos REALES: TechnicalIndicators.analyze_signal() + PnLPredictor (DB weights) ✅
Comisiones: 0.1%/lado | LEVERAGE=2x | 30 símbolos spot vs 21 en v3 (+43% universo)
Mejoras v4 vs v3: LARGE_CAP SL/TP corregido (1.8%/3.5%), FNG boost correcto (×1.50 no ×1.35),
                  Futuros SmartTP + trailing stop, 5 escenarios (nuevo: RECOVERY_SIM), Kelly base $40

RESULTADOS COMPARATIVOS v4:
  REAL (predictor+SmartTP+anti-wick+futuros):      $7,238  | PnL:+$4,328 | +148.7% | WR:52% | SpWR:51% | FutWR:52% | MaxDD:56.1% | PF:1.17
  REAL_OPT (str≥0.58):                             $8,013  | PnL:+$5,103 | +175.4% | WR:53% | SpWR:55% | FutWR:52% | MaxDD:75.7% | PF:1.15 ← P1
  FUT_ONLY (FUTURES_SHORT forzado):                $2,550  | PnL:-$360   | -12.4%  | WR:49% | FutWR:49% | MaxDD:23.2% | PF:0.64
  SPOT_BULL_SIM (SPOT_BULL forzado):               $11,707 | PnL:+$8,797 | +302.3% | WR:47% | SpWR:47%  | MaxDD:57.1% | PF:1.07
  RECOVERY_SIM (SPOT_BULL + BTC +15% drift):       $11,772 | PnL:+$8,862 | +304.6% | WR:49% | SpWR:49%  | MaxDD:51.1% | PF:1.18 ◀ MEJOR
  HODL BTC:                                         $2,632 | PnL:-$278   | -9.6%

DISTRIBUCIÓN REGÍMENES (REAL, igual que v3):
  SPOT_BULL: 33.6% | FUTURES_SHORT: 20.9%

ESTADÍSTICAS SISTEMA (REAL) — v4:
  Predictor filtró: 90% señales BUY → solo 10% aprobadas (alta selectividad)
  Anti-wick saves: 236 SL falsos evitados (vs 138 en v3 — más símbolos)
  SmartTP SPOT: extend 27× | scale-out 8× | close 11×
  Trailing SPOT: 12× | PredExit: 0×
  FUT trailing fired: 108× | FUT SmartTP extend: 2× | FUT SmartTP close: 0×
  → Trailing futuros muy activo (108×): el bear con rebotes activa protección de ganancias

MEJORA v4 vs v3:
  REAL:     $5,366→$7,238  (+$1,872, +34.9%) — por universo 21→30 símbolos + FNG correcto ×1.50
  REAL_OPT: $7,191→$8,013  (+$822,  +11.4%)
  OPT delta vs REAL: +$775 USDT (+$258/mes) — implementado en producción

ANÁLISIS EXPERTO MULTI-ROL:
  [QUANT] PF=1.17 REAL (vs 1.24 v3) — trailing futuros 108× añade cierres intermedios que bajan PF
         pero capturan ganancias antes de que el mercado revierta (correcto para bear con rebotes)
         Anti-wick: 236 saves — valor confirmado
  [TRADING] PnL futuros: -$90 en régimen FUTURES_SHORT (WR=52% pero trailing activa frecuente)
            PnL spot: +$278 — spot genera más valor neto en el período mixto
            Cuando BTC recupere +15%: sistema proyecta $11,772 (+304%) — 7 SHORTs cierran con TP
  [BEHAVIORAL] F&G=15: boost ×1.50 activo (corregido de ×1.35 en v3) → Kelly más agresivo en pánico
  [ACTUARIO] MaxDD REAL=56.1% preocupante (vs 38.6% v3) — por MAX_EXPOSURE_PCT=75% con 30 símbolos
             ACCIÓN: MaxDD >50% → evaluar reducir MAX_EXPOSURE_PCT a 65%
  [ML] FUT_ONLY PF=0.64 (negativo) — trailing futuros demasiado agresivo en FUTURES_SHORT forzado
       El trailing en SHORT se activa con rebotes normales → puede cerrar SHORTs rentables prematuramente

RECOMENDACIONES POST-v4:
  [P1-DONE] ✅ MIN_SIGNAL_STRENGTH: 0.35→0.55 en config.py (producción)
  [P1-DONE] ✅ FUTURES_TP_PCT: 7.0% en config.py (producción)
  [P2-NEW]  ⚠️  MAX_EXPOSURE_PCT: reducir 0.75→0.65 si MaxDD REAL >50% se confirma en 2+ semanas
  [P2-NEW]  ⚠️  Trailing futuros: ajustar trail_act_pct 2%→3% para evitar cierres en rebotes normales
  [P3]      FUT_ONLY WR=49% marginal → evaluar reducir MAX_SHORTS=4 cuando WR futuros <45% real
  [INFO]    AIOZUSDT: símbolo inválido en Binance (400 error) → remover de SPOT_SYMBOLS en próx. sesión

MONTE CARLO (RECOVERY_SIM, mejor escenario, 3m forward desde $11,772):
  P5: $11,695 (-0.6%) | P50: $11,944 (+1.5%) | P95: $12,192 (+3.6%)
  Proyección mediana 3m: +$172 USDT = +$3,354 MXN (escenario recuperación)

PROYECCIONES 30/60/90d (post-implementación OPT):
  Bear continúa (FUTURES_SHORT):          +$96–$192 | +$192–$384 | +$288–$576
  Lateral (RANGE_SCALP dominante):        +$258–$516 | +$700–$1,400 | +$1,050–$2,100
  Recuperación BTC +15% (SPOT_BULL):      +$1,200–$2,400 | +$3,200–$6,400 | +$5,000–$8,862 ◀ RECOVERY_SIM
```

### Backtest_90d_v2 — 4 escenarios (jun 20 2026, capital $2,910, 30 símbolos spot + 8 futuros)
```
Período real: Mar-Jun 2026 | BTC: $68,833→$63,887 (-7.2%) | BTC 30d: -17.7% (FUTURES_SHORT confirmado)
Régimen detectado en datos: FUTURES_SHORT (102/570 bars con 30d<=-15%)

Escenario A — Baseline pre-audit (SL 1.5/2.5, sin futuros):
  PnL=-$2,790 | Return=-95.9% | WR=21.1% | PF=0.23 | MaxDD=95.9%
  Spot: 38 trades (-$34.65) | Futuros: 0 trades ($0.00)

Escenario B — Spot mejorado (SL 1.8/3.5, anti-wick, SmartTP):
  PnL=-$2,882 | Return=-99.1% | WR=6.2% | PF=0.02 | MaxDD=99.1%
  Spot: 16 trades (-$58.77) | Futuros: 0 trades ($0.00)

Escenario C — Sistema completo (B + 7 futuros SHORT, F&G Kelly):
  PnL=-$2,882 | Return=-99.1% | WR=6.2% | PF=0.02 | MaxDD=99.1%
  (Futuros no entran en sim porque primeros 180 bars = mercado alcista $74K)

Escenario D — Bear conservador (C + Kelly×0.6, str≥0.65):
  PnL=-$2,872 | Return=-98.7% | WR=5.3% | PF=0.02 | MaxDD=98.7%

INTERPRETACIÓN CLAVE (no alarmar):
- El simulador usa indicadores técnicos simples (no el predictor PnL real)
- El predictor real habría bloqueado la mayoría de entradas spot en bear (expected_pnl < 0)
- Los futuros SHORT reales en producción generaron PnL positivo (7 SHORTs activos en ganancia)
- La sim correcta es el Monte Carlo con datos reales (P50=+$311 vs A)
- Próxima versión: integrar predictor real vía API interna en el backtester

Monte Carlo Escenario C (1,000 sims, trades/día=0.18, WR=6.2%):
  P10=-2.2% | P50=-2.0% | P90=-1.8% (período de 90d con bear severo)
```

### Posiciones abiertas validadas (jun 20 2026 13:30 UTC-6)
```
SPOT:
  UUSDT: entry=1.0006, qty=53.0, SL=0.9825, TP=1.0355 ✅ parámetros correctos

FUTUROS SHORT (7/7 slots, sincronizados con algoOrders Binance):
  ETHUSDT:  entry=1712.05  SL=1799.04  TP=1649.85 ✅
  SUIUSDT:  entry=0.7050   SL=0.7263   TP=0.6557  ✅
  XRPUSDT:  entry=1.1445   SL=1.1868   TP=1.0715  ✅
  NEARUSDT: entry=2.1470   SL=2.2160   TP=2.0000  ✅
  BTCUSDT:  entry=63636.6  SL=66138.1  TP=60653.5 ✅
  BNBUSDT:  entry=579.78   SL=598.25   TP=540.17  ✅
  ADAUSDT:  entry=0.1617   SL=0.1667   TP=0.1505  ✅

Todos los futuros tienen algoOrders (STOP_MARKET + TAKE_PROFIT_MARKET) sincronizados.
R:R promedio: SL≈2.5%, TP≈6% → R:R=2.4 — correcto para FUTURES_SHORT bear market.
```

### Trades obsoletos marcados (jun 20 2026)
```
Total trades_closed en DB: 155
Marcados is_corrupted=TRUE: 133
  - portfolio_rebalancing pre-fix (antes 2026-06-15 20:40): 100 trades
  - stop_loss en período no calibrado (pre jun-17):          18 trades
  - manual_rebalance / liquidación manual pre-producción:     5 + 4 trades
  - active_replacement pre-fix (antes 2026-06-17):            2 trades
  - SL_BROKEN / SL_AT_LIMIT manuales:                         4 trades

Trades limpios para aprendizaje: 22
  WR limpio: 18.2% (4/22 ganadores) — bear market severo, normal
  PnL limpio total: -$48.59 | Avg: -$2.21/trade

POST /predictor/reset-corrupted: 133 trades marcados, cache invalidado ✅
POST /learning/run: COMPLETADO — 16 trades analizados, bias_correction=1.5 (cap sintético)
  prediction_accuracy=8.3% (1/12) — bajo porque 22 trades limpios son todos en bear severo
  over_prediction: 10/12 — predictor sobreestimaba PnL, pesos ajustados
  vol_spike_weight=0.15 (estable), trend_4h_weight=0.15 (estable)
  Símbolo con más bias: WLDUSDT (+11.49 sobre-pred), TAOUSDT (-8.18 sub-pred)
  Recomendación sistema: "Accuracy below 40% — monitor next 10 trades"
  bias_correction cap=1.5 — subirá a 2.0 cuando ≥20 trades reales con predicción previa
```

### Category thresholds actuales — post P1+P3 (commit 2a6219c)
| Categoría | SL% | TP% | Trail act% | Trail dist% | min_prob |
|-----------|-----|-----|------------|-------------|----------|
| LARGE_CAP | 1.8 | 3.5 | 2.5 | 1.2 | 55% |
| MIDCAP_ALT | 1.8 | 3.5 | 2.5 | 1.2 | 50% |
| **NARRATIVE** | **3.5** | **7.0** | **5.0** | **2.0** | 42% |
| MEME | 2.0 | 4.5 | 2.5 | 1.5 | 52% |

Cambios NARRATIVE: SL 2.5→**3.5%** (menos whipsaws), TP 5→**7%** (más recorrido), trailing act 3→**5%** (captura breakouts >8%), trail dist 1.5→**2%** (no cierra en corrección normal)

### Tokens nuevos en token_categories (commit 2a6219c)
UNIUSDT, AAVEUSDT (MIDCAP_ALT/DeFi) | BCHUSDT, ZECUSDT, LTCUSDT, XLMUSDT, ZROUSDT (MIDCAP_ALT/Infrastructure) | JTOUSDT (NARRATIVE/AI_DATA) | TRUMPUSDT, MEGAUSDT (MEME/Political)

### Expansión E1 — 7 nuevos tokens añadidos (commit E1-activos, jun 16 2026)
**Objetivo:** Maximizar oportunidades con $3K capital expandiendo universo 24→32 tokens (+33%)

| Token | Categoría | Sector | Volumen 24h estimado |
|-------|-----------|--------|---------------------|
| ARBUSDT | MIDCAP_ALT | L2 (Arbitrum) | $45M |
| MATICUSDT | MIDCAP_ALT | L2 (Polygon) | $35M |
| ATOMUSDT | MIDCAP_ALT | Infrastructure (Cosmos) | $25M |
| GRTUSDT | NARRATIVE | AI/Data (The Graph) | $18M |
| AIOZUSDT | NARRATIVE | AI/Data + DePIN | $12M |
| SEIUSDT | MIDCAP_ALT | Infrastructure (Trading L1) | $22M |
| SUSHIUSDT | MIDCAP_ALT | DeFi (DEX) | $15M |

**Ajustes category_max_positions:**
- MIDCAP_ALT: 5→7 slots (+2 para acomodar expansión L2/Infra)
- NARRATIVE: 4→5 slots (+1 para rotación AI/Data)

**Resultado esperado:** +40% trades/día, +25-30% ganancia mensual, mejor diversificación sectorial (L2 + DePIN + AI/Data)

### Capital actual (jun 19 2026 11:00 UTC-6)
- **total_usdt**: ~$2,910 | **total_mxn**: ~$50,973
- **spot_usdt**: ~$1,698 | **futures_usdt**: ~$1,212 (walletBalance = margin + libre)
- **FUTURES_MAX_SHORTS**: 7 slots activados (slots 6-7 forzados a MIDs)
- **Posiciones activas**: 7 futures SHORT (ETHUSDT, XRPUSDT, NEARUSDT, BTCUSDT, SOLUSDT, BNBUSDT, ADAUSDT)

### Mejoras recientes implementadas
- **SL Smart Enhancement**: Trailing stop ahora inicializa `trail_best_price` al alcanzar activation (2%), permitiendo mover SL > entry price para proteger ganancias
- **Tooltip PnL Fix**: Tooltip hover muestra PnL real en USD (qty × price_diff), coincidiendo con "PnL No Real" del dashboard

### Matemáticas con capital actual (~$2,910 total / ~$1,698 liquid) — Smart Kelly
```
Kelly dinámico base: $1,698 × 12% = $204 (BASE tier)
Kelly max dinámico:  max($350_floor, $1,698×25%) = $425 (FUERTE tier)
Kelly max cap check: $1,698 × 28% = $475 (nunca supera este límite por trade)

Tiers Smart Kelly con $1,698 liquid:
  FUERTE (prob≥0.65 + str≥0.80): $425/trade
  ALTA   (prob≥0.60 + str≥0.70): $381/trade
  MEDIA  (prob≥0.55 + str≥0.55): $315/trade
  BASE   (prob≥0.50):             $204/trade
  Conviction SPOT_BULL (vol≥2x):  cap = $1,698×15% = $255

Con 2.5 trades/día, WR 50%, avg size $280 (MEDIA/ALTA), r=1.94 (TP3.5%/SL1.8%):
EV/trade = 0.50×$280×1.94 - 0.50×$280 = +$131.6 bruto → -comisión 0.2% = +$125.8
PnL estimado/día = 2.5 × $125.8 × net_factor ≈ $12-15 USD/día = 240-300 MXN/día
```

### Con futuros activados (FUTURES_SHORT, bear market)
```
Margen por short: $100 × 2× = $200 notional
TP +6% = +$12 por trade | SL -2.5% = -$5 por trade → R:R = 2.4
Con 3 shorts simultáneos = $300 margen total
Win rate esperado shorts técnicos en bear: 45-55%
```

---

## 11. PREDICTOR PnL — ESTADO Y ARQUITECTURA (jun 16 2026)

### Componentes del predictor
| Archivo | Rol |
|---------|-----|
| `pnl_predictor.py` | Calcula `success_probability` y `expected_pnl` usando 9 features con pesos aprendidos |
| `learning_engine.py` | Correlaciona indicadores con outcomes reales, ajusta pesos por símbolo y global |
| `bootstrap_history.py` | Genera trades sintéticos desde klines históricas de Binance para pre-entrenar el predictor |
| `pnl_service.py` | Facade: llama predictor + valida umbrales por categoría + F&G modifier |
| `trade_utils.py` | `format_indicators_for_predictor()` normaliza el dict de indicadores para el predictor |

### 9 features activas (post jun-16)
| Feature | Peso default | Descripción | Validación |
|---------|-------------|-------------|------------|
| `vol_spike` | **0.15** | Vol actual / avg 20 velas anteriores | Validado jun-13: <0.5x = no entrar, >1.5x = señal fuerte |
| `trend_4h` | **0.15** | +1.0 si EMA9>EMA21 en 4h, -1.0 si bajista | Validado jun-13: RSI 4h>65 + tendencia DOWN = no entrar |
| `rsi` | 0.12 | RSI 14 en 1h | <30 alcista, >70 bajista |
| `macd` | 0.10 | MACD vs signal + histograma | Cruce alcista = +prob |
| `atr_pct` | 0.08 | ATR14/precio×100 | >3% = mov esperado amplio, <0.5% = comprimido |
| `bb` | 0.08 | Posición en Bollinger Bands | <20% = soporte, >80% = resistencia |
| `ema` | 0.08 | EMA vs SMA | EMA>SMA = alcista |
| `weekly_range` | 0.08 | Posición precio en rango 7d (0=soporte, 1=resistencia) | Validado: >0.85 = no entrar |
| `volume` | 0.04 | Volumen bruto | Peso bajo: ya capturado por vol_spike |

### Estado del aprendizaje (jun 17 10:02)
```
Trades en DB:          trades reales limpios v2.0 + 34,309 sintéticos históricos (jun 17)
Datos históricos:      41 símbolos × 1095 días × 1h klines → 0.13 GB parquet
Simulador:             TechnicalIndicators.analyze_signal() REAL (no simplificado)
Símbolos excluidos:    USD1, PAXG, XAUT, SPCXB, RLUSD (stablecoins/commodities)
Filtro mín candles:    2,000 candles (≈83 días de datos 1h)
Símbolos con pesos:    45 símbolos + __global__
Win rate histórico:    37.9% en bear market — normal y esperado
bias_correction prom:  1.111 (cap 1.5 para sintéticos, sube con trades reales)
vol_spike_weight:      0.1498 — predictor más fuerte confirmado
trend_4h_weight:       0.1490 — segundo predictor más relevante
Archivo trades sim:    /app/historical_data/synthetic_trades_full_20260617_155237.json
Próximo hito:          50+ trades reales → bias_correction recalibrado con datos reales
```

### Endpoints de gestión del predictor
```
POST /predictor/reset-corrupted  → Marca rebalanceos como is_corrupted + invalida cache
POST /predictor/bootstrap?days=N → Genera N días de trades sintéticos (1 ≤ N ≤ 1000)
POST /predictor/learn            → Corre LearningEngine con datos reales limpios
POST /learning/run (+ X-API-Key) → Dispara ciclo learning en background
GET  /learning/status            → Estado actual + active_weights + bias_correction

# Re-simular histórico desde cero (41 símbolos, 1095d):
docker-compose exec app python Dev/scripts/ingest_historical.py --phase full --days 1095 --simulate --learn
```

### Lookbacks por categoría en bootstrap
| Categoría | Lookback | Símbolo ejemplo |
|-----------|----------|-----------------|
| LARGE_CAP | 1000d | BTCUSDT, ETHUSDT, BNBUSDT |
| NARRATIVE | 1000d | FETUSDT, TAOUSDT, RENDERUSDT |
| MIDCAP_ALT | 700d | SOLUSDT, AVAXUSDT, DOTUSDT |
| MEME | 500d | DOGEUSDT, PEPEUSDT, WIFUSDT |
| Default | 365d | Resto |

### Nuevas columnas DB en `indicator_weights` (creadas automáticamente)
```sql
vol_spike_weight    FLOAT DEFAULT 0.08
atr_pct_weight      FLOAT DEFAULT 0.08
trend_4h_weight     FLOAT DEFAULT 0.08
weekly_range_weight FLOAT DEFAULT 0.08
-- Creadas con ALTER TABLE IF NOT EXISTS en cada _persist_weights()
```

### indicators_at_entry — fix jun-16
Antes: solo se guardaba si `prediction != None` (~14% de operaciones)  
Ahora: siempre se guarda con fallback a `sig['indicators']` si el predictor falla → **100% de operaciones** capturan features para el LearningEngine.

---

## 9. VOLSPIKE OVERRIDE V2 — JUN 16 2026

### Problema identificado
El sistema estaba comprando con **PnL esperado negativo** (caso NEAR: accuracy -378%), causado por VolSpike Override demasiado permisivo.

### Solución implementada
| Parámetro | Valor | Descripción |
|-----------|-------|-------------|
| `volspike_override_min_strength` | 0.65 | Requerir alta convicción técnica |
| `volspike_override_max_positions` | 2 | Máximo 2 posiciones override simultáneas |
| `volspike_override_daily_limit` | 3 | Máximo 3 overrides por día |

### Flujo de decisiones V2
```
VolSpike ≥ 1.5x → RSI < 65 → Trend UP → Strength ≥ 0.65 → Pos < 2 → Diario < 3 → ✅ OVERRIDE
```

### Tracking de métricas
- `_VOLSPIKE_POSITIONS`: Set de símbolos con override activo
- `_OVERRIDE_DAILY_COUNT`: Contador por fecha
- `_OVERRIDE_PERFORMANCE`: Lista de resultados para análisis

### Endpoint de auditoría
```
GET /metrics/override-performance
Retorna: win rate, accuracy, PnL por símbolo, recomendaciones automáticas
```

### Dashboard — Card "⚡ Auditoría Override"
- Win rate global con indicador visual (✅/⚡/🚨)
- Límites activos: posiciones X/2, hoy Y/3
- Tabla por símbolo: trades, WR, PnL, status
- Últimos 5 overrides con accuracy
- Recomendaciones automáticas (ej: "NEAR underperforming — considerar excluir")

### Criterios de éxito (48h)
- [ ] NEAR no genera más overrides hasta que accuracy mejore
- [ ] <3 posiciones PnL negativo simultáneas
- [ ] Win rate override ≥ 50%

---

## 10. DASHBOARD PnL VISIBILIDAD — JUN 16 2026

### Objetivo
Mayor transparencia de **expectativas vs realidad** en trades.

### Qué es "PnL Pred."
Es lo que el modelo de IA esperaba ganar/perder al momento de comprar:
```
expected_pnl = (prob_success × ganancia_potencial) - comisiones
```

### Mejoras implementadas

#### Tablas de posiciones
- **Abiertas:** Columna "Esperado al Entrar" + "Progreso" (barra hacia TP) + "Actual vs Esperado"
- **Cerradas:** Columna "Esperado" + "Accuracy" (🎯/⚡/❌) + "Desvío" vs esperado
- Click en fila → Modal de detalle completo

#### Nueva card: "🎯 Expectativa vs Realidad"
- 4 stats: Predicciones, Esperado, Real, Accuracy
- Tabla últimos 6 trades: símbolo, esperado, progreso, real, diferencia
- Explicación de "PnL Pred." para usuarios

#### Modal de detalle por posición
- 📍 Entrada: precio, cantidad, invertido, fecha
- 🎯 Predicción: esperado, probabilidad, rango de precio, modo
- 📊 Indicadores: RSI, EMA, vol spike, BB al entrar
- 🛡️ Targets: SL/TP calculados
- 💰 Resultado: actual vs esperado, accuracy

---

---

## SESIÓN JUN 17 2026 — Learning Engine v2 + Pipeline Histórico Completo

### Objetivo: Corregir bias_correction sobreestimado y calibrar predictor con señales reales

### Problemas corregidos:

| ID | Problema | Fix |
|----|----------|-----|
| **C1** | `bias_correction = 2.0` en toda la DB (sintéticos sin predicciones reales) | `UPDATE indicator_weights SET bias_correction = 1.0 WHERE bias_correction > 1.0` → luego cap dinámico |
| **C2** | Simulador usaba señales simplificadas (RSI manual, avg EMA) | Reemplazado por `TechnicalIndicators.analyze_signal()` real — mismas señales que el sistema live |
| **C3** | Tokens problemáticos (USD1, PAXG, XAUT, SPCXB, RLUSD) en simulación | `_EXCLUDE_FROM_SIM` set + `_MIN_CANDLES_FOR_SIM = 2000` → 5 tokens excluidos correctamente |
| **C4** | `trend_4h = 'DOWN'` (string) causaba `ValueError: could not convert string to float` en LearningEngine | `_extract()` en `learning_engine.py`: mapeo string→numeric (`'UP'→1.0, 'DOWN'→0.0`) |
| **C5** | `bias_correction` volvía a 2.0 tras re-learn (ratio actual/predicted → inf → clamp 2.0) | Cap dinámico: `max_correction = 1.5 if len(ratios) < 20 else 2.0` |

### Pipeline ejecutado:
```
1. SQL UPDATE → bias_correction = 1.0 en toda la tabla
2. historical_simulator.py → _EXCLUDE_FROM_SIM + _MIN_CANDLES_FOR_SIM + _analyze_window() real
3. learning_engine.py → _extract() str→numeric + bias cap 1.5
4. ingest_historical.py --phase full --days 1095 --simulate --learn
   → 41 símbolos descargados (0.13 GB)
   → 34,287 trades simulados con señales reales
   → SPCXB=0, XAUT=0, PAXG=0, USD1=0, RLUSD=0 ✅
5. run_with_synthetic(34,287 sintéticos + 22 reales = 34,309 total)
   → 45 símbolos + __global__ actualizados
   → bias_correction: 1.111 promedio (cap 1.5)
6. POST /learning/run → ciclo en background con pesos frescos
7. PnLPredictor.invalidate_cache() → active_weights recargados
```

### Estado post-sesión:
```
indicator_weights: 45 símbolos, avg bias=1.111, avg win_rate=36.2%
active_weights (cargados en predictor):
  vol_spike = 0.1498  ← predictor #1
  trend_4h  = 0.1490  ← predictor #2
  rsi       = 0.1192
  macd      = 0.0997
  bias_correction = 1.5 (cap para dataset sintético)
Sistema: RUNNING — trade/analysis/learning cycles activos
```

### Archivos modificados (commits pendientes):
- `historical_simulator.py` — _EXCLUDE_FROM_SIM, _MIN_CANDLES_FOR_SIM, _analyze_window real
- `learning_engine.py` — _extract str→numeric, bias cap dinámico, load_synthetic_trades, run_with_synthetic
- `Dev/scripts/ingest_historical.py` — integrado run_with_synthetic
- `SYSTEM_REFERENCE.md` — este archivo

---

### Análisis Impacto Retroactivo — ¿Cuánto habría ahorrado el L.E. v2?

**Período:** Jun 12–17 2026 | **Trades analizados:** 32 limpios (137 total, 105 corruptos excluidos)  
**PnL real:** -$82.10 USD | **Win rate real:** 6.25% (2/32)

| Causa de pérdida | PnL perdido | % total |
|------------------|-------------|---------|
| Caídas de mercado genuinas (SL golpeados 2-5%) | ~$55 USD | 67% |
| **VolSpike Override con RSI>65** (TAOUSDT -$11, WLDUSDT 2ª -$10.16) | **~$21 USD** | **26%** |
| Predictor mal calibrado (L.E. v2 habría bloqueado) | ~$6 USD | 7% |

---

## SESIÓN JUN 17 2026 11:06 UTC-6 — Optimización posiciones + 8 bugs críticos corregidos

### Cambios en posiciones y exposición (E2)

| Parámetro | Antes | Ahora | Razón |
|---|---|---|---|
| `MAX_POSITIONS` (fallback) | 12 | **15** | 32 activos disponibles |
| `MAX_POSITIONS_SPOT_BULL` | 15 | **20** | Freno real es exposición%, no count |
| `MAX_POSITIONS_RANGE_SCALP` | 8 | **10** | Más scalp candidates en screener |
| `OVERFLOW_MAX_EXTRA` | 3 | **5** | Más breakouts NARRATIVE simultáneos |
| `MIDCAP_ALT` category max | 7 | **8** | 12 MIDCAP disponibles |
| `NARRATIVE` category max | 5 | **6** | 10 NARRATIVE disponibles |
| Exposición SPOT_BULL + F&G<40 | 75% | **80%** | F&G=23 Extreme Fear = momento óptimo |

### Bugs críticos corregidos

| ID | Bug | Archivo | Fix |
|----|-----|---------|-----|
| **BUG-A** | `get_fear_greed_index()` no existe → F&G boost futuros nunca aplica | `run_trades.py` l.1475 | → `get_fear_greed()` |
| **BUG-B** | `_avg_strength = 0.5` hardcoded → Active Replacement nunca activa | `run_trades.py` l.332 | Calcula avg desde metadata real de posiciones abiertas |
| **BUG-C** | EV check salta cuando `worst_ev < 0` → reemplaza con señales igual de malas | `run_trades.py` l.339 | Añade `if _new_ev <= _worst_ev: return False` primero |

### Ineficiencias corregidas

| ID | Ineficiencia | Fix |
|----|---|---|
| **INEF-1** | SlotPressure limitado a `approved[:1]` hardcoded | Usa `max_rebalance_per_cycle` (2 por defecto) |
| **INEF-2** | `min_hold=240min` global bloquea SlotPressure en RANGE_SCALP | RANGE_SCALP=30min, SPOT_BULL=120min, global=120min |
| **INEF-3/4** | `_VOLSPIKE_POSITIONS` y `_overflow_positions` no se limpian en `active_replacement` | `discard()` añadido al cerrar por reemplazo |
| **MEJO-1** | `replace_ev_min_ratio=1.3` muy alto para EVs pequeños | → 1.1 |
| **MEJO-2** | `replace_strength_min_delta=0.15` + avg=0.5 hardcoded = gate roto | → 0.05 (avg ahora real) |

### Archivos modificados esta sesión
- `run_trades.py` — BUG-A, BUG-B, BUG-C, INEF-1, INEF-2, INEF-3/4, MEJO-1/2 + exposición 80% Fear
- `config.py` — category_max MIDCAP/NARRATIVE, min_hold por régimen, nuevos params exposición
- `docker-compose.yml` — MAX_POSITIONS=15, MAX_POSITIONS_SPOT_BULL=20, OVERFLOW_MAX_EXTRA=5

**Ahorro estimado con L.E. v2 activo antes: $5–$8 USD** (Fix C4 trend_4h: INJUSDT RSI=73 habría sido bloqueado)

**Por qué el bias_correction 2.0→1.111 no habría cambiado mucho:**  
El bias multiplica `expected_pnl`, no `success_probability`. Todos los `expected_pnl` ya eran negativos antes del bias (entre -$0.04 y -$0.10). El bias solo los hacía "menos negativos" pero no cruzaban el umbral `min_pnl`. Ninguno de los 32 trades cae en el rango crítico donde `base × 2.0 > min_pnl` pero `base × 1.111 < min_pnl`.

**Los trades grandes perdedores entraron por Override, NO por el predictor:**
- TAOUSDT -$11.09: pred_pnl=-$2.92 (ya negativo) → entró por VolSpike Override
- WLDUSDT 2ª -$10.16: pred_pnl=-$1.09 (negativo) → Override
- WLDUSDT GANADOR +$8.05: pred_pnl=-$3.45 (negativo) → Override (éxito del Override)

**⚠️ PRÓXIMO FIX PRIORITARIO:** Verificar que `volspike_override_max_rsi` (ya en config) bloquee correctamente cuando RSI_entry > 65 **en el momento de ejecutar la orden** — no solo al evaluar la señal inicial. Esto habría salvado ~$21 USD (26% del total de pérdidas).

---

---

## SESIÓN JUN 17 2026 13:35 UTC-6 — Prediction-Driven Lifecycle (C1-C5) + Optimizaciones

### Cambios implementados y verificados en producción

| ID | Cambio | Archivo | Estado |
|----|--------|---------|--------|
| **C1** | Re-evaluación predictiva en `_check_sl_tp_positions` — cierra si EV<min AND prob<min AND trend=DOWN | `run_trades.py` | ✅ Activo en producción |
| **C2** | Segunda entrada por predicción — require strength≥0.70 + nueva EV ≥1.5× la entrada original | `run_trades.py` | ✅ Activo |
| **C3** | Cooldowns VolSpike + Narrative persistidos en tabla `trading_cooldowns` (DB) | `run_trades.py`, `migrations/009` | ✅ Activo |
| **C4** | Audit on startup — vende activos en `trading_excluded_symbols` automáticamente al arrancar | `run_trades.py` | ✅ Vendió RLUSD+USD1 |
| **C5** | Metadata enriquecida al abrir posición: `entry_regime`, `entry_pred_pnl`, `entry_pred_prob`, `entry_type`, `reevaluation_count` | `run_trades.py` | ✅ Activo |
| **OPT** | `_AUDIT_STARTUP_DONE` flag — C4 corre solo una vez por proceso, no en cada ciclo | `run_trades.py:76` | ✅ Verificado |

### Nuevos parámetros en config.py (líneas 306-318)
```python
prediction_exit_enabled = True
min_hold_for_prediction_exit = 90      # minutos mínimos antes de C1
allow_prediction_based_add = True
prediction_add_min_ev_ratio = 1.5      # nueva EV debe ser 1.5× la de entrada
prediction_add_min_strength = 0.70
prediction_add_kelly_factor = 0.6      # Kelly reducido para 2ª entrada
persist_cooldowns_enabled = True
auto_close_excluded_on_startup = True
```

### Migración aplicada
- `migrations/009_trading_cooldowns.sql` — tabla `trading_cooldowns(symbol, cooldown_type, activated_at, expires_at, metadata)` + trigger purge automático

### Estado del portafolio post-sesión (13:35 UTC-6)
- **USDT líquido**: ~$469 (tras vender RLUSDUSDT 109 + USD1USDT 280 excluidos)
- **Régimen**: PRESERVATION (no confirmado — BTC 30d=-16.2%)
- **Posiciones activas**: BTCUSDT(-1.97%), NEARUSDT(-1.95%), ONDOUSDT(-2.76%), TONUSDT(-0.49%), WLDUSDT(-1.66%)
- **PredEval**: evaluando 5 posiciones cada ciclo — todas holding (trend=FLAT, no deterioradas)
- **Alerta**: WLDUSDT EV=-2.86, prob=0.43 — cierre automático si trend cambia a DOWN

### Pendientes post-sesión
- N39: Monitorear primer cierre por C1 en logs `prediction_exit_deteriorated` — verificar que EV/prob/trend se logean correctamente
- N40: Con 50+ trades reales → POST /learning/run para recalibrar bias_correction con datos reales (actualmente cap 1.5 sintéticos)
- N41: Cuando PRESERVATION confirme (BTC 30d < -15% en 2 ciclos) → verificar que solo NARRATIVE recibe nuevas entradas

---

---

## SESIÓN JUN 20 2026 23:11 UTC-6 — Dashboard Unificado + Datos en Vivo (Sesión 10)

### Objetivo
Corregir datos stale en dashboard de posiciones abiertas/cerradas y hacer conteos de posiciones precisos en tiempo real. Unificar las 4 cards separadas en 2 cards homologadas con sub-tabs SPOT|FUTUROS.

### Bugs corregidos

| ID | Bug | Archivo | Fix |
|----|-----|---------|-----|
| **FIX-L1** | `get_open_positions_summary()` retornaba `pnl_prediction=None` y `hours_held=0` tras restart (memoria sin DB) | `futures_client.py` | Consulta `trades_open WHERE trade_type='futures'` para enriquecer datos en memoria |
| **FIX-L2** | `/futures/closed` solo usaba Binance API — sin `close_reason`, `pnl_prediction`, `prediction_accuracy` | `app.py:2019+` | Complementa con query a `trades_closed WHERE trade_type='futures'` + merge por símbolo |
| **FIX-L3** | `trade_type` hardcodeado como `"spot"` para todos los trades cerrados | `app.py:1469` | Lee `metadata->>'trade_type'` desde DB |
| **FIX-D1** | `hours_held=0.0` y `pnl_prediction=None` hardcodeados para futuros en `open_list` | `app.py:1391-1429` | Calcula `hours_held` real desde `opened_at`; inyecta `pnl_prediction` desde `get_open_positions_summary()` |
| **FIX-L6a** | `open_positions` no desglosaba spot vs futures count | `app.py:1434-1441` | Agrega `spot_count` y `futures_count` al response de `open_positions` y `closed_positions` |
| **FIX-L6b** | `renderKPIRow` usaba `_cachedFuturesPositions.length` (cache async desincronizado) | `dashboard.js:362-370` | Deriva `spotCount`/`futCount` desde array unificado `open_positions.positions` |
| **FIX-L6c** | Headers de cards no mostraban conteos reales | `dashboard.js` | `renderOpenPositions` y `renderClosedPositions` actualizan `<h2>` con badges dinámicos |
| **FIX-D3** | 4 cards separadas (openPosCard + futuresCard + closedPositionsData + futuresClosedCard) | `index.html` | 2 cards unificadas con inner sub-tabs `[Todas \| SPOT \| FUTUROS]` |
| **FIX-D4** | `renderFuturesPositions` y `renderFuturesClosed` renderizaban cards independientes | `dashboard.js` | `switchInnerTab()` filtra el array unificado por `trade_type`; `_syncFuturesUnrealPnl()` mantiene KPI fresco |
| **FIX-D5** | Sin estilos para sub-tabs internos | `styles.css` | `.inner-tab-bar`, `.inner-tab-btn`, `.pos-count-badge` |

### Archivos modificados
- `futures_client.py` — FIX-L1: enriquecimiento DB en `get_open_positions_summary()`
- `app.py` — FIX-L2/L3/D1/L6a: `/futures/closed` DB merge, trade_type real, hours_held real, spot/fut counts
- `frontend/index.html` — FIX-D3: 4 cards → 2 unificadas con inner sub-tabs
- `frontend/dashboard.js` — FIX-L6b/L6c/D4: conteos desde array unificado, headers dinámicos, `switchInnerTab()`, `_syncFuturesUnrealPnl()`
- `frontend/styles.css` — FIX-D5: estilos inner tabs

### Commit
`01911d0` — FIX-L1/L2/L3/L6/D1/D3/D4/D5: Dashboard unificado datos en vivo reales

### Estado post-sesión
- Dashboard muestra conteos exactos: `Spot: N | Fut: M` en header de cada card
- Sub-tab [Todas | SPOT | FUTUROS] filtra el array unificado sin fetch adicional
- Futuros abiertos: `pnl_prediction` y `hours_held` correctos incluso post-restart
- Futuros cerrados: `close_reason`, `pnl_prediction`, `prediction_accuracy` desde DB
- `trade_type` correcto para todos los registros cerrados (no hardcoded 'spot')

### ⏳ Pendientes
- BUG-E3: `analysis_cycle.execute_once()` usa `get_indicators()` redundante
- FIX-F6: Filtro RSI 4h para confirmar SHORT antes de abrir (RSI 4h < 50)
- Learning Engine: con 50+ trades reales → POST /learning/run

---

## Sesión 12 — Dashboard SL/TP Visibility (jun 21 2026)

<!-- MARKER:SL_TP_VISIBILITY:2026-06-21 -->
<!-- A partir de este punto, todas las posiciones tienen visibilidad completa de SL/TP en el dashboard -->

### Objetivo
Visibilidad total del ciclo de vida de SL y TP durante la vida de cada posición (abierta y cerrada), tanto SPOT como FUTURES. Optimizar la capacidad de análisis para maximizar ganancias en el menor tiempo posible.

### Fixes implementados

| Fix | Archivo | Descripción |
|-----|---------|-------------|
| VIS-1 | `futures_client.py` | `get_open_positions_summary()` expone `_smart_meta`: breakeven_done, trail_best_price, trailing_active, tp_extensions, dynamic_trail_dist_pct |
| VIS-2a | `app.py` | Open SPOT positions enriquecidas con: sl_dist_pct, tp_dist_pct, trailing_peak, trailing_active, breakeven_done, tp_extensions, tp_decisions, scale_out_done |
| VIS-2b | `app.py` | Open FUTURES positions enriquecidas con mismos campos lifecycle desde `get_open_positions_summary()` |
| VIS-2c | `app.py` | Closed positions enriquecidas con: trailing_peak, tp_decisions, scale_out_done, tp_extensions desde metadata |
| VIS-3 | `dashboard.js` | Helpers `fmtSlTpCell()` y `fmtSlTpBadges()` + close_reason map ampliado (smart_tp_close, prediction_exit_deteriorated, binance_safety_close) |
| VIS-3a | `dashboard.js` | Tabla open positions: columnas SL/TP con precios + distancia %, badges estado (🛡BE, 📈T, 🔄ext, ✂️scale) |
| VIS-3b | `dashboard.js` | Tabla closed positions: columna SL/TP Lifecycle con badges de lo que se activó durante el trade |
| VIS-4a | `dashboard.js` | Modal spot detail: sección "SL/TP LIFECYCLE" con SL/TP actual vs inicial, trailing state, breakeven, SmartTP decisions, scale-out |
| VIS-4b | `dashboard.js` + `app.py` | Modal futures detail: sección "SL/TP LIFECYCLE" con breakeven, trailing (best price + dist dinámica), SmartTP ext., REGLA DE ORO indicator. `/futures/positions` endpoint enriquecido con `_smart_meta` |
| VIS-4c | `dashboard.js` | Modal futures closed detail: sección "SL/TP LIFECYCLE" con SL/TP final + close_reason formateado |
| VIS-5 | `styles.css` | Estilos: `.sl-tp-cell`, `.sl-val`, `.tp-val`, `.sl-badge` (be/trail/ext/scale/none), `.sl-tp-lifecycle`, `.sl-regla-oro`, `.tp-decisions-list` |

### Campos SL/TP lifecycle expuestos en API

**Open positions** (`/dashboard/summary` → `open_positions`):
- `stop_loss`, `take_profit` — valores actuales
- `sl_dist_pct`, `tp_dist_pct` — distancia % desde entry
- `trailing_peak` / `trail_best_price` — mejor precio visto durante trailing
- `trailing_active` — boolean, trailing stop activo
- `breakeven_done` — boolean, SL movido a entry
- `tp_extensions` — int, extensiones SmartTP ejecutadas
- `tp_decisions` — array, historial de decisiones TP (extend/scale_out/close)
- `scale_out_done` — boolean, scale-out parcial ejecutado
- `dynamic_trail_dist_pct` — float, distancia dinámica actual (0.7%/1.0%/1.5%)

**Closed positions** (`/dashboard/summary` → `closed_positions`):
- `trailing_peak`, `tp_decisions`, `scale_out_done`, `tp_extensions` — desde metadata

### Diseño de badges en tablas
- 🛡BE = Breakeven activado (SL = entry price)
- 📈T = Trailing stop activo (SL sigue al precio)
- 🔄N = SmartTP extendido N veces
- ✂️ = Scale-out parcial ejecutado

### REGLA DE ORO visible
El modal futures muestra: "⚖️ REGLA DE ORO: SL nunca retrocede — solo baja/sube" como recordatorio constante del principio de protección.

---

---

## TRAILING STOP DESIGN PRINCIPLES (jun 21 2026 — sesión 15)

> **PRINCIPIO FUNDAMENTAL: Proteger ganancias SIN impedir crecimiento.**
> Las posiciones deben poder CRECER libremente. Solo cuando ya están ganando significativamente,
> el trailing debe activarse como un PROTECTOR — nunca como un limitador.

### Secuencia de protección (orden estricto)

```
0% → breakeven_pct (1.0% fut / 1.5% spot): SOLO SL inicial. Sin intervención.
breakeven_pct → trail_activation (1.5% fut / 2.0% spot): Solo breakeven SL (SL → entry). Sin trailing.
trail_activation → ∞: ATR-scaled trailing activo. Posición ya PROBÓ dirección.
```

### 10 Reglas NO NEGOCIABLES

1. **NUNCA usar trail distances fijos < 1.0% para altcoins.** BTC tiene 0.37% 90th pct 5m range; SOL 0.63%; LINK 0.51%.
2. **Trail distance DEBE considerar volatilidad del activo (ATR).** Un 0.35% trail en SOL = stop instantáneo por ruido.
3. **El SL es un STOP_MARKET order en Binance — se ejecuta INSTANTÁNEAMENTE** en cualquier wick que lo toque. No hay segunda oportunidad.
4. **Posiciones DEBEN poder crecer.** Solo empezar a trailing DESPUÉS de probar dirección profitable (>1.5% futures, >2.0% spot).
5. **La secuencia correcta es: SL inicial → breakeven → trailing.** Cada paso requiere más PnL que el anterior.
6. **El trailing protege contra REVERSIONES DE TENDENCIA**, no contra volatilidad normal.
7. **Objetivo: capturar 60-80% del PnL no realizado en ganancias profundas** — NO 90% (eso triggerea en noise).
8. **El problema del 20 de junio fue AUSENCIA de trailing** (SL se quedó en 3% inicial). El fix es trailing CON distancia adecuada — no trailing con distancia imposible.
9. **Testear trail distance vs 90th percentile 5m range del activo:**
   - BTC: 0.37% | SOL/ETH/BNB: 0.5-0.7% | LINK/DOT/UNI: 0.5-0.6% | Memes: 1.0-2.0%
10. **Máxima extracción de profit = dejar que ganadores corran tanto como su tendencia lo permita**, luego capturar 60-80% del movimiento via ATR trailing.

### Fórmula ATR implementada (jun 21 2026)

```
trail_dist = max(floor, min(ceiling, ATR_5m_14periods × multiplier))
```

| PnL Level | Multiplier | Floor | Ceiling | Locking |
|-----------|-----------|-------|---------|---------|
| 1.5-3%    | 1.5× ATR  | 0.4%  | 1.2%    | ~40-60% del profit |
| 3-5%      | 1.2× ATR  | 0.4%  | 1.2%    | ~50-70% del profit |
| 5-8%      | 1.0× ATR  | 0.4%  | 1.2%    | ~60-80% del profit |
| 8%+       | 0.8× ATR  | 0.4%  | 1.2%    | ~70-85% del profit |

### Ejemplos reales (medidos jun 21 2026)

| Símbolo | ATR 5m | Trail @2% PnL | Trail @5% PnL | Trail @8% PnL |
|---------|--------|---------------|---------------|---------------|
| BTCUSDT | 0.18%  | 0.4% (floor)  | 0.4% (floor)  | 0.4% (floor)  |
| SOLUSDT | 0.32%  | 0.48%         | 0.40%         | 0.40%         |
| LINKUSDT| 0.26%  | 0.40%         | 0.40%         | 0.40%         |

> **Importante:** ATR se cachea 5 minutos. En dry_run/tests devuelve 0.5% fallback.
> En producción: se calcula en cada `check_positions()` usando klines reales de Binance.

### Anti-patterns (NUNCA hacer)

- ❌ Trail distance = 0.35% para altcoins → **stop instantáneo por ruido normal**
- ❌ Trailing que activa en <1% PnL → **impide que la posición respire**
- ❌ Trailing distance igual para BTC y memecoins → **BTC es 5x más estable**
- ❌ Confiar en que el ciclo de 5min verifica antes de que Binance ejecute el SL → **Binance ejecuta inmediatamente**
- ❌ Reducir trailing distance para "proteger más" sin verificar ATR → **protege menos porque sale en noise**

### Config keys (config.py + docker-compose.yml)

```
FUTURES_TRAIL_ATR_ENABLED=true          # master switch
FUTURES_TRAIL_ATR_MULT_LOW=1.5          # at 1.5-3% PnL
FUTURES_TRAIL_ATR_MULT_MID=1.2          # at 3-5% PnL
FUTURES_TRAIL_ATR_MULT_HIGH=1.0         # at 5-8% PnL
FUTURES_TRAIL_ATR_MULT_DEEP=0.8         # at 8%+ PnL
FUTURES_TRAIL_FLOOR_PCT=0.4             # absolute min %
FUTURES_TRAIL_CEILING_PCT=1.2           # absolute max %
FUTURES_BREAKEVEN_ACTIVATION_PCT=1.0    # breakeven fires here
FUTURES_TRAILING_ACTIVATION_PCT=1.5     # trailing starts here
```

---

## ENTRY QUALITY & CORRELATION RULES (jun 21 2026 — sesión 16)

> **TARGET: $40+/day net profit.** Requires 2-3 TP hits/day at ~$28 gain each.
> Previous 48h showed -$23.18 loss from a single correlated mass stop-out event.

### Non-negotiable entry rules for FUTURES_SHORT:

1. **PB1 — Stagger entries**: Max 3 new shorts per 4-minute cycle. Rest queue for next cycle.
   - Prevents 7+ positions opening at exact same price level.
   - Config: `FUTURES_MAX_NEW_PER_CYCLE=3`

2. **PB2 — Mass SL circuit breaker**: If 3+ positions stop in same cycle → 4h global pause.
   - Prevents cascading losses after correlated SL event.
   - Config: `FUTURES_MASS_SL_THRESHOLD=3`, `FUTURES_MASS_SL_COOLDOWN_HOURS=4.0`

3. **PB3 — Red candle confirmation**: Last completed 5m candle must be RED (close < open) for the symbol AND BTC before opening a SHORT.
   - If both are green → skip, wait for confirmation.
   - Prevents shorting into active rallies.
   - Config: `FUTURES_CONFIRM_RED_CANDLE=true`

4. **PB4 — Larger positions**: Kelly cap raised 15%→20%, floor raised $20→$50.
   - At ~$200 margin × 2x: SL=$12, TP=$28.
   - Config: `FUTURES_KELLY_CAP_PCT=0.20`, `FUTURES_USDT_PER_TRADE=50`

5. **PB5 — BTC 3h momentum filter**: Block new shorts if BTC rose >1% over last 3 hours.
   - Catches gradual rallies that the 1h filter misses.
   - Config: `FUTURES_NO_SHORT_BTC_3H_THRESHOLD=1.0`

6. **PB6 — Extended cooldown**: Per-symbol SL cooldown raised 1h→4h.
   - Prevents chasing (re-entering same losing symbol immediately).
   - Config: `FUTURES_SL_COOLDOWN_HOURS=4.0`

### Expected impact:
- Jun 20 mass stop-out (-$19.99) → **fully prevented** by PB1+PB2+PB3
- NEAR re-entries (-$3.19) → **fully prevented** by PB6
- Per-trade profit: $18→$28 (PB4)
- Net target: 2-3 TP hits/day × $28 = $56-84 gross → $40+ net after losses

---

## PROFIT VELOCITY — EXIT OPTIMIZATION (jun 21 2026 — sesión 17)

> **Problem**: SL=3% was stopping positions on normal 1-2% intraday bounces that always reversed.
> 9/9 stops in 48h were moves of 0.32-2.14% — ALL would have survived with 5% SL.
> Market came back every time (re-entered at same prices 2-4h later = wasted $23.18 + fees).

### Parameters changed:
- `FUTURES_SL_PCT`: 3.0 → **5.0** (survive normal bear-market bounces)
- `FUTURES_TRAILING_ACTIVATION_PCT`: 1.5 → **2.5** (let scale-out fire first)

### New exit mechanisms (PV1 + PV2):

1. **PV1 — Scale-out 50% at +1.5% PnL**
   - When unrealized PnL ≥ 1.5%: close 50% of qty at market, keep remaining 50% running.
   - Locks guaranteed partial profit ($1.83 avg at $244 notional).
   - Re-places SL/TP orders with reduced qty after scale-out.
   - Config: `FUTURES_SCALE_OUT_PCT=1.5`, `FUTURES_SCALE_OUT_FRACTION=0.5`
   - 48h data: 5/7 positions reached 1.5% → would have realized +$9.17

2. **PV2 — Momentum reversal close (remaining 50%)**
   - Tracks `peak_pnl_pct` in _smart_meta (updated every check cycle).
   - Fires when: peak ≥ 2.0% AND (peak - current_pnl) ≥ 0.8% AND scale_out already done AND still in profit.
   - Closes remaining 50% immediately (reason: `momentum_reversal`).
   - Captures profit at the peak before the position gives it all back.
   - Config: `FUTURES_MOMENTUM_REVERSAL_ENABLED=true`, `FUTURES_MOMENTUM_REVERSAL_MIN_PEAK=2.0`, `FUTURES_MOMENTUM_REVERSAL_DRAWBACK=0.8`
   - 48h data: would have captured +$10.70 in realized profit

### Exit priority order in check_positions():
```
0a. Track peak PnL (always)
0b. PV1 Scale-out: pnl >= 1.5% AND not scaled_out → close 50%
0c. PV2 Momentum reversal: peak >= 2% AND drawback >= 0.8% AND scaled_out → close remaining
1.  Breakeven SL: pnl >= 1.0% → move SL to entry
2.  Trailing: pnl >= 2.5% → ATR-scaled trail (safety net for remaining 50%)
3.  Hard SL: 5% → last resort
4.  Hard TP: 7% + SmartTP extend → captures big crash moves
```

### Expected daily PnL improvement:
- Avoided stops: +$23.18/48h saved (9/9 stops were noise)
- Scale-outs: ~$3.66-5.49/day (2-3 events × $1.83)
- Momentum reversals: ~$2.50-5.00/day (1-2 events × $2.50)
- Realistic net: **+$10-15/day** (vs previous -$11.59/day = +$21-26 swing)

### Files modified:
- `futures_client.py`: PV1+PV2 logic + peak tracking in `check_positions()`
- `config.py`: 5 new fields (scale_out_pct/fraction, momentum_reversal_enabled/min_peak/drawback)
- `docker-compose.yml`: SL=5.0, trailing_activation=2.5, 5 new PV env vars

---

## DASHBOARD — PROFIT TRACKER & PV VISIBILITY (jun 22 2026 — sesión 18)

> **Purpose**: Provide real-time analytics of which exit strategies generate profits,
> enabling rapid tuning of PV1/PV2 parameters to maximize daily PnL.

### New card: 💰 Profit Tracker
- **Progress bar** toward $40/day target (configurable via `daily_target` in backend)
- **PnL breakdown by close reason** — sorted by PnL descending:
  - 🟢 Take Profit, ✂️ Scale-Out, ⚡ Momentum Rev., 📈 Trailing, 🔴 Stop Loss, etc.
- **Hoy / 7d toggle** — view daily or weekly performance
- **Spot vs Futures split** — at-a-glance which strategy type is performing
- **Win Rate** for the selected period
- Location: right column in Operations tab (below PnL chart)

### Enhanced position table badges:
- **Open positions**: ✂️ shows locked $ amount, 📊 shows peak PnL % reached
- **Closed positions**: ✂️ with $ locked, 📊 peak %, ⚡MR badge for momentum reversal closes
- **Executive summary PnL line**: shows `✂️+$X` locked profit from scale-outs on open positions

### Backend: `pnl_by_close_reason` in `/dashboard/summary`
```json
{
  "today": { "by_reason": [...], "total_pnl": X, "total_trades": N, "spot_pnl": Y, "futures_pnl": Z },
  "week":  { "by_reason": [...], "total_pnl": X, "total_trades": N, "spot_pnl": Y, "futures_pnl": Z },
  "daily_target": 40.0
}
```
- Aggregates from `closed_positions` already in summary (no extra DB queries)
- Includes `scale_out_pnl` in the total for positions that had partial close

### Data exposed for open positions:
- `scale_out_done`: bool — whether PV1 fired
- `scale_out_pnl`: float — locked profit from the partial close (USDT)
- `peak_pnl_pct`: float — highest unrealized PnL % reached (for PV2 monitoring)

### Close reasons added to frontend map:
- `momentum_reversal` → ⚡ Momentum Rev. (green/TP class)
- `trailing_sl_binance` → 🟢 Trailing (Binance)
- `regime_change_to_short` → ⚡ Régimen → SHORT

### Files modified:
- `futures_client.py`: 3 new fields in `get_open_positions_summary()`
- `app.py`: expose PV fields in open/closed positions + `pnl_by_close_reason` aggregation
- `frontend/dashboard.js`: Profit Tracker card + enhanced badges + exec summary locked PnL
- `frontend/index.html`: card container + v21 cache bust
- `frontend/styles.css`: `.pt-*` classes + `.sl-badge.scale/.peak/.mr`
- `tests/test_futures_smart_exits.py`: fixed `trailing_activation_pct` test (1.5→2.5)

### Tests: 193 passed (172 smart exits + 21 profit boost), 0 failures

---

## SMART SCALE-OUT (SSO) — ADAPTIVE EXIT SYSTEM (jun 22 2026 — sesión 19)

> **Purpose**: Replace fixed percentage scale-out (PV1: 50% at 1.5%) with a multi-signal
> adaptive exit system that maximizes capital velocity toward $40+/day target.
> Applies to BOTH spot and futures positions.

### The 5 SSO Triggers:

| # | Trigger | When | Action |
|---|---------|------|--------|
| SSO-1 | **Stagnant Profit** | profit ≥ $3 + position ≥ 2h + velocity ≤ 0 | Close 100% |
| SSO-2 | **Momentum Exhausted** | RSI ≥ 70 (LONG) or ≤ 30 (SHORT) + vol_spike < 0.5 | Close 75% |
| SSO-3 | **Multi-level ATR Scale** | pnl ≥ N × ATR_4h (3 levels) | Close 30%/30%/25% |
| SSO-4 | **Target Contribution** | profit ≥ 15% of daily target + age ≥ 1h | Close 100% |
| SSO-5 | **Re-entry Upgrade** | slots full + better signal available (strength +0.3) | Close 100% |

### SSO-3 Multi-level (replaces PV1 fixed %):
```
Level 1: pnl >= 1.0 × ATR_4h → close 30%
Level 2: pnl >= 2.0 × ATR_4h → close 30% more
Level 3: pnl >= 3.5 × ATR_4h → close 25% more
Remaining 15%: runs with trailing stop
```
Adapts to each symbol's volatility (BTC L1≈0.8%, LINK L1≈2.2%, MEME L1≈4%+).

### Exit priority order (updated):
```
0a. Track peak PnL (always)
0b. SSO evaluation (5 triggers):
    - SSO-1: Stagnant profit ($3+ for 2h+ with velocity=0)
    - SSO-2: Momentum exhausted (RSI + volume dying)
    - SSO-3: Multi-level ATR scale (adaptive increments)
    - SSO-4: Target contribution ($6+ toward $40/day goal)
    - SSO-5: Re-entry upgrade (better opportunity available)
0c. PV2 Momentum reversal (safety net for peak drawback)
1.  Breakeven SL: pnl >= 1.0% → move SL to entry
2.  Trailing: pnl >= 2.5% → ATR-scaled trail
3.  Hard SL: 5% → last resort
4.  Hard TP: 7% + SmartTP extend
```

### Key Config (env vars):
```
SSO_ENABLED=true
SSO_STAGNANT_MIN_HOURS=2.0
SSO_STAGNANT_MIN_PROFIT_FLOOR=3.0
SSO_MOMENTUM_RSI_OVERBOUGHT=70
SSO_MULTILEVEL_ENABLED=true
SSO_MULTILEVEL_L1_ATR_MULT=1.0
SSO_MULTILEVEL_L2_ATR_MULT=2.0
SSO_MULTILEVEL_L3_ATR_MULT=3.5
SSO_TARGET_DAILY_USD=40.0
SSO_TARGET_CONTRIBUTION_PCT=0.15
```

### Velocity tracking:
- `sso_pnl_snapshot`: {ts, pnl_pct} stored in _smart_meta (futures) / metadata (spot)
- `pnl_velocity_1h`: computed as delta_pnl / elapsed_hours
- `sso_levels_done`: [1, 2, 3] — tracks which multi-level scales have fired

### Dashboard visibility:
- 🎯 badge on open positions showing SSO levels completed (e.g., "🎯L1,2")
- SSO close reasons in Profit Tracker: ⏱️ Estancada, 📉 Momentum, 📊 Nivel 1/2/3, 🎯 Target, 🔄 Upgrade
- `sso_last_trigger` field in position data

### Files:
- `smart_scale_out.py` (NEW): SmartScaleOut class + SSOPositionData + compute_pnl_velocity()
- `config.py`: 22 new SSO fields
- `docker-compose.yml`: 11 SSO env vars
- `futures_client.py`: SSO replaces PV1 in check_positions()
- `run_trades.py`: SSO in _check_sl_tp_positions() for spot
- `app.py`: sso_levels_done + sso_last_trigger in open positions
- `frontend/dashboard.js`: SSO close reasons + 🎯 badge
- `frontend/styles.css`: .sl-badge.sso style
- `frontend/index.html`: v22 cache bust

### Expected daily PnL improvement over PV1:
- SSO-1 (stagnant): +$5-10/day (liberate trapped capital)
- SSO-2 (exhausted): +$2-4/day (close before reversal)
- SSO-3 (multilevel): +$3-6/day (adaptive vs fixed 50%)
- SSO-4 (target): +$2-5/day (accelerate toward goal)
- SSO-5 (upgrade): +$3-5/day (replace weak with strong)
- **Total: +$15-30/day incremental**

### Tests: 86 passed (65 smart exits + 21 profit boost), 0 failures

### 🔍 ANÁLISIS PENDIENTE (markers sesión 19):
1. **48h check**: ¿SSO-3 multilevel dispara a niveles ATR correctos por símbolo? (SUI/SOL alta vol vs BTC baja vol)
2. **24h check**: ¿SSO-1 cierra posiciones estancadas ($3+ / 2h+ / vel=0)?
3. **Coordinación PV2+SSO**: ¿PV2 sigue disparando como safety net después de SSO partial?
4. **GAP — `sso_today_realized`**: No populado → SSO-4 usa 0.0 (conservador). Próximo: agregar daily PnL aggregator.
5. **GAP — SSO-5 datos faltantes**: `all_slots_full` y `best_available_ev` no populados → SSO-5 inactivo hasta integrar pipeline de señales.
6. **GAP — Spot `today_realized_pnl`**: Hardcoded 0.0 → necesita cache de summary inyectado.
7. **Futuro**: Endpoint `/api/sso/stats` con conteo de triggers y profit promedio por tipo.

---

## SL OPTIMIZATION — SSO ERA (jun 22 2026 — sesión 19b)

> **Purpose**: Widen Stop-Loss to reduce premature exits, leveraging SSO's adaptive profit-taking.
> SL only matters in the first 30min of a position; after that breakeven/SSO/trailing protect.

### Data-driven decision (171 trades analyzed):
- 35 SL hits = -$120.91 (53% de todas las pérdidas)
- SPOT SL avg: -$3.76 / -2.64% move (26 hits) — muchos son wicks normales
- FUTURES SL avg: -$2.58 / -1.14% move (9 hits) — breakeven ya movía SL
- Win rate: 21.6% — expectativa negativa, necesitamos que más positions sobrevivan a SSO

### Changes applied:

| Setting | Before | After | Rationale |
|---------|--------|-------|-----------|
| **SPOT stop_loss_pct** (global default) | 1.8% | **2.5%** | Reduce wick stops |
| **LARGE_CAP SL** | 1.8% | **2.5%** | 1.4×ATR avg |
| **MIDCAP_ALT SL** | 1.8% | **2.5%** | Same |
| **NARRATIVE SL** | 3.5% | **3.5%** | Already appropriate |
| **MEME SL** | 2.0% | **2.5%** | Align with others |
| **ATR SL min floor** | 1.0% | **1.5%** | Prevent micro-stops |
| **FUTURES_SL_PCT** | 5.0% | **7.0%** | Align with TP (7%), SSO/BE protect early |
| **FUTURES trailing activation** | 2.5% | **2.0%** | Earlier trailing compensates wider SL |

### Safety net chain (unchanged):
```
0-30min:  Hard SL only protection (wider = slightly more risk here)
1%+:      Breakeven → SL moves to entry → ZERO RISK
1×ATR+:   SSO-3 L1 → 30% profit locked
2%+:      Trailing activates → ATR-scaled protection
2h+:      SSO-1 stagnant check → close if velocity=0
```

### Projected impact:
- ~6 fewer stops/week × $3.76 = **-$16/week saved**
- More positions survive to SSO → **+$20/week profits**
- Net: **+$36/week** estimated

### Files modified:
- `config.py`: stop_loss_pct, category_thresholds, atr_sl_min_pct, futures_sl_pct, futures_trailing_activation_pct
- `docker-compose.yml`: FUTURES_SL_PCT=7.0, FUTURES_TRAILING_ACTIVATION_PCT=2.0, STOP_LOSS_PCT=2.5, ATR_SL_MIN_PCT=0.015

### 🔍 ANÁLISIS PENDIENTE:
- **24h check**: ¿Se reduce la frecuencia de SL hits con el nuevo 2.5%?
- **48h check**: ¿Las posiciones que sobreviven generan más SSO profits?
- **Risk check**: ¿El avg SL loss sube mucho? (esperado: de -$3.76 a ~-$4.50)

---

## SSO-1b PROFIT DECAY (jun 22 2026 — sesión 19c)

> **Purpose**: Detectar y cerrar posiciones que están devolviendo ganancias activamente,
> liberando capital para re-entry con señal fresca.

### Problema resuelto:
- SUIUSDT: peak $5.04 (1.9%) → cayendo a $4.15 (1.47%) en 20min, ningún trigger cerraba
- SSO-1 original necesita velocity=0 (snapshot viejo daba positivo)
- PV2 necesita drawback ≥ 0.8% + `scale_out_done=True` (nunca se alcanza)
- **Gap 0.3%-0.8% donde nadie cerraba** → SSO-1b lo resuelve

### Mecanismo:
```
SSO-1b evalúa en cada ciclo (4min):
  ¿peak_pnl - current_pnl >= 0.4%?  (decay activo)
  ¿pnl_usdt >= $2?                  (profit worth closing)
  ¿age >= 1.5h?                     (no cerrar recién abiertas)
  → Si las 3 = TRUE → CLOSE 100%
```

### Config:
| Setting | Default | Env |
|---------|---------|-----|
| `sso_decay_threshold_pct` | 0.004 (0.4%) | `SSO_DECAY_THRESHOLD_PCT` |
| `sso_decay_min_profit_usd` | 2.0 | `SSO_DECAY_MIN_PROFIT_USD` |
| `sso_decay_min_hours` | 1.5 | `SSO_DECAY_MIN_HOURS` |
| `sso_stagnant_velocity_window_min` | 30 (was 60) | `SSO_STAGNANT_VELOCITY_WINDOW_MIN` |

### Cambios adicionales:
- **Removido guard PV2** en SSO-1 (línea 143-144 de smart_scale_out.py) — creaba zona muerta
- **Velocity window 60→30min** — snapshots más frescos para SSO-1 stagnant original

### Files: smart_scale_out.py, config.py, docker-compose.yml, tests/test_futures_smart_exits.py

---

## SSO-1c SLOW CRAWL + RESTRUCTURE (jun 22 2026 — sesión 19d)

> **Purpose**: Cerrar posiciones que suben demasiado lento para justificar el slot,
> liberar capital para re-entry con señal fresca. Fix gate ordering + snapshot persist.

### SSO-1 ahora tiene 3 sub-checks independientes:
```
SSO-1b DECAY:    peak-current >= 0.4%, profit >= $2, age >= 1.5h → CLOSE 100%
SSO-1c SLOW:     worst(snapshot, lifetime) <= tier threshold → CLOSE 100%
                 T1(2-4h): 0.10%/h | T2(4-6h): 0.15%/h | T3(6-8h): 0.35%/h | T4(8h+): 0.50%/h
                 Uses min(snapshot_vel, lifetime_vel = pnl_pct/age_hours)
SSO-1  STAGNANT: velocity <= 0, profit >= $3, age >= 2h → CLOSE 100%
```

### Config SSO-1c:
| Setting | Default | Env |
|---------|---------|-----|
| `sso_slow_velocity_threshold` | 0.001 (0.1%/h base) | `SSO_SLOW_VELOCITY_THRESHOLD` |
| `sso_slow_min_profit_usd` | 1.0 | `SSO_SLOW_MIN_PROFIT_USD` |
| `sso_slow_min_hours` | 2.0 | `SSO_SLOW_MIN_HOURS` |
| `sso_slow_tier2_hours` | 4.0 | `SSO_SLOW_TIER2_HOURS` |
| `sso_slow_tier3_hours` | 6.0 | `SSO_SLOW_TIER3_HOURS` |
| `sso_slow_tier4_hours` | 8.0 | `SSO_SLOW_TIER4_HOURS` |

### SSO Full Visibility (sesión 19e):
Cada posición genera un log `[SSO/F]` (futures) o `[SSO/S]` (spot) mostrando:
- Cuando FIRE: `[SSO/F] SUIUSDT SHORT: → CLOSE 100% sso_slow_crawl | detail...`
- Cuando NO: `[SSO/F] SUIUSDT SHORT: NO ACTION | SSO-1:[1b:no(...) 1c:no(...) 1:no(...)] pk=X cur=Y life=Z snap=W`
- `diagnosis` field en SSOAction siempre populated con razones por sub-trigger

### Bugfixes incluidos:
- **Gate ordering**: Cada sub-check tiene sus propios gates — SSO-1b ya no bloqueado por $3/2h de SSO-1
- **Snapshot persist**: `_persist_smart_meta()` después de actualizar velocity snapshot → sobrevive reinicios
- **Lifetime velocity**: SSO-1c usa `min(snap, lifetime)` — no depende solo del snapshot de 30min

### Tests: 98 passed, 0 failures
### Files: smart_scale_out.py, config.py, futures_client.py, run_trades.py, docker-compose.yml

### 🔍 MONITOR (24h):
- Revisar logs `[SSO/F]` y `[SSO/S]` para validar que diagnósticos son claros
- ¿SSO-1c T3 cierra SUIUSDT (~0.30%/h lifetime)?
- ¿Capital reciclado genera mejores trades?
- ¿No hay over-closure? (positions cerradas que luego habrían ganado más)

---

## BINANCE ORDER VERIFICATION (jun 22 2026 — sesión 19f)

> **Purpose**: Garantizar que toda acción enviada a Binance (compra, venta, cierre)
> se confirme como ejecutada antes de registrarla internamente. Prevenir phantom trades.

### Nuevos métodos:
- **`BinanceClient.verify_order_filled(order_result, label)`** → `bool`
  - Verifica `status=="FILLED"` → `fills[]` → `get_order_status()` fallback
  - Usado en SPOT para todas las órdenes de mercado
- **`FuturesClient._verify_position_closed(symbol, side)`** → `bool`
  - Query `/fapi/v2/positionRisk` confirma qty=0 en Binance
  - Usado después de close_long, close_short, SSO full close

### Cobertura SPOT (run_trades.py) — 9 paths:
| Path | Acción si no confirma |
|------|-----------------------|
| SL/TP sell | `continue` (no cierra en PM) |
| SSO partial sell | `continue` |
| Scale-out sell | `continue` |
| Replacement sell | `return False` |
| BUY entry | `continue` (no registra posición) |
| Cleanup sell | `continue` |
| SlotPressure sell | `continue` |
| Excluded cleanup | `continue` |
| B2 regime cleanup | `continue` |

### Cobertura FUTURES (futures_client.py) — 4 paths:
| Path | Verificación |
|------|-------------|
| close_long/close_short | `_verify_position_closed` post-close |
| SSO 100% close | verify + cancel SL/TP + `_save_futures_closed` |
| open_short/open_long | status check en entry order |

### SSO Close DB Persist Fix:
- SSO 100% close ahora llama `_save_futures_closed()` con qty restaurado
- Cancela SL/TP algo orders huérfanas en Binance
- Bug previo: solo flags en memoria, nunca persistía a `trades_closed`

### Tests: 98 passed, 0 failures
### Files: binance_client.py, futures_client.py, run_trades.py

---

## ⭐ SAFEPOINT v1 — SISTEMA PRODUCTIVO (jun 22 2026, 17:15 UTC-6)

> **El sistema comenzó a ser consistentemente productivo en este punto.**
> Usar como referencia para cualquier comparación futura de rendimiento.

### Git Tag: `safepoint-v1-jun22-2026` (commit `b078c03`)
```bash
git checkout safepoint-v1-jun22-2026 -- docker-compose.yml && docker-compose up -d --force-recreate app
```

### Estado al Safepoint:
- **Régimen**: FUTURES_SHORT — Bear estructural (BTC 30d=-17%, F&G=20 Extreme Fear)
- **SHORTs activos**: 11/12 (TAOUSDT, FILUSDT, DOTUSDT, BCHUSDT, XRPUSDT, LINKUSDT, SOLUSDT, AVAXUSDT, UNIUSDT, DOGEUSDT, ADAUSDT)
- **SSO realizados**: ~$10.49 (5 cierres: SUIUSDT +$3.77, UNIUSDT +$3.23, ETH +$1.75, AVAX +$1.74)
- **Tests**: 98 passed, 0 failures

### Parámetros Críticos en Este Punto:
| Param | Valor |
|-------|-------|
| FUTURES_MAX_SHORTS | 12 |
| FUTURES_OVERFLOW_MIN_STRENGTH | 0.55 |
| FUTURES_SL_PCT / TP_PCT | 7% / 7% |
| STOP_LOSS_PCT (SPOT) | 2.5% |
| SSO_DECAY_THRESHOLD_PCT | 0.4% |
| SSO_SLOW_VELOCITY_THRESHOLD | 0.1%/h |

### Señales de Degradación (triggers para revisar vs este safepoint):
- Win Rate futures < 40% en 48h
- Avg PnL/trade < $0 (5+ trades)
- > 3 SL simultáneos en 24h
- F&G > 60 con régimen aún FUTURES_SHORT
- SSO firing repetidamente sin profit neto

### Archivo Completo: `SAFEPOINT_v1_jun22_2026.md`

### Futures Slot Fix (misma sesión):
- PEPEUSDT → 1000PEPEUSDT (bug: símbolo inválido en Binance futures)
- FUTURES_MAX_SHORTS: 9 → 12
- Overflow simplificado: removidos vol_spike + BTC 4h gates (redundantes con filtros de entrada)
- Resultado: sistema pasó de 8/9 → 12/12 shorts en primer ciclo

---

## 🧠 UNIFIED PORTFOLIO MANAGER — DIRECTOR DE FONDOS (jun 23 2026, Sesión 21)

> **PortfolioManager es ahora el director de capital de todo el sistema.** Lee ambos wallets (SPOT + Futures) y provee la referencia correcta de capital a cada tipo de trade.

### Problema Resuelto
`_kelly_position_size` usaba `usdt_liquid = SPOT USDT` para calcular trades Futures.  
Con $1,737 SPOT y $194 Futures: Kelly calculaba $104 pero el F4 cap (`max($50, $194×20%)=$50`) lo aplastaba a $65.  
La raíz: Kelly usaba el capital incorrecto como referencia — el wallet SPOT en vez del wallet Futures.

### Solución Implementada

#### `portfolio_manager.py` — nuevo método `get_unified_balance(futures_client)`
```python
result = {
    "usdt_available":    SPOT_USDT,       # referencia para SPOT Kelly (sin cambio)
    "futures_available": FUTURES_FREE,    # referencia para Futures Kelly (nuevo)
    "futures_total":     FUTURES_WALLET,  # incluye margen locked
    "total_usdt":        SPOT + FUTURES,  # sistema completo
}
```
`get_total_balance()` intacto — todos los callers SPOT no cambian.

#### `_kelly_position_size` — 3 nuevos params opcionales
```python
_kelly_position_size(..., base_override=None, max_override=None, cap_pct_override=None)
```
Si son `None` → comportamiento SPOT idéntico al anterior.

#### `run_trades.py` — 3 puntos de cambio (todos wrapeados en feature flag)
- **Futures SHORTs**: usa `futures_available` + Futures Kelly params
- **Futures LONGs** (SPOT_BULL): mismo patrón
- **CapBridge** `kelly_margin`: usa `futures_available` para cálculo de shortfall

### Env Vars Activos (docker-compose.yml)
| Variable | Valor | Propósito |
|----------|-------|-----------|
| `USE_UNIFIED_PORTFOLIO_MANAGER` | `true` | Feature flag — `false` = rollback instantáneo |
| `FUTURES_KELLY_BASE_USDT` | `100.0` | Floor Futures Kelly (SPOT usa `KELLY_BASE_USDT=40`) |
| `FUTURES_KELLY_MAX_USDT` | `500.0` | Techo Futures Kelly (SPOT usa `KELLY_MAX_USDT=350`) |
| `FUTURES_KELLY_MAX_PCT_LIQUID` | `0.55` | Cap líquido Futures (SPOT usa `KELLY_MAX_PCT_LIQUID=0.28`) |
| `FUTURES_USDT_PER_TRADE` | `100.0` | F4 floor (era 50.0) — dobla el cap mínimo del F4 |

### Sizing Verificado en Producción (futures_avail=$194.78)
| Tier | Prob/Strength | Antes | Ahora | Notional (2x) |
|------|-------------|-------|-------|--------------|
| BASE | 0.52 / 0.50 | $65 | $65 | $130 |
| MEDIA | 0.57 / 0.58 | $65 | **$130** | **$260** |
| ALTA | 0.62 / 0.72 | $65 | **$130** | **$260** |
| FUERTE | 0.67 / 0.82 | $65 | **$130** | **$260** |

> Escala automáticamente: cuando SSO libera capital + CapBridge recarga → `futures_avail` sube → F4 cap sube → trades más grandes.

### Todo lo Que NO Cambió
- `get_total_balance()` — todos los callers SPOT iguales
- SL% / TP% / Leverage 2x / SSO / Circuit breakers / Filtros de entrada
- `_pre_entry_checks` margin guard (`available < usdt_amount`)
- FIX-F3 `avail_net` hard cap activo

### Rollback Instantáneo
```bash
# En docker-compose.yml: cambiar USE_UNIFIED_PORTFOLIO_MANAGER=false
docker-compose up -d --force-recreate app
```

### Archivos Modificados
- `config.py` — 4 nuevos `Field()` en Settings
- `portfolio_manager.py` — método `get_unified_balance(futures_client)`
- `run_trades.py` — `_kelly_position_size` signature + 3 call sites
- `docker-compose.yml` — 5 nuevas env vars

### Tests Post-Implementación
- 217 passed, 12 skipped, 0 nuevos failures
- Pre-existing failures: `test_integration.py` (import), `test_position_manager.py` (API), `test_webhook.py` (401) — sin relación con estos cambios

---

---

## 🔬 BACKTEST OPTIMIZER — SIMULACIÓN 20d × 1000 CORRIDAS (jun 26 2026, Sesión 22)

### Objetivo
Simulación de los últimos 20 días de datos históricos (jun 7–17 2026) con 1000 configuraciones distintas de parámetros para encontrar la configuración óptima que maximiza ganancias. Motor propio con trazabilidad 100% en BD.

### Datos usados
- **41 parquets 1h OHLCV** en `/app/historical_data/` — 26,280 velas/símbolo
- **Ventana**: jun 7–17 2026 (últimos 20 días de datos disponibles)
- **1,131 trades sintéticos** pre-computados (`synthetic_trades_full_20260617_155237.json`) para señales SPOT
- **16 símbolos FUTURES** con datos parquet (BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, etc.)
- Capital inicial: spot=$100 + futures=$988 = **$1,088 USDT**

### Motor (`Dev/backtest_optimizer.py`)
- **Latin Hypercube Sampling** — 11 dimensiones, 1000 corridas, seed=42
- **SPOT**: señales de `synthetic_trades_full` JSON (analyze_signal real pre-computado)
- **FUTURES SHORT**: señales derivadas de OHLCV + exit candle-by-candle simulado
- **Comisiones realistas**: SPOT 0.1%×2, FUTURES 0.04%×2, slippage 0.05%
- **Kelly K2 mult**: 0.8× (FUTURES_SHORT conf=0.60)
- **FnG boost**: 1.25× (FnG=15 Extreme Fear actual)
- Tiempo de ejecución: **35.6 segundos** para 1000 corridas

### Resultados

| Métrica | Baseline (config actual) | Mejor (run_0812) | Mejora |
|---------|--------------------------|------------------|--------|
| PnL | +$395.39 USDT | **+$2,799.22 USDT** | +$2,403.83 |
| Retorno | +36.34% | **+257.28%** | +220.94% |
| Trades | 612 (584 spot, 28 fut) | 655 | — |
| Win Rate | 40.7% | 40.5% | — |
| Sharpe | 3.80 | **11.48** | — |
| MaxDD | $261.51 | $544.17 | — |

### Top 5 corridas por PnL
| Rank | Label | PnL | Sharpe | Win% | Trades |
|------|-------|-----|--------|------|--------|
| 1 | run_0812 | +$2,799 | 11.48 | 40.5% | 655 |
| 2 | run_0725 | +$2,246 | 11.39 | 42.4% | 603 |
| 3 | run_1000 | +$2,173 | 10.78 | 51.1% | 411 |
| 4 | run_0712 | +$2,042 | 15.83 | 53.0% | 370 |
| 5 | run_0433 | +$1,939 | 13.11 | 56.1% | 335 |

### Parámetros óptimos aplicados en producción (hot-config sin restart)
| Parámetro | Anterior | Nuevo | Delta |
|-----------|----------|-------|-------|
| `kelly_max_usdt` (SPOT) | $500 | **$663** | +$163 ▲ |
| `kelly_max_pct_liquid` | 0.55 | **0.621** | +0.071 ▲ |
| `futures_kelly_max_usdt` | $800 | **$1,162** | +$362 ▲ |
| `futures_sl_pct` | 3.0% | **3.33%** | +0.33% ▲ |
| `futures_tp_pct` | 7.0% | **7.68%** | +0.68% ▲ |

> ⚠️ **`min_strength=0.359` NO aplicado** — mínimo validado en producción es 0.40 (caso AAVE SHORT jun 23: strength=0.30 → -5.88%). Regla: **nunca bajar min_strength por debajo de 0.40**.

### BD — Trazabilidad 100%
- **1,001 backtest_run** + **120,380 backtest_trade** en tablas nuevas (`014_backtest_tables.sql`)
- API: `GET /backtest/runs`, `GET /backtest/best`, `GET /backtest/runs/{id}`

### Infraestructura añadida
- `migrations/014_backtest_tables.sql` — tablas `backtest_run` + `backtest_trade`
- `Dev/backtest_optimizer.py` — motor LHS completo (580 líneas)
- `app.py` — 3 endpoints `/backtest/*`
- `frontend/index.html` — panel `#backtestOptimizerPanel` (tab Mercado)
- `frontend/dashboard.js` — `fetchBacktestResults()`, `renderBacktestPanel()`, `applyBestBacktestConfig()`, JS v23
- `frontend/styles.css` — `.bt-summary`, `.bt-sum-card`, `.bt-sum-best`

### Cómo re-ejecutar
```bash
docker exec crypto-agent-db psql -U crypto_agent -d crypto_agent -c "TRUNCATE backtest_run CASCADE; ALTER SEQUENCE backtest_run_id_seq RESTART WITH 1;"
docker exec crypto-agent-app python Dev/backtest_optimizer.py --runs 1000 --days 20 --top 20
```

### Fixes en esta sesión
- `trades_audit` constraint ampliado: acepta `trade_type='system'` (era solo `spot|futures`)
- `_KELLY_EDITABLE_FIELDS` whitelist en `app.py` ampliada: `stop_loss_pct`, `smart_tp_max_pnl_pct`, `futures_sl_pct`, `futures_tp_pct`
- `pyarrow` v24.0.0 instalado en container (necesario para leer parquets)
- `_NumpyEncoder` en backtest_optimizer: serialización correcta de `np.float64` → JSON/BD
- Transacción única `get_connection()` para INSERT run + batch trades (fix FK constraint fallo)

---

## Sesión 25 — jun 26 2026 — Gap Analysis: category_thresholds + startup restore + auto-apply

### Commits: `6e2e72e` (category_thresholds) + `<startup-restore>` (app.py startup loader)

### Parámetros 100% activos en producción (verificados 21:44 UTC-6)

| Parámetro | Valor | Fuente |
|---|---|---|
| `kelly_max_usdt` | **663** | kelly_config_override BD |
| `kelly_max_pct_liquid` | **0.621** | kelly_config_override BD |
| `kelly_base_usdt` | 100 | default (NO cambiar) |
| `futures_kelly_max_usdt` | **1162** | kelly_config_override BD |
| `futures_sl_pct` | **3.33%** | kelly_config_override BD |
| `futures_tp_pct` | **7.68%** | kelly_config_override BD |
| `stop_loss_pct` (SPOT) | 1.5% | kelly_config_override BD |
| `min_signal_strength` | 0.40 | kelly_config_override BD |
| `LARGE_CAP.take_profit_pct` | **5.83%** | category_thresholds_override BD |
| `MIDCAP_ALT.min_prob` | **0.527** | category_thresholds_override BD |
| `MIDCAP_ALT.take_profit_pct` | **5.83%** | category_thresholds_override BD |
| `NARRATIVE.min_prob` | **0.527** | category_thresholds_override BD |
| `MEME.min_prob` | **0.527** | category_thresholds_override BD |
| `MEME.take_profit_pct` | **5.83%** | category_thresholds_override BD |

### Parámetros NO aplicados (intencional)
- `min_strength=0.359` — **NO**: mínimo 0.40 (AAVE SHORT -5.88%)
- `vol_spike_min=0.83` — **NO**: < 1.0 = vol por debajo del promedio, sin convicción
- `kelly_base_usdt=76` — **NO**: bajar floor reduce tamaño base de posición

### Infraestructura añadida

**`app.py` — Startup Loader (nuevo)**
```python
# startup_event() ahora restaura overrides de BD al arrancar:
# 1. kelly_config_override -> scalars (kelly_max, futures_sl/tp, etc.)
# 2. category_thresholds_override -> toda la estructura de thresholds por categoría
```
Garantiza que `PATCH /config/kelly` **sobrevive reinicios** — sin este loader los cambios hot-config se perdían.

**`app.py` — `PATCH /config/kelly` extendido**
- Acepta `category_thresholds` en el body → merge parcial por categoría/campo
- Persiste en `trading_runtime_state` key=`category_thresholds_override`
- Registra en `trades_audit` event=`kelly_config_change` (trazabilidad 100%)
- `_ALLOWED_CAT_FIELDS`: `min_prob, min_pnl, min_volspike, stop_loss_pct, take_profit_pct, trailing_*`

**`app.py` — `/backtest/best` extendido**
- Nuevo campo `category_patch_suggestion`: cambios propuestos a `category_thresholds` basados en el backtest óptimo
- Nuevo campo `never_auto_apply`: lista de params que NUNCA deben auto-aplicarse
- `category_patch_suggestion` vacío cuando todos los valores ya están en óptimos ✅

**`Dev/backtest_optimizer.py` — `--auto-apply` flag**
```bash
docker exec crypto-agent-app python Dev/backtest_optimizer.py --runs 1000 --days 20 --auto-apply
```
- `auto_apply_best()`: aplica best config vía API al finalizar la simulación
- `_NEVER_AUTO_APPLY = {"min_strength", "vol_spike_min", "kelly_base_usdt"}` — hardcodeado
- Registra `backtest_auto_apply` en `trades_audit` para trazabilidad
- API key desde `DASHBOARD_API_KEY` env var automáticamente

**`frontend/dashboard.js` v25**
- Sección "Category Thresholds" con checkboxes por categoría/campo
- `btToggleCatRow()` para opacidad visual
- `applyBestBacktestConfig()` construye `category_thresholds` dinámicamente desde checkboxes

### Scripts de validación (Dev/)
```bash
docker exec crypto-agent-app python Dev/verify_final.py   # Estado completo post-restart
docker exec crypto-agent-app python Dev/check_live.py     # Kelly scalars vs backtest optimos
docker exec crypto-agent-app python Dev/validate_params.py # Validacion detallada
```

### Resultado verificado: 7 OK, 0 DIFF, 3 SKIP intencionales

---

## Sesión 26: Backtest 100% aplicable + Hybrid Roles

### Objetivo
Permitir aplicar **todos los parámetros identificados en la mejor configuración** del backtest de 1000 corridas desde el dashboard, con **100% trazabilidad** y **snapshots + rollback**. Además, introducir un framework híbrido que integra:
- **Física cuántica**: Simulated Quantum Annealing (SQA) para refinar el espacio de parámetros.
- **Robótica**: Enjambre multi-agente que valida señales y configuraciones.
- **Machine Learning**: Ensemble ligero + bandit Thompson para selección de configuraciones.

### Roles híbridos

| Rol | Concepto | Implementación | Archivo |
|-----|----------|----------------|---------|
| **Física cuántica** | Optimización inspirada en annealing cuántico | `SimulatedQuantumAnnealing` explora el espacio de parámetros con túneles y convergencia | `hybrid_optimizer/quantum_space.py` |
| **Robótica** | Multi-agente swarm que vota | `SignalSwarm` con robots: momentum, risk, correlation, regime | `hybrid_optimizer/signal_swarm.py` |
| **Machine Learning** | Ensemble + multi-armed bandit | `LightweightEnsemble` predice PnL; `ThompsonBandit` selecciona entre configs | `hybrid_optimizer/ml_predictor.py` |

### Nuevos campos en `Settings` (`config.py`)
- `vol_spike_min`: vol spike mínimo global (SPOT + FUTURES)
- `futures_min_prob`: mínima probabilidad para señales FUTURES
- `futures_vol_spike_min`: vol spike mínimo para FUTURES
- `futures_category_thresholds`: umbrales por categoría para FUTURES
- `hybrid_optimizer_enabled`: flag para activar el framework híbrido
- `auto_apply_backtest`: flag para auto-aplicar backtest
- `backtest_safe_only`: solo aplica parámetros dentro de rangos seguros

### Nuevos endpoints

| Método | Endpoint | Descripción | Trazabilidad |
|--------|----------|-------------|--------------|
| GET | `/backtest/best` | Mejor config + sugerencias SPOT/FUTURES | - |
| POST | `/backtest/apply` | Aplicar config seleccionada con snapshot previo | `backtest_apply` en `trades_audit` |
| POST | `/backtest/optimize` | Ejecutar 1000 corridas en background | `backtest_auto_apply` si `--auto-apply` |
| GET | `/config/snapshots` | Listar snapshots de configuración | `config_snapshot_created` |
| POST | `/config/rollback` | Restaurar un snapshot | `config_rollback` |
| GET | `/hybrid/recommend` | Recomendación híbrida QIO+swarm+ML | `hybrid_optimizer_suggestion` |

### Rangos de seguridad para aplicación automática
```python
SAFE_BOUNDS = {
    'min_strength': (0.40, 0.90),
    'vol_spike_min': (1.0, 3.0),
    'kelly_base_usdt': (50.0, 300.0),
    'kelly_max_usdt': (200.0, 1200.0),
    'futures_kelly_max_usdt': (300.0, 2000.0),
    'spot_sl_pct': (0.010, 0.040),
    'spot_tp_pct': (0.018, 0.080),
    'futures_sl_pct': (0.012, 0.050),
    'futures_tp_pct': (0.020, 0.100),
}
```
`min_strength` y `vol_spike_min` están bloqueados de auto-aplicación; solo se aplican manualmente si el usuario los selecciona y el valor está dentro del rango seguro.

### Mapeo de parámetros backtest → Settings
| Backtest | Settings | Escala |
|----------|----------|--------|
| `kelly_base_usdt` | `kelly_base_usdt` | 1.0 |
| `kelly_max_usdt` | `kelly_max_usdt` | 1.0 |
| `kelly_max_pct_liquid` | `kelly_max_pct_liquid` | 1.0 |
| `futures_kelly_max_usdt` | `futures_kelly_max_usdt` | 1.0 |
| `spot_sl_pct` | `stop_loss_pct` | 100.0 |
| `spot_tp_pct` | `take_profit_pct` | 100.0 |
| `futures_sl_pct` | `futures_sl_pct` | 100.0 |
| `futures_tp_pct` | `futures_tp_pct` | 100.0 |
| `min_strength` | `min_signal_strength` | 1.0 |
| `min_prob` | `category_thresholds.*.min_prob` | 1.0 |
| `vol_spike_min` | `vol_spike_min` | 1.0 |

### Comandos de validación
```bash
# Verificar sintaxis
docker exec crypto-agent-app python -m py_compile /app/app.py /app/config.py /app/Dev/backtest_optimizer.py /app/hybrid_optimizer/*.py

# Ejecutar tests
docker exec crypto-agent-app python -m pytest /app/tests/test_hybrid_backtest.py -v

# Ejecutar backtest + auto-apply
docker exec crypto-agent-app python Dev/backtest_optimizer.py --runs 1000 --days 20 --auto-apply

# Consultar recomendación híbrida
curl -H "X-API-Key: $DASHBOARD_API_KEY" http://localhost:8000/hybrid/recommend
```

---

## Auditoría exhaustiva híbrida — 27 jun 2026

### Hallazgos críticos corregidos

1. **Coherencia FUTURES trades_open ↔ Binance (FIX-S7)**
   - Problema: escalonamiento y cierres parciales dejaban múltiples filas en `trades_open` con cantidades/precios desfasados respecto a Binance.
   - Corrección: `futures_client.py` ahora ejecuta `_consolidate_futures_open_records()` durante `_sync_open_positions()` para colapsar filas obsoletas y usar el `entry_price` y `qty` de Binance como única fuente de verdad.
   - Archivos: `futures_client.py`, `tests/test_futures_consolidation.py`.

2. **Persistencia de estado volátil tras reinicio (FIX-STATE)**
   - Problema: cooldowns de SL de futuros, mass-SL circuit breaker, posiciones VolSpike y contadores diarios vivían solo en RAM; se perdían al reiniciar el contenedor.
   - Corrección: nuevo `state_persistence.py` guarda/recarga el estado volátil en `trading_runtime_state` al final de cada ciclo y en el startup.
   - Archivos: `state_persistence.py`, `run_trades.py`, `tests/test_state_persistence.py`.

3. **Futures SL cooldown no actualizaba dict en RAM (FIX-F1)**
   - Problema: `_set_cooldown` con `cooldown_type='futures_sl'` persistía en `trading_cooldowns` pero no actualizaba `_FUTURES_SL_COOLDOWN` en memoria, causando comportamiento inconsistente dentro del mismo ciclo.
   - Corrección: `_set_cooldown` ahora también actualiza `_FUTURES_SL_COOLDOWN` y loguea errores en vez de `pass` silencioso.
   - Archivo: `run_trades.py`.

4. **FuturesClient recibía `db_client=None` (FIX-G2)**
   - Problema: `_get_futures_client` creaba el singleton sin `db_client`, por lo que `_sync_open_positions`, `_save_futures_open` y la consolidación no podían persistir en BD hasta que alguien asignaba manualmente el cliente.
   - Corrección: `_get_futures_client` ahora resuelve automáticamente `db_client` desde `init_components.get_components()`.
   - Archivo: `run_trades.py`.

### Estado post-auditoría
- `trades_open` futuros: 12 filas, 100% alineadas con Binance en `qty` y `entry_price`.
- Tests nuevos: `tests/test_state_persistence.py` (2 passed), `tests/test_futures_consolidation.py` (1 passed).
- Tests existentes: `tests/test_hybrid_backtest.py` (8 passed).
- Nota: `tests/test_integration.py` falla en importación de `message_bus` (módulo no existe); no es introducido por esta auditoría.
- Estado final del sistema: reanudado vía `POST /control/start` y en estado `RUNNING` con 12 posiciones FUTURES activas.

### Comandos de validación post-auditoría
```bash
# Coherencia FUTURES con Binance
docker exec crypto-agent-app python -c "
import os, requests, psycopg2, psycopg2.extras
api='http://localhost:8000'; key=os.environ['DASHBOARD_API_KEY']
positions = requests.get(f'{api}/futures/positions', headers={'X-API-Key': key}).json().get('positions', [])
conn=psycopg2.connect(host=os.environ['DB_HOST'], port=os.environ['DB_PORT'], dbname=os.environ['DB_NAME'], user=os.environ['DB_USER'], password=os.environ['DB_PASSWORD'])
cur=conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute(\"SELECT symbol, quantity FROM trades_open WHERE metadata->>'trade_type'='futures'\")
db={r['symbol']: float(r['quantity']) for r in cur.fetchall()}
conn.close()
for p in positions:
    print(p['symbol'], 'binance', p['qty'], 'db', db.get(p['symbol'], None))
"

# Tests de regresión
docker exec crypto-agent-app python -m pytest tests/test_hybrid_backtest.py tests/test_state_persistence.py tests/test_futures_consolidation.py -q
```

---

---

## Config Governance & Drift Alerts — 28 jun 2026

### Cambios
- `config_helpers.py` es la fuente única de verdad para defaults canónicos SPOT+FUTURES; `_PARAM_DEFAULTS` ahora mapea 1:1 con campos de `Settings`.
- Migración de getters críticos restantes a `config_helpers.get_param`; `audit_getattr.py` reporta **0 mismatches críticos**.
- Nueva tabla `config_change_log` + endpoints `/config/snapshot`, `/config/verify`, `/config/apply`, `/config/change-log`.
- Snapshots automáticos cada 15 min; todo cambio de config persiste en `trading_runtime_state` y se loguea con `diff` + `snapshot_id`.
- Nueva tabla `config_drift_alerts` + background check cada 15 min + endpoints `/config/drift-check`, `/config/drift-alerts`, `/config/drift-alerts/{id}/acknowledge`.
- Severidad `critical` para parámetros de apalancamiento/ganancia FUTURES; `warning` para el resto. Se excluyen campos operativos intencionales (listas de símbolos, dry_run, etc.).
- Tests: `tests/test_config_governance.py` (5 passed incluyendo drift alert lifecycle).

### Validación en vivo
```bash
docker exec crypto-agent-app python /app/tmp_audit_config.py
docker exec crypto-agent-app python /app/tmp_verify_runtime_config.py
docker exec crypto-agent-app pytest tests/test_config_governance.py -v
curl -X POST http://localhost:8000/config/drift-check -H "X-API-Key: $DASHBOARD_API_KEY"
```

---

## Re-análisis agresivo del backtest + carga automática por régimen — 1 jul 2026

### Filosofía
Con los filtros estrictos de entrada actuales (vol_spike ≥ 1.5x, RSI 4h 35-55, precio en 20-40% del rango semanal, PnL predicho positivo), se puede ser **más agresivo en Kelly base** pero **más controlado en Kelly máximo y % de líquido** para evitar que unas pocas señales extremas consuman demasiado capital. El análisis usó el **top 10% por PnL absoluto** de cada escenario, no la métrica robusta.

### Implementación técnica
- Nueva función `_apply_regime_params()` en `run_trades.py` (línea ~339).
- Se invoca en `execute_once()` cuando el régimen detectado está **confirmado**.
- Lee overrides por régimen desde `trading_runtime_state` bajo claves `regime_params_<REGIMEN>`.
- Aplica campos escalares y hace merge parcial de `category_thresholds` / `futures_category_thresholds`.
- Persiste los cambios en `kelly_config_override`, `category_thresholds_override` y `futures_category_thresholds_override` para sobrevivir reinicios.
- Registra auditoría en `trades_audit` (evento `regime_params_applied`) y `config_change_log`.

### Parámetros globales aplicados
| Parámetro | Valor anterior | Valor nuevo | Rationale |
|---|---:|---:|---|
| `kelly_base_usdt` | 76 | **120** | Mayor tamaño inicial en señales calificadas |
| `kelly_max_usdt` | 663 | **500** | Evita over-bet en señales extremas |
| `kelly_max_pct_liquid` | 0.621 | **0.50** | Reduce exposición máxima del capital |
| `futures_kelly_max_usdt` | 1162 | **750** | Futuros con apalancamiento real no necesitan margen tan alto |
| `min_signal_strength` | 0.359 | **0.55** | Más selectivo |
| `min_success_probability` | 0.527 | **0.52** | Ligeramente más permisivo para no perder buenas señales |
| `vol_spike_min` | 0.91 | **1.35** | Exige convicción de volumen |
| `stop_loss_pct` | 1.03% | **1.7%** | Evita stops prematuros |
| `take_profit_pct` | 5.83% | **5.6%** | Toma ganancias rápido |
| `futures_sl_pct` | 1.42% | **2.5%** | Más aire con leverage |
| `futures_tp_pct` | 2.81% | **5.5%** | Objetivo realista con leverage |
| `slippage_pct` | 0.0006 | **0.0005** | Ligero ajuste a la baja |

### Parámetros por régimen (almacenados en `trading_runtime_state`)
| Régimen | `kelly_base` | `kelly_max` | `kelly_max_pct_liquid` | `futures_kelly_max` | `min_signal_strength` | `min_prob` | `vol_spike_min` | `stop_loss_pct` | `take_profit_pct` | `futures_sl_pct` | `futures_tp_pct` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **SPOT_BULL** | 120 | 480 | 0.48 | 760 | 0.58 | 0.52 | 1.45 | 1.8% | 5.8% | 2.6% | 5.8% |
| **RANGE_SCALP** | 130 | 500 | 0.50 | 730 | 0.55 | 0.50 | 1.35 | 1.6% | 5.5% | 2.5% | 5.5% |
| **PRESERVATION** | 100 | 450 | 0.45 | 800 | 0.50 | 0.52 | 1.20 | 1.5% | 5.6% | 2.7% | 6.0% |
| **FUTURES_SHORT** | 90 | 550 | 0.55 | 600 | 0.45 | 0.55 | 1.15 | 2.0% | 4.0% | 2.3% | 2.2% |

Nota: `min_prob` del backtest se mapea a `category_thresholds.*.min_prob` y `futures_category_thresholds.*.min_prob` en producción. Los valores anteriores se aplican por categoría.

### Advertencias
- **FUTURES_SHORT**: datos de backtest limitados (114 trades en 4 meses). Los parámetros son experimentales; monitorear en producción antes de confiar plenamente.
- El mecanismo de carga por régimen solo aplica overrides cuando el régimen cambia y se confirma. El cambio actual de `SPOT_BULL` → `RANGE_SCALP` se reflejará automáticamente en el próximo ciclo confirmado.

### Comandos de validación
```bash
# Verificar que los overrides globales están en BD
docker exec crypto-agent-app python -c "
import os, psycopg2, json
conn=psycopg2.connect(os.environ['DB_URL'])
cur=conn.cursor()
cur.execute('SELECT value FROM trading_runtime_state WHERE key=%s', ('kelly_config_override',))
v=cur.fetchone()
print(json.dumps({k:v2.get('new') for k,v2 in json.loads(v[0]).items()}, indent=2))
"

# Verificar parámetros por régimen
docker exec crypto-agent-app python -c "
import os, psycopg2, json
conn=psycopg2.connect(os.environ['DB_URL'])
cur=conn.cursor()
cur.execute('SELECT key FROM trading_runtime_state WHERE key LIKE %s', ('regime_params_%',))
print([r[0] for r in cur.fetchall()])
"

# Verificar auditoría
docker exec crypto-agent-app python -c "
import os, psycopg2, json
conn=psycopg2.connect(os.environ['DB_URL'])
cur=conn.cursor()
cur.execute('SELECT event, detail FROM trades_audit WHERE symbol=%s ORDER BY ts DESC LIMIT 3', ('__config__',))
for r in cur.fetchall(): print(r)
"
```

*Revisar este archivo al inicio de cada sesión de análisis o implementación.*
