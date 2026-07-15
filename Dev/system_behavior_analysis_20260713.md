# System Behavior, ML & SuperMIMIC Diagnostic — 2026-07-13

## Executive Summary

The bot is **operational and generally stable**, but it is currently in a **high-variance, learning-poor regime**. The last three recorded live trading days show the system can be profitable when entry quality is respected (9 Jul: +16.92 USDT, 70.6% WR), but a single bad day (12 Jul: -24.05 USDT) wiped out 1.4x the prior gain because the predictor was ignored and low-volume/low-conviction entries were allowed. The machine-learning layer is still bootstrap-dominated (only ~12 real trades feeding it), and SuperMIMIC exists but has not yet run a completed experiment. The most recent lifecycle-kernel MIMIC work implemented sweep-derived profiles A/B/C, yet profiles B and C collapsed to identical results, confirming that the current bottleneck is **signal fidelity**, not parameter tuning.

| Area | Status |
|------|--------|
| Infrastructure / parity | Green — only 1 orphan mismatch and residual dust |
| Live PnL (short term) | Yellow — profitable days possible, but fragile |
| Entry-quality gate | Red — predictor negative + vol_spike < 1.0 entries fired |
| Machine learning | Red — bootstrap mode, low accuracy, not yet useful |
| SuperMIMIC | Yellow — architecture ready, no completed experiments |
| Lifecycle kernel MIMIC | Yellow — profiles implemented, signal generator too weak to differentiate them |

---

## 1. Live Trading Behavior

### 1.1. Recent daily snapshots

| Date | Trades closed | Net PnL USDT | Win rate | Régimen | Notes |
|------|--------------:|-------------:|---------:|---------|-------|
| 2026-07-09 | 17 | **+16.92** | 70.6% | RANGE_SCALP-A | PRS/peak capture dominated profitability |
| 2026-07-12 | 29 | **-24.05** | — | RANGE_SCALP-A | 9 stop-losses = -52.77 USDT |
| 2026-06-28 | 33 futures SHORTs | **+33.02** | 66.7% | FUTURES_SHORT | Small position sizing, leverage bug fixed |

### 1.2. 12 July breakdown (worst recent day)

Source: `Dev/benchmarks/live_jul12_closed.json`, `Dev/benchmarks/live_vs_mimic_jul12_analysis.md`

- **29 closed SPOT trades**, total PnL **-24.05 USDT**.
- **Stop losses**: 9 trades, **-52.77 USDT**. Without these, the day would have been **+28.72 USDT**.
- **Winning exits**: `profit_predexit_capture` (+18.97 on PEPE), `sso_target_contribution` (+7.13 on SUI), `profit_realization_supervisor_*` (+5.04 on NEAR/DOGE).
- **Symbol damage**: SOL -14.31, BTC -8.39, SUI -2.93, BNB -2.17, PEPE net -1.83.
- **Symbol success**: NEAR +4.77, TRX +0.88, DOGE +0.58.

### 1.3. Entry-quality problem on 12 July

The root cause is **not** the exit machinery — exits fired correctly. The problem was the **entry filter**.

- **23 of 29 trades** had a negative `last_pred_pnl` at entry/last evaluation.
- **17 of 29 trades** had both negative predicted PnL **and** `vol_spike < 1.0`.
- Examples:
  - BTCUSDT 19:50: `vol_spike=0.18`, `pred_pnl=-2.38`, closed by stop_loss for -4.09 USDT.
  - SUIUSDT 20:07: `vol_spike=0.064`, `pred_pnl=-14.77`, closed by stop_loss for -7.69 USDT.
  - BNBUSDT 20:56: `vol_spike=0.49`, `pred_pnl=-1.62`, closed by stop_loss for -1.94 USDT.

This violates the validated expert rule from June 2026: *do not buy when `vol_spike < 0.5x` and PnL predicted is negative*.

### 1.4. Why did low-quality entries fire?

The `pnl_service` bootstrap fallback relaxes validation when the Learning Engine has too few real trades:

```python
if prediction.get("bootstrap_mode"):
    return True, "bootstrap mode – thresholds relaxed until trade history builds"
```

With only ~12 real trades feeding the model, the predictor is in bootstrap and the system overrides its own negative predictions. Additionally, `entry_quality_max_failures_range_scalp=1` and `entry_quality_vol_spike_min_range_scalp=0.35` allow a trade to pass even if volume is essentially absent.

### 1.5. 9 July contrast (good day)

Source: `daily_analysis_2026-07-09.md`

