from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from unified_governance_supervisor import UnifiedGovernanceContext, evaluate


def _settings(**overrides):
    base = {
        "unified_governance_enabled": True,
        "unified_governance_profit_enabled": True,
        "unified_governance_min_profit_hold_minutes": 0.0,
        "ug_allow_partial_to_full_escalation": True,
        "ug_partial_to_full_cap_pct": 0.75,
        "ug_partial_to_full_escalation_min_signal_strength": 0.58,
        "ug_partial_to_full_escalation_min_velocity_pct_h": 0.08,
        "ug_partial_to_full_escalation_cut_signal_strength": 0.62,
        "ug_partial_to_full_escalation_cut_velocity_pct_h": 0.12,
        "profit_realization_enabled": True,
        "profit_realization_futures_enabled": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _base_context(now):
    return UnifiedGovernanceContext(
        symbol="ETHUSDT",
        trade_type="futures",
        side="SELL",
        pnl_pct=0.012,
        pnl_usdt=12.0,
        entry_price=100.0,
        current_price=98.8,
        opened_at=now - timedelta(minutes=80),
        metadata={},
        regime_mode="FUTURES_SHORT",
        profile_label="B",
        pnl_velocity_1h=0.0012,
        current_signal_strength=0.72,
        current_signal_direction="SELL",
        sso_advisory_should_close=True,
        sso_advisory_close_pct=0.50,
        sso_advisory_reason="sso_target_contribution",
        sso_advisory_trigger="SSO-4",
        prs_advisory_should_close=True,
        prs_advisory_close_pct=1.0,
        prs_advisory_reason="profit_realization_supervisor_full",
        prs_advisory_detail="prs suggests full close",
    )


def test_sso_partial_plus_prs_full_is_capped_when_follow_through_is_alive():
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    ctx = _base_context(now)

    decision = evaluate(ctx, _settings(), now=now)

    assert decision.should_close is True
    assert decision.close_pct == 0.75
    assert "follow-through guard" in decision.detail


def test_sso_partial_plus_prs_full_escalates_when_regression_risk_is_high():
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    ctx = _base_context(now)
    ctx.current_signal_direction = "BUY"
    ctx.current_signal_strength = 0.82
    ctx.pnl_velocity_1h = -0.0018

    decision = evaluate(ctx, _settings(), now=now)

    assert decision.should_close is True
    assert decision.close_pct == 1.0
    assert "escalated by PRS" in decision.detail
