# Breakeven en Hard-Risk Monitor (s99d)

**Tipo:** canonical-memory  
**Dominio:** lifecycle de salida, protección de ganancias, hard-risk monitor  
**Fecha:** 2026-07-26

---

## 1. Problema observado

Caso `BANKUSDT` (2026-07-26):
- Posición **SPOT LONG** en modo LATERAL / RANGE_SCALP.
- Entrada: `$0.37`, inversión: `$1,479.01`.
- Alcanzó ganancia no realizada de **~$10** (+0.68 %), suficiente para activar breakeven.
- El precio se derrumbó y el hard-risk monitor cerró la posición por `hard_risk_stop` a `$0.36`.
- Resultado: **-$20.22 USDT** (-1.37 %).

**Por qué pasó:**
- El `hard_risk_monitor` revisa stop-loss cada **2 s** pero no activaba breakeven.
- El breakeven solo se activaba en el trade cycle (`_check_sl_tp_positions` en `run_trades.py`), que corre cada **3–5 min**.
- Si el pico de ganancia ocurre entre ciclos, el SL no sube a BE.
- Cuando el precio cae, el monitor cierra al SL original (o al USD stop) sin haber protegido la ganancia.

---

## 2. Fix implementado

Módulo `hard_risk_monitor.py`:
- Nueva función `_try_breakeven(manager, settings, db_client, symbol, pos, price, commission_rate)`.
- Corre cada vez que `monitor_spot_hard_stops_once()` revisa una posición (cada 2 s).
- Si `pnl_pct_net` supera el umbral de BE más agresivo entre regímenes (**0.15 %** para RANGE_SCALP), mueve el SL a **net breakeven**:
  ```
  net_be_pct = 2 * binance_commission_rate + slippage_pct
  net_be_price = entry_price * (1 + net_be_pct)
  ```
- La función se ejecuta **antes** del SL breach check, de modo que el monitor use el SL actualizado.
- El `PositionManager.update_position_sl` conserva la regla de oro: **SL nunca empeora**.
- Cada activación se audita en `trades_audit` con evento `hard_risk_breakeven`.

Configuración añadida:
- `hard_risk_breakeven_enabled` (default `True`) en `config.py`, `config_helpers.py` y `app.py` `_KELLY_EDITABLE_FIELDS`.
- Usa los umbrales existentes `range_scalp_breakeven_pct` y `spot_bull_breakeven_pct`, aplicando el más agresivo (`min`) como red de seguridad.

---

## 3. Relación con el pipeline de salida canónico

Este fix modifica la etapa **Hard SL/TP / hard-risk monitor** del pipeline documentado en `calibrar-ciclo-vida-52fe5d.md`:

```
1. Hard SL/TP (órdenes algo en Binance)  ← ahora incluye BE móvil cada 2s
2. Dust/ghost cleanup
3. Breakeven (trade cycle, 3–5 min)
4. Trailing stop
5. SSO / PRS / UG / Peak Capture
```

El breakeven ya no depende exclusivamente del trade cycle. El hard-risk monitor actúa como **primera línea de protección de ganancias** con latencia de segundos, mientras el trade cycle sigue refinando trailing/SSO/PRS/UG.

---

## 4. Rollback

```
PATCH /config/kelly {"hard_risk_breakeven_enabled": false}
```

También deshabilita el hard-risk monitor completo:
```
PATCH /config/kelly {"range_scalp_short_first_enabled": false, "hard_risk_monitor_enabled": false}
```

---

## 5. Validación

- `tests/test_hard_risk_monitor.py`: 6/6 passed.
  - BE se activa cuando `pnl_pct_net >= 0.15 %`.
  - BE no se activa por debajo del umbral.
  - BE no empeora un SL ya superior.
  - Después de BE, si el precio cae al nuevo SL, el cierre usa el SL de BE (no el original).
- Tests focalizados acumulados: **526 passed** sin fallos vinculados al cambio.
- `test_config_governance.py` requiere entorno Docker (`/app/audit_getattr.txt`) y no se ejecuta local; pre-existente.

---

## 6. Notas operativas

- Este cambio no reemplaza el breakeven del trade cycle; lo complementa con una red de seguridad de baja latencia.
- El umbral de 0.15 % es el más agresivo entre regímenes para asegurar protección temprana. El trade cycle mantiene su propio umbral por régimen.
- Si `trailing_stop_enabled` está activo, el BE del hard-risk monitor también estará activo por defecto.
