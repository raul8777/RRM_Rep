# Peak Lifecycle Exit Policy Tuning Report
- Generated: 2026-07-17T01:14:27.759016Z
- Configs evaluated: 151
- Windows: 12

## Oracle vs Lifecycle (top by pnl/day)

| rank | fingerprint | pnl/day | pnl% | max DD% | PF | capture% |
|------|-------------|---------|------|---------|----|----------|
| 1 | {"A_breakeve | 20.18 | 10.08 | 6.32 | 1.33 | 0.46 |
| 2 | {"A_breakeve | 19.99 | 9.75 | 6.49 | 2.00 | 0.44 |
| 3 | {"A_breakeve | 17.27 | 8.67 | 5.89 | 1.72 | 0.39 |
| 4 | {"A_breakeve | 16.02 | 7.96 | 6.36 | 1.30 | 0.36 |
| 5 | {"A_breakeve | 15.36 | 6.91 | 6.06 | 1.28 | 0.31 |

## Top by capture ratio (among configs that pass gates)

| rank | fingerprint | pnl/day | pnl% | max DD% | PF | capture% |
|------|-------------|---------|------|---------|----|----------|
| 1 | {"A_breakeve | 20.18 | 10.08 | 6.32 | 1.33 | 0.46 |
| 2 | {"A_breakeve | 19.99 | 9.75 | 6.49 | 2.00 | 0.44 |
| 3 | {"A_breakeve | 17.27 | 8.67 | 5.89 | 1.72 | 0.39 |
| 4 | {"A_breakeve | 16.02 | 7.96 | 6.36 | 1.30 | 0.36 |
| 5 | {"A_breakeve | 15.36 | 6.91 | 6.06 | 1.28 | 0.31 |

## Best combined (pnl/day * capture%)

Fingerprint `{"A_breakeve` score=9.25, pnl/day=20.18, capture%=0.46, max DD%=6.32


## Best config per-profile exit policy

### Profile A

- `hard_sl_atr_mult`: 3.689675743200163
- `hard_tp_atr_mult`: 6.754763449679763
- `trailing_activation_pct`: 0.018747902108098957
- `trailing_atr_mult`: 2.4084502125248815
- `breakeven_activation_pct`: 0.01336670692715154
- `stagnation_min_hours`: 1
- `stagnation_max_drawdown_from_peak_pct`: 0.05150075574039984
- `momentum_rsi_overbought`: 85
- `momentum_rsi_oversold`: 16
- `momentum_fng_extreme_greed`: 80
- `momentum_fng_extreme_fear`: 21
- `partial_take_mode`: none
- `use_expected_gain_for_tp`: False
- `expected_gain_mult`: 0.5008737710561939
- `time_stop_hours`: 22.972490852124878
### Profile B

- `hard_sl_atr_mult`: 2.3829119083748784
- `hard_tp_atr_mult`: 1.0513141035179159
- `trailing_activation_pct`: 0.003273591105185997
- `trailing_atr_mult`: 2.01100537842917
- `breakeven_activation_pct`: 0.0232074679910783
- `stagnation_min_hours`: 11
- `stagnation_max_drawdown_from_peak_pct`: 0.06938932327175967
- `momentum_rsi_overbought`: 68
- `momentum_rsi_oversold`: 16
- `momentum_fng_extreme_greed`: 87
- `momentum_fng_extreme_fear`: 27
- `partial_take_mode`: tp1_25_at_1.5_tp2_25_at_2.5
- `use_expected_gain_for_tp`: False
- `expected_gain_mult`: 1.4277013338099762
- `time_stop_hours`: 18.05193440334516
### Profile C

- `hard_sl_atr_mult`: 1.0381410202617594
- `hard_tp_atr_mult`: 4.214576646820292
- `trailing_activation_pct`: 0.01658576877336439
- `trailing_atr_mult`: 0.2625626938025958
- `breakeven_activation_pct`: 0.001628042126743231
- `stagnation_min_hours`: 5
- `stagnation_max_drawdown_from_peak_pct`: 0.0881317887444935
- `momentum_rsi_overbought`: 83
- `momentum_rsi_oversold`: 17
- `momentum_fng_extreme_greed`: 78
- `momentum_fng_extreme_fear`: 18
- `partial_take_mode`: tp1_25_at_1.5_tp2_25_at_2.5
- `use_expected_gain_for_tp`: True
- `expected_gain_mult`: 0.7853376248451325
- `time_stop_hours`: 7.935499779112171
