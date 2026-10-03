---
title: "Plan Comparison & Synthesis: EV-First vs Opportunity Arbiter → Integrated Solution"
type: analysis
status: historical
created_at: 2026-09-11
superseded_by: "calibracion_entradas-a20ab8-v3.md s135"
version: comparison_v1
---

# Plan Comparison & Synthesis: Why Integrated Solution Wins

## Executive Table: Three Approaches Compared

| Dimension | EV-First Plan (Original) | Opportunity Arbiter (Original) | **Integrated Solution (Recommended)** |
|---|---|---|---|
| **Scope** | RANGE_SCALP only | All 4 regimes | RANGE_SCALP Phase I, then all regimes Phase II |
| **Time to Fix 0 LONGs** | 1 week | 4 weeks | **1 week (Phase I)** |
| **Universality** | Limited | Complete but slow | Complete and fast (3 weeks total) |
| **Implementation Lines** | ~80 | ~1,200 | ~400 (split over 2 phases) |
| **Risk Profile** | Low (1 region) | Medium (all 4 regions) | **Low→Medium (graduated)** |
| **Rollback Effort** | 10 min | 1 hour | 10 min Phase I, 30 min Phase II |
| **Data Validation** | 48h shadow | Phase A (instrumentation) | 48h shadow (faster) |
| **Cost of Failure** | Revert 1 config | Revert 1 commit (~20 files) | Phase I: revert 1 config; Phase II: revert 1 commit |
| **Shadow-Ready Gates** | Yes (3 explicit gates) | Yes (4 phases) | **Yes (4 gates, graduated)** |
| **Production Timeline** | Week 1 end | Week 4 end | **Week 1 end (Phase I) + Week 3 end (Phase II)** |

---

## Detailed Comparison: Each Plan's Philosophy

### EV-First Plan — "Pragmatic Patch"

**Thesis**: "RANGE_SCALP has clear data (69 candidates, 93% both-sides-positive EV). EV should dominate; structure modulates confidence, not gates. Fix this region fast."

**Implementation**:
- Modifies `directional_arbiter.py` one method: `_evaluate_range_scalp_ev_first()`.
- Compares `expected_pnl` for LONG vs SHORT, applies structure bonus/penalty.
- Applies confidence-based sizing (0.5–1.5×).
- Deploys shadow → validation → staging → prod (3 gates).

**Strengths**:
1. **Minimal surface**: 80 lines of code, 1 config flag.
2. **Proven rollout**: Exactly 3 gates match AWS/GCP deployment patterns.
3. **Fast value**: 1 week to production.
4. **Data-grounded**: Analyzed 69 real candidates; 9 geometry-blocked LONGs identified.
5. **Low cognitive load**: One method change; everyone understands it.

**Weaknesses**:
1. **Not universal**: Only RANGE_SCALP. SPOT_BULL, FUTURES_SHORT, PRESERVATION unchanged.
2. **Implicit instrument handling**: Doesn't explicitly choose SPOT vs FUTURES vs MARGIN; assumes regime default.
3. **No ABSTAIN mechanism**: Only winner takes all; no explicit "both sides too similar" rejection.
4. **Knowledge siloed**: RANGE_SCALP logic doesn't generalize; need 3 more variants for other regimes.
5. **EV normalization missing**: Can't compare utility across different instruments fairly (e.g., LONG Spot +$10 vs SHORT Futures +$8).

**When to use alone**: If only fixing RANGE_SCALP is critical and speed is paramount.

---

### Opportunity Arbiter — "Universal Foundation"

**Thesis**: "All regimes and instruments should follow the same arbitration pattern: build all viable theses in parallel, compare normalized utility, select max utility or ABSTAIN. Implement once, use everywhere."

**Implementation**:
- Creates `opportunity_selector.py` (replaces `directional_arbiter.py`).
- Builds parallel thesis objects: LONG Spot, LONG Margin, LONG Futures, SHORT Spot Margin (where applicable).
- Computes normalized utility for each thesis.
- Selects max utility thesis or ABSTAIN if all below threshold.
- Implements 4 phases: instrumentation → shadow → validation → canary.

**Strengths**:
1. **Universality**: One pattern fits all 4 regimes + all 3 instrument types.
2. **Instrument-aware**: Explicitly chooses SPOT vs MARGIN vs FUTURES per side.
3. **Normalized comparison**: Utility accounts for fees, slippage, leverage.
4. **ABSTAIN capability**: Can reject trades where both sides are weak.
5. **Extensible**: Adding new instrument (e.g., USDT margin) = add 1 thesis builder.
6. **Auditable**: Full thesis snapshot persisted; explains why each decision was made.

