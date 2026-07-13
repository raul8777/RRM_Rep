# Lifecycle Kernel MIMIC Baseline Evaluation — 2026-07-13

## Objetivo
Evaluar el nuevo `lifecycle` kernel sobre una ventana sintética MIMIC, usando la
configuración ganadora del sweep SPOT_BULL-A como punto de partida. Esto sirve
como baseline rápido antes de correr el sweep real contra datos históricos.

## Configuración mapeada

Fuente: `Dev/sweep_analysis_SPOT_BULL_20260713.md` Step 7 — mejor SPOT_BULL-A.

| Parámetro sweep | Valor sweep | Parámetro LifecycleConfig |
|---|---|---|
| `min_signal_strength` | 0.55 | `signal_policy.min_signal_strength = 0.55` |
| `min_success_probability` | 0.55 | `signal_policy.min_success_probability = 0.55` |
| `stop_loss_pct` | 4.5% | `exit_policy.hard_sl_atr_mult = 2.25` (≈4.5% SL para ATR 2%) |
| `kelly_max_usdt` | 1000 | `sizing_policy.max_per_trade_usdt = 1000` |
| `kelly_dynamic_base_pct` | 0.18 | `sizing_policy.kelly_base_pct = 0.18` |
| `max_positions_spot_bull` | 5 | `entry_policy.max_total_positions = 5` |

## Ventana sintética

- 6 pasos horarios.
- 3 activos: BTCUSDT, SOLUSDT, FETUSDT.
- Serie de precios: rally inicial + leve retroceso final.
- ATR fijo: BTC 900, SOL 1.8, FET 0.04.

## Pipeline ejecutado

1. `RegimeKernel` fuerza `SPOT_BULL`.
2. `SignalPipeline` filtra y diversifica por categoría.
3. `EntryGate` aplica umbrales y límites de posición.
4. `PositionSizer` asigna capital Kelly con F&G neutro.
5. `LifecycleRunner` ejecuta entradas al inicio y evalúa salidas en cada paso.
6. `ExitEngine` dispara partial takes por TP1/TP2.

## Resultados

```json
{
  "trades": 7,
  "peak_capture_ratio": 0.78,
  "final_positions": 0
}
```

- **Entradas**: BTCUSDT (LARGE_CAP) y SOLUSDT (MIDCAP_ALT). FETUSDT fue
  rechazada por el gate de diversificación y umbral de liquidez en la ventana
  sintética.
- **Salidas**: 7 partial closes (TP1/TP2) en BTC y SOL. Ningún SL fue tocado.
- **peak_capture_ratio**: 0.78 — aceptable baseline; la métrica real requiere
  equity curve completa.

## Métricas placeholder

`pnl_per_day_usdt`, `drawdown_pct`, `win_rate` y `capital_deployment_ratio`
están en 0.0 en esta evaluación sintética porque `compute_metrics()` aún no
agrega curva de equity. El siguiente paso es:

1. Integrar `LifecycleRunner` en `simulation/mimic_runner.py`.
2. Correr sweep real SPOT_BULL-A/B/C sobre ventanas históricas.
3. Calcular métricas reales: PnL total/día, drawdown, win rate, CDR.

## Conclusión

El lifecycle kernel toma decisiones coherentes con la configuración ganadora del
sweep: entra en LARGE_CAP + MIDCAP_ALT, escala parciales en TP, no fuerza
operaciones débiles. Listo para evaluación contra datos históricos.

## Archivos

- `lifecycle/mimic_baseline_eval.py`: herramienta de evaluación.
- `lifecycle/mimic_baseline_report.py`: generador del JSON.
- `Dev/benchmarks/lifecycle_baseline_report.json`: artefacto de esta corrida.
