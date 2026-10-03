import pytest

import Dev.scripts.analyze_universal_entry_market_context as market_context
from Dev.scripts.analyze_universal_entry_market_context import closed_btc_return_4h, evaluate_gate
from Dev.scripts.analyze_universal_entry_signal_execution_basis import (
    _exclusive_trigger_margin,
    _validate_feature_signal_parity,
    _summarize as summarize_signal_execution,
    compare_signal_sets,
    paired_feature_diagnostic,
    paired_delta_summary,
)

from Dev.scripts.score_universal_entry_minimal_lifecycle import (
    cluster_mean_summary,
    concordance_filter_summary,
    overlap_summary,
    summarize,
)
from Dev.scripts.screen_ten_universal_entry_strategies import _metric_summary, strategy_selects
from Dev.scripts.screen_1000_universal_entry_formulas import (
    build_candidates,
    confirmation_passes as formula_confirmation_passes,
)
from Dev.scripts.screen_100000_universal_entry_formulas import (
    attach_prior_volume_reference,
    build_candidates as build_100000_formula_candidates,
    confirmation_passes_corrected,
    prior_volume_ratio_20,
)
from Dev.scripts.screen_100000_market_regime_formulas import (
    MACRO_MODES,
    MACRO_LEVELS,
    _market_contexts,
    _regime_passes,
    build_candidates as build_market_regime_candidates,
)
from Dev.scripts.screen_100000_scenario_switch_formulas import (
    ACTIONS as SCENARIO_ACTIONS,
    MARKET_STATES,
    _score_assignment as score_scenario_assignment,
    build_assignments as build_scenario_assignments,
    classify_market_state,
    assignment_from_id as scenario_assignment_from_id,
)
from Dev.scripts.screen_10000_universal_entry_formulas import (
    build_candidates as build_10000_formula_candidates,
    candidate_selects as formula_10000_selects,
    confirmation_passes as formula_10000_confirmation_passes,
)

from Dev.scripts.analyze_universal_entry_stop_survival import (
    _notional_for_max_loss_budget,
    _post_stop_recovery_after_costs,
    _stop_net_per_100,
    replay_entry_stop_only,
    score_minimal_lifecycle,
)


@pytest.mark.parametrize("side,close,stop", [("LONG", 102.0, 95.0), ("SHORT", 98.0, 105.0)])
def test_minimal_lifecycle_closes_survivor_and_charges_both_fills(side, close, stop):
    proposal = {"side": side, "entry_price_um": 100.0, "entry_time": 0}
    result = score_minimal_lifecycle(proposal, {
        "label_valid": True, "stop_price_theoretical_unrounded": stop,
        "stop_triggered": False, "horizon_close": close, "gap_through": False,
    })
    direction = 1.0 if side == "LONG" else -1.0
    entry_fill = 100.0 * (1.0 + direction * 0.001)
    exit_fill = close * (1.0 - direction * 0.001)
    expected = direction * (exit_fill - entry_fill) - (entry_fill + exit_fill) * 0.0005
    assert result["exit_reason"] == "horizon_close"
    assert result["exit_time_upper_bound"] == 14400
    assert result["scenarios"]["slippage_10bps_per_fill"]["net_usdt_no_funding"] == pytest.approx(expected)


def test_minimal_lifecycle_stop_wins_and_sizing_is_fixed_across_scenarios():
    proposal = {"side": "LONG", "entry_price_um": 100.0, "entry_time": 0}
    result = score_minimal_lifecycle(proposal, {
        "label_valid": True, "stop_price_theoretical_unrounded": 95.0,
        "stop_triggered": True, "stop_fill_reference_price": 94.0,
        "trigger_time": 60, "horizon_close": 110.0, "gap_through": True,
    }, risk_budget_usdt=1.0)
    assert result["exit_reference_price"] == 94.0
    assert result["exit_time_upper_bound"] == 120
    assert result["planned_initial_risk_usdt_20bps"] == pytest.approx(1.0)
    assert result["reference_notional_usdt"] < 100.0
    assert result["scenarios"]["slippage_20bps_per_fill"]["net_usdt_no_funding"] < -1.0
    assert result["scenarios"]["slippage_0bps_per_fill"]["net_usdt_no_funding"] > result["scenarios"]["slippage_20bps_per_fill"]["net_usdt_no_funding"]


