---
title: "Opportunity Arbiter — Regime-Agnostic, Instrument-Aware Selection Engine"
type: design
status: superseded
created_at: 2026-09-11
superseded_at: 2026-09-12
superseded_by: "calibracion_entradas-a20ab8-v3.md s135"
version: v1
scope: "Complete entry opportunity evaluation across SPOT, SPOT_MARGIN, FUTURES (LONG/SHORT) and all regimes"
---

# Opportunity Arbiter v1 — Multiregime, Multiinstrument, Multiside Unification

## Executive Summary

The current system generates a side-specific candidate (SHORT Spot Margin, LONG Futures, etc.) early, then confirms that pre-chosen side. This architecture allows a bad side to be locked in by inersia before a better alternative is even tested.

**Recommended redesign**: Build a unified **opportunity** object containing all simultaneously-viable theses (LONG Spot, LONG Futures, SHORT Spot Margin, SHORT Futures), evaluate each under identical conditions on a common market snapshot, compute normalized net utilities, and select the single best executable thesis—or abstain if none clear the bar.

This document specifies that contract for production.

---

## 1. Regime-Instrument-Side Matrix

### 1.1 Canonical Grid

| Regime | Instrument | Side | Anchor | Target | Use Case | Kelly Multiplier | SL Geometry | TP Geometry |
|---|---|---|---|---|---|---|---|---|
| **SPOT_BULL** | SPOT | LONG | Support | Resistance | Buy pullback in uptrend | 1.05× | ATR % | ATR % |
| | SPOT_MARGIN | LONG | Support | Resistance | Leveraged pullback (2x) | 1.2× | ATR % | ATR % |
| | FUTURES | LONG | Support | Resistance | Futures pullback (2x lev) | 1.2× | ATR % | ATR % |
| | SPOT | SHORT | (None) | (None) | Not available in bull | — | — | — |
| | SPOT_MARGIN | SHORT | Resistance | Support | Short resistance in bull transition | 0.8× | ATR % | ATR % |
| | FUTURES | SHORT | (None) | (None) | Not executable in SPOT_BULL | — | — | — |
| **RANGE_SCALP** | SPOT | LONG | BB Lower | BB Upper | Mean reversion: buy support | 1.0× | BB width | BB width |
| | SPOT_MARGIN | LONG | BB Lower | BB Upper | Lev mean reversion (2x) | 1.1× | BB width | BB width |
| | FUTURES | LONG | BB Lower | BB Upper | Futures scalp, long bounce | 1.0× | BB width | BB width |
| | SPOT | SHORT | BB Upper | BB Lower | Mean reversion: sell resistance | 1.0× | BB width | BB width |
| | SPOT_MARGIN | SHORT | BB Upper | BB Lower | Lev mean reversion, short (2x) | 1.1× | BB width | BB width |
| | FUTURES | SHORT | BB Upper | BB Lower | Futures scalp, short bounce | 1.0× | BB width | BB width |
| **FUTURES_SHORT** | SPOT | LONG | (None) | (None) | Not available in bear | — | — | — |
| | SPOT_MARGIN | LONG | (None) | (None) | Not available in bear | — | — | — |
| | FUTURES | LONG | (None) | (None) | Disabled in bear; emergent only | 0.3× | ATR % | ATR % |
| | SPOT | SHORT | (None) | (None) | Not available in structured bear | — | — | — |
| | SPOT_MARGIN | SHORT | Resistance | Support | Short bounce in bear (2x) | 1.0× | ATR % | ATR % |
| | FUTURES | SHORT | Resistance | Support | Short bear bounce | 1.0× | ATR % | ATR % |
| **PRESERVATION** | SPOT | LONG | Narrative only | (Catalyst + volume) | Narrative token rally | 0.6× | Hard TP | Hard TP |
| | SPOT_MARGIN | LONG | (None) | (None) | Narrative margin disabled | — | — | — |
| | FUTURES | LONG | (None) | (None) | Narrative futures disabled | — | — | — |
| | SPOT | SHORT | (None) | (None) | Not available in preservation | — | — | — |
| | SPOT_MARGIN | SHORT | (None) | (None) | Not available in preservation | — | — | — |
| | FUTURES | SHORT | (None) | (None) | Not available in preservation | — | — | — |

**Key constraints:**
- **SPOT_BULL**: LONG only (Spot, Spot Margin, Futures). SHORT via Spot Margin only in rare transition scenarios.
- **RANGE_SCALP**: LONG and SHORT equally viable (all instruments). Both sides exploit mean reversion symmetrically.
- **FUTURES_SHORT**: SHORT only (Futures, Spot Margin for bounces). LONG Futures emergent only if RSI <32 in structural bear (disabled by default).
- **PRESERVATION**: Narrative LONG only, Spot instrument only. All other instruments and sides disabled.

---

## 2. Common Market Snapshot

For each symbol, at each entry-decision window, freeze exactly one snapshot:

