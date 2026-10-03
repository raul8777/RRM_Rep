---
title: "Opportunity Arbiter — One-Minute Reference"
type: cheatsheet
status: historical
created_at: 2026-09-11
superseded_by: "calibracion_entradas-a20ab8-v3.md s135"
---

# Opportunity Arbiter Quick Reference

## Problem → Solution

| Problem | Why | Solution |
|---|---|---|
| 11/11 trades are SHORT, no LONGs | LONG/SHORT decided early, pre-chosen side locked in | Both theses evaluated under identical snapshot; pick the best |
| LONG/SHORT see different data | Evaluated at different times, different screeners | Frozen snapshot: all theses get same market data |
| Asymmetric gates | SHORT: "at resistance" vs LONG: "not overbought" | Unified gates; both sides pass/fail together |

---

## Architecture Layers

```
┌─────────────────────────────────────────────────────────┐
│ Layer 1: Frozen Snapshot (Symbol-wide)                 │
│   price, atr, rsi, bb, support, resistance, vol_spike  │
└─────────────────────────────────────────────────────────┘
                            ↓
    ┌───────────────────────┬───────────────────────┐
    ↓                       ↓                       ↓
┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│ LONG Spot   │      │ SHORT Marg  │      │ LONG Fut    │
│ Utility:    │      │ Utility:    │      │ Utility:    │
│ +0.35%      │      │ -0.12%      │      │ +0.18%      │
└─────────────┘      └─────────────┘      └─────────────┘
    ↓                   ↓                       ↓
    └───────────────────┬───────────────────────┘
                        ↓
            ┌─────────────────────────┐
            │ Max Utility = LONG Spot │
            │ +0.35% > Threshold 0.5% │
            └─────────────────────────┘
                        ↓
            ┌─────────────────────────┐
            │ OPEN: LONG Spot         │
            │ (Persist thesis_v1)     │
            └─────────────────────────┘
```

---

## Utility Formula (Normalized)

$$
U = P(\text{success}) \times R_{\text{net}} - (1-P) \times L_{\text{net}} - C_{\text{round-trip}}
$$

**Applied to all (side, instrument) pairs equally**:
- SPOT: commission 0.1%, slippage 2-5bps
- SPOT_MARGIN: commission 0.1%, slippage 2-5bps, leverage 2x
- FUTURES: commission 0.04%, slippage 1-3bps, funding rate, leverage 2x

**Scaled by regime confidence**: $U_{\text{adj}} = U \times \text{regime\_confidence}$

---

## Regime × Instrument × Side Matrix (Simplified)

| Regime | LONG Spot | LONG Margin | LONG Fut | SHORT Spot | SHORT Margin | SHORT Fut |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| SPOT_BULL | ✓ | ✓ | ✓ | ✗ | (~) | ✗ |
| RANGE_SCALP | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| FUTURES_SHORT | ✗ | ✗ | (~) | ✗ | ✓ | ✓ |
| PRESERVATION | ✓ (narrative only) | ✗ | ✗ | ✗ | ✗ | ✗ |

Legend: ✓ = enabled, ✗ = disabled, (~) = emergent/rare

---

## Expected Outcomes

### Today
- LONG: 5-10% of trades
- SHORT: 90-95%
- All SHORTs in RANGE_SCALP (only regime active)

### With Arbiter (Week 3+)
- **SPOT_BULL**: 70-80% LONG, 20-30% SHORT
- **RANGE_SCALP**: 45-55% LONG, 45-55% SHORT ← Major change
- **FUTURES_SHORT**: 5-15% LONG, 85-95% SHORT
- **PRESERVATION**: 100% LONG

---

## Phase Timeline

| Phase | Weeks | Activity | Risk | Behavior Change |
|---|---|---|---|---|
| **A** | 1-2 | Build theses, log both sides | None | None (instrumentation only) |
| **B** | 2-3 | Shadow selection (optimal vs current) | None | None (diagnostic only) |
| **C** | 3-4 | Canary 10% → 50% → 100% | Medium | Yes (production trades affected) |
| **D** | 5+ | Full production, optimize | Low | Yes (fully enabled) |

