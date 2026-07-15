# Asset & Signal Selection Deep Analysis — 2026-07-13

## Executive Summary

The system is selecting assets from a **hybrid universe** (static `analysis_symbols` + `pair_screener` ranked by volume × momentum), generating BUY candidates with `AutonomousSignalGenerator.analyze_symbol()`, and then filtering them through a long sequence of regime, liquidity, predictor, and position gates in `_buy_top_symbols()`. 

The data shows a clear pattern: **the entry gate is too permissive when the predictor is in bootstrap mode, and too restrictive by category when the predictor is not.** On 12 Jul 2026 the system traded 29 low-conviction setups (many with `vol_spike < 1.0` and negative predicted PnL) and lost 24.05 USDT. Yet on 13 Jul the missed-opportunity scan found only 3.7% true opportunities among 710 generated BUY signals, meaning the bulk of rejected signals were correctly blocked. The real problem is **selectivity inside the opportunity set**: the system takes trades it should not (bootstrap override), and occasionally misses high-volume setups because of category caps.

| Area | Finding |
|------|---------|
| Universe | Static list + dynamic screener; caps tied to regime/profile; ok in principle. |
| Signal generator | Real 1h indicators; regime-aware ranking; good design. |
| Predictor | Bootstrap-dominated; relaxes thresholds; not yet a reliable veto. |
| Entry gate | Many correct filters, but bootstrap bypass and `entry_quality_max_failures=1` let bad entries through. |
| Missed opportunities | 26 true opportunities / 710 signals (3.7%); category cap blocked high-volume names like ARBUSDT. |
| MIMIC sweep | Best window yielded +0.74% with only 3 trades; sweep results confirm low signal density. |
| Lifecycle kernel | Profiles B/C collapsed to identical results because signal fidelity is the bottleneck. |

---

## 1. How the Universe Is Built

### 1.1. Static lists

`config.py` defines `analysis_symbols` (default 10 large/mid caps), `futures_symbols`, and `narrative_tokens`. These are the fallback anchors.

### 1.2. Dynamic screener

`pair_screener.py` fetches Binance 24h tickers and ranks pairs by:

- `SPOT_BULL`: `vol_24h × positive chg` (long momentum)
- `RANGE_SCALP`: `vol_24h × abs(chg)` (range volatility)
- `PRESERVATION` / `FUTURES_SHORT`: spot score zeroed; futures uses `_futures_short_score` (bear-bounce, RSI 45-65, trend DOWN rewarded)

Liquidity tiers:
- TIER_A: `vol_24h >= $100M` → futures eligible
- TIER_B: `vol_24h >= $10M` → spot eligible
- TIER_C: below → excluded

`universe_selector.py` caps the universe by regime and profile:

```python
_REGIME_CAPS = {
    "SPOT_BULL": 20, "RANGE_SCALP": 16,
    "PRESERVATION": 8, "FUTURES_SHORT": 14,
}
_PROFILE_MULTIPLIERS = {"A": 1.2, "B": 1.0, "C": 0.8}
```

This is a sound architecture. The main risk is that the **static list is small and stale**, and the screener is volume-first without checking whether a symbol has historically been profitable for the bot.

### 1.3. Observation: many top daily movers are absent

The 13 Jul missed-opportunity scan shows top 24h movers like `CREAMUSDT`, `PNTUSDT`, `DODOUSDT`, `XECUSDT` had volumes mostly below $20M. The screener correctly excludes them (TIER_C). For a short-horizon profit system, this is appropriate: low-liquidity pumps are not tradable at scale.

---

## 2. Signal-to-Trade Funnel

```
Universe (static + screener, capped 16-20 symbols)
   ↓
AutonomousSignalGenerator.analyze_symbol() → BUY/HOLD/SELL + strength
   ↓
get_top_buy_signals() filters by min_strength and regime-aware ranking
   ↓
_select_symbols() / _compute_profit_score() / _apply_qubo_selector()
   ↓
_buy_top_symbols() entry gate (many sequential checks)
   ↓
Live trade
```

### 2.1. Signal generation quality

