---
title: "Why 0 LONGs? Root Cause + Opportunity Arbiter Fix"
type: analysis
status: historical
created_at: 2026-09-11
superseded_by: "calibracion_entradas-a20ab8-v3.md s135"
---

# Why There Are 0 LONGs and How Opportunity Arbiter Fixes It

## The Problem: Directional Bias by Incident, Not Design

### What We Observed

After deploy on 2026-09-07, the system opened 11 candidatas out of 58 arbitraje decisions. All 11 were SHORTs.

On 2026-09-09 (48h snapshot), the same pattern persisted: no LONGs in RANGE_SCALP, only in edge cases.

### Why This Happened

The issue is **not** that the system is biased to prefer SHORTs. It's that the system constructs a LONG or SHORT candidate first, then confirms that side's thesis. By the time the counterside is even *generated*, the pre-chosen side has already monopolized the available capital and time windows.

**Root causes (not mutually exclusive):**

1. **Inersia of pre-selection**: When a `ready_candidate` is created, it already has `expected_side` baked in from the signal generation phase. The FSM (`staged_entry._direction_transition`) confirms *that* side, not the other side.

2. **Non-symmetric theses evaluation**: 
   - SHORT Futures receives: full SR path, funding costs, optimal sizing.
   - LONG Spot receives (later or in parallel): simpler analysis, sometimes from stale data.
   - They're not evaluated under identical market snapshots.

3. **Selective gate application**:
   - SHORT gate: "Must be at resistance" (strong, spatial).
   - LONG gate: "Must not be overbought" (weak, momentum-based).
   - A SHORT at resistance passes; an equivalent LONG at support never gets tested because the SHORT already consumed the cycle.

4. **Portfolio selection order**:
   - `portfolio_selector.py` ranks by (expected_pnl, signal_strength, probability, -timestamp).
   - Two candidates with identical scores: the one that reached `ready` first gets priority.
   - If SHORT candidates are created earlier in the cycle (due to faster futures screener), they'll accumulate confirmed checks first.

5. **Quality gate timing**:
   - SHORT might pass entry_profile_gate A/B/C at timestamp T.
   - LONG might fail it at T+5s due to slightly different rsi/vol_spike, or might not even be generated if the screener timeout was hit.
   - Result: SHORT opens, LONG never reaches the queue.

### Evidence

From the live 48h analysis (§5.2 of `rules.md`):
- **12 trades opened**: 11 SHORTs (range scalp), 1 LONG (spot bull, late in the window).
- **SHORTs that won** (exits by SSO/PredExit): all had `vol_spike >= 1.5x`.
- **SHORTs that lost** (exits by SL): all had `vol_spike < 1.0x` — yet they opened because LONG was never evaluated.
- **Rejected sides that would have won**: In simulated 48h backtest of "evaluate both sides, pick the best," LONG would have been chosen ~35% of the time vs. SHORT's 65%.

---

## How Opportunity Arbiter Fixes This

### The Design

Instead of:
```
signal → side chosen → `expected_side=SHORT` → FSM confirms SHORT
```

Use:
```
symbol → frozen snapshot → build both theses in parallel → evaluate both under identical conditions → pick the best
```

### Key Differences

| Aspect | Current | Opportunity Arbiter |
|---|---|---|
| **Timing of side choice** | Early (signal generation) | Late (after both theses evaluated) |
| **Market data** | LONG and SHORT see different snapshots | Both see identical frozen snapshot |
| **Gate application** | Asymmetric (different gates per signal) | Symmetric (same gate logic, applied to both) |
| **Utility comparison** | Implicit (first to pass quality wins) | Explicit (normalized utility score) |
| **ABSTAIN option** | No; weak trades still open | Yes; abstain if no thesis clears bar |
| **Capital allocation** | Greedy (fill slots as candidates arrive) | Optimal (best trade per symbol per regime) |

### Example: ASTERUSDT (from §12 of multiregime spec)

**Scenario**: RANGE_SCALP, price near support (BB lower).

**Current system**:
1. Signal generator produces SHORT candidate (resistance scalp).
2. Direction FSM confirms SHORT direction.
3. SHORT passes quality gate (vol_spike=1.65 > 1.2 floor).
4. SHORT opens.
5. LONG opportunity (same price, same vol_spike) never reached the gate.

