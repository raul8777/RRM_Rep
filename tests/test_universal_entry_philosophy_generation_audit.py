from datetime import datetime, timezone

from Dev.scripts.audit_universal_entry_philosophy_generation import (
    count_eligible_decision_slots,
    audit_cluster_lineage,
    summarize_disagreement_patterns,
    summarize_family_rates,
)


def test_eligible_slots_apply_warmup_contiguity_and_horizon_maturity():
    start = datetime(2025, 2, 5, tzinfo=timezone.utc).timestamp()
    bars = [{"open_time": start + index * 3600} for index in range(40)]

    assert count_eligible_decision_slots(
        bars, window_start=start, window_end=start + 40 * 3600,
    ) == 11


def test_family_rates_are_coverage_normalized_and_do_not_score_outcomes():
    proposals = [
        {"philosophy_family": "mean_reversion", "dedupe_cluster_id": "a", "symbol": "BTCUSDT", "decision_time": 1738800000},
        {"philosophy_family": "mean_reversion", "dedupe_cluster_id": "b", "symbol": "ETHUSDT", "decision_time": 1738803600},
    ]

    result = summarize_family_rates(proposals, covered_symbol_hours=100, eligible_decision_slots=50)

    assert result["mean_reversion"]["opportunities"] == 2
    assert result["mean_reversion"]["unique_clusters"] == 2
    assert result["mean_reversion"]["opportunities_per_1000_covered_symbol_hours"] == 20.0
    assert result["mean_reversion"]["opportunities_per_1000_mature_decision_slots"] == 40.0


def test_disagreement_summary_counts_opposing_and_same_side_theses_without_selecting_a_winner():
    common = {"dedupe_cluster_id": "c1", "symbol": "BTCUSDT", "decision_time": 100.0,
              "feature_snapshot": {"zscore_24h": 2.5, "return_4h": 0.02}}
    proposals = [
        {**common, "philosophy_family": "breakout_expansion", "philosophy_variant": "close_break", "side": "LONG", "thesis_type": "LONG_RESISTANCE_BREAKOUT"},
        {**common, "philosophy_family": "mean_reversion", "philosophy_variant": "zscore", "side": "SHORT", "thesis_type": "SHORT_MEAN_REVERSION"},
        {**common, "philosophy_family": "volatility_expansion", "philosophy_variant": "true_range", "side": "LONG", "thesis_type": "LONG_VOLATILITY_EXPANSION"},
    ]

    result = summarize_disagreement_patterns(proposals)

    assert result["shared_clusters"] == 1
    assert result["clusters_with_multiple_sides"] == 1
    assert result["pair_side_counts"]["breakout_expansion + mean_reversion | LONG+SHORT"] == 1
    assert result["pair_side_counts"]["breakout_expansion + volatility_expansion | LONG+LONG"] == 1
    assert result["review_scope"].endswith("row_by_row_review_pending")


def test_row_by_row_cluster_review_checks_identity_feature_symmetry_and_time_order():
    common = {
        "dedupe_cluster_id": "c1", "window_id": "dev", "window_role": "development",
        "symbol": "BTCUSDT", "decision_time": 100.0, "feature_available_at": 100.0,
        "entry_time": 100.0, "entry_price": 10.0, "feature_snapshot": {"zscore": 2.1},
        "label_status": "pending_contract_and_outcome_replay",
    }
    proposals = [
        {**common, "opportunity_event_id": "a", "philosophy_family": "breakout_expansion", "philosophy_variant": "close_break", "side": "LONG", "thesis_type": "LONG_BREAKOUT"},
        {**common, "opportunity_event_id": "b", "philosophy_family": "mean_reversion", "philosophy_variant": "zscore", "side": "SHORT", "thesis_type": "SHORT_REVERSION"},
    ]

    result = audit_cluster_lineage(proposals)

    assert result["passed"] is True
    assert result["reviewed_clusters"] == 1
    assert result["clusters_with_opposing_sides"] == 1
    assert result["clusters"][0]["feature_snapshots_identical"] is True


def test_row_by_row_cluster_review_accepts_the_explicit_validation_role():
    proposal = {
        "dedupe_cluster_id": "validation-cluster",
        "window_id": "validation-window",
        "window_role": "validation",
        "symbol": "BTCUSDT",
        "decision_time": 100.0,
        "feature_available_at": 100.0,
        "entry_time": 100.0,
        "entry_price": 10.0,
        "feature_snapshot": {"relative_strength_zscore_24h": 1.2},
        "label_status": "pending_contract_and_outcome_replay",
    }

    result = audit_cluster_lineage([proposal], expected_window_role="validation")

    assert result["passed"] is True
    assert result["reviewed_clusters"] == 1


def test_row_by_row_cluster_review_allows_family_specific_features_but_detects_within_family_drift():
    common = {
        "dedupe_cluster_id": "shared-source",
        "window_id": "dev",
        "window_role": "development",
        "symbol": "BTCUSDT",
        "decision_time": 100.0,
        "feature_available_at": 100.0,
        "entry_time": 100.0,
        "entry_price": 10.0,
        "label_status": "pending_contract_and_outcome_replay",
    }
    proposals = [
        {**common, "opportunity_event_id": "a", "philosophy_family": "mean_reversion", "philosophy_variant": "z1", "side": "LONG", "thesis_type": "LONG_REVERSION", "feature_snapshot": {"zscore": -2.1}},
        {**common, "opportunity_event_id": "b", "philosophy_family": "breakout_expansion", "philosophy_variant": "b1", "side": "LONG", "thesis_type": "LONG_BREAKOUT", "feature_snapshot": {"prior_high_break": True}},
        {**common, "opportunity_event_id": "c", "philosophy_family": "mean_reversion", "philosophy_variant": "z2", "side": "LONG", "thesis_type": "LONG_REVERSION", "feature_snapshot": {"zscore": -2.1}},
    ]

    result = audit_cluster_lineage(proposals)
    assert result["passed"] is True
    assert result["feature_mismatch_clusters"] == 0

    proposals[2]["feature_snapshot"] = {"zscore": -1.9}
    drifted = audit_cluster_lineage(proposals)
    assert drifted["passed"] is False
    assert drifted["feature_mismatch_clusters"] == 1