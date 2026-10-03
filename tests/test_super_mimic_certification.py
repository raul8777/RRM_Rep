import json

import pandas as pd
import pytest

from super_mimic.core import DesignSpec
from super_mimic.certification import build_differentiation_report, build_invariant_report, validate_parameter_scope
from super_mimic.deploy_gate import DeployGate
from super_mimic.engines import _best_recommendation, _effective_worker_count, run_aic_certification_experiment
from super_mimic.kelly_certification import run_kelly_certification
from super_mimic.lifecycle_certification import _base_policy, _load_entry_cohort, _replay_policy, run_lifecycle_certification
from super_mimic.window_certification import _timestamps, certify_window
from simulation.mimic_clients import SimulationFuturesClient, SimulationSpotMarginClient
from simulation.mimic_protocols import SimulationClock
from simulation.mimic_runner import SimulationRunner
from Dev.scripts.download_historical_data import _fetch_klines_range
from Dev.scripts.mimic_mode_profile_sweep import (
    _already_ran,
    _derive_e2e_evidence,
    _derive_entry_funnel,
    _derive_metrics,
    _job_key,
    _select_stratified_jobs,
)


def test_certification_design_is_safe_and_bounded():
    design = DesignSpec(
        experiment_stage="entry",
        certification_mode=True,
        apply_allowed=True,
        max_workers=99,
        max_jobs=999,
    )

    assert design.apply_allowed is False
    assert design.max_workers == 10
    assert design.max_jobs == 999
    assert design.auto_start is True


def test_certification_design_defaults_to_autonomous_eight_workers():
    design = DesignSpec()

    assert design.certification_mode is True
    assert design.auto_start is True
    assert design.max_workers == 8
    assert design.max_jobs == 100


def test_certification_design_rejects_unknown_stage():
    with pytest.raises(ValueError, match="Unsupported experiment_stage"):
        DesignSpec(experiment_stage="unknown")


def test_design_resolves_mode_to_the_forced_regime():
    design = DesignSpec(mode="spot_bull", profile="b", profiles=["A", "B", "C"])

    assert design.mode == "SPOT_BULL"
    assert design.forced_regime == "SPOT_BULL"
    assert design.profile == "B"


def test_design_rejects_mode_regime_divergence():
    with pytest.raises(ValueError, match="mode and forced_regime must match"):
        DesignSpec(mode="SPOT_BULL", forced_regime="RANGE_SCALP")


def test_design_rejects_requested_profile_absent_from_sweep():
    with pytest.raises(ValueError, match="Requested profile A is absent"):
        DesignSpec(profile="A", profiles=["B", "C"])


def test_recommendation_does_not_fall_back_to_another_profile():
    report = {"recommendations": [{"profile": "B", "best_params": {"risk": 0.5}}]}

    assert _best_recommendation(report, "A") is None


def test_deploy_audit_uses_pre_apply_rollback_snapshot(monkeypatch):
    class FakeDb:
        def execute_query(self, _query, _params):
            return [{"value": json.dumps({"risk": "new"})}]

    captured = {}

    def record_deployment(self, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        "super_mimic.core.ExperimentRegistry.record_deployment",
        record_deployment,
    )
    gate = DeployGate(FakeDb())
    old_snapshot = {"risk": "old", "legacy_field": True}

    gate.apply_recommended_config(
        "experiment-1",
        {"risk": "new"},
        operator="staging-operator",
        rollback_payload=old_snapshot,
    )

    assert captured["rollback_payload"] == old_snapshot


def test_reduced_design_round_trip_keeps_sampling_fields():
    design = DesignSpec(stratified_jobs=True, max_symbols=12)
    restored = DesignSpec.from_dict(design.to_dict())

    assert restored.stratified_jobs is True
    assert restored.max_symbols == 12


def test_stratified_jobs_balance_every_window_and_profile():
    jobs = [
        {"window": {"window_id": window}, "profile": profile, "params": {"value": value}}
        for window in ("w1", "w2")
        for profile in ("A", "B", "C")
        for value in range(20)
    ]

    selected = _select_stratified_jobs(jobs, 40, 7)
    counts = {
        (window, profile): sum(
            1 for job in selected
            if job["window"]["window_id"] == window and job["profile"] == profile
        )
        for window in ("w1", "w2")
        for profile in ("A", "B", "C")
    }

    assert len(selected) == 40
    assert sum(count for (window, _), count in counts.items() if window == "w1") == 20
    assert sum(count for (window, _), count in counts.items() if window == "w2") == 20
    assert max(counts.values()) - min(counts.values()) <= 1
    assert selected == _select_stratified_jobs(jobs, 40, 7)