```python
# Common market snapshot (one per symbol per entry window)
snapshot = {
    # Prices and time
    "symbol": "BTCUSDT",
    "timestamp": datetime.now(tz=UTC),
    "current_price": 42567.89,
    
    # Candlestick data (frozen at decision time)
    "bars_1h": [...],   # Last 100 candles (1h interval), closed
    "bars_4h": [...],   # Last 50 candles (4h interval), closed
    "bars_15m": [...],  # Last 100 candles (15m interval), closed
    
    # Support/Resistance (no look-ahead, only closed levels)
    "support_level": 41230.00,
    "support_strength": 0.85,  # 0.0-1.0, confluence count or volume-weighted
    "resistance_level": 43800.00,
    "resistance_strength": 0.90,
    
    # Support/Resistance events (closed-bar confirms only)
    "sr_bounce": False,         # Vela de soporte con vol spike + close > entrada
    "sr_breakout": False,       # Cierre sostenido sobre resistencia
    "sr_breakdown": False,      # Cierre sostenido bajo soporte
    "sr_rejection": False,      # Vela roja en resistencia con vol
    
    # Technical indicators (1h, 4h)
    "rsi_1h": 45.2,
    "rsi_4h": 52.1,
    "atr_pct_1h": 0.0125,  # ATR / current_price
    "bb_lower": 41800.00,
    "bb_middle": 42500.00,
    "bb_upper": 43200.00,
    "bb_position": 0.32,    # (price - lower) / (upper - lower), [0, 1]
    "trend_1h": "UP",       # UP, DOWN, FLAT (EMA9 vs EMA21)
    "trend_4h": "UP",
    "vol_24h_usdt": 18_500_000_000,
    "vol_spike_1h": 2.3,    # Current 1h vol / avg 24h vol
    
    # Funding rate and open interest (futures only)
    "funding_rate_annual_pct": 42.5,
    "oi_change_1h_pct": 3.2,
    
    # Market regime context
    "regime_mode": "RANGE_SCALP",
    "regime_confidence": 0.78,
    "btc_7d_pct": -2.1,
    "btc_30d_pct": -8.5,
}
```

**Rules for snapshot:**
- Snapshot is frozen at decision window. All theses (LONG Spot, LONG Futures, SHORT Spot Margin, SHORT Futures) evaluate against **identical** market data.
- No symbol-specific data is fetched twice. Screener provides the universe; signals enrich the top N candidates; opportunity evaluator works within that set.
- If data is stale (older than `signal_ttl_seconds`), snapshot is refreshed; candidates wait, not evaluated on expired data.

---

## 3. Thesis Object (Multiside Candidate)

Instead of a single directed candidate, build a thesis object that contains **all** simultaneously-viable sides:

```python
@dataclass
class ThesisSnapshot:
    """Unified opportunity container for one symbol.
    
    Holds both LONG and SHORT branches as optional Thesis objects.
    Only the selected thesis (determined by utility comparison) gets opened.
    """
    symbol: str
    snapshot_timestamp: datetime
    market_snapshot: dict  # Frozen snapshot (see §2)
    
    # Parallel theses
    long_thesis: Optional[Thesis] = None      # LONG Spot/Margin/Futures
    short_thesis: Optional[Thesis] = None     # SHORT Spot Margin/Futures
    
    # Selection outcome
    selected_side: Optional[str] = None       # "LONG" or "SHORT" or None (ABSTAIN)
    selection_reason: str = ""
    selection_utilities: dict = {}            # {side: utility_score, ...}
    
    @property
    def is_ambiguous(self) -> bool:
        """True if utilities are within noise (< 1% difference)."""
        ...
    
    @property
    def has_executable_thesis(self) -> bool:
        """True if at least one side is eligible and can be routed."""
        ...


@dataclass
class Thesis:
    """A single side's complete entry thesis.
    
    Attributes determine all entry/exit mechanics for one side.
    """
    side: str                              # "LONG" or "SHORT"
    trade_type: str                        # "spot", "spot_margin", "futures"
    regime_mode: str                       # "SPOT_BULL", "RANGE_SCALP", etc.
    
    # Geometry and price levels
    anchor_price: float                    # Support (LONG) or resistance (SHORT)
    target_price: float                    # Resistance (LONG) or support (SHORT)
    next_levels: List[float] = field(default_factory=list)  # Further targets
    sl_price: float = 0.0                  # Derived from anchor + atr/geometry
    atr_pct: float = 0.0                   # Volatility used for SL/TP sizing
    expected_candles: int = 0              # Geometry: |target-entry|/atr
    
    # Eligibility and routing
    eligible: bool = True                  # Passes all gating rules
    rejection_codes: List[str] = field(default_factory=list)
    
    # Economics
    entry_price: float = 0.0               # Snapshot current price
    position_size_usdt: float = 0.0        # Kelly-derived sizing (before fees)
    commission_rate_pct: float = 0.001     # Instrument-specific: spot 0.1%, futures 0.04%
    slippage_bps: float = 2.0              # Estimate: spot 2-5bp, futures 1-3bp
    funding_bps: float = 0.0               # Futures only; annualized funding / 365
    
    # Predicted outcomes
    success_probability: float = 0.50      # P(close at TP or above/below)
    expected_pnl_usdt: float = 0.0         # Predicted profit in USDT
    expected_pnl_pct: float = 0.0          # As % of position size
    max_loss_usdt: float = 0.0             # Loss at SL (hard cap applied)
    
    # Risk-adjusted utility
    utility_netto: float = 0.0             # P(win)*reward - P(loss)*loss - costs
    utility_adjusted: float = 0.0          # utility_netto × regime_confidence × profile_multiplier
    
    # Quality signals
    signal_strength: float = 0.50
    confidence: float = 0.50               # Composite of features + agreement
    feature_completeness: float = 1.0      # Fraction of expected features present
    
    # Tracking
    ready_candidate_id: Optional[int] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    thesis_id: str = ""                    # Unique identifier for this side
```

