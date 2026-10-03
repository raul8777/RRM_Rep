# Universal Entry Selection Formula Benchmark

**Canonical identifier:** `universal_entry_selection_formula_benchmark`

## Purpose

Determine which formula best selects symbols for opening attempts when every formula receives the same candidate tape, executable contracts, prices, costs, slots and portfolio state.

This benchmark optimizes candidate selection only. It does not recalibrate sizing, Kelly, leverage, lifecycle, exits, transfers or BNB.

## Absolute Base

Use the current `RANGE_SCALP` entry contract as the universal behavior for every mode:

- identical candidate validity;
- fresh bilateral LONG/SHORT contract;
- EV, probability, cost and risk gates;
- identical selection priority;
- identical portfolio selection;
- identical abstention behavior.

Regimes provide features and market context only. They must not select a different formula or bypass a universal gate. A zero-candidate result in a defensive mode must come from eligibility evidence, not from a selector bypass.

## Trading Philosophies and Entry Generators

Formula families answer **how to rank/select a fixed candidate tape**. Trading philosophies answer **how a candidate opportunity and its thesis are generated**. These are separate experimental axes. Every philosophy below produces the same canonical opportunity contract shape, passes the same RANGE_SCALP-derived gates and is available to every regime; regime data can inform features but cannot hard-route a mode to a different philosophy.

Compare multiple predeclared variants inside each evaluable family. The variants below are experiment arms, not assumed winners. Tune parameters only on each training fold. Do not mix exit/lifecycle policy into a philosophy comparison.

| Philosophy family | Entry hypothesis | Variants to compare |
|---|---|---|
| Mean reversion / range | A dislocation reverts while structure remains valid. | S/R bounce or rejection; Bollinger/normalized-distance extreme; RSI/stochastic exhaustion plus structure; VWAP/rolling-mean reversion; volatility-scaled deviation with MAE veto. |
| Time-series momentum / continuation | Directional persistence continues over the selected horizon. | 1h trend; 4h trend; multi-horizon momentum agreement; EMA/MACD slope plus return persistence; pullback/re-entry instead of impulse chase. |
| Breakout / volatility expansion | A confirmed boundary break expands into a new range. | Close beyond S/R; breakout plus retest/hold; ATR/bandwidth compression then expansion; volume-confirmed breakout; volatility-adjusted channel breakout. |
| Cross-sectional / relative strength | Prefer symbols outperforming the market or peers. | Relative return vs BTC; sector-relative strength; cross-sectional momentum rank; breadth/leadership confirmation; beta-adjusted residual strength. |
| Carry / funding-aware | Carry/funding changes expected contract utility or supports a directional thesis. | Funding level; funding change/acceleration; funding plus open-interest confirmation; carry-adjusted directional EV; spot-perpetual basis dislocation. Include funding exactly once in contract economics. |
| Volatility / risk-premium state | Prioritize setups whose expected move compensates for structural risk and cost. | ATR-normalized move; volatility-cluster transition; low-vol compression vs high-vol expansion; volatility-scaled distance to level. This changes candidate score, not sizing. |
| Statistical arbitrage / relative value | A relative-price spread is expected to converge. | Rolling beta-spread z-score; cointegration/error-correction; correlation-residual deviation; sector-neutral residual. Evaluate only with synchronized point-in-time data, hedge ratio, both executable legs, costs and fill assumptions. If one is absent, keep the family `obtainable_pending` while checking archives, reconstructing synchronized candles and attempting prospective two-leg capture; mark `not_evaluable_historical` only after those paths are exhausted. |
| Microstructure / order flow | Short-horizon imbalance/liquidity predicts executable movement. | Order-book imbalance; signed trade-flow imbalance; spread/depth-adjusted pressure; imbalance persistence/reversal; liquidity shock/recovery. Evaluate only with archived point-in-time books/trades and latency/fill evidence. |

News, on-chain and scheduled-event ideas are optional families only when timestamped point-in-time history exists with revision/survivorship controls. Never use today's metadata as if it were known at entry time. Families without confirmed local data start as `obtainable_pending`; search archives/providers, attempt valid reconstruction and consider prospective capture before they may be classified `not_evaluable_historical`. They must never be silently simulated or treated as losing strategies.

### Philosophy Comparison Protocol

1. Compare philosophy variants first under one fixed baseline selector, candidate capacity, bilateral contract logic, costs, risk gates and exits. Run every philosophy across every regime and report regime strata; do not assign each philosophy to a single mode.
2. Preserve raw opportunity stream, thesis, timestamp, feature availability, four venue contracts, abstentions and rejection reasons.
3. Control candidate-generation frequency: report raw opportunity coverage and compare at equal candidate budgets or matched coverage so a high-frequency generator does not win by emitting more rows.
4. Compare selection formulas separately on the same frozen candidate stream. Formula tests may not regenerate candidates or change side/thesis/venue.
5. Cross only shortlisted philosophy finalists with formula finalists in a later, small, multiplicity-corrected confirmatory phase.
6. Require each variant to define its falsifiable thesis, point-in-time features, invalidation, horizon and executable-contract mapping. If it requires changing sizing/exits or cannot be represented by the shared contract, classify it as a separate experiment.

In addition to the common benchmark metrics, report opportunity-generation coverage, candidate rate per symbol-hour, duplicate/recheck rate, thesis confirmation rate, executable-contract rate, time from source event to entry, and EV of rejected alternatives. Pairs additionally report hedge residual and both-leg fill completeness; microstructure arms report data freshness, spread/depth regime and latency-adjusted fill quality.

## Formula Families

1. `baseline_current_universal`: current behavior normalized to one policy, including its real priority and gates, as the control.
2. Additive calibrated utility/ranking.
3. Multiplicative quality and bottleneck score.
4. Executable-contract EV-first.
5. Calibrated predictor EV.
6. Robust lower-confidence EV with uncertainty penalty.
7. Pareto nondominated frontier.
8. Constraint/threshold outranking, such as ELECTRE-like or lexicographic vetoes.
9. Marginal portfolio utility with correlation, concentration and opportunity-cost penalties.
10. Calibrated QUBO with learned linear utility and quadratic portfolio penalties.
11. Adaptive formula selection only as a later OOS-gated phase. It must never be trained and judged on the same outcome window.

QUBO is one portfolio-selection family among several, not the definition of the entry strategy.

## Formula Definitions

### Executable-contract EV

For candidate $i$:

$$
EV_i = p_iG_i - (1-p_i)L_i - C_i
$$

Where $p_i$ is calibrated success probability, $G_i$ is net target gain, $L_i$ is stop loss and $C_i$ contains commissions, slippage, funding and execution costs.

### Additive Utility

$$
U_i = \alpha EV_i + \beta P_i + \gamma Q_i + \delta Liquidity_i + \phi Freshness_i - \rho Risk_i - \eta Slippage_i - \xi Uncertainty_i
$$

### Robust EV

$$
EV^-_i = EV_i - z\sigma_{EV_i}
$$

Uncertainty includes sparse samples, model disagreement, missing features and MFE/MAE dispersion.

### Portfolio Utility

$$
Q(S) = \sum_{i\in S}U_i - \lambda_c\sum_{i,j\in S}Corr_{ij} - \lambda_r\sum_{i\in S}Risk_i - \lambda_b\sum_{i\in S}BucketPenalty_i - \lambda_f\sum_{i\in S}FreshnessPenalty_i
$$

The QUBO variant must compare exact `brute`/`greedy` solutions on small candidate sets against reproducible SQA solutions.

## Calibration Scope

Calibrate:

- EV weight;
- calibrated probability weight;
- quality weight;
- predictor/geometric agreement;
- liquidity;
- freshness;
- risk;
- slippage;
- uncertainty;
- probability calibration through isotonic/logistic calibration or Bayesian shrinkage;
- confidence level and uncertainty penalty;
- missing-feature policy;
- disagreement penalty;
- QUBO correlation, risk and size weights;
- bucket concentration penalty;
- marginal opportunity cost;
- cardinality treatment;
- minimum EV, probability, liquidity, quality and freshness thresholds;
- abstention conditions.

Keep sizing, Kelly, leverage, lifecycle, exits, transfers, BNB and order execution frozen.

## Comparison Protocol

Freeze the following before comparison:

- candidate tape;
- code and configuration hashes;
- model and threshold hashes;
- universe;
- contract construction;
- commissions and costs;
- fill model;
- available slots;
- starting portfolio.

Run every formula against exactly the same candidates and executable contracts. Only the selection formula may vary.

Use intrabar 1m/5m replay where available. When TP and SL occur in the same candle, use a conservative interpretation and run a sensitivity alternative.

Use:

- chronological development and validation windows;
- walk-forward OOS;
- changepoint cohorts;
- the 3y/60000 baseline;
- paired formula differences;
- bootstrap confidence intervals;
- minimum sample gates.

`n >= 30` is the absolute floor. `n >= 50` is preferred before recommending a formula. No recommendation may rely on one short window or raw PnL alone.

## Metrics

### Primary

- net EV per selected opportunity;
- net PnL;
- profit factor;
- maximum drawdown;
- maximum loss;
- risk-adjusted return;
- PnL per available slot.

### Secondary

- win rate;
- Brier score and calibration error;
- A/B/C profile quality;
- false positives;
- abstention rate;
- coverage;
- MFE/MAE;
- time to TP, SL or expiry;
- slippage;
- total costs;
- concentration;
- pairwise correlation;
- opportunity capture.

Stratify every result by venue, side, regime, profile, category and liquidity.

## Acceptance Criteria

A formula can be recommended only if it:

- improves net EV or is statistically non-inferior to baseline;
- does not materially degrade drawdown or maximum loss;
- preserves or improves probability calibration;
- remains stable across time, regimes and venues;
- does not win only by lowering gates or opening more trades;
- has sufficient sample size and confidence intervals;
- does not depend on one symbol, category or short market segment.

Existing canonical quality gates remain reference controls: positive net PnL, `PF >= 1.15`, minimum coverage, no worse drawdown, high feature completeness and sufficient sample.

The valid conclusion may be that no alternative beats baseline.

## Failure Controls

Prevent:

- future leakage;
- candidate-tape drift;
- per-cycle min-max score distortion;
- QUBO stochastic instability;
- fabricated values for missing data;
- TP/SL ambiguity bias;
- regime-specific hidden bypasses;
- confusing more trades with better performance;
- selection formulas changing sizing or lifecycle behavior.

QUBO must use fixed seeds and report solver diagnostics. Small candidate sets must be checked with exact brute-force or deterministic greedy solutions. Persist all rankings, selected candidates, excluded near-winners, rejection reasons and formula diagnostics.

## Implementation Order

1. Audit point-in-time features, candidate lineage, intrabar data, fills and per-philosophy data requirements; mark unverified families `obtainable_pending` and follow the acquisition protocol before any `not_evaluable_historical` decision.
2. Extract a universal opportunity/contract record from `entry_selection_candidates`, `ready_candidates`, arbitration and contract diagnostics.
3. Add pure interfaces for philosophy generators and selection formulas, with no live side effects; keep their causal comparisons separate.
4. Freeze multiple train-tuned variants per evaluable philosophy and compare generators under the baseline selector.
5. Freeze candidate tapes for formula comparisons and compare formula families without changing candidate generation.
6. Cross only shortlisted philosophy/formula pairs in a separate, multiplicity-corrected confirmatory phase.
7. Add a common replay harness and reports for rankings, rejected near-winners, abstention value and selected portfolio sets.
8. Run smoke tests, then development/train, held-out validation and temporal walk-forward OOS.
9. Add a stable v1 to active Staging execution on Binance Demo/Testnet after offline readiness gates pass; use a bounded, audited canary, not a no-submit/shadow phase.
10. Do not change Production entry behavior. Staging is the operational sandbox used to test the accepted hypothesis before any separate Production approval.

## Relevant Code Surfaces

- `entry_decision.py`, `staged_entry.py`, `run_trades.py`: universal eligibility, ranking inputs and candidate lineage.
- `portfolio_selector.py`: QUBO and alternate portfolio-selection families.
- `opportunity_contract.py`, `directional_arbiter.py`, `pnl_predictor.py`, `entry_profile_gate.py`: executable contract, predictor, probability and quality evidence.
- `Dev/benchmarks` and `historical_data`: frozen tapes, manifests, OHLCV and existing SuperMimic evidence.
- `tests/`: formula invariants, deterministic ranking, no-gate-bypass, same-tape comparison and OOS artifact checks.

## Expected Deliverables

- formula-policy interface;
- pure formula implementations;
- common replay harness;
- frozen manifests;
- per-formula reports;
- paired and OOS statistics;
- sensitivity tables;
- rejection reasons;
- QUBO solver diagnostics;
- final recommendation with explicit acceptance or rejection rationale.

The final recommendation must identify whether the winner is EV-first, predictor-based, uncertainty-aware, Pareto/constraint-based, marginal-portfolio, QUBO, or baseline. No formula is selected from intuition or one short window.

## Pre-Benchmark Readiness Gates

### Active Staging Pilot Contract

Staging is the operational sandbox closest to Production. It is expected to submit real API orders to Binance Spot Demo and USD-M Futures Testnet with `BINANCE_USE_TESTNET=true`, `DRY_RUN=false`, and `ALLOW_REAL_TRADING=true`; this is not a no-submit/shadow phase. Production remains out of scope and must never be a target of Staging credentials or endpoints. Spot Demo and Futures Testnet balances are independent; Spot Margin and cross-wallet SAPI transfers remain disabled.

The pilot has two separate acceptance layers:

1. **Offline evidence gate:** freeze a development tape, reconstruct opportunity identity/contracts, prove simulator parity and define the baseline/challenger comparison. The validation windows remain untouched until the pilot configuration is frozen.
2. **Active Testnet canary:** deploy the frozen v1 to the normal Staging execution path and verify candidate -> bilateral contract -> order acknowledgement -> confirmed fill -> protective orders -> close -> persisted lineage/audit on each enabled venue. A successful canary proves operational functionality, not profitability or formula superiority.

Before the canary, `/status` and `/control/status` must identify healthy Staging with no active entry pause, Spot/Futures sandbox endpoints must be verified, both open-position routes must report a clean or explicitly isolated canary state, and no cross-wallet transfer may be required. After the canary, keep v1 active on Testnet while the predeclared full sample accrues; Production is not changed.

The benchmark must not start until the following gates are closed and verified. Existing Devin review notes below remain the source review; this section consolidates their requirements into an implementation order and records current code evidence.

### Gate 1 — Single Entry Authority and Pause Safety

- Persisted `pause_new_entries` must be checked fail-closed on the normal cycle and at the FastPath execution boundary. A DB read error, missing DB client or malformed pause state must not authorize an entry.
- Recheck immediately before candidate execution; a pause activated while the watcher is preparing candidates must block the FastPath.
- The watcher must only record `PAUSED_DB` when pause is actually active. Never stamp an unpaused candidate with a false pause rejection.
- Verify with tests for active pause, expired pause, unavailable DB and pause changing during candidate preparation. Acceptance: zero order-submit calls while paused, with an auditable reason.