def test_minimal_lifecycle_rejects_invalid_path():
    with pytest.raises(ValueError, match="complete path"):
        score_minimal_lifecycle({}, {"label_valid": False})


def test_cluster_mean_does_not_overweight_repeated_proposals():
    result = cluster_mean_summary([("first", 1.0), ("first", 3.0), ("second", 8.0)])
    assert result["clusters"] == 2
    assert result["cluster_equal_mean"] == 5.0
    assert result == cluster_mean_summary([("first", 1.0), ("first", 3.0), ("second", 8.0)])


def test_overlap_deduplicates_events_and_separates_opposing_sides():
    rows = []
    for family, side in [("mean_reversion", "LONG"), ("mean_reversion", "LONG"), ("breakout_volatility", "LONG"), ("breakout_volatility", "SHORT")]:
        rows.append({
            "family": family, "side": side, "symbol": "BTCUSDT", "entry_time": 0, "stop": "1.5",
            "lifecycle": {"scenarios": {"slippage_10bps_per_fill": {"net_per_100_reference_notional": 1.0}}},
        })
    result = overlap_summary(rows)[0]
    assert result["same_time_symbol_side_events"] == 1
    assert result["jaccard_pct"] == 50.0
    assert result["opposite_side_same_time_symbol_events"] == 1
    assert result["shared_event_return_correlation"] is None


def test_summary_counts_horizon_and_stop_outcomes_with_all_costs():
    proposal = {"side": "LONG", "entry_price_um": 100.0, "entry_time": 0}
    rows = []
    for triggered, close in [(False, 102.0), (True, 110.0)]:
        lifecycle = score_minimal_lifecycle(proposal, {
            "label_valid": True, "stop_price_theoretical_unrounded": 95.0,
            "stop_triggered": triggered, "horizon_close": close, "gap_through": False,
            "trigger_time": 60, "stop_fill_reference_price": 95.0,
        })
        rows.append({"cluster": str(triggered), "lifecycle": lifecycle})
    summary = summarize(rows)
    values = [row["lifecycle"]["scenarios"]["slippage_10bps_per_fill"]["net_usdt_no_funding"] for row in rows]
    assert summary["stop_pct"] == 50.0
    assert summary["scenarios"]["slippage_10bps_per_fill"]["mean_net_usdt"] == pytest.approx(sum(values) / 2.0)
    assert summary["scenarios"]["slippage_10bps_per_fill"]["positive_close_pct"] == 50.0
    assert summary["scenarios"]["slippage_10bps_per_fill"]["profit_factor"] == pytest.approx(values[0] / -values[1])


def test_concordance_filter_deduplicates_anchor_and_leaves_abstentions_zero():
    rows = []
    for family, timestamp in [("breakout_volatility", 0), ("breakout_volatility", 0), ("cross_sectional_relative_strength", 0), ("breakout_volatility", 60)]:
        rows.append({
            "family": family, "side": "LONG", "symbol": "BTCUSDT", "entry_time": timestamp, "stop": "1.5",
            "lifecycle": {"scenarios": {
                f"slippage_{bps}bps_per_fill": {"net_usdt_no_funding": 1.0 if timestamp == 0 else -2.0}
                for bps in (0, 5, 10, 20)
            }},
        })
    result = concordance_filter_summary(rows)["by_side"]["LONG"]
    scenario = result["scenarios"]["slippage_10bps_per_fill"]
    assert result["unique_anchor_events"] == 2
    assert result["selected_unique_events"] == 1
    assert result["coverage_pct"] == 50.0
    assert scenario["selected_event_mean_net_usdt"] == 1.0
    assert scenario["filter_net_per_anchor_opportunity_usdt_abstentions_zero"] == 0.5
    assert scenario["paired_selection_delta_per_anchor_opportunity_usdt"] == 1.0
    assert concordance_filter_summary([])["status"] == "not_evaluable_missing_component"