---

## 4. Thesis Construction (Regime-Specific)

For each regime, construct both LONG and SHORT theses from the common snapshot. Ineligible sides return `thesis.eligible=False` with codes.

### 4.1 SPOT_BULL Regime

#### LONG Spot Thesis
```python
def build_spot_bull_long_spot_thesis(symbol: str, snapshot: dict, settings) -> Thesis:
    thesis = Thesis(
        side="LONG",
        trade_type="spot",
        regime_mode="SPOT_BULL",
        anchor_price=snapshot["support_level"],
        target_price=snapshot["resistance_level"],
        entry_price=snapshot["current_price"],
    )
    
    # Eligibility gates
    if snapshot["trend_4h"] != "UP":
        thesis.eligible = False
        thesis.rejection_codes.append("TREND_NOT_BULLISH")
    
    if snapshot["current_price"] > snapshot["resistance_level"]:
        thesis.eligible = False
        thesis.rejection_codes.append("ALREADY_AT_TARGET")
    
    if snapshot["vol_spike_1h"] < settings.entry_quality_vol_spike_min_spot_bull:
        thesis.eligible = False
        thesis.rejection_codes.append("VOL_SPIKE_INSUFFICIENT")
    
    if snapshot["rsi_1h"] > 70:  # Overbought
        thesis.eligible = False
        thesis.rejection_codes.append("RSI_OVERBOUGHT")
    
    if not thesis.eligible:
        return thesis
    
    # Geometry and economics
    thesis.atr_pct = snapshot["atr_pct_1h"]
    thesis.sl_price = thesis.anchor_price * (1 - thesis.atr_pct * settings.spot_bull_hard_sl_atr_mult)
    thesis.expected_candles = abs(thesis.target_price - thesis.entry_price) / (thesis.entry_price * thesis.atr_pct)
    
    # Sizing
    kelly_base = settings.kelly_base_usdt
    kelly_pct = settings.kelly_dynamic_base_pct * settings.kelly_multiplier_spot_bull
    thesis.position_size_usdt = min(
        settings.kelly_max_usdt,
        kelly_base * (1 + kelly_pct)
    )
    
    # Costs
    thesis.commission_rate_pct = 0.001  # Spot: 0.1%
    thesis.slippage_bps = 2.0
    
    # Expected outcome (from predictor or simple model)
    reward_pct = (thesis.target_price - thesis.entry_price) / thesis.entry_price
    loss_pct = (thesis.entry_price - thesis.sl_price) / thesis.entry_price
    
    # Normalized for comparison
    thesis.expected_pnl_usdt = (reward_pct - thesis.commission_rate_pct/100 - thesis.slippage_bps/10000) * thesis.position_size_usdt
    thesis.success_probability = predictor.predict_pnl(...).get("success_probability", 0.55)
    
    # Utility
    thesis.utility_netto = (
        thesis.success_probability * reward_pct
        - (1 - thesis.success_probability) * loss_pct
        - (thesis.commission_rate_pct / 100 + thesis.slippage_bps / 10000) * 2  # Round trip
    )
    thesis.utility_adjusted = thesis.utility_netto * snapshot["regime_confidence"] * settings.profile_multiplier
    
    return thesis
```

#### LONG Spot Margin (2x) Thesis
- Same structure as SPOT but:
  - `commission_rate_pct = 0.001` (Spot Margin = Spot commission)
  - `position_size_usdt *= 2.0` (2x leverage)
  - `atr_pct` doubled in SL calculation to account for leverage
  - Kelly multiplier: `1.2×` instead of `1.05×`

#### LONG Futures Thesis
- Same structure but:
  - `commission_rate_pct = 0.0004` (Futures: 0.04%)
  - `position_size_usdt *= 2.0` (2x leverage)
  - `funding_bps` added (annualized funding rate / 365)
  - PnL formula: `(mark_price - entry_price) * qty / entry_price` (never multiplied by leverage again)
  - Kelly multiplier: `1.2×`