def test_effective_workers_respects_requested_cap():
    assert 1 <= _effective_worker_count(10, 1024) <= 10
    assert _effective_worker_count(1, 1024) == 1


def test_sweep_resume_identity_is_stable_and_parameter_order_independent():
    params = {"min_signal_strength": 0.6, "stop_loss_pct": 3.36}
    reordered = {"stop_loss_pct": 3.36, "min_signal_strength": 0.6}
    existing = [{"window_id": "w1", "profile": "A", "params": reordered}]

    assert _job_key("w1", "A", params) == _job_key("w1", "A", reordered)
    assert _already_ran(existing, "w1", "A", params) is True
    assert _already_ran(existing, "w2", "A", params) is False


def test_design_round_trip_keeps_certification_fields():
    original = DesignSpec(experiment_stage="aic", seed=7, max_jobs=42)
    restored = DesignSpec.from_dict(json.loads(json.dumps(original.to_dict())))

    assert restored.experiment_stage == "aic"
    assert restored.seed == 7
    assert restored.max_jobs == 42
    assert restored.apply_allowed is False


def test_timestamp_detection_supports_seconds_and_milliseconds():
    seconds = pd.Series([1_700_000_000, 1_700_003_600])
    milliseconds = pd.Series([1_700_000_000_000, 1_700_003_600_000])

    assert _timestamps(seconds).iloc[0] == _timestamps(milliseconds).iloc[0]


def test_window_certificate_validates_ohlcv_and_btc_context(tmp_path):
    rows = pd.DataFrame({
        "open_time": [1_700_000_000_000, 1_700_003_600_000],
        "open": [10.0, 11.0],
        "high": [11.0, 12.0],
        "low": [9.0, 10.0],
        "close": [10.5, 11.5],
        "volume": [100.0, 120.0],
    })
    for name in ("BTCUSDT_1h", "BTCUSDT_4h", "BTCUSDT_1d", "ETHUSDT_1h"):
        rows.to_parquet(tmp_path / f"{name}.parquet", index=False)

    certificate = certify_window(
        {"window_id": "test", "symbols": ["BTCUSDT", "ETHUSDT"]},
        str(tmp_path),
        "entry",
    )

    assert certificate["certified"] is True
    assert certificate["status"] == "certified"
    assert certificate["intrabar_fidelity"] == "low"


def test_window_certificate_rejects_missing_symbol_data(tmp_path):
    certificate = certify_window(
        {"window_id": "test", "symbols": ["MISSINGUSDT"]},
        str(tmp_path),
        "entry",
    )

    assert certificate["certified"] is False
    assert "MISSINGUSDT_1h" in certificate["errors"]


def test_parameter_scope_rejects_lifecycle_fields_in_entry_grid():
    scope = validate_parameter_scope("entry", {
        "min_signal_strength": [0.4, 0.6],
        "trailing_activation_pct": [0.01, 0.02],
    })

    assert scope["valid"] is False
    assert scope["disallowed_parameters"] == ["trailing_activation_pct"]


def test_differentiation_reports_active_parameter():
    report = build_differentiation_report([
        {"params": {"min_signal_strength": 0.4}, "trades": 5, "pnl_usdt": 10.0},
        {"params": {"min_signal_strength": 0.6}, "trades": 2, "pnl_usdt": -1.0},
    ])

    assert report["active_parameters"] == 1
    assert report["parameter_sensitivity_ratio"] == 1.0
    assert report["passed"] is True


def test_aic_certification_produces_100_distinct_scenarios(tmp_path):
    results, recommended = run_aic_certification_experiment(DesignSpec(
        experiment_stage="aic",
        max_jobs=100,
        output_dir=str(tmp_path),
    ))

    assert recommended == {}
    assert results.summary["total_runs"] == 100
    assert len(results.results) == 100
    trigger = next(row for row in results.results if row["scenario"] == "trigger_fill_gap")
    assert trigger["real_exit_price"] != trigger["expected_exit_price"]
    assert results.summary["differentiation"]["unique_outputs"] > 10


