import json
from types import SimpleNamespace
from unittest.mock import patch

from directional_arbiter import evaluate_range_scalp
from opportunity_contract import scale_contract
from run_trades import (
    _fast_path_authority_block_reason,
    _execute_validated_fast_path,
    _is_new_entries_paused,
    _maybe_open_range_scalp_shorts,
    _maybe_open_sr_pool_shorts,
    _merge_futures_short_analysis,
    _open_arbitrated_range_scalp_long,
    _open_arbitrated_range_scalp_short,
    _prepare_range_scalp_staging_candidates,
    _revalidate_entry_policy_after_arbitration_change,
    _entry_generation_payload,
)
import run_trades


def _settings(**overrides):
    values = dict(
        range_scalp_direction_mode="ev_symmetric",
        range_scalp_direction_min_probability=0.55,
        range_scalp_long_support_zone_max=0.35,
        range_scalp_short_resistance_threshold=0.65,
        range_scalp_long_entries_enabled=True,
        range_scalp_short_instrument="futures",
        spot_margin_short_enabled=False,
        max_loss_usd_per_trade=18.0,
        opportunity_contract_futures_entry_fee_rate=0.0002,
        opportunity_contract_futures_exit_fee_rate=0.0008,
        opportunity_contract_spot_entry_fee_rate=0.0005,
        opportunity_contract_spot_exit_fee_rate=0.001,
        slippage_pct=0.00052,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _prediction(side, probability=0.70, reward_pct=4.0, risk_pct=2.0, position_size=100.0):
    is_short = side == "SHORT"
    return {
        "expected_pnl": 1.0,
        "success_probability": probability,
        "position_size_usdt": position_size,
        "current_price": 100.0,
        "target_price": 100.0 * (1.0 - reward_pct / 100.0 if is_short else 1.0 + reward_pct / 100.0),
        "stop_price": 100.0 * (1.0 + risk_pct / 100.0 if is_short else 1.0 - risk_pct / 100.0),
        "trade_side": side,
    }


def _evaluate(meta, long, futures_long, short, settings=None, futures=True, short_executable=None):
    metadata = {"current_price": 100.0, **meta}
    return evaluate_range_scalp(
        "BTCUSDT", metadata, long, futures_long, short, futures,
        settings or _settings(), short_executable=short_executable,
    )


def test_fast_path_fails_closed_on_persisted_entry_pause(monkeypatch):
    db_client = object()
    gate_events = []
    monkeypatch.setattr(
        run_trades.staged_entry,
        "get_qubo_authority_snapshot",
        lambda *_args, **_kwargs: {
            "cycle_id": "qubo-cycle-1",
            "symbols_hash": "a" * 64,
            "symbols": ["BTCUSDT"],
        },
    )
    monkeypatch.setattr(run_trades, "_is_new_entries_paused", lambda _db: True)
    monkeypatch.setattr(
        run_trades,
        "_update_fast_path_gate",
        lambda db, ready_id, passed, reason, _balance: gate_events.append(
            (db, ready_id, passed, reason)
        ),
    )

    result = _execute_validated_fast_path(
        client=object(),
        settings=SimpleNamespace(staged_entry_fast_path_enabled=True),
        regime_mode="RANGE_SCALP",
        regime_obj=None,
        db_client=db_client,
        portfolio_manager=None,
        position_manager=None,
        rows=[{
            "id": 17,
            "symbol": "BTCUSDT",
            "qubo_cycle_id": "qubo-cycle-1",
            "qubo_symbols_hash": "a" * 64,
        }],
    )

    assert result == []
    assert gate_events == [(db_client, 17, False, "PAUSED_DB")]


def test_venue_executability_capture_preserves_unknown_separately_from_false():
    result = run_trades._venue_executability_by_mode([
        {"mode": "SPOT_LONG", "venue_executable_at_time": True},
        {"mode": "FUTURES_LONG", "venue_executable_at_time": False},
        {"mode": "FUTURES_SHORT", "venue_executable_at_time": None},
        {"mode": "SPOT_MARGIN_SHORT"},
    ])

    assert result == {
        "SPOT_LONG": True,
        "FUTURES_LONG": False,
        "FUTURES_SHORT": None,
        "SPOT_MARGIN_SHORT": None,
    }


def test_fast_path_fails_closed_without_qubo_cycle_provenance(monkeypatch):
    db_client = object()
    gate_events = []
    monkeypatch.setattr(run_trades, "_is_new_entries_paused", lambda _db: False)
    monkeypatch.setattr(
        run_trades,
        "_update_fast_path_gate",
        lambda db, ready_id, passed, reason, _balance: gate_events.append(
            (db, ready_id, passed, reason)
        ),
    )

    result = _execute_validated_fast_path(
        client=object(),
        settings=SimpleNamespace(staged_entry_fast_path_enabled=True),
        regime_mode="RANGE_SCALP",
        regime_obj=None,
        db_client=db_client,
        portfolio_manager=None,
        position_manager=None,
        rows=[{"id": 18, "symbol": "ETHUSDT"}],
    )

    assert result == []
    assert gate_events == [(db_client, 18, False, "NO_QUBO_AUTHORITY")]


def test_fast_path_fails_closed_when_qubo_authority_is_stale(monkeypatch):
    db_client = object()
    gate_events = []
    monkeypatch.setattr(
        run_trades.staged_entry,
        "get_qubo_authority_snapshot",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        run_trades,
        "_update_fast_path_gate",
        lambda db, ready_id, passed, reason, _balance: gate_events.append(
            (db, ready_id, passed, reason)
        ),
    )

    result = _execute_validated_fast_path(
        client=object(),
        settings=SimpleNamespace(staged_entry_fast_path_enabled=True),
        regime_mode="RANGE_SCALP",
        regime_obj=None,
        db_client=db_client,
        portfolio_manager=None,
        position_manager=None,
        rows=[{
            "id": 19,
            "symbol": "SOLUSDT",
            "qubo_cycle_id": "old-cycle",
            "qubo_symbols_hash": "c" * 64,
        }],
    )

    assert result == []
    assert gate_events == [(db_client, 19, False, "NO_QUBO_AUTHORITY")]


def test_fast_path_pre_submit_authority_recheck_blocks_pause_and_stale_cycle(monkeypatch):
    row = {
        "symbol": "BTCUSDT",
        "qubo_cycle_id": "cycle-current",
        "qubo_symbols_hash": "a" * 64,
    }
    monkeypatch.setattr(run_trades, "_is_new_entries_paused", lambda _db: False)
    monkeypatch.setattr(
        run_trades.staged_entry,
        "get_qubo_authority_snapshot",
        lambda *_args, **_kwargs: {
            "cycle_id": "cycle-current",
            "symbols_hash": "a" * 64,
            "symbols": ["BTCUSDT"],
        },
    )
    assert _fast_path_authority_block_reason(object(), "RANGE_SCALP", row) is None

    monkeypatch.setattr(run_trades, "_is_new_entries_paused", lambda _db: True)
    assert _fast_path_authority_block_reason(object(), "RANGE_SCALP", row) == "PAUSED_DB"

    monkeypatch.setattr(run_trades, "_is_new_entries_paused", lambda _db: False)
    monkeypatch.setattr(
        run_trades.staged_entry,
        "get_qubo_authority_snapshot",
        lambda *_args, **_kwargs: {
            "cycle_id": "cycle-newer",
            "symbols_hash": "b" * 64,
            "symbols": ["BTCUSDT"],
        },
    )
    assert _fast_path_authority_block_reason(object(), "RANGE_SCALP", row) == "NO_QUBO_AUTHORITY"


def test_entry_pause_is_inactive_when_no_pause_row_exists():
    class _Db:
        def execute_query(self, _query, _params=None):
            return []

    assert _is_new_entries_paused(_Db()) is False


def test_entry_pause_fails_closed_on_malformed_state_or_db_error():
    class _MalformedDb:
        def execute_query(self, _query, _params=None):
            return [{"value": "not-json"}]

    class _BrokenDb:
        def execute_query(self, _query, _params=None):
            raise RuntimeError("database unavailable")

    assert _is_new_entries_paused(_MalformedDb()) is True
    assert _is_new_entries_paused(_BrokenDb()) is True


def test_expired_pause_resumes_only_when_delete_and_audit_commit_together():
    from datetime import datetime, timedelta, timezone

    expired_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    pause = {
        "paused": True,
        "reason": "test_pause",
        "started_at": (expired_at - timedelta(minutes=10)).isoformat(),
        "expires_at": expired_at.isoformat(),
    }

    class _ResumeDb:
        def __init__(self, resumed):
            self.resumed = resumed
            self.queries = []

        def execute_query(self, query, _params=None):
            self.queries.append(query)
            if len(self.queries) == 1:
                return [{"value": pause, "updated_at": expired_at}]
            return [{"resumed": self.resumed}]

    committed = _ResumeDb(1)
    failed_or_raced = _ResumeDb(0)

    assert _is_new_entries_paused(committed) is False
    assert "DELETE FROM trading_runtime_state" in committed.queries[1]
    assert "INSERT INTO trades_audit" in committed.queries[1]
    assert _is_new_entries_paused(failed_or_raced) is True


def test_near_support_compares_bounce_with_breakdown_on_executable_contract():
    result = _evaluate(
        {"sr_zone_position": 0.10},
        _prediction("LONG", probability=0.58),
        _prediction("LONG", probability=0.58),
        _prediction("SHORT", probability=0.75),
    )
    assert result.selected_mode == "FUTURES_SHORT"
    assert result.selected_thesis_type == "SHORT_SUPPORT_BREAKDOWN"
    assert result.selected_contract["economic_risk_usdt"] <= 18.0 + 1e-9


def test_near_resistance_compares_rejection_with_breakout():
    result = _evaluate(
        {"sr_zone_position": 0.90, "sr_rejection": True},
        _prediction("LONG", probability=0.78),
        _prediction("LONG", probability=0.76),
        _prediction("SHORT", probability=0.60),
    )
    assert result.selected_mode == "SPOT_LONG"
    assert result.selected_thesis_type == "LONG_RESISTANCE_BREAKOUT"
    assert result.alternative_contract["thesis_type"] == "SHORT_RESISTANCE_REJECTION"


def test_locked_margin_matches_selected_contract_without_rounding_up():
    result = _evaluate(
        {"sr_zone_position": 0.90, "sr_rejection": True},
        _prediction("LONG", probability=0.78, position_size=100.00000049),
        _prediction("LONG", probability=0.76, position_size=100.00000049),
        _prediction("SHORT", probability=0.60, position_size=100.00000049),
    )

    assert result.selected_contract is not None
    assert result.locked_position_size_usdt == result.selected_contract["margin_usdt"]
    assert scale_contract(result.selected_contract, result.locked_position_size_usdt)["margin_usdt"] == result.locked_position_size_usdt


def test_entry_generation_payload_contains_code_and_config_identity():
    payload = _entry_generation_payload(_settings())

    assert payload["entry_generation"].startswith("entry:")
    assert len(payload["entry_code_hash"]) == 64
    assert len(payload["entry_config_hash"]) == 64
    assert len(payload["entry_model_hash"]) == 64
    assert payload["entry_generation"].endswith(
        f"{payload['entry_code_hash'][:12]}:{payload['entry_config_hash'][:12]}:{payload['entry_model_hash'][:12]}"
    )


def test_entry_generation_config_hash_changes_with_selection_thresholds():
    baseline = _entry_generation_payload(_settings(range_scalp_direction_min_probability=0.55))
    changed = _entry_generation_payload(_settings(range_scalp_direction_min_probability=0.60))

    assert baseline["entry_config_hash"] != changed["entry_config_hash"]
    assert baseline["entry_generation"] != changed["entry_generation"]


def test_contract_costs_and_common_risk_can_change_raw_prediction_preference():
    result = _evaluate(
        {"sr_zone_position": 0.10},
        _prediction("LONG", probability=0.64, reward_pct=5.0, risk_pct=1.0, position_size=100),
        _prediction("LONG", probability=0.64, reward_pct=5.0, risk_pct=1.0, position_size=100),
        _prediction("SHORT", probability=0.75, reward_pct=2.0, risk_pct=2.0, position_size=100),
    )
    assert result.selected_mode in ("SPOT_LONG", "FUTURES_LONG")
    assert result.long_expected_net_return > result.short_expected_net_return


def test_short_priority_is_only_a_deprecated_alias():
    result = _evaluate(
        {"sr_zone_position": 0.90, "sr_rejection": True},
        _prediction("LONG", probability=0.80),
        _prediction("LONG", probability=0.78),
        _prediction("SHORT", probability=0.60),
        _settings(range_scalp_direction_mode="short_priority", range_scalp_short_priority_bonus=99.0),
    )
    assert result.selected_mode == "SPOT_LONG"


def test_unconfirmed_resistance_rejection_competes_by_net_economics():
    result = _evaluate(
        {"sr_zone_position": 0.90},
        _prediction("LONG", probability=0.40),
        _prediction("LONG", probability=0.40),
        _prediction("SHORT", probability=0.80),
    )
    assert result.selected_mode == "FUTURES_SHORT"
    assert result.selected_contract["rejection_evidence"]["confirmed"] is False


def test_breakout_pressure_does_not_bypass_bilateral_economic_selection():
    result = _evaluate(
        {"sr_zone_position": 0.90, "sr_rejection": True, "sr_breakout": True},
        _prediction("LONG", probability=0.75),
        _prediction("LONG", probability=0.73),
        _prediction("SHORT", probability=0.85),
    )
    assert result.selected_mode == "FUTURES_SHORT"
    assert result.selected_contract["rejection_evidence"]["breakout_pressure"] is True


def test_middle_range_selects_economically_valid_direction_without_sr_event():
    result = _evaluate(
        {"sr_zone_position": 0.50},
        _prediction("LONG", probability=0.80),
        _prediction("LONG", probability=0.75),
        _prediction("SHORT", probability=0.60),
    )
    assert result.selected_mode == "SPOT_LONG"
    assert result.selected_thesis_type == "LONG_DIRECTIONAL_EDGE"
    assert result.reason.startswith("BILATERAL_NEUTRAL_LONG_")


def test_middle_range_still_abstains_when_no_contract_passes_economics():
    result = _evaluate(
        {"sr_zone_position": 0.50},
        _prediction("LONG", probability=0.40, reward_pct=1.0, risk_pct=4.0),
        _prediction("LONG", probability=0.40, reward_pct=1.0, risk_pct=4.0),
        _prediction("SHORT", probability=0.40, reward_pct=1.0, risk_pct=4.0),
    )
    assert result.selected_mode == "ABSTAIN"
    assert result.reason == "BILATERAL_NEUTRAL_NO_POSITIVE_EXECUTABLE_RETURN"


def test_futures_long_routing_intent_does_not_override_better_spot_contract():
    result = _evaluate(
        {"sr_zone_position": 0.10, "routing_intent": "FUTURES_LONG_ANALYSIS"},
        _prediction("LONG", probability=0.80),
        _prediction("LONG", probability=0.78),
        _prediction("SHORT", probability=0.50),
    )
    assert result.selected_mode == "SPOT_LONG"
    assert result.capital_intent == "SPOT"


def test_best_short_venue_and_its_prediction_are_selected():
    futures_short = _prediction("SHORT", probability=0.61)
    margin_short = _prediction("SHORT", probability=0.82)
    result = evaluate_range_scalp(
        "BTCUSDT",
        {"current_price": 100.0, "sr_zone_position": 0.90, "spot_executable": True},
        _prediction("LONG", probability=0.40),
        _prediction("LONG", probability=0.40),
        futures_short,
        True,
        _settings(spot_margin_short_enabled=True),
        futures_short_prediction=futures_short,
        spot_margin_short_prediction=margin_short,
        spot_margin_executable=True,
    )
    assert result.selected_mode == "SPOT_MARGIN_SHORT"
    assert result.short_prediction == margin_short
    assert {row["mode"] for row in result.short_venue_contracts} == {"FUTURES_SHORT", "SPOT_MARGIN_SHORT"}


def test_legacy_short_executors_are_disabled_when_bilateral_policy_is_active():
    settings = _settings(directional_policy_mode="active")
    assert _maybe_open_range_scalp_shorts(
        object(), None, settings, None, None, None, "RANGE_SCALP", "cycle-1",
    ) == []
    assert _maybe_open_sr_pool_shorts(
        settings, None, [{"symbol": "BTCUSDT", "side": "SHORT"}],
    ) == []


def test_spot_long_survives_when_futures_are_not_executable():
    result = _evaluate(
        {"sr_zone_position": 0.10, "spot_executable": True},
        _prediction("LONG", probability=0.75),
        _prediction("LONG", probability=0.90),
        _prediction("SHORT", probability=0.80),
        futures=False,
        short_executable=False,
    )
    assert result.selected_mode == "SPOT_LONG"
    assert {row["mode"] for row in result.all_venue_contracts} == {
        "SPOT_LONG", "FUTURES_LONG", "FUTURES_SHORT", "SPOT_MARGIN_SHORT",
    }
    by_mode = {row["mode"]: row for row in result.all_venue_contracts}
    assert by_mode["SPOT_LONG"]["venue_executable_at_time"] is True
    assert by_mode["FUTURES_LONG"]["venue_executable_at_time"] is False
    assert by_mode["FUTURES_SHORT"]["venue_executable_at_time"] is False
    assert by_mode["SPOT_MARGIN_SHORT"]["venue_executable_at_time"] is False
    assert by_mode["SPOT_MARGIN_SHORT"]["venue_unavailable_reason"] == "SPOT_MARGIN_DISABLED_OR_UNAVAILABLE"


def test_short_route_flag_blocks_futures_short_but_preserves_alternative_contract():
    result = _evaluate(
        {"sr_zone_position": 0.90, "sr_rejection": True},
        _prediction("LONG", probability=0.40),
        _prediction("LONG", probability=0.40),
        _prediction("SHORT", probability=0.90),
        futures=True,
        short_executable=False,
    )

    by_mode = {row["mode"]: row for row in result.all_venue_contracts}
    assert result.selected_mode != "FUTURES_SHORT"
    assert by_mode["FUTURES_SHORT"]["venue_executable_at_time"] is False
    assert by_mode["FUTURES_SHORT"]["venue_unavailable_reason"] == "FUTURES_SHORT_UNAVAILABLE"
    assert by_mode["FUTURES_SHORT"]["validation_evidence"]["input_executable"] is False


def test_real_arbitration_contract_set_survives_candidate_persistence():
    class _Db:
        def __init__(self):
            self.calls = []

        def execute_update(self, query, params):
            self.calls.append((query, params))

    spot_filter_snapshot = {
        "exchange_filters_validated": True,
        "min_qty": 0.001,
        "max_qty": 100.0,
        "market_step": 0.001,
        "market_min_qty": 0.001,
        "market_max_qty": 100.0,
        "min_notional": 5.0,
        "tick_size": 0.01,
        "filter_snapshot": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"}],
        "filter_snapshot_sha256": "a" * 64,
        "filter_snapshot_retrieved_at": "2026-09-30T12:00:00+00:00",
        "filter_snapshot_venue": "spot_demo",
        "filter_snapshot_symbol": "BTCUSDT",
        "filter_snapshot_source": "BinanceClient.get_symbol_filters",
    }
    futures_filter_snapshot = {
        **spot_filter_snapshot,
        "filter_snapshot_sha256": "b" * 64,
        "filter_snapshot_venue": "usd_m_futures_testnet",
        "filter_snapshot_source": "FuturesClient.get_symbol_info",
    }
    arbitration = _evaluate(
        {
            "sr_zone_position": 0.90,
            "sr_rejection": True,
            "current_price": 100.0,
            "spot_exchange_filters": spot_filter_snapshot,
            "futures_exchange_filters": futures_filter_snapshot,
        },
        _prediction("LONG", probability=0.78),
        _prediction("LONG", probability=0.76),
        _prediction("SHORT", probability=0.72),
    )
    decision = arbitration.to_dict()
    side = "SHORT" if "SHORT" in arbitration.selected_mode else "LONG"
    trade_type = "futures" if arbitration.selected_mode.startswith("FUTURES") else "spot"
    candidate = {
        "symbol": "BTCUSDT",
        "current_price": 100.0,
        "expected_side": side,
        "trade_type": trade_type,
        "thesis_type": arbitration.selected_thesis_type,
        "directional_arbitration": decision,
    }
    db_client = _Db()
    run_trades._selection_begin_cycle(db_client, "RANGE_SCALP", "A")
    run_trades._selection_capture_candidates(
        [candidate], {"BTCUSDT": {"cycle_duration_minutes": 30}}, "bilateral_prepared",
    )

    payload = json.loads(db_client.calls[-1][1][-1])
    assert payload["directional_arbitration"] == decision
    assert {row["mode"] for row in payload["all_venue_contracts"]} == {
        "SPOT_LONG", "FUTURES_LONG", "FUTURES_SHORT", "SPOT_MARGIN_SHORT",
    }
    assert payload["venue_executable_at_time"]["SPOT_MARGIN_SHORT"] is False
    by_mode = {row["mode"]: row for row in payload["all_venue_contracts"]}
    assert by_mode["SPOT_LONG"]["validation_evidence"]["exchange_filter_snapshot_sha256"] == "a" * 64
    assert by_mode["FUTURES_LONG"]["validation_evidence"]["exchange_filter_snapshot_sha256"] == "b" * 64
    assert by_mode["FUTURES_LONG"]["validation_evidence"]["exchange_filter_snapshot_retrieved_at"] == "2026-09-30T12:00:00+00:00"
    assert payload["ev_horizon_minutes"] == 30


def test_staging_uses_arbitrated_short_route_and_prediction():
    short_prediction = _prediction("SHORT")
    decision = {
        "selected_mode": "FUTURES_SHORT",
        "selected_thesis_type": "SHORT_SUPPORT_BREAKDOWN",
        "reason": "BILATERAL_SUPPORT_SHORT_MAX_NET_RETURN",
        "selected_contract": {"effective_target_price": 96.0, "effective_stop_price": 104.5},
        "short_prediction": short_prediction,
        "long_prediction": _prediction("LONG"),
        "futures_long_prediction": _prediction("LONG"),
    }
    candidate = {
        "symbol": "BTCUSDT", "signal_id": "s1", "strength": 0.8,
        "expected_pnl": 9.0, "success_probability": 0.99,
        "target_price": 110.0, "stop_price": 90.0,
        "range_reversion_prediction": {"valid": True, "trade_side": "LONG"},
    }
    with patch("run_trades._range_scalp_direction_decision", return_value=decision):
        signals, predictions, metadata = _prepare_range_scalp_staging_candidates(
            [candidate], {"BTCUSDT": _prediction("LONG")}, {"BTCUSDT": candidate},
            "RANGE_SCALP", object(), _settings(), {"futures": ["BTCUSDT"]},
        )
    assert signals[0]["expected_side"] == "SHORT"
    assert signals[0]["thesis_type"] == "SHORT_SUPPORT_BREAKDOWN"
    selected_prediction = {**short_prediction, "target_price": 96.0, "stop_price": 104.5}
    assert signals[0]["entry_decision_prediction"] == selected_prediction
    assert signals[0]["expected_pnl"] == short_prediction["expected_pnl"]
    assert signals[0]["success_probability"] == short_prediction["success_probability"]
    assert signals[0]["target_price"] == short_prediction["target_price"]
    assert signals[0]["stop_price"] == 104.5
    assert signals[0]["range_reversion_prediction"]["reason"] == "not_applicable_to_selected_thesis"
    assert predictions["BTCUSDT"] == selected_prediction
    assert metadata["BTCUSDT"]["directional_arbitration"] == decision


def test_pre_filter_staging_preserves_selected_long_and_original_short_side():
    long_prediction = _prediction("LONG", probability=0.72)
    decision = {
        "selected_mode": "SPOT_LONG",
        "selected_thesis_type": "LONG_RESISTANCE_BREAKOUT",
        "reason": "BILATERAL_RESISTANCE_LONG_MAX_NET_RETURN",
        "decision_id": "decision-long",
        "selected_contract": {
            "effective_target_price": 104.0,
            "effective_stop_price": 98.0,
            "side": "LONG",
            "trade_type": "spot",
        },
        "alternative_contract": {
            "side": "SHORT",
            "trade_type": "futures",
            "effective_target_price": 96.0,
            "effective_stop_price": 104.0,
        },
        "long_prediction": long_prediction,
        "futures_long_prediction": _prediction("LONG", probability=0.70),
        "short_prediction": _prediction("SHORT", probability=0.60),
    }
    candidate = {
        "symbol": "BTCUSDT",
        "expected_side": "SHORT",
        "trade_type": "futures",
        "strength": 0.8,
    }

    with patch("run_trades._range_scalp_direction_decision", return_value=decision):
        signals, predictions, metadata = _prepare_range_scalp_staging_candidates(
            [candidate],
            {"BTCUSDT": _prediction("SHORT")},
            {"BTCUSDT": candidate},
            "RANGE_SCALP",
            object(),
            _settings(),
            {"spot": ["BTCUSDT"], "futures": ["BTCUSDT"]},
        )

    assert signals[0]["entry_signal_side"] == "SHORT"
    assert signals[0]["expected_side"] == "LONG"
    assert signals[0]["trade_type"] == "spot"
    assert signals[0]["thesis_type"] == "LONG_RESISTANCE_BREAKOUT"
    assert signals[0]["selected_contract"]["side"] == "LONG"
    assert signals[0]["alternative_contract"]["side"] == "SHORT"
    assert predictions["BTCUSDT"]["trade_side"] == "LONG"
    assert metadata["BTCUSDT"]["directional_arbitration"] == decision


def test_pre_filter_long_selection_drops_short_side_profile_for_reclassification():
    candidate = {
        "symbol": "BTCUSDT",
        "expected_side": "SHORT",
        "peak_quality_profile": "B",
        "peak_quality_score": 0.6,
        "entry_profile_gate_info": {"score_source": "short_side"},
    }
    decision = {
        "selected_mode": "SPOT_LONG",
        "selected_thesis_type": "LONG_RESISTANCE_BREAKOUT",
        "selected_contract": {"side": "LONG", "effective_target_price": 104.0, "effective_stop_price": 98.0},
        "long_prediction": _prediction("LONG"),
        "short_prediction": _prediction("SHORT"),
    }

    with patch("run_trades._range_scalp_direction_decision", return_value=decision):
        signals, _, _ = _prepare_range_scalp_staging_candidates(
            [candidate], {"BTCUSDT": _prediction("SHORT")}, {"BTCUSDT": candidate},
            "RANGE_SCALP", object(), _settings(), {"spot": ["BTCUSDT"]},
            refresh_profile_for_selected_side=True,
        )

    assert signals[0]["entry_signal_profile"] == "B"
    assert signals[0]["expected_side"] == "LONG"
    assert "peak_quality_profile" not in signals[0]
    assert "entry_profile_gate_info" not in signals[0]


def test_bilateral_preparation_supports_non_range_scalp_regimes():
    decision = {
        "selected_mode": "SPOT_LONG",
        "selected_thesis_type": "LONG_RESISTANCE_BREAKOUT",
        "selected_contract": {"side": "LONG", "effective_target_price": 104.0, "effective_stop_price": 98.0},
        "long_prediction": _prediction("LONG", probability=0.72),
        "short_prediction": _prediction("SHORT", probability=0.60),
    }
    candidate = {"symbol": "BTCUSDT", "expected_side": "SHORT", "trade_type": "futures"}

    with patch("run_trades._range_scalp_direction_decision", return_value=decision):
        signals, predictions, _ = _prepare_range_scalp_staging_candidates(
            [candidate], {"BTCUSDT": _prediction("SHORT")}, {"BTCUSDT": candidate},
            "SPOT_BULL", object(), _settings(), {"spot": ["BTCUSDT"], "futures": ["BTCUSDT"]},
            refresh_profile_for_selected_side=True,
        )

    assert signals[0]["expected_side"] == "LONG"
    assert signals[0]["trade_type"] == "spot"
    assert signals[0]["thesis_type"] == "LONG_RESISTANCE_BREAKOUT"
    assert signals[0]["directional_arbitration"]["selected_mode"] == "SPOT_LONG"
    assert predictions["BTCUSDT"]["trade_side"] == "LONG"


def test_futures_short_injection_preserves_full_analysis_payload():
    signal = {"symbol": "BTCUSDT", "score": 50, "reason": "FUTURES_SHORT_AUTO"}
    analysis = {
        "symbol": "BTCUSDT",
        "signal": "SELL",
        "strength": 0.8,
        "indicators": {
            "rsi": 38.0,
            "atr": 2.5,
            "support_level": 95.0,
            "resistance_level": 105.0,
            "sr_zone_position": 0.9,
            "sr_rejection": True,
            "sr_break_pressure_short": 0.75,
        },
    }

    merged = _merge_futures_short_analysis(signal, analysis)

    assert merged["indicators"] == analysis["indicators"]
    assert merged["support_level"] == 95.0
    assert merged["resistance_level"] == 105.0
    assert merged["sr_rejection"] is True
    assert merged["sr_break_pressure_short"] == 0.75


def test_fresh_final_long_rearbitration_revalidates_profile_and_entry_decision(monkeypatch):
    import entry_profile_gate

    classified = []
    settings = _settings(
        entry_decision_enabled=True,
        entry_decision_regime_mode={"RANGE_SCALP": "enforced"},
        entry_decision_min_expected_pnl={"RANGE_SCALP": 0.0},
        entry_decision_min_probability={"RANGE_SCALP": 0.50},
        range_reversion_predictor_mode="enforced",
        entry_profile_gate_enabled=True,
    )
    def classify(meta, regime, direction, symbol, **kwargs):
        classified.append((direction, meta.get("thesis_type")))
        return "B", {"score": 0.62, "score_source": "fresh_long_profile", "thresholds": {"C": 0.4}}

    monkeypatch.setattr(entry_profile_gate, "classify", classify)
    metadata = {
        "expected_side": "LONG",
        "trade_type": "futures",
        "peak_quality_profile": "A",
        "peak_quality_score": 0.8,
        "current_price": 100.0,
        "indicators": {"atr_pct": 1.0},
    }
    decision = {
        "selected_mode": "SPOT_LONG",
        "selected_thesis_type": "LONG_RESISTANCE_BREAKOUT",
        "selected_contract": {
            "side": "LONG",
            "trade_type": "spot",
            "entry_price": 100.0,
            "effective_target_price": 104.0,
            "effective_stop_price": 98.0,
        },
        "alternative_contract": {"side": "SHORT", "trade_type": "futures"},
        "long_prediction": {
            "expected_pnl": 1.0,
            "expected_pnl_percentage": 1.0,
            "success_probability": 0.70,
            "current_price": 100.0,
            "target_price": 105.0,
            "stop_price": 97.0,
            "model": "directional_continuation",
        },
    }

    eligible, reason, refreshed = _revalidate_entry_policy_after_arbitration_change(
        "BTCUSDT", metadata, decision, "RANGE_SCALP", settings,
        previous_side="SHORT", previous_thesis_type="SHORT_RESISTANCE_REJECTION",
    )

    assert eligible is True
    assert reason == "OK"
    assert refreshed["expected_side"] == "LONG"
    assert refreshed["trade_type"] == "spot"
    assert refreshed["peak_quality_profile"] == "B"
    assert refreshed["entry_profile_gate_info"]["score_source"] == "fresh_long_profile"
    assert classified == [("LONG", "LONG_RESISTANCE_BREAKOUT")]
    assert refreshed["entry_decision_prediction"]["target_price"] == 104.0
    assert refreshed["entry_decision_prediction"]["stop_price"] == 98.0
    assert refreshed["entry_decision"]["eligible"] is True
    assert refreshed["entry_decision"]["target_price"] == 104.0
    assert refreshed["entry_decision"]["stop_price"] == 98.0


def test_short_executor_preserves_stale_reward_risk_block_reason(monkeypatch):
    settings = _settings(futures_leverage=2, futures_max_shorts=5, futures_max_longs=2,
                         futures_max_largecap_shorts=3, futures_largecap_symbols=[])
    futures = SimpleNamespace(dry_run=False, get_available_usdt=lambda: 800.0, db_client=None)
    decision = {
        "decision_id": "decision-1",
        "locked_position_size_usdt": 100.0,
        "selected_contract": {
            "entry_price": 100.0,
            "effective_stop_price": 104.0,
            "effective_target_price": 96.0,
        },
        "short_prediction": _prediction("SHORT"),
    }
    monkeypatch.setattr("run_trades._get_futures_client", lambda *_args, **_kwargs: futures)
    monkeypatch.setattr("run_trades._symbol_has_open_futures_position", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        "run_trades._evaluate_funding_guard_for_entry",
        lambda *_args, **_kwargs: {"block": False, "mode": "shadow", "sampled": False},
    )
    monkeypatch.setattr(
        "run_trades._revalidate_entry_before_submit",
        lambda *_args, **_kwargs: (False, "STALE_ENTRY_REWARD_RISK_BELOW_MIN", 99.9),
    )

    result = _open_arbitrated_range_scalp_short(
        "BTCUSDT", decision, settings, futures, None, None, {}, usdt_override=100.0,
    )

    assert result is None
    assert decision["execution_block_reason"] == "STALE_ENTRY_REWARD_RISK_BELOW_MIN"


def test_long_executor_preserves_stale_reward_risk_block_reason(monkeypatch):
    settings = _settings(futures_leverage=2, futures_max_shorts=5, futures_max_longs=2,
                         futures_max_largecap_shorts=3, futures_largecap_symbols=[])
    futures = SimpleNamespace(
        dry_run=False, get_available_usdt=lambda: 800.0, db_client=None,
        _open_positions={},
    )
    decision = {
        "decision_id": "decision-long-1",
        "locked_position_size_usdt": 100.0,
        "selected_contract": {
            "entry_price": 100.0,
            "effective_stop_price": 96.0,
            "effective_target_price": 104.0,
        },
        "futures_long_prediction": _prediction("LONG"),
    }
    monkeypatch.setattr("run_trades._get_futures_client", lambda *_args, **_kwargs: futures)
    monkeypatch.setattr(
        "run_trades._evaluate_funding_guard_for_entry",
        lambda *_args, **_kwargs: {"block": False, "mode": "shadow", "sampled": False},
    )
    monkeypatch.setattr(
        "run_trades._revalidate_entry_before_submit",
        lambda *_args, **_kwargs: (False, "STALE_ENTRY_REWARD_RISK_BELOW_MIN", 100.1),
    )

    result = _open_arbitrated_range_scalp_long(
        "BTCUSDT", decision, settings, futures, None, None, {}, usdt_override=100.0,
    )

    assert result is None
    assert decision["execution_block_reason"] == "STALE_ENTRY_REWARD_RISK_BELOW_MIN"