#### SHORT Spot Margin (rare transition case)
- Enabled only if regime is transitioning SPOT_BULL → RANGE_SCALP.
- Requires: `trend_4h == "UP"` OR `sr_rejection == True` (sell into failed resistance)
- Lower Kelly multiplier (`0.8×`) due to counter-trend nature.

#### SHORT Futures
- **Rejected** in SPOT_BULL. Returns `eligible=False` with code `REGIME_SHORT_DISABLED`.

---

### 4.2 RANGE_SCALP Regime

Both LONG and SHORT are equally valid in RANGE_SCALP. Build both theses in parallel.

#### LONG Thesis (Scalp Support)
```python
def build_range_scalp_long_thesis(symbol: str, snapshot: dict, settings) -> Thesis:
    thesis = Thesis(
        side="LONG",
        trade_type="spot",  # or spot_margin, futures (parallel theses)
        regime_mode="RANGE_SCALP",
        anchor_price=snapshot["bb_lower"],
        target_price=snapshot["bb_upper"],
        entry_price=snapshot["current_price"],
    )
    
    # Geometry: price should be near lower band
    dist_to_lower_pct = abs(thesis.entry_price - snapshot["bb_lower"]) / snapshot["bb_lower"]
    if dist_to_lower_pct > settings.range_scalp_long_dist_to_lower_max_pct:
        thesis.eligible = False
        thesis.rejection_codes.append(f"LONG_TOO_FAR_FROM_SUPPORT_{dist_to_lower_pct:.2%}")
    
    # RSI should not be oversold extreme
    if snapshot["rsi_1h"] < 20:
        thesis.eligible = False
        thesis.rejection_codes.append("RSI_EXTREME_LOW")
    
    if snapshot["bb_position"] > 0.40:  # Too high in range
        thesis.eligible = False
        thesis.rejection_codes.append("BB_POSITION_TOO_HIGH")
    
    if not thesis.eligible:
        return thesis
    
    # Sizing
    thesis.position_size_usdt = settings.kelly_base_usdt * settings.kelly_dynamic_base_pct  # 1.0x multiplier
    
    # Costs
    thesis.commission_rate_pct = 0.001
    thesis.slippage_bps = 3.0  # Slightly wider for range scalp
    
    # Expected outcome: tight reversal
    reward_pct = (thesis.target_price - thesis.entry_price) / thesis.entry_price
    loss_pct = settings.range_scalp_hard_sl_atr_mult * snapshot["atr_pct_1h"]
    thesis.sl_price = thesis.entry_price * (1 - loss_pct)
    
    thesis.utility_netto = (
        0.55 * reward_pct  # Assumed 55% win rate at support
        - 0.45 * loss_pct
        - (thesis.commission_rate_pct / 100 + thesis.slippage_bps / 10000) * 2
    )
    
    return thesis
```

#### SHORT Thesis (Scalp Resistance)
- Symmetric to LONG: anchor at `bb_upper`, target at `bb_lower`.
- Gate: `bb_position < 0.60` (not too low in range).
- RSI gate: avoid `rsi_1h > 80` (extreme overbought).

#### Parallel Spot Margin & Futures versions
- Same theses, different sizing and fees.
- SPOT_MARGIN: `position_size_usdt *= 1.1×`, commission 0.1%.
- FUTURES: `position_size_usdt *= 1.0×`, commission 0.04%, funding included.

---

### 4.3 FUTURES_SHORT Regime

#### SHORT Futures Thesis (Primary)
```python
def build_futures_short_short_thesis(symbol: str, snapshot: dict, settings) -> Thesis:
    thesis = Thesis(
        side="SHORT",
        trade_type="futures",
        regime_mode="FUTURES_SHORT",
        anchor_price=snapshot["resistance_level"],
        target_price=snapshot["support_level"],
        entry_price=snapshot["current_price"],
    )
    
    # Regime gate: BTC must be in bear
    if snapshot["btc_30d_pct"] > settings.futures_short_bear_30d_threshold:
        thesis.eligible = False
        thesis.rejection_codes.append("NOT_STRUCTURAL_BEAR")
    
    # Bounce confirmation
    if not (snapshot["sr_rejection"] or (snapshot["bb_position"] > 0.65 and snapshot["rsi_1h"] > 50)):
        thesis.eligible = False
        thesis.rejection_codes.append("NO_BOUNCE_CONFIRMATION")
    
    if not thesis.eligible:
        return thesis
    
    # Sizing
    thesis.position_size_usdt = settings.futures_kelly_base_usdt * settings.futures_kelly_dynamic_base_pct
    
    # Costs
    thesis.commission_rate_pct = 0.0004  # Futures
    thesis.funding_bps = snapshot["funding_rate_annual_pct"] / 365 / 100  # Annualized to daily
    
    # Expected outcome
    reward_pct = (thesis.entry_price - thesis.target_price) / thesis.entry_price  # SHORT wins when price falls
    loss_pct = settings.futures_short_hard_sl_atr_mult * snapshot["atr_pct_1h"]
    thesis.sl_price = thesis.entry_price * (1 + loss_pct)
    
    thesis.utility_netto = (
        0.50 * reward_pct
        - 0.50 * loss_pct
        - (thesis.commission_rate_pct / 100 + thesis.funding_bps) * 2
    )
    
    return thesis
```

