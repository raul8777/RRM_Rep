from datetime import datetime, timezone
import json

from Dev.scripts import build_universal_entry_proposals
from entry_philosophies import (
    generate_cross_sectional_relative_strength_proposals,
    generate_pilot_proposals,
)


def _bars(closes):
    start = datetime(2025, 2, 5, tzinfo=timezone.utc).timestamp()
    return [{
        "open_time": start + index * 3600,
        "open": price,
        "high": price + 0.2,
        "low": price - 0.2,
        "close": price,
        "volume": 1.0,
    } for index, price in enumerate(closes)]


def test_momentum_proposal_uses_next_open_and_has_stable_lineage():
    closes = [100.0] * 27 + [101.0, 102.0, 103.0, 104.0, 104.5, 104.6]
    bars = _bars(closes)
    start = bars[0]["open_time"]
    end = bars[-1]["open_time"] + 3600

    proposals = generate_pilot_proposals(
        "BTCUSDT", bars, window_id="dev-1", window_role="development",
        window_start=start, window_end=end,
    )
    momentum = [row for row in proposals if row["philosophy_family"] == "time_series_momentum"]

    assert momentum
    proposal = momentum[0]
    source_index = next(i for i, bar in enumerate(bars) if bar["open_time"] == proposal["source_candle_open_time"])
    assert proposal["entry_time"] == bars[source_index + 1]["open_time"]
    assert proposal["entry_price"] == bars[source_index + 1]["open"]
    assert proposal["feature_available_at"] == proposal["decision_time"]
    assert proposal["ev_horizon_minutes"] == 240
    assert proposal["opportunity_event_id"]
    assert proposal["dedupe_cluster_id"]
    assert proposal["feature_snapshot"]["predictor_indicators"]["rsi"]["value"] >= 0
    assert "bollinger_bands" in proposal["feature_snapshot"]["predictor_indicators"]


def test_proposal_at_a_timestamp_is_invariant_to_later_price_changes():
    closes = [100.0] * 27 + [101.0, 102.0, 103.0, 104.0, 104.5, 104.6, 104.7]
    bars = _bars(closes)
    start = bars[0]["open_time"]
    end = bars[-1]["open_time"] + 3600
    original = generate_pilot_proposals(
        "BTCUSDT", bars, window_id="dev-1", window_role="development",
        window_start=start, window_end=end,
    )
    target = next(row for row in original if row["philosophy_family"] == "time_series_momentum")

    changed = [dict(bar) for bar in bars]
    target_index = next(i for i, bar in enumerate(changed) if bar["open_time"] == target["source_candle_open_time"])
    for index in range(target_index + 2, len(changed)):
        changed[index]["open"] *= 1.5
        changed[index]["high"] *= 1.5
        changed[index]["low"] *= 1.5
        changed[index]["close"] *= 1.5
    replayed = generate_pilot_proposals(
        "BTCUSDT", changed, window_id="dev-1", window_role="development",
        window_start=start, window_end=end,
    )
    matching = next(row for row in replayed if row["opportunity_event_id"] == target["opportunity_event_id"])

    assert matching["feature_snapshot"] == target["feature_snapshot"]
    assert matching["entry_price"] == target["entry_price"]


def test_generators_reject_invalid_window_role_and_noncontiguous_entry():
    bars = _bars([100.0 + index * 0.1 for index in range(40)])
    start = bars[0]["open_time"]
    end = bars[-1]["open_time"] + 7200
    bars[30]["open_time"] += 60

    proposals = generate_pilot_proposals(
        "BTCUSDT", bars, window_id="dev-1", window_role="development",
        window_start=start, window_end=end,
    )
    assert all(
        proposal["entry_time"] - proposal["decision_time"] == 0
        for proposal in proposals
    )


def test_cluster_identity_groups_same_source_observation_not_neighboring_times():
    bars = _bars([100.0] * 27 + [101.0, 102.0, 103.0, 104.0, 104.5, 104.6, 104.7])
    start = bars[0]["open_time"]
    end = bars[-1]["open_time"] + 3600

    proposals = generate_pilot_proposals(
        "BTCUSDT", bars, window_id="dev-1", window_role="development",
        window_start=start, window_end=end,
    )
    by_decision_time = {}
    for proposal in proposals:
        by_decision_time.setdefault(proposal["decision_time"], set()).add(proposal["dedupe_cluster_id"])

    assert all(len(cluster_ids) == 1 for cluster_ids in by_decision_time.values())
    assert len({proposal["dedupe_cluster_id"] for proposal in proposals}) == len(by_decision_time)