**Opportunity Arbiter**:
1. Snapshot frozen: price, vol, RSI, ATR, bands, everything.
2. Build LONG thesis: anchor at support, target at resistance, utility = +0.615%.
3. Build SHORT thesis: anchor at resistance, target at support, utility = -0.484% (rejected, no rejection signal).
4. Select LONG (highest utility).
5. LONG opens with better expected value.

---

## Specific Fixes for Common Blockers

### Blocker 1: "SHORT Futures is not executable due to margin"

**Current**: Candidate waits, tries again next cycle, eventually times out. LONG never evaluated.

**Arbiter fix**: 
- Build both SHORT and LONG theses.
- If SHORT Futures fails `eligible=False` due to margin, but LONG Spot passes, select LONG.
- SHORT Spot Margin is still considered as fallback.
- Execution-viability is part of the utility calculation, not a separate veto.

### Blocker 2: "LONG Spot was gated out by entry_quality_vol_spike"

**Current**: `vol_spike=0.8 < 1.2 floor` → gate rejects LONG immediately. SHORT (with same vol) evaluates under a looser gate (0.30 floor for profile A) → SHORT passes.

**Arbiter fix**:
- Unified gate: `vol_spike >= X` applied to both sides.
- If both have same vol_spike, both pass or both fail.
- Asymmetric gates are no longer possible because they're baked into signal generation (per-side logic).
- Only degree of freedom is utility threshold (`min_utility_threshold`), which applies uniformly.

### Blocker 3: "LONG and SHORT were evaluated at different times"

**Current**: 
- SHORT evaluated at T=10:30:00 on 1h bar [10:00-11:00).
- LONG evaluated at T=10:35:00 on [10:00-11:00) (same bar, but 5 seconds of price movement).
- Price moved -0.2% in those 5 seconds → LONG's probability dropped, SHORT's rose.

**Arbiter fix**: 
- Snapshot timestamp is fixed at decision time.
- Both SHORT and LONG evaluate against that **same** historical candle close.
- Price within that 5-second window doesn't affect either thesis.

### Blocker 4: "SHORT scored higher on expected_pnl due to better predictor state"

**Current**: 
- Predictor has 11 SHORT trades in history (confident).
- Predictor has 2 LONG trades in history (less confident).
- Predictor gives SHORT: P(success)=0.55, LONG: P(success)=0.48.
- SHORT wins by scoring higher, even if both have identical geometry.

**Arbiter fix**: 
- Predictor quality is a factor in utility, not a veto.
- If LONG has lower confidence, its utility is lower, but it's still compared fairly.
- Over time, as both sides accumulate real trades, the predictor confidence evens out.
- Bootstrap mode no longer bypasses the behavioral gate (from `pnl_service.py` fix in s76), so both sides must meet hard behavioral thresholds.

---

## How This Resolves "0 LONGs"

### Immediate Effect (First 7 Days)

**Expected**:
- LONG trades increase from ~5% to ~35-45% (closer to symm market).
- Not 50/50 because some regimes (FUTURES_SHORT) naturally prefer SHORT.
- PnL should improve (or stay same) due to better opportunity selection.

**Measurement**:
```
SELECT 
    COUNT(*) as total_trades,
    SUM(CASE WHEN side = 'LONG' THEN 1 ELSE 0 END) as longs,
    SUM(CASE WHEN side = 'SHORT' THEN 1 ELSE 0 END) as shorts,
    ROUND(100.0 * SUM(CASE WHEN side = 'LONG' THEN 1 ELSE 0 END) / COUNT(*)) as long_pct
FROM trades_opened
WHERE opened_at >= NOW() - INTERVAL '7 days'
GROUP BY date_trunc('day', opened_at);
```

### Medium-term (Weeks 2-4)

**Expected**:
- Utility predictions become more accurate (as per §9.1, KPI: "Utility vs Realized PnL correlation > 0.65").
- Regime-specific theses show distinct side distributions:
  - SPOT_BULL: 70-80% LONG.
  - RANGE_SCALP: 45-55% LONG/SHORT (symmetric).
  - FUTURES_SHORT: 15-25% LONG (only emergent cases), 75-85% SHORT.
  - PRESERVATION: 100% LONG (by design).

### Performance Impact

**Do NOT expect**:
- Instant 2x win rate (too optimistic).
- Elimination of losses (risk is still real).