def test_max_loss_budget_scales_notional_for_wider_stops_and_costs():
    assert _notional_for_max_loss_budget(
        max_loss_usdt=10.0,
        stop_distance_frac=0.15,
        fee_bps=5.0,
        slippage_bps=10.0,
    ) == pytest.approx(10.0 / 0.153)


def test_btc_context_uses_closed_contiguous_history_and_ignores_future():
    bars = [{"open_time": index * 3600, "close": 100.0 - index} for index in range(7)]
    assert closed_btc_return_4h(bars, 5 * 3600) == pytest.approx(-0.04)
    bars[5]["close"] = 10000.0
    assert closed_btc_return_4h(bars, 5 * 3600) == pytest.approx(-0.04)
    assert closed_btc_return_4h(bars[:4], 5 * 3600) is None
    assert closed_btc_return_4h([bar for index, bar in enumerate(bars) if index != 2], 5 * 3600) is None


def test_context_gate_preserves_missingness_and_abstention_economics():
    events = []
    for cluster, context, net in [("selected", -0.01, 1.0), ("rejected", 0.01, -2.0), ("missing", None, -99.0)]:
        events.append({"cluster": cluster, "btc_return_4h": context, "net_r": net,
                       "nets": {f"slippage_{bps}bps_per_fill": net for bps in (0, 5, 10, 20)}})
    result = evaluate_gate(events)
    assert result["eligible_events"] == 2
    assert result["missing_context_events"] == 1
    assert result["selected_events"] == 1
    scenario = result["scenarios"]["slippage_10bps_per_fill"]
    assert scenario["selected_mean_net_usdt"] == 1.0
    assert scenario["filter_net_per_eligible_opportunity_abstentions_zero"] == 0.5
    assert scenario["selection_delta_per_eligible_opportunity"] == 1.0


def test_context_analysis_refuses_validation_before_reading_tape(tmp_path, monkeypatch):
    report_dir = tmp_path / "minimal_lifecycle_v2"
    report_dir.mkdir()
    (report_dir / "report.json").write_text(
        '{"windows":{"validation":{"role":"explored_validation"}}}', encoding="utf-8",
    )
    monkeypatch.setattr(market_context, "ROOT", tmp_path)
    monkeypatch.setattr(market_context, "_sha256", lambda path: pytest.fail("validation tape must not be read"))
    with pytest.raises(ValueError, match="must not inspect validation"):
        market_context.analyze_development_window("validation", tmp_path)


def test_signal_source_comparison_matches_exact_events_and_counts_side_conflicts():
    def proposal(side, family="mean_reversion", decision=100):
        return {"symbol": "BTCUSDT", "philosophy_family": family, "philosophy_variant": "v1",
            "decision_time": decision, "side": side,
            "feature_snapshot": {"zscore_24h": 2.1}}

    result = compare_signal_sets(
        [proposal("LONG"), proposal("SHORT", decision=200)],
        [proposal("LONG"), proposal("LONG", decision=200)],
    )
    assert result["spot_signal_count"] == result["um_signal_count"] == 2
    assert result["exact_event_side_agreement"] == 1
    assert result["exact_event_side_jaccard_pct"] == pytest.approx(100.0 / 3.0)
    assert result["same_observation_both_signal_sides_conflict_count"] == 1
    assert result["side_conflict_examples"][0]["spot_side"] == "SHORT"
    assert result["side_conflict_examples"][0]["um_side"] == "LONG"


