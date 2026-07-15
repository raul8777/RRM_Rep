# Diagnóstico de Cuello de Botella — SPOT_BULL (2026-07-13)

## 1. Resumen Ejecutivo

- **Capital live de referencia**: $5,058.00 USDT
- **Target diario**: $120.00 USDT
- **Jobs analizados**: 13445
- **Jobs con trades > 0**: 1055
- **Mejor PnL (ventana 15d)**: $71.55 (sweep $2,910) → **$124.36 escalado a $5,058**
- **Mejor PnL/día**: $59.71
- **Gap vs target diario**: $60.29
- **PnL promedio (todos los jobs)**: $-13.41 escalado
- **PnL promedio (jobs con trades)**: $-9.04 escalado
- **Porcentaje de jobs con PnL positivo**: 31.92%


## 2. Mejores Configuraciones por PnL Total

| Rank | Perfil | Window | PnL | PnL/día | Trades | DD% | CDR | Params |
|---|---|---|---|---|---|---|---|---|
| 1 | B | win_20260415_20260430 | 71.55 | 34.35 | 89 | 0.0597 | 0.6671 | `{"min_signal_strength": 0.5, "min_success_probability": 0.45, "stop_loss_pct": 3.36, "kelly_max_usdt": 1200.0, "kelly_dynamic_base_pct": 0.22, "max_positions_spot_bull": 8}` |
| 2 | B | win_20260415_20260430 | 56.38 | 27.06 | 43 | 0.2479 | 0.2548 | `{"min_signal_strength": 0.45, "min_success_probability": 0.5, "stop_loss_pct": 4.5, "kelly_max_usdt": 1200.0, "kelly_dynamic_base_pct": 0.26, "max_positions_spot_bull": 8}` |
| 3 | C | win_20260501_20260515 | 45.41 | 21.8 | 99 | 0.008 | 0.9551 | `{"min_signal_strength": 0.45, "min_success_probability": 0.55, "stop_loss_pct": 3.36, "kelly_max_usdt": 1000.0, "kelly_dynamic_base_pct": 0.18, "max_positions_spot_bull": 5}` |
| 4 | C | win_20260501_20260515 | 45.02 | 21.61 | 103 | 0.0082 | 0.9909 | `{"min_signal_strength": 0.55, "min_success_probability": 0.6, "stop_loss_pct": 2.5, "kelly_max_usdt": 1200.0, "kelly_dynamic_base_pct": 0.22, "max_positions_spot_bull": 8}` |
| 5 | B | win_20260401_20260415 | 41.46 | 19.9 | 96 | 0.0204 | 0.7957 | `{"min_signal_strength": 0.55, "min_success_probability": 0.55, "stop_loss_pct": 2.5, "kelly_max_usdt": 1000.0, "kelly_dynamic_base_pct": 0.22, "max_positions_spot_bull": 8}` |

## 3. Aggregate por Perfil

| Perfil | Jobs | Avg PnL | Median PnL | Avg Sharpe | Median Sharpe | Avg DD% | Pos PnL% | Avg Trades | Avg CDR |
|---|---|---|---|---|---|---|---|---|---|
| A | 4220 | -8.70 | -5.40 | -11.06 | -17.81 | 0.60 | 26.59 | 7.08 | 0.19 |
| B | 4603 | -7.76 | -1.83 | -7.83 | -8.50 | 0.62 | 33.98 | 5.96 | 0.18 |
| C | 4622 | -6.77 | -1.83 | -8.50 | -8.50 | 0.57 | 34.73 | 4.81 | 0.19 |

## 4. Capital Deployment Efficiency (CDR)

- **CDR promedio (todos los jobs)**: 18.52%
- **CDR promedio (jobs con trades)**: 49.31%
- **Capital deployado promedio** (a $5,058): $936.91
- **Target CDR 75%**: $3793.50
- **Capital inactivo extra para alcanzar 75% CDR**: $2856.59
- **Observación**: CDR bajo indica que el sistema pasa mucho tiempo en cash. Root causes probables: `category_max_positions_enabled`, `max_positions_*` bajos, predictor conservador, o ausencia de señales en el modo.


## 5. Frecuencia de Trades

- **Jobs con trades**: 1055
- **Trades promedio por job**: 75.4
- **Median trades**: 94.00
- **PnL promedio por trade**: $-0.046
- **Trades necesarios para $120/día** (si PnL/trade promedio se mantiene): -2612 trades/día


## 6. Sensibilidad de Parámetros

### min_signal_strength

| Valor | Jobs | Median PnL | Median Sharpe | Median DD% | Avg Trades | Avg CDR |
|---|---|---|---|---|---|---|
| 0.45 | 3316 | -5.39 | -17.81 | 0.42 | 7.62 | 0.19 |
| 0.5 | 3336 | -5.39 | -17.81 | 0.42 | 5.76 | 0.19 |
| 0.55 | 3386 | -5.05 | -16.11 | 0.42 | 5.54 | 0.18 |
| 0.6 | 3407 | -1.83 | -4.61 | 0.42 | 4.79 | 0.18 |

### min_success_probability

| Valor | Jobs | Median PnL | Median Sharpe | Median DD% | Avg Trades | Avg CDR |
|---|---|---|---|---|---|---|
| 0.45 | 3325 | -5.39 | -17.81 | 0.42 | 6.28 | 0.19 |
| 0.5 | 3332 | -3.20 | -15.25 | 0.42 | 6.12 | 0.19 |
| 0.55 | 3401 | -2.12 | -10.03 | 0.42 | 5.87 | 0.18 |
| 0.6 | 3387 | -3.03 | -15.25 | 0.42 | 5.40 | 0.18 |

### stop_loss_pct

