from datetime import datetime, timedelta, timezone

import app


def test_signal_display_states_distinguish_final_pending_and_expired():
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)

    assert app._signal_display_state({"decision": "executed"}, now)["display_status"] == "executed"
    assert app._signal_display_state({"decision": "rejected"}, now)["display_status"] == "rejected"
    assert app._signal_display_state({"decision": "pending"}, now)["display_status"] == "pending_review"
    assert app._signal_display_state({"received_at": (now - timedelta(minutes=46)).isoformat()}, now)["display_status"] == "expired"
    assert app._signal_display_state({"received_at": (now - timedelta(minutes=1)).isoformat()}, now)["display_status"] == "unresolved"


def test_selection_display_state_labels_each_operational_condition():
    base = {"id": 1, "symbol": "BTCUSDT", "status": "validated"}

    assert app._selection_display_state({**base, "status": "ready"}, "SPOT_BULL", set(), set())["display_status"] == "validating"
    assert app._selection_display_state(base, "SPOT_BULL", set(), {1})["display_status"] == "ready_to_open"
    assert app._selection_display_state(base, "SPOT_BULL", set(), set())["display_status"] == "validated_queue"
    assert app._selection_display_state({**base, "regime_mode": "SPOT_BULL"}, "RANGE_SCALP", set(), {1})["display_status"] == "blocked_regime"
    assert app._selection_display_state(base, "SPOT_BULL", {"BTCUSDT"}, {1})["display_status"] == "duplicate_position"
    assert app._selection_display_state({**base, "status": "discarded"}, "SPOT_BULL", set(), set())["display_status"] == "discarded"
    assert app._selection_display_state({**base, "status": "expired"}, "SPOT_BULL", set(), set())["display_status"] == "expired"


class FakeSelectionDb:
    def __init__(self, rows):
        self.rows = rows

    def execute_query(self, _sql, _params):
        return self.rows


def test_selection_observability_only_marks_ranked_validated_candidate_ready_to_open():
    rows = [
        {
            "id": 1, "symbol": "BTCUSDT", "trade_type": "spot", "regime_mode": "SPOT_BULL",
            "status": "validated", "positive_checks": 2, "total_checks": 2,
            "data": {"priority": [10, 0.9, 0.8], "policy": {}},
            "config_snapshot": {"max_open_per_cycle": 1},
        },
        {
            "id": 2, "symbol": "ETHUSDT", "trade_type": "spot", "regime_mode": "SPOT_BULL",
            "status": "validated", "positive_checks": 2, "total_checks": 2,
            "data": {"priority": [5, 0.8, 0.7], "policy": {}},
            "config_snapshot": {"max_open_per_cycle": 1},
        },
        {
            "id": 3, "symbol": "XLMUSDT", "trade_type": "spot", "regime_mode": "SPOT_BULL",
            "status": "validated", "positive_checks": 2, "total_checks": 2,
            "data": {"priority": [20, 0.9, 0.8], "policy": {}},
            "config_snapshot": {"max_open_per_cycle": 1},
        },
    ]

    payload = app._build_selection_observability(
        FakeSelectionDb(rows), active_regime="SPOT_BULL", open_symbols={"XLMUSDT"}
    )
    states = {candidate["symbol"]: candidate["display_status"] for candidate in payload["candidates"]}

    assert states == {
        "BTCUSDT": "ready_to_open",
        "ETHUSDT": "validated_queue",
        "XLMUSDT": "duplicate_position",
    }
    assert payload["display_counts"] == {
        "ready_to_open": 1,
        "validated_queue": 1,
        "duplicate_position": 1,
    }