def test_exclusive_signal_margins_use_family_specific_generator_thresholds():
    assert _exclusive_trigger_margin({
        "philosophy_family": "mean_reversion", "side": "LONG",
        "feature_snapshot": {"zscore_24h": -2.02},
    }) == ("absolute_zscore_excess_over_2", pytest.approx(0.02))
    assert _exclusive_trigger_margin({
        "philosophy_family": "breakout_expansion", "side": "SHORT",
        "feature_snapshot": {"close": 98.0, "prior_24h_low": 100.0, "atr_24h": 4.0},
    }) == ("signed_break_distance_in_generator_atr", pytest.approx(0.5))
    assert _exclusive_trigger_margin({
        "philosophy_family": "volatility_expansion", "side": "LONG",
        "feature_snapshot": {"true_range_expansion_ratio": 2.03},
    }) == ("true_range_ratio_excess_over_2_before_body_gate", pytest.approx(0.03))


def test_paired_feature_diagnostic_counts_threshold_flip_and_detects_generator_drift():
    spot = {
        ("BTCUSDT", 100): {"pilot_generator_eligible": True, "zscore_24h": 2.1, "return_4h": 0.0,
            "previous_returns": [], "source_close": 99.0, "prior_24h_high": 100.0, "prior_24h_low": 90.0,
            "atr_24h": 2.0, "true_range_expansion_ratio": 1.0, "body_return": 0.0,
            "relative_strength_zscore_24h": 0.0, "cross_section_size": 2},
        ("ETHUSDT", 100): {"pilot_generator_eligible": True, "zscore_24h": 0.0, "return_4h": 0.0,
            "previous_returns": [], "source_close": 99.0, "prior_24h_high": 100.0, "prior_24h_low": 90.0,
            "atr_24h": 2.0, "true_range_expansion_ratio": 1.0, "body_return": 0.0,
            "relative_strength_zscore_24h": 0.0, "cross_section_size": 2},
    }
    um = {key: dict(value) for key, value in spot.items()}
    um[("BTCUSDT", 100)]["zscore_24h"] = 1.9
    diagnostic = paired_feature_diagnostic(spot, um)
    assert diagnostic["paired_observation_count"] == 2
    assert diagnostic["signal_rule_agreement_by_family"]["mean_reversion"]["trigger_state_or_side_disagreements"] == 1
    assert diagnostic["feature_differences"]["zscore_24h"]["abs_delta_p50"] == pytest.approx(0.1)

    expected = [{"symbol": "BTCUSDT", "decision_time": 100, "philosophy_family": "mean_reversion",
                 "philosophy_variant": "zscore_24h_2", "side": "SHORT"}]
    assert _validate_feature_signal_parity(spot, expected) == {}
    assert _validate_feature_signal_parity(um, expected) == {"mean_reversion": 1}


def test_execution_venue_delta_uses_equal_cluster_weighting():
    result = paired_delta_summary([
        {"cluster": "one", "delta": 1.0},
        {"cluster": "one", "delta": 3.0},
        {"cluster": "two", "delta": 8.0},
    ])
    assert result["paired_events"] == 3
    assert result["mean_delta_net_usdt"] == 4.0
    assert result["cluster_summary"]["cluster_equal_mean"] == 5.0


def test_signal_execution_group_summary_uses_both_venue_scenario_rows():
    def row(cluster, net, stop):
        return {"cluster": cluster, "execution": {
            "lifecycle": {"exit_reason": "initial_stop" if stop else "horizon_close", "scenarios": {
                f"slippage_{bps}bps_per_fill": {
                    "net_usdt_no_funding": net - bps / 100.0,
                    "net_per_100_reference_notional": (net - bps / 100.0) / 2.0,
                    "net_r_no_funding": (net - bps / 100.0) / 2.0,
                } for bps in (0, 5, 10, 20)
            }},
        }}

    result = summarize_signal_execution([row("a", 2.0, True), row("b", -1.0, False)])
    assert result["event_count"] == 2
    assert result["stop_pct"] == 50.0
    assert result["scenarios"]["slippage_10bps_per_fill"]["mean_net_usdt"] == pytest.approx(0.4)