`analyze_symbol()` computes a strong feature set:
- RSI divergence boost/penalty
- 4h trend filter (downgrade BUY when 4h DOWN, except RANGE_SCALP)
- Volume-spike confirmation
- EMA200 trend
- Bollinger Band lower-band touch / near-lower / squeeze breakout
- Hourly momentum breakout

These features align with the expert-validated June 2026 criteria (buy near support, volume >1.5x, RSI 35-55). The code is good; the problem is downstream.

### 2.2. Regime-aware ranking

`get_top_buy_signals()` adapts ranking by regime:

| Regime | Ranking logic |
|--------|---------------|
| `SPOT_BULL` | strength descending |
| `RANGE_SCALP` | `bb_lower_touch` > `bb_near_lower` > low RSI |
| `FUTURES_SHORT` | high RSI + SELL signals get bonus |

This is correct. In RANGE_SCALP the system wants to buy dips; in FUTURES_SHORT it wants to short overbought bounces.

### 2.3. The long entry gate in `_buy_top_symbols()`

For SPOT buys the gate includes:
1. Portfolio exposure cap
2. Per-symbol max positions
3. Prediction-add logic (second entry requires EV ≥1.5x better)
4. Category limits
5. WR screener (5 consecutive SL block)
6. Regime confirmation
7. BTC correlation cap
8. Liquidity / slippage filter
9. Funding-rate filter
10. Regime-specific filters (RSI, BB position, narrative-only in PRESERVATION)
11. BTC dominance filter
12. NARRATIVE cooldown
13. PnL prediction validation / VolSpike override
14. A4 entry quality score gate
15. Kelly sizing
16. Min notional / quantity validation

This is a **very robust gate in design**, but two weaknesses corrupt it:

#### Weakness A: bootstrap bypasses the predictor veto

`pnl_service.validate()`:

```python
if prediction.get("bootstrap_mode"):
    return True, "bootstrap mode – thresholds relaxed until trade history builds"
```

On 9 Jul 2026 only **12 real trades** fed the learner; accuracy was 8.3%. In this state the predictor cannot distinguish good from bad setups, so the system falls back to technical gates only. That is acceptable **only if** the technical gates are strict enough.

#### Weakness B: A4 entry quality gate allows 1 failure

```python
_eq_max_bad = int(getattr(settings, _eq_regime_field, None)
                  or getattr(settings, 'entry_quality_max_failures', 0))
if _eq_bad > _eq_max_bad:  # was >= before fix
    ...block
```

For RANGE_SCALP the relevant settings are:
- `range_scalp_max_rsi` = 60
- `range_scalp_max_bb_position` = 0.40
- `entry_quality_vol_spike_min_range_scalp` = 0.35
- `entry_quality_max_failures_range_scalp` = ? (default falls back to `entry_quality_max_failures`)

Because the vol floor for RANGE_SCALP is only 0.35x and the gate allows up to 1 failing dimension, a symbol can enter with:
- `vol_spike = 0.50` (below the 1.5x expert threshold)
- `RSI = 59` (ok but not oversold)
- `strength = 0.45` (below 0.50)

That is exactly what happened on 12 Jul.

### 2.4. VolSpike override

When the predictor rejects a signal, the system can still enter via `volspike_override_enabled` if:
- symbol is narrative (or `volspike_override_narrative_only=False`)
- 4h volume spike ≥ `volspike_override_min_spike` (default 2.0)
- 4h RSI < `volspike_override_max_rsi` (default 65, hard cap 75)
- 4h trend UP
- 1h RSI < max_rsi
- funding rate ≤ 0.05%
- strength ≥ 0.75
- open override slots < 1
- daily limit not exceeded
- no cooldown

This override is appropriately gated. The issue is that on 12 Jul many entries were **not** overrides; they passed the normal A4 gate because of bootstrap mode + low vol floor + max_failures=1.

---

## 3. Symbol-Level Scorecard (Live 12 Jul)

Source: `Dev/benchmarks/live_jul12_closed.json` (29 closed SPOT trades, PnL -24.05 USDT).

