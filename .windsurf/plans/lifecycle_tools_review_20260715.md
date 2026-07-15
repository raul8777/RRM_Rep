# Revisión: Ciclo de vida de posiciones abiertas

Fecha: 2026-07-15 (post-fix SL ~21:15 UTC-6)  
Alcance: posiciones abiertas actuales + cierres/auditoría desde el fix del SL.

## Resumen ejecutivo

**Los motores del ciclo de vida SÍ están funcionando** (Unified Governance, PRS, SSO, bounce/rescue, PredExit/Peak Capture). Sin embargo, la mayoría de las posiciones abiertas actuales son demasiado jóvenes o no han alcanzado los umbrales de activación de trailing/breakeven. Se detectaron **dos problemas concretos**: un **zombie LINKUSDT (qty=0)** que sigue generando eventos SSO/bounce, y un **TP de entrada demasiado ajustado** causado por el mismo bug de ATR que el SL (ahora sin floor para TP).

## Posiciones abiertas actuales (5 registros en DB, 1 zombie)

| Símbolo | Precio entrada | SL | TP | Distancia SL | Distancia TP | Edad | Categoría | Notas |
|---------|---------------|-----|-----|--------------|--------------|------|-----------|-------|
| ONDOUSDT | 0.3538 | 0.3538 | 0.3631 | 0.00% | +2.63% | 0.8h | NARRATIVE | `scale_out_done=True`. TP muy por debajo del 7% de categoría. |
| XLMUSDT | 0.1868 | 0.1789 | 0.1901 | -4.22% | +1.76% | 2.2h | MIDCAP_ALT | Ningún motor activado aún. TP < 3.5% categoría. |
| UNIUSDT | 3.6280 | 3.4737 | 3.6884 | -4.25% | +1.66% | 2.4h | MIDCAP_ALT | Ningún motor activado aún. TP < 3.5% categoría. |
| ETHUSDT | 1932.1400 | 1849.9564 | 1953.5036 | -4.25% | +1.10% | 2.6h | LARGE_CAP | Ningún motor activado. TP muy por debajo de 3.5%. |
| LINKUSDT | 8.5960 | 8.2951 | 8.9153 | -3.50% | +3.71% | 7.5h | MIDCAP_ALT | **ZOMBIE: qty=0E-8**. Sigue emitiendo `sso_close` y `bounce_gate_fired`. |

### Por qué no se ve trailing/breakeven en las posiciones actuales

Para que se active el trailing stop en SPOT se requiere que el precio suba al menos hasta `trailing_activation_pct` de la categoría:

| Categoría | trailing_activation_pct |
|-----------|-------------------------|
| LARGE_CAP (ETH) | 2.5% |
| MIDCAP_ALT (XLM, UNI, LINK) | 2.5% |
| NARRATIVE (ONDO) | 5.0% |

Picos máximos observados desde entrada:

| Símbolo | Peak PnL real | ¿Supera activación trailing? |
|---------|---------------|------------------------------|
| ONDO | +2.37% | No (falta 5.0%) |
| XLM | +0.80% | No |
| UNI | +0.83% | No |
| ETH | +0.17% | No |
| LINK | +0.28% | No |

**Conclusión:** es normal que no haya trailing activado. Las posiciones no han subido lo suficiente.

## Cierres desde el fix del SL (~2026-07-15 17:26 UTC)

| Motivo de cierre | Cantidad | PnL total | PnL promedio |
|------------------|----------|-----------|--------------|
| `profit_realization_supervisor_full` | 1 | +$2.46 | +$2.46 |
| `prs_peak_protection` | 1 | +$2.19 | +$2.19 |
| `unified_governance_loss_defensive_close` | 1 | -$11.19 | -$11.19 |

Esto demuestra que **PRS, Peak Protection y Unified Governance sí están disparando y cerrando posiciones**.

## Eventos de auditoría desde el fix

| Evento | Cantidad | Interpretación |
|--------|----------|----------------|
| `unified_governance_decision` | 72 | UG evalúa cada ciclo. |
| `profit_realization_supervisor_decision` | 13 | PRS genera advisories. |
| `sso_close` | 9 | SSO ha cerrado o intentado cerrar posiciones. |
| `bounce_gate_fired` | 9 | Gate de rescate/rebote ha actuado. |
| `profit_predexit_decision` | 7 | PredExit evaluando peaks. |
| `unified_governance_close_triggered` | 3 | UG determinó cierre total. |
| `sso_partial_close` | 1 | Cierre parcial SSO. |
| `emergency_sl_remediate` | 3 | Auditoría del fix de SL en AVAX/ZEC/LINK. |