def test_ten_strategy_overlays_apply_frozen_trend_volume_and_relative_rules():
    proposal = {
        "philosophy_family": "breakout_expansion", "side": "LONG",
        "feature_snapshot": {"predictor_indicators": {
            "trend_4h": {"value": 1.0}, "vol_spike": {"value": 1.1},
        }},
    }
    assert strategy_selects("05_breakout_base", proposal)
    assert strategy_selects("06_breakout_volume_confirmed", proposal)
    proposal["feature_snapshot"]["predictor_indicators"]["vol_spike"]["value"] = 0.99
    assert strategy_selects("05_breakout_base", proposal)
    assert not strategy_selects("06_breakout_volume_confirmed", proposal)

    proposal["philosophy_family"] = "mean_reversion"
    assert strategy_selects("02_mean_reversion_trend_aligned", proposal)
    proposal["side"] = "SHORT"
    assert not strategy_selects("02_mean_reversion_trend_aligned", proposal)

    proposal.update({"philosophy_family": "cross_sectional_relative_strength", "side": "LONG"})
    proposal["feature_snapshot"]["relative_strength_zscore_24h"] = 0.99
    assert not strategy_selects("10_cross_sectional_relative_strength_high_conviction", proposal)
    proposal["feature_snapshot"]["relative_strength_zscore_24h"] = 1.0
    assert strategy_selects("10_cross_sectional_relative_strength_high_conviction", proposal)


def test_ten_strategy_net_per_original_opportunity_counts_abstentions_as_zero():
    def make_row(selected, net):
        return {
            "event_id": str(net), "cluster": str(net), "selected": selected,
            "net_r_10bps": net,
            "net_by_cost": {
                f"slippage_{bps}bps_per_fill": net - bps / 100.0
                for bps in (0, 10, 20)
            },
        }

    result = _metric_summary([make_row(True, 2.0), make_row(False, -1.0)], baseline_count=2)
    assert result["selected_events"] == 1
    assert result["scenarios"]["slippage_10bps_per_fill"]["selected_mean_net_usdt"] == pytest.approx(1.9)
    assert result["scenarios"]["slippage_10bps_per_fill"]["net_per_source_family_opportunity_abstentions_zero"] == pytest.approx(0.95)


def test_1000_formula_grid_is_unique_and_cross_section_confirmations_use_breadth():
    candidates = build_candidates()
    assert len(candidates) == len({candidate["strategy_id"] for candidate in candidates}) == 1000
    proposal = {
        "philosophy_family": "cross_sectional_relative_strength", "side": "LONG",
        "feature_snapshot": {"relative_strength_zscore_24h": 0.6, "return_24h": 0.02, "cross_section_size": 12},
    }
    assert formula_confirmation_passes("activity_1", "cross_sectional_relative_strength", proposal)
    assert not formula_confirmation_passes("activity_1_25", "cross_sectional_relative_strength", proposal)
    assert not formula_confirmation_passes("strong", "cross_sectional_relative_strength", proposal)


def test_10000_formula_grid_has_exact_family_side_trigger_confirmation_product():
    candidates = build_10000_formula_candidates()
    assert len(candidates) == len({candidate["strategy_id"] for candidate in candidates}) == 10000
    assert len({(c["family"], c["side"], c["trigger_level_index"], c["confirmation_mode"], c["confirmation_level_index"]) for c in candidates}) == 10000


def test_10000_formula_candidate_uses_fixed_trigger_and_relative_confirmation():
    candidate = {
        "family": "cross_sectional_relative_strength", "side": "LONG",
        "trigger_margin_min": 0.1,
        "confirmation_mode": "rsi_or_relative_extreme", "confirmation_level": 0.75,
    }
    proposal = {
        "philosophy_family": "cross_sectional_relative_strength", "side": "LONG",
        "feature_snapshot": {"relative_strength_zscore_24h": 0.8},
    }
    assert formula_10000_confirmation_passes("rsi_or_relative_extreme", 0.75, candidate["family"], proposal)
    assert formula_10000_selects(candidate, proposal) is True
    proposal["feature_snapshot"]["relative_strength_zscore_24h"] = 0.6
    assert formula_10000_selects(candidate, proposal) is False


