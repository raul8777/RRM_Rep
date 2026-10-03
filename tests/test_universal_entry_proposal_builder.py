import pytest

import entry_philosophies
from Dev.scripts.build_universal_entry_proposals import audit_proposal_clusters


def _proposal(cluster_id, *, symbol="BTCUSDT", decision_time=100.0, family="mean_reversion"):
    return {
        "dedupe_cluster_id": cluster_id,
        "window_id": "dev-1",
        "symbol": symbol,
        "decision_time": decision_time,
        "opportunity_event_id": f"{family}:{decision_time}",
        "philosophy_family": family,
        "side": "LONG",
        "thesis_type": "LONG_MEAN_REVERSION",
    }


def test_cluster_audit_preserves_multiple_philosophies_for_one_observation():
    audit = audit_proposal_clusters([
        _proposal("dev-1:BTCUSDT:100", family="mean_reversion"),
        _proposal("dev-1:BTCUSDT:100", family="breakout_expansion"),
    ])

    assert len(audit) == 1
    assert audit[0]["proposal_count"] == 2
    assert audit[0]["philosophies"] == ["breakout_expansion", "mean_reversion"]


def test_sr_event_proposals_use_only_closed_history_and_next_contiguous_open(monkeypatch):
    seen_history_lengths = []

    def fake_sr_features(closes, highs, lows, volumes, **kwargs):
        seen_history_lengths.append(len(closes))
        return {
            "state": "ok",
            "support_bounce_buy": len(closes) == 5 and closes[-1] == 104.0,
            "resistance_bounce_sell": False,
            "sr_breakout": False,
            "sr_breakdown": False,
            "support_level": 103.0,
            "resistance_level": 200.0,
        }

    monkeypatch.setattr(entry_philosophies, "compute_sr_features", fake_sr_features)
    bars = [
        {
            "open_time": index * 3600,
            "open": 100.0 + index,
            "high": 101.0 + index,
            "low": 99.0 + index,
            "close": 100.0 + index,
            "volume": 10.0,
        }
        for index in range(8)
    ]
    bars[4]["close"] = 104.0
    proposals = entry_philosophies.generate_sr_event_proposals(
        "BTCUSDT",
        bars,
        window_id="dev-1",
        window_role="development",
        window_start=0.0,
        window_end=36000.0,
        horizon_hours=1,
        lookback_bars=5,
        sr_order=1,
    )

    assert len(proposals) == 1
    assert proposals[0]["side"] == "LONG"
    assert proposals[0]["decision_time"] == 5 * 3600
    assert proposals[0]["entry_time"] == 5 * 3600
    assert proposals[0]["entry_price"] == bars[5]["open"]
    assert proposals[0]["feature_snapshot"]["source_close"] == bars[4]["close"]
    assert seen_history_lengths == [5, 4, 5, 4, 5, 4]


def test_sr_breakout_uses_resistance_from_bars_before_the_signal_candle(monkeypatch):
    def fake_sr_features(closes, highs, lows, volumes, **kwargs):
        prior = len(closes) == 4
        return {
            "state": "ok",
            "support_bounce_buy": False,
            "resistance_bounce_sell": False,
            "sr_breakout": False,
            "sr_breakdown": False,
            "support_level": 98.0,
            "resistance_level": 103.5 if prior else 200.0,
        }

    monkeypatch.setattr(entry_philosophies, "compute_sr_features", fake_sr_features)
    bars = [
        {
            "open_time": index * 3600,
            "open": 99.0 + index,
            "high": 100.0 + index,
            "low": 98.0 + index,
            "close": 100.0 + index,
            "volume": 10.0,
        }
        for index in range(8)
    ]
    proposals = entry_philosophies.generate_sr_event_proposals(
        "BTCUSDT",
        bars,
        window_id="dev-1",
        window_role="development",
        window_start=0.0,
        window_end=36000.0,
        horizon_hours=1,
        lookback_bars=5,
        sr_order=1,
    )

    breakouts = [proposal for proposal in proposals if proposal["thesis_type"] == "LONG_RESISTANCE_BREAKOUT"]
    assert len(breakouts) == 1
    assert breakouts[0]["feature_snapshot"]["prior_pivot_resistance"] == 103.5
    assert breakouts[0]["feature_snapshot"]["prior_pivot_breakout"] is True


def test_trend_pullback_requires_closed_confirmation_and_uses_next_open():
    closes = [100.0 + index for index in range(25)]
    closes.extend([124.0, 123.5, 124.0, 124.5, 125.0, 125.5])
    bars = []
    for index, close in enumerate(closes):
        bars.append({
            "open_time": index * 3600,
            "open": close - 0.6,
            "high": close + 0.5,
            "low": close - 0.8,
            "close": close,
            "volume": 20.0,
        })
    bars[26].update({"open": 124.5, "high": 125.0, "low": 123.0, "close": 124.0})
    bars[27].update({"open": 124.0, "high": 124.5, "low": 122.5, "close": 123.5})
    bars[28].update({"open": 123.2, "high": 124.2, "low": 123.0, "close": 124.0})
    bars[29].update({"open": 124.0, "high": 125.0, "low": 123.8, "close": 124.5})
    bars[30].update({"open": 124.5, "high": 125.5, "low": 124.2, "close": 125.0})

    proposals = entry_philosophies.generate_trend_pullback_proposals(
        "BTCUSDT",
        bars,
        window_id="dev-1",
        window_role="development",
        window_start=0.0,
        window_end=200000.0,
        horizon_hours=1,
    )

    assert proposals
    assert all(proposal["side"] == "LONG" for proposal in proposals)
    for proposal in proposals:
        source = next(bar for bar in bars if bar["open_time"] == proposal["source_candle_open_time"])
        entry = next(bar for bar in bars if bar["open_time"] == proposal["entry_time"])
        assert proposal["feature_available_at"] == proposal["decision_time"]
        assert proposal["decision_time"] == proposal["source_candle_open_time"] + 3600
        assert proposal["entry_price"] == entry["open"]
        assert proposal["feature_snapshot"]["pullback_atr"] >= 0.5
        assert source["close"] > source["open"]


@pytest.mark.parametrize(
    "other",
    [
        {"symbol": "ETHUSDT", "decision_time": 100.0},
        {"symbol": "BTCUSDT", "decision_time": 200.0},
    ],
)
def test_cluster_audit_rejects_identity_collision(other):
    with pytest.raises(ValueError, match="spans multiple source observations"):
        audit_proposal_clusters([
            _proposal("collision"),
            _proposal("collision", **other),
        ])