#### SHORT Spot Margin (Secondary, Bounce play)
- Only when `sr_rejection == True` (explicit sell signal at resistance).
- Lower utility floor (`min_expected_pnl > 2.0`).
- Kelly: `1.0×`.

#### LONG Futures (Emergent, Disabled by Default)
- Enabled only if `btc_7d_pct < -7%` AND `rsi_1h < 32` (extreme oversold in bear).
- Restricted kelly: `0.3×` (tiny position).
- Returns `eligible=False` otherwise.

#### LONG/SHORT Spot Theses
- Both rejected. Return `eligible=False, rejection_codes=["SPOT_DISABLED_IN_FUTURES_SHORT"]`.

---

### 4.4 PRESERVATION Regime

#### LONG Spot Thesis (Narrative tokens only)
```python
def build_preservation_long_spot_thesis(symbol: str, snapshot: dict, settings) -> Thesis:
    thesis = Thesis(
        side="LONG",
        trade_type="spot",
        regime_mode="PRESERVATION",
        anchor_price=None,  # Narrative, no structural support
        target_price=None,  # Hard TP used instead
        entry_price=snapshot["current_price"],
    )
    
    # Gate: must be in narrative_tokens list
    if symbol not in settings.narrative_tokens:
        thesis.eligible = False
        thesis.rejection_codes.append("NOT_NARRATIVE_TOKEN")
    
    # Gate: RSI not overbought
    if snapshot["rsi_1h"] > settings.preservation_max_rsi:
        thesis.eligible = False
        thesis.rejection_codes.append("RSI_TOO_HIGH_FOR_NARRATIVE")
    
    # Gate: volume spike confirms interest
    if snapshot["vol_spike_1h"] < 1.2:
        thesis.eligible = False
        thesis.rejection_codes.append("INSUFFICIENT_NARRATIVE_VOLUME")
    
    if not thesis.eligible:
        return thesis
    
    # Sizing: conservative
    thesis.position_size_usdt = settings.kelly_base_usdt * 0.6  # Smaller multiplier
    
    # Hard TP (no geometric structure)
    thesis.target_price = thesis.entry_price * (1 + settings.preservation_hard_tp_pct)
    thesis.sl_price = thesis.entry_price * (1 - settings.preservation_hard_sl_pct)
    
    thesis.utility_netto = (
        0.48 * settings.preservation_hard_tp_pct
        - 0.52 * settings.preservation_hard_sl_pct
        - (0.001 + 0.0002) * 2
    )
    
    return thesis
```

#### All Other Theses (Margin, Futures, SHORT)
- All rejected with `eligible=False`.

---

## 5. Utility Calculation (Normalized Comparison)

Once all theses are constructed, compute a **normalized** utility that is comparable across instruments, sides, and regimes:

$$
U_{\text{netto}} = P(\text{success}) \times R_{\text{netto}} - (1 - P(\text{success})) \times L_{\text{netto}} - C_{\text{fricción}}
$$

Where:

- $P(\text{success})$: Predicted probability of reaching target (or better) before hitting SL.
- $R_{\text{netto}}$: Profit at target, after round-trip commissions and slippage.
  - SPOT: $(T - E) / E - 2 \times c_{\text{rate}} - 2 \times s_{\text{bps}} / 10000$
  - FUTURES: $(T - E) / E - 2 \times c_{\text{rate}} - 2 \times s_{\text{bps}} / 10000 - f_{\text{bps}}$ (hold time)
- $L_{\text{netto}}$: Loss at stop, after entry costs (no exit cost since SL is hard stop).
  - $(E - SL) / E - c_{\text{rate}}$
- $C_{\text{fricción}}$: All fees already included in $R_{\text{netto}}$ and $L_{\text{netto}}$.

**Regime-aware scaling** (optional, to align utility magnitudes across regimes):

$$
U_{\text{adjusted}} = U_{\text{netto}} \times \text{regime\_confidence} \times \text{profile\_multiplier}
$$

- `regime_confidence`: How certain we are about the active regime (0.5–1.0).
- `profile_multiplier`: A/B/C profile scaling (0.88, 1.0, 1.18 for C/B/A respectively).

**Selection rule**:
1. Build theses for all eligible (side, instrument) pairs.
2. Compute $U_{\text{adjusted}}$ for each thesis.
3. Select thesis with highest $U_{\text{adjusted}}$.
4. If highest < `min_utility_threshold` (default 0.5%), **ABSTAIN** (no trade).
5. If multiple theses within 1% of top, mark as `ambiguous=True` and require additional confirmation (e.g., SR event or vol anomaly).

