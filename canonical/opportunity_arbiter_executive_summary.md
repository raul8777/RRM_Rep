---
title: "Opportunity Arbiter — Executive Summary & Decision Matrix"
type: summary
status: historical
created_at: 2026-09-11
superseded_by: "calibracion_entradas-a20ab8-v3.md s135"
audience: "Product owner, traders, system architects"
---

# Opportunity Arbiter — What, Why, How

## TL;DR

**Problem**: System opens only SHORTs (11/11 trades on 2026-09-07). LONGs never get evaluated.

**Root cause**: LONG/SHORT decision is made at signal generation time (pre-selection), not after both are evaluated.

**Solution**: Build both theses from a frozen market snapshot, compute risk-adjusted utility for each, select the best—or abstain.

**Expected outcome**: 
- LONGs return to ~35-50% of trades (regime-dependent).
- Better PnL (utility predictions 20%+ more accurate).
- Same risk/protection (SL, hard stops unchanged).

**Implementation timeline**: 3-4 weeks (shadow mode + canary rollout).

**Rollback risk**: Low (shadow + instant docker restart).

---

## Why 0 LONGs? The Four Layers

```
Layer 1: Pre-selection
  ├─ SHORT candidate created early (faster screener)
  └─ `expected_side=SHORT` baked into ready_candidate
           ↓
Layer 2: Direction FSM
  ├─ Confirms that pre-chosen SHORT
  └─ LONG is never even generated
           ↓
Layer 3: Gate Asymmetry
  ├─ SHORT gate: "at resistance" (spatial, strong)
  ├─ LONG gate: "not overbought" (temporal, weaker)
  └─ Same vol_spike treated differently per side
           ↓
Layer 4: Portfolio Selection
  ├─ Ranks by (expected_pnl, score, probability, -timestamp)
  └─ SHORT reached `ready` first → wins the slot
           ↓
Result: LONG never reaches evaluation
```

---

## Opportunity Arbiter: The Fix

```
Symbol enters screener
           ↓
Frozen snapshot (one per symbol, all theses see it)
           ├─ Current price, ATR, RSI, BB, support, resistance
           ├─ Regime confidence, BTC context, vol spike
           └─ Same data for BOTH sides
           ↓
Build LONG thesis (spot/margin/futures) ─┐
Build SHORT thesis (spot/margin/futures) ─┤
           ↓
Validate each (gates, margin, capital) ─┬─ Both against same snapshot
           ↓
Compute utility: U = P(win)×R - P(loss)×L - costs
           ├─ Normalized across instruments
           └─ Regime-confidence scaled
           ↓
Compare: pick max(U_long, U_short)
           ├─ If max < min_threshold: ABSTAIN
           ├─ If ambiguous (within 1%): require confirmation
           └─ Otherwise: SELECT & OPEN
           ↓
Result: Best available trade per symbol per regime
```

---

## Regime-Specific Expected Effects

### SPOT_BULL
| Dimension | Today | With Arbiter | Reason |
|---|---|---|---|
| LONG % | ~80% | ~70-80% | LONGs favored in bull; SHORT only on transitions. |
| SHORT % | ~20% | ~20-30% | Transitions to bear or resistance rejection. |
| Win rate | ~45% | ~48-52% (target) | Better side selection reduces false SHORT entries. |
| Avg hold | ~2.5h | ~2.5h | Exit mechanics unchanged. |

### RANGE_SCALP
| Dimension | Today | With Arbiter | Reason |
|---|---|---|---|
| LONG % | ~5-10% | ~45-55% | Mean reversion is symmetric; both sides equal. |
| SHORT % | ~90-95% | ~45-55% | **Biggest change here.** SHORTs no longer monopolize. |
| Win rate | ~55% | ~55-60% (target) | Each side evaluated fairly; less "wrong-sided" entries. |
| Ambiguous % | ~2% | ~10-15% | Some trades too close to call; abstain increases. |

### FUTURES_SHORT (Bearer Market)
| Dimension | Today | With Arbiter | Reason |
|---|---|---|---|
| LONG % | 0% | ~5-15% | Emergent LONG only if RSI <32 (disabled by default). |
| SHORT % | ~100% | ~85-95% | SHORT dominates, but emergence path is now explicit. |
| Win rate | ~52% | ~55%+ (target) | SHORT bounces better selected; LONG emergence rare. |

### PRESERVATION (Narrative)
| Dimension | Today | With Arbiter | Reason |
|---|---|---|---|
| LONG % | ~100% | ~100% | LONG only regime; no changes. |
| No change | — | — | Narrative tokens, single side only. |

---

## Execution Comparison

### Before (Current)

```python
# run_trades.py
def _buy_top_symbols(...):
    for symbol in candidates:
        signal = autonomous_signals.analyze_symbol(symbol)
        if signal['signal'] == 'SHORT':
            # SHORT candidate created
            candidate = create_ready_candidate(
                symbol=symbol,
                expected_side='SHORT',  # ← LOCKED IN
                trade_type='futures'
            )
        elif signal['signal'] == 'LONG':
            # LONG candidate created separately, later
            candidate = create_ready_candidate(
                symbol=symbol,
                expected_side='LONG',
                trade_type='spot'
            )
        # Same slot consumed by first to pass gate
        select_for_opening(candidate)
```