**Conclusión:** el pipeline de lifecycle está vivo. El usuario percibe "inactividad" porque las posiciones actuales no han cumplido condiciones de activación o porque el dashboard no muestra claramente los estados intermedios (SSO snapshots, UG decisions hold, breakeven pending).

## Problemas detectados

### 1. LINKUSDT es un zombie (qty=0 en `trades_open`)

- `quantity=0E-8`, `partial_close=True`, `partial_qty_remaining=0`.
- Sigue generando eventos `sso_close` (trigger SSO-6 `sso_bounce_rescue`) y `bounce_gate_fired` cada ciclo.
- Esto consume ciclos y ensucia el audit sin efecto real.
- **Recomendación:** aplicar limpieza similar a `Dev/scripts/_fix_all_s64.py` G0-A para LINKUSDT; o verificar si el ghost-position fix (`run_trades.py` ~L4303) debería haberlo eliminado y por qué no lo hizo.

### 2. TP de entrada demasiado ajustado por ATR sin floor

El mismo bloque de ATR que causó el bug de SL ahora causa TP ajustados:

```python
sl_price = price - atr * 1.5
tp_price = price + atr * 2.5
```

Se aplicó **floor al SL** pero **no al TP**. Resultado: TP por debajo del mínimo de categoría:

| Símbolo | TP almacenado | TP mínimo categoría | Observación |
|---------|---------------|---------------------|-------------|
| ETHUSDT | +1.10% | +3.5% (LARGE_CAP) | Riesgo/recompensa invertido: SL 4.25% vs TP 1.10%. |
| UNIUSDT | +1.66% | +3.5% (MIDCAP_ALT) | |
| XLMUSDT | +1.76% | +3.5% (MIDCAP_ALT) | |
| ONDOUSDT | +2.63% | +7.0% (NARRATIVE) | |

**Impacto:** posiciones se cerrarán por TP muy pronto con R:R negativa, limitando upside y confirmando la sensación de "ganancias estranguladas".

**Recomendación:** aplicar un **floor de TP igual al `take_profit_pct` de categoría** en el mismo bloque donde se aplica el floor de SL.

### 3. ONDOUSDT: SL = precio de entrada (breakeven implícito) sin flag

- SL=0.3538 coincide exactamente con entry=0.3538.
- Esto indica que probablemente se activó el mecanismo de break-even o de "move SL to entry", pero `breakeven_done` sigue como `None` en metadata.
- **Recomendación:** verificar que el estado de breakeven se persista correctamente en `_smart_meta` o en metadata raíz para que el dashboard lo muestre.

## Estado de cada herramienta de ciclo de vida

| Herramienta | Estado | Evidencia |
|-------------|--------|-----------|
| Hard SL/TP | Activo | 3 cierres por SL/TP/UG desde el fix; SLs almacenados correctos. |
| Break-even | Parcial | ONDO muestra SL movido a entrada, pero flag `breakeven_done=None`. |
| Trailing stop | Activo (sin activar aún) | No hay posiciones con peak >= activation_pct. |
| SSO | Activo | 9 `sso_close`, 1 `sso_partial_close`, `scale_out_done=True` en ONDO. |
| PRS | Activo | 13 advisories, 1 cierre full, 1 peak protection. |
| PredExit/Peak Capture | Activo | 7 evaluaciones, reconstrucción de peak en metadata. |
| Unified Governance | Activo | 72 decisiones, 3 cierres ejecutados. |
| Bounce/Rescue gate | Activo | 9 eventos en LINK (zombie). |

## Acciones recomendadas

1. **Limpiar zombie LINKUSDT** de `trades_open` y auditar el motivo (ghost-position fix debería haberlo eliminado).
2. **Aplicar floor de TP por categoría** en `_buy_top_symbols()` junto al floor de SL ya existente.
3. **Verificar persistencia del flag `breakeven_done`** cuando SL se mueve al precio de entrada.
4. **Mejorar visibilidad del dashboard** para mostrar estados de lifecycle: trailing pending/active, breakeven, SSO snapshots, UG last decision.
5. **Monitorear próximas 12-24h** para confirmar que los nuevos TP más amplios permitan que trailing/BE/PRS actúen con R:R positiva.

## Archivos relevantes

- `run_trades.py` `_buy_top_symbols()` (~L6094): ATR SL/TP floor.
- `run_trades.py` `_check_sl_tp_positions()` (~L3148): pipeline de lifecycle.
- `Dev/scripts/_fix_all_s64.py`: limpieza de zombies (G0-A).
- `smart_scale_out.py`, `profit_realization_supervisor.py`, `unified_governance_supervisor.py`, `peak_capture_engine.py`: motores evaluados.
