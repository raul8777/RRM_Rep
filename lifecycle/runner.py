"""lifecycle/runner.py — LifecycleRunner orchestrates the full pipeline.

Given a LifecycleConfig and an ExecutionAdapter, the runner executes one full
lifecycle cycle:

    1. Build MarketContext
    2. Detect / confirm regime
    3. Generate signals
    4. Run entry gate
    5. Size approved entries
    6. Execute entries
    7. Evaluate exits for open positions
    8. Execute exits

The runner is stateless and deterministic; all mutable state lives in the
adapter (positions, orders, audit log).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from lifecycle.adapter import ExecutionAdapter
from lifecycle.base import (
    EntryDecision,
    ExitDecision,
    LifecycleConfig,
    MarketContext,
    Position,
    Signal,
    SignalPolicy,
)
from lifecycle.entry import EntryGate
from lifecycle.exit import ExitEngine, update_peak_pnl
from lifecycle.signal import SignalPipeline
from lifecycle.sizing import PositionSizer


class LifecycleRunner:
    """Orchestrates one lifecycle cycle."""

    def __init__(
        self,
        adapter: ExecutionAdapter,
        signal_pipeline: SignalPipeline,
        entry_gate: EntryGate,
        position_sizer: PositionSizer,
        exit_engine: ExitEngine,
    ):
        self.adapter = adapter
        self.signal_pipeline = signal_pipeline
        self.entry_gate = entry_gate
        self.position_sizer = position_sizer
        self.exit_engine = exit_engine

    def run_cycle(
        self,
        analyses: List[Dict[str, Any]],
        config: LifecycleConfig,
        total_liquid: float,
    ) -> Dict[str, Any]:
        """Run one full cycle and return a summary."""
        ctx = self.adapter.build_context(config)
        positions = self.adapter.get_positions(config)

        # Update peak PnL before exit evaluation
        positions = update_peak_pnl(positions, self.exit_engine.price_resolver)

        # ── Entry side ──────────────────────────────────────────────────────
        signals = self.signal_pipeline.evaluate(analyses, ctx, config.signal_policy)
        entries = self.entry_gate.evaluate(signals, ctx, config.entry_policy, positions)
        budget = self.position_sizer.build_budget(total_liquid, positions, config.sizing_policy)

        executed_entries: List[Dict] = []
        for entry in entries:
            trade = self.position_sizer.size(entry, budget, ctx, config.sizing_policy)
            if trade.position_usdt <= 0:
                continue
            result = self.adapter.execute_entry(trade, config)
            self.adapter.audit(entry, result, config)
            executed_entries.append({
                "symbol": entry.signal.symbol,
                "position_usdt": trade.position_usdt,
                "quantity": trade.quantity,
                "result": result,
            })
            # Refresh budget and positions after execution
            budget = self.position_sizer.build_budget(
                total_liquid,
                self.adapter.get_positions(config),
                config.sizing_policy,
            )

        # ── Exit side ───────────────────────────────────────────────────────
        positions = self.adapter.get_positions(config)
        positions = update_peak_pnl(positions, self.exit_engine.price_resolver)
        exits = self.exit_engine.evaluate(positions, ctx, config.exit_policy, now=ctx.timestamp)

        executed_exits: List[Dict] = []
        for decision in exits:
            if decision.action.value == "HOLD":
                continue
            pos = next((p for p in positions if p.symbol == decision.symbol), None)
            if pos is None:
                continue
            result = self.adapter.execute_exit(decision, pos, config)
            self.adapter.audit(decision, result, config)
            executed_exits.append({
                "symbol": decision.symbol,
                "action": decision.action.value,
                "pct": decision.pct,
                "reason": decision.reason,
                "result": result,
            })

        return {
            "regime": ctx.regime,
            "regime_confidence": ctx.regime_confidence,
            "signals": len(signals),
            "entries": executed_entries,
            "exits": executed_exits,
        }