def test_prior_volume_ratio_excludes_the_signal_candle_from_its_baseline():
    bars = [
        {"open_time": index * 3600, "volume": 10.0 if index < 20 else 20.0}
        for index in range(21)
    ]
    corrected = prior_volume_ratio_20(bars, 20 * 3600)
    inclusive_old = bars[-1]["volume"] / sum(bar["volume"] for bar in bars[-20:]) * 20.0
    assert corrected == 2.0
    assert inclusive_old == pytest.approx(20.0 / 10.5)
    with pytest.raises(ValueError, match="warmup"):
        prior_volume_ratio_20(bars[1:], 20 * 3600)
    gapped = [{"open_time": index * 3600, "volume": 10.0} for index in range(20)]
    gapped.append({"open_time": 21 * 3600, "volume": 20.0})
    with pytest.raises(ValueError, match="not contiguous"):
        prior_volume_ratio_20(gapped, 21 * 3600)


def test_prior_volume_enrichment_supports_relative_strength_without_predictor_snapshot():
    proposal = {"source_candle_open_time": 20 * 3600, "feature_snapshot": {"relative_strength_zscore_24h": -0.8}}
    bars = [{"open_time": index * 3600, "volume": 10.0 if index < 20 else 20.0} for index in range(21)]
    attach_prior_volume_reference(proposal, bars)
    assert proposal["feature_snapshot"]["prior_volume_ratio_20"] == 2.0
    assert "predictor_indicators" not in proposal["feature_snapshot"]
    assert confirmation_passes_corrected(
        "activity", 0.75, "cross_sectional_relative_strength",
        {"side": "SHORT", "feature_snapshot": {"cross_section_size": 10}},
    ) is True


def test_100000_activity_confirmation_uses_corrected_prior_volume_ratio():
    proposal = {
        "philosophy_family": "breakout_expansion", "side": "LONG",
        "feature_snapshot": {
            "prior_volume_ratio_20": 2.0,
            "predictor_indicators": {"vol_spike": {"value": 1.5}},
        },
    }
    assert confirmation_passes_corrected("activity", 1.75, "breakout_expansion", proposal)


def test_100000_formula_grid_has_exact_count_and_two_confirmation_clauses():
    candidates = build_100000_formula_candidates()
    assert len(candidates) == len({candidate["strategy_id"] for candidate in candidates}) == 100000
    assert all(candidate["confirmation_1_mode"] and candidate["confirmation_2_mode"] for candidate in candidates)
    assert all(candidate["confirmation_1_definition"] and candidate["confirmation_2_definition"] for candidate in candidates)


def test_stop_exit_deducts_round_trip_slippage_once():
    net = _stop_net_per_100(
        side="LONG",
        entry=100.0,
        fill_reference=95.0,
        notional=100.0,
        fee_bps=5.0,
        slippage_bps=10.0,
    )
    assert net == pytest.approx(-5.0 - 0.0974525 - 0.195)


def test_market_regime_grid_has_exact_100k_unique_specs_and_context_modes():
    candidates = build_market_regime_candidates()
    assert len(candidates) == len({candidate["strategy_id"] for candidate in candidates}) == 100000
    assert {candidate["market_regime_1"] for candidate in candidates} == set(MACRO_MODES)
    assert {candidate["market_regime_2"] for candidate in candidates} == set(MACRO_MODES)
    assert {candidate["regime_1_level_index"] for candidate in candidates} == set(range(10))
    assert all(len(MACRO_LEVELS[mode]) == 10 for mode in MACRO_MODES)


