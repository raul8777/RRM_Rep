from Dev.scripts.score_universal_entry_fast_formulas import (
    CANONICAL_FAMILY_MAP,
    FORMULAS,
    _bootstrap_paired_difference,
    _cluster_net_returns,
    _select_batch,
    score_candidate_components,
)


def _row(candidate_id, symbol, *, ev, probability, quality, outcome="TP"):
    return {
        "candidate_id": candidate_id,
        "symbol": symbol,
        "family": "mean_reversion_range",
        "mode": "SPOT_LONG",
        "raw_expected_pnl_per_100": ev,
        "calibrated_probability": probability,
        "quality": quality,
        "risk_pct": 1.5,
        "reward_pct": 2.0,
        "gross_pnl_per_100": 0.5 if outcome == "TP" else -0.5,
        "simulated_outcome": outcome,
        "dedupe_cluster_id": candidate_id,
        "prediction_payload": {},
    }


def _stats():
    return {
        "mean_reversion_range|SPOT_LONG": {
            "base_probability": 0.45,
            "base_probability_rows": 100,
            "base_probability_source": "family_mode_train",
            "train_clusters": 100,
            "return_sigma": 0.75,
            "predictor_ev_mean": 0.0,
            "predictor_ev_std": 1.0,
            "calibrated_probability_mean": 0.45,
            "calibrated_probability_std": 0.2,
            "quality_mean": 0.5,
            "quality_std": 0.25,
            "risk_pct_mean": 1.5,
            "risk_pct_std": 0.5,
        }
    }


def test_formula_candidate_score_does_not_consume_simulated_outcome():
    row = _row("candidate-a", "AAAUSDT", ev=1.0, probability=0.6, quality=0.8, outcome="TP")
    before = score_candidate_components(row, stats=_stats(), friction_bps_per_fill=5.0)
    row["simulated_outcome"] = "SL"
    row["gross_pnl_per_100"] = -0.5
    after = score_candidate_components(row, stats=_stats(), friction_bps_per_fill=5.0)

    assert before == after


def test_all_formula_arms_select_from_same_candidate_pool_and_respect_slots():
    candidates = [
        _row("candidate-a", "AAAUSDT", ev=1.2, probability=0.65, quality=0.8),
        _row("candidate-b", "BBBUSDT", ev=0.8, probability=0.55, quality=0.5),
        _row("candidate-c", "CCCUSDT", ev=-0.1, probability=0.35, quality=0.9),
    ]
    for row in candidates:
        row["_components"] = score_candidate_components(row, stats=_stats(), friction_bps_per_fill=0.0)

    for formula in FORMULAS:
        selected, _ = _select_batch(
            formula,
            candidates,
            slots=2,
            correlation={("AAAUSDT", "BBBUSDT"): 0.4, ("BBBUSDT", "AAAUSDT"): 0.4},
            seed=17,
        )
        selected_ids = {row["candidate_id"] for row in selected}
        assert len(selected_ids) <= 2
        assert selected_ids <= {row["candidate_id"] for row in candidates}


def test_breakout_and_volatility_variants_share_one_canonical_family():
    assert CANONICAL_FAMILY_MAP["breakout_expansion"] == CANONICAL_FAMILY_MAP["volatility_expansion"]
    assert CANONICAL_FAMILY_MAP["cross_sectional_relative_strength"] == "cross_sectional_relative_strength"


def test_paired_cluster_bootstrap_includes_unselected_pool_as_zero_return():
    candidates = [
        {"candidate_id": "a", "dedupe_cluster_id": "cluster-a"},
        {"candidate_id": "b", "dedupe_cluster_id": "cluster-b"},
    ]
    baseline = _cluster_net_returns(candidates, [candidates[0]], {"a": 1.0})
    challenger = _cluster_net_returns(candidates, [candidates[1]], {"b": 2.0})

    report = _bootstrap_paired_difference(baseline, challenger, seed=7, replicates=50)

    assert baseline == {"cluster-a": 1.0, "cluster-b": 0.0}
    assert challenger == {"cluster-a": 0.0, "cluster-b": 2.0}
    assert report["clusters"] == 2
    assert report["mean_difference_net_pnl_per_100"] == 0.5