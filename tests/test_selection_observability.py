import json
from datetime import datetime, timezone

import run_trades
import staged_entry


class FakeDb:
    def __init__(self):
        self.calls = []
        self.runtime_state = {}

    def execute_update(self, sql, params=None):
        self.calls.append((sql, params))
        if "INSERT INTO trading_runtime_state" in sql and params:
            self.runtime_state[params[0]] = {
                "value": json.loads(params[1]),
                "updated_at": datetime.now(timezone.utc),
            }
        return 1

    def execute_query(self, _sql, params=None):
        key = params[0] if params else None
        row = self.runtime_state.get(key)
        return [row] if row else []


class CycleSettings:
    cycle_spot_bull = 10
    cycle_range_scalp = 5
    cycle_preservation = 45
    cycle_futures_short = 3
    trade_cycle_minutes = 15


def test_cycle_result_retains_regime_and_interval_for_early_returns():
    result = run_trades._cycle_result(CycleSettings(), "RANGE_SCALP")

    assert result["regime_mode"] == "RANGE_SCALP"
    assert result["next_cycle_minutes"] == 5
    assert result["buy_orders"] == []


def test_selection_context_persists_candidate_and_execution():
    db = FakeDb()
    run_trades._selection_begin_cycle(db, "SPOT_BULL", "A")
    run_trades._selection_capture_candidates(
        [{"symbol": "BTCUSDT", "strength": 0.7, "trade_type": "spot"}],
        {"BTCUSDT": {"expected_pnl": 4.2, "success_probability": 0.61}},
        "predicted",
    )
    run_trades._selection_record_decision("BTCUSDT", "executed", "EJECUTADO", "spot")
    run_trades._selection_complete_cycle({"outcome": "executed"})

    sql = "\n".join(call[0] for call in db.calls)
    assert "entry_selection_cycles" in sql
    assert "entry_selection_candidates" in sql
    assert "UPDATE entry_selection_candidates" in sql
    assert "completed_at=NOW()" in sql
    assert run_trades._selection_context == {}


def test_pre_qubo_filtered_summary_persists_zero_qubo_input():
    db = FakeDb()
    run_trades._selection_begin_cycle(db, "SPOT_BULL", "A")
    run_trades._selection_capture_candidates(
        [{"symbol": "BTCUSDT", "trade_type": "spot"}],
        {},
        "predicted",
    )
    run_trades._selection_complete_cycle({"outcome": "all_filtered", "reason": "PRE_QUBO_FILTERED"})

    payload = json.loads(db.calls[-1][1][3])
    assert payload["reason"] == "PRE_QUBO_FILTERED"
    assert payload["profile_pass_count"] == 0
    assert payload["entry_decision_pass_count"] == 0
    assert payload["qubo_input_count"] == 0
    assert payload["qubo_selected_count"] == 0
    assert payload["qubo_excluded_count"] == 0