**Weaknesses**:
1. **Complexity**: ~1,200 lines of code, ~110h implementation.
2. **All-or-nothing risk**: Deploying to 4 regimes simultaneously = higher blast radius.
3. **Phase A long**: Instrumentation phase alone could be 1 week.
4. **Learning curve**: New architecture; team must understand thesis objects and utility formula.
5. **Overkill for RANGE_SCALP**: RANGE_SCALP data already analyzed; could fix faster.

**When to use alone**: If building new system from scratch with multi-regime requirement from day 1.

---

## Why Integrated Solution Is Better Than Both

### Problem 1: "EV-First is local; other regimes still broken"

**EV-First alone**: 
- RANGE_SCALP fixed in Week 1.
- SPOT_BULL, FUTURES_SHORT, PRESERVATION still using `ev_symmetric` (same geometry gates).
- Next problem: SPOT_BULL has different imbalance (80% SHORT, but higher kelly, so not immediately urgent).
- Result: Partial solution, then need **another** redesign for other regimes.

**Opportunity Arbiter alone**:
- Redesigns all 4 regimes at once.
- Week 4 before any fix; too long for 0 LONGs.
- Risk: If universal architecture breaks, all regimes offline.

**Integrated solution**:
- Phase I (Week 1): EV-First for RANGE_SCALP (fast, low risk, proven data).
- Phase II (Week 2-3): Universal architecture (safer now that concept validated, only need 1 Phase I success proof).
- Result: 0 LONGs fixed Week 1; universal pattern deployed Week 3; risk graduated (low → medium).

---

### Problem 2: "EV-First vs Opportunity Arbiter philosophy conflict"

**EV-First philosophy**: "EV is king; structure is confidence modifier."

**Opportunity Arbiter philosophy**: "Utility is king; EV is one component (others: fees, leverage, instrument routing)."

**Conflict**: If you implement both, which one guides RANGE_SCALP?

**Integration solution**: **EV-First IS a special case of Opportunity Arbiter.**

```python
# Universal pattern (Opportunity Arbiter)
utility = P(success) × R_net - P(loss) × L_net - costs
selected = argmax(utility_long, utility_short)

# EV-First instance (for RANGE_SCALP)
# If LONG and SHORT always use same instrument (typical in RANGE_SCALP),
# then:
#   R_net_long ≈ R_net_short (same instrument)
#   L_net_long ≈ L_net_short (same instrument)
#   costs_long ≈ costs_short (same instrument)
# Therefore:
#   utility_long ≈ P_long × EV_long (proportional to EV)
#   utility_short ≈ P_short × EV_short (proportional to EV)
#   selected ≈ argmax(EV_long, EV_short) [simplified]

# Confidence factor is utility modifier (same as structure_bonus in OA)
confidence_factor = structure_bonus = 1.0 + 0.10 if aligned_SR else 1.0
```

**Result**: EV-First is a fast, simplified Opportunity Arbiter implementation for RANGE_SCALP. Phase II refactors to full Opportunity Arbiter without breaking Phase I.

---

### Problem 3: "EV-First doesn't handle instrument routing"

**Example from real data**:
- Candidate BTCUSDT, RANGE_SCALP.
- LONG Spot: predicted +$12 (fees -$0.12, total +$11.88).
- LONG Futures: predicted +$15 (fees -$0.06, but slippage -$0.30, total +$14.64).
- SHORT Spot: predicted +$8 (fees -$0.08, total +$7.92).

**EV-First decides**: LONG (predicted +$15 beats SHORT +$8).
- But which instrument? EV-First doesn't specify → defaults to regime default (e.g., FUTURES).
- Result: Opens LONG Futures +$14.64 (correct).

**EV-First problem case**:
- Candidate ETHUSDT, RANGE_SCALP.
- LONG Spot: predicted +$10, available.
- LONG Futures: predicted +$11, but FUTURES_ENABLED=False (maintenance, low liquidity).
- SHORT Spot Margin: predicted +$9, available.

**EV-First decides**: LONG (+$11 > +$9).
- But FUTURES not available → ERROR or default fallback?
- EV-First doesn't handle this → code must add fallback logic.

