# Short-Term Profit Objective Analysis — 2026-07-13

## 1. What Is the System's Actual Short-Term Profit Target?

The system runs on a **5-minute trade cycle** (`trade_cycle_minutes=5`), but its PnL predictor is calibrated for a **240-minute (4h) horizon** (`trade_horizon_minutes=240`). That is the first mismatch: the cycle is ultra-short, yet the expected-profit model looks four hours ahead.

The real short-term profit engine is in the **exit layer**, not the entry layer:

| Exit Tool | Objective |
|-----------|-----------|
| **PRS** (`profit_realization_supervisor.py`) | Capture profit once a position has moved +0.25% (spot) and held 35+ minutes; defend peak via `_peak_protection_pct`. |
| **SSO** (`smart_scale_out.py`) | 7 triggers including `sso_target_contribution` (≈$40/day target), `sso_stagnant`, `sso_momentum_exhausted`, opportunity collapse. |
| **PeakCapture / PredExit** (`peak_capture_engine.py`) | Decide at each cycle: keep hunting a larger peak or take current profit now. |
| **Hard SL/TP** | Last-resort backstop, not the profit engine. |
| **UG** (`unified_governance_supervisor.py`) | Arbiter that combines PRS/SSO/PeakCapture into one decision. |

**Effective target**: close a position within minutes to a few hours with a positive PnL, using hard SL only when the setup failed.

The numbers confirm this. On 12 Jul 2026 the best trade was:
- **PEPEUSDT**: entry 17:23:42 → exit 18:05:55, **42 minutes**, `profit_predexit_capture`, **+$18.97**.

That is the desired behavior: a quick, high-conviction move, captured at the peak.

---

## 2. Lifecycle Tool Effectiveness (Live 12 Jul Data)

Source: `Dev/benchmarks/live_jul12_closed.json` (29 closed SPOT trades).

### 2.1. Exit reason distribution

| Close reason | N | Total PnL | Avg PnL | Assessment |
|--------------|---|-----------|--------:|------------|
| `stop_loss` | 7 | -22.24 | -3.18 | **Backstop failing**: these positions should not have been entered. |
| `profit_predexit_capture` | 1 | +18.97 | +18.97 | **Ideal**: fast peak capture. |
| `sso_target_contribution` | 2 | +10.49 | +5.24 | Good short-term target hits. |
| `profit_realization_supervisor_partial/full` | 2 | +4.74 | +2.37 | PRS working as intended. |
| `sso_flat_stagnation_timeout` | 1 | +0.88 | +0.88 | Stagnation exit saved capital. |
| `active_replacement` | 1 | -2.37 | -2.37 | Forced replacement, not a lifecycle exit. |
| `ghost_balance_mismatch` / `manual_*` | 6 | ~0.00 | ~0.00 | Operational noise, not strategy exits. |

**Net strategy PnL excluding SL and operational noise**: ~+$34.57 in ~9 lifecycle-managed closes.  
**Net including SL**: **-$24.05**.  

**Conclusion**: the profit-capture tools are working. The problem is that **7 bad entries erased all gains**.

### 2.2. Hold-time analysis

| Trade type | Avg hold time | Result |
|------------|--------------:|--------|
| Lifecycle profit exits (PRS/SSO/PredExit) | ~40–120 min | Positive |
| `stop_loss` exits | ~35 min to 2h | Negative |
| Biggest loser SOL | 25 min | -$14.09 |
| Biggest winner PEPE | 42 min | +$18.97 |

The system is already fast. It does not need longer hold times; it needs **higher-quality entries** that reach profit quickly.

### 2.3. SL is the default failure path

On 12 Jul, **hard SL was the end state for 7 of 29 closes** (24%). More importantly, those 7 losses totaled **-$22.24**, which is **92% of the net loss**. Every other tool (PRS, SSO, PredExit) was profitable in aggregate.

This validates the user's intuition: **SL must remain the last resort**, and the entry process must select assets that the other tools can exit profitably within the short-term window.

---

## 3. Entry Selection vs Short-Term Momentum

### 3.1. The entry gate is too permissive

The A4 entry quality gate allows **up to 1 failing dimension** and uses a `vol_spike_min` of 0.30–0.35 in `RANGE_SCALP`. The expert-validated June 2026 criteria require `vol_spike > 1.5x` to confirm direction. Several 12 Jul losing entries violated the expert rule:

| Symbol | vol_spike_at_entry | last_pred_pnl | close_reason | Hold time |
|--------|-------------------:|--------------:|--------------|-----------|
| BTCUSDT | 0.0038 | -1.66 | `stop_loss` | ~2h |
| SUIUSDT | 0.0643 | -14.77 | `stop_loss` | ~1h |
| PEPEUSDT (loser) | 0.0008 | -0.93 | `stop_loss` | 29 min |
| BNBUSDT | 0.4886 | -1.62 | `stop_loss` | 30 min |
| BTCUSDT (2nd) | 0.1809 | -2.38 | `stop_loss` | ~2h |