**Problems**:
- Side choice irreversible.
- LONG and SHORT compete for same slot, first to qualify wins.
- Utilities never compared head-to-head.

### After (Opportunity Arbiter)

```python
# run_trades.py
def _buy_top_symbols(...):
    for symbol in candidates:
        snapshot = freeze_market_snapshot(symbol)  # ← Common data
        
        # Build both theses in parallel
        long_thesis = build_long_thesis(symbol, snapshot, settings)
        short_thesis = build_short_thesis(symbol, snapshot, settings)
        
        # Evaluate both
        long_utility = compute_utility(long_thesis)  # +0.35%
        short_utility = compute_utility(short_thesis)  # -0.12%
        
        # Select best (or abstain)
        if long_utility > short_utility and long_utility > min_threshold:
            selected_side = 'LONG'
            selected_thesis = long_thesis
        elif short_utility > min_threshold:
            selected_side = 'SHORT'
            selected_thesis = short_thesis
        else:
            selected_side = None  # ABSTAIN
        
        if selected_side:
            candidate = create_ready_candidate(
                symbol=symbol,
                selected_side=selected_side,
                thesis_snapshot_v1={
                    'long': long_thesis,
                    'short': short_thesis,
                    'selected': selected_side,
                    'utilities': {...}
                }
            )
            select_for_opening(candidate)
```

**Benefits**:
- Both sides evaluated under identical conditions.
- Utilities compared explicitly.
- ABSTAIN option available.
- Full audit trail of why each side was chosen/rejected.

---

## Risk Assessment

### What Could Go Wrong?

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Utility predictions wrong early | High | Trades based on bad predictions | Shadow mode (Phase B) validates before enabling. |
| Instrument eligibility check fails | Medium | Wrong instrument opened | Test Phase A against real margin/futures API. |
| Regime detection unstable | Low | Theses built for wrong regime | Regime transitions already well-tested in s111. |
| Ambiguous trades too frequent (>40%) | Medium | Capital idle too often | Calibrate ambiguity threshold based on Phase A data. |
| ABSTAIN rate too high (>40%) | Medium | Missed opportunities | Lower min_utility_threshold, measure trade-offs. |

### What Won't Break?

- ✅ Hard SL (`max_loss_usd_per_trade`): Unchanged, applied after side selection.
- ✅ Exit tools (PredExit, SSO, UG): Unchanged, work on both sides equally.
- ✅ Position limits (`max_open_per_cycle`): Unchanged, still enforced.
- ✅ SR validation (s113): Unchanged, now applied symmetrically to both sides.
- ✅ Futures leverage (never doubled): Unchanged, PnL formula stays same.

---

## Phase Rollout

### Phase A: Instrumentation (Week 1, No Behavior Change)
**Time**: ~40 hours  
**Risk**: None (read-only logging)

```
1. Add thesis_snapshot_v1 to DB schema.
2. Implement thesis builders for all 4 regimes.
3. Log all theses (both sides) to audit trail.
4. Deploy with current selection logic unchanged.
5. Measure: side distribution, gate rejection patterns, utility accuracy.
```

**Acceptance criteria**:
- All 58 ready_candidates have thesis_snapshot_v1 populated.
- Audit logs show both LONG and SHORT evaluated.
- Dashboard shows thesis evaluation statistics.

### Phase B: Shadow Selection (Week 2, Diagnostic Only)
**Time**: ~20 hours  
**Risk**: None (shadow only, no trades affected)

```
1. Compute optimal side (best utility) in parallel.
2. Log: "current=SHORT, optimal=LONG, delta=-0.15%"
3. Daily report: "Today 3 trades chose suboptimal side."
4. Measure: frequency of suboptimal selection, magnitude of utility loss.
```

**Acceptance criteria**:
- 100+ trades analyzed.
- Suboptimal selection rate measured.
- Utility accuracy statistics (correlation, MAE).
- No behavior change (current selection still used).

### Phase C: Canary Rollout (Week 3-4, 10% Cohort)
**Time**: ~30 hours  
**Risk**: Medium (requires monitoring)

```
1. Enable for 10% of candidates (random cohort).
2. Daily KPI monitoring (win rate, Sharpe, side distribution).
3. Auto-alert if win rate drops >5% from baseline.
4. Expand to 50% if metrics positive after 50+ trades.
5. Rollback: PATCH /config {"opportunity_arbiter_enabled": False}
```

**Acceptance criteria**:
- Win rate >= baseline (no regression).
- Utility correlation > 0.55 (improved prediction).
- Side distribution matches regime expectations (§above).
- 50+ trades with no major issue.

### Phase D: Full Production (Week 5+)
**Time**: ~20 hours  
**Risk**: Low (proven in Phase C)

```
1. Expand to 100% of candidates.
2. Monitor weekly KPIs.
3. Retire old pre-selection code paths.
4. Document learnings and threshold tunings.
```