| Symbol | Trades | Net PnL USDT | Avg per trade | Main exit reason | Notes |
|--------|-------:|-------------:|--------------:|------------------|-------|
| NEARUSDT | 4 | +4.77 | +1.19 | PRS/SSO | Best performer, narrative/high-volume |
| XPLUSDT | 1 | +2.93 | +2.93 | sso_target_contribution | Mid-cap winner |
| PEPEUSDT | 3 | +0.38 | +0.13 | profit_predexit_capture + SL | High variance, one big PredExit win |
| DOGEUSDT | 2 | +0.35 | +0.18 | sso_slow_crawl | Meme, low vol spike |
| TRXUSDT | 1 | +0.28 | +0.28 | sso_target_contribution | Stable-ish large cap |
| BTCUSDT | 4 | -8.39 | -2.10 | stop_loss | Entered with vol_spike 0.18-3.60; one bad entry |
| SOLUSDT | 3 | -14.31 | -4.77 | stop_loss | Worst symbol; low volume on entries |
| SUIUSDT | 1 | -2.93 | -2.93 | stop_loss | vol_spike 0.064, pred -14.77 |
| BNBUSDT | 3 | -2.17 | -0.72 | stop_loss / active_replacement | vol_spike 0.49, pred -1.62 |
| Others | 7 | -4.88 | -0.70 | mixed | |

### Key pattern

- **Winners** exited via PRS, SSO target contribution, or slow crawl — the exit machinery captured profits.
- **Losers** exited via `stop_loss` after low-conviction entries.
- SOL and BTC accounted for **$22.70 of the $24.05 loss** (94%). Both are large-cap, liquid, and frequently selected; they became traps when the entry gate failed.

---

## 4. Missed Opportunities (13 Jul Scan)

Source: `Dev/missed_ops_analysis_20260713.txt`

### Signal classification (7 days, 710 BUY signals)

| Class | Count | % | PnL if traded (Kelly $20) |
|-------|------:|---:|--------------------------:|
| OPORTUNIDAD (true opportunity) | 26 | 3.7% | +10.65 USDT |
| FALSO_RECHAZO (false reject) | 79 | 11.1% | +30.35 USDT |
| CORRECTO (correctly blocked, price fell) | 247 | 34.8% | protected |
| EVITADO (correctly avoided, regime mismatch) | 89 | 12.5% | protected |
| NEUTRO | 269 | 37.9% | — |

### Important interpretation

The "FALSO_RECHAZO" +30.35 USDT looks attractive, but the report explicitly notes that in the current `FUTURES_SHORT` regime those 79 signals were **altcoin long rebounds during a BTC -18.5% bear**. Buying them would have violated the regime strategy. In other words, they are false rejects only if evaluated in isolation; from a regime-consistent perspective they were correctly blocked.

The **real opportunity set** is tiny: 26 trades over 7 days (~3-4 per day) with expected +10.65 USDT. The system is currently generating **too many signals** and then filtering poorly at the final gate, rather than generating fewer but higher-conviction signals upstream.

### Blocking reasons

Top block reasons over 7 days:
- `RANGE_SIN_BB_LOWER`: 33 cases
- `RSI_ALTO`: various thresholds, ~150 cases
- `BTC_DOM`: blocked altcoins when BTC dominance high
- `VOL_BAJO`: volume too low
- `STRENGTH_BAJO`: signal strength below threshold

### Per-symbol theoretical performance

| Symbol | Oport | Falso | Corr | PnL teórico | Assessment |
|--------|------:|------:|-----:|------------:|------------|
| PEPEUSDT | 5 | 6 | 10 | +0.95 | Winner but noisy; needs sizing discipline |
| BTCUSDT | 3 | 1 | 11 | +0.96 | Slight net positive in scan; live was negative |
| BNBUSDT | 2 | 2 | 13 | +0.15 | Flat; blocked by BTC.D often |
| ETHUSDT | 2 | 6 | 18 | +0.72 | Net positive; large-cap volatility |
| LINKUSDT | 2 | 5 | 12 | +0.41 | Promising mid-cap |
| INJUSDT | 2 | 8 | 13 | -0.34 | Avoid: low win rate |
| SUIUSDT | 2 | 4 | 15 | +0.09 | Marginal |
| AVAXUSDT | 1 | 6 | 17 | -0.45 | Avoid |
| DOTUSDT | 1 | 4 | 9 | +0.12 | Marginal |
| WLDUSDT | 1 | 14 | 18 | -0.55 | Avoid: many false rejects |
| RENDERUSDT | 1 | 0 | 15 | -1.22 | Avoid |
| FETUSDT | 1 | 2 | 13 | -0.31 | Avoid |