- 17 trades, **+16.92 USDT**, 70.6% WR.
- Profitability came from **PRS peak protection** and **SSO slow crawl**, not from raw entry edge.
- The predictor was still bootstrap (accuracy 8.3%, `bias_correction=1.5`, 12 real trades), but the entry set happened to align with market microstructure.
- The main operational bottleneck was the `MIDCAP_ALT (2/2)` category limit — 47.6% of rejections.
- `ARBUSDT` (+13.14% / $16.7M vol) was repeatedly rejected by the category cap.

### 1.6. Futures SHORTs (28 June)

Source: `daily_analysis_2026-06-28.md`

- 33 SHORT closes, **+33.02 USDT net**, 66.7% WR.
- Position sizing was too small because leverage was hardcoded to 2x (now fixed to 10x).
- Backtest expectation was ~15.6%/day vs actual 1.11%/day. After the leverage/Kelly fixes, projected daily PnL rose to ~5.5%/day.

---

## 2. State of Exits & Symbol Handling

### 2.1. Exit machinery

The exit pipeline is working:

- Hard SL/TP → BreakEven → Trailing → SSO → PRS → UG → PredExit precedence is respected.
- Most profitable trades exited via `profit_realization_supervisor_*`, `prs_peak_protection`, or `sso_target_contribution`.
- Most losing trades exited via `stop_loss` or `active_replacement`.

### 2.2. Ghost / dust status

Source: `audit_truth_final.json` (2026-07-11)

- Only **1 active mismatch**: `SPOT_ORPHAN_IN_BINANCE` for **BNBUSDT** (~10.39 USDT).
- Many symbols show residual dust below notional thresholds (e.g. BTC 7.69e-06 qty, ~0.50 USDT).
- The previous `ZECUSDT` ghost was resolved; the DB now matches Binance for active positions.

This is a green-light technical state: no phantom positions blocking risk management.

### 2.3. Symbol concentration

Recent losers:
- **SOL/BTC/SUI/BNB** are repeatedly stop-lossed in RANGE_SCALP. They are large-cap / high-liquidity names but the entries lacked volume conviction.
- **PEPE** is high variance: one big PredExit win (+18.97) offset by multiple small SL losses.
- **NEAR/TRX/DOGE/XPLUSDT** have been the reliable winners.

From `daily_analysis_2026-07-09.md`, validated 30-day winners include FIL, ADA, XLM, UNI, BCH, NEAR. Avoid list: FET, JTO, U.

---

## 3. Machine Learning Diagnostic

### 3.1. Current model state

Source: `learning_engine.py`, `daily_analysis_2026-07-09.md`

- The Learning Engine reads closed trades from `trades_closed`, computes per-symbol indicator weights, and persists them to `indicator_weights`.
- It also loads synthetic historical trades via `load_synthetic_trades()` to bootstrap when real data is scarce.
- **Weight range**: [0.02, 0.35]; **bias correction**: [0.5, 2.0]; **weight TTL**: 7 days.
- Real-trade count on 9 Jul: **12**.
- Predictor accuracy on 9 Jul: **8.3%**.
- Bootstrap `bias_correction`: **1.5**.

### 3.2. What this means

The ML layer is **not yet informative**. With ~12 real trades, the model is dominated by synthetic bootstrap data, and `pnl_service` falls back to relaxed thresholds. Consequently:

- Predicted PnL is negative for most entries, yet entries are allowed.
- The system cannot yet distinguish good from bad setups reliably.
- The 7-day weight TTL is appropriate because the market regime is shifting, but it also means the model keeps resetting before enough real samples accumulate.

### 3.3. Learning loop status

- `POST /learning/run` is available but there is no evidence it has been run with enough real trades to exit bootstrap.
- The engine excludes corrupted/manual/rebalance closes, which is correct.
- It merges synthetic + real trades via `run_with_synthetic()`, but if synthetic data does not match current market microstructure, weights can be misleading.

### 3.4. Expert view (statistics / ML)

With N < 30, confidence intervals for win-rate are too wide for actionable inference. The 8.3% accuracy means the predictor is essentially a coin-flip right now. **Do not rely on it for entry gating until ≥50 real trades have accumulated and accuracy improves to at least 40-50%.** Until then, the entry gate must rely on hard technical rules (volume spike, RSI, range position) rather than predicted PnL.

---

## 4. SuperMIMIC Diagnostic

### 4.1. Architecture

Source: `super_mimic/core.py`, `super_mimic/engines.py`, `super_mimic/meta_learner.py`, `super_mimic/deploy_gate.py`

SuperMIMIC is a declarative experiment framework on top of MIMIC:

