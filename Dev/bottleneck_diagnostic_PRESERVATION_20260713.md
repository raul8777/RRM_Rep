# Diagnóstico de Cuello de Botella — PRESERVATION (2026-07-13)

## 1. Resumen Ejecutivo

- **Capital live de referencia**: $5,058.00 USDT
- **Target diario**: $120.00 USDT
- **Jobs analizados**: 0
- **Jobs con trades > 0**: 0
- **Mejor PnL (ventana 15d)**: $N/A (sweep $2,910) → **$N/A escalado a $5,058**
- **Mejor PnL/día**: $N/A
- **Gap vs target diario**: $N/A
- **PnL promedio (todos los jobs)**: $N/A escalado
- **PnL promedio (jobs con trades)**: $N/A escalado
- **Porcentaje de jobs con PnL positivo**: N/A%


## 2. Mejores Configuraciones

No hay jobs con trades suficientes.


## 3. Aggregate por Perfil

| Perfil | Jobs | Avg PnL | Median PnL | Avg Sharpe | Median Sharpe | Avg DD% | Pos PnL% | Avg Trades | Avg CDR |
|---|---|---|---|---|---|---|---|---|---|

## 4. Capital Deployment Efficiency

No hay datos de CDR.


## 5. Frecuencia de Trades

No hay jobs con trades.


## 6. Sensibilidad de Parámetros


## 7. Distribución de PnL

Sin datos.


## 8. Gap hacia $120/día

Sin datos.


## 9. Transiciones de Régimen

No hay transiciones registradas en los resultados.


## 10. Hipótesis de Cuellos de Botella

- **Hipótesis 1 — Modo muy defensivo**: by design reduce trades; difícil generar $120/día desde aquí.
- **Hipótesis 2 — Narrative-only restrictivo**: solo permite tokens narrative en un mercado bajista.
- **Hipótesis 3 — Perfil C por defecto**: si el selector cae a C, casi no opera.



## 11. Recomendaciones por Módulo

| Módulo | Problema probable | Acción sugerida |
|---|---|---|
| Modo en sí | Diseño defensivo | No esperar $120/día en PRESERVATION; objetivo es preservar capital |
| `market_regime.py` | Transición tardía a SPOT_BULL/RANGE | Mejorar detección de fin del modo preservación |



## 12. ¿Qué haría Super MIMIC aquí?

- **Landscape/attractors**: Super MIMIC detectaría configs robustas para PRESERVATION y las separaría de valles inestables.
- **Deploy gate**: Filtraría configs con PnL/día ≤ 0, DD ≥ 15%, trades < 3, attractors < 1. En PRESERVATION muchas configs actuales no pasarían gate.
- **Monte Carlo**: Daría intervalos de confianza sobre PnL y Sharpe; mostraría que el mejor resultado puede ser suerte de ventana.
- **Lo que NO haría Super MIMIC**: no detecta automáticamente "peak capture loss", category-limit bottlenecks, ni predictor bootstrap. Necesita integrar métricas de missed-opportunity y capital deployment para eso.
- **Recomendación**: enriquecer artifact de Super MIMIC con métricas de rechazo, CDR y peak-capture ratio por modo.