---

## 5. MIMIC Sweep & Lifecycle Validation

### 5.1. Sweep results (SPOT_BULL)

Source: `Dev/benchmarks/lifecycle_sweep/mimic_sweep_report.json`

- 1,296 runs across the parameter grid
- Best PnL: **+0.7438%** (21.64 USDT)
- Average PnL: **-0.4176%**
- Best Sharpe: 3.39
- Best window: `win_20260415_20260430`
- Best parameters: `min_signal_strength=0.5`, `min_success_probability=0.5`, `stop_loss_pct=2.5`, `kelly_max_usdt=1200`, `kelly_dynamic_base_pct=0.18`, `max_positions_spot_bull=5`

The sweep confirms that **signal density is low**. Even the best configuration only produced 3 trades in the best window. The synthetic signal generator in MIMIC is momentum-only and does not reproduce the full `analyze_symbol()` logic, so it underestimates opportunity count, but it correctly shows that the strategy is highly selective.

### 5.2. Lifecycle kernel profiles

Source: `Dev/lifecycle_kernel_validation_20260713.md`

| Profile | Avg PnL % | Entries | Exits | Realized PnL | Wins | Losses |
|---------|----------:|--------:|------:|-------------:|-----:|-------:|
| A | -0.38 | 8 | 37 | -27.59 | 34 | 3 |
| B | -0.37 | 11 | 40 | -66.34 | 34 | 6 |
| C | -0.37 | 11 | 40 | -66.34 | 34 | 6 |

**Critical finding**: profiles B and C produced identical results. The synthetic signal generator is so selective that parameter differences in position cap and success probability did not change the approved signal set. This validates that the current bottleneck is **signal fidelity/quantity**, not parameter tuning.

---

## 6. Predictor Diagnostic

`pnl_predictor.py` uses a weighted sum of:
- `vol_spike` (0.15)
- `trend_4h` (0.15)
- `rsi` (0.12)
- `macd` (0.10)
- `atr_pct` (0.08)
- `bb` (0.08)
- `ema` (0.08)
- `weekly_range` (0.08)
- `volume` (0.04)
- `bias_correction` (1.0)

The weights are learned from closed trades and refreshed every 7 days (`_WEIGHT_TTL_DAYS`).

Current issues:
1. **Bootstrap mode**: with only ~12 real trades, weights stay at defaults and `bootstrap_mode=True`. The predictor cannot veto bad entries.
2. **Synthetic dominance**: `run_with_synthetic()` merges historical simulated trades with real ones. If the synthetic distribution does not match current market microstructure, learned weights may be misleading.
3. **Cycle duration**: correctly set to 240 min (4h), avoiding the old 5-min bug where all predictions were negative due to commissions.
4. **Bias correction cap**: 1.5 when <20 real prediction ratios, 2.0 otherwise. With 12 real trades the cap is active.

---

## 7. Expert Synthesis

### 7.1. Crypto / trading lens

- The 7-13 Jul period was a **BTC-dominated lateral/bearish market** (BTC.D ~56%, BTC 30d ≈ -18.5%).
- In this environment the only consistently profitable SPOT longs are sharp oversold bounces in high-volume mid-caps/memes (PEPE, DOGE, NEAR, LINK).
- Large-cap alts (SOL, BTC, ETH, BNB) are **liquidity traps** when they lack volume conviction; they drift with BTC and stop out easily.

### 7.2. Statistics lens

- 710 signals → 26 true opportunities = **3.7% base rate**. The classifier (entry gate) must be very precise.
- With a 3.7% opportunity rate, lowering thresholds to "trade more" inevitably increases false positives.
- Live 12 Jul WR was effectively ~38% (11 wins / 29 trades), close to the long-term bootstrap 43.1% reported in June. The system is **not broken; it is variance-sensitive**.

