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
from lifecycle.profile_classifier import classify_profile
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
        self.symbol_profile_map: Dict[str, str] = {}

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

        profile_mode = bool(
            config.profile_thresholds and config.profile_sizing_policies
        )

        executed_entries: List[Dict] = []
        for entry in entries:
            assigned_profile: Optional[str] = None
            sizing_policy = config.sizing_policy
            if profile_mode:
                assigned_profile = classify_profile(
                    entry.signal,
                    ctx,
                    config.profile_order,
                    config.profile_thresholds,
                )
                if assigned_profile is None:
                    continue
                sizing_policy = config.profile_sizing_policies.get(
                    assigned_profile, config.sizing_policy
                )
                # Per-profile position cap and budget
                open_positions = self.adapter.get_positions(config)
                profile_count = sum(
                    1
                    for p in open_positions
                    if self.symbol_profile_map.get(p.symbol) == assigned_profile
                )
                if profile_count >= sizing_policy.max_total_positions:
                    continue
                profile_positions = [
                    p
                    for p in open_positions
                    if self.symbol_profile_map.get(p.symbol) == assigned_profile
                ]
                budget = self.position_sizer.build_budget(
                    total_liquid, profile_positions, sizing_policy
                )
            else:
                budget = self.position_sizer.build_budget(
                    total_liquid, positions, config.sizing_policy
                )

            trade = self.position_sizer.size(entry, budget, ctx, sizing_policy)
            if trade.position_usdt <= 0:
                continue
            result = self.adapter.execute_entry(trade, config)
            self.adapter.audit(entry, result, config)
            if assigned_profile:
                self.symbol_profile_map[entry.signal.symbol] = assigned_profile
            executed_entries.append({
                "symbol": entry.signal.symbol,
                "position_usdt": trade.position_usdt,
                "quantity": trade.quantity,
                "profile": assigned_profile or config.profile,
                "result": result,
                "signal": {
                    "side": entry.signal.side.value,
                    "category": entry.signal.category,
                    "composite_score": entry.signal.composite_score,
                    "expected_pnl": entry.signal.expected_pnl,
                    "success_probability": entry.signal.success_probability,
                    "signal_strength": entry.signal.signal_strength,
                    "regime_alignment": entry.signal.regime_alignment,
                    "features": dict(entry.signal.features or {}),
                },
                "sizing": {
                    "conviction": trade.multipliers.get("conviction"),
                    "fng_multiplier": trade.multipliers.get("fng_multiplier"),
                },
            })
            # Refresh positions for the next iteration
            positions = self.adapter.get_positions(config)

        # ── Exit side ───────────────────────────────────────────────────────
        positions = self.adapter.get_positions(config)
        positions = update_peak_pnl(positions, self.exit_engine.price_resolver)

        # Per-profile exit policies: if a profile is assigned, use its exit policy.
        if config.profile_exit_policies:
            exits: List[ExitDecision] = []
            for pos in positions:
                assigned = self.symbol_profile_map.get(pos.symbol)
                policy = config.profile_exit_policies.get(
                    assigned, config.exit_policy
                )
                exits.extend(
                    self.exit_engine.evaluate([pos], ctx, policy, now=ctx.timestamp)
                )
        else:
            exits = self.exit_engine.evaluate(positions, ctx, config.exit_policy, now=ctx.timestamp)

        executed_exits: List[Dict] = []
        exit_reasons: Dict[str, int] = {}
        realized_pnl_usdt = 0.0
        win_count = 0
        loss_count = 0
        for decision in exits:
            if decision.action.value == "HOLD":
                continue
            pos = next((p for p in positions if p.symbol == decision.symbol), None)
            if pos is None:
                continue
            result = self.adapter.execute_exit(decision, pos, config)
            self.adapter.audit(decision, result, config)
            if decision.action.value in ("CLOSE_ALL", "SELL"):
                self.symbol_profile_map.pop(decision.symbol, None)
            exit_record = {
                "symbol": decision.symbol,
                "action": decision.action.value,
                "pct": decision.pct,
                "reason": decision.reason,
                "result": result,
                "realized_pnl_usdt": result.get("realized_pnl_usdt", 0.0),
            }
            executed_exits.append(exit_record)

            # Aggregate exit analytics.
            reason = decision.reason or "unknown"
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
            pnl = result.get("realized_pnl_usdt", 0.0)
            realized_pnl_usdt += pnl
            if pnl > 0:
                win_count += 1
            elif pnl < 0:
                loss_count += 1

        total_exit_events = win_count + loss_count
        profit_factor: Optional[float] = None
        if total_exit_events > 0:
            total_wins = sum(e.get("realized_pnl_usdt", 0.0) for e in executed_exits if e.get("realized_pnl_usdt", 0.0) > 0)
            total_losses = abs(sum(e.get("realized_pnl_usdt", 0.0) for e in executed_exits if e.get("realized_pnl_usdt", 0.0) < 0))
            if total_losses > 0:
                profit_factor = round(total_wins / total_losses, 4)
            elif total_wins > 0:
                profit_factor = float("inf")

        return {
            "regime": ctx.regime,
            "regime_confidence": ctx.regime_confidence,
            "signals": len(signals),
            "entries": executed_entries,
            "exits": executed_exits,
            "exit_reasons": exit_reasons,
            "realized_pnl_usdt": round(realized_pnl_usdt, 6),
            "win_count": win_count,
            "loss_count": loss_count,
            "profit_factor": profit_factor,
        }
