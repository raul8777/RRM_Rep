# Lifecycle Kernel MIMIC Sweep Report — 2026-07-13

## Ejecución

- Comando: `python Dev/scripts/mimic_lifecycle_sweep.py --max-workers 20 --max-cycles 100`
- Ventanas: `win_20260415_20260430`, `win_20260515_20260531`
- Strategy: `lifecycle-quick`
- Grid: 216 combos × 3 perfiles × 2 ventanas = **1,296 jobs**
- Workers: 20
- Duración real: ~10 min 30 s
- Estado: **completado sin errores**

## Resultados agregados

| Métrica | Valor |
|---|---|
| total_runs | 1,296 |
| best_pnl_usdt | **21.64** |
| best_pnl_pct | 0.74% |
| worst_pnl_pct | -1.58% |
| avg_pnl_pct | -0.42% |
| best_sharpe | **3.39** |
| avg_sharpe | -1.91 |

## Mejor configuración por perfil

| Perfil | Ventana | PnL total | PnL/día | Trades | DD% | Sharpe | Params |
|---|---|---|---|---|---|---|---|
| A | win_20260415_20260430 | 21.64 | 5.19 | 3 | 2.32 | 3.39 | min_signal_strength=0.5, min_success_probability=0.5, stop_loss_pct=2.5, kelly_max_usdt=1200, kelly_dynamic_base_pct=0.18, max_positions_spot_bull=5 |
| B | win_20260415_20260430 | 21.64 | 5.19 | 3 | 2.32 | 3.39 | kelly_max_usdt=800 (resto igual) |
| C | win_20260415_20260430 | 21.64 | 5.19 | 3 | 2.32 | 3.39 | kelly_max_usdt=800 (resto igual) |

## Comparación contra sweep baseline heredado

| Métrica | Baseline heredado (full) | Lifecycle kernel (lifecycle-quick) |
|---|---|---|
| best_pnl_usdt | 41.20 (SPOT_BULL-A) | 21.64 |
| best_trades | 206 | 3 |
| best_drawdown_pct | 0.12 | 2.32 |
| best_sharpe | 53.00 | 3.39 |
| jobs evaluados | 15,552 | 1,296 |

## Observaciones

- El lifecycle kernel **sí genera operaciones y PnL positivo** en la mejor ventana, con drawdown controlado (< 15%).
- El número de trades es muy bajo (3 en el mejor caso vs 206 en baseline). Esto indica que el `SimulationLifecycleAdapter` o el análisis sintético están siendo demasiado restrictivos, o que los cierres parciales no se registran como sells.
- La métrica `profit_factor` y `win_rate` quedan `null` porque el conteo actual de sells en modo lifecycle aproxima el PnL por ciclo; sin sells reales no se computa win rate.
- Régimen estático `SPOT_BULL` limita la generalización. En la ventana `win_20260515_20260531` (peor del baseline) los resultados lifecycle fueron consistentemente peores.

## Conclusión

El lifecycle kernel es funcional en MIMIC y no muestra regressiones catastróficas (no hay pérdidas masivas, drawdown aceptable). Sin embargo, antes de reemplazar el pipeline heredado se necesita:

1. **Aumentar la frecuencia de operaciones**: revisar por qué se generan tan pocos trades.
2. **Mejorar el análisis sintético** o conectar el análisis real de `analysis_cycle` en simulación.
3. **Agregar régimen dinámico** para que no se fuerce `SPOT_BULL` en ventanas defensivas.
4. **Corregir el conteo de sells/SL/TP** en las métricas del modo lifecycle.

## Archivos

- Reporte completo: `Dev/benchmarks/lifecycle_sweep/mimic_sweep_report.json`
- Resultados linea por linea: `Dev/benchmarks/lifecycle_sweep/mimic_sweep_results.jsonl`
- Status: `Dev/benchmarks/lifecycle_sweep/mimic_sweep_status.json`