### 7.3. Risk lens

- Concentration in SOL/BTC caused 94% of the 12 Jul loss. A per-symbol daily loss cap would have cut the damage.
- Category limits blocked ARBUSDT (high-volume winner) on 9 Jul, showing the cap can be too tight.
- The A4 gate is the right concept but its current parameters are too loose.

### 7.4. ML lens

- The predictor is **not yet usable for entry gating**. It needs ≥50 real trades and accuracy ≥40%.
- Until then, the system should rely on hard technical rules: vol_spike ≥1.0 (preferably 1.5), RSI 30-60, BB position ≤0.40, and positive 4h trend or confirmed RANGE_SCALP dip.

---

## 8. Recommendations

### Immediate (before next trading session)

1. **Tighten RANGE_SCALP entry quality**
   - Set `entry_quality_max_failures_range_scalp = 0` (currently falls back to global default, likely 1).
   - Raise `entry_quality_vol_spike_min_range_scalp` from 0.35 to **1.0 minimum** (target 1.5).
   - This directly blocks the 12 Jul SOL/BTC/SUI bad entries.

2. **Disable predictor bootstrap bypass**
   - In `pnl_service.validate()`, do not allow `bootstrap_mode` to auto-approve entries.
   - Instead, when bootstrap is active, require the A4 gate to pass **all** dimensions (same as recommendation 1).

3. **Per-symbol daily loss circuit breaker**
   - Track realized PnL per symbol per day; if a symbol loses ≥ 2× its average win, block new entries in that symbol for the rest of the day. This would have stopped SOL after the first -$4 loss.

### Short-term (this week)

4. **Make category caps dynamic**
   - Instead of a fixed `max_category_positions`, allocate slots by **realized win rate per category** over the last 30 days. If MIDCAP_ALT has higher win rate, give it more slots.
   - Keep a hard upper bound to avoid over-concentration.

5. **Feed real analysis pipeline into lifecycle kernel MIMIC**
   - Replace the synthetic `_build_analyses()` in `simulation/mimic_lifecycle_adapter.py` with `analysis_cycle.analyze_symbols()` or `AutonomousSignalGenerator.analyze_symbol()`.
   - This is required before profiles A/B/C can be meaningfully differentiated.

6. **Run Learning Engine aggressively**
   - Trigger `POST /learning/run` after every batch of closed trades.
   - The goal is to exit bootstrap (≥50 real trades) so the predictor can start vetoing.

### Medium-term (next 2-4 weeks)

7. **Add a symbol-level "permission list"**
   - Use 30-day realized win rate from `trades_closed` to dynamically promote/demote symbols in the universe.
   - Promote: NEAR, LINK, PEPE, DOGE (when volume ≥1.5x).
   - Demote: SOL, SUI, BNB, WLD, RENDER, FET, INJ, AVAX unless they show sustained positive EV.

8. **Implement a pre-trade expected edge check**
   - Before executing, compute `expected_edge = win_rate_30d × avg_win - loss_rate_30d × avg_loss` for the symbol in the current regime.
   - Only trade if `expected_edge > 0` or if the setup is a clear technical override.

9. **Complete and deploy SuperMIMIC experiments**
   - The existing `sm_20260713_040749_539906` experiment is still in `designing` status.
   - Run it with `max_cycles >= 50` and use the deploy gate to validate any config changes before applying them.

---

## 9. Files Referenced

- `config.py`
- `universe_selector.py`
- `pair_screener.py`
- `autonomous_signals.py`
- `pnl_service.py`
- `pnl_predictor.py`
- `analysis_cycle.py`
- `run_trades.py` (`_buy_top_symbols`, `_get_top_buy_symbols`, A4 gate)
- `portfolio_selector.py`
- `Dev/benchmarks/live_jul12_closed.json`
- `Dev/benchmarks/live_vs_mimic_jul12_analysis.md`
- `Dev/missed_ops_analysis_20260713.txt`
- `Dev/benchmarks/lifecycle_sweep/mimic_sweep_report.json`
- `Dev/lifecycle_kernel_validation_20260713.md`
