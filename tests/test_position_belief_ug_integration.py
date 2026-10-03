from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from unified_governance_supervisor import UnifiedGovernanceContext, evaluate


def _settings(**overrides):
    base = {
        "unified_governance_enabled": True,
        "unified_governance_profit_enabled": True,
        "unified_governance_loss_enabled": True,
        "unified_governance_min_profit_hold_minutes": 0.0,
        "position_belief_enabled": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _profit_context(now, belief_last=None, monitor_only=False):
    if belief_last is not None:
        belief_last = {"evaluated_at": now.isoformat(), **belief_last}
    return UnifiedGovernanceContext(
        symbol="ETHUSDT",
        trade_type="futures",
        side="LONG",
        pnl_pct=0.02,
        pnl_usdt=10.0,
        entry_price=100.0,
        current_price=102.0,
        opened_at=now - timedelta(minutes=5),
        metadata={"belief_last": belief_last} if belief_last else {},
        regime_mode="SPOT_BULL",
        profile_label="B",
        monitor_only=monitor_only,
        sso_advisory_should_close=True,
        sso_advisory_close_pct=1.0,
        sso_advisory_reason="sso_decay",
        sso_advisory_trigger="SSO-1",
        prs_advisory_should_close=True,
        prs_advisory_close_pct=1.0,
        prs_advisory_reason="profit_realization_supervisor_full",
    )


def _loss_context(now, belief_last=None, monitor_only=False):
    if belief_last is not None:
        belief_last = {"evaluated_at": now.isoformat(), **belief_last}
    return UnifiedGovernanceContext(
        symbol="ETHUSDT",
        trade_type="futures",
        side="LONG",
        pnl_pct=-0.02,
        pnl_usdt=-15.0,
        entry_price=100.0,
        current_price=98.0,
        opened_at=now - timedelta(minutes=200),
        metadata={"belief_last": belief_last} if belief_last else {},
        regime_mode="SPOT_BULL",
        profile_label="B",
        monitor_only=monitor_only,
        distance_to_sl_pct=5.0,
    )


def test_belief_exhausted_forces_close_in_profit_branch():
    now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    ctx = _profit_context(now, belief_last={"belief_state": "exhausted", "thesis_complete": True, "reasons": ["adverse_evidence_no_favor"]})
    decision = evaluate(ctx, _settings(), now=now)
    assert decision.should_close is True
    assert decision.close_pct == 1.0
    assert decision.reason == "belief_exhausted"
    assert decision.priority == 99


def test_belief_exhausted_forces_close_in_loss_branch():
    now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    ctx = _loss_context(now, belief_last={"belief_state": "exhausted", "thesis_complete": True, "structural_stop_breached": True})
    decision = evaluate(ctx, _settings(), now=now)
    assert decision.should_close is True
    assert decision.reason == "belief_exhausted"


def test_belief_running_strong_holds_despite_sso_prs_wanting_close():
    now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    ctx = _profit_context(now, belief_last={"belief_state": "running_strong", "reasons": ["momentum_favorable"]})
    decision = evaluate(ctx, _settings(), now=now)
    assert decision.should_close is False
    assert decision.reason == "belief_running_strong"


def test_belief_running_strong_holds_in_loss_branch_despite_age():
    now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    ctx = _loss_context(now, belief_last={"belief_state": "running_strong", "reasons": ["momentum_favorable", "signal_aligned"]})
    decision = evaluate(ctx, _settings(), now=now)
    assert decision.should_close is False
    assert decision.reason == "belief_running_strong"


def test_belief_running_strong_does_not_override_monitor_only_degraded_mode():
    now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    ctx = _loss_context(
        now,
        belief_last={"belief_state": "running_strong"},
        monitor_only=True,
    )
    ctx.pnl_pct = -0.03
    decision = evaluate(ctx, _settings(unified_governance_monitor_only_force_loss_pct=2.0, unified_governance_monitor_only_force_age_minutes=90.0), now=now)
    # Falls through to existing monitor_only defensive close logic, not held by belief.
    assert decision.reason != "belief_running_strong"


def test_stale_belief_does_not_override_current_governance():
    now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    ctx = _profit_context(now, belief_last={
        "belief_state": "running_strong",
        "evaluated_at": (now - timedelta(minutes=10)).isoformat(),
    })
    decision = evaluate(ctx, _settings(position_belief_max_age_seconds=90.0), now=now)
    assert decision.reason != "belief_running_strong"


def test_belief_disabled_flag_falls_through_to_legacy_behavior():
    now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    ctx = _profit_context(now, belief_last={"belief_state": "exhausted"})
    decision = evaluate(ctx, _settings(position_belief_enabled=False), now=now)
    assert decision.reason != "belief_exhausted"


def test_incomplete_exhausted_belief_falls_through_to_legacy_behavior():
    now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    ctx = _profit_context(now, belief_last={"belief_state": "exhausted", "thesis_complete": False})
    decision = evaluate(ctx, _settings(), now=now)
    assert decision.reason != "belief_exhausted"


def test_belief_uncertain_falls_through_unchanged():
    now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    ctx = _profit_context(now, belief_last={"belief_state": "uncertain"})
    decision = evaluate(ctx, _settings(), now=now)
    assert decision.reason != "belief_exhausted"
    assert decision.reason != "belief_running_strong"


def test_missing_belief_falls_through_unchanged():
    now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    ctx = _profit_context(now, belief_last=None)
    decision = evaluate(ctx, _settings(), now=now)
    assert decision.reason != "belief_exhausted"
    assert decision.reason != "belief_running_strong"
