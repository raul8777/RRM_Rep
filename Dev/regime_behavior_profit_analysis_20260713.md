# Regime-Behavior Profit Objective Analysis — 2026-07-13

## 1. Core Principle: Behavior First, Symbol Second

The objective is the same in every regime and profile: **open positions on assets whose current price behavior is likely to produce a profitable exit within a small number of cycles**. The asset's name is irrelevant; only its current regime-appropriate behavior matters.

| Regime | What "profitable short-term behavior" means |
|--------|---------------------------------------------|
| `SPOT_BULL` | Price is rising with volume confirmation, not overbought, in a bullish structure. |
| `RANGE_SCALP` | Price is at or near range support, showing mean-reversion pressure, with volume spike. |
| `PRESERVATION` | Only narrative/trending assets with a clear short-term catalyst and strong volume. |
| `FUTURES_SHORT` | Price is bouncing or stalling in a bearish structure; *declining* price is the opportunity. |

Profiles A/B/C only change **how aggressively** we pursue that behavior, not what behavior we pursue:
- **A**: larger sizes, slightly looser quality floor, more patience.
- **B**: balanced baseline.
- **C**: smaller sizes, tighter quality floor, faster exits.

A per-symbol daily loss circuit breaker is the wrong tool because it confuses "this symbol fell" with "this trade failed." In `FUTURES_SHORT` we want symbols that fall; in `SPOT_BULL` we want symbols that rise. The filter must be regime-aware and behavior-aware, not symbol-punitive.

---

## 2. Current Behavior Selection by Regime

### 2.1. Universe selection (`pair_screener.py`)

The screener already has the right idea:

| Regime | Spot ranking | Futures ranking |
|--------|--------------|-----------------|
| `SPOT_BULL` | `vol × positive chg` (long momentum) | `vol × abs(chg)` (momentum) |
| `RANGE_SCALP` | `vol × abs(chg)` (range volatility) | `vol × abs(chg)` or short-bounce score |
| `PRESERVATION` / `FUTURES_SHORT` | narrative-only or zeroed | `_futures_short_score` (bear-bounce, RSI 45-65, trend DOWN) |

**Strength**: it correctly zeros spot longs in bearish regimes and tries to short bounces in `FUTURES_SHORT`.

**Weakness**: it uses 24h ticker metrics only. A symbol can have high 24h volume but dead current action, or vice versa. It also ignores whether the current 1h/4h indicators confirm the regime-appropriate behavior.

### 2.2. Signal generation (`autonomous_signals.py`)

`analyze_symbol()` computes a rich set of 1h features:
- RSI divergence
- 4h trend filter
- Volume spike confirmation
- EMA200 trend
- Bollinger Band lower-band touch / near-lower / squeeze
- Hourly momentum breakout
- 12h breakout detector

These are **behavioral** features, not symbol labels. The design is correct. The issue is the **thresholds and overrides** applied afterwards.

### 2.3. Entry gate (`run_trades.py:_buy_top_symbols`)

The gate has many correct pieces, but three problems corrupt behavior selection:

1. **Bootstrap predictor bypass**: `pnl_service.validate()` returns `True` in bootstrap mode, overriding behavioral veto. On 12 Jul this allowed BTC (`vol_spike=0.0038`), SUI (`vol_spike=0.0643`), and PEPE (`vol_spike=0.0008`) to enter despite no confirming behavior.

2. **A4 quality gate too loose for behavior**: `entry_quality_max_failures` allows 1 failing dimension and `entry_quality_vol_spike_min_range_scalp` is 0.30–0.40. Volume is the primary behavior confirmation; without it the system is guessing direction.

3. **Static symbol/category caps**: `category_max_positions_enabled` was recently disabled (s75) to unlock capital, but the category concept itself (LARGE_CAP, MIDCAP_ALT, NARRATIVE, MEME) still ranks symbols by label rather than by current behavior. On 13 Jul, `ARBUSDT` was repeatedly blocked by `LÍM_CATEGORÍA MIDCAP_ALT` even though it had +13% / $16.7M volume behavior.

