# Post-Sweep Analysis: SPOT_BULL (2026-07-13)

## Step 1: Sweep Validation

- **Status**: completed
- **Total jobs**: 15552
- **Completed jobs**: 15552
- **Failed jobs**: 0
- **Jobs with trades > 0**: 2043
- **SPOT_BULL-dominant jobs (≥40 cycles)**: 13179


## Step 2: Aggregate by Profile

| Profile | Runs | Avg PnL | Median PnL | Avg Sharpe | Median Sharpe | Avg DD% | Pos PnL% | Avg Trades |
|---|---|---|---|---|---|---|---|---|
| A | 5184 | -7.03 | -1.83 | -11.02 | -17.81 | 0.51 | 26.31 | 12.51 |
| B | 5184 | -6.94 | -1.83 | -7.07 | -8.50 | 0.57 | 35.61 | 13.07 |
| C | 5184 | -5.98 | -1.83 | -7.36 | -4.99 | 0.53 | 36.75 | 12.05 |

**Attractor profile by total PnL**: B (best single result $71.55)
**Attractor profile by Sharpe**: C (Sharpe 88.47)

## Step 3: SL/TP Exit Analysis

- **Jobs with SL/TP exits > 0**: 0
- **Total SL/TP exits**: 0
- **Total sells**: 71170
- **SL/TP / sells ratio**: 0.00%

| stop_loss_pct | Jobs | Avg SL/TP | Max SL/TP |
|---|---|---|---|
| 2.5% | 5184 | 0.0 | 0 |
| 3.36% | 5184 | 0.0 | 0 |
| 4.5% | 5184 | 0.0 | 0 |

**Goal check**: SL is not strangling — zero SL/TP exits recorded.

## Step 4: Kelly Impact Analysis

| kelly_max_usdt | Jobs | Avg PnL | Median PnL | Avg Sharpe | Avg DD% | Avg CDR |
|---|---|---|---|---|---|---|
| 800.0 | 5184 | -6.80 | -1.83 | -8.58 | 0.54 | 0.21 |
| 1000.0 | 5184 | -6.49 | -1.83 | -8.23 | 0.53 | 0.21 |
| 1200.0 | 5184 | -6.65 | -1.83 | -8.39 | 0.54 | 0.21 |

- **Avg PnL increase per +$100 Kelly**: $0.04
- **Max Kelly tested**: $1200.0
- **Projected PnL at 1.5× ($1800.0)**: $-6.44
- **Projected PnL at 2× ($2400.0)**: $-6.22

## Step 5: Cascading Profile Analysis (A → B → C)

- **A best overall**: PnL=$41.2 trades=206 dd=0.12% window=win_20260415_20260430 params={'min_signal_strength': 0.55, 'min_success_probability': 0.55, 'stop_loss_pct': 4.5, 'kelly_max_usdt': 1000.0, 'kelly_dynamic_base_pct': 0.18, 'max_positions_spot_bull': 5}
- **A worst window**: win_20260515_20260531
- **B rescues A-worst**: PnL=$20.44 trades=74 dd=0.2313% window=win_20260515_20260531 params={'min_signal_strength': 0.5, 'min_success_probability': 0.45, 'stop_loss_pct': 3.36, 'kelly_max_usdt': 800.0, 'kelly_dynamic_base_pct': 0.22, 'max_positions_spot_bull': 5}
- **B worst window**: win_20260515_20260531
- **C rescues B-worst**: PnL=$16.48 trades=75 dd=0.8021% window=win_20260515_20260531 params={'min_signal_strength': 0.45, 'min_success_probability': 0.45, 'stop_loss_pct': 3.36, 'kelly_max_usdt': 1000.0, 'kelly_dynamic_base_pct': 0.18, 'max_positions_spot_bull': 5}

**Cascade verification**: DD floor should improve A→B→C.

## Step 6: Parameter Sensitivity

### min_signal_strength