1. `DesignSpec` defines mode, profile, windows, engine (`mimic_sweep`, `backtest`, `monte_carlo`), strategy, grid.
2. `engines.py` dispatches to the existing `Dev/scripts/mimic_mode_profile_sweep.py`.
3. `meta_learner.py` builds a **regime fingerprint** from BTC trend (1d/4h/1h), BTC RSI 4h, dominance, F&G, avg vol spike, and 7-day win rate; it recommends warm-start configs based on Euclidean similarity to past winning fingerprints.
4. `deploy_gate.py` applies hard rules (PnL/day > 0, drawdown < 15%, ≥3 trades, ≥1 attractor, peak-capture ratio ≥ 0.60, capital deployment ratio ≥ 0.30) plus an ML advisor score before allowing a config to be merged into `kelly_config_override`.
5. `api.py` exposes endpoints to create/run/deploy experiments.

### 4.2. Current state

- A SuperMIMIC experiment was created on 13 Jul 2026 04:07 UTC (`sm_20260713_040749_539906`) with mode `RANGE_SCALP`, profile `A`, window `win_20260628_20260630`, engine `mimic_sweep`, strategy `quick`.
- **Status: `designing`**. It has not run yet.
- Therefore, no recommendations, metrics, fingerprint, or deploy gate result exists yet.

### 4.3. Assessment

SuperMIMIC is **architecturally sound** and aligns with the rules in `.windsurf/rules.md` (deterministic simulator, no duplicated trading logic, DB-merge semantics). However:

- It reuses `mimic_mode_profile_sweep.py`, which currently runs `max_cycles` that may be too short (past sweeps used 5 cycles; 50 were recommended).
- The fingerprint similarity approach is reasonable for warm-start, but with few completed experiments it will default to no historical winners.
- The deploy gate requires `capital_deployment_ratio >= 0.30`, which the lifecycle-kernel validation failed to reach because the synthetic signal generator is too selective.

### 4.4. Connection to live trading

SuperMIMIC is not yet feeding live config. The live regime/profile selector still uses meta-fields stored in `regime_profiles` from older sweeps. On 12 Jul the runtime `kelly_config_override` was out of sync with the DB profile update (desconexión entre perfiles y runtime), meaning even good SuperMIMIC recommendations would not have auto-deployed until `_apply_regime_params` is fixed to detect content-hash changes.

---

## 5. Lifecycle Kernel MIMIC (Latest Implementation)

### 5.1. What was built

Source: `simulation/mimic_lifecycle_adapter.py`, `lifecycle/runner.py`, `tmp_lifecycle_multi_window.py`, `Dev/lifecycle_kernel_validation_20260713.md`

- Implemented dedicated SPOT_BULL profiles A/B/C with sweep-optimal parameters.
- `LifecycleRunner.run_cycle()` now reports `exit_reasons`, realized PnL, win/loss counts, and profit factor.
- `SimulationLifecycleAdapter.execute_exit()` computes realized PnL from simulated fills.
- Multi-window validation script supports `--profile A/B/C/all`.

### 5.2. Validation results (3 windows, 100 cycles each, dry-run)

| Profile | Avg PnL % | Entries | Exits | Realized PnL USDT | Wins | Losses | Avg DD % |
|---------|----------:|--------:|------:|------------------:|-----:|-------:|---------:|
| A | -0.38 | 8 | 37 | -27.59 | 34 | 3 | 0.85 |
| B | -0.37 | 11 | 40 | -66.34 | 34 | 6 | 0.85 |
| C | -0.37 | 11 | 40 | -66.34 | 34 | 6 | 0.85 |

### 5.3. Key finding: profiles B and C are identical

Despite different `max_total_positions`, `max_per_trade_usdt`, and probability thresholds, profiles B and C produced **identical** results on the three validation windows. This indicates the synthetic signal generator is so selective that neither the higher position cap of B nor the stricter success filter of C changed the approved signal set.

**Implication:** the lifecycle kernel’s bottleneck is **signal fidelity**, not parameter tuning. More sweep windows or different SL/TP values will not help until the signal pipeline uses real analysis data.

---

## 6. Expert Synthesis

### 6.1. Crypto / macro lens

- Market regime around 9-13 Jul 2026 was **lateral-to-bearish** (BTC ~flat to down, F&G in fear/capitulation territory).
- `RANGE_SCALP` was the active regime, which is the correct mode for choppy price action.
- Large-cap alts (SOL, BTC, SUI, BNB) struggled because lack of directional conviction meant range-breakdowns were common.
- Meme/high-beta names (PEPE, DOGE) provided the few outsized wins via PredExit capture.

### 6.2. Trading / risk lens

