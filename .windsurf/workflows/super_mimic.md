---
description: Run and deploy Super MIMIC experiments
---

# Super MIMIC workflow

1. Ensure DB migration is applied:
   ```bash
   docker-compose exec db psql -U $POSTGRES_USER -d $POSTGRES_DB -f /app/migrations/019_super_mimic_tables.sql
   ```

2. Open dashboard → tab **Super MIMIC**.

3. Click **🎯 Capturar fingerprint** to capture current regime fingerprint.

4. Click **🔥 Warm-start configs** to see historical configs for the selected mode/profile.

5. Choose windows and strategy:
   - `quick`: MIMIC sweep only
   - `standard`: sweep + validation
   - `full`: predictor retrain + sweep + validation

6. Click **Crear experimento**.

7. Click **▶ Ejecutar** to start the background worker.

8. Wait for status `completed`. Monitor log panel.

9. When the deploy gate is green:
   - PnL/día > 0
   - Drawdown < 15%
   - trades >= 3
   - attractors >= 1
   click **🚀 Desplegar config (manual)**. This calls `POST /super-mimic/experiment/{id}/deploy`, which:
   - Re-evaluates the deploy gate.
   - Applies the recommended config via the same whitelist/persistence used by `PATCH /config/kelly` (active immediately, no restart).
   - Records an audit trail in `super_mimic_deployments`.

10. Optional: click **↩ Rollback** to call `POST /super-mimic/experiment/{id}/rollback`, which restores the previous `kelly_config_override` payload and records a rollback audit entry.
