# AIC — Análisis Inteligente de Cierres

> Protocolo canónico recurrente para convertir cierres negativos en cambios medibles de entrada, lifecycle y configuración.

## Identidad

- Nombre: **Análisis Inteligente de Cierres**.
- Sigla: **AIC**.
- ID de ejecución: `AIC-YYYYMMDD-NN`.
- Primera ejecución: `AIC-20260807-01`.
- Unidad primaria: tesis consolidada por `(symbol, entry_time)`; las filas de cierres parciales no se cuentan como tesis independientes.

## Actualización s138 — AIC de ejecución y slippage

- Todo análisis de cierre debe separar precio observado/esperado, VWAP estimado, fill promedio real, slippage esperado/real, PnL bruto/neto esperado/real, fees y funding.
- La política se evalúa por urgencia: hard-risk/emergencia priorizan salida inmediata; invalidación no crítica y profit pueden usar FOK full-size con fallback MARKET.
- La unidad sigue siendo la tesis, nunca cada fill interno de Binance.
- El dashboard debe reportar cobertura expected-vs-real y diferencias por SPOT/FUTURES, LONG/SHORT, venue, régimen y perfil A/B/C.
- Ningún cambio se promueve con datos históricos mezclados: el corte post-deploy y la muestra válida deben quedar identificados explícitamente.

## Preguntas obligatorias

Cada ejecución debe responder, en este orden:

1. **A — No abrir:** ¿qué información disponible antes del fill habría distinguido la pérdida sin bloquear una cantidad desproporcionada de ganancias?
2. **B — Proteger después de abrir:** ¿qué monitor, stop, supervisor o dato en tiempo real debía limitar la excursión adversa o cerrar antes?
3. **Configuración existente:** ¿el control ya existía pero estaba apagado, mal calibrado, fuera de hot-config, desconectado o consumiendo campos incorrectos?
4. **Código:** ¿hay un defecto de cálculo, contrato, persistencia, frecuencia, routing o atribución que impida actuar al control existente?
5. **Evidencia:** ¿cuál es la mejora contrafactual neta después de restar ganancias sacrificadas?

## Protocolo

### 1. Cohorte y verdad contable

- Fijar ventana UTC, `trade_type`, side, régimen y motivos de cierre.
- Excluir filas corruptas y reconciliar duplicados.
- Reportar filas de cierre y tesis distintas por separado.
- Usar PnL neto cuando comisiones y funding estén disponibles.

### 2. Forense de incidentes

- Priorizar pérdidas por severidad y recurrencia, no solo por frecuencia.
- Reconstruir señal, staged candidate, fill, excursión adversa, telemetry, decisiones de supervisores y cierre real en Binance.
- Verificar cobertura de `position_telemetry`, `trades_audit`, `ready_candidate_checks` y metadata de entrada.

### 3. Rama A — prevención de entrada

- Usar únicamente features disponibles antes de la entrada; no se permite look-ahead.
- Contrastar cada regla candidata contra una cohorte amplia de ganadores y perdedores.
- Medir: muestra, operaciones bloqueadas, pérdidas evitadas, ganancias sacrificadas, mejora neta y precisión de pérdidas.
- No promover una regla desde casos incidentales si la cohorte amplia no conserva el beneficio.

### 4. Rama B — protección de lifecycle

- Medir cruces de pérdida, tiempo desde entrada, recuperación posterior y MFE/MAE cuando exista.
- Distinguir cierre ciego por umbral de deterioro confirmado.
- Verificar watcher de 1s, hard-risk de 2s, SL/TP real, event stream y persistencia antes de proponer otro supervisor.
- Un cap USD debe usar PnL contractual; leverage no se multiplica dos veces.

### 5. Configuración antes que complejidad

- Inventariar flags y umbrales existentes antes de agregar lógica.
- Confirmar defaults, overrides restaurados desde DB, whitelist de `/config/kelly` y valor efectivo dentro del contenedor.
- Preferir calibrar o reparar el control propietario del comportamiento antes que crear un gate paralelo.

### 6. Decisión y despliegue

- Implementar solo cambios con hipótesis falsable y check focal.
- Añadir regresiones para el defecto y la excepción que evita sobrebloqueo.
- Actualizar los canónicos de entrada y lifecycle afectados.
- Reiniciar el proceso cuando el cambio sea de runtime Python.
- Verificar health, errores, datos live y razón de auditoría; un test verde no sustituye esta comprobación.

## Criterios de promoción

Un cambio AIC puede activarse cuando:

- la fuente es predecisional o de lifecycle sin look-ahead;
- la mejora neta incluye ganancias sacrificadas;
- la muestra y sus limitaciones quedan explícitas;
- existe rollback por hot-config o reversión localizada;
- hay cobertura de tests y verificación live del camino controlador.