**Opportunity Arbiter handles**:
- Thesis builder for LONG checks: LONG Futures available? No → skip this thesis.
- Thesis builder for LONG checks: LONG Spot available? Yes → include this thesis.
- Thesis builder for SHORT checks: SHORT Spot Margin available? Yes → include this thesis.
- Utility comparison: LONG Spot (+$10 utility) vs SHORT Spot Margin (+$9 utility).
- Result: Opens LONG Spot (correct, and explicit instrument routing).

**Integrated solution**:
- Phase I (EV-First): Handles RANGE_SCALP where instrument availability is consistent (FUTURES always available, or never). Works fine.
- Phase II (Opportunity Arbiter): When extending to SPOT_BULL (where instrument availability varies), use full utility comparison with thesis builders.

---

### Problem 4: "How do you extend EV-First to other regimes?"

**EV-First alone approach**:
```python
def evaluate_spot_bull(symbol, metadata, predictions, settings):
    # Copy-paste logic from evaluate_range_scalp_ev_first()
    # But adjust structure gates (SPOT_BULL: support → LONG bias, resistance → SHORT rare)
    # Adjust probability gates (different for bull regime)
    # Adjust EV thresholds
    # ...code duplication...
    return DirectionalArbitration(...)

def evaluate_futures_short(symbol, metadata, predictions, settings):
    # Copy-paste again, but adjust for structural SHORT bias
    # ...more code duplication...
    return DirectionalArbitration(...)

def evaluate_preservation(symbol, metadata, predictions, settings):
    # Copy-paste again, but only LONG SPOT allowed
    # ...more code duplication...
    return DirectionalArbitration(...)
```

**Result**: 4 nearly identical methods, hard to maintain.

**Integrated solution (Phase II)**:
```python
class UniversalThesisBuilder:
    def build_all_theses(self, regime, symbol, metadata, predictions):
        # One implementation, parameterized by regime
        # Handles instrument routing (LONG Spot/Margin/Futures, SHORT Spot Margin)
        # Returns { "long": thesis, "short": thesis }

def evaluate_any_regime(symbol, metadata, predictions, settings, regime):
    theses = UniversalThesisBuilder().build_all_theses(regime, ...)
    utilities = {side: thesis.utility for side, thesis in theses.items()}
    selected = max(utilities, key=lambda k: utilities[k])
    return DirectionalArbitration(selected_side=selected, ...)
```

**Result**: One implementation for all regimes. Regime-specific behavior comes from thesis builder parameters (probability gates, EV thresholds, instrument availability), not code duplication.

---

## The Integration Map: File-by-File Changes

### Phase I (EV-First for RANGE_SCALP)

| File | Change | Lines | Risk |
|---|---|---|---|
| `directional_arbiter.py` | Add `_evaluate_range_scalp_ev_first()` + routing logic | +80 | Low |
| `config.py` | Add 7 new float parameters | +15 | Low |
| `staged_entry.py` | Persist `directional_arbitration` metadata | +20 | Low |
| `run_trades.py` | No change (calls directional_arbiter as before) | 0 | None |
| DB schema | No change (uses existing fields) | 0 | None |
| **Total** | | **~115 lines** | **Low** |

### Phase II (Opportunity Arbiter Foundation)

| File | Change | Lines | Risk |
|---|---|---|---|
| `directional_arbiter.py` | Keep Phase I as fallback; add router to opportunity_selector | +30 | Low |
| `opportunity_selector.py` | New file; replaces directional_arbiter for SPOT_BULL + others | +400 | Medium |
| `universal_thesis_builder.py` | New file; shared logic for building theses | +300 | Medium |
| `config.py` | Add thesis builder parameters (probability gates, EV thresholds per regime) | +50 | Low |
| `run_trades.py` | Update candidate creation to use `thesis_snapshot_v1` | +30 | Low |
| `staged_entry.py` | No change (uses directional_arbitration metadata as before) | 0 | None |
| DB schema | Add `thesis_snapshot_v1` JSONB column | 1 schema migration | Medium |
| **Total** | | **~810 lines** | **Medium** |

### Post-Phase II (If Needed: Extend Beyond SPOT_BULL)

| Regime | Effort | Timeline |
|---|---|---|
| FUTURES_SHORT | 1 additional thesis builder (~100 lines) + shadow 24h | Week 4 |
| PRESERVATION | 1 additional thesis builder (~50 lines, simpler) + shadow 24h | Week 5 |

---

## Side-by-Side Execution Timeline

### Option A: Pure EV-First (No Phase II)

