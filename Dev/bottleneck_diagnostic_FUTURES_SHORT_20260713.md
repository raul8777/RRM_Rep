# Diagnóstico de Cuello de Botella — FUTURES_SHORT (2026-07-13)

## 1. Resumen Ejecutivo

- **Capital live de referencia**: $5,058.00 USDT
- **Target diario**: $120.00 USDT
- **Jobs analizados**: 265
- **Jobs con trades > 0**: 0
- **Mejor PnL (ventana 15d)**: $32.13 (sweep $2,910) → **$55.85 escalado a $5,058**
- **Mejor PnL/día**: $55.85
- **Gap vs target diario**: $64.15
- **PnL promedio (todos los jobs)**: $10.31 escalado
- **PnL promedio (jobs con trades)**: $N/A escalado
- **Porcentaje de jobs con PnL positivo**: 87.55%


## 2. Mejores Configuraciones

No hay jobs con trades suficientes.


## 3. Aggregate por Perfil

| Perfil | Jobs | Avg PnL | Median PnL | Avg Sharpe | Median Sharpe | Avg DD% | Pos PnL% | Avg Trades | Avg CDR |
|---|---|---|---|---|---|---|---|---|---|
| A | 191 | 6.55 | 0.02 | -1.18 | -44.54 | 0.04 | 84.29 | 0.00 | 0.18 |
| B | 47 | 4.74 | 0.02 | -34.48 | -34.48 | 0.01 | 93.62 | 0.00 | 0.17 |
| C | 27 | 3.66 | 0.02 | -28.23 | -28.23 | 0.00 | 100.00 | 0.00 | 0.17 |

## 4. Capital Deployment Efficiency (CDR)

- **CDR promedio (todos los jobs)**: 17.63%
- **CDR promedio (jobs con trades)**: 0.00%
- **Capital deployado promedio** (a $5,058): $891.95
- **Target CDR 75%**: $3793.50
- **Capital inactivo extra para alcanzar 75% CDR**: $2901.55
- **Observación**: CDR bajo indica que el sistema pasa mucho tiempo en cash. Root causes probables: `category_max_positions_enabled`, `max_positions_*` bajos, predictor conservador, o ausencia de señales en el modo.


## 5. Frecuencia de Trades

No hay jobs con trades.


## 6. Sensibilidad de Parámetros

### min_signal_strength

| Valor | Jobs | Median PnL | Median Sharpe | Median DD% | Avg Trades | Avg CDR |
|---|---|---|---|---|---|---|
| 0.45 | 68 | 0.02 | -44.54 | 0.00 | 0.00 | 0.19 |
| 0.5 | 69 | 0.02 | 84.35 | 0.00 | 0.00 | 0.17 |
| 0.55 | 80 | 0.02 | 16.84 | 0.00 | 0.00 | 0.16 |
| 0.6 | 48 | 0.10 | -55.89 | 0.00 | 0.00 | 0.19 |

### min_success_probability

| Valor | Jobs | Median PnL | Median Sharpe | Median DD% | Avg Trades | Avg CDR |
|---|---|---|---|---|---|---|
| 0.45 | 73 | 0.02 | -1.21 | 0.00 | 0.00 | 0.17 |
| 0.5 | 54 | 0.02 | -54.14 | 0.00 | 0.00 | 0.18 |
| 0.55 | 53 | 0.02 | -54.14 | 0.00 | 0.00 | 0.17 |
| 0.6 | 85 | 0.02 | -44.54 | 0.00 | 0.00 | 0.18 |

### stop_loss_pct

| Valor | Jobs | Median PnL | Median Sharpe | Median DD% | Avg Trades | Avg CDR |
|---|---|---|---|---|---|---|
| 2.5 | 98 | 0.02 | -49.34 | 0.00 | 0.00 | 0.18 |
| 3.36 | 86 | 0.02 | 0.28 | 0.00 | 0.00 | 0.17 |
| 4.5 | 81 | 0.02 | -54.14 | 0.00 | 0.00 | 0.18 |

### kelly_max_usdt

| Valor | Jobs | Median PnL | Median Sharpe | Median DD% | Avg Trades | Avg CDR |
|---|---|---|---|---|---|---|
| 800.0 | 86 | 0.02 | -44.54 | 0.00 | 0.00 | 0.17 |
| 1000.0 | 90 | 0.02 | -54.14 | 0.00 | 0.00 | 0.18 |
| 1200.0 | 89 | 0.02 | -54.14 | 0.00 | 0.00 | 0.18 |