def test_qubo_authority_writer_round_trips_cycle_and_symbol_hash():
    db = FakeDb()
    run_trades._selection_begin_cycle(db, "SPOT_BULL", "A")
    cycle_id = run_trades._selection_context["cycle_id"]
    symbols = ["ETHUSDT", "BTCUSDT"]

    run_trades._persist_qubo_authority(db, "SPOT_BULL", symbols)
    snapshot = staged_entry.get_qubo_authority_snapshot(db, "SPOT_BULL")

    assert snapshot["cycle_id"] == cycle_id
    assert snapshot["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert snapshot["symbols_hash"] == staged_entry.qubo_symbols_hash_for(symbols)


def test_qubo_selected_candidate_capture_keeps_authority_provenance():
    db = FakeDb()
    run_trades._selection_begin_cycle(db, "SPOT_BULL", "A")
    cycle_id = run_trades._selection_context["cycle_id"]
    symbols = ["BTCUSDT", "ETHUSDT"]
    run_trades._persist_qubo_authority(db, "SPOT_BULL", symbols)
    run_trades._selection_capture_candidates(
        [{"symbol": "BTCUSDT", "trade_type": "spot"}],
        {},
        "qubo_selected",
    )

    payload = json.loads(db.calls[-1][1][-1])
    assert payload["cycle_id"] == cycle_id
    assert payload["qubo_cycle_id"] == cycle_id
    assert payload["qubo_symbols_hash"] == staged_entry.qubo_symbols_hash_for(symbols)


def test_selection_persists_gate_evidence_and_rejection_summaries():
    db = FakeDb()
    run_trades._selection_begin_cycle(db, "SPOT_BULL", "A")
    run_trades._selection_capture_candidates(
        [{"symbol": "BTCUSDT", "trade_type": "spot"}],
        {"BTCUSDT": {"expected_pnl": 1.2, "success_probability": 0.49}},
        "predicted",
    )
    run_trades._selection_merge_candidate_data("BTCUSDT", {
        "peak_quality_profile": "B",
        "peak_quality_score": 0.42,
        "entry_profile_gate_info": {"score": 0.42, "score_source": "model", "thresholds": {"C": 0.25}},
        "entry_decision": {"eligible": False, "reason": "PROBABILITY_BELOW_THRESHOLD"},
    })
    run_trades._selection_record_filter_rejection("PROBABILITY_BELOW_THRESHOLD")
    run_trades._selection_complete_cycle({"outcome": "all_filtered", "reason": "PRE_QUBO_FILTERED"})

    merged_payload = json.loads(db.calls[-2][1][0])
    summary = json.loads(db.calls[-1][1][3])
    assert merged_payload["entry_profile_gate_info"]["score_source"] == "model"
    assert merged_payload["entry_decision"]["reason"] == "PROBABILITY_BELOW_THRESHOLD"
    assert summary["entry_decision_rejection_counts"] == {"PROBABILITY_BELOW_THRESHOLD": 1}
    assert summary["filter_rejection_counts"] == {"PROBABILITY_BELOW_THRESHOLD": 1}


def test_selection_context_keeps_rejection_reason():
    db = FakeDb()
    run_trades._selection_begin_cycle(db, "FUTURES_SHORT", "C")
    run_trades._selection_capture_candidates(
        [{"symbol": "ETHUSDT", "trade_type": "futures"}],
        {},
        "predicted",
    )
    run_trades._selection_record_decision("ETHUSDT", "rejected", "BTC_MOMENTUM", "futures")

    update_params = db.calls[-1][1]
    assert update_params[0:3] == ("rejected", "BTC_MOMENTUM", "rejected")
    run_trades._selection_complete_cycle({"outcome": "no_entry"})


def test_candidate_rejection_persists_stage_reason_and_counterfactual_ev():
    db = FakeDb()
    run_trades._selection_begin_cycle(db, "RANGE_SCALP", "A")
    run_trades._selection_capture_candidates([{
        "symbol": "BTCUSDT",
        "trade_type": "futures",
        "directional_arbitration": {
            "decision_id": "arb-rejected-1",
            "selected_mode": "ABSTAIN",
            "all_venue_contracts": [
                {"mode": "SPOT_LONG", "expected_net_pnl_usdt": 3.0},
                {"mode": "FUTURES_SHORT", "expected_net_pnl_usdt": -1.0},
            ],
        },
    }], {}, "bilateral_prepared")

    run_trades._selection_record_candidate_rejection(
        {
            "symbol": "BTCUSDT",
            "trade_type": "futures",
            "directional_arbitration": {
                "decision_id": "arb-rejected-1",
                "selected_mode": "ABSTAIN",
                "all_venue_contracts": [
                    {"mode": "SPOT_LONG", "expected_net_pnl_usdt": 3.0},
                    {"mode": "FUTURES_SHORT", "expected_net_pnl_usdt": -1.0},
                ],
            },
        },
        stage="entry_decision",
        reason="PROBABILITY_BELOW_THRESHOLD",
        details={"probability": 0.48},
    )

    merge_call = next(call for call in db.calls if "SET data=COALESCE" in call[0])
    rejection = json.loads(merge_call[1][0])
    assert rejection["selection_outcome"] == "abstained"
    assert rejection["decision_id"] == "arb-rejected-1"
    assert len(rejection["all_venue_contracts"]) == 2
    assert rejection["selection_rejection"]["stage"] == "entry_decision"
    assert rejection["selection_rejection"]["reason"] == "PROBABILITY_BELOW_THRESHOLD"
    assert rejection["rejected_contract_expected_net_pnl_usdt"] == {
        "SPOT_LONG": 3.0,
        "FUTURES_SHORT": -1.0,
    }
    assert any("SET decision=%s" in call[0] for call in db.calls)