---

## 6. Execution Flow (Unified Entry Decision)

```
┌─────────────────────────────────────────────────────┐
│ 1. Symbol passes screener + signal generation       │
│    → Snapshot frozen (common to all theses)         │
└─────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────┐
│ 2. Build all viable theses (LONG Spot, SHORT Fut,   │
│    etc.) in parallel from snapshot                  │
│    → Each thesis: eligible, utility_adjusted        │
└─────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────┐
│ 3. Validate each thesis (margin, instrument avail,  │
│    exchange API, capital constraints)               │
│    → Update rejected thesis.rejection_codes         │
└─────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────┐
│ 4. Select thesis with max U_adjusted                │
│    Filter by min_utility_threshold                  │
│    → Selected side (LONG/SHORT) or ABSTAIN          │
└─────────────────────────────────────────────────────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
        ┌──────────┐  ┌──────────┐  ┌──────────┐
        │ LONG OK  │  │ SHORT OK │  │ ABSTAIN  │
        └──────────┘  └──────────┘  └──────────┘
              │             │             │
              ▼             ▼             ▼
    Create ready_  Create ready_   No candidate
    candidate w/   candidate w/    persisted
    LONG thesis    SHORT thesis
```

---

## 7. Persistence and Audit Trail

### 7.1 ready_candidates (Enhanced)

```sql
CREATE TABLE ready_candidates (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    status VARCHAR(20),  -- ready, validated, opened, discarded, expired
    
    -- Thesis selection metadata
    thesis_snapshot_v1 JSONB,  -- {
                               --   long_thesis: {...},
                               --   short_thesis: {...},
                               --   selected_side: "LONG",
                               --   selection_utilities: {...}
                               -- }
    
    selected_side VARCHAR(10),  -- "LONG", "SHORT", or null (ABSTAIN)
    trade_type VARCHAR(20),  -- "spot", "spot_margin", "futures"
    regime_mode VARCHAR(20),
    
    -- Selection evidence
    long_utility FLOAT,  -- Long U_adjusted or null if ineligible
    short_utility FLOAT,
    selected_utility FLOAT,
    utility_ratio FLOAT,  -- max / second-best (ambiguity metric)
    
    -- Theses (alternative: store complete thesis objects)
    long_thesis_reject_codes TEXT[],
    short_thesis_reject_codes TEXT[],
    
    signal_meta JSONB,  -- From screener/signals (snapshot frozen here)
    data JSONB,  -- Lifecycle: checks, gates, execution attempts
    
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    opened_at TIMESTAMP,
    opened_trade_id BIGINT,
    discarded_at TIMESTAMP
);
```

### 7.2 Audit Record

Every thesis evaluation produces an audit entry:

```python
{
    "thesis_evaluation_v1": {
        "timestamp": datetime,
        "symbol": "BTCUSDT",
        "regime_mode": "RANGE_SCALP",
        
        "long_thesis": {
            "trade_type": "spot",
            "eligible": True,
            "utility_netto": 0.0032,
            "utility_adjusted": 0.0026,
            "rejection_codes": [],
            "features": {
                "vol_spike": 1.8,
                "rsi_1h": 35.2,
                "bb_position": 0.15,
                "sr_event": "support_bounce_buy"
            }
        },
        
        "short_thesis": {
            "trade_type": "futures",
            "eligible": False,
            "utility_netto": None,
            "rejection_codes": ["BB_POSITION_TOO_LOW", "RSI_EXTREME_LOW"]
        },
        
        "selection": {
            "selected_side": "LONG",
            "selected_utility": 0.0026,
            "best_alternative": "SHORT",
            "alternative_utility": None,
            "ambiguous": False,
            "reason": "LONG_CLEARLY_SUPERIOR"
        }
    }
}
```

---

## 8. Instrument Eligibility by Regime

### 8.1 Routing Decision Tree

```
Regime ← Current market regime (BTC 7d/30d, EMA, BB)

if regime == SPOT_BULL:
    ├─ LONG Spot:      Enabled (default)
    ├─ LONG Spot Margin: Enabled (2x, higher kelly)
    ├─ LONG Futures:   Enabled (2x, higher kelly)
    ├─ SHORT Spot:     Disabled
    ├─ SHORT Spot Margin: Disabled (rare transition only, low kelly)
    └─ SHORT Futures:  Disabled

elif regime == RANGE_SCALP:
    ├─ LONG Spot:      Enabled (mean reversion)
    ├─ LONG Spot Margin: Enabled
    ├─ LONG Futures:   Enabled
    ├─ SHORT Spot:     Enabled
    ├─ SHORT Spot Margin: Enabled
    └─ SHORT Futures:  Enabled (symmetric)

elif regime == FUTURES_SHORT:
    ├─ LONG Spot:      Disabled
    ├─ LONG Spot Margin: Disabled
    ├─ LONG Futures:   Emergent only (RSI <32, disabled by default)
    ├─ SHORT Spot:     Disabled
    ├─ SHORT Spot Margin: Enabled (bounces)
    └─ SHORT Futures:  Enabled (primary)

elif regime == PRESERVATION:
    ├─ LONG Spot (narrative): Enabled
    ├─ LONG Spot Margin: Disabled
    ├─ LONG Futures: Disabled
    ├─ SHORT Spot:  Disabled
    ├─ SHORT Spot Margin: Disabled
    └─ SHORT Futures: Disabled
```