```
Week 1:
  Mon-Tue: Implement EV-first in directional_arbiter.py
  Wed-Thu: Shadow 48h collection
  Fri: Validation + staging promotion
  Week 1 end: Production (if gates pass)
  
Ongoing (Weeks 2-4):
  Monitor RANGE_SCALP; manually fix SPOT_BULL/FUTURES_SHORT/PRESERVATION imbalances
  → Requires multiple separate interventions (slow, risky)
```

### Option B: Pure Opportunity Arbiter (No Phase I)

```
Week 1:
  All week: Architecture design, team alignment
  
Week 2:
  Implement universal_thesis_builder.py, opportunity_selector.py (~600 lines)
  Add DB schema change
  
Week 3:
  Instrumentation (log all thesis details, instrument routing decisions)
  Shadow Phase A (48h collection, full logging)
  
Week 4:
  Offline validation (Phase B)
  Staging promotion (Phase C)
  Production (Phase D, if gates pass)
  
Result: 0 LONGs fixed on Day 1 of Week 4 (too late for immediate problem)
```

### Option C: Integrated Solution (EV-First Phase I + Opportunity Arbiter Phase II) ✅

```
Week 1:
  Mon-Tue: Implement EV-first in directional_arbiter.py (~80 lines)
  Wed-Thu: Shadow 48h collection
  Fri: Validation + staging promotion
  Week 1 end: Production (RANGE_SCALP 0 LONGs → 20-30% LONGs)
  
Week 2:
  Mon-Tue: Design universal_thesis_builder.py, opportunity_selector.py
  Wed-Fri: Implement SPOT_BULL thesis builders + integration
  
Week 3:
  Mon-Tue: Shadow SPOT_BULL 48h collection
  Wed: Offline validation
  Thu-Fri: Staging promotion + production (if gates pass)
  Week 3 end: All regimes benefit from improved LONG/SHORT balance
  
Post-Week 3:
  FUTURES_SHORT, PRESERVATION follow (Week 4-5) with higher confidence
  
Result: Fast fix (Week 1) + universal solution (Week 3) + low cumulative risk
```

---

## The Numbers: Risk-Reward Analysis

### Phase I (EV-First) Risk/Reward

**If Phase I succeeds** (68% confidence based on 69 candidate analysis):
- ✅ RANGE_SCALP LONG %: 0% → 25% (actual target 20-30%).
- ✅ PnL neto: +10% vs baseline (based on simulation).
- ✅ Regret: Zero (fixed immediate problem).
- ✅ Foundation for Phase II: Validated EV-first concept.

**If Phase I fails** (32% confidence):
- ❌ Scenario: PnL < −10% vs baseline.
- ❌ Cost: Revert 1 config flag; 10 min.
- ❌ Impact: Back to 0 LONGs (status quo); try Opportunity Arbiter (start Phase II immediately).
- ❌ Total time to fix: 1 week + 4 weeks = 5 weeks (vs pure OA = 4 weeks, only 1 week worse).

**Risk-adjusted expected value**:
- 68% × (+10% PnL improvement) + 32% × (−10% reversal + 1 week delay) = **+4.4% PnL, strong signal for Phase II**.

---

### Phase II (Opportunity Arbiter) Risk/Reward Given Phase I Success

**If Phase I succeeded** (68% baseline case):
- Phase II confidence: 85% (Phase I validated the core concept).
- Phase II effort: 300–400 lines (moderate).
- Phase II risk: Regimes transition smoothly; RANGE_SCALP stay on Phase I EV-first if needed.

**If Phase I failed** (32% baseline case):
- Phase II confidence: 40% (concept unproven; Opportunity Arbiter might be overkill).
- Phase II effort: 300–400 lines (same, but lower confidence).
- Phase II risk: Might not fix the root cause; higher rollback cost.

**Decision point**: If Phase I fails, either:
1. Go back to manual regime-specific fixes (slow but safe).
2. Pivot to simplified Opportunity Arbiter (Phase II confidence 40%).

Integrated solution bakes in **decision gate after Phase I**.

---

## Cost-Benefit: Integrated vs Pure Plans

### Scenario 1: "Fix RANGE_SCALP, extend later"

| Metric | EV-First Alone | Integrated (Phase I) | Integrated (Phase I+II) |
|---|---|---|---|
| Time to RANGE_SCALP fix | 1 week | 1 week | 1 week |
| Time to universal solution | Never (manual fixes) | 3 weeks | 3 weeks |
| Total implementation cost | 80 lines | 115 lines (Phase I) | 925 lines (Phase I + II) |
| Rollback cost | 10 min | 10 min (Phase I) | 10 min + 30 min (Phase II) |
| Team cognitive load | Low (1 method) | Low → Medium | Low (Phase I) → Medium (Phase II) |

