"""lifecycle/base.py — contracts and data classes for the trade lifecycle kernel.

The kernel is intentionally abstract: it knows nothing about Binance, SQLite,
Docker or the existing modules. It consumes MarketContext and LifecycleConfig and
produces decisions (Signal, EntryDecision, SizedTrade, ExitDecision). Adapters
are responsible for execution and persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Protocol, Tuple
from enum import Enum


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class Action(str, Enum):
    HOLD = "HOLD"
    CLOSE_PCT = "CLOSE_PCT"
    CLOSE_ALL = "CLOSE_ALL"


@dataclass(frozen=True)
class MarketContext:
    """Snapshot of market data used by the lifecycle kernel."""
    timestamp: float
    regime: str                       # dominant regime (SPOT_BULL, etc.)
    regime_confidence: Dict[str, float]  # confidence per regime
    btc_7d_pct: float
    btc_30d_pct: float
    btc_day_pct: float
    fear_greed_index: float
    btc_dominance: float
    funding_rate: Optional[float] = None
    altcoin_breadth: Optional[float] = None  # % of alts above 20d EMA


@dataclass(frozen=True)
class SignalPolicy:
    """Parameters that drive signal generation and ranking."""
    min_signal_strength: float
    min_success_probability: float
    min_expected_pnl: float
    min_liquidity_usdt: float
    categories_enabled: Tuple[str, ...]
    max_candidates: int
    diversification_mode: str = "category"  # "category" | "qubo" | "none"


@dataclass(frozen=True)
class EntryPolicy:
    """Declarative entry gate."""
    min_composite_score: float
    min_expected_pnl: float
    min_success_probability: float
    min_liquidity_usdt: float
    max_total_positions: int
    max_category_positions: Dict[str, int]
    btc_dominance_min: Optional[float] = None
    btc_dominance_max: Optional[float] = None
    funding_rate_max: Optional[float] = None
    regime_specific_rules: Dict[str, Callable] = field(default_factory=dict)
    volspike_override_enabled: bool = True


@dataclass(frozen=True)
class SizingPolicy:
    """Parameters for position sizing."""
    regime_allocation_pct: float       # % of liquid capital allocable to this regime
    max_per_trade_usdt: float
    kelly_base_pct: float
    kelly_max_pct: float
    conviction_max_pct: float
    category_caps: Dict[str, float]
    fng_capital_multiplier: bool = True


@dataclass(frozen=True)
class PartialTake:
    pct: float                         # % of position to close
    trigger_atr_mult: float
    label: str


@dataclass(frozen=True)
class MomentumExitRule:
    rsi_overbought: float
    rsi_oversold: float
    fng_extreme_greed: float
    fng_extreme_fear: float


@dataclass(frozen=True)
class StagnationRule:
    min_hours: float
    max_drawdown_from_peak_pct: float
    decay_score_threshold: float


@dataclass(frozen=True)
class ExitPolicy:
    """Declarative exit policy."""
    regime: str
    profile: str
    hard_sl_atr_mult: float
    hard_tp_atr_mult: float
    trailing_activation_pct: float
    trailing_atr_mult: float
    breakeven_activation_pct: float
    partial_takes: Tuple[PartialTake, ...]
    momentum_exit: MomentumExitRule
    stagnation_rule: StagnationRule


@dataclass(frozen=True)
class LifecycleConfig:
    """Complete configuration for one regime × profile."""
    regime: str
    profile: str
    signal_policy: SignalPolicy
    entry_policy: EntryPolicy
    sizing_policy: SizingPolicy
    exit_policy: ExitPolicy

    def fingerprint(self) -> str:
        """Stable string fingerprint for Super MIMIC / landscape comparison."""
        from hashlib import sha256
        payload = f"{self.regime}|{self.profile}|" + repr(
            (self.signal_policy, self.entry_policy, self.sizing_policy, self.exit_policy)
        )
        return sha256(payload.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: Side
    category: str
    composite_score: float
    expected_pnl: float
    success_probability: float
    signal_strength: float
    regime_alignment: float
    liquidity_usdt_24h: float
    features: Dict[str, float] = field(default_factory=dict)
    source: str = "autonomous"


@dataclass(frozen=True)
class EntryDecision:
    signal: Signal
    approved: bool
    reason: Optional[str] = None
    entry_price: Optional[float] = None
    atr: Optional[float] = None


@dataclass(frozen=True)
class CapitalBudget:
    total_liquid: float
    regime_allocation_pct: float
    available_for_regime: float
    max_per_trade_usdt: float
    category_caps: Dict[str, float]
    already_deployed: Dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class SizedTrade:
    entry_decision: EntryDecision
    position_usdt: float
    quantity: float
    budget: CapitalBudget
    multipliers: Dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ExitDecision:
    symbol: str
    action: Action
    pct: float  # 0..1, only meaningful for CLOSE_PCT
    reason: str
    priority: int
    metadata: Dict[str, float] = field(default_factory=dict)

    @staticmethod
    def hold(symbol: str, reason: str = "hold") -> "ExitDecision":
        return ExitDecision(symbol=symbol, action=Action.HOLD, pct=0.0, reason=reason, priority=0)

    @staticmethod
    def close_pct(symbol: str, pct: float, reason: str, priority: int) -> "ExitDecision":
        return ExitDecision(symbol=symbol, action=Action.CLOSE_PCT, pct=pct, reason=reason, priority=priority)

    @staticmethod
    def close_all(symbol: str, reason: str, priority: int) -> "ExitDecision":
        return ExitDecision(symbol=symbol, action=Action.CLOSE_ALL, pct=1.0, reason=reason, priority=priority)


@dataclass
class Position:
    symbol: str
    side: Side
    quantity: float
    entry_price: float
    entry_time: float
    position_usdt: float
    category: str
    regime_at_entry: str
    profile_at_entry: str
    metadata: Dict = field(default_factory=dict)
    peak_pnl_pct: float = 0.0


class LifecycleKernel(Protocol):
    """Protocol for a lifecycle kernel. Implementations must be stateless."""

    def evaluate_signals(self, ctx: MarketContext, config: LifecycleConfig) -> List[Signal]:
        ...

    def select_entries(self, signals: List[Signal], ctx: MarketContext, config: LifecycleConfig) -> List[EntryDecision]:
        ...

    def size_position(self, entry: EntryDecision, budget: CapitalBudget, ctx: MarketContext, config: LifecycleConfig) -> SizedTrade:
        ...

    def evaluate_exits(self, positions: List[Position], ctx: MarketContext, config: LifecycleConfig) -> List[ExitDecision]:
        ...