| Valor | Jobs | Median PnL | Median Sharpe | Median DD% | Avg Trades | Avg CDR |
|---|---|---|---|---|---|---|
| 2.5 | 4497 | -2.85 | -15.25 | 0.42 | 5.97 | 0.19 |
| 3.36 | 4472 | -2.13 | -15.25 | 0.42 | 5.69 | 0.18 |
| 4.5 | 4476 | -3.17 | -15.25 | 0.42 | 6.09 | 0.19 |

### kelly_max_usdt

| Valor | Jobs | Median PnL | Median Sharpe | Median DD% | Avg Trades | Avg CDR |
|---|---|---|---|---|---|---|
| 800.0 | 4469 | -3.05 | -15.25 | 0.42 | 6.06 | 0.19 |
| 1000.0 | 4477 | -2.12 | -15.25 | 0.42 | 5.93 | 0.19 |
| 1200.0 | 4499 | -3.04 | -15.25 | 0.42 | 5.75 | 0.18 |

### kelly_dynamic_base_pct

| Valor | Jobs | Median PnL | Median Sharpe | Median DD% | Avg Trades | Avg CDR |
|---|---|---|---|---|---|---|
| 0.18 | 4485 | -2.90 | -15.25 | 0.42 | 6.57 | 0.18 |
| 0.22 | 4503 | -2.85 | -15.25 | 0.42 | 6.12 | 0.19 |
| 0.26 | 4457 | -2.60 | -15.25 | 0.42 | 5.06 | 0.18 |

### max_positions_spot_bull

| Valor | Jobs | Median PnL | Median Sharpe | Median DD% | Avg Trades | Avg CDR |
|---|---|---|---|---|---|---|
| 5 | 6780 | -2.79 | -15.25 | 0.42 | 5.63 | 0.19 |
| 8 | 6665 | -2.85 | -15.25 | 0.42 | 6.21 | 0.18 |


## 7. Distribución de PnL (escalado a $5,058)

| Rango USD | Count | % |
|---|---|---|
| <-50 | 163 | 1.21% |
| -50 to -25 | 5777 | 42.97% |
| -25 to -10 | 133 | 0.99% |
| -10 to -5 | 645 | 4.80% |
| -5 to 0 | 2436 | 18.12% |
| 0 to 5 | 1520 | 11.31% |
| 5 to 10 | 204 | 1.52% |
| 10 to 25 | 1991 | 14.81% |
| 25 to 50 | 552 | 4.11% |
| >50 | 24 | 0.18% |

## 8. Gap hacia el Target de $120/día

- **Mejor PnL/día observado**: $59.71
- **Gap**: $60.29 por día
- **Multiplicador necesario sobre el mejor resultado**: 2.01x
- **Rentabilidad PnL/$ deployado por ventana**: -0.42% por quincena
- **Rentabilidad PnL/$ deployado por día**: -0.03% por día
- **Capital deployado necesario para $120/día**: $N/A
- **CDR necesario para $120/día**: 0.00%

**Interpretación**: Si la rentabilidad por dólar deployado no cambia, necesitamos N/A CDR para alcanzar el target. Si el CDR no puede subir tanto, la única vía es aumentar la rentabilidad por trade (mejores entradas/salidas).


## 9. Transiciones de Régimen

No hay transiciones registradas en los resultados.


## 10. Hipótesis de Cuellos de Botella

- **Hipótesis 1 — Llegada tarde al régimen**: `market_regime.py` requiere 2 ciclos consecutivos para confirmar SPOT_BULL; eso puede perder el inicio del impulso alcista.
- **Hipótesis 2 — Sizing pequeño**: Kelly base $100 / dinámico 0.18 en un modo que debería "dejar correr runners" resulta en posiciones pequeñas vs capital.
- **Hipótesis 3 — Exits demasiado tempranos**: SSO/PRS/Peak capturan ganancias rápidas pero sacrifican grandes runners en SPOT_BULL.
- **Hipótesis 4 — Predictor conservador**: en bootstrap, expected_pnl negativo bloquea entradas incluso cuando el mercado es alcista.
- **Hipótesis 5 — Límites de categoría**: category limits impiden llenar slots en SPOT_BULL.



## 11. Recomendaciones por Módulo

| Módulo | Problema probable | Acción sugerida |
|---|---|---|
| `market_regime.py` | Confirmación lenta de SPOT_BULL | Evaluar reducir ciclos de confirmación o adelantar señal parcial |
| `_kelly_position_size()` | Sizing pequeño para runners | Subir Kelly base / max en SPOT_BULL-A |
| `smart_scale_out.py` | Cierra ganadores muy pronto | Relajar SSO-1 stagnant en SPOT_BULL, ampliar trailing |
| `learning_engine.py` | Predictor bootstrap bloquea entradas alcistas | Forzar recalibración con trades reales |
| `portfolio_selector.py` | Slots vacíos por límites de categoría | Relajar category limits en SPOT_BULL |



## 12. ¿Qué haría Super MIMIC aquí?

- **Landscape/attractors**: Super MIMIC detectaría configs robustas para SPOT_BULL y las separaría de valles inestables.
- **Deploy gate**: Filtraría configs con PnL/día ≤ 0, DD ≥ 15%, trades < 3, attractors < 1. En SPOT_BULL muchas configs actuales no pasarían gate.
- **Monte Carlo**: Daría intervalos de confianza sobre PnL y Sharpe; mostraría que el mejor resultado puede ser suerte de ventana.
- **Lo que NO haría Super MIMIC**: no detecta automáticamente "peak capture loss", category-limit bottlenecks, ni predictor bootstrap. Necesita integrar métricas de missed-opportunity y capital deployment para eso.
- **Recomendación**: enriquecer artifact de Super MIMIC con métricas de rechazo, CDR y peak-capture ratio por modo.