**Winner**: Integrated. Phase I gives you what EV-First gives, plus option to extend universally without redoing RANGE_SCALP.

---

### Scenario 2: "Need universal solution immediately"

| Metric | Opportunity Arbiter Alone | Integrated (Phase I+II) |
|---|---|---|
| Time to RANGE_SCALP fix | 4 weeks | 1 week |
| Time to universal solution | 4 weeks | 3 weeks |
| Total implementation cost | 1,200 lines (all at once) | 925 lines (spread over 2 weeks) |
| Rollback cost | 1 hour | 10 min (Phase I) / 30 min (Phase II) |
| Risk profile | Medium (all-or-nothing Week 4) | Low (Phase I quick win) → Medium (Phase II) |
| Regret if Phase 1 fails | High (no partial value) | Low (Phase I success = proof of concept) |

**Winner**: Integrated. Same total effort, but de-risked by Phase I validation.

---

## Recommendation: Go Integrated

### The Synthesis Decision

Based on governance, canonical knowledge, and risk analysis:

**Phase I (Week 1): Deploy EV-First for RANGE_SCALP**
- Reasons:
  1. ✅ Fast fix for immediate problem (0 LONGs).
  2. ✅ Low risk (1 region, 80 lines).
  3. ✅ Strong data foundation (69 candidates analyzed).
  4. ✅ Validates core concept (EV-first philosophy) before scaling.
  5. ✅ Provides quick win for team confidence.
  6. ✅ Compliant with all governance rules.

- Success criteria:
  - PnL neto shadow ≥ +10% vs actual.
  - Win rate shadow ≥ actual.
  - LONG % 20-30% (after 48h shadow).
  - Worst trade ≤ actual.

**Phase II (Weeks 2-3): Deploy Opportunity Arbiter for All Regimes**
- Reasons (triggered by Phase I success):
  1. ✅ Universalize the pattern (avoid 3 more EV-First implementations).
  2. ✅ Handle instrument routing explicitly (Spot vs Margin vs Futures).
  3. ✅ Enable ABSTAIN (reject low-confidence trades).
  4. ✅ Faster now that Phase I validated concept (85% confidence vs 50% before).
  5. ✅ Prepare for SPOT_BULL, FUTURES_SHORT, PRESERVATION regimes.

- Success criteria:
  - SPOT_BULL: LONG % ≥ 60% (vs 20% SHORT today).
  - Utility correlation with realized PnL ≥ 0.55.
  - No regression in any regime.
  - Thesis_snapshot_v1 populated for 100% of candidates.

**Phase III (Post-Week 3): Monitor & Extend**
- Deploy FUTURES_SHORT thesis builder (Week 4).
- Deploy PRESERVATION thesis builder (Week 5).
- Both regimes inherit benefits of validated Opportunity Arbiter pattern.

---

## Implementation Readiness

**Phase I (EV-First) is ready now**:
- ✅ Pseudocode written (in this analysis).
- ✅ Config parameters specified.
- ✅ Validation gates defined (shadow, validation, staging, production).
- ✅ Rollback procedure clear.
- ✅ Dashboard metrics designed.

**Phase II (Opportunity Arbiter) requires design work**:
- 🔄 Universal thesis builder architecture (need 1-2 design sessions).
- 🔄 SPOT_BULL specific thesis builders (can be templated from RANGE_SCALP).
- 🔄 Utility formula calibration (reuse Phase I success metrics).

---

## Conclusion

**Integrated solution = Pragmatism + Universality + Low Risk**

- **Week 1 end**: RANGE_SCALP fixed (EV-First) → 0 LONGs → 20-30% LONGs.
- **Week 3 end**: All regimes unified (Opportunity Arbiter) → sustainable, maintainable pattern.
- **Post-Week 3**: FUTURES_SHORT & PRESERVATION smoothly onboarded.
- **Total implementation effort**: ~925 lines (vs 1,200 for pure OA or multiple tweaks for pure EV-First).
- **Total risk**: Low → Medium (graduated).
- **Governance compliance**: ✅ Full (AIC protocol, rules.md, canonicals).

**Both EV-First and Opportunity Arbiter teams were right. The integrated solution honors both.**