def test_market_context_uses_closed_um_hours_and_does_not_read_future_bars():
    symbols = ["BTCUSDT"] + [f"COIN{index}USDT" for index in range(19)]
    hourly = {}
    for symbol_index, symbol in enumerate(symbols):
        hourly[symbol] = [
            {"open_time": index * 3600, "open": 100.0, "high": 101.0 + symbol_index * 0.01,
             "low": 99.0, "close": 100.0 + index * 0.01 * (1 if symbol_index % 2 == 0 else -1),
             "volume": 10.0 if index < 24 else 30.0}
            for index in range(27)
        ]
    first = _market_contexts(hourly, {25 * 3600})[25 * 3600]
    assert first["btc_source_open_time"] == 24 * 3600
    assert first["btc_volume_ratio_prior20"] == 3.0
    assert first["context_symbol_count"] == 20
    hourly["BTCUSDT"][25]["close"] = 1_000_000.0
    hourly["BTCUSDT"][26]["volume"] = 1_000_000.0
    second = _market_contexts(hourly, {25 * 3600})[25 * 3600]
    assert second == first


def test_market_regime_modes_match_feature_units_and_trade_side():
    context = {
        "btc_return_4h": -0.01, "btc_return_24h": -0.02,
        "market_breadth_long_4h": 0.25, "market_breadth_long_24h": 0.2,
        "market_dispersion_4h": 0.01, "market_dispersion_24h": 0.04,
        "btc_atr_pct": 0.5, "market_median_atr_pct": 0.4,
        "market_volatility_breadth": 0.3, "market_volume_breadth": 0.6,
    }
    assert _regime_passes({"market_regime": "btc_4h_direction", "regime_level": 0.005, "side": "SHORT"}, context)
    assert not _regime_passes({"market_regime": "btc_4h_direction", "regime_level": 0.005, "side": "LONG"}, context)
    assert _regime_passes({"market_regime": "market_4h_breadth", "regime_level": 0.7, "side": "SHORT"}, context)
    assert _regime_passes({"market_regime": "market_4h_dispersion", "regime_level": 0.01, "side": "LONG"}, context)
    assert _regime_passes({"market_regime": "market_24h_dispersion", "regime_level": 0.04, "side": "SHORT"}, context)