All of these had **predicted PnL deeply negative** and **effectively zero volume conviction**. Yet they were allowed to enter. The bootstrap bypass (`prediction bootstrap_mode → allow`) plus `entry_quality_max_failures ≥ 1` let them through.

### 3.2. Winners had volume conviction

| Symbol | vol_spike_at_entry | close_reason | PnL | Time |
|--------|-------------------:|--------------|-----:|------|
| DOGEUSDT | 2.90 / 3.24 | PRS partial, ghost | small positive | ≤28 min |
| PEPEUSDT (winner) | 0.2941 | `profit_predexit_capture` | +$18.97 | 42 min |
| SUIUSDT (winner) | 0.0074 | `sso_target_contribution` | +$7.13 | 57 min |
| NEARUSDT (winners) | 0.2381 | PRS partial/full | +$4.74 | 34–40 min |

Even the winner PEPE had `vol_spike=0.2941`, below the 1.5x expert threshold. This suggests the **system is capturing mean-reversion bounces in low-volume conditions**, not directional volume-confirmed moves. It wins some, but the risk/reward is poor because many low-volume entries immediately drift to SL.

### 3.3. Contrast with 9 Jul success

On 9 Jul 2026 the system had **70.6% win rate and +$16.92** in 17 trades. The big winners were:
- `XPLUSDT` (3 trades, +$14.96, mix of PRS/SSO slow crawl)
- `JTOUSDT` (+$6.34)
- `NEARUSDT`, `WLDUSDT`

These had stronger signals and the exits (PRS peak protection + SSO slow crawl) captured profit quickly. The key difference from 12 Jul was **entry quality**, not exit tooling.

---

## 4. Why SL Is Still Over-Used

### 4.1. The SL/TP parameters are reasonable, but irrelevant if entries are bad

Current category thresholds:
- `LARGE_CAP`: SL 3.5%, TP 3.5%, trail activation 2.5%
- `MIDCAP_ALT`: SL 3.5%, TP 3.5%, trail activation 2.5%
- `NARRATIVE`: SL 3.5%, TP 7.0%, trail activation 5.0%
- `MEME`: SL 2.5%, TP 4.5%, trail activation 2.0%

For `RANGE_SCALP`: SL 0.8%, TP 1.8%.

These are not "too tight." The issue is that a BTC position entered with `vol_spike=0.0038` and negative EV has almost no probability of reaching +1.8% before hitting -0.8%. The SL is doing exactly what it should: cutting a bad trade. But the trade should never have been taken.

### 4.2. The predictor horizon does not match the short-term objective

`trade_horizon_minutes=240` (4h). The exit tools operate on 20–120 minute timescales. A predictor trained on 4h expected PnL cannot effectively veto or validate 30-minute trades. This is why:
- Many 12 Jul entries had `last_pred_pnl` negative yet still won sometimes (PEPE +$18.97).
- Many winners had negative predicted PnL at entry.
- The `min_hold_for_prediction_exit_range_scalp=20` setting allows a re-check after 20 minutes, but the original prediction was already noisy.

### 4.3. UG loss branch gives too much patience to hopeless positions

`UnifiedGovernance` loss branch uses:
- `min_age` = 30–84 min depending on regime/profile
- `escalation_age` = 60–162 min

In a `RANGE_SCALP` regime, if a position has not reached profit within **30–40 minutes**, the odds of it doing so later are low. The current `min_age` is defensible, but it is paired with entries that have **no short-term edge**, so every minute of patience is just delayed SL.

---

## 5. MIMIC / Lifecycle Validation Perspective

Source: `Dev/lifecycle_kernel_validation_20260713.md` and `Dev/benchmarks/lifecycle_sweep/mimic_sweep_report.json`.

### 5.1. Lifecycle exit distribution

Across multi-window runs with the synthetic signal generator:
- Most profitable exits: `partial tp1`, `hard TP`
- Losing exits: `hard SL`
- Profiles B and C produced **identical results** because the signal generator is so selective that parameter differences did not matter.

This confirms: **once you have a decent entry, the exit toolkit captures profit. The bottleneck is entry generation/selection**.

### 5.2. Sweep results show low opportunity density

- Best window: `win_20260415_20260430`, +0.74% PnL, **only 3 trades**.
- Average PnL across 1,296 runs: -0.42%.

Even with a simplified synthetic generator, the message is clear: **there are very few high-quality short-term opportunities**. The system must be extremely selective, not try to trade every cycle.

---

## 6. Expert Synthesis

### 6.1. Trading / crypto lens

In a sideways-to-bearish market, the only reliable short-term edge is:
1. High-volume spike (`vol_spike ≥ 1.5x`) confirming a directional move.
2. Entry near support (BB lower / 20-40% weekly range).
3. RSI not overbought.
4. Tight timeframe: aim for the move to resolve within 30–90 minutes.

The June 2026 expert criteria are exactly this. The current system relaxes them (`vol_spike_min=0.30-0.35`, `entry_quality_max_failures ≥ 1`, bootstrap bypass), producing entries with no directional edge.

