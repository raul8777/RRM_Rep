from position_belief import evaluate_belief, RUNNING_STRONG, EXHAUSTED, UNCERTAIN


THESIS_LONG = {
    "side": "LONG",
    "stop_price": 90.0,
    "target_price": 120.0,
    "support_level": 90.0,
    "resistance_level": 120.0,
    "entry_success_probability": 0.55,
}

THESIS_SHORT = {
    "side": "SHORT",
    "stop_price": 110.0,
    "target_price": 80.0,
    "support_level": 80.0,
    "resistance_level": 110.0,
    "entry_success_probability": 0.55,
}


def test_structural_stop_breach_forces_exhausted_long():
    result = evaluate_belief(
        side="LONG",
        pnl_usdt=-5.0,
        current_price=89.0,
        thesis_snapshot=THESIS_LONG,
        momentum={"velocity_per_sec": 0.001, "recovery_signal": True},
    )
    assert result["belief_state"] == EXHAUSTED
    assert result["structural_stop_breached"] is True
    assert "structural_stop_breached" in result["reasons"]


def test_structural_stop_breach_forces_exhausted_short():
    result = evaluate_belief(
        side="SHORT",
        pnl_usdt=-5.0,
        current_price=111.0,
        thesis_snapshot=THESIS_SHORT,
        momentum={"velocity_per_sec": 0.001, "recovery_signal": True},
    )
    assert result["belief_state"] == EXHAUSTED
    assert result["structural_stop_breached"] is True


def test_winner_running_strong_with_single_favorable_signal():
    result = evaluate_belief(
        side="LONG",
        pnl_usdt=5.0,
        current_price=100.0,
        thesis_snapshot=THESIS_LONG,
        momentum={"velocity_per_sec": 0.001, "recovery_signal": False},
    )
    assert result["belief_state"] == RUNNING_STRONG


def test_loser_requires_two_favorable_signals_for_running_strong():
    # Only momentum favorable — not enough evidence for a loser.
    result = evaluate_belief(
        side="LONG",
        pnl_usdt=-3.0,
        current_price=100.0,
        thesis_snapshot=THESIS_LONG,
        momentum={"velocity_per_sec": 0.001, "recovery_signal": False},
    )
    assert result["belief_state"] == UNCERTAIN

    # Momentum + signal aligned — enough for a loser to be running_strong.
    result2 = evaluate_belief(
        side="LONG",
        pnl_usdt=-3.0,
        current_price=100.0,
        thesis_snapshot=THESIS_LONG,
        momentum={"velocity_per_sec": 0.001, "recovery_signal": False},
        signal_direction="BUY",
        signal_strength=0.8,
    )
    assert result2["belief_state"] == RUNNING_STRONG


def test_any_adverse_evidence_blocks_running_strong():
    result = evaluate_belief(
        side="LONG",
        pnl_usdt=5.0,
        current_price=100.0,
        thesis_snapshot=THESIS_LONG,
        momentum={"velocity_per_sec": 0.001, "recovery_signal": False},
        signal_direction="SELL",
        signal_strength=0.8,
    )
    assert result["belief_state"] != RUNNING_STRONG


def test_exhausted_when_adverse_and_no_favor_with_two_signals():
    result = evaluate_belief(
        side="LONG",
        pnl_usdt=2.0,
        current_price=100.0,
        thesis_snapshot=THESIS_LONG,
        momentum={"velocity_per_sec": -0.001, "recovery_signal": False},
        signal_direction="SELL",
        signal_strength=0.8,
    )
    assert result["belief_state"] == EXHAUSTED


def test_near_stop_without_recovery_signal_demotes_to_uncertain():
    # Very close to structural stop, momentum "favorable" but no real recovery
    # bounce yet — should not be trusted as running_strong.
    close_thesis = dict(THESIS_LONG)
    result = evaluate_belief(
        side="LONG",
        pnl_usdt=1.0,
        current_price=90.4,  # 0.44% above stop_price=90.0
        thesis_snapshot=close_thesis,
        momentum={"velocity_per_sec": 0.001, "recovery_signal": False},
    )
    assert result["belief_state"] != RUNNING_STRONG


def test_near_stop_with_recovery_signal_can_be_running_strong():
    close_thesis = dict(THESIS_LONG)
    result = evaluate_belief(
        side="LONG",
        pnl_usdt=1.0,
        current_price=90.4,
        thesis_snapshot=close_thesis,
        momentum={"velocity_per_sec": 0.001, "recovery_signal": True},
    )
    assert result["belief_state"] == RUNNING_STRONG


def test_no_evidence_at_all_returns_uncertain():
    result = evaluate_belief(
        side="LONG",
        pnl_usdt=0.0,
        current_price=100.0,
        thesis_snapshot=None,
        momentum=None,
    )
    assert result["belief_state"] == UNCERTAIN
    assert result["structural_stop_breached"] is False


def test_missing_thesis_never_breaches_structural_stop():
    result = evaluate_belief(
        side="LONG",
        pnl_usdt=-100.0,
        current_price=1.0,
        thesis_snapshot=None,
        momentum={"velocity_per_sec": -0.01, "recovery_signal": False},
    )
    assert result["structural_stop_breached"] is False


def test_missing_thesis_cannot_exhaust_from_momentum_and_signal():
    result = evaluate_belief(
        side="LONG",
        pnl_usdt=-10.0,
        current_price=100.0,
        thesis_snapshot=None,
        momentum={"velocity_per_sec": -0.001, "recovery_signal": False},
        signal_direction="SELL",
        signal_strength=0.8,
    )

    assert result["belief_state"] == UNCERTAIN
    assert result["thesis_complete"] is False
    assert result["reasons"] == ["thesis_incomplete"]