| Value | Jobs | Median PnL | Median Sharpe | Median DD% | Mean Trades (active) |
|---|---|---|---|---|---|
| 0.45 | 3888 | -1.83 | -15.25 | 0.29 | 104.52 |
| 0.5 | 3888 | -1.83 | -15.25 | 0.40 | 98.41 |
| 0.55 | 3888 | -1.83 | -15.25 | 0.42 | 95.13 |
| 0.6 | 3888 | -1.83 | -4.61 | 0.40 | 84.44 |

### min_success_probability

| Value | Jobs | Median PnL | Median Sharpe | Median DD% | Mean Trades (active) |
|---|---|---|---|---|---|
| 0.45 | 3888 | -1.83 | -15.25 | 0.40 | 94.18 |
| 0.5 | 3888 | -1.83 | -8.86 | 0.40 | 92.11 |
| 0.55 | 3888 | -1.83 | -8.50 | 0.40 | 96.49 |
| 0.6 | 3888 | -1.83 | -9.92 | 0.40 | 99.72 |

### stop_loss_pct

| Value | Jobs | Median PnL | Median Sharpe | Median DD% | Mean Trades (active) |
|---|---|---|---|---|---|
| 2.5 | 5184 | -1.83 | -13.14 | 0.40 | 95.01 |
| 3.36 | 5184 | -1.83 | -9.67 | 0.40 | 97.40 |
| 4.5 | 5184 | -1.83 | -8.50 | 0.40 | 94.17 |

### kelly_max_usdt

| Value | Jobs | Median PnL | Median Sharpe | Median DD% | Mean Trades (active) |
|---|---|---|---|---|---|
| 800.0 | 5184 | -1.83 | -11.45 | 0.40 | 98.68 |
| 1000.0 | 5184 | -1.83 | -8.50 | 0.40 | 93.86 |
| 1200.0 | 5184 | -1.83 | -14.09 | 0.40 | 93.92 |

### kelly_dynamic_base_pct

| Value | Jobs | Median PnL | Median Sharpe | Median DD% | Mean Trades (active) |
|---|---|---|---|---|---|
| 0.18 | 5184 | -1.83 | -9.08 | 0.40 | 97.67 |
| 0.22 | 5184 | -1.83 | -11.06 | 0.40 | 93.32 |
| 0.26 | 5184 | -1.83 | -10.05 | 0.40 | 95.44 |

### max_positions_spot_bull

| Value | Jobs | Median PnL | Median Sharpe | Median DD% | Mean Trades (active) |
|---|---|---|---|---|---|
| 5 | 7776 | -1.83 | -10.55 | 0.40 | 94.45 |
| 8 | 7776 | -1.83 | -9.37 | 0.40 | 96.42 |


## Step 7: Optimal Configuration

### By total PnL

| Profile | Window | PnL total | PnL/día | Trades | DD% | Params |
|---|---|---|---|---|---|---|
| A | win_20260415_20260430 | 41.2 | 19.77 | 206 | 0.12 | `{"min_signal_strength": 0.55, "min_success_probability": 0.55, "stop_loss_pct": 4.5, "kelly_max_usdt": 1000.0, "kelly_dynamic_base_pct": 0.18, "max_positions_spot_bull": 5}` |
| B | win_20260415_20260430 | 71.55 | 34.35 | 89 | 0.0597 | `{"min_signal_strength": 0.5, "min_success_probability": 0.45, "stop_loss_pct": 3.36, "kelly_max_usdt": 1200.0, "kelly_dynamic_base_pct": 0.22, "max_positions_spot_bull": 8}` |
| C | win_20260501_20260515 | 45.41 | 21.8 | 99 | 0.008 | `{"min_signal_strength": 0.45, "min_success_probability": 0.55, "stop_loss_pct": 3.36, "kelly_max_usdt": 1000.0, "kelly_dynamic_base_pct": 0.18, "max_positions_spot_bull": 5}` |

### By Sharpe