def test_entry_cohort_loader_preserves_contract_lineage(tmp_path):
    path = tmp_path / "entry_results.jsonl"
    path.write_text(json.dumps({
        "window_id": "w1",
        "e2e_evidence": {"lineage": [{
            "symbol": "BTCUSDT",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "entry_price": 100.0,
            "quantity": 2.0,
            "selected_contract": {"side": "LONG"},
        }]},
    }) + "\n", encoding="utf-8")

    cohort = _load_entry_cohort(str(path))

    assert cohort[0]["window_id"] == "w1"
    assert cohort[0]["selected_contract"]["side"] == "LONG"


def test_aic_certification_consumes_real_entry_cohort(tmp_path):
    cohort_path = tmp_path / "cohort.jsonl"
    lineage = [{
        "symbol": f"S{index}USDT",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "entry_price": 100.0 + index,
        "quantity": 2.0,
        "selected_side": "LONG" if index % 2 == 0 else "SHORT",
    } for index in range(10)]
    cohort_path.write_text(json.dumps({
        "window_id": "w1",
        "e2e_evidence": {"lineage": lineage},
    }) + "\n", encoding="utf-8")

    results, _ = run_aic_certification_experiment(DesignSpec(
        experiment_stage="aic",
        max_jobs=100,
        output_dir=str(tmp_path / "aic"),
        entry_cohort_path=str(cohort_path),
    ))

    assert results.summary["cohort_source"] == "entry_results"
    assert results.summary["cohort_records"] == 10
    assert len(results.results) == 100


def test_lifecycle_certification_keeps_fixed_entry_and_blocks_partials():
    timestamps = pd.date_range("2026-01-01", periods=30, freq="h", tz="UTC")
    frame = pd.DataFrame({
        "timestamp": timestamps,
        "open": [100.0] * 30,
        "high": [101.0] * 30,
        "low": [99.0] * 30,
        "close": [100.0 + index * 0.2 for index in range(30)],
        "volume": [1000.0] * 30,
    })
    data = {"BTCUSDT": frame}

    first = _replay_policy(data, _base_policy("A", {"hard_tp_atr_mult": 2.0}), 0.0004)
    second = _replay_policy(data, _base_policy("A", {"hard_tp_atr_mult": 4.0}), 0.0004)

    assert first["entry_cohort_hash"] == second["entry_cohort_hash"]
    assert first["partial_exits"] == 0
    assert second["partial_exits"] == 0


def test_lifecycle_policy_uses_requested_regime():
    policy = _base_policy("C", {}, regime="PRESERVATION")

    assert policy.regime == "PRESERVATION"
    assert policy.profile == "C"


def test_lifecycle_window_end_is_right_censored_not_realized():
    timestamps = pd.date_range("2026-01-01", periods=30, freq="h", tz="UTC")
    frame = pd.DataFrame({
        "timestamp": timestamps,
        "open": [100.0] * 30,
        "high": [100.05] * 30,
        "low": [99.95] * 30,
        "close": [100.0] * 30,
        "volume": [1000.0] * 30,
    })

    result = _replay_policy(
        {"BTCUSDT": frame},
        _base_policy("A", {"hard_sl_atr_mult": 1000.0, "hard_tp_atr_mult": 1000.0}),
        0.0004,
    )

    assert result["trades"] == 0
    assert result["right_censored"] == 1
    assert result["exit_reasons"] == {"window_end": 1}


