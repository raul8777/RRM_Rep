import json
from datetime import datetime, timezone

import pytest

from Dev.scripts.recover_wayback_binance_filter_snapshots import _decode_exchange_info


def _server_time(timestamp):
    return int(datetime.strptime(timestamp[:8], "%Y%m%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def test_archived_exchange_info_extracts_filters_as_point_observation():
    timestamp = "20250426005818"
    payload = {
        "serverTime": _server_time(timestamp),
        "symbols": [
            {"symbol": "BTCUSDT", "status": "TRADING", "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.001"}]}
        ],
    }

    result = _decode_exchange_info(json.dumps(payload).encode(), timestamp, "archive-digest", ["BTCUSDT", "DELISTEDUSDT"])

    assert result["venue"] == "spot_production"
    assert result["applicability"] == "point_observation_only_not_backdated"
    assert result["symbols"][0]["status"] == "TRADING"
    assert result["symbols"][0]["filter_snapshot"] == payload["symbols"][0]["filters"]
    assert result["symbols"][1]["status"] == "not_listed_at_capture"
    assert result["symbols"][1]["filter_snapshot"] is None


def test_archived_exchange_info_rejects_server_time_from_a_different_day():
    payload = {"serverTime": _server_time("20250425000000"), "symbols": []}

    with pytest.raises(ValueError, match="does not match"):
        _decode_exchange_info(json.dumps(payload).encode(), "20250426005818", "archive-digest", [])