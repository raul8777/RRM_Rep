# Reglas de operación — crypto-agent

> Archivo vivo. Añadir patrones, decisiones de diseño y reglas de operación que afecten el comportamiento del sistema.

---

## 1. Staged entry lifecycle

- Las posiciones NO se abren inmediatamente tras el QUBO. El candidato pasa primero al estado `ready` en `ready_candidates`.
- Estados válidos: `ready`, `validated`, `opened`, `discarded`, `expired`.
- Un candidato `ready` debe acumular `required_positive_checks` positivas antes de pasar a `validated`.
- Solo los candidatos `validated` que sigan en el top QUBO se ejecutan (`opened`).
- Si un candidato falla un re-chequeo, pasa a `discarded` con `discard_reason`.

## 2. Re-chequeos

- `inter-cycle`: evaluación entre ciclos. Se ejecuta como parte de `run_trades.execute_once`.
- `intra-cycle`: validación dentro del mismo ciclo, controlada por el scheduler de `orchestrator.py`.
- Cada re-chequeo genera un registro en `ready_candidate_checks` con `check_type`, `positive`, `execution_ms` y features.
- El `price_source` puede ser `avg` (precio promedio) o `ticker` (último precio), configurable por régimen.

## 3. Configuración por régimen

- `staged_entry_enabled` activa/desactiva el lifecycle global.
- `staged_entry_regime_config` contiene por régimen: `enabled`, `required_positive_checks`, `checks_per_cycle`, `max_cycles`, `max_age_min`, `intra_mode`, `price_source`.
- SPOT_BULL: `required_positive_checks` por defecto 2, `max_age_min` 60, `price_source` `avg`.
- RANGE_SCALP: `required_positive_checks` 2, `max_age_min` 20, `intra_mode` `predictor`.
- FUTURES_SHORT y PRESERVATION: desactivados por defecto hasta tener gates calibrados.

## 4. MIMIC

- `SimulationDatabaseClient` implementa `ready_candidates` y `ready_candidate_checks` en memoria vía `_StagedTables`.
- El sweep `Dev/scripts/mimic_staged_entry_sweep.py` fuerza un régimen y calibra `staged_entry_regime_config`.
- En MIMIC se usa `forced_regime` para garantizar que el lifecycle reciba candidatos; `entry_profile_gate` se simula con el perfil forzado.

## 5. Rollback inmediato

```
PATCH /config/kelly {"staged_entry_enabled": false}
```

## 6. Patrones de bug recurrentes

- **MAX_CYCLES_REACHED antes de validar**: asegurar `max_cycles >= required_positive_checks + 1` para dar al menos `required_positive_checks` oportunidades.
- **Ready candidate descartado tras validar**: `_maybe_expire` debe saltar filas ya `validated`.
- **DB no inicializada en simulación**: `analysis_cache` debe caer a memoria cuando `connection_pool` no existe.

## 7. Activación en producción

- `config.py` y `config_helpers.py` tienen `staged_entry_enabled=True` por defecto.
- SPOT_BULL y RANGE_SCALP: `enabled=True`, `required_positive_checks=2`, `max_cycles=3`.
- FUTURES_SHORT y PRESERVATION: `enabled=False` hasta calibrar gate específico.
- Hot-config disponible vía `PATCH /config/kelly` con `staged_entry_enabled` o `staged_entry_regime_config`.

## 8. Monitoreo en producción

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
- `validated` crece pero no pasa a `opened` → revisar `get_openable_candidates` y `_buy_top_symbols`.
- Errores `[StagedEntry] staged entry error` en logs → revisar excepción y rollback temporal.