**Acceptance criteria**:
- 500+ trades completed.
- Metrics stable at improved levels.
- No regressions in any regime.

---

## Configuration Changes

### New Section: opportunity_arbiter

```yaml
opportunity_arbiter:
  enabled: False  # Phase A: false; Phase C+: true

  # Utility thresholds
  min_utility_threshold_bps: 50  # 0.5% minimum positive utility
  ambiguity_threshold_ratio: 0.95  # utilities within 5% → mark ambiguous

  # Thesis construction
  snapshot_ttl_seconds: 240  # Refresh snapshot if older than this
  
  # Selection strategy
  selection_mode: "utility"  # "utility" or "legacy"
  
  # Canary rollout (Phase C)
  canary_fraction: 0.10  # Start at 10%
  canary_rollout_enabled: False  # Phase C+: true

  # Per-regime Kelly multipliers
  regimes:
    SPOT_BULL:
      kelly_spot: 1.05
      kelly_margin: 1.2
      kelly_futures: 1.2
    RANGE_SCALP:
      kelly_spot: 1.0
      kelly_margin: 1.1
      kelly_futures: 1.0
    FUTURES_SHORT:
      kelly_margin: 1.0
      kelly_futures: 1.0
      kelly_futures_long: 0.3  # Emergent only
    PRESERVATION:
      kelly_spot: 0.6
```

### Environment Variables (No Changes)
- Existing `FUTURES_ENABLED`, `SPOT_MARGIN_ENABLED` still control instrument availability.
- New `OPPORTUNITY_ARBITER_ENABLED` mirrors the config above for quick toggle.

---

## Decision Checklist

Before proceeding to Phase A, confirm:

- [ ] Current code matches `rules.md` + `calibracion_entradas-a20ab8-v3.md`.
- [ ] Predictor confidence is not bypassing behavioral gates (verify s76 fix).
- [ ] `support_resistance.py` computes SR without look-ahead bias.
- [ ] `entry_profile_gate.py` applies same thresholds per regime (not signal-dependent).
- [ ] Database schema allows adding `thesis_snapshot_v1 JSONB` to `ready_candidates`.
- [ ] Staging environment can run full 4-phase rollout without prod impact.
- [ ] KPI dashboard (`/metrics/shadow-readiness`) can be extended with utility metrics.

---

## Success Metrics (Post-Deployment)

### Week 1 (Phase A Validation)
- ✓ 100% of ready_candidates have thesis_snapshot_v1.
- ✓ LONG % increases from 5-10% to 20-30% (transitional).
- ✓ Audit logs show both theses evaluated.

### Week 2-3 (Phase B + Phase C Start)
- ✓ Suboptimal selection rate < 25%.
- ✓ Utility correlation with realized PnL > 0.55.
- ✓ Win rate >= baseline (no regression).
- ✓ LONG % stabilizes at regime-expected levels (SPOT_BULL 70%, RANGE_SCALP 50%, FUTURES_SHORT 10%).

### Week 4+ (Phase C Full + Phase D)
- ✓ PnL netto improves 5-15% vs baseline (if utilities are predictive).
- ✓ Sharpe ratio improves 3-8%.
- ✓ Side distribution matches market regime expectations.
- ✓ Ambiguous trade rate 10-20% (within tolerance).
- ✓ ABSTAIN rate 15-25% (design target).

---

## Comparison to Alternatives

| Approach | LONG % Expected | Risk | Implementation Time | Benefit |
|---|---|---|---|---|
| **Current (no change)** | 5-10% | Low | 0h | Stable, known. |
| **Quota rule** ("30% must be LONG") | ~30% | Medium | 5h | Crude; can trap capital on bad LONGs. |
| **Per-side thresholds** (raise LONG bar) | ~20% | Low | 10h | Still asymmetric gates. |
| **Opportunity Arbiter** (this proposal) | 35-50% | Medium | 90h | Best; universal comparison. |

---

## Go/No-Go Decision

### GO if:
- [ ] Team confident in 3-4 week timeline.
- [ ] Staging environment available for full phases A-D.
- [ ] KPI dashboard can be extended within 1 week.
- [ ] Risk appetite: willing to accept 5% regression risk for 15% upside.

### NO-GO if:
- [ ] Urgent prod issues need the team now (defer to next sprint).
- [ ] Utility predictions untrusted (need more validation work first).
- [ ] Regime detection too unstable (work on s111 more first).

---

## Next: What Happens in Review?

1. **Reviewer checks**:
   - Does the architecture (§2-5 of multiregime spec) match current codebase?
   - Are all 4 regimes correctly modeled (SPOT_BULL, RANGE_SCALP, FUTURES_SHORT, PRESERVATION)?
   - Is the utility formula (§5) implementable with existing predictor?

2. **Staging validation**:
   - Phase A runs cleanly without behavior change.
   - Thesis logs are populated and readable.
   - Dashboard can visualize utility per thesis.

3. **Go-live gating**:
   - Phase C canary passes for 50+ trades.
   - Win rate regression < 5%.
   - Utility correlation > 0.55.