### 8.2 Cross-Regime Constraints

**Margin availability** (Binance account settings):
- SPOT_MARGIN only if `isolated_margin_enabled=True` (per symbol) and `margin_ratio <= 66%`.
- Fallback: if margin unavailable, LONG Spot Margin thesis returns `eligible=False, rejection_code="MARGIN_UNAVAILABLE"`.

**Futures availability** (Account & symbol):
- FUTURES only if `futures_enabled=True` (global).
- Fallback: LONG/SHORT Futures theses return `eligible=False, rejection_code="FUTURES_DISABLED"`.

---

## 9. Measurement and Validation

### 9.1 KPIs by Dimension

| KPI | Target | Window | Numerator | Denominator |
|---|---|---|---|---|
| Avg Utility (opened trades) | > Baseline +5% | 30 trades | Sum(utility_adjusted at entry) | # opened |
| PnL Netto | Baseline + 10% | 7 days | Sum(final_pnl) | N/A |
| Utility vs Realized PnL correlation | > 0.65 | 100+ trades | Pearson(U_adj, PnL%) | N/A |
| Ambiguous trades (U_max ≈ U_2nd) | < 10% | 30 trades | # where utility_ratio 0.95-1.05 | # total |
| Rejected side (would have won) | < 15% | 30 trades | # where unchosen side beats chosen | # total |
| Abstention rate | 15–25% | 30 trades | # ABSTAIN | # candidates |
| Instrument distribution | regime-aware | 30 trades | Count by (trade_type, side, regime) | # total |

### 9.2 Regression Detection

**Fail condition** (automatic rollback):
- Win rate drops >5% vs baseline.
- Any instrument class goes negative for 5+ consecutive days.
- Ambiguous selection rate spikes >40% (indicates tuning failure).

**Gradual rollout** (canary):
- Phase 0: Shadow mode only (audit trail, no decisions enforced).
- Phase 1: Apply selection to 10% of candidates (canary cohort, explicit marking).
- Phase 2: 50% if Phase 1 passed (Sharpe ratio >= baseline).
- Phase 3: 100% once 200+ trades validated across all regimes.

---

## 10. Transition Plan (From Current State)

### 10.1 Current Architecture Issues

1. `expected_side` baked into candidate early; direction FSM confirms that pre-chosen side.
2. No common market snapshot; LONG and SHORT evaluated at different times.
3. Utility comparison incomplete: no normalized metric across instruments.
4. No explicit ABSTAIN mechanism; weak theses enter and SL recovers.

### 10.2 Phased Implementation

**Phase A (Week 1): Instrumentation**
- Add `thesis_snapshot_v1` field to `ready_candidates`.
- Implement thesis-building functions (4.1-4.4) without changing entry decisions.
- Persist audit trail for all theses (both sides).
- All existing logic runs unchanged; new data is logged only.

**Phase B (Week 2): Shadow Thesis Selection**
- Compute utility for all theses.
- Compare selected (current) vs. utility-optimal side.
- Log divergences and measure frequency.
- No behavior change; visibility only.

**Phase C (Week 3-4): Controlled Rollout**
- Activate opportunity selection for RANGE_SCALP (most liquid regime).
- Start with canary (10% of candidates).
- Measure KPIs (§9.1) vs. baseline daily.
- Gradually increase to 50%, then 100% if KPIs positive.

**Phase D (Week 5+): Other Regimes**
- Extend to SPOT_BULL, FUTURES_SHORT, PRESERVATION in order.
- Use learnings from RANGE_SCALP tuning.

### 10.3 Rollback Strategy

```
# Immediate: revert to pre-selection behavior
PATCH /config/kelly {
    "opportunity_arbiter_enabled": False,
    "thesis_building_enabled": True,  # Keep logging
    "selection_mode": "legacy"  # Revert to current pre-chosen side
}

# Detailed rollback: restore old `entry_profile_gate` routing
git checkout HEAD~1 -- run_trades.py staged_entry.py
docker compose restart app
```

---

## 11. Edge Cases and Tolerances

### 11.1 Data Quality

**Scenario**: Snapshot is stale (older than `signal_ttl_seconds`).
- **Action**: Refresh snapshot before thesis construction. Block theses if refresh fails.

**Scenario**: One side has complete data, other is missing key features.
- **Action**: Both theses constructed; missing-feature thesis gets quality penalty (lower utility).

