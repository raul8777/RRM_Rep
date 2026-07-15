"""Unit tests for super_mimic.meta_learner."""
import json
from datetime import datetime, timezone

import pytest

from super_mimic.meta_learner import RegimeFingerprint, SuperMimicMetaLearner


class FakeDB:
    """Minimal in-memory DB client for testing."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.queries = []
        self.updates = []

    def execute_query(self, query, params=None):
        self.queries.append((query, params))
        return self.rows

    def execute_update(self, query, params=None):
        self.updates.append((query, params))


def test_fingerprint_distance():
    a = RegimeFingerprint(
        values={k: 0.5 for k in ["btc_trend_1d", "btc_trend_4h", "btc_trend_1h",
                                   "btc_rsi_4h", "btc_dominance", "fng_index",
                                   "avg_vol_spike", "win_rate_7d"]}
    )
    b = RegimeFingerprint(
        values={k: 0.6 for k in ["btc_trend_1d", "btc_trend_4h", "btc_trend_1h",
                                   "btc_rsi_4h", "btc_dominance", "fng_index",
                                   "avg_vol_spike", "win_rate_7d"]}
    )
    dist = a.distance(b)
    assert dist > 0
    assert a.distance(a) == 0.0


def _all_features(value: float) -> dict:
    return {
        "btc_trend_1d": value,
        "btc_trend_4h": value,
        "btc_trend_1h": value,
        "btc_rsi_4h": value,
        "btc_dominance": value,
        "fng_index": value,
        "avg_vol_spike": value,
        "win_rate_7d": value,
    }


def test_recommend_warm_start_ignores_low_trade_experiments():
    rows = [
        {
            "id": "exp_low_trades",
            "recommended_config": {"min_signal_strength": 0.45},
            "metrics": json.dumps({"trades": 2, "pnl_per_day_usdt": 100.0}),
            "fingerprint": json.dumps({"values": _all_features(0.5), "mode": ""}),
        },
        {
            "id": "exp_good",
            "recommended_config": {"min_signal_strength": 0.55},
            "metrics": json.dumps({"trades": 10, "pnl_per_day_usdt": 50.0, "win_rate": 0.55}),
            "fingerprint": json.dumps({"values": _all_features(0.52), "mode": ""}),
        },
    ]
    meta = SuperMimicMetaLearner(db_client=FakeDB(rows))
    fp = RegimeFingerprint(values=_all_features(0.5))
    recs = meta.recommend_warm_start(fp, mode="RANGE_SCALP", profile="A", top_k=5, min_trades=3)
    assert len(recs) == 1
    assert recs[0]["experiment_id"] == "exp_good"


def test_record_experiment_result_persists_fingerprint():
    fake_db = FakeDB()
    meta = SuperMimicMetaLearner(db_client=fake_db)
    fp = RegimeFingerprint(values={"btc_rsi_4h": 0.4})
    meta.record_experiment_result("exp_1", fp, {"trades": 5})
    assert len(fake_db.updates) == 1
    query, params = fake_db.updates[0]
    assert "fingerprint" in query.lower()
    assert params[2] == "exp_1"
    assert json.loads(params[0]) == fp.to_dict()


def test_extract_metrics_handles_string_and_dict():
    meta = SuperMimicMetaLearner()
    row_str = {"metrics": json.dumps({"best_pnl_per_day_usdt": 12.0, "best_win_rate": 0.5})}
    row_dict = {"metrics": {"best_pnl_per_day_usdt": 12.0, "best_win_rate": 0.5}}
    assert meta._extract_metrics(row_str)["pnl_per_day_usdt"] == 12.0
    assert meta._extract_metrics(row_dict)["win_rate"] == 0.5


def test_capture_fingerprint_normalizes_values():
    meta = SuperMimicMetaLearner()
    fp = meta.capture_fingerprint(mode="RANGE_SCALP")
    assert fp.mode == "RANGE_SCALP"
    assert 0.0 <= fp.values.get("btc_rsi_4h", 0.0) <= 1.0
    assert -1.0 <= fp.values.get("btc_trend_1d", 0.0) <= 1.0
