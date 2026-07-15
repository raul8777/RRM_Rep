# Diagnóstico de Cuello de Botella — RANGE_SCALP (2026-07-13)

## 1. Resumen Ejecutivo

- **Capital live de referencia**: $5,058.00 USDT
- **Target diario**: $120.00 USDT
- **Jobs analizados**: 1245
- **Jobs con trades > 0**: 988
- **Mejor PnL (ventana 15d)**: $41.20 (sweep $2,910) → **$71.61 escalado a $5,058**
- **Mejor PnL/día**: $34.36
- **Gap vs target diario**: $85.64
- **PnL promedio (todos los jobs)**: $-1.73 escalado
- **PnL promedio (jobs con trades)**: $-1.60 escalado
- **Porcentaje de jobs con PnL positivo**: 47.55%


## 2. Mejores Configuraciones por PnL Total

| Rank | Perfil | Window | PnL | PnL/día | Trades | DD% | CDR | Params |
|---|---|---|---|---|---|---|---|---|
| 1 | A | win_20260415_20260430 | 41.2 | 19.77 | 206 | 0.12 | 0.8226 | `{"min_signal_strength": 0.55, "min_success_probability": 0.55, "stop_loss_pct": 4.5, "kelly_max_usdt": 1000.0, "kelly_dynamic_base_pct": 0.18, "max_positions_spot_bull": 5}` |
| 2 | C | win_20260501_20260515 | 39.16 | 18.79 | 241 | 0.0263 | 0.8999 | `{"min_signal_strength": 0.45, "min_success_probability": 0.55, "stop_loss_pct": 2.5, "kelly_max_usdt": 800.0, "kelly_dynamic_base_pct": 0.22, "max_positions_spot_bull": 5}` |
| 3 | A | win_20260401_20260415 | 36.3 | 17.42 | 60 | 0.0353 | 0.7386 | `{"min_signal_strength": 0.55, "min_success_probability": 0.5, "stop_loss_pct": 4.5, "kelly_max_usdt": 1200.0, "kelly_dynamic_base_pct": 0.26, "max_positions_spot_bull": 5}` |
| 4 | B | win_20260401_20260415 | 33.6 | 16.13 | 136 | 0.1051 | 0.437 | `{"min_signal_strength": 0.6, "min_success_probability": 0.5, "stop_loss_pct": 3.36, "kelly_max_usdt": 800.0, "kelly_dynamic_base_pct": 0.18, "max_positions_spot_bull": 5}` |
| 5 | A | win_20260401_20260415 | 32.15 | 15.43 | 134 | 0.4684 | 0.8233 | `{"min_signal_strength": 0.55, "min_success_probability": 0.45, "stop_loss_pct": 4.5, "kelly_max_usdt": 1200.0, "kelly_dynamic_base_pct": 0.26, "max_positions_spot_bull": 8}` |

## 3. Aggregate por Perfil

| Perfil | Jobs | Avg PnL | Median PnL | Avg Sharpe | Median Sharpe | Avg DD% | Pos PnL% | Avg Trades | Avg CDR |
|---|---|---|---|---|---|---|---|---|---|
| A | 321 | -2.94 | -2.26 | -11.89 | -12.87 | 0.28 | 25.23 | 109.00 | 0.45 |
| B | 431 | -1.07 | 0.09 | 1.13 | 1.64 | 0.28 | 55.22 | 93.55 | 0.49 |
| C | 493 | 0.34 | 0.09 | 3.26 | 1.64 | 0.26 | 55.38 | 81.57 | 0.47 |

## 4. Capital Deployment Efficiency (CDR)

- **CDR promedio (todos los jobs)**: 47.12%
- **CDR promedio (jobs con trades)**: 55.22%
- **Capital deployado promedio** (a $5,058): $2383.18
- **Target CDR 75%**: $3793.50
- **Capital inactivo extra para alcanzar 75% CDR**: $1410.32
- **Observación**: CDR bajo indica que el sistema pasa mucho tiempo en cash. Root causes probables: `category_max_positions_enabled`, `max_positions_*` bajos, predictor conservador, o ausencia de señales en el modo.


## 5. Frecuencia de Trades

- **Jobs con trades**: 988
- **Trades promedio por job**: 116.9
- **Median trades**: 99.00
- **PnL promedio por trade**: $-0.159
- **Trades necesarios para $120/día** (si PnL/trade promedio se mantiene): -754 trades/día


## 6. Sensibilidad de Parámetros

### min_signal_strength

| Valor | Jobs | Median PnL | Median Sharpe | Median DD% | Avg Trades | Avg CDR |
|---|---|---|---|---|---|---|
| 0.45 | 262 | -0.68 | -4.21 | 0.20 | 101.44 | 0.46 |
| 0.5 | 315 | -0.67 | -4.07 | 0.24 | 99.22 | 0.48 |
| 0.55 | 305 | -0.68 | -4.24 | 0.21 | 96.99 | 0.50 |
| 0.6 | 363 | 0.09 | 1.64 | 0.15 | 77.44 | 0.45 |

### min_success_probability

| Valor | Jobs | Median PnL | Median Sharpe | Median DD% | Avg Trades | Avg CDR |
|---|---|---|---|---|---|---|
| 0.45 | 330 | -1.16 | -8.56 | 0.18 | 84.82 | 0.43 |
| 0.5 | 329 | -0.22 | -0.61 | 0.21 | 91.42 | 0.49 |
| 0.55 | 283 | 0.09 | 0.61 | 0.21 | 99.90 | 0.50 |
| 0.6 | 303 | -0.23 | -1.34 | 0.17 | 96.31 | 0.47 |

### stop_loss_pct