---

## 3. Per-Regime Diagnostic

### 3.1. `SPOT_BULL`

**Desired behavior**: assets in sustained upward move, entering on pullbacks with volume.

**Expert criteria (June 2026)**:
- Price 20–40% of weekly range (pullback to support)
- `vol_spike > 1.5x`
- RSI 4h 35–55

**Current system**:
- Screener ranks by 24h `vol × positive chg`.
- Signal generator has all needed indicators.
- A4 gate default `vol_spike_min=1.2` is reasonable but bootstrap bypass can override it.
- `min_success_probability` and `min_pnl` filters are relaxed in bootstrap.

**Finding**: SPOT_BULL is the least active regime recently, so there is little live data. The architecture is sound, but the bootstrap bypass and category caps remain risks.

### 3.2. `RANGE_SCALP`

**Desired behavior**: price at lower band of a range, mean-reversion likely, volume confirming interest.

**Current system**:
- Screener ranks by `vol × abs(chg)`.
- Signal generator rewards `bb_lower_touch` and low RSI.
- Profile A uses `entry_quality_vol_spike_min_range_scalp=0.30`, which is far below the 1.5x needed to confirm direction.

**Live 12 Jul data**: 29 trades, all in `RANGE_SCALP`. The big winners (PEPE +$18.97 in 42 min, SUI +$7.13 in 57 min, NEAR +$4.74) were captured by PredExit/SSO/PRS. The big losers (SOL, BTC, PEPE SLs) entered with near-zero volume. **The exit tools worked; the entry behavior filter failed.**

### 3.3. `PRESERVATION`

**Desired behavior**: only narrative/trending tokens with a strong short-term catalyst and volume.

**Current system**:
- Screener zeroes spot ranking except narrative tokens.
- `preservation_narrative_only=True`.
- Entry gate has `preservation_max_rsi=65`.

**Finding**: correct in principle. The 12 Jul TAOUSDT loss was a narrative token with overheated RSI/momentum; this suggests the narrative set or the RSI filter needs behavior tuning, not that the regime concept is wrong.

### 3.4. `FUTURES_SHORT`

**Desired behavior**: short bounces in a bear market. We want assets that are *currently rising* within a downtrend (overbought bounces), because those are the ones likely to fall next.

**Current system**:
- Screener uses `_futures_short_score`: rewards moderate bounces (chg 0.5%–8%), penalizes already-dumped (`chg < -8%`) or breakout (`chg > 12%`) assets.
- Prefers RSI 45–65 with trend DOWN.
- Ranking: `RSI + SELL bonus` overbought signals get priority.

**Finding**: this is the most behaviorally sophisticated part of the system. The concept that "we want assets that lose value" is correctly encoded. The main risk is the **static futures symbol list**; behavior should drive selection, not a fixed list.

---

## 4. Profile-A / Profile-B / Profile-C Behavior Mapping

Profiles must not change the target behavior; they change **exposure and patience**.

| Dimension | A (aggressive) | B (balanced) | C (defensive) |
|-----------|---------------|--------------|---------------|
| `min_signal_strength` | lower (-0.05) | baseline | higher (+0.05) |
| Position size multiplier | 1.18 | 1.00 | 0.88 |
| Quality floor (`entry_quality_vol_spike_min`) | 0.30 | 0.35 | 0.40 |
| `range_scalp_max_rsi` | 60 | 60 | 58 |
| Patience / hold time | longer | baseline | shorter |
| SL/TP tolerance | wider | baseline | tighter |

**Current issue**: A is so aggressive that it accepts behaviorally dead entries (`vol_spike=0.0038`). Profile A should mean "more capital on good setups," not "lower standards for what a good setup is."

---

## 5. What the Live Data Shows About Behavior vs Symbol