def test_lifecycle_certification_rejects_missing_real_cohort(tmp_path):
    windows_dir = tmp_path / "windows"
    windows_dir.mkdir()
    (windows_dir / "w1.json").write_text(json.dumps({
        "window_id": "w1",
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-02T00:00:00Z",
        "symbols": ["BTCUSDT"],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="non-empty real entry cohort"):
        run_lifecycle_certification(DesignSpec(
            experiment_stage="lifecycle",
            window_manifest_dir=str(windows_dir),
            output_dir=str(tmp_path / "output"),
        ), str(tmp_path))


def test_invariants_detect_leverage_and_partial_exit_violations():
    report = build_invariant_report([
        {"leverage": 3, "partial_exits": 1},
    ], "lifecycle")

    assert report["passed"] is False
    assert {item["invariant"] for item in report["violations"]} == {
        "leverage_max_2x", "no_partial_exits",
    }


def test_e2e_invariants_fail_when_no_lineage_evidence_is_observed():
    report = build_invariant_report([{"entry_funnel": {"candidates": 1}}], "e2e")

    assert report["passed"] is False
    assert report["missing_required_evidence"] == ["e2e_lineage"]


def test_e2e_metrics_include_nested_futures_pnl_and_equity_peak_capture():
    metrics = _derive_metrics([
        {"total_value": 1000.0, "result": {"buy_orders": [{"symbol": "BTCUSDT"}]}},
        {"total_value": 1100.0, "result": {"futures_closes": [{"result": {"pnl_usdt": 100.0}}]}},
        {"total_value": 1080.0, "result": {
            "buy_orders": [{"symbol": "ETHUSDT"}],
            "futures_closes": [{"result": {"pnl_usdt": -20.0}}],
        }},
    ], 1000.0)

    assert metrics["trades"] == 2
    assert metrics["profit_factor"] == 5.0
    assert metrics["win_rate"] == 0.5
    assert metrics["peak_capture_ratio"] == 0.8


def test_e2e_evidence_requires_contract_lineage_and_close():
    evidence = _derive_e2e_evidence([{
        "result": {
            "buy_orders": [{
                "symbol": "BTCUSDT",
                "side": "BUY",
                "entry_metadata": {
                    "expected_side": "LONG",
                    "trade_type": "spot",
                    "selected_contract": {"side": "LONG", "trade_type": "spot", "leverage": 1},
                },
            }],
            "sl_tp_closes": [{"symbol": "BTCUSDT", "pnl_usdt": 1.0}],
        },
    }])["e2e_evidence"]

    assert evidence["entries"] == 1
    assert evidence["closes"] == 1
    assert evidence["contracts"] == 1
    assert evidence["violations"] == []

    missing_close = _derive_e2e_evidence([{
        "result": {
            "buy_orders": [{
                "symbol": "BTCUSDT",
                "side": "BUY",
                "entry_metadata": {"selected_contract": {"side": "LONG", "trade_type": "spot", "leverage": 1}},
            }],
        },
    }])["e2e_evidence"]
    assert "entry_close_count_alignment" in missing_close["violations"]


def test_entry_funnel_exposes_parameter_effects_before_fills():
    funnel = _derive_entry_funnel([
        {"result": {"top_symbols": ["BTCUSDT", "ETHUSDT"], "buy_orders": []}},
        {"result": {"top_symbols": ["BTCUSDT"], "buy_orders": [{"symbol": "BTCUSDT"}]}},
    ])

    assert funnel["entry_funnel"]["candidates"] == 3
    assert funnel["entry_funnel"]["cycles_with_candidates"] == 2
    assert funnel["entry_funnel"]["buy_orders"] == 1


def test_simulation_spot_margin_short_uses_historical_price_and_two_x_max():
    now = pd.Timestamp("2026-01-01T00:00:00Z").to_pydatetime()
    client = SimulationSpotMarginClient(
        clock=SimulationClock(now),
        price_feed={"BTCUSDT": [{"open_time": now.timestamp(), "close": 100.0}]},
        initial_usdt=1000.0,
    )

    position = client.open_short("BTCUSDT", 100.0, sl_pct=1.0, tp_pct=2.0, leverage=10)

    assert position is not None
    assert position.leverage == 2
    assert position.quantity == 2.0
    assert position.stop_price == 101.0
    assert position.target_price == 98.0
    assert client.get_available_usdt() == 900.0


def test_futures_intrabar_stop_closes_at_stop_price():
    now = pd.Timestamp("2026-01-01T00:00:00Z").to_pydatetime()
    feed = {"BTCUSDT": [{
        "open_time": now.timestamp(), "open": 100.0, "high": 103.0,
        "low": 99.0, "close": 100.5,
    }]}
    client = SimulationFuturesClient(SimulationClock(now), feed)
    client._available_usdt = 1000.0
    position = client.open_short("BTCUSDT", 100.0, sl_pct=2.0, tp_pct=4.0, leverage=2)

    closes = client.check_positions(settings=None)

    assert position is not None
    assert position.closed is True
    assert closes[0]["reason"] == "SL"
    assert closes[0]["exit_price"] == position.stop_price
    assert closes[0]["pnl_usdt"] < 0


def test_futures_intrabar_stop_wins_when_stop_and_target_share_bar():
    now = pd.Timestamp("2026-01-01T00:00:00Z").to_pydatetime()
    feed = {"BTCUSDT": [{
        "open_time": now.timestamp(), "open": 100.0, "high": 103.0,
        "low": 95.0, "close": 100.0,
    }]}
    client = SimulationFuturesClient(SimulationClock(now), feed)
    client._available_usdt = 1000.0
    client.open_short("BTCUSDT", 100.0, sl_pct=2.0, tp_pct=4.0, leverage=2)

    closes = client.check_positions(settings=None)

    assert closes[0]["reason"] == "SL"
    assert closes[0]["exit_price"] == 102.0


def test_e2e_lifecycle_closes_futures_with_belief_exhausted_reason():
    now = pd.Timestamp("2026-01-01T12:00:00Z").to_pydatetime()
    bars = [{
        "open_time": now.timestamp() - offset * 3600,
        "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
    } for offset in reversed(range(16))]
    client = SimulationFuturesClient(SimulationClock(now), {"BTCUSDT": bars})
    client._available_usdt = 1000.0
    position = client.open_short("BTCUSDT", 100.0, sl_pct=10.0, tp_pct=10.0, leverage=2)
    assert position is not None
    position.opened_at = now.timestamp() - 3600

    runner = SimulationRunner.__new__(SimulationRunner)
    runner.clock = client.clock
    runner.price_feed = {"BTCUSDT": bars}
    runner.futures_client = client
    runner.lifecycle_exit_policy = _base_policy("A", {"time_stop_hours": 0.5})
    runner.lifecycle_time_stop_reason = "belief_exhausted"

    closes = runner.monitor_lifecycle_exits("FUTURES_SHORT")

    assert closes[0]["reason"] == "belief_exhausted"
    assert closes[0]["lifecycle_source"] == "e2e_exit_policy"
    assert position.closed is True


def test_futures_wallet_counts_margin_not_leveraged_notional():
    now = pd.Timestamp("2026-01-01T00:00:00Z").to_pydatetime()
    feed = {"BTCUSDT": [{"open_time": now.timestamp(), "close": 100.0}]}
    client = SimulationFuturesClient(SimulationClock(now), feed)
    client._available_usdt = 1000.0

    position = client.open_long("BTCUSDT", 100.0, sl_pct=1.0, tp_pct=2.0, leverage=2)

    assert position is not None
    assert client.get_available_usdt() == 900.0
    assert client.get_total_wallet_usdt() == 1000.0
    assert client.reevaluate_open_positions(None, "SPOT_BULL", "A") == 0
    assert position.closed is False
    assert client.get_total_wallet_usdt() == 1000.0


def test_historical_range_fetch_uses_explicit_window_and_pages_forward():
    class API:
        def __init__(self):
            self.calls = []

        def get_klines(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return [[kwargs["startTime"] + 1_000, "1", "2", "0.5", "1.5", "10"]] * 1000
            return [[kwargs["startTime"] + 1_000, "1", "2", "0.5", "1.5", "10"]]

    class Client:
        client = API()

    start = pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime()
    end = pd.Timestamp("2026-01-02", tz="UTC").to_pydatetime()
    rows = _fetch_klines_range(Client(), "BTCUSDT", "5m", start, end)

    assert len(rows) == 1001
    assert Client.client.calls[0]["startTime"] == int(start.timestamp() * 1000)
    assert Client.client.calls[0]["endTime"] == int(end.timestamp() * 1000)
    assert Client.client.calls[1]["startTime"] == int(start.timestamp() * 1000) + 1_001


def test_kelly_certification_runs_100_jobs_without_compressing_stops(tmp_path):
    results, recommended = run_kelly_certification(
        DesignSpec(experiment_stage="kelly", max_jobs=100, output_dir=str(tmp_path)),
        str(tmp_path),
    )

    assert recommended == {}
    assert results.summary["total_runs"] == 100
    assert results.summary["executable_contracts"] == 100
    assert results.summary["scaled_contracts"] > 0
    assert results.summary["differentiation"]["parameter_sensitivity_ratio"] == 1.0
    assert results.summary["invariants"]["passed"] is True
    assert all(row["leverage"] <= 2 for row in results.results)
    assert all(row["kelly_contract"]["stop_unchanged"] for row in results.results)
    assert all(row["economic_risk_usdt"] <= 18.0 + 1e-9 for row in results.results)
