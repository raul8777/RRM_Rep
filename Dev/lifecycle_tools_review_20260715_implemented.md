# Revisión e implementación: Ciclo de vida + ONDO + TP floor

Fecha: 2026-07-15  
Estado: implementado y verificado

## Acciones aplicadas

1. **Código: floor de TP por categoría** en `run_trades.py` ~L6112-6133.
   - El bloque ATR de SL/TP ya tenía floor de SL; ahora también impone `tp_price >= price * (1 + category.take_profit_pct)`.
   - Previene R:R invertida como ETH SL -4.25% / TP +1.10%.

2. **DB: remediación de TP ajustado en posiciones abiertas**.
   - ETHUSDT: TP 1953.50 → 1999.76 (+3.5%)
   - ONDOUSDT: TP 0.36306 → 0.37857 (+7.0%)
   - UNIUSDT: TP 3.68843 → 3.75498 (+3.5%)
   - XLMUSDT: TP 0.19005 → 0.19334 (+3.5%)
   - Auditoría: eventos `tp_floor_remediate`.

3. **DB: limpieza de zombie LINKUSDT**.
   - `quantity=0E-8` en `trades_open`; movido a `trades_closed` con `close_reason=ghost_balance_mismatch` y eliminado de `trades_open`.
   - Auditoría: evento `ghost_position_closed`.

## Posiciones abiertas ahora (post-fix)

| Símbolo | Entry | SL | TP | TP % | Categoría TP |
|---------|-------|-----|-----|------|--------------|
| ETHUSDT | 1932.14 | 1849.96 | 1999.76 | +3.50% | +3.5% |
| ONDOUSDT | 0.3538 | 0.3538 | 0.3786 | +7.00% | +7.0% |
| UNIUSDT | 3.6280 | 3.4737 | 3.7550 | +3.50% | +3.5% |
| XLMUSDT | 0.1868 | 0.1789 | 0.1933 | +3.50% | +3.5% |

LINKUSDT ya no está en `trades_open`.

## ONDO: ¿se cortaron ganancias?

**No se ejecutó una venta real del ONDO actual (id=1055).** Sigue abierto con `qty=454.9`.

Lo que pasó:
- `sso_partial_close` se disparó dos veces (`sso_multilevel_l1`, `sso_momentum_exhausted`) a las 21:18 y 21:08 UTC.
- `scale_out_done=True`, `sso_levels_done=[1]`, `sso_pnl_snapshot` en +0.79%.
- El SL se movió al precio de entrada (0.3538), lo que indica que SSO nivel 1 ejecutó la acción de **mover SL a breakeven**, no de vender.
- No existe registro en `trades_closed` para este `id=1055` porque no hubo venta.
- Sí existe un cierre anterior de ONDO (id=886) a las 19:08 UTC por `prs_peak_protection` con +$2.19; ese sí aparece en `trades_closed`.

**Conclusión:** el usuario vio activación de SSO/PRS/UG y pensó que se había cerrado, pero fue una acción defensiva (SL a entrada). La posición actual sigue abierta y ahora con TP corregido a +7.0%.

## Estado de herramientas de ciclo de vida

| Herramienta | Estado | Evidencia |
|-------------|--------|-----------|
| Hard SL/TP | Activo | 3 cierres post-fix; SLs/TPs almacenados correctos. |
| Break-even | Activo | ONDO SL = entry, aunque flag `breakeven_done` no se persiste en metadata. |
| Trailing stop | Activo (sin activar aún) | Ningún peak superó `trailing_activation_pct`. |
| SSO | Activo | 9 `sso_close`, 2 `sso_partial_close`, `scale_out_done` en ONDO. |
| PRS/Peak Capture | Activo | Cierre ONDO anterior por `prs_peak_protection`. |
| Unified Governance | Activo | 72 decisiones, 3 cierres. |
| Bounce/Rescue gate | Activo | Eventos en LINK (zombie, ya limpiado). |

## Pruebas

- `tests/test_system_health.py -k "not Cat7"`: **116 passed, 12 deselected**, 0 regresiones.

## ONDO — cierres parciales reconstruidos y fix SSO (post-revisión)

Durante el análisis del bajón de PnL de ONDOUSDT se descubrió que los cierres parciales ejecutados por SSO no persistían en `trades_closed`, por lo que el dashboard mostraba solo el PnL no realizado del remanente.

### Hechos

- `trades_open.id=1055` tenía `qty=454.9` y metadata con `scale_out_done=True`, `sso_levels_done=[1]`.
- Audit `sso_partial_close` registró dos ventas parciales en 2026-07-15 21:08 y 21:18 UTC, pero no existían filas en `trades_closed`.
- Las órdenes de venta fueron confirmadas contra fills de Binance.

### Acciones aplicadas

1. **DB:** se eliminó el cierre completo erróneo `trades_closed.id=890`, se restauró `trades_open.id=1055` con qty=454.9 y se insertaron dos cierres parciales:
   - 1,831.6 tokens a $0.36179575 → PnL **+$14.31 USDT**
   - 155.6 tokens a $0.3641 → PnL **+$1.57 USDT**
2. **Código:** `run_trades.py` ahora usa `position_manager.close_position(..., qty_closed=...)` para cierres parciales de SSO, insertando el registro en `trades_closed` y actualizando `quantity` de forma atómica.
3. **Dashboard:** `app.py:/dashboard/summary` expone `realized_pnl` y `total_pnl`; `frontend/dashboard.js` muestra en cada fila PnL no realizado, realizado y total.
4. **Tests:** `test_system_health.py` (116 passed), `test_position_manager.py` + `test_smart_scale_out.py` (16 passed).

### Pendiente opcional

- Persistir `breakeven_done=True` en metadata cuando SL se mueva al precio de entrada para que el dashboard lo muestre.
- Mejorar visibilidad del dashboard para estados intermedios: trailing pending/active, breakeven, SSO snapshots, UG last decision.

## Archivos modificados

- `run_trades.py`: floor de TP en ATR SL/TP entry path **+ fix SSO partial close persistence**.
- `app.py`: endpoint `/dashboard/summary` expone `realized_pnl`/`total_pnl` para posiciones abiertas.
- `frontend/dashboard.js`: tabla de posiciones abiertas muestra PnL no realizado, realizado y total.
- `tmp_lifecycle_remediate.py` / `.windsurf/plans/fix_ondo_restore.py`: scripts de reconstrucción DB (temporales).
- `Dev/lifecycle_tools_review_20260715_implemented.md`: este reporte.
