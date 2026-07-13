"""lifecycle — declarative, regime-aware trade lifecycle kernel.

This package replaces the scattered entry/exit/sizing logic in run_trades.py and
futures_client.py with a unified pipeline:

    signal → entry → sizing → exit

Each stage is driven by a LifecycleConfig (regime + profile) and audited through
the AuditLogger. Adapters connect the kernel to SPOT, FUTURES and MIMIC.
"""

from lifecycle.base import (
    LifecycleConfig,
    SignalPolicy,
    EntryPolicy,
    SizingPolicy,
    ExitPolicy,
    PartialTake,
    MomentumExitRule,
    StagnationRule,
    MarketContext,
    Signal,
    EntryDecision,
    SizedTrade,
    ExitDecision,
    Position,
    LifecycleKernel,
)

from lifecycle.regime import RegimeState, RegimeKernel, get_regime_v2
from lifecycle.signal import SignalPipeline, analysis_to_signal
from lifecycle.entry import EntryGate, count_positions_by_category
from lifecycle.sizing import PositionSizer, size_entry
from lifecycle.exit import ExitEngine, update_peak_pnl
from lifecycle.adapter import ExecutionAdapter
from lifecycle.runner import LifecycleRunner
from lifecycle.adapter_mimic import MimicExecutionAdapter
from lifecycle.super_mimic_integration import (
    lifecycle_config_to_dict,
    fingerprint,
    build_mimic_experiment_design,
    run_mimic_backtest,
    compute_metrics,
)

__all__ = [
    "LifecycleConfig",
    "SignalPolicy",
    "EntryPolicy",
    "SizingPolicy",
    "ExitPolicy",
    "PartialTake",
    "MomentumExitRule",
    "StagnationRule",
    "MarketContext",
    "Signal",
    "EntryDecision",
    "SizedTrade",
    "ExitDecision",
    "Position",
    "LifecycleKernel",
    "RegimeState",
    "RegimeKernel",
    "get_regime_v2",
    "SignalPipeline",
    "analysis_to_signal",
    "EntryGate",
    "count_positions_by_category",
    "PositionSizer",
    "size_entry",
    "ExitEngine",
    "update_peak_pnl",
    "ExecutionAdapter",
    "LifecycleRunner",
    "MimicExecutionAdapter",
    "lifecycle_config_to_dict",
    "fingerprint",
    "build_mimic_experiment_design",
    "run_mimic_backtest",
    "compute_metrics",
]
