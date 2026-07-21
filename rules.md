# Reglas de operación — crypto-agent

> Archivo vivo. Añadir patrones, decisiones de diseño y reglas de operación que afecten el comportamiento del sistema.

---

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
- Los cuatro regímenes están activados de forma conservadora y con `max_open_per_cycle=1`.
- SPOT_BULL, FUTURES_SHORT y PRESERVATION requieren perfil A/B/C, predictor válido y EV neto actual estrictamente mayor a `min_expected_pnl`.
- RANGE_SCALP requiere perfil A/B/C y predictor íntegro, pero se prioriza por calidad de reversión; el signo del predictor long genérico no es un veto.
- `preservation_upgrade_to_futures=False` mantiene PRESERVATION independiente; solo se convierte en FUTURES_SHORT al activarlo explícitamente.
- `range_reversion_predictor_mode=shadow` registra EV, probabilidad, target y stop de reversión sin bloquear aperturas.

## 5. MIMIC

- `SimulationDatabaseClient` implementa `ready_candidates` y `ready_candidate_checks` en memoria vía `_StagedTables`.
- El sweep `Dev/scripts/mimic_staged_entry_sweep.py` fuerza un régimen y calibra `staged_entry_regime_config`.
- En MIMIC se usa `forced_regime` para garantizar que el lifecycle reciba candidatos; `entry_profile_gate` se simula con el perfil forzado.

## 6. Rollback inmediato

```
PATCH /config/kelly {"staged_entry_enabled": false}
```

## 7. Patrones de bug recurrentes

- **MAX_CYCLES_REACHED antes de validar**: asegurar `max_cycles >= required_positive_checks + 1` para dar al menos `required_positive_checks` oportunidades.
- **Ready candidate descartado tras validar**: `_maybe_expire` debe saltar filas ya `validated`.
- **DB no inicializada en simulación**: `analysis_cache` debe caer a memoria cuando `connection_pool` no existe.

## 8. Activación en producción

- `config.py` y `config_helpers.py` tienen `staged_entry_enabled=True` por defecto.
- SPOT_BULL y RANGE_SCALP: `enabled=True`, `required_positive_checks=2`, `max_cycles=3`.
- FUTURES_SHORT y PRESERVATION: `enabled=True` con predictor válido, EV neto positivo y una apertura máxima por ciclo; sus políticas siguen en calibración.
- El predictor de reversión de RANGE_SCALP permanece en sombra hasta superar al baseline fuera de muestra en EV neto y calibración de probabilidad.
- Hot-config disponible vía `PATCH /config/kelly` con `staged_entry_enabled` o `staged_entry_regime_config`.

## 9. Monitoreo en producción

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
