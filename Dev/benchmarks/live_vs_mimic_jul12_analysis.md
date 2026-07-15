# Análisis: Resultados negativos del 12 jul 2026 — Live vs MIMIC

## Resumen ejecutivo

El 12 de julio 2026 el sistema cerró **29 trades** en SPOT con un PnL total de **-24.05 USDT**. La causa principal no es que "los sweeps MIMIC mintieron", sino que **el entorno live está operando con reglas de entrada mucho más permisivas que las que MIMIC simula**, y el predictor PnL está siendo ignorado porque el Learning Engine sigue en modo bootstrap para la mayoría de los símbolos.

## 1. Datos de producción (12 jul 2026)

- **Total trades cerrados:** 29
- **PnL total:** -24.05 USDT
- **Régimen predominante:** RANGE_SCALP (26 de 29)
- **Perfil activo:** A
- **Principal mecanismo de pérdida:** Stop Loss (9 trades, -52.77 USDT)

### Pérdidas por símbolo

| Símbolo | Trades | PnL (USDT) |
|---------|--------|------------|
| SOLUSDT | 4 | -14.31 |
| BTCUSDT | 4 | -8.39 |
| SUIUSDT | 3 | -2.93 |
| BNBUSDT | 2 | -2.17 |
| PEPEUSDT | 8 | -1.83 |
| ADAUSDT | 1 | -0.63 |

### Ganancias por símbolo

| Símbolo | Trades | PnL (USDT) |
|---------|--------|------------|
| NEARUSDT | 3 | +4.77 |
| TRXUSDT | 1 | +0.88 |
| DOGEUSDT | 3 | +0.58 |

### Cierres por motivo

| Motivo | Count | PnL (USDT) |
|--------|-------|------------|
| stop_loss | 9 | -52.77 |
| sso_target_contribution | 2 | +7.39 |
| profit_predexit_capture | 1 | +18.97 |
| profit_realization_supervisor_partial | 2 | +3.10 |
| profit_realization_supervisor_full | 1 | +1.90 |
| active_replacement | 1 | -2.37 |
| manual_close | 2 | -1.63 |
| ghost_balance_mismatch | 7 | +0.30 |
| others | 4 | +0.08 |

## 2. Hallazgo crítico #1: entradas con PnL predicho negativo

- **23 de 29 trades** (79%) tenían `last_pred_pnl` negativo al momento de la entrada o última reevaluación.
- **17 de 29 trades** tenían simultáneamente predicción negativa **y** `vol_spike < 1.0`.
- PnL agregado de esos 17 trades: **-4.99 USDT**.

Esto viola explícitamente la regla documentada: *"El sistema NO debe comprar cuando: PnL predicho negativo con horizonte 4h"*.

### ¿Por qué entraron a pesar del veto del predictor?

En `pnl_service.py:118-119`:

```python
if prediction.get("bootstrap_mode"):
    return True, "bootstrap mode – thresholds relaxed until trade history builds"
```

Cuando el Learning Engine no tiene suficientes trades reales para un símbolo, cae a bootstrap mode y **relaja los umbrales de PnL y probabilidad**. La metadata de las posiciones muestra `bootstrap_mode: true` en múltiples trades.

De acuerdo a la memoria del sistema, al 17 jun 2026 solo había **22 trades reales** vs 34,287 sintéticos. El predictor sigue dominado por datos sintéticos con `bias_correction` cap de 1.5, por lo que las predicciones en vivo son poco fiables y el sistema las ignora.

## 3. Hallazgo crítico #2: entradas con volumen muy bajo

Configuración activa en runtime:

| Parámetro | Valor actual |
|-----------|--------------|
| `entry_quality_max_failures_range_scalp` | **1** |
| `entry_quality_vol_spike_min_range_scalp` | **0.35** |
| `range_scalp_max_rsi` | 60.0 |
| `range_scalp_min_rsi` | 25.0 |

Esto significa que en RANGE_SCALP se permite **exactamente 1 dimensión de calidad fallida**. Como el umbral de volumen es solo 0.35x, un token puede entrar con vol_spike de 0.18x (BTC), 0.007x (SUI), 0.0038x (BTC) si las otras dimensiones (RSI, strength) pasan.

Ejemplos concretos del 12 jul:

- **BTCUSDT** 19:50: `vol_spike_at_entry=0.1809`, `last_pred_pnl=-2.38`, cerrado por stop_loss (-4.09 USDT)
- **SUIUSDT** 20:07: `vol_spike_at_entry=0.0643`, `last_pred_pnl=-14.77`, cerrado por stop_loss (-7.69 USDT)
- **BNBUSDT** 20:56: `vol_spike_at_entry=0.4886`, `last_pred_pnl=-1.62`, cerrado por stop_loss (-1.94 USDT)

La regla de entrada validada en jun 2026 exigía `vol_spike > 1.5x` para confirmar convicción. La config actual permite entradas con casi nulo volumen.

## 4. Hallazgo crítico #3: desconexión entre perfiles de régimen y runtime

La base de datos tiene perfiles RANGE_SCALP A/B/C actualizados el 12 jul a las 15:02 con parámetros agresivos:

- Perfil A: `min_success_probability=0.48`, `kelly_max_usdt=2500.0`, `kelly_dynamic_base_pct=0.26`

Sin embargo, el `kelly_config_override` activo en runtime muestra valores conservadores:

- `min_success_probability: 0.52`
- `kelly_max_usdt: 1000.0`
- `kelly_dynamic_base_pct: 0.18`

Esto indica que `_apply_regime_params` en `run_trades.py` no re-aplicó los perfiles actualizados. El código usa `_last_applied_regime_mode` y `_last_applied_regime_profile` para evitar re-aplicaciones, pero **no tiene hash del contenido del perfil**, por lo que si el perfil A cambia de contenido pero sigue siendo "A", el sistema salta la actualización.

Nota: Aunque se aplicaran los parámetros agresivos del perfil A, probablemente empeorarían el resultado live (más tamaño por trade, menor umbral de probabilidad), pero el punto es que live no está corriendo la config que se consideró "óptima" en el sweep.

## 5. ¿Por qué MIMIC no predijo esto?

### 5.1. El sweep actual no ha llegado a julio 2026

Estado del sweep (12 jul 2026 ~20:00):

- **Jobs completados:** 180 / 15,552
- **Ventana actual:** `win_20260303_20260317` (marzo 2026)
- **Resultados acumulados en disco:** 397 resultados, todos de la misma ventana de marzo
- **PnL agregado de esa ventana:** +1,041 USDT (positivo)

Los resultados "negativos/cero" que el usuario ve son probablemente configuraciones individuales dentro de esa ventana de marzo con PnL 0 o negativo. No tienen relación con lo que pasó el 12 de julio.

### 5.2. MIMIC no simula el modo bootstrap

El sweep `_base_settings` fija:

```python
settings.min_signal_strength = 0.55
settings.min_success_probability = 0.55
settings.stop_loss_pct = 3.36
settings.kelly_max_usdt = 1000.0
settings.category_max_positions_enabled = True
```

Esto es más conservador que la config live actual (`min_signal_strength=0.45`, `stop_loss_pct=2.5`, `category_max_positions_enabled=False`). Además, MIMIC usa el Learning Engine con datos históricos/sintéticos, no en modo bootstrap, por lo que el `pnl_service.validate` **sí** rechaza predicciones negativas.

### 5.3. El sweep anterior max_cycles=5 era insuficiente

La memoria del 11 jul indica:

> "Limitación: 5 ciclos insuficientes para trades completos. Sweep max_cycles=50 pendiente."

Un trade RANGE_SCALP con SL 2.5% / TP 1.8% puede necesitar decenas de ciclos de 5-60 min. Con solo 5 ciclos, el sweep no captura ni los stops ni los take-profits reales, por lo que su "optimalidad" no traduce a live.

## 6. Comparativa live vs MIMIC esperado

| Símbolo | PnL live 12 jul | Régimen | Cierre | vol_spike | pred_pnl | Comentario |
|---------|-----------------|---------|--------|-----------|----------|--------------|
| SOLUSDT | -14.31 | RANGE_SCALP | stop_loss | 0.73x | -3.44 | Volumen bajo + pred negativa |
| BTCUSDT | -8.39 | RANGE_SCALP | stop_loss (x2), manual ghost | 0.00-0.18x | -0.07 a -2.38 | Volumen casi nulo |
| SUIUSDT | -2.93 | RANGE_SCALP | stop_loss + active_replacement | 0.01-0.46x | -4.39 a -14.77 | Predicción muy negativa |
| BNBUSDT | -2.17 | RANGE_SCALP | stop_loss + manual | 0.49x | -1.62 | Pred negativa |
| PEPEUSDT | -1.83 | RANGE_SCALP | mixto | 0.00-1.36x | -0.93 a -5.30 | Alta volatilidad, resultado neto neutral |
| NEARUSDT | +4.77 | RANGE_SCALP | PRS parcial+full | 0.24x | -1.11 a -2.78 | Excepción ganadora con bajo volumen |
| TRXUSDT | +0.88 | RANGE_SCALP | SSO stagnation | 1.58x | -1.89 | Único con volumen decente |
| DOGEUSDT | +0.58 | RANGE_SCALP | mixto | 2.90-3.24x | -0.57 | Alto volumen, resultado mixto |

## 7. Conclusiones

1. **Los sweeps MIMIC no son el problema.** El sweep actual apenas va en la ventana de marzo y aún no ha procesado julio. El sweep anterior max_cycles=5 era demasiado corto.
2. **El problema es la calidad de entrada en vivo.** El sistema está comprando señales que el propio predictor marca como perdedoras (PnL negativo) y con volumen extremadamente bajo (<1.0x, muchas <0.5x).
3. **La causa raíz es la combinación de:**
   - Learning Engine en bootstrap mode (relaja validación de PnL/probabilidad).
   - `entry_quality_max_failures_range_scalp=1` permitiendo una dimensión fallida.
   - `entry_quality_vol_spike_min_range_scalp=0.35` (umbral de volumen ridículamente bajo).
   - Desconexión entre perfiles de régimen actualizados y runtime (`kelly_config_override` con valores conservadores no se actualiza).
4. **Las pérdidas se concentran en SL de trades de baja calidad.** 9 stop_losses representan -52.77 USDT; sin ellos, el día habría sido positivo (+28.72 USDT) a pesar de los ghosts y cierres manuales.

## 8. Recomendaciones

1. **No usar los resultados del sweep actual para explicar julio hasta que llegue a las ventanas de julio con max_cycles>=50.**
2. **Re-calibrar el Learning Engine** con los trades reales cerrados recientemente (`POST /learning/run`) para salir del bootstrap mode.
3. **Restringir la puerta de calidad A4 en RANGE_SCALP:**
   - `entry_quality_max_failures_range_scalp = 0`
   - `entry_quality_vol_spike_min_range_scalp >= 1.0` (alineado con la regla validada de 1.5x)
4. **Corregir `_apply_regime_params`** para que detecte cambios de contenido del perfil (hash) y re-aplique a `kelly_config_override`.
5. **Considerar pausar entradas manuales** y subir temporalmente `min_success_probability` hasta que el predictor deje de estar en bootstrap para la mayoría de los símbolos.
