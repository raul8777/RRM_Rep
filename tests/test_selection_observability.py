import run_trades


class FakeDb:
    def __init__(self):
        self.calls = []

    def execute_update(self, sql, params=None):
        self.calls.append((sql, params))
        return 1


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