**Current evidence:** the watcher reads `_is_new_entries_paused`; the FastPath checks it at entry and before each order-submit branch. The watcher no longer writes `PAUSED_DB` unconditionally. Absence of a pause row means unpaused, consistent with the resume endpoint deleting the row; DB errors, malformed values and failed/raced TTL resume remain blocked. Direct unit tests cover those cases. A threaded watcher race test is still required before closing the gate.

The pause helper now treats an absent row as unpaused (the API's resume operation deletes the row), rejects malformed values fail-closed, and atomically deletes an expired pause with its audit event. FastPath rechecks pause at entry and immediately before each route submit. Direct unit tests cover absent row, malformed state, DB error, expired-pause atomic resume, and active-pause FastPath refusal.

### Gate 2 — Current QUBO Authority on Every Entry Route

- Every staged entry route must require a fresh, regime-matching `qubo_cycle_id` and selected symbol set.
- Filter candidates by the authoritative set. Missing, empty, stale or unreadable authority means abstain, not `None` as an implicit unfiltered fallback.
- Verify both normal-cycle and intra-cycle FastPath callsites and stale validated candidates.
- Record the authority cycle/hash used by each attempted entry.

**Current evidence:** `get_qubo_authority_snapshot` verifies regime, freshness, nonempty `cycle_id`, and a SHA-256 hash of the selected symbols. The cycle writer stores that hash. Normal-cycle staged queue, no-signal fallback and intra-cycle watcher pass the exact cycle/hash into `get_openable_candidates`; the SQL filters membership, selected ready data records the authority, and FastPath refuses rows without provenance. Direct tests cover valid/missing/mismatched snapshots and membership. The historical Devin H2 statement that the SQL ignored this parameter was stale; the original filter existed, and this work adds provenance enforcement.

### Gate 3 — Persist the Full Opportunity Contract Set

Before any benchmark tape is frozen, persist for each unique opportunity all applicable alternatives: Spot LONG, Futures LONG, Spot Margin SHORT and Futures SHORT. Persist inapplicable/unavailable venues too, with explicit reasons such as disabled, filter failure, insufficient balance, borrow unavailable or stale market data. Store each contract's thesis, side, venue, prices, geometry, probability, gross/net EV, costs, max loss, sizing basis, exchange-filter snapshot and executability.

The selector's action unit is an opportunity event with a fixed bilateral arbitration result. The directional/router fix must first produce the one canonical executable contract per event using the same RANGE_SCALP rules; the benchmark formula then ranks those event winners for symbol/slot selection. All four alternatives are retained for audit and rejected-set replay, but benchmark formulas do not change side, thesis or venue. This keeps the known direction-generation defect separate from symbol-selection causality.

### Gate 4 — Reproducible Baseline and Opportunity Identity

- Freeze explicit `code_hash`, effective non-secret `config_hash`, model/threshold hashes, `entry_generation`, universe, cost model, exchange filter snapshot and QUBO policy/solver seed.
- Assign stable `opportunity_event_id` and `dedupe_cluster_id`. Do not count raw cycle rows as independent observations.
- Deduplicate repeated signal rows into opportunity clusters while preserving rechecks/attempts as lineage. The measured 1,154 rows / 161 unique entries implies material row inflation; a `(symbol, minute)` key is only a provisional fallback, not the canonical identity where thesis/context/expiry differ.
- Keep `thesis_type`, `venue_executable_at_time`, `ev_horizon`, cycle/time and entry generation as required replay fields.

### Gate 5 — Abstention Value and Comparable Coverage

Report rejected-set counterfactual value (`EV_of_abstained` and realized rejected-set outcomes when observable), opportunity cost, selected coverage, slot utilization, and risk-adjusted metrics at matched coverage. A policy cannot pass by abstaining on everything and claiming zero drawdown or undefined/infinite per-selected-trade EV. Report both unconstrained policy results and comparisons at a predeclared minimum coverage/slot-utilization range.

### Gate 6 — Leakage, Multiple Comparisons and Rollback

- Split tuning and evaluation by chronological folds. Calibrate score weights, probability calibration, thresholds, normalization and formula choice on train only; do not inspect or tune against the evaluation fold.
- Group all rows from the same `opportunity_event_id`/`dedupe_cluster_id` in one fold. Do not let symbol/event repeats cross train and evaluation in a way that leaks outcomes.
- Reduce the first benchmark to five priority families: universal baseline, executable-contract EV, calibrated predictor EV, robust lower-confidence EV, and calibrated QUBO. Add Pareto/constraint/marginal-portfolio families only if independent sample power is sufficient.
- Use at least 50 independent opportunity clusters per family-vs-baseline comparison as the preferred minimum; 30 is only a pilot floor and cannot alone authorize promotion. Correct paired multiple comparisons (e.g. Holm) and report effective independent sample size.
- Define the rollback gate before the run: if the OOS paired-difference confidence interval fails the predeclared non-inferiority margin, or calibration/coverage/drawdown gates fail, select the frozen baseline automatically. A CI crossing zero alone is not universally a failure if non-inferiority was predeclared; acceptance must test the stated margin, not post-hoc significance.

### Gate 7 — Predictor Probability Calibration

- Reproduce Devin's reported high-confidence/low-realization cohort from raw decision and outcome records before calling the predictor corrupt: match `decision_id`/opportunity cluster, thesis, side, venue, prediction timestamp, horizon and executable label; separate stale evidence, label mismatch, rejected/nonfilled contracts and true forecasting errors.
- Fit Brier/reliability/isotonic or shrinkage calibration only on each train fold; test on its untouched chronological validation/OOS fold, clustered by opportunity event. Report calibration by thesis, side, venue and horizon plus confidence intervals and minimum independent cluster counts.
- EV families may use only calibrated probabilities or a declared train-fold base-rate/shrinkage estimate. If the probability is not calibrated for a cohort, that EV arm is `not_evaluable_pending_calibration`; non-EV arms may still run. Do not pass raw `p_i` through five EV families and treat their correlated results as independent evidence.

### Gate 8 — Outcome-Simulator Parity

- Share the corrected Entry SimulationRunner's fixed contract outcome/fill resolver for TP-first, SL-first, ambiguous same-candle touches, gap-through, time horizon/expiry, fees, slippage and funding. Reuse the outcome mechanics, not lifecycle management or exit-decision policy.
- Differential-test replay vs corrected simulator on deterministic fixtures and actual archived candle snippets. Same input contracts/data/costs must yield identical labels and net PnL within declared rounding tolerance.
- If a common resolver cannot be reused, implement one pure resolver and prove equivalence before either baseline or candidate formulas are scored. Do not compare against a baseline generated under different TP/SL or time-stop semantics.

### Prerequisite Implementation Order

1. Close and test pause/FastPath authority; correct stale audit labeling.
2. Verify QUBO authority on every caller and fail-closed stale/missing behavior.
3. Fix and validate bilateral thesis/venue generation separately from symbol ranking; the measured directional inversion is not a selector-formula result.
4. Persist the complete contract set per opportunity and exchange executability at decision time.
5. Add stable event dedupe identity, `entry_generation`, and code/config/model/cost/filter hashes.
6. Build train-only calibration, held-out replay, abstention counterfactual and multiplicity-corrected reports.
7. Close gates 1-8, complete the data-source acquisition attempts, and freeze the first candidate tape only after their evidence/tests pass; then run the five-family pilot.

Do not claim the benchmark is ready, freeze a tape, or compare formulas until every prerequisite has an owner, regression test/evidence and a recorded pass status. Code changes closing these gates must not tune formula parameters or alter lifecycle/sizing/production.

### Current Readiness Status — 2026-09-29

| Gate | Status | Evidence and remaining work |
|---|---|---|
| Pause DB/FastPath authority | **Implemented; threaded race proof pending** | Missing pause row is unpaused; malformed state/read errors fail closed; TTL delete+audit are atomic. FastPath checks at entry and immediately before each submit. Direct tests pass. Still add a watcher integration test that activates pause during fresh arbitration and proves zero submits. |
| QUBO authority membership/provenance | **Implemented; runtime verification pending** | QUBO authority stores `cycle_id + symbols_hash`; reader checks regime/freshness/hash. Normal staged queue, fallback and watcher pass the full snapshot; candidate filtering accepts only a subset of its authorized universe; FastPath rechecks that persisted cycle/hash is current before submit. Selected-candidate JSON stores the exact QUBO authority. Writer-reader round-trip, subset membership and stale/missing-authority tests pass. Deploy only Staging and verify runtime before using as data. |
| Full four-venue contract evidence | **Implemented locally; historical Staging tape incomplete** | New `all_venue_contracts` records SPOT LONG, FUTURES LONG, FUTURES SHORT and SPOT MARGIN SHORT, availability/reason and contract validation evidence; local persistence tests pass. Read-only Staging audit found 89,835 candidate rows but zero with `all_venue_contracts`; only 2,326 contain directional arbitration/venue-list payloads. Reconstruct the full alternatives from point-in-time inputs and pinned code where possible, otherwise collect new snapshots. Do not imply the older tape already contains all four. |
| Baseline/model identity | **Implemented locally; historical Staging tape partial** | Composite generation hashes explicit repo-relative selection modules, effective non-secret settings, model/threshold file contents, plus each prediction's learned-weight content hash/source/regime. Local tests pass. Staging contains code/config/model hash fields in only 2,179 of 89,835 candidate rows; historical identity is incomplete. Freeze hashes, QUBO policy/seed and exchange-filter snapshots in the next tape manifest; label unrecoverable legacy rows `unknown_legacy`, never infer hashes. |
| Exchange executability evidence | **Core price/lot/notional filters implemented; venue/account preflight remains** | Spot/Futures record raw `exchangeInfo`; validated snapshots require `LOT_SIZE`, `MARKET_LOT_SIZE` including min/max/step, `PRICE_FILTER` and min notional. Contract quantity floors to the MARKET step before recomputing notional/margin/risk; explicit empty/incomplete snapshots fail closed. Dynamic percent-price bands, order-count/account limits and Margin borrow/collateral are not modeled by this pure contract and must remain venue preflight gates or be marked unavailable in replay. |
| Opportunity event and dedupe | **Instrumented; historical reconstruction remains blocking** | New payload stores signal/ready IDs, decision ID, cycle/capture/source timestamps and horizon. Staging has 89,835 candidate rows, 7,852 cycles and 285 symbols, but candidate JSON contains no source timestamp or signal/ready ID key. The DB does contain 1,107,947 `ready_candidate_checks` across 7,844 candidates and 6,468 cycles, plus cycle start/check timestamps; use these to attempt auditable joins, not to assume equivalence. Re-run the known 1,154-row cohort and validate the ~161 clusters before estimating effective sample size. |
| Thesis/side selection boundary | **Separate directional prerequisite** | Bilateral arbitration generates the fixed contract winner before formula ranking. The formulas do not change side/thesis/venue. Before comparing formulas, separately verify bilateral thesis generation is fresh/symmetric; the reported directional inversion is not evidence that any ranking formula failed. |
| Abstention counterfactual | **Evidence persisted; replay metric open** | Rejections retain stage, reason, arbitration, all contracts, per-venue expected net PnL, decision ID and generation. Rejected opportunities still need intrabar realized counterfactual outcomes, opportunity-cost aggregation and matched-coverage comparisons. |
| Train/OOS, multiplicity, rollback | **Open** | Need chronological grouped folds, train-only calibration, multiplicity correction and predeclared non-inferiority/automatic baseline fallback. |
| Predictor probability calibration | **Open blocker for EV families** | Read-only audit of 500 recent unique closed positions linked all 500 to ready candidates; 499 have a pre-entry probability/EV snapshot. No explicit EV horizon was present in those snapshots, and hashes/venue lineage remain incomplete. Reproduce the `p≈0.80` / ~3% claim with matched point-in-time labels; recover/define the horizon, calibrate on train only, and validate by thesis/side/venue/horizon before predictor-driven formula comparisons. |
| Outcome-simulator parity | **Open blocker for all outcome comparisons** | Differential-test the common contract TP/SL/time-horizon/cost resolver against the corrected SimulationRunner. No formula scores until labels and net PnL are semantically identical. |
| Historical feature acquisition | **1m OHLCV and USD-M metrics matrix attempted; bookDepth/flow/context pending** | Across 24 calibration windows × 39 opportunity symbols, the venue-aware 1m manifest contains 14,281 unique daily Spot/USD-M targets: 14,058 full-day archives passed official SHA-256/cadence checks, 222 USD-M targets returned 404/`INVALID_SYMBOL`, and one Spot day is partial (`TONUSDT`, 2026-06-30, 180/1,440 bars through 02:59 UTC). For USD-M `metrics`, all 7,583 contract-days were attempted: 7,353 full 288-row/5m days passed checksum/schema validation, 222 returned 404, and 8 TONUSDT days contain only 1–109 rows after contract settling. There are no failed or unreported targets and no `.part` files; 45 USD-M pre-listing dates are excluded using official onboard dates. For `bookDepth`, 182/7,583 contract-days now pass official checksum and full-day coverage, with 2,880 snapshots/day at ten percentage bands/snapshot; 7,401 remain (~3.3 GB estimated from observed files). Remaining feature work: finish `bookDepth`, acquire `aggTrades`, venue-specific Futures 5m/1h context, funding and point-in-time external features; true L2 and dominance remain separate source/reconstruction work. Keep 222 OHLCV and 222 metrics 404s `obtainable_pending` until historical listings, aliases and alternate sources are exhausted; keep 8 partial metric days `reconstructable_with_limits`. |
| Replay resource budget | **Open before full variants** | Pilot wall time/RSS/storage at 1/2/4 workers; use 1–2 initially, cap at 6, checkpoint/resume and persist incrementally; verify no OOM before expanding philosophy×variant folds. |
| Staging runtime identity | **Preflight passed; canary not started** | `crypto-agent-app_STG` is running with isolated DB/Redis; `/status` reports `environment=staging`, `/control/status` reports `pause_new_entries.active=false`, and effective flags are `BINANCE_USE_TESTNET=true`, `DRY_RUN=false`, `ALLOW_REAL_TRADING=true`, `SPOT_MARGIN_SHORT_ENABLED=false`. Spot/Futures open-position endpoints currently report count 0. Recheck immediately before the canary; no order has been submitted by this task. |
| Benchmark/tape freeze | **Not started** | No candidate tape has been frozen and no formula benchmark run. The next offline gate is lineage/dedupe plus simulator parity, followed by an active Demo/Testnet canary; no no-submit Staging phase is required. |

## Data Acquisition Before Declaring Insufficient

Missing data is **not** immediately classified `insufficient`. For each feature/family, first perform and record a bounded but exhaustive acquisition/reconstruction pass. Use the states `available`, `obtainable_pending`, `reconstructable_with_limits`, `prospective_only`, and only finally `not_evaluable_historical`. A family may be unavailable for historical evaluation but still eligible for prospective shadow collection. Do not use today's values as historical point-in-time features.

### Required Acquisition Sequence

1. Inventory all local files/manifests/backups and benchmark caches; inspect schema, timestamps, timezone, symbol coverage, gaps, duplicates, hashes and generation. Search application DB tables (`entry_selection_candidates`, `ready_candidates`, `ready_candidate_checks`, `trades_audit`, `trades_closed`, model/learning tables) for already captured decision-time evidence before downloading replacements.
2. Inspect existing retrieval scripts and their source semantics. Preserve provenance (`source`, endpoint/archive, query window, retrieval time, API version, symbol, interval, row count, gaps and SHA-256). Reconstruct only fields derivable without look-ahead, and label them `derived` rather than exchange-observed.
3. For public historical sources, verify official endpoint/archive availability and retention limits, then fetch only missing ranges. Validate overlap against existing files and preserve immutable raw files/manifests before building normalized datasets.
4. If historical backfill is impossible but the feature is potentially useful, add a read-only prospective recorder in Staging: no order submission, no account secrets in artifacts, bounded storage, event timestamps plus local receive timestamps, gap/sequence diagnostics and daily checksums. Accumulate the predeclared sample/window before deciding whether the family is evaluable prospectively.
5. Only after steps 1-4 document exhausted sources, limits, attempts and blockers may that feature/family be marked `not_evaluable_historical`. Do not mark the whole benchmark insufficient because one optional family lacks data; run the supported families and report the unsupported family separately.

### Acquisition Map by Philosophy

| Philosophy/data | First obtain or reconstruct | If backfill is partial |
|---|---|---|
| Mean reversion / momentum / breakout | Recover point-in-time OHLCV for selected symbols at 1m/5m plus the strategy context intervals (1h/4h); rebuild S/R, Bollinger, ATR, trend, volume and source-candle timestamps from raw candles with closed-bar/no-lookahead rules. Prefer official Binance Vision daily/monthly USD-M futures klines (available archive begins 2020 per official `binance/binance-public-data`) for futures symbols; use the repo downloader only for targeted residual gaps. Snapshot exchange filters separately. | Evaluate only symbol-window slices passing the declared completeness threshold; report coverage/missingness. Do not substitute current indicators. |
| Cross-sectional / relative strength | Obtain point-in-time symbol universe/listing/delisting and category/sector snapshots plus synchronized benchmark (BTC/sector) returns and historical liquidity. Audit present-day universe survivorship; search archived screeners, exchangeInfo snapshots and daily ticker-derived data. | Reconstruct only when historical membership can be verified; otherwise run a restricted survivorship-aware cohort and state the universe limitation, not a fabricated full-universe result. |
| Carry / funding / basis | Extend per-symbol funding history through the full window using the existing external downloader/API and validate timestamps/rates. Binance Vision archives mark/index/premium klines. Although REST `openInterestHist` (`GET /futures/data/openInterestHist`) returns only the latest month, dated Binance Vision `metrics` objects were verified for BTCUSDT (2025-02-05, 2025-07-15) and ETHUSDT (2026-06-07); the CSV contains 5m OI, OI value, top-trader/account long-short ratios and taker long-short volume ratio. | OI/funding/basis are `obtainable_pending`, not unavailable: fetch target symbols/dates and audit checksums, intervals and coverage. Do not infer historical OI from the REST endpoint after its retention window. |
| Volatility / risk-premium | Reconstruct realized volatility, ATR, range expansion and compression from verified OHLCV. Search for archived options/implied-volatility feeds if an implied-risk-premium variant is proposed. | Realized-volatility variants remain evaluable from candles; implied-volatility variants require their own timestamped option history and are not inferred from realized volatility. |
| Statistical arbitrage / pairs | Align paired 1m/5m OHLCV on exact timestamps; recover historical universe, both-leg exchange filters, volume/depth, fees, funding/borrow and data gaps. Estimate hedge ratio/cointegration only on the train fold and replay both legs with synchronized execution assumptions. | Test the public archive and existing MIMIC symbol files before declaring unavailable. If both-leg execution state cannot be reconstructed, mark only executable pairs results unavailable; keep statistical signal discovery labeled exploratory. |
| Microstructure / order flow | Official Binance Public Data includes USD-M `aggTrades`/`trades` history from 2020 for taker-flow proxies. Binance Vision `bookDepth` was verified for BTCUSDT (2025-02-05) and ETHUSDT (2025-07-15); each sampled daily CSV has `timestamp,percentage,depth,notional`, 28,800 rows and 2,880 timestamps (~30s cadence across percentage bands). This is aggregate depth, not order-level L2. Search target dates/symbols and approved archives/vendors for true L2/bookTicker; if absent after that, create a read-only Staging recorder. | Trade-flow and aggregated-depth variants remain `obtainable_pending` until target coverage/schema/gap audits pass. Full L2 is a distinct variant and only becomes prospective-only after archive/vendor avenues are exhausted. Never equate aggTrades/bookDepth bands with a replayable matching-engine book. |
| News / on-chain / scheduled events | Search archived event/news/on-chain feeds with publish time, observation time, revisions and source provenance. Reconstruct only features available before each decision timestamp. | If only revised/current values exist, obtain a point-in-time archive or begin prospective capture; otherwise mark the affected variant unavailable after documenting those attempts. |

### Acquisition Plan by Devin Finding

| Finding | Obtain or reconstruct before classifying insufficient |
|---|---|
| D1 — Predictor probability may be miscalibrated | Rebuild a point-in-time table joining prediction snapshots to unique outcomes by decision/event ID, thesis, venue and horizon. Prefer the four-hour realized TP/SL outcome from intrabar 1m/5m plus actual fees/slippage; separate label mismatch, leakage, stale side and execution failure. Recover missing labels from archived klines only when entry/contract timestamps are trustworthy. Fit Brier/reliability/isotonic or shrinkage calibrators on train folds, report calibration by thesis/side/venue/horizon with cluster bootstrap. Verify Devin's reported `p≈0.80` versus ~3% on the exact cohort before treating it as a model defect. If sample is sparse, pool hierarchically/shrink to a base rate; if still unreliable, EV families depending on raw `p_i` remain not evaluable until new labels are collected. |
| D2 — Microstructure/order-flow | Search local raw trades, aggTrades, archived bookTicker/depth files, stream logs and manifests. Check official historical trade archives/public endpoints for prints and reconstruct trade-flow imbalance where aggressor-side data exists; do not claim this reconstructs historical L2 depth. For depth/imbalance, check approved point-in-time vendors/archives. If no valid historic book exists, deploy a read-only recorder of depth snapshots + diff events, sequence IDs, exchange/receive timestamps, spread/depth and gap/reset status, without orders. Classify the historical variant `not_evaluable_historical` only after these archive/source checks; keep it `prospective_only` while collecting. |
| D2 — Statistical arbitrage/pairs | Search all symbol OHLCV intervals, delisted-symbol manifests, pair universes and external archives. Align pair candles by exchange timestamp, measure overlap/gaps, estimate hedge ratio/cointegration on train only, and test stability in later folds. Reconstruct both legs' venue filters, fees, funding/borrow, liquidity and simultaneous-fill/slippage constraints. Only after the full archive/vendor/reconstruction sequence is exhausted may the signal research be called exploratory and the executable-pairs result marked `not_evaluable_historical` for that cohort; attempt prospective synchronized recording if retained as a candidate family. |
| D3 — Simulator parity | Reuse the corrected SimulationRunner's intrabar outcome/fill primitives, not just aggregate `_derive_metrics`. Build deterministic fixtures for TP-first, SL-first, same-candle ambiguity, gap-through, time-stop expiry, fees, slippage and funding; differential-test each fixture against the formula replay. Keep lifecycle decision logic out, but share the exact fixed contract outcome mechanics and declared `entry_time_stop_hours`. If corrected simulator behavior cannot be imported, extract one pure shared resolver and prove old/new parity before running any formula. |
| D4 — `venue_executable_at_time` | First search stored exchangeInfo/filter snapshots, account/balance snapshots, feature flags, routing decisions, margin borrow/risk checks and execution-gate audits. For future periods, capture per-decision Spot/Futures exchange filters, symbol state, min/max/step/tick/notional, enabled venue, balance/margin availability and Margin borrowability/collateral status. Join by timestamp and fail closed when unavailable. Current exchangeInfo or current borrow status cannot be backdated as historical truth. Mark only that venue alternative unavailable for periods without reconstructable state; do not mark the entire event or philosophy insufficient. |
| D5 — Replay resource budget | Before full grids, profile one symbol/window/fold at 1, 2 and 4 workers; record wall time, peak RSS, parquet cache size and output throughput. Start conservatively at 1-2 workers, cap at `max_workers <= 6`, use worker recycling (`max_tasks_per_child` where supported), bounded per-worker memory, incremental JSONL/checkpoints, resumable job IDs and output hashes. Increase concurrency only after a no-OOM pilot with reserve memory. |
| D6 — Dedupe/event clusters | Re-extract the measured 1,154 rows with source `signal_id`/`ready_candidate_id` first, then source-candle timestamp + symbol + interval; keep `cycle_id` as attempt lineage, not independent sample. Produce a cluster audit with rows/cluster, conflicting theses, repeated checks, missing identity and manually inspected false merges/splits. Compare the result with the known ~161-entry cohort (±5% is a diagnostic alert, not a target to tune the key toward). Explain every material delta; only then calculate effective sample size and grouped folds. |
| D7 — Watcher pause race | Add deterministic barrier-based integration test: watcher reads unpaused/current QUBO, pauses between candidate retrieval and pre-submit recheck, test changes persisted pause, then release watcher; assert zero order-submit calls and `PAUSED_DB`. Repeat with QUBO cycle/hash rotation and with DB read failure. Use stubbed exchange clients; no real orders. Run the test in CI/focused suite, then optionally verify audit/log behavior in Staging with trading paused. |

### Local Sources Already Identified (Not a Sufficiency Verdict)

- OHLCV acquisition exists in `Dev/scripts/download_historical_data.py`, but it calls `BinanceClient.client.get_klines` backed by the standard Spot `binance.client.Client`; its `historical_data` parquets are Spot-derived and cannot certify USD-M coverage. Official Binance Public Data publishes daily/monthly archives separately for Spot and USD-M Futures, with checksums; preserve them as separate venue series. A BTCUSDT 1m archive for 2025-02-05 was downloaded for both venues into `Dev/benchmarks/universal_entry_selection/raw/binance_archives/`; both official SHA-256 sidecars matched. Each ZIP contains 1,440 unique bars from 00:00–23:59 UTC. Spot CSV has no header and microsecond timestamps; USD-M CSV has a header and millisecond timestamps. The parser/acquisition path must detect both formats. This proves the first RANGE_SCALP date is obtainable, not that its full symbol/window matrix is covered.
- Local 1m audit (2026-09-29): 10 symbols exist. AAVE/ADA/AVAX/BCH/BNB/BTC span 2025-03-09–2026-06-30, each with 167,047 rows, no duplicate timestamps, but six gaps and a longest gap of 234,720 minutes (~163 days). ASTER/BIO span 2026-03-16–2026-06-30, each with 60,483 rows, no duplicates, two gaps and a longest gap of 77,760 minutes (~54 days). DOGE/ENA span 2025-03-09–2026-02-23, each with 106,564 rows, no duplicates, three gaps and a longest gap of ~163 days. Treat these as discontinuous chunks, not full-range 1m coverage.
- The local symbol×window×interval manifest covers 2,232 checks: 1,580 complete, 536 missing files, 88 windows not covered, and 28 1h windows with internal gaps; all 744 5m checks are complete for the local Spot-derived series. For 1m, RANGE_SCALP has 149 incomplete symbol-window checks (107 missing files, 42 outside local file ranges) and none complete. A deduplicated, full-day upper-bound fill for the currently incomplete local 1m checks is 6,698 symbol-days across 39 symbols; this is Spot scope only, and USD-M/other venue obligations still need a venue-aware manifest. The 28 incomplete 1h checks span at most 420 symbol-days if whole days are fetched; compute exact gaps before acquisition.
- Read-only Staging calibration audit (2026-09-29): among 500 most recent unique closed positions linked by `trades_closed.metadata.ready_candidate_id`, all 500 matched a `ready_candidates` row and 499 had a pre-entry check; searching backward to the latest pre-entry check containing the fields found `success_probability` and EV for 499/500. None had an explicit `ev_horizon_minutes` field. This supports outcome linkage and a lifecycle-close calibration cohort, but not a fixed-horizon calibration gate until the horizon/target semantics are recovered or prospectively instrumented. A separate bounded sample of 3,000 recent `entry_selection_candidates` rows had code/config hashes on 2,196 and no full venue contracts, source timestamp or signal ID. The direct `opened_trade_id` to `trades_closed.id` join is not a valid general linker; use the builder's metadata `ready_candidate_id` path and validate close grouping.
- Venue-aware archive acquisition (2026-09-29/30): `Dev/scripts/acquire_universal_entry_archives.py` defaults to plan-only, uses unique atomic checkpoint names, supports resumable/checksummed downloads with up to six workers, and records the raw USD-M catalog hash. Across all 24 calibration windows, the final unique-target audit reconciles 14,281 Spot/USD-M 1m daily targets: 14,058 full days passed SHA-256/cadence checks; 222 USD-M targets were `not_found`; one Spot day is partial; zero targets failed or remain unreported; zero partial downloads remain. Of these, 1,800 Spot targets were already complete in local parquet and are covered by its source hashes, while 45 USD-M pre-listing dates are excluded from the venue target set using official onboard dates. The first RANGE_SCALP date maps `PEPEUSDT` to `1000PEPEUSDT`; `PAXGUSDT` Futures onboarded on 2025-03-27 and is excluded before that date. For RLUSDUSDT, SPCXBUSDT, USD1USDT and UUSDT, current USD-M `exchangeInfo` has no contract; sampled daily/monthly archives returned 404 and the Futures API returned `-1121`. `SPCXBUSDT` is not interchangeable with another ticker without documented point-in-time evidence; retain all four cohorts as `obtainable_pending` while checking historical listings, aliases and alternate sources. `TONUSDT` Spot on 2026-06-30 has matching monthly archive and Spot API results of exactly 180 bars through 02:59 UTC; keep as partial coverage, not full-day or corruption.
- USD-M feature archive progress (2026-09-30): `Dev/scripts/acquire_universal_entry_features.py` defaults to plan-only, records the catalog hash and validates official checksums plus schema/cadence. Across the full `metrics` matrix, 7,353/7,583 contract-days are complete; 222 USD-M days returned 404 (`RLUSDUSDT` 69, `USD1USDT` 69, `UUSDT` 69, `SPCXBUSDT` 15) and 8 TONUSDT days are partial around settlement (23–30 Jun). No metric target is unreported or technically failed. `bookDepth` now has 182/7,583 contract-days independently checksum/cadence-validated, with 7,401 remaining (~3.3 GB estimated from observed samples); each sample has 2,880 snapshots/day at ten percentage bands with observed 13–53s gaps. This establishes historical aggregate-depth availability, not full family coverage or L2 equivalence; continue the bounded backfill and alternate-source checks before any insufficiency verdict.
- The existing `window_data_download_1m_stress.json` reports 72/72 jobs successful for nine windows, but it intentionally selected only eight symbols per window and covered `FUTURES_SHORT`/`TRANSITION` windows. Rechecking calibration manifests shows eight near-complete symbols for those tested windows, with DOGE/ENA missing in later windows; `RANGE_SCALP` has no complete 1m coverage in its five windows, and several `SPOT_BULL` windows also have none. The 2026-06 RANGE_SCALP window has only partial (~36%) candles for six existing files. Successful download jobs are not proof of continuous coverage.
- Recovery runbook: generate a symbol×window×interval gap manifest from `window_plan.json`; fetch only missing daily/monthly ZIPs from `https://data.binance.vision/data/futures/um/{daily,monthly}/klines/{symbol}/1m/` (and needed 5m/context intervals), verify `.CHECKSUM`, merge by open timestamp without deleting/backing over unique data, and re-audit exact window bounds/duplicates/gaps. Pilot with 1–2 workers, incremental/checkpointed output and resume; do not use the old eight-worker stress script for a broad run. Funding and trade downloads follow the same bounded/provenance/checksum process.
- Official Binance Public Data exposes USD-M `aggTrades` and `trades` history from 2020. Download these for the shortlisted symbols/windows to build buyer-maker/taker-flow proxies. This makes trade-flow variants obtainable; it does not recreate historical Level-2 depth.
- External retrieval exists in `Dev/scripts/mimic_download_external.py`; current local files include funding through 2026-07-10/11, plus Fear & Greed, ticker and dominance. Audit each selected symbol's funding completeness and extend the missing window ranges with the downloader/API. Fear & Greed can be extended from its historical endpoint after verifying timestamps/coverage.
- `download_btc_dominance()` calls CoinGecko's current `/global` endpoint for each requested date and stamps fetch time; these rows are not historical BTC dominance and must not be used point-in-time. Search CoinGecko historical/global endpoints, approved providers and archived snapshots; if none supplies the decision-time series, begin prospective capture before deciding that feature is historically unavailable.
- Binance Vision archive probes returned HTTP 200 for BTCUSDT metrics (2025-02-05 and 2025-07-15), ETHUSDT metrics (2026-06-07), BTCUSDT bookDepth (2025-02-05), and ETHUSDT bookDepth (2025-07-15). Verified paths follow `data/futures/um/daily/metrics/{SYMBOL}/{SYMBOL}-metrics-YYYY-MM-DD.zip` and `data/futures/um/daily/bookDepth/{SYMBOL}/{SYMBOL}-bookDepth-YYYY-MM-DD.zip`. The metrics sample has 288 rows/unique timestamps from 00:00 to 23:55 at 5m with columns for `sum_open_interest`, OI value, top-trader/account long-short ratios and taker long-short volume ratio. The bookDepth sample has 28,800 rows/2,880 timestamps from 00:00:05 to 23:59:32, schema `timestamp,percentage,depth,notional` (~30s snapshots across percentage bands). These probes prove dated objects exist, not full symbol/window coverage; fetch/checksum/audit the exact manifest matrix next. `bookDepth` is aggregated depth, not level-by-level L2.
- Official USD-M REST `openInterestHist` (`GET /futures/data/openInterestHist`) itself retains only the latest month, but Binance Vision dated `metrics` ZIPs contain historical OI and trader ratios. Thus OI is not classified insufficient; use archive metrics for older windows and the REST endpoint only for recent overlap/cross-checks.
- The workspace has no local L2/order-book/depth dataset, but official Binance Vision `bookDepth` historical objects were found. Determine whether its 30s percentage-band aggregate supports the proposed depth variants; it is not full L2. Search for level-by-level archives/vendor feeds and, if none meet requirements, collect true L2 prospectively. Trade-flow proxies are obtainable from public aggTrades independently.

The above is an initial inventory, not the final insufficiency decision. Keep each data item `obtainable_pending` until the specified archive/API/vendor/reconstruction/prospective steps are attempted and the attempt report is stored with source, query range, coverage, failure reason and checksum. No family is marked `not_evaluable_historical` solely because its data was absent from the initial local search.

**Decision precedence:** the later user instruction (2026-09-29) supersedes Devin Review 2026-09-29-02's preliminary suggestion to mark microstructure and statistical-arbitrage families `not_evaluable` at the outset. Verified Binance Vision `metrics` and `bookDepth` objects demonstrate that OI and aggregate-depth history can be obtained for at least sampled symbol/dates; full target-matrix coverage and whether aggregate depth suffices remain to be tested. Keep these families `obtainable_pending` until the acquisition sequence is exhausted; only true level-by-level L2 may remain a separate prospective/vendor search after that audit.

`staged_immediate_entry_enabled` remains default-off and is excluded from the benchmark; if enabled, it commits before direction confirmation and must be disabled or brought under the same authority before any tape freeze.

Focused verification completed so far: latest combined prerequisite suites `165 passed, 45 warnings` before the newest documentation/data-audit changes; Pylance reports no errors on touched files; `git diff --check` clean (only pre-existing CRLF notices). Earlier focused contract/filter suite `19 passed`; QUBO writer-reader round-trip, four-contract persistence, source-candle clustering, learned-weight/config hash and per-candidate abstention regressions pass. Test runs overlap; do not sum counts. No Staging restart or production action was performed for this offline instrumentation.

<!-- DEVIN_REVIEW_START: 2026-09-29 -->
## [DEVIN REVIEW 2026-09-29] Análisis crítico — qué mejorar antes de ejecutar

Revisado contra los hallazgos medidos esta semana (161 entradas únicas de 1,154 filas; stop 0.26×ATR; señal invertida ambos lados; bypasses operativos identificados en `plan-cierre-bypasses-entrada-20260929.md`). Tag: **[D]**.

### Prerequisitos bloqueantes — ejecutar ANTES del benchmark

1. **[D] Cerrar los bypasses de ejecución.** El FastPath ignora `pause_new_entries` (DB) y `get_openable_candidates` ignora `top_symbols_qubo`. Si la cinta congelada contiene decisiones hechas durante bypasses, el benchmark mide una política que nunca existió consistentemente. Congelar tape solo DESPUÉS de `plan-cierre-bypasses` implementado.

2. **[D] Persistir contratos alternativos por candidato.** El replay offline requiere los 4 contratos por candidato (Spot LONG, Futures LONG, Margin SHORT, Futures SHORT), no solo el ganador. `entry_selection_candidates`/`ready_candidates` solo persisten el elegido — este plan debe incluir como paso de implementación previo: *"persist the full contract set per decision before benchmarking"*.

3. **[D] Baseline congelado por hash.** `baseline_current_universal` debe llevar `config_hash + code_hash` explícito — el "comportamiento actual" cambió 3 veces en una semana (v2 flags, s139, s140). Sin hash, "baseline" es ambiguo.

### Definiciones que faltan

4. **[D] Unidad de análisis: entrada única.** La doc habla de "filas" pero la medición real es `1,154 filas = 161 entradas únicas (7.2× inflación)`. Especificar dedupe por `(symbol, floor(cycle_ts, minute))` o cluster de oportunidad — si no, un símbolo re-aparecido domina la muestra artificialmente.

5. **[D] Dimensión `thesis` faltante en las familias.** Todas las fórmulas son sobre *qué candidatos* seleccionar, pero el bug real era *qué lado/tesis* elegir dentro del candidato. Añadir `thesis_type` como estratificador obligatorio y considerar pesos condicionados por tesis (reversion vs breakout tienen priors distintos). La selección universal debe elegir símbolo Y lado, no solo símbolo.

6. **[D] `abstention_quality` como métrica primaria.** Una fórmula que siempre se abstiene tiene drawdown 0 y EV infinito por muestra — el criterio "no gana solo bajando gates" no basta. Medir `EV_de_lo_que_se_abstuvo` (contrafactual del rejected set) para que la abstención tenga costo medible.

### Protecciones estadísticas a endurecer

7. **[D] Separar tuning tape de evaluation tape.** "Calibrate weights" + "evaluate on same tape" = overfitting garantizado. Los pesos deben ajustarse solo en train fold (walk-forward ya lo permite, pero especificar que `U_i` weights se ajustan SOLO en el fold de entrenamiento de cada paso, nunca en el de validación).

8. **[D] Sample size escala con familias.** `n≥30` es el piso absoluto para UNA comparación. Con 11 familias × pesos múltiples, el mínimo realista es `n≥50` por familia vs baseline, o reducir familias a 4-5 de prioridad antes de evaluar. Corrección de multiplicidad (Holm o similar) sobre los p-values emparejados.

9. **[D] Reversión automática al baseline.** Si el intervalo de confianza de la fórmula ganadora cruza cero en OOS → revertir a baseline, no "analizar más". Especificarlo como gate, no como observación.

### Variables faltantes en "Calibration Scope"

10. **[D] Añadir:** `thesis_type` conditioned weights, `venue_executable_at_time` (Margin disabled hoy), `entry_generation` (para distinguir baseline/v2/s140 en datos live), `dedupe_cluster_id`, `ev_horizon` (el EV del contrato depende del horizonte temporal elegido — `entry_time_stop_hours` es variable del EV, no solo del exit).

### Fortalezas que NO cambiar

- Congelar sizing/Kelly/leverage/lifecycle — correcto.
- "La conclusión puede ser que nada supera baseline" — correcto, mantener.
- QUBO como una familia entre varias — correcto, evita el bias hacia la solución más compleja.
- Filtros Binance dentro del contrato antes de declarar `executable` — correcto.

<!-- DEVIN_REVIEW_END: 2026-09-29 -->

<!-- DEVIN_REVIEW_START: 2026-09-29-02 -->
## [DEVIN REVIEW 2026-09-29-02] Re-análisis de la propuesta actualizada — 8 filosofías añadidas

### Confirmación: sí, se añadieron las 8 familias de filosofías

La sección "Trading Philosophies and Entry Generators" (líneas 24-52) añade 8 familias: mean reversion/range, momentum, breakout/vol-expansion, cross-sectional, carry/funding, volatility/risk-premium, stat-arb, microstructure — más la separación correcta de dos ejes experimentales (generador de oportunidad vs fórmula de selección). Es la separación conceptual correcta y cubre el hueco que yo marqué (mi punto #5: el bug real era de tesis, no de símbolo).

### Verificación del "Current Readiness Status" contra el código

| Claim de Copilot | Verificado por mí |
|---|---|
| Watcher chequea `_is_new_entries_paused` (DB) | ✅ `orchestrator.py:865-866` — el FastPath ya lee la pausa persistida |
| `get_qubo_authority_snapshot` con cycle_id+hash | ✅ `staged_entry.py:2746`, pasado desde 5 callsites en `run_trades.py` |
| `all_venue_contracts` con los 4 contratos | ✅ `directional_arbiter.py:291-318` |
| Contract quantity floors a MARKET step + filtros dinámicos | ✅ presente en `opportunity_contract.py` |

Los gates de readiness reflejan código real — no son aspiracionales.

### Lo que mi review anterior pidió vs lo que está cubierto

Todos mis 10 puntos están absorbidos por los Gates 1-6: bypasses (Gate 1+2), 4 contratos persistidos (Gate 3), baseline por hash (Gate 4), dedupe (Gate 4), abstención (Gate 5), train/eval separation + multiplicidad + rollback (Gate 6). Mi punto #5 (thesis-conditioned weights) quedó correctamente rediseñado como el eje de filosofías — mejor solución que pesos condicionados.

### Lo que aún falta o debe endurecerse (nuevos puntos [D])

**[D1] La calibración de probabilidad del predictor debe ser un GATE, no una familia.** Todas las fórmulas basadas en EV (familias 4, 5, 6, 9, 10) comparten `p_i`. Nuestra data real: predictor decía p≈0.80 donde el mercado realizó ~3% (SHORT rejection) — si `p` está corrupta, 5 de 10 familias heredan la misma corrupción y el benchmark no lo detecta. Añadir **Gate 7**: validar calibración de `p_i` (Brier/reliability curve en train fold) antes de que cualquier fórmula EV entre al benchmark. Si la calibración falla, solo quedan evaluables las familias no-EV (outranking, Pareto parcial, cardinalidad).

**[D2] Dos familias probablemente son `not_evaluable` desde el inicio — declararlo ya.** Microstructure necesita libros de órdenes archivados point-in-time — `historical_data` tiene OHLCV, no L2 books. Stat-arb necesita datos sincronizados de pares + hedge ratio + ambas patas ejecutables — no existe la infraestructura de captura. Marcarlas `not_evaluable` desde el principio ahorra el costo de implementarlas para descubrir que faltan datos (Gate 1 del Implementation Order ya lo prevé; solo registro que esas dos son las candidatas casi-seguras).

**[D3] Paridad de semántica con el simulador Entry.** El replay del benchmark debe usar exactamente la misma mecánica intrabar que el SimulationRunner corregido (SL/TP intrabar + `entry_time_stop_hours` + fill confirmado + costes). Si el replay usa una resolución de TP/SL distinta a la del sim que produjo el baseline 115/280, las fórmulas se comparan sobre un outcome-model distinto del que generó la evidencia. Especificar que el harness reutiliza `_derive_metrics`/la lógica intrabar del sweep, no una réplica.

**[D4] `venue_executable_at_time` debe capturar el estado real.** Margin está `enabled=false` en staging — un contrato Margin SHORT puede ser "ejecutable" en teoría pero no hoy. La cinta congelada debe grabar el flag real de disponibilidad, no el teórico, o el benchmark premiará fórmulas que seleccionan venues no operables.

**[D5] Presupuesto de recursos del replay.** Con 8 workers el sweep llevó el daemon a OOM tres veces. El benchmark con 8 filosofías × variantes × folds de walk-forward es computacionalmente mayor que el sweep Entry. Fijar desde ahora: `max_workers ≤ 6`, `max_tasks_per_child` para reciclar workers, y persistencia incremental obligatoria.

**[D6] Validar `dedupe_cluster_id` contra el caso medido.** Gate 4 lo marca como pendiente — correcto. El criterio real: re-correr la extracción sobre los 1,154 registros conocidos debe producir ~161 clusters (±5%). Si produce 800 o 12, la política de cluster está mal calibrada antes de congelar la cinta.

**[D7] Resolver el test pendiente de Gate 1 antes de tape freeze.** El readiness status marca "threaded watcher race test pendiente" — debe cerrarse; una pausa activada mid-preparación que no bloquee al watcher es exactamente el tipo de bypass que invalida una cinta.

### Veredicto sobre la propuesta

La estructura de dos ejes (filosofía → fórmula) es la respuesta correcta a nuestro diagnóstico: el bug era de generación de tesis, no de ranking. La fase de filosofías validará si momentum/breakout captura lo que la reversión perdía; la fase de fórmulas optimiza la selección sobre candidatos ya correctamente generados. Con los Gates 1-6 implementados + mis puntos D1-D7 cerrados, el benchmark es ejecutable y conclusivo.

<!-- DEVIN_REVIEW_END: 2026-09-29-02 -->
