# Dev folder

Este directorio contiene notas y experimentos para el desarrollo del agente de trading.

- `notes.md` — apuntes de diseño y mejoras.
- `experiments.md` — pruebas y resultados de estrategias.
- `scripts/` — scripts de apoyo para pruebas manuales.
- `phase4_gate_runner.py` — runner operativo de Fase 4 (drifts criticos + trazabilidad/persistencia + performance + paridad secundaria).
- `phase4_auto_remediate.py` — remediacion automatica de Gate 1/2 (drift editable + limpieza de trazabilidad base).

## Phase 4 gate runner

Capturar baseline pre-ajuste (Gate 4 referencia):

```bash
python Dev/phase4_gate_runner.py capture-baseline --out Dev/phase4_pre_gap.json
```

Evaluar gates y emitir decision final:

```bash
python Dev/phase4_gate_runner.py evaluate \
	--pre-gap-file Dev/phase4_pre_gap.json \
	--out Dev/phase4_gate_report.json
```

Opcional para runtimes legacy de auditoria:

```bash
python Dev/phase4_gate_runner.py evaluate --allow-event-aliases
```

Remediar Gate 1 y Gate 2 automaticamente:

```bash
python Dev/phase4_auto_remediate.py --out Dev/phase4_remediation_report.json
```
