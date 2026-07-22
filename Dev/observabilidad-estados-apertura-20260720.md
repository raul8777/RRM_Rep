# Observabilidad de estados de apertura

**Fecha:** 20 Jul 2026  
**Estado:** Implementado y desplegado

## Objetivo

Evitar que el dashboard presente cualquier señal sin decisión final como una orden pendiente de apertura.

## Estados de presentación

| Estado | Significado | Accionable |
|---|---|---|
| `En validación` | Candidata staged en estado `ready`; aún reúne checks. | No |
| `Lista para abrir` | Candidata `validated`, seleccionada por prioridad y dentro del cupo `max_open_per_cycle`. | Sí |
| `Validada · en cola` | Candidata `validated` que no alcanzó el cupo de apertura del ciclo. | No |
| `Bloqueada por régimen` | El régimen de la candidata no coincide con el régimen activo. | No |
| `Duplicada · posición abierta` | Ya existe una posición abierta para el símbolo. | No |
| `Expirada` | Terminó el TTL de la señal o la candidatura alcanzó su edad máxima. | No |
| `Rechazada · descartada` | Un gate final descartó la candidatura. | No |
| `Sin decisión final` | Señal reciente sin decisión persistida; no representa una orden. | No |
| `Aceptada · abierta` / `Ejecutada` | Resultado final de una candidatura o señal. | No |

## Contrato API

- `GET /signals` y `GET /signals/recent` añaden `display_status`, `display_label` y `actionable` sin modificar la señal almacenada.
- `GET /dashboard/selection-observability` añade los mismos campos por candidatura y `display_counts`.
- La clasificación consulta únicamente el régimen activo, las posiciones abiertas y los datos staged persistidos; no modifica gates, ranking ni ejecución de órdenes.

## Regla de apertura

La etiqueta **Lista para abrir** se asigna exclusivamente a candidatas `validated` que:

1. pertenecen al régimen activo;
2. no duplican una posición abierta;
3. están dentro del ranking de prioridad; y
4. caben dentro de `max_open_per_cycle`.

El resto de las candidatas validadas se muestran como **Validada · en cola**.

## Archivos

- `app.py`: clasificación de estados y enriquecimiento de respuestas API.
- `frontend/dashboard.js`: etiquetas y contadores visibles.
- `tests/test_selection_display_states.py`: regresiones de estados y ranking.

## Verificación

```powershell
python -m pytest tests/test_selection_observability.py tests/test_selection_display_states.py tests/test_open_positions_detail.py -q
```

Resultado de implementación: `8 passed`.

## Actualización: régimen, latido y candidatas validadas

- El régimen mostrado por dashboard y health se resuelve primero desde `effective_runtime_selection_snapshot`; el valor del orquestador queda como fallback.
- Cada retorno del ciclo posterior a la detección de régimen devuelve `regime_mode` y `next_cycle_minutes`, incluidos `NO_SIGNALS` y `PRE_QUBO_FILTERED`.
- Si no sobreviven señales nuevas, el ciclo reevalúa candidatas staged y puede abrir las `validated` mediante el mismo flujo principal de compra, preflight y auditoría.
- La observabilidad de selección expone `latest_selection_cycle`, con hora, régimen, outcome, razón y conteos de universo, QUBO y órdenes.

## Verificación de actualización

```powershell
python -m pytest tests/test_selection_observability.py tests/test_selection_display_states.py tests/test_staged_entry.py -q
```

Resultado: `21 passed`.
