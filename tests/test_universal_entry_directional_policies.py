from Dev.scripts.analyze_universal_entry_directional_policies import (
    _decision_features,
    _paired_policy_comparison,
    choose_direction,
)


def test_four_direction_policies_use_only_predeclared_features_and_abstain_below_thresholds():
    features = {
        "return_4h": 0.006,
        "zscore_24h": 2.1,
        "relative_strength_zscore_24h": -0.8,
    }

    assert choose_direction("always_invert_original_side", "LONG", features) == "SHORT"
    assert choose_direction("time_series_momentum_follow_4h_0_5pct", "SHORT", features) == "LONG"
    assert choose_direction("mean_reversion_fade_zscore_2", "LONG", features) == "SHORT"
    assert choose_direction("relative_strength_follow_zscore_0_5", "LONG", features) == "SHORT"
    assert choose_direction("time_series_momentum_follow_4h_0_5pct", "LONG", {"return_4h": 0.004}) is None
    assert choose_direction("mean_reversion_fade_zscore_2", "LONG", {"zscore_24h": 1.99}) is None
    assert choose_direction("relative_strength_follow_zscore_0_5", "LONG", {"relative_strength_zscore_24h": 0.49}) is None


def test_point_in_time_features_align_only_fully_closed_hourly_candles():
    start = 1_700_000_000
    symbols = {}
    for symbol, multiplier in (("AAAUSDT", 1.0), ("BBBUSDT", 1.1), ("CCCUSDT", 0.9), ("DDDUSDT", 1.2), ("EEEUSDT", 0.8)):
        bars = []
        for index in range(26):
            close = multiplier * (100.0 + index * (0.5 if symbol == "AAAUSDT" else 0.1))
            bars.append({
                "open_time": start + index * 3600,
                "open": close - 0.1,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
            })
        symbols[symbol] = bars

    features = _decision_features(symbols)
    decision_time = start + 25 * 3600

    assert ("AAAUSDT", decision_time) in features
    assert features[("AAAUSDT", decision_time)]["return_4h"] > 0
    assert "relative_strength_zscore_24h" in features[("AAAUSDT", decision_time)]
    assert ("AAAUSDT", start + 26 * 3600) not in features


def test_paired_policy_bootstrap_uses_union_of_clusters_and_zero_for_abstentions():
    baseline = {"cluster-a": 1.0, "cluster-b": -1.0, "cluster-c": 0.5}
    challenger = {"cluster-a": 0.0, "cluster-b": -1.0}

    result = _paired_policy_comparison(baseline, challenger, seed=42, replicates=100)

    assert result["clusters"] == 3
    assert result["mean_delta_net_per_cluster"] == -0.5
    assert len(result["bootstrap_ci95"]) == 2