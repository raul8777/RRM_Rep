---
description: Post-sweep analysis protocol for any regime and its profiles (Quantum Cascade Analysis)
---

# Post-Sweep Analysis Protocol (Quantum Cascade Analysis)

Run this workflow after completing a MIMIC full sweep for any regime mode.
It produces actionable insights on parameter impact, profile cascading behavior, and Kelly sizing optimization.

## Prerequisites

- Completed sweep with results in `Dev/benchmarks/mimic_sweep_results.jsonl`
- Sweep status confirmed in `Dev/benchmarks/mimic_sweep_status.json` (all jobs completed, 0 errors)
- Read `SYSTEM_REFERENCE.md` and `.windsurf/rules.md` before starting

## Step 1: Sweep Validation

1. Confirm sweep completion: read `Dev/benchmarks/mimic_sweep_status.json`
2. Count total jobs, jobs with trades, and regime-dominant jobs (≥40 cycles in target regime)
3. Report: `Total jobs: X | With trades: Y | <REGIME>-dominant: Z`

## Step 2: Aggregate by Profile

For the target regime (≥40 cycles), compute per-profile (A/B/C):
- **n** (number of configs tested)
- **avg_pnl** and **median_pnl** ($USDT)
- **avg_sharpe** and **median_sharpe**
- **avg_drawdown** (%)
- **positive_pnl_ratio** (% of configs with PnL > 0)
- **avg_trades** (activity level)

Identify the **attractor profile** (highest Sharpe + highest PnL).

## Step 3: SL/TP Exit Analysis

Count SL/TP exits across all regime-dominant jobs:
- `sl_tp > 0` jobs count
- Total SL/TP exits vs total sells
- By SL parameter value: avg_sl_tp, max_sl_tp

**Goal**: Confirm SL is acting as safety net (near-zero exits) not strangling positions.

## Step 4: Kelly Impact Analysis

For each `kelly_max_usdt` value in sweep:
- avg_pnl, median_pnl, avg_sharpe, avg_dd
- avg_capital_deployed_ratio (CDR)
- avg_sortino

Compute **Kelly x KD matrix** (avg PnL) for the attractor profile.
Calculate **linear scaling**: PnL increase per +$100 Kelly.
Project to higher Kelly values (1.5×, 2×, 2.5× of max sweep value).

**Key question**: Does Sharpe remain stable as Kelly increases? If yes → sizing is the constraint, not quality.

## Step 5: Cascading Profile Analysis (A → B → C)

This models how the automatic profile selector should behave:

1. **Find Profile A's best config** (highest Sharpe across all windows)
2. **Identify A's worst window** (lowest avg PnL by window)
3. **Apply Profile B to A's worst window** — report B's best config and avg PnL in that window
4. **Identify B's worst window** (may be same or different)
5. **Apply Profile C to B's worst window** — report C's best config and avg PnL

**Report cascade summary**:
```
A best overall:       Sharpe=X PnL=$Y [window_Z]
B rescues A-worst:    Sharpe=X PnL=$Y [window_Z] (A avg was $W)
C rescues B-worst:    Sharpe=X PnL=$Y [window_Z] (B avg was $W)
```

**Verify**: Does worst-case DD decrease as we cascade A→B→C? (C should have the highest floor)

## Step 6: Parameter Sensitivity

For each sweep parameter, compute impact on PnL/Sharpe:
- Group by parameter value, report median metrics
- Identify parameters with **minimal impact** (medians nearly identical → parameter doesn't matter)
- Identify parameters with **strong impact** (clear monotonic trend)

## Step 7: Optimal Configuration

Extract the best config per profile (by Sharpe and by PnL separately).
Compare with current production values.
Flag any parameter where production differs significantly from optimal.

## Step 8: Kelly Uniformity Check

**Key insight**: Profiles differentiate risk via entry gates (min_success_probability, min_signal_strength) and exit timers (SSO, PRS), NOT via Kelly sizing.

If a profile approves a trade, that trade already passed stricter filters — it deserves the same capital.
Reducing Kelly per profile is **double-penalizing**.

**Decision**: Unless sweep data shows a specific profile has fundamentally different risk characteristics, use **uniform Kelly across all profiles**.

## Step 9: Implementation

1. Apply code changes (context multiplier, harmony clamps) only for target regime
2. Update DB profiles via `POST /regime/profiles` or maintenance script
3. Run tests: `docker exec crypto-agent-app python -m pytest /app/tests --ignore=/app/tests/test_integration.py --ignore=/app/tests/test_webhook.py -k "not TestCat7" -q --tb=short`
4. Rebuild container: `docker-compose -f docker-compose.yml up -d --build --force-recreate app`
5. Verify via `mode_profile_harmony_snapshot` that new values are active

## Step 10: Post-Implementation Monitoring (48h)

Monitor in production dashboard:
- **CDR** (Capital Deployed Ratio) — should increase to target
- **Sharpe** — should not fall >20% from pre-change baseline
- **SL exit proportion** — should remain near 0%
- **Exit composition** — SSO/PRS/PredExit/UG should dominate

**Rollback trigger**: CDR doesn't improve OR Sharpe drops >20% OR real drawdown exceeds projected × 1.5

## Output Format

Save analysis as markdown in `Dev/` with naming: `sweep_analysis_<REGIME>_<DATE>.md`
Include all tables, cascade summary, and implementation decisions.
Update `SYSTEM_REFERENCE.md` with changes applied.

## Workflow Metadata

| Field | Value |
|-------|-------|
| Created | 2026-07-12 |
| First used on | RANGE_SCALP full sweep (5,184 jobs, max_cycles=50) |
| Applicable to | Any regime: SPOT_BULL, RANGE_SCALP, PRESERVATION, FUTURES_SHORT |
| Key files | `Dev/benchmarks/mimic_sweep_results.jsonl`, `Dev/benchmarks/mimic_sweep_status.json` |
| Analysis script | `Dev/scripts/_tmp_sweep.py` (create ad-hoc per analysis, delete after) |