---

## Rollback Procedure

**Instant (Phase C emergency)**:
```bash
docker compose restart app
# Reverts to current selection logic (config default)
```

**Explicit (Phase D)**:
```
PATCH /config/kelly {
  "opportunity_arbiter_enabled": False
}
docker compose restart app
```

---

## Key Changes Required

| File | Change | Priority |
|---|---|---|
| `run_trades.py` | Call thesis builder instead of pre-choosing side | 🔴 Critical |
| `lib/opportunity_thesis.py` | New file: thesis builders for 4 regimes | 🔴 Critical |
| `lib/utility_score.py` | New file: utility calculation | 🔴 Critical |
| `ready_candidates` (DB) | Add `thesis_snapshot_v1 JSONB` | 🟡 High |
| `app.py` | Extend `/metrics/shadow-readiness` with utility stats | 🟡 High |
| `config.py` | Add `opportunity_arbiter` section | 🟢 Medium |

---

## KPI Dashboard (Real-time)

```
┌─────────────────────────────────┐
│ Opportunity Arbiter Readiness   │
├─────────────────────────────────┤
│ ✓ LONG %:  5.3% (baseline)      │
│   → Expected: 35-50% post-fix   │
│                                 │
│ Thesis Coverage:                │
│   ✓ Both sides built: 98.7%     │
│   ✓ Utility computed: 98.7%     │
│   ✓ Audit trail: 98.7%          │
│                                 │
│ Suboptimal Selection (shadow):  │
│   Today: 23.1% (expect 15-30%)  │
│   Avg utility loss: -0.12%      │
│                                 │
│ Phase: A (Instrumentation)      │
│ Ready for B: YES ✓              │
└─────────────────────────────────┘
```

---

## Success = Regime-Aware Side Distribution

```
     SPOT_BULL          RANGE_SCALP      FUTURES_SHORT      PRESERVATION
         
    ┌─────────┐        ┌─────────┐        ┌─────────┐        ┌─────────┐
    │ 75% LONG│        │ 50% LONG│        │ 10% LONG│        │100% LONG│
    │ 25% SHO │        │ 50% SHO │        │ 90% SHO │        │  0% SHO │
    └─────────┘        └─────────┘        └─────────┘        └─────────┘
    Bull regime        Mean reversion    Bear downturn       Narrative
    favors LONG        is symmetric      favors SHORT        fixed LONG
```

**Today (broken)**: All regimes look like RANGE_SCALP-only (90% SHORT).

---

## Risk ↔ Reward

| Outcome | Probability | Impact | Mitigation |
|---|---|---|---|
| **Success** (KPIs improve) | 70% | +$150K PnL over 3mo | Invest full phase D. |
| **Neutral** (KPIs flat) | 20% | Status quo | Keep for regime clarity. |
| **Regression** (KPIs drop) | 10% | -5% PnL (temporary) | Rollback, tune, retry. |

---

## One Question: "Why Not Just Quota?"

**Quota Rule** ("ensure 30% LONGs"):
- ✗ Forces bad LONGs into trades.
- ✗ Wastes capital on suboptimal picks.
- ✗ Still asymmetric (different LONG/SHORT gates).

**Opportunity Arbiter**:
- ✓ Picks best side per symbol per regime.
- ✓ LONGS naturally emerge when better.
- ✓ Side distribution follows market, not config.

Result: RANGE_SCALP will be ~50/50 naturally, SPOT_BULL ~70/30 naturally, because that's what the data supports—not because a config forced it.

---

## Documents

| Doc | Purpose | Audience |
|---|---|---|
| `opportunity_arbiter_multiregime_v1.md` | Full spec (§1-14, 550 lines) | Engineers, architects |
| `opportunity_arbiter_root_cause_analysis.md` | Why 0 LONGs + fixes (§1-7, 300 lines) | Product, traders, engineers |
| `opportunity_arbiter_executive_summary.md` | GO/NO-GO + phases (§1-8, 300 lines) | Leadership, decision makers |
| *This cheat sheet* | One-minute overview | Everyone |