### kelly_dynamic_base_pct

| Valor | Jobs | Median PnL | Median Sharpe | Median DD% | Avg Trades | Avg CDR |
|---|---|---|---|---|---|---|
| 0.18 | 102 | 0.02 | -54.14 | 0.00 | 0.00 | 0.18 |
| 0.22 | 80 | 0.02 | 6.05 | 0.00 | 0.00 | 0.17 |
| 0.26 | 83 | 0.02 | -54.14 | 0.00 | 0.00 | 0.17 |

### max_positions_spot_bull

| Valor | Jobs | Median PnL | Median Sharpe | Median DD% | Avg Trades | Avg CDR |
|---|---|---|---|---|---|---|
| 5 | 131 | 0.02 | -54.14 | 0.00 | 0.00 | 0.18 |
| 8 | 134 | 0.02 | -44.54 | 0.00 | 0.00 | 0.18 |


## 7. Distribución de PnL (escalado a $5,058)

| Rango USD | Count | % |
|---|---|---|
| <-50 | 0 | 0.00% |
| -50 to -25 | 0 | 0.00% |
| -25 to -10 | 7 | 2.64% |
| -10 to -5 | 15 | 5.66% |
| -5 to 0 | 5 | 1.89% |
| 0 to 5 | 155 | 58.49% |
| 5 to 10 | 12 | 4.53% |
| 10 to 25 | 5 | 1.89% |
| 25 to 50 | 47 | 17.74% |
| >50 | 19 | 7.17% |

## 8. Gap hacia el Target de $120/día

- **Mejor PnL/día observado**: $55.85
- **Gap**: $64.15 por día
- **Multiplicador necesario sobre el mejor resultado**: 2.15x
- **Rentabilidad PnL/$ deployado por ventana**: 0.00% por quincena
- **Rentabilidad PnL/$ deployado por día**: 0.00% por día
- **Capital deployado necesario para $120/día**: $N/A
- **CDR necesario para $120/día**: 0.00%

**Interpretación**: Si la rentabilidad por dólar deployado no cambia, necesitamos N/A CDR para alcanzar el target. Si el CDR no puede subir tanto, la única vía es aumentar la rentabilidad por trade (mejores entradas/salidas).


## 9. Transiciones de Régimen

No hay transiciones registradas en los resultados.


## 10. Hipótesis de Cuellos de Botella

- **Hipótesis 1 — Pocos datos en sweep**: FUTURES_SHORT depende de ciclos separados (`cycle_futures_short`); en simulación SPOT_BULL puede no activarse.
- **Hipótesis 2 — Sizing pequeño**: `kelly_max_usdt` para shorts puede estar limitado.
- **Hipótesis 3 — Confirmación bajista lenta**: requiere vela roja 5m confirmada y RSI 4h; puede perder inicio de caídas.



## 11. Recomendaciones por Módulo

| Módulo | Problema probable | Acción sugerida |
|---|---|---|
| `futures_client.py` | Ciclo lento | Ajustar cycle_futures_short si el mercado cae rápido |
| `futures_client.py` | Sizing limitado | Revisar Kelly max para shorts |
| `market_regime.py` | Confirmación bajista lenta | Evaluar trigger anticipado de FUTURES_SHORT |



## 12. ¿Qué haría Super MIMIC aquí?

- **Landscape/attractors**: Super MIMIC detectaría configs robustas para FUTURES_SHORT y las separaría de valles inestables.
- **Deploy gate**: Filtraría configs con PnL/día ≤ 0, DD ≥ 15%, trades < 3, attractors < 1. En FUTURES_SHORT muchas configs actuales no pasarían gate.
- **Monte Carlo**: Daría intervalos de confianza sobre PnL y Sharpe; mostraría que el mejor resultado puede ser suerte de ventana.
- **Lo que NO haría Super MIMIC**: no detecta automáticamente "peak capture loss", category-limit bottlenecks, ni predictor bootstrap. Necesita integrar métricas de missed-opportunity y capital deployment para eso.
- **Recomendación**: enriquecer artifact de Super MIMIC con métricas de rechazo, CDR y peak-capture ratio por modo.