| Profile | Window | Sharpe | PnL total | Trades | DD% | Params |
|---|---|---|---|---|---|---|
| A | win_20260415_20260430 | 83.9409 | 8.95 | 85 | 0.0597 | `{"min_signal_strength": 0.45, "min_success_probability": 0.55, "stop_loss_pct": 2.5, "kelly_max_usdt": 800.0, "kelly_dynamic_base_pct": 0.22, "max_positions_spot_bull": 8}` |
| B | win_20260501_20260515 | 87.9515 | 11.19 | 47 | 0.0081 | `{"min_signal_strength": 0.55, "min_success_probability": 0.55, "stop_loss_pct": 2.5, "kelly_max_usdt": 1000.0, "kelly_dynamic_base_pct": 0.18, "max_positions_spot_bull": 5}` |
| C | win_20260401_20260415 | 88.4718 | 2.59 | 93 | 0.0223 | `{"min_signal_strength": 0.6, "min_success_probability": 0.5, "stop_loss_pct": 4.5, "kelly_max_usdt": 800.0, "kelly_dynamic_base_pct": 0.26, "max_positions_spot_bull": 8}` |


## Step 8: Kelly Uniformity Check

| Profile | Kelly | Avg PnL | Avg Sharpe | Avg DD% |
|---|---|---|---|---|
| A | 800.0 | -7.22 | -11.04 | 0.51 |
| A | 1000.0 | -6.82 | -10.86 | 0.50 |
| A | 1200.0 | -7.04 | -11.15 | 0.51 |
| B | 800.0 | -6.97 | -7.10 | 0.57 |
| B | 1000.0 | -6.99 | -7.12 | 0.58 |
| B | 1200.0 | -6.84 | -7.00 | 0.57 |
| C | 800.0 | -6.21 | -7.83 | 0.54 |
| C | 1000.0 | -5.67 | -6.98 | 0.53 |
| C | 1200.0 | -6.07 | -7.28 | 0.53 |

**Decision**: Uniform Kelly is recommended unless a profile shows materially different risk-adjusted returns. Review the table above before finalizing.

## Step 9: Implementation Plan

Proposed configs (by total PnL, SPOT_BULL dominant):

| Profile | Params | Profile Overrides |
|---|---|---|
| A | `{"min_signal_strength": 0.55, "min_success_probability": 0.55, "stop_loss_pct": 4.5, "kelly_max_usdt": 1000.0, "kelly_dynamic_base_pct": 0.18, "max_positions_spot_bull": 5}` | `{"cycle_spot_bull": 8, "cycle_range_scalp": 2, "cycle_preservation": 36, "cycle_futures_short": 3}` |
| B | `{"min_signal_strength": 0.5, "min_success_probability": 0.45, "stop_loss_pct": 3.36, "kelly_max_usdt": 1200.0, "kelly_dynamic_base_pct": 0.22, "max_positions_spot_bull": 8}` | `{"cycle_spot_bull": 10, "cycle_range_scalp": 3, "cycle_preservation": 45, "cycle_futures_short": 4}` |
| C | `{"min_signal_strength": 0.45, "min_success_probability": 0.55, "stop_loss_pct": 3.36, "kelly_max_usdt": 1000.0, "kelly_dynamic_base_pct": 0.18, "max_positions_spot_bull": 5}` | `{"cycle_spot_bull": 12, "cycle_range_scalp": 4, "cycle_preservation": 56, "cycle_futures_short": 5}` |

**Actions required before going live**:
1. Review proposed configs vs current production values.
2. Apply via safe endpoint or maintenance script (dry-run first).
3. Run test suite excluding heavy integration tests.
4. Verify `mode_profile_harmony_snapshot` reflects new values.

## Step 10: Post-Implementation Monitoring (48h)

Monitor in production dashboard:

- **CDR** (Capital Deployed Ratio) — should increase toward target.
- **Sharpe** — should not fall >20% from pre-change baseline.
- **SL exit proportion** — should remain near 0%.
- **Exit composition** — SSO/PRS/PredExit/UG should dominate.

**Rollback trigger**:
- CDR doesn't improve, OR
- Sharpe drops >20%, OR
- Real drawdown exceeds projected × 1.5.
