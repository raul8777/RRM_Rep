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

__all__ = [
    "LifecycleConfig",
    "SignalPolicy",
    "EntryPolicy",
    "SizingPolicy",
    "ExitPolicy",
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
]