Source: `Dev/benchmarks/live_jul12_closed.json` and `Dev/daily_analysis_2026-07-09.md`.

### 5.1. Winners were behaviorally alive

| Trade | Regime | Exit reason | Hold | vol_spike at entry | PnL |
|-------|--------|-------------|------|-------------------:|-----:|
| PEPE | RANGE_SCALP | `profit_predexit_capture` | 42 min | 0.2941 | +$18.97 |
| SUI | RANGE_SCALP | `sso_target_contribution` | 57 min | 0.0074 | +$7.13 |
| NEAR | RANGE_SCALP | PRS partial/full | 34–40 min | 0.2381 | +$4.74 |
| DOGE | RANGE_SCALP | PRS partial | 28 min | 3.24 | +$0.25 |
| TRX | RANGE_SCALP | `sso_flat_stagnation_timeout` | 3h | 1.58 | +$0.88 |

Even PEPE (vol_spike 0.29) was in a micro-pump; it was not a random flat asset. The exits captured the moves quickly.

### 5.2. Losers were behaviorally dead

| Trade | Regime | Exit reason | vol_spike at entry | last_pred_pnl |
|-------|--------|-------------|-------------------:|--------------:|
| BTC | RANGE_SCALP | `stop_loss` | 0.0038 | -1.66 |
| BTC | RANGE_SCALP | `stop_loss` | 0.1809 | -2.38 |
| SUI | RANGE_SCALP | `stop_loss` | 0.0643 | -14.77 |
| PEPE | RANGE_SCALP | `stop_loss` | 0.0008 | -0.93 |
| SOL | RANGE_SCALP | `stop_loss` | 0.7261 | -3.44 |
| BNB | RANGE_SCALP | `stop_loss` | 0.4886 | -1.62 |

All had near-zero volume spikes and negative predicted PnL. The system entered symbols with no short-term directional behavior. SL was the correct backstop, but the entries should have been blocked.

### 5.3. Missed opportunities were blocked by label, not behavior

Source: `Dev/missed_ops_analysis_20260713.txt` and `Dev/daily_analysis_2026-07-09.md`.

- `ARBUSDT`: +13.14%, $16.7M volume, blocked by `LÍM_CATEGORÍA MIDCAP_ALT`.
- `SENTUSDT`: +30.86%, $76M volume, not in monitored universe.
- These were behaviorally strong assets blocked by static category/universe constraints.

---

## 6. Expert Synthesis by Role

### 6.1. Crypto / trading lens

- **Bull regimes**: buy pullbacks with volume; avoid chasing overbought moves.
- **Range regimes**: buy support, sell resistance; volume confirms the bounce.
- **Bear regimes**: short bounces; "assets losing value" is the product.
- The symbol is a container; the behavior is the edge.

### 6.2. Statistics lens

- 12 Jul: strategy-managed exits were profitable in aggregate; only SL exits destroyed PnL.
- Base rate of true opportunities is ~3.7% of raw signals (`missed_ops_analysis`).
- A behavior-first filter should raise precision, not recall. Lowering thresholds increases recall but destroys precision when base rate is low.

### 6.3. ML lens

- Predictor is bootstrap; it cannot yet classify behavior.
- Until 50+ real trades exist, hard behavioral rules (volume spike, RSI range, BB position, 4h trend) must gate entries.
- After bootstrap, train a **regime-specific short-horizon model** (30–60 min) that predicts the probability the asset will exhibit the desired behavior, not just raw PnL.

### 6.4. Risk / operations lens

- SL is the backstop; it must not be the plan.
- Position sizing (Kelly + profile multipliers) should scale the exposure to the quality of the behavior, not compensate for weak behavior.
- Category limits should be replaced or augmented by **behavior limits**: max positions per behavior bucket (e.g., only N low-vol entries, only M high-vol entries).

---

## 7. Recommendations

### 7.1. Immediate: make entry behavior-regime explicit

