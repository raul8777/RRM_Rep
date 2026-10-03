from types import SimpleNamespace

from Dev.scripts.capture_universal_entry_staging_preflight import evaluate_preflight


def _fixture():
    return {
        "status": {"environment": "staging", "binance_testnet": "True", "runtime_flags": {
            "APP_ENV": "staging", "BINANCE_USE_TESTNET": "true", "DRY_RUN": "false", "ALLOW_REAL_TRADING": "true",
        }},
        "control": {"pause_new_entries": {"active": False}, "unified_status": {"effective_state": "RUNNING_FULL"}},
        "capital": {
            "ok": True,
            "spot": {"status": "ok", "usdt": {"free": 1000.0, "locked": 0.0}},
            "futures": {"status": "ok", "assets": {"USDT": {"availableBalance": 500.0}}, "open_positions": []},
            "proactive_liquidity": {"spot": {"spendable_usdt": 900.0}, "futures": {"spendable_usdt": 400.0}},
        },
        "spot_positions": {"count": 0, "positions": []},
        "pilot_status": {
            "prospective_sandbox_filter_snapshot": {"filters_currentness_verified": True},
            "current_sandbox_commission_snapshot": {"current_account_commissions_verified": True},
            "data": {"development": {"complete": True}},
            "proposals": {"complete": True},
            "philosophy_generation_audit": {"row_by_row_cluster_review": {"passed": True}},
            "bilateral_contract_scaffold": {"source_integrity_verified": True},
            "bilateral_scaffold_outcomes": {"source_binding_matches": True},
            "replay_parity": {"parity_passed": True},
            "phases": {"contract_tape": "bilateral_venue_native_geometry_scaffold_ready_executable_tape_blocked"},
        },
        "settings": SimpleNamespace(
            max_per_trade_usdt=100.0, staging_pilot_max_order_notional_usdt=100.0,
            futures_usdt_per_trade=100.0, max_positions=1, max_positions_per_symbol=1,
            futures_max_new_per_cycle=1, max_positions_spot_bull=1,
            max_positions_range_scalp=1, max_positions_preservation=1,
            max_positions_futures_short=1, futures_max_shorts=1, futures_max_longs=1,
            futures_max_largecap_shorts=1,
            overflow_max_extra=0, futures_overflow_max_extra=0,
        ),
    }


def test_funded_staging_runtime_stays_blocked_without_cross_venue_position_guard():
    data = _fixture()
    report = evaluate_preflight(**data)

    assert report["checks"]["pilot_order_and_position_limits_enforced"] is False
    assert report["effective_pilot_limits"]["cross_venue_single_position_guard_verified"] is False
    assert report["runtime_preflight_passed"] is False
    assert report["offline_gates_ready"] is False
    assert report["canary_eligible"] is False
    assert report["orders_allowed"] is False
    assert report["orders_submitted"] is False


def test_open_futures_position_blocks_single_position_canary_preflight():
    data = _fixture()
    data["capital"]["futures"]["open_positions"] = [{"position_amt": 0.01}]

    report = evaluate_preflight(**data)

    assert report["checks"]["no_futures_positions_open"] is False
    assert report["runtime_preflight_passed"] is False
    assert report["orders_allowed"] is False


def test_oversized_effective_trade_and_position_limits_block_runtime_preflight():
    data = _fixture()
    data["settings"] = SimpleNamespace(
        max_per_trade_usdt=1500.0, staging_pilot_max_order_notional_usdt=0.0,
        futures_usdt_per_trade=150.0, max_positions=4, max_positions_per_symbol=1,
        futures_max_new_per_cycle=3, max_positions_spot_bull=8,
        max_positions_range_scalp=6, max_positions_preservation=6,
        max_positions_futures_short=5, futures_max_shorts=4, futures_max_longs=3,
        futures_max_largecap_shorts=3,
        overflow_max_extra=1, futures_overflow_max_extra=1,
    )

    report = evaluate_preflight(**data)

    assert report["checks"]["pilot_order_and_position_limits_enforced"] is False
    assert report["effective_pilot_limits"]["pilot_limits_verified"] is False
    assert report["runtime_preflight_passed"] is False
    assert report["orders_allowed"] is False