def test_scenario_switch_grid_is_exact_product_and_market_states_are_disjoint():
    candidates = build_scenario_assignments()
    assert len(candidates) == len({candidate["strategy_id"] for candidate in candidates}) == 100000
    for index in (0, len(candidates) // 2, len(candidates) - 1):
        assert scenario_assignment_from_id(candidates[index]["strategy_id"]) == candidates[index]
    risk_off = {"btc_return_4h": -0.01, "market_breadth_long_4h": 0.05, "market_volatility_breadth": 0.8}
    risk_on = {"btc_return_4h": 0.01, "market_breadth_long_4h": 0.95, "market_volatility_breadth": 0.8}
    assert classify_market_state(risk_off) == "broad_risk_off"
    assert classify_market_state(risk_on) == "broad_risk_on"
    assert len(MARKET_STATES) == 5 and len(SCENARIO_ACTIONS) == 10


def test_scenario_policy_net_is_compared_to_static_baseline_not_its_own_delta():
    components = {state: {} for state in MARKET_STATES}
    for state in MARKET_STATES:
        for action in SCENARIO_ACTIONS:
            components[state][action] = {
                "selected_events": 0, "clusters": [], "selection_sha256": "empty",
                "net_sums": {10: 0.0, 20: 0.0},
                "gains": {10: 0.0, 20: 0.0}, "losses": {10: 0.0, 20: 0.0},
            }
    components["broad_risk_off"]["mean_reversion|LONG"] = {
        "selected_events": 1, "clusters": ["cluster-1"], "selection_sha256": "one",
        "net_sums": {10: 2.0, 20: 1.0},
        "gains": {10: 2.0, 20: 1.0}, "losses": {10: 0.0, 20: 0.0},
    }
    assignment = {"strategy_id": "test", "actions_by_state": {state: "__ABSTAIN__" for state in MARKET_STATES}}
    assignment["actions_by_state"]["broad_risk_off"] = "mean_reversion|LONG"
    scored = score_scenario_assignment(assignment, components, {state: 100 for state in MARKET_STATES}, 500, {10: 0.002, 20: 0.001})
    assert scored["scenarios"][10]["net_per_unique_source_opportunity_abstentions_zero"] == pytest.approx(0.004)
    assert scored["scenarios"][10]["best_static_net_per_unique_source_opportunity"] == pytest.approx(0.002)
    assert scored["scenarios"][10]["paired_delta_vs_best_static_per_opportunity"] == pytest.approx(0.002)
    assert scored["genuine_switch"] is True


def test_counterfactual_recovery_must_clear_round_trip_costs_after_trigger():
    assert _post_stop_recovery_after_costs(
        stop_triggered=True,
        post_trigger_mfe_pct=0.3,
        fee_bps_per_fill=5.0,
        slippage_bps_per_fill=10.0,
    ) is True
    assert _post_stop_recovery_after_costs(
        stop_triggered=True,
        post_trigger_mfe_pct=0.299,
        fee_bps_per_fill=5.0,
        slippage_bps_per_fill=10.0,
    ) is False
    assert _post_stop_recovery_after_costs(
        stop_triggered=False,
        post_trigger_mfe_pct=1.0,
        fee_bps_per_fill=5.0,
        slippage_bps_per_fill=10.0,
    ) is False


def test_long_stop_uses_trigger_and_excludes_stop_bar_favorable_wick():
    bars = [
        {"open_time": 0, "open": 100.0, "high": 100.4, "low": 99.8, "close": 100.1},
        {"open_time": 60, "open": 100.1, "high": 103.0, "low": 94.0, "close": 95.0},
    ]
    result = replay_entry_stop_only(
        bars,
        side="LONG",
        entry_time=0,
        entry_price=100.0,
        stop_price=95.0,
        horizon_minutes=2,
    )

    assert result["stop_triggered"] is True
    assert result["stop_fill_reference_price"] == 95.0
    assert result["mfe_before_trigger_pct"] == pytest.approx(0.4)
    assert result["mfe_after_trigger_bar_pct"] == 0.0
    assert result["full_horizon_mfe_pct"] == pytest.approx(3.0)


def test_short_gap_through_fills_at_open_and_marks_counterfactual_recovery():
    bars = [
        {"open_time": 0, "open": 100.0, "high": 100.2, "low": 99.8, "close": 100.0},
        {"open_time": 60, "open": 106.0, "high": 107.0, "low": 99.0, "close": 100.0},
    ]
    result = replay_entry_stop_only(
        bars,
        side="SHORT",
        entry_time=0,
        entry_price=100.0,
        stop_price=105.0,
        horizon_minutes=2,
    )

    assert result["stop_triggered"] is True
    assert result["gap_through"] is True
    assert result["stop_fill_reference_price"] == 106.0
    assert result["mfe_before_trigger_pct"] == pytest.approx(0.2004008)
    assert result["mfe_after_trigger_bar_pct"] == 0.0
    assert result["full_horizon_mfe_pct"] == pytest.approx(1.0101010)


def test_stop_only_path_survives_without_tp_or_lifecycle_exit():
    bars = [
        {"open_time": 0, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5},
        {"open_time": 60, "open": 100.5, "high": 102.0, "low": 100.0, "close": 101.5},
    ]
    result = replay_entry_stop_only(
        bars,
        side="LONG",
        entry_time=0,
        entry_price=100.0,
        stop_price=95.0,
        horizon_minutes=2,
    )

    assert result["stop_triggered"] is False
    assert result["stop_fill_reference_price"] is None
    assert result["mfe_before_trigger_pct"] == pytest.approx(2.0)
    assert result["full_horizon_mfe_pct"] == pytest.approx(2.0)