**Scenario**: No theses eligible (both sides rejected).
- **Action**: ABSTAIN. Log `no_eligible_thesis` audit record.

### 11.2 Market Constraints

**Scenario**: Exchange API for one instrument is down (e.g., spot API slow, futures fast).
- **Action**: Thesis for unavailable instrument gets `eligible=False, rejection_code="INSTRUMENT_UNAVAILABLE"`. Other side proceeds if eligible.

**Scenario**: Margin ratio is 65% (almost full).
- **Action**: LONG Spot Margin thesis rejected automatically; LONG Spot or Futures can proceed.

**Scenario**: Two theses have identical utility (numerically tied).
- **Action**: Prefer lower-risk instrument (SPOT > SPOT_MARGIN > FUTURES) or existing trend direction.

---

## 12. Example Walkthrough: ASTER USDT

### Scenario
Regime: RANGE_SCALP | Symbol: ASTERUSDT | Time: 2026-09-11 14:30 UTC

### Snapshot (frozen)
```python
{
    "current_price": 0.7840,
    "support_level": 0.7620,
    "resistance_level": 0.8050,
    "bb_lower": 0.7640,
    "bb_upper": 0.8020,
    "bb_position": 0.28,  # Near lower band
    "rsi_1h": 38.5,  # Normal
    "vol_spike_1h": 1.65,
    "trend_4h": "FLAT",
    "atr_pct_1h": 0.0082,
    "regime_confidence": 0.75,
}
```

### Theses Built

**LONG Spot**
- Anchor (support): 0.7620
- Target (resistance): 0.8050
- Reward: (0.8050 - 0.7840) / 0.7840 = 2.68%
- Loss: 0.0082 × 4.0 = 3.28%
- Commission/slippage round trip: 0.4%
- P(success): 0.55
- U_netto = 0.55 × (2.68% - 0.2%) - 0.45 × (3.28% + 0.2%) = **+0.82%**
- U_adjusted = 0.82% × 0.75 × 1.0 = **+0.615%**

**SHORT Futures**
- Anchor (resistance): 0.8050
- Target (support): 0.7620
- Reward: (0.7840 - 0.7620) / 0.7840 = 2.81%
- Loss: 0.0082 × 4.0 = 3.28%
- Commission/slippage + funding: 0.35%
- P(success): 0.50 (no strong rejection signal)
- U_netto = 0.50 × (2.81% - 0.175%) - 0.50 × (3.28% + 0.175%) = **-0.645%**
- U_adjusted = -0.645% × 0.75 × 1.0 = **-0.484%** → **Rejected**

**SHORT Spot**
- Similar to SHORT Futures but lower fees.
- U_netto = 0.50 × (2.81% - 0.2%) - 0.50 × (3.28% + 0.2%) = **-0.785%** → **Rejected**

### Selection
- LONG Spot: U_adj = +0.615% ✓
- SHORT options: All negative.
- **Selected**: LONG Spot
- **Reason**: Only eligible thesis with positive utility.

### Outcome
- Position opened: ASTERUSDT LONG 1 USDT (simplified).
- If price reaches 0.8050: +$0.027 (matches prediction).
- If SL hit at 0.7560: -$0.035 (below prediction due to slippage).

---

## 13. Next Steps

1. **Implement thesis builders** (§4) for all regimes in `lib/opportunity_thesis.py`.
2. **Add utility calculator** (§5) in `lib/utility_score.py`.
3. **Update `run_trades.py`** to call thesis selection instead of pre-choosing side.
4. **Add `thesis_snapshot_v1` persistence** to `ready_candidates`.
5. **Test Phase A & B** against live Staging data (no behavior change).
6. **Measure (§9.1)** before Phase C rollout.

---

## Appendix: Configuration Defaults

```yaml
# config.py addition
opportunity_arbiter:
  enabled: False  # Phase A: instrumentation only
  
  # Utility thresholds
  min_utility_threshold_bps: 50  # 0.5% minimum
  ambiguity_threshold_ratio: 0.95  # utilities within 5% → ambiguous
  
  # Per-regime overrides
  regimes:
    SPOT_BULL:
      kelly_multiplier_spot: 1.05
      kelly_multiplier_spot_margin: 1.2
      kelly_multiplier_futures: 1.2
      
    RANGE_SCALP:
      kelly_multiplier_spot: 1.0
      kelly_multiplier_spot_margin: 1.1
      kelly_multiplier_futures: 1.0
      
    FUTURES_SHORT:
      kelly_multiplier_spot_margin: 1.0
      kelly_multiplier_futures: 1.0
      kelly_multiplier_futures_long: 0.3
      
    PRESERVATION:
      kelly_multiplier_spot: 0.6
  
  # Thesis construction
  thesis_building_enabled: True
  snapshot_ttl_seconds: 240
  
  # Selection and rollout
  selection_mode: "utility"  # or "legacy" (current behavior)
  canary_fraction: 0.10  # Phase C: start at 10%
  canary_rollout_enabled: False
```

