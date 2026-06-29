---
description: How to audit, change, and persist trading configuration
---

# Configuration Governance Workflow

## Rules

1. **Single source of truth**: canonical defaults live only in `config_helpers.py::_PARAM_DEFAULTS`.
2. **Runtime overrides**: only via `trading_runtime_state` (DB) or environment variables declared in `config.py`.
3. **No new hardcodes**: never add `getattr(settings, 'X', hardcode)` or magic constants in business logic.
4. **Traceability**: every config change must be saved as a snapshot in `trading_runtime_state` and audited in `trades_audit`.

## Change a parameter

1. Pause new entries:
   ```bash
   docker exec crypto-agent-app python /app/tmp_pause_new_entries.py
   ```
2. Snapshot current config:
   ```bash
   docker exec crypto-agent-app python /app/tmp_save_snapshot.py
   ```
3. Update the canonical default in `config_helpers.py::_PARAM_DEFAULTS` (and `config.py` if it has the field).
4. If the parameter is accessed via `getattr`, migrate the call site to `get_param(settings, 'name')`.
5. Rebuild the container:
   ```bash
   docker-compose -f docker-compose.yml up -d --build --force-recreate app
   ```
6. Run the audit:
   ```bash
   docker exec crypto-agent-app python /app/tmp_audit_config.py
   ```
   Verify `Critical mismatches: 0`.
7. Run tests:
   ```bash
   docker exec crypto-agent-app python -m pytest /app/tests --ignore=/app/tests/test_integration.py --ignore=/app/tests/test_webhook.py -k "not TestCat7" -q --tb=short
   ```
8. Resume new entries by deleting the pause key:
   ```bash
   docker exec crypto-agent-app python -c "from db_client import get_db_client; c=get_db_client(); c.execute_update('DELETE FROM trading_runtime_state WHERE key=%s', ('pause_new_entries',)); print('resumed')"
   ```

## Audit current configuration

```bash
docker exec crypto-agent-app python /app/tmp_audit_config.py
```

Check `audit_getattr.txt` for any `CRITICAL` mismatch and fix them using the change workflow above.

## Verify runtime config matches a backtest

1. The baseline is defined in `config_helpers.py::_PARAM_DEFAULTS` and annotated with backtest IDs (e.g., `run_0812`).
2. Use `config_helpers.dump_active_config(settings)` to compare runtime values against the baseline.
3. Any deviation must be an explicit override stored in `trading_runtime_state` with a `run_id` or backtest reference.