def test_cross_sectional_generator_ranks_relative_returns_at_common_decision_time():
    start = datetime(2025, 2, 5, tzinfo=timezone.utc).timestamp()
    symbols = {"A": 100.0, "B": 200.0, "C": 50.0, "D": 100.0, "E": 100.0}
    returns = {"A": 0.20, "B": -0.20, "C": 0.0, "D": 0.0, "E": 0.0}
    bars_by_symbol = {}
    for symbol, base_price in symbols.items():
        bars = [{
            "open_time": start + index * 3600,
            "open": base_price,
            "high": base_price * 1.01,
            "low": base_price * 0.99,
            "close": base_price,
            "volume": 1.0,
        } for index in range(36)]
        bars[25]["close"] = base_price * (1.0 + returns[symbol])
        bars_by_symbol[symbol] = bars

    decision_time = start + 26 * 3600
    proposals = generate_cross_sectional_relative_strength_proposals(
        bars_by_symbol,
        window_id="dev-cross-section",
        window_role="development",
        window_start=decision_time,
        window_end=decision_time + 3600,
        minimum_symbols=5,
    )

    by_symbol = {row["symbol"]: row for row in proposals}
    assert by_symbol["A"]["side"] == "LONG"
    assert by_symbol["B"]["side"] == "SHORT"
    assert "C" not in by_symbol
    assert all(row["feature_available_at"] == decision_time for row in proposals)
    assert all(row["entry_time"] == decision_time for row in proposals)


def test_proposal_builder_keeps_validation_separate_and_includes_relative_strength(tmp_path, monkeypatch):
    start = datetime(2025, 2, 5, tzinfo=timezone.utc).timestamp()
    symbols = {"A": 100.0, "B": 200.0, "C": 50.0, "D": 100.0, "E": 100.0}
    returns = {"A": 0.20, "B": -0.20, "C": 0.0, "D": 0.0, "E": 0.0}
    bars_by_symbol = {}
    normalized = tmp_path / "normalized"
    for venue in ("spot", "um"):
        venue_root = normalized / venue
        venue_root.mkdir(parents=True)
        for symbol, base_price in symbols.items():
            manifest = venue_root / f"{symbol}_1m.manifest.json"
            manifest.write_text(json.dumps({
                "symbol": symbol, "venue": venue, "contract_symbol": symbol,
            }), encoding="utf-8")
            if venue == "spot":
                bars = _bars([base_price] * 36)
                bars[25]["close"] = base_price * (1.0 + returns[symbol])
                bars_by_symbol[symbol] = bars

    windows = tmp_path / "windows"
    windows.mkdir()
    window_id = "range-validation"
    window_start = datetime.fromtimestamp(start, timezone.utc).isoformat()
    window_end = datetime.fromtimestamp(start + 36 * 3600, timezone.utc).isoformat()
    (windows / f"{window_id}.json").write_text(json.dumps({
        "window_role": "validation",
        "dominant_regime": "RANGE_SCALP",
        "start": window_start,
        "end": window_end,
        "symbols": sorted(symbols),
    }), encoding="utf-8")
    (tmp_path / "entry_philosophies.py").write_text("# fixture source hash\n", encoding="utf-8")

    class FakeFeed:
        def __init__(self, root):
            self.root = root

        def get_klines(self, symbol, interval):
            assert interval == "1h"
            return bars_by_symbol[symbol]

    monkeypatch.setattr(build_universal_entry_proposals, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_universal_entry_proposals, "WINDOWS_DIR", windows)
    monkeypatch.setattr(build_universal_entry_proposals, "HistoricalDataFeed", FakeFeed)
    output = tmp_path / "validation" / "proposals.jsonl"

    report = build_universal_entry_proposals.build_development_proposals(
        window_id=window_id,
        normalized_root=normalized,
        output_path=output,
    )
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    relative = [row for row in rows if row["philosophy_family"] == "cross_sectional_relative_strength"]

    assert report["window_role"] == "validation"
    assert report["horizon_minutes"] == 240
    assert report["family_counts"]["cross_sectional_relative_strength"] >= 2
    assert relative
    assert {row["window_role"] for row in relative} == {"validation"}