| Valor | Jobs | Median PnL | Median Sharpe | Median DD% | Avg Trades | Avg CDR |
|---|---|---|---|---|---|---|
| 2.5 | 393 | -0.58 | -3.06 | 0.19 | 93.20 | 0.48 |
| 3.36 | 426 | -0.68 | -4.17 | 0.19 | 90.47 | 0.46 |
| 4.5 | 426 | -0.24 | -1.73 | 0.18 | 94.73 | 0.48 |

### kelly_max_usdt

| Valor | Jobs | Median PnL | Median Sharpe | Median DD% | Avg Trades | Avg CDR |
|---|---|---|---|---|---|---|
| 800.0 | 423 | -0.17 | -2.16 | 0.18 | 94.11 | 0.44 |
| 1000.0 | 418 | -0.24 | -1.60 | 0.17 | 90.95 | 0.48 |
| 1200.0 | 404 | -0.86 | -4.88 | 0.21 | 93.31 | 0.49 |

### kelly_dynamic_base_pct

| Valor | Jobs | Median PnL | Median Sharpe | Median DD% | Avg Trades | Avg CDR |
|---|---|---|---|---|---|---|
| 0.18 | 417 | -0.38 | -1.97 | 0.16 | 91.41 | 0.45 |
| 0.22 | 395 | -0.68 | -4.24 | 0.19 | 92.82 | 0.48 |
| 0.26 | 433 | -0.51 | -2.88 | 0.22 | 94.09 | 0.49 |

### max_positions_spot_bull

| Valor | Jobs | Median PnL | Median Sharpe | Median DD% | Avg Trades | Avg CDR |
|---|---|---|---|---|---|---|
| 5 | 573 | -0.51 | -2.88 | 0.19 | 93.93 | 0.48 |
| 8 | 672 | -0.51 | -2.88 | 0.19 | 91.82 | 0.47 |


## 7. Distribución de PnL (escalado a $5,058)

| Rango USD | Count | % |
|---|---|---|
| <-50 | 26 | 2.09% |
| -50 to -25 | 110 | 8.84% |
| -25 to -10 | 115 | 9.24% |
| -10 to -5 | 118 | 9.48% |
| -5 to 0 | 284 | 22.81% |
| 0 to 5 | 210 | 16.87% |
| 5 to 10 | 91 | 7.31% |
| 10 to 25 | 227 | 18.23% |
| 25 to 50 | 58 | 4.66% |
| >50 | 6 | 0.48% |

## 8. Gap hacia el Target de $120/día

- **Mejor PnL/día observado**: $34.36
- **Gap**: $85.64 por día
- **Multiplicador necesario sobre el mejor resultado**: 3.49x
- **Rentabilidad PnL/$ deployado por ventana**: -0.06% por quincena
- **Rentabilidad PnL/$ deployado por día**: -0.00% por día
- **Capital deployado necesario para $120/día**: $N/A
- **CDR necesario para $120/día**: 0.00%

**Interpretación**: Si la rentabilidad por dólar deployado no cambia, necesitamos N/A CDR para alcanzar el target. Si el CDR no puede subir tanto, la única vía es aumentar la rentabilidad por trade (mejores entradas/salidas).


## 9. Transiciones de Régimen

No hay transiciones registradas en los resultados.


## 10. Hipótesis de Cuellos de Botella

- **Hipótesis 1 — Límite de categoría MIDCAP_ALT**: bloquea ~48% de señales; deja capital ocioso.
- **Hipótesis 2 — Ciclo muy lento**: `cycle_range_scalp` puede ser mayor al momentum del rebote.
- **Hipótesis 3 — Exits tempranos**: SSO-1 stagnant cierra ganancias pequeñas que podrían ser mayores si se deja correr hasta TP.
- **Hipótesis 4 — Universe limitado**: SENTUSDT y otros pumps no están en el universo.
- **Hipótesis 5 — Sizing conservador**: Fear & Greed modifier deprime tamaño en lateral.



## 11. Recomendaciones por Módulo

| Módulo | Problema probable | Acción sugerida |
|---|---|---|
| `run_trades.py` | Límite MIDCAP_ALT 2/2 | Desactivar category limits (`category_max_positions_enabled=false`) y subir max_positions_range_scalp |
| `_kelly_position_size()` | Sizing bajo en lateral | Subir kelly_dynamic_base_pct / kelly_max_usdt para RANGE_SCALP-A |
| `unified_governance_supervisor.py` | Bloquea salidas por pérdida mínima | Revisar umbral `loss_below_min_usdt` |
| `pair_screener.py` | Universe no captura pumps | Evaluar ampliar universe con filtros de liquidez |
| `smart_scale_out.py` | Cierra antes del TP | Ajustar SSO-1/SSO-4 thresholds para RANGE_SCALP |



## 12. ¿Qué haría Super MIMIC aquí?

- **Landscape/attractors**: Super MIMIC detectaría configs robustas para RANGE_SCALP y las separaría de valles inestables.
- **Deploy gate**: Filtraría configs con PnL/día ≤ 0, DD ≥ 15%, trades < 3, attractors < 1. En RANGE_SCALP muchas configs actuales no pasarían gate.
- **Monte Carlo**: Daría intervalos de confianza sobre PnL y Sharpe; mostraría que el mejor resultado puede ser suerte de ventana.
- **Lo que NO haría Super MIMIC**: no detecta automáticamente "peak capture loss", category-limit bottlenecks, ni predictor bootstrap. Necesita integrar métricas de missed-opportunity y capital deployment para eso.
- **Recomendación**: enriquecer artifact de Super MIMIC con métricas de rechazo, CDR y peak-capture ratio por modo.