**DO expect**:
- More consistent side distribution (end of "all SHORTs" days).
- Better PnL correlation with predicted utility (fewer "high-utility trades that lost").
- Slightly higher Sharpe ratio (due to implicit regime awareness + utility normalization).

---

## Implementation Path (From s76 → Production)

### Phase 0: Readiness Check (This Week)
1. Verify `rules.md` accurately reflects current code (staging vs. docs).
2. Identify any per-regime asymmetry in gates that's not documented.
3. Confirm predictor confidence is not dominating gate decisions.

### Phase A: Instrumentation (Weeks 1-2)
1. Add `thesis_snapshot_v1` field to `ready_candidates`.
2. Implement thesis builders for all 4 regimes (without changing behavior).
3. Log audit trail: every thesis evaluation (both sides).
4. Measure: side distribution, utility accuracy, gate distribution.

**Risk**: None (read-only, no behavior change).

### Phase B: Shadow Selection (Week 2-3)
1. Compute optimal side (best utility) in parallel with current selection.
2. Log divergences: "current selected SHORT, optimal would have been LONG."
3. Measure frequency and magnitude of divergences.
4. No trades affected; purely diagnostic.

**Risk**: None (shadow only).

### Phase C: Canary Rollout (Week 3-4)
1. Enable opportunity selection for 10% of candidates (cohort marking).
2. Daily KPI monitoring (§9.1).
3. If KPIs positive after 50+ trades, expand to 50%.
4. Automatic rollback if win rate drops >5% or any regime goes negative.

**Risk**: Requires close monitoring; can be rolled back instantly with `docker compose restart app`.

### Phase D: Full Production (Week 5+)
1. Extend to 100% of candidates.
2. Consolidate learnings; retire old signal-generation code paths.

---

## Why This Won't Break Existing Protections

### Hard Stops Still Apply
- `max_loss_usd_per_trade`: Still enforced in `hard_risk_monitor`, applies to both sides equally.
- `sr_thesis_check` (s113): Validates that SHORT is at resistance; still applies. LONG-specific validation added symmetrically.
- `direction_confirmed`: FSM still waits for confirmation (new regime-specific geometry anchor from s111 still applies).

### Exit Tools Unchanged
- PredExit, SSO, UG, hard SL: No changes. Same mechanisms protect both LONG and SHORT.

### Capital and Risk Limits
- Kelly sizing: Now applied **after** side is selected, not before. No change to risk exposure.
- `max_open_per_cycle`: Still enforced.

---

## Quick Comparison Table

| Dimension | Current State (s113) | With Opportunity Arbiter |
|---|---|---|
| LONGs per 30 trades | ~3-5 (10-17%) | ~10-15 (33-50%) |
| SHORTs monopolizing capital | Yes (60-70%) | No (regime-dependent 15-85%) |
| Asymmetric gates | Yes (per signal type) | No (unified gates) |
| ABSTAIN rate | 0% (weak trades still open) | 15-25% (expected) |
| PnL vs predicted utility correlation | ~0.35-0.45 | ~0.65+ (target) |
| Regime-aware side distribution | Accidental | Designed |
| Rollout risk | N/A | Low (shadow + canary) |

---

## Key Principle: Maximize Opportunity Quality, Not Side Balance

The goal is **not** "50/50 LONGs and SHORTs." The goal is:

> **For every symbol, for every regime, for every market snapshot, open the single trade with the highest risk-adjusted expected value—or open nothing if no thesis clears the bar.**

If that means 80% SHORTs in FUTURES_SHORT (because it's a bear market), that's correct. If it means 70% LONGs in SPOT_BULL (because it's a bull market), that's also correct. And if it means 50/50 in RANGE_SCALP (symmetric regime), that's perfect.

The current "0 LONGs" state is an artifact of implementation order, not market conditions. Opportunity Arbiter fixes the implementation without changing the market conditions or the regime definitions.

---

## Next Actions

1. **Read and verify** `canonical/opportunity_arbiter_multiregime_v1.md` against current code.
2. **Identify dependencies**: What code already exists that can be reused (predictor, kelly sizing, gates)?
3. **Estimate effort**: 
   - Phase A: ~40 hours (thesis builders + persistence).
   - Phase B: ~20 hours (shadow selection + KPI dashboard).
   - Phase C-D: ~30 hours (rollout, monitoring, tuning).
4. **Schedule**: Target Phase C rollout early next week (after Phase A + B validation).

