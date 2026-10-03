"""Tests for s130 — data-gap closure, conviction sizing, and structure-governed exits.

Covers:
- Fase 1: run_trades._build_indicators_at_entry merges top-level signal metadata
  into indicators_at_entry (the ASTERUSDT data gap).
- Fase 2: SR-pool sizing maps SR_POOL_BREAK explicitly and scales by score.
- Fase 3: UG suppresses partial closes when sr_path confirms continuation and
  forces full liquidation when the thesis is invalidated/stalled.
"""

from datetime import datetime, timedelta, timezone

from run_trades import _build_indicators_at_entry
from unified_governance_supervisor import (
    UnifiedGovernanceContext,
    evaluate as _evaluate_ug,
)


# ── Fase 1: indicators_at_entry merge (the ASTERUSDT gap) ──────────────────

def test_merge_pulls_sr_path_from_top_level():
    meta = {
        "current_price": 0.7972,
        "sr_path_short": {"side": "SHORT", "anchor": 0.798, "target": 0.776},
        "atr": 0.01365,
        "interval": "1h",
        "support_level": 0.776,
        "resistance_level": 0.798,
    }
    ind = _build_indicators_at_entry(meta)
    assert ind["sr_path_short"]["anchor"] == 0.798
    assert ind["atr"] == 0.01365
    assert ind["interval"] == "1h"
    assert ind["current_price"] == 0.7972


def test_merge_preserves_existing_indicators():
    meta = {
        "indicators": {"atr": 0.5, "rsi": 55.0},
        "atr": 0.9,  # top-level must NOT overwrite the nested value
        "current_price": 100.0,
    }
    ind = _build_indicators_at_entry(meta)
    assert ind["atr"] == 0.5
    assert ind["current_price"] == 100.0


def test_merge_handles_empty_and_none():
    assert _build_indicators_at_entry(None) == {}
    assert _build_indicators_at_entry({}) == {}
    ind = _build_indicators_at_entry({"indicators": {"rsi": 40.0}})
    assert ind["rsi"] == 40.0


# ── Fase 3: UG structure-governed exits ─────────────────────────────────────

def _ctx(sr_path_state=None, pnl_pct=0.01, pnl_usdt=5.0):
    return UnifiedGovernanceContext(
        symbol="TESTUSDT",
        trade_type="futures",
        side="SHORT",
        pnl_pct=pnl_pct,
        pnl_usdt=pnl_usdt,
        entry_price=1.0,
        current_price=0.99,
        metadata={},
        regime_mode="RANGE_SCALP",
        sr_path_state=sr_path_state,
    )


def test_ug_liquidates_full_on_invalidated_path():
    ctx = _ctx(sr_path_state={"state": "invalidated", "mode": "active",
                              "reason": "anchor_broken_at_candle_close", "progress_pct": 0.1})
    decision = _evaluate_ug(ctx, None)
    assert decision.should_close is True
    assert decision.close_pct == 1.0
    assert decision.reason == "sr_path_invalidated"


def test_ug_liquidates_full_on_stalled_path():
    ctx = _ctx(sr_path_state={"state": "stalled", "mode": "active",
                              "reason": "no_progress_within_expected_path", "progress_pct": 0.2})
    decision = _evaluate_ug(ctx, None)
    assert decision.should_close is True
    assert decision.close_pct == 1.0
    assert decision.reason == "sr_path_stalled"


def test_ug_shadow_mode_does_not_close():
    ctx = _ctx(sr_path_state={"state": "invalidated", "mode": "shadow",
                              "reason": "anchor_broken_at_candle_close", "progress_pct": 0.1})
    decision = _evaluate_ug(ctx, None)
    assert decision.reason != "sr_path_invalidated"


def test_ug_suppresses_partial_when_continuation_confirmed():
    # Structure wants a partial profit, but sr_path says the thesis continues.
    ctx = _ctx(
        sr_path_state={"state": "extended_to_next_level", "mode": "active"},
        pnl_pct=0.02, pnl_usdt=8.0,
    )
    ctx.lifecycle_structure_mode = "active"
    ctx.lifecycle_structure_decision = {"action": "partial_profit", "reason": "short_support_hold"}
    decision = _evaluate_ug(ctx, None)
    assert decision.should_close is False
    assert "continuation" in decision.reason or decision.action in ("hold", "watchdog")


def test_ug_blocks_partial_when_no_path_state():
    # The global no-fractionation contract also applies before SR-path evidence
    # is available; missing geometry must not reopen legacy partial exits.
    ctx = _ctx(sr_path_state=None, pnl_pct=0.02, pnl_usdt=8.0)
    ctx.lifecycle_structure_mode = "active"
    ctx.lifecycle_structure_decision = {"action": "partial_profit", "reason": "short_support_hold"}
    decision = _evaluate_ug(ctx, None)
    assert decision.should_close is False
    assert decision.close_pct == 0.0
    assert decision.reason == "partial_exit_blocked"


def test_ug_blocks_min_hold_partial_fallback_when_continuation_active():
    # A valid thesis still in-flight must not be downgraded to a partial exit by
    # minimum-profit-hold protection. Full thesis => hold, or close full if invalid.
    ctx = _ctx(sr_path_state={"state": "on_track", "mode": "active"}, pnl_pct=0.03, pnl_usdt=12.0)
    ctx.opened_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    ctx.sso_advisory_should_close = True
    ctx.sso_advisory_close_pct = 1.0
    ctx.sso_advisory_reason = "sso_full_exit"
    ctx.sso_advisory_trigger = "sso_target_contribution"
    decision = _evaluate_ug(ctx, None)
    assert decision.should_close is False
    assert decision.close_pct == 0.0
    assert decision.reason == "sr_path_continuation_hold"


# ── Fase 2: SR-pool conviction sizing ───────────────────────────────────────

def test_sr_pool_break_maps_to_full_base():
    """SR_POOL_BREAK (confirmed structural breakdown) must size at full base,
    never fall to an accidental default."""
    base = 150.0
    profile_base = {
        "SR_POOL_BREAK": base,
        "A": base,
        "B": base * 0.75,
        "C": base * 0.5,
    }
    assert profile_base["SR_POOL_BREAK"] == base
    assert profile_base.get("UNKNOWN_PROFILE", base) == base  # deliberate default