Create a single `_is_behavior_valid_for_regime(symbol, signal_meta, regime, profile)` helper that returns `(is_valid, reason)` and is called by `_filter_buy_candidates` / `_buy_top_symbols`.

| Regime | Required behavior at entry |
|--------|---------------------------|
| `SPOT_BULL` | `vol_spike >= 1.5`, `RSI_4h 35–55`, `BB_position <= 0.55`, `trend_4h != DOWN` |
| `RANGE_SCALP` | `vol_spike >= 1.2`, `RSI 25–60`, `BB_position <= 0.40`, recent lower-band touch or squeeze |
| `PRESERVATION` | narrative token + `vol_spike >= 1.5` + `RSI <= 60` + catalyst signal |
| `FUTURES_SHORT` | `vol_spike >= 1.0`, RSI 45–70, `trend_4h == DOWN`, recent bounce (positive 1h/4h change but not >12%) |

Use the stricter profile values for A/B/C only inside each regime's bounds (e.g., A=1.2 vol, B=1.5 vol, C=1.8 vol in RANGE_SCALP), never below a hard floor.

### 7.2. Immediate: remove bootstrap predictor bypass for entry

When `bootstrap_mode=True`, the predictor must **not** auto-approve. The behavioral gate above becomes the sole approver.

### 7.3. Short-term: replace category caps with behavior buckets

Instead of `MIDCAP_ALT 2/2`, `LARGE_CAP 1/1`, etc., allocate slots by:
- High-conviction behavior score (e.g., top N by combined volume × BB position × RSI fit)
- Regime-specific max positions per behavior tier (momentum, mean-reversion, narrative, short-bounce)

This would have allowed ARBUSDT to enter on 13 Jul.

### 7.4. Short-term: tune exit tools per regime/profile

| Regime | Target cycles to profit | Primary exit | Secondary exit |
|--------|------------------------:|--------------|----------------|
| `SPOT_BULL` | 6–24 cycles (30 min–2h) | PRS / trailing | SSO-4 target |
| `RANGE_SCALP` | 2–12 cycles (10 min–1h) | PredExit / SSO target | PRS peak protection |
| `FUTURES_SHORT` | 2–8 cycles (10–40 min) | PredExit / SSO-6 bounce rescue | PRS / trailing |
| `PRESERVATION` | 4–16 cycles (20 min–1.3h) | SSO / PRS | Hard TP |

Profile A adds +20% patience; profile C subtracts -20%.

### 7.5. Medium-term: behavior-predictive model

After 50+ real trades, train per-regime classifiers that predict:
- Probability the asset will reach target profit within the target cycle window.
- Probability it will hit SL within that window.

Input features: `vol_spike`, `rsi`, `bb_position`, `trend_4h`, `ema200_trend`, regime, profile.

### 7.6. Medium-term: dynamic universe by behavior

Use `pair_screener.py` to scan all USDT pairs, but sort into behavior tiers and let regime choose which tier to trade. A symbol should enter the universe based on having enough liquidity **and** the right current behavior, not because it is in a static list.

---

## 8. Files Referenced

- `run_trades.py` — entry gate, A4 quality gate, SL/TP/PRS/SSO/UG/PeakCapture orchestration
- `pair_screener.py` — regime-aware universe ranking
- `autonomous_signals.py` — behavioral signal generation
- `pnl_service.py` / `pnl_predictor.py` — predictor validation and bootstrap behavior
- `config.py` — regime/profile parameters, category thresholds
- `profit_realization_supervisor.py`, `smart_scale_out.py`, `peak_capture_engine.py`, `unified_governance_supervisor.py` — exit toolkit
- `Dev/benchmarks/live_jul12_closed.json` — live closed-trade data
- `Dev/daily_analysis_2026-07-09.md` — 24h performance breakdown
- `Dev/missed_ops_analysis_20260713.txt` — missed-opportunity scan
- `Dev/scripts/fix_futures_profiles_s72_7e8828.py` — profile parameter definitions