### 6.2. Statistics lens

- 12 Jul live data: **winners avg +0.46%** vs **losers avg -3.18%**.
- Risk/reward is inverted because losers are held too long and exit via SL, while winners are captured quickly.
- The system needs a **positive expected value per cycle**, not per 30-day holding period.

### 6.3. ML lens

- The predictor is in bootstrap with only ~12 real trades and 8.3% accuracy.
- Until 50+ real trades exist, the predictor should **not veto** entries; instead, hard technical rules should gate entries.
- After 50+ trades, the predictor should be retrained with a **shorter horizon** (e.g., 30–60 min) to match the actual exit timeframe.

### 6.4. Risk lens

- SL is not the problem; bad entries are.
- A per-symbol daily loss cap would have prevented the repeated BTC/SOL/PEPE SL sequences.
- The UG loss branch should also consider **opportunity cost**: if a better signal exists and the current position is underwater, close faster.

---

## 7. Recommendations to Align with Short-Term Profit Objective

### Immediate (before next cycle)

1. **Raise entry quality floor to match the expert short-term criteria**
   - `entry_quality_vol_spike_min_range_scalp` → **1.0 minimum** (target 1.5).
   - `entry_quality_min_strength` → **0.50** (currently 0.40).
   - `entry_quality_max_failures_range_scalp` → **0** (currently falls back to global default).
   - This blocks BTC/SUI/PEPE/SL entries from 12 Jul.

2. **Disable bootstrap predictor bypass for entry approval**
   - In `pnl_service.validate()`, when `bootstrap_mode=True`, do not return `True` automatically.
   - Instead, if bootstrap, require A4 gate to pass **all** dimensions (no failures allowed).

3. **Add a short-term stagnation rule inside UG / SSO**
   - If a `RANGE_SCALP` position has `pnl_pct_net < 0` and `held_min ≥ 30` and `pnl_velocity_1h ≤ 0`, treat as deteriorated and allow UG loss branch to close, regardless of distance to SL.
   - This prevents holding bad entries until hard SL.

4. **Per-symbol daily loss circuit breaker**
   - Track realized PnL per symbol per day; if cumulative loss > 2× the symbol's average win, block new entries in that symbol for 24h.
   - This would have stopped repeated BTC/SOL/PEPE entries on 12 Jul.

### Short-term (this week)

5. **Recalibrate predictor horizon to match actual exit timeframe**
   - Add a 30-minute and 60-minute prediction target alongside the 4h target.
   - Train the short-horizon predictor on real trades closed by PRS/SSO/PredExit.
   - Use the short-horizon prediction for entry veto and PredExit decisions.

6. **Tie SSO/PRS targets to cycle count, not just minutes**
   - Add `sso_target_min_cycles` and `prs_profit_min_cycles` (e.g., 6–12 cycles = 30–60 min).
   - If target contribution is not reached within target cycles, accelerate stagnation exit.

7. **Dynamic category caps by recent short-term win rate**
   - Replace fixed `max_category_positions` with a 24h rolling win-rate score.
   - Symbols/categories with positive short-term EV get more slots; negative EV get fewer.

### Medium-term (next 2–4 weeks)

8. **Implement a pre-trade expected cycles-to-profit check**
   - Before entry, estimate `E[cycles_to_profit]` from recent regime behavior and symbol volatility.
   - Reject if `E[cycles_to_profit] > 12` in `RANGE_SCALP` or `> 6` in `FUTURES_SHORT`.

9. **Complete SuperMIMIC live-paper loop**
   - Use the 13 Jul `sm_20260713_040749_539906` experiment with `max_cycles ≥ 50` to validate these rule changes before deploying to live config.

---

## 8. Files Referenced

- `config.py` — `trade_cycle_minutes`, `trade_horizon_minutes`, SL/TP defaults, category thresholds, `range_scalp_*`.
- `run_trades.py` — `_buy_top_symbols` (entry gate, A4 quality gate), `_check_sl_tp_positions` (exit orchestration, SL/TP/breakeven/trailing/PRS/SSO/UG/PeakCapture).
- `profit_realization_supervisor.py` — PRS profit-capture logic.
- `smart_scale_out.py` — SSO 7-trigger scale-out.
- `peak_capture_engine.py` — PredExit / Wolf Hunter peak capture.
- `unified_governance_supervisor.py` — UG arbitration between PRS/SSO/PeakCapture and loss branch.
- `pnl_service.py` / `pnl_predictor.py` — predictor validation and bootstrap behavior.
- `autonomous_signals.py` — signal generation and regime-aware ranking.
- `Dev/benchmarks/live_jul12_closed.json` — live closed-trade data.
- `Dev/daily_analysis_2026-07-09.md` — 24h performance benchmark with exit breakdown.
- `Dev/lifecycle_kernel_validation_20260713.md` — lifecycle kernel multi-window validation.
- `Dev/benchmarks/lifecycle_sweep/mimic_sweep_report.json` — sweep summary and best parameters.