- The system has a positive edge **when entry filters are respected** (9 Jul, 28 Jun futures).
- The 12 Jul loss is a classic **overtrading-in-low-conviction** pattern: too many entries with no volume and negative predicted PnL.
- Stop losses did their job — they prevented worse drawdown. The issue is entry selection, not exit timing.
- Capital deployment is suboptimal: category limits block high-volume winners (ARBUSDT), while low-volume losers still get through.

### 6.3. Statistics / ML lens

- Sample sizes are too small to draw confident conclusions about any single parameter change.
- Win rate swings from 66.7% to 70.6% to negative in three observed days — this is expected variance, not a broken strategy.
- The predictor at 8.3% accuracy is not ready to gate entries. Rely on hard rules until N ≥ 50 and accuracy ≥ 40%.

### 6.4. Operations / engineering lens

- Parity is good: only one minor BNB orphan (~10 USDT) and residual dust.
- The regime/profile selector is structurally sound but the runtime config application has a stale-content bug (`_apply_regime_params` does not detect profile content changes).
- SuperMIMIC is ready to run but has not produced an experiment yet.
- The lifecycle kernel is validated end-to-end but needs a real signal feed.

---

## 7. Red / Yellow / Green Flags

| Flag | Item | Severity |
|------|------|----------|
| Red | Predictor in bootstrap with only ~12 real trades and 8.3% accuracy | High |
| Red | Entries firing with `vol_spike < 1.0` and negative predicted PnL | High |
| Red | Live runtime config out of sync with DB profile updates | High |
| Yellow | SuperMIMIC created but not executed | Medium |
| Yellow | Lifecycle kernel profiles B/C identical — signal generator too weak | Medium |
| Yellow | Category caps block high-conviction winners (ARBUSDT) | Medium |
| Green | Position parity / ghost cleanup | Low |
| Green | Exit pipeline (PRS, SSO, PredExit, UG) functioning | Low |
| Green | Regime detection and profile selector mechanics | Low |

---

## 8. Actionable Recommendations

### Immediate (do before next trading session)

1. **Tighten RANGE_SCALP entry-quality gate**
   - Set `entry_quality_max_failures_range_scalp = 0`.
   - Raise `entry_quality_vol_spike_min_range_scalp` to **≥1.0** (target 1.5 per validated June rules).
   - Do not allow bootstrap mode to bypass a negative predicted PnL; instead reject the entry and log `bootstrap_veto`.

2. **Fix runtime profile application**
   - Update `_apply_regime_params()` in `run_trades.py` to hash the profile JSON and re-apply when content changes, not just when the profile letter changes.

3. **Run `POST /learning/run`**
   - Accumulate every real closed trade possible. The goal is to exit bootstrap (≥50 real trades) so the predictor can actually veto bad entries.

### Short-term (this week)

4. **Complete a SuperMIMIC experiment**
   - Run the existing `sm_20260713_040749_539906` experiment with `max_cycles >= 50` so it produces a deployable recommendation.

5. **Connect real signal feed to lifecycle kernel**
   - Replace the synthetic `_build_analyses()` in `simulation/mimic_lifecycle_adapter.py` with `analysis_cycle` / `pnl_service` so the lifecycle kernel results become comparable to live trading.

6. **Re-evaluate category limits**
   - Consider raising `max_category_positions` for `MIDCAP_ALT` in `RANGE_SCALP` from 2 to 3, or enabling dynamic caps based on available setups, so high-volume winners like ARBUSDT are not systematically blocked.

### Medium-term (next 2-4 weeks)

7. **Run a full 12-window lifecycle sweep**
   - Use `max_cycles=50+` across all SPOT_BULL, RANGE_SCALP, PRESERVATION, and FUTURES_SHORT windows to populate SuperMIMIC fingerprints and regime-profile meta-fields.

8. **Add an ML-readiness dashboard metric**
   - Show real-trade count, bootstrap flag, predictor accuracy, and a "predictor veto rate" so operators can see when the model is actually influencing decisions.

---

## 9. Files Referenced

- `Dev/benchmarks/live_jul12_closed.json`
- `Dev/benchmarks/live_vs_mimic_jul12_analysis.md`
- `daily_analysis_2026-07-09.md`
- `daily_analysis_2026-06-28.md`
- `audit_truth_final.json`
- `learning_engine.py`
- `super_mimic/core.py`, `engines.py`, `meta_learner.py`, `deploy_gate.py`, `api.py`
- `simulation/mimic_lifecycle_adapter.py`
- `lifecycle/runner.py`
- `Dev/lifecycle_kernel_validation_20260713.md`