Debe quedar en shadow o descartarse cuando:

- depende de menos de una cohorte representativa;
- usa campos contradictorios sin una fuente autoritativa;
- mejora pérdidas brutas pero destruye más ganancias;
- duplica un control existente sin corregir su fallo real.

## Artefactos por ejecución

Cada `AIC-*` debe producir:

- benchmark JSON reproducible en `Dev/benchmarks/`;
- replay o consulta versionada en `Dev/scripts/` cuando sea reutilizable;
- resumen en los canónicos de entrada y lifecycle;
- resultado de tests y compilación;
- estado de despliegue y evidencia live;
- pendientes fuera de muestra.

## Cadencia

Ejecutar AIC:

- diariamente sobre las mayores pérdidas del día;
- inmediatamente ante una pérdida individual que exceda el cap esperado;
- semanalmente sobre la cohorte completa para detectar deriva;
- después de cambios de sizing, leverage, SL/TP, supervisores o routing.

## Registro de ejecuciones

### AIC-20260807-01 — FUTURES SHORT: entrada agotada y protección ausente

- Cohorte amplia: 255 cierres, 203 tesis, 60.1% WR, `-$474.83`.
- Rama A: SHORT a `<=0.5%` del soporte con RSI `<50`, salvo breakdown confirmado. En 30 tesis afectadas evitó `-$239.65`, sacrificó `+$66.23` y mejoró el contrafactual en `+$173.42`.
- Rama B: 0/9 incidentes tenían telemetry FUTURES; se repararon `qty`, ID estable, PnL contractual y velocidad normalizada. El hard-risk de 2s ahora aplica `max_loss_usd_per_trade` con `max_loss_usd_stop`.
- Validación: 69 tests passed, 2 skipped; compilación limpia.
- Producción: `RUNNING`, hard-risk y telemetry sin error; 21 snapshots recientes para 3 posiciones FUTURES tras el restart.
- Evidencia: `Dev/benchmarks/futures_short_near_support_gate_20260807.json` y `Dev/benchmarks/incident_preopen_1h_replay_20260807.json`.
- Pendiente: medir fuera de muestra la mejora neta y la tasa de `SHORT_NEAR_SUPPORT_EXHAUSTION` durante 5-7 días.

### AIC-20260913-01 — `SEIUSDT` FUTURES SHORT: stop forzado, slippage y cierre con triggerPrice

- **Cohorte**: 1 tesis (`SEIUSDT`, `RANGE_SCALP`, `SHORT_SUPPORT_BREAKDOWN`), `trades.id=2386`.
- **Resumen**: entrada `0.04440`, cierre por stop Binance, `realized_profit_raw=-20.90`, `pnl=-22.07`. El `trades_closed.exit_price=0.0447` era el trigger, no el fill real (`~0.04488`).
- **Rama A — No abrir**:
  - El cap `$18` acercó el stop a `0.0447` (0.68 %), por debajo de la resistencia `0.04496`.
  - El notional `$1,915` era demasiado alto para `SEI` a `$0.044`; el contrato solo reservaba `slippage` de 5.2 bps.
  - El LONG Spot alternativo (`LONG_SUPPORT_BOUNCE`) recibió `notional=0` por falta de `position_size_usdt`, eliminando la comparación bilateral.
- **Rama B — Proteger después de abrir**:
  - El cierre por Binance ejecutó como `STOP_MARKET` y el fill real estuvo 41 bps por encima del trigger.
  - `position_telemetry` y `hard_risk_monitor` usaban `pos.exit_price=0.0447` (trigger), no el fill real.
- **Código / Configuración**:
  - `opportunity_contract` acercaba el stop en vez de escalar notional.
  - `futures_client` persistía `exit_price` y `realized_gross_pnl` con `triggerPrice`.
  - `slippage_pct=0.00052` global no ajustaba por tick/ATR/liquidez.
  - `ready_candidates` `max_age_min=25` permitió un setup de 31 minutos.
- **Corrección implementada**:
  - `opportunity_contract` escala notional al stop; `directional_arbiter` inyecta `position_size_usdt` en LONG; `futures_client` usa fill real; `staged_entry` TTL `RANGE_SCALP` 15 min.
- **Tests**: `591 passed, 3 skipped, 59 warnings`.
- **Evidencia**: `trades_audit` filas `2026-09-14 00:30–00:54` para `SEIUSDT`; `trades.id=2386`; `ready_candidate.id=5380`.
- **Pendiente**: medir fuera de muestra el efecto del sizing ajustado y la tasa de `RANGE_SCALP` descartados por TTL en 15 min.
