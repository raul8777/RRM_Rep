# Parametrizacion Modo/Perfil - Cierre de Fases

Fecha: 2026-07-04
Objetivo: completar implementacion quirurgica para trabajar por modo+perfil sin cambiar logica de trading.

## Fases

1. Fase 1 (P0): wiring base whitelist + runtime apply + cache coherente + startup reload.
2. Fase 2 (P1): expandir palancas criticas de velocidad/riesgo/exposicion por perfil.
3. Fase 3 (P2): observabilidad y validacion no-regresion de cobertura.

## Entregables completados

- Whitelist extendida: app.py (_KELLY_EDITABLE_FIELDS)
- Runtime apply extendido: run_trades.py (_scalar_map)
- Invalidacion cache screener al switch perfil/modo: app.py y run_trades.py
- Recarga startup de overrides persistidos: config_helpers.py + init_components.py
- Script de cobertura: Dev/scripts/profile_param_coverage_report.py

## Verificacion operativa

Comando:

python Dev/scripts/profile_param_coverage_report.py --repo-root . --output Dev/param_coverage_report_2026_07_04.json --enforce-critical

Criterio de cierre:

- critical_missing_in_whitelist == []
- critical_missing_in_scalar_map == []

## Rollback

Cambios son reversibles por archivo en:

- app.py
- run_trades.py
- config_helpers.py
- init_components.py
- Dev/scripts/profile_param_coverage_report.py

No se modifico la estrategia ni el orden de decision del ciclo execute_once.
