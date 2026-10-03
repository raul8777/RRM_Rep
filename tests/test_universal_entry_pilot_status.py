from datetime import datetime, timezone
from datetime import date, timedelta
import hashlib
import json

from Dev.scripts import universal_entry_pilot_status
from Dev.scripts.universal_entry_pilot_status import audit_historical_entry_rows


def test_historical_entry_audit_reproduces_1154_rows_and_161_symbol_minute_clusters():
    rows = []
    for cluster_index in range(161):
        repetitions = 8 if cluster_index < 27 else 7
        timestamp = 1_800_000_000 + cluster_index * 60
        for _ in range(repetitions):
            rows.append({
                "symbol": f"SYM{cluster_index}",
                "side": "SHORT",
                "cycle_ts": timestamp + 7,
                "entry_flags": {"generation": "baseline" if cluster_index % 2 else "v2_entry_fix"},
            })

    result = audit_historical_entry_rows(rows)

    assert result["short_rows"] == 1154
    assert result["unique_symbol_minute_clusters"] == 161
    assert result["rows_missing_identity"] == 0
    assert result["matches_known_1154_161_diagnostic"] is True
    assert result["generation_counts"] == {"baseline": 573, "v2_entry_fix": 581}


def test_historical_entry_audit_accepts_iso_timestamp_but_does_not_fabricate_missing_identity():
    timestamp = datetime(2026, 9, 20, 12, 0, 30, tzinfo=timezone.utc).isoformat()

    result = audit_historical_entry_rows([
        {"symbol": "BTCUSDT", "expected_side": "SHORT", "source_timestamp": timestamp},
        {"symbol": "ETHUSDT", "expected_side": "SHORT"},
    ])

    assert result["short_rows"] == 2
    assert result["unique_symbol_minute_clusters"] == 1
    assert result["rows_missing_identity"] == 1
    assert result["matches_known_1154_161_diagnostic"] is False


def test_historical_entry_audit_reads_selected_side_in_e2e_lineage():
    result = audit_historical_entry_rows([
        {"symbol": "BTCUSDT", "selected_side": "SHORT", "timestamp": "2026-09-20T12:00:30+00:00"},
        {"symbol": "ETHUSDT", "selected_side": "LONG", "timestamp": "2026-09-20T12:00:30+00:00"},
    ])

    assert result["short_rows"] == 1
    assert result["unique_symbol_minute_clusters"] == 1
    assert result["rows_missing_identity"] == 0


def test_historical_filter_status_reports_partial_spot_evidence_without_promoting_executability(tmp_path, monkeypatch):
    pilot_root = tmp_path / "universal_entry_selection"
    report_path = pilot_root / "historical_filter_snapshots" / "wayback_spot_production_2025.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps({
        "source_venue": "spot_production_only",
        "venue_executable_at_time": "unknown_for_pilot_windows",
        "window_coverage": {"development": "no_capture_in_window"},
        "captures": [{"venue": "spot_production", "archive_capture_timestamp": "20250426005818"}],
    }), encoding="utf-8")
    monkeypatch.setattr(universal_entry_pilot_status, "PILOT_ROOT", pilot_root)

    result = universal_entry_pilot_status._historical_filter_status()

    assert result["status"] == "partial_archived_spot_production_snapshots_outside_windows"
    assert result["capture_count"] == 1
    assert result["venue_executable_at_time"] == "unknown_for_pilot_windows"
    assert result["deployed_to_staging"] is False


def test_philosophy_generation_status_keeps_descriptive_audit_separate_from_scoring(tmp_path, monkeypatch):
    pilot_root = tmp_path / "universal_entry_selection"
    report_path = pilot_root / "pilot_development" / "philosophy_generation_audit.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps({
        "audit_passed": True,
        "scope": "development_opportunity_generation_only",
        "proposal_count": 2201,
        "regenerated_proposal_count": 2201,
        "hourly_coverage": {"covered_symbol_hours": 8736, "mature_decision_slots": 8060},
        "families": {"mean_reversion": {"opportunities": 1071}},
        "formula_scoring_allowed": False,
    }), encoding="utf-8")
    monkeypatch.setattr(universal_entry_pilot_status, "PILOT_ROOT", pilot_root)

    result = universal_entry_pilot_status._philosophy_generation_status()

    assert result["audit_passed"] is True
    assert result["covered_symbol_hours"] == 8736
    assert result["formula_scoring_allowed"] is False
    assert result["row_by_row_cluster_review"] == {}


def test_prospective_filter_status_flags_only_venue_with_stale_exchange_info_time(tmp_path, monkeypatch):
    pilot_root = tmp_path / "universal_entry_selection"
    snapshot_root = pilot_root / "prospective_filter_snapshots"
    snapshot_root.mkdir(parents=True)
    report_path = snapshot_root / "exchange_filters_test.json"
    report_path.write_text(json.dumps({
        "capture_type": "prospective_response_capture_not_historical",
        "captured_at": "2026-10-01T05:30:05+00:00",
        "read_only": True,
        "credentials_used": False,
        "orders_submitted": False,
        "symbols": [{"opportunity_symbol": "BTCUSDT"}],
        "source_server_time_checks": {
            "spot_demo": {"status": "fresh_within_tolerance"},
            "usd_m_futures_testnet": {"status": "inconsistent"},
            "usd_m_futures_demo": {"status": "stale_exchange_info_server_time_pilot_filters_stable"},
        },
        "sources": {
            "usd_m_futures_testnet": {
                "url": "https://testnet.binancefuture.com/fapi/v1/exchangeInfo",
                "repeated_samples": {
                    "exchange_info_body_hashes": ["body-a", "body-b"],
                    "catalog_hashes_excluding_server_time": ["catalog-a"],
                    "server_times": [100, 200],
                    "samples": [{"pilot_filter_fingerprints": {"usd_m_futures_testnet:BTCUSDT": "filters-a"}}],
                },
            },
            "usd_m_futures_demo_comparison_only": {
                "url": "https://demo-fapi.binance.com/fapi/v1/exchangeInfo",
                "repeated_samples": {
                    "exchange_info_body_hashes": ["body-a", "body-b"],
                    "catalog_hashes_excluding_server_time": ["catalog-a"],
                    "server_times": [100, 200],
                    "samples": [{"pilot_filter_fingerprints": {"usd_m_futures_demo:BTCUSDT": "filters-a"}}],
                },
            },
        },
        "venue_executable_at_capture": {
            "spot_demo": "unknown_account_state_not_queried",
            "usd_m_futures_testnet": "unknown_account_state_not_queried",
        },
    }), encoding="utf-8")
    monkeypatch.setattr(universal_entry_pilot_status, "PILOT_ROOT", pilot_root)

    result = universal_entry_pilot_status._prospective_sandbox_filter_status()

    assert result["server_time_checks"] == {
        "spot_demo": "fresh_within_tolerance",
        "usd_m_futures_testnet": "inconsistent",
    }
    assert result["filters_currentness_verified"] is False
    assert result["usd_m_futures_demo_comparison"] == {
        "host": "https://demo-fapi.binance.com/fapi/v1/exchangeInfo",
        "comparison_only": True,
        "same_exchange_info_body_hash_set": True,
        "same_catalog_hash_set_excluding_server_time": True,
        "same_exchange_info_server_time_set": True,
        "same_pilot_filter_fingerprints": True,
        "can_substitute_for_testnet": False,
    }


def test_bilateral_scaffold_status_keeps_geometry_outcomes_and_raw_audit_non_executable(tmp_path, monkeypatch):
    pilot_root = tmp_path / "universal_entry_selection"
    pilot = pilot_root / "pilot_development"
    pilot.mkdir(parents=True)
    artifacts = {
        "bilateral_contract_scaffold.jsonl": {
            "proposal_count": 4402,
            "unique_source_event_count": 2201,
            "bilateral_thesis_count": 4402,
            "contract_count": 17608,
            "counts_by_status": {"invalid_derived_geometry": 1678},
            "all_contracts_non_executable": True,
            "policy": {"stop_atr_multiple": 1.5},
        },
        "bilateral_scaffold_outcomes.jsonl": {
            "rows_written": 4925,
            "outcome_counts": {"TP": 824, "SL": 1109},
            "geometry_status_counts": {"derived_point_in_time_geometry": 4925},
            "skipped_modes": {"SPOT_MARGIN_SHORT": 4402},
            "label_is_executable": False,
            "horizon_minutes": 240,
        },
    }
    reports = {
        "bilateral_contract_scaffold.jsonl": "output_sha256",
        "bilateral_scaffold_outcomes.jsonl": "output_sha256",
    }
    for filename, payload in artifacts.items():
        path = pilot / filename
        path.write_text(json.dumps(payload), encoding="utf-8")
        report_path = path.with_suffix(".report.json")
        report_path.write_text(json.dumps({**payload, reports[filename]: hashlib.sha256(path.read_bytes()).hexdigest()}), encoding="utf-8")
    raw_report = pilot / "bilateral_raw_calibration_audit_bilateral_scaffold.json"
    raw_report.write_text(json.dumps({
        "joined_rows": 4925,
        "train_rows": 3439,
        "validation_rows": 1486,
        "train_independent_clusters": 1000,
        "validation_independent_clusters": 430,
        "clusters_split_across_folds": 0,
        "train_brier_raw": 0.3671,
        "validation_brier_raw": 0.3923,
        "include_opposite_theses": True,
        "all_venues": True,
        "usable_for_formula_scoring": False,
    }), encoding="utf-8")
    monkeypatch.setattr(universal_entry_pilot_status, "PILOT_ROOT", pilot_root)

    scaffold = universal_entry_pilot_status._bilateral_contract_scaffold_status()
    outcomes = universal_entry_pilot_status._bilateral_scaffold_outcome_status()
    raw_audit = universal_entry_pilot_status._bilateral_raw_calibration_status()

    assert scaffold["all_contracts_non_executable"] is True
    assert scaffold["report_hash_matches"] is True
    assert outcomes["executable_outcomes"] is False
    assert outcomes["skipped_modes"] == {"SPOT_MARGIN_SHORT": 4402}
    assert outcomes["report_hash_matches"] is True
    assert raw_audit["clusters_split_across_folds"] == 0
    assert raw_audit["usable_for_formula_scoring"] is False


def test_bilateral_status_detects_changed_parquet_and_unbound_outcomes(tmp_path, monkeypatch):
    pilot_root = tmp_path / "universal_entry_selection"
    pilot = pilot_root / "pilot_development"
    market = pilot_root / "normalized" / "pilot_development" / "spot"
    pilot.mkdir(parents=True)
    market.mkdir(parents=True)
    manifest = market / "BTCUSDT_1m.manifest.json"
    manifest.write_text('{"symbol":"BTCUSDT"}', encoding="utf-8")
    parquet = market / "BTCUSDT_1m.parquet"
    parquet.write_bytes(b"original market")
    scaffold = pilot / "bilateral_contract_scaffold.jsonl"
    scaffold.write_text("{}\n", encoding="utf-8")
    hashes = {"spot:BTCUSDT:1m": hashlib.sha256(parquet.read_bytes()).hexdigest()}
    scaffold.with_suffix(".report.json").write_text(json.dumps({
        "output_sha256": hashlib.sha256(scaffold.read_bytes()).hexdigest(),
        "source_market_manifest_hashes": {"spot:BTCUSDT": hashlib.sha256(manifest.read_bytes()).hexdigest()},
        "source_market_parquet_hashes": hashes,
        "hourly_source_modes": {"spot:BTCUSDT": "resampled_from_1m"},
    }), encoding="utf-8")
    outcome = pilot / "bilateral_scaffold_outcomes.jsonl"
    outcome.write_text("{}\n", encoding="utf-8")
    outcome.with_suffix(".report.json").write_text(json.dumps({
        "output_sha256": hashlib.sha256(outcome.read_bytes()).hexdigest(),
        "input_sha256": "wrong-scaffold-hash",
        "scaffold_source_market_parquet_hashes": hashes,
        "label_is_executable": False,
    }), encoding="utf-8")
    monkeypatch.setattr(universal_entry_pilot_status, "PILOT_ROOT", pilot_root)

    assert universal_entry_pilot_status._bilateral_contract_scaffold_status()["source_integrity_verified"] is True
    assert universal_entry_pilot_status._bilateral_scaffold_outcome_status()["source_binding_matches"] is False
    hourly = market / "BTCUSDT_1h.parquet"
    hourly.write_bytes(b"new hourly source")
    assert "hourly_source_changed:spot:BTCUSDT" in universal_entry_pilot_status._bilateral_contract_scaffold_status()["source_integrity_errors"]
    hourly.unlink()
    parquet.write_bytes(b"modified market")
    status = universal_entry_pilot_status._bilateral_contract_scaffold_status()
    assert status["source_integrity_verified"] is False
    assert status["source_integrity_errors"] == ["parquet_changed:spot:BTCUSDT:1m"]


def test_historical_funding_status_rechecks_public_evidence_hashes(tmp_path, monkeypatch):
    pilot_root = tmp_path / "Dev/benchmarks/universal_entry_selection"
    pilot = pilot_root / "pilot_development"
    pilot.mkdir(parents=True)
    local = tmp_path / "historical_data/external/funding_rate_20250109_20260630.parquet"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"verified local funding")
    raw = pilot / "public_production_funding_1000PEPEUSDT_20250205_20250220.json"
    raw.write_bytes(b"verified PEPE funding")
    (pilot / "public_production_funding_coverage.json").write_text(json.dumps({
        "source_venue": "usd_m_public_production_only",
        "local_source_sha256": hashlib.sha256(local.read_bytes()).hexdigest(),
        "recovered_raw_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
        "local_pilot_contracts": 25,
        "recovered_symbol": "1000PEPEUSDT",
        "recovered_rows": 45,
    }), encoding="utf-8")
    monkeypatch.setattr(universal_entry_pilot_status, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(universal_entry_pilot_status, "PILOT_ROOT", pilot_root)

    status = universal_entry_pilot_status._historical_funding_status()
    assert status["public_production_contracts_with_rows"] == 26
    assert status["source_hashes_match"] is True
    assert status["testnet_historical_funding_verified"] is False
    assert status["formula_scoring_allowed"] is False
    raw.write_bytes(b"altered PEPE funding")
    status = universal_entry_pilot_status._historical_funding_status()
    assert status["source_hashes_match"] is False
    assert status["public_production_contracts_with_rows"] == 0


def test_feature_audit_status_rechecks_all_development_archives(tmp_path, monkeypatch):
    pilot_root = tmp_path / "Dev/benchmarks/universal_entry_selection"
    output = pilot_root / "pilot_development"
    output.mkdir(parents=True)
    rows = []
    for index in range(26):
        symbol = f"SYM{index}USDT"
        directory = pilot_root / "raw/binance_archives/um/metrics" / symbol
        directory.mkdir(parents=True)
        for offset in range(15):
            day = (date(2025, 2, 5) + timedelta(days=offset)).isoformat()
            path = directory / f"{symbol}-metrics-{day}.zip"
            path.write_bytes(b"archive bytes")
            rows.append({"contract_symbol": symbol, "date": day, "status": "already_verified", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    (output / "feature_source_audit_metrics.json").write_text(json.dumps({
        "dataset": "metrics", "contract_count": 26, "expected_archive_count": 390,
        "result_counts": {"already_verified": 390}, "all_full_days_verified": True,
        "results": rows,
    }), encoding="utf-8")
    monkeypatch.setattr(universal_entry_pilot_status, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(universal_entry_pilot_status, "PILOT_ROOT", pilot_root)

    assert universal_entry_pilot_status._feature_source_audit_status("metrics")["full_coverage_verified"] is True
    (pilot_root / "raw/binance_archives/um/metrics/SYM0USDT/SYM0USDT-metrics-2025-02-05.zip").write_bytes(b"changed bytes")
    status = universal_entry_pilot_status._feature_source_audit_status("metrics")
    assert status["full_coverage_verified"] is False
    assert status["source_errors"] == ["archive_changed:SYM0USDT:2025-02-05"]


def test_historical_account_is_not_required_for_simulation_but_live_preflight_still_blocks(monkeypatch):
    monkeypatch.setattr(universal_entry_pilot_status, "_prospective_sandbox_filter_status", lambda: {
        "filters_currentness_verified": True,
        "server_time_checks": {"spot_demo": "fresh_within_tolerance", "usd_m_futures_testnet": "fresh_within_tolerance"},
    })

    status = universal_entry_pilot_status.build_status()

    assert status["evidence_scopes"]["historical_market_replay"]["account_state_2025_required"] is False
    assert status["evidence_scopes"]["historical_market_replay"]["labels_are_exchange_fills"] is False
    live = status["evidence_scopes"]["active_staging_canary"]
    assert live["current_filters_verified"] is True
    assert live["current_independent_wallets_required"] is True
    assert live["wallet_and_position_preflight_verified"] is False
    assert live["orders_allowed_by_pilot"] is False
    assert status["pilot_complete"] is False


def test_fast_formula_screen_status_reports_simulated_review_without_winner(monkeypatch, tmp_path):
    repo_root = tmp_path
    pilot_root = repo_root / "Dev/benchmarks/universal_entry_selection"
    screen_root = pilot_root / "comparison_runs/run-1/formula_screen_v2"
    screen_root.mkdir(parents=True)
    report_path = screen_root / "validation_formula_comparison.report.json"
    report_path.write_text(json.dumps({
        "role": "validation",
        "winner_claim_allowed": False,
        "metrics_are_simulated_non_executable": True,
    }), encoding="utf-8")
    summary_path = screen_root / "summary.json"
    summary_path.write_text(json.dumps({
        "state": "simulation_screen_complete_review_required",
        "family_results": {"validation": str(report_path.relative_to(repo_root)).replace("\\", "/")},
        "families_in_tape": ["mean_reversion_range"],
        "pending_canonical_families": ["microstructure_order_flow"],
        "formula_arms": ["baseline_current_universal"],
        "roles": ["validation"],
        "metrics_are_simulated_non_executable": True,
        "winner_claim_allowed": False,
        "staging_touched": False,
        "orders_submitted": False,
    }), encoding="utf-8")
    (screen_root / "status.json").write_text(json.dumps({
        "run_id": "run-1-formula-screen-v2",
        "state": "formula_screen_complete_no_winner_claim",
        "phase": "same_tape_formula_scoring",
        "counts": {"completed": 1, "pending": 0, "failed": 0},
        "summary_path": str(summary_path.relative_to(repo_root)).replace("\\", "/"),
    }), encoding="utf-8")
    monkeypatch.setattr(universal_entry_pilot_status, "REPO_ROOT", repo_root)
    monkeypatch.setattr(universal_entry_pilot_status, "PILOT_ROOT", pilot_root)

    result = universal_entry_pilot_status._fast_formula_screen_status()

    assert result["screen_complete"] is True
    assert result["metrics_are_simulated_non_executable"] is True
    assert result["winner_claim_allowed"] is False
    assert result["staging_touched"] is False
    assert result["pending_canonical_families"] == ["microstructure_order_flow"]
    assert result["common_runtime_hard_gates_applied"] is False
    assert result["predictor_weight_cutoff_verified"] is False
    assert result["baseline_matches_exact_runtime"] is False
    assert result["multiplicity_adjusted"] is False
    assert len(result["decision_blocking_gaps"]) >= 4


def test_current_testnet_filters_use_time_and_http_date_not_ignored_exchange_info_time():
    now = datetime(2026, 10, 1, 16, 0, 5, tzinfo=timezone.utc)
    payload = {
        "snapshot_version": "binance_pilot_exchange_filters_v4",
        "captured_at": "2026-10-01T16:00:03+00:00",
        "read_only": True, "credentials_used": False, "orders_submitted": False,
        "source_server_time_checks": {"usd_m_futures_testnet": {
            "exchange_info_server_time": 1790170223940,
            "time_endpoint_server_time": int(datetime(2026, 10, 1, 16, 0, 2, tzinfo=timezone.utc).timestamp() * 1000),
            "exchange_info_http_headers": {"Date": "Thu, 01 Oct 2026 16:00:02 GMT"},
            "time_endpoint_http_headers": {"Date": "Thu, 01 Oct 2026 16:00:02 GMT"},
        }},
        "sources": {"usd_m_futures_testnet": {
            "url": "https://testnet.binancefuture.com/fapi/v1/exchangeInfo",
            "repeated_samples": {
                "sample_count": 3, "samples": [{}, {}, {}],
                "catalog_hashes_excluding_server_time": ["catalog"],
                "pilot_filter_fingerprint_count": 26, "pilot_filters_stable": True,
            },
        }},
        "symbols": [{"usd_m_futures_testnet": {"status": "TRADING", "exchange_filters_validated": True}} for _ in range(26)],
    }

    evidence = universal_entry_pilot_status._current_testnet_filter_evidence(payload, now)
    assert evidence["verified"] is True
    assert evidence["clock_source"] == "/fapi/v1/time_and_http_date_not_exchange_info_server_time"
    payload["source_server_time_checks"]["usd_m_futures_testnet"]["time_endpoint_http_headers"]["Date"] = "Thu, 01 Oct 2026 15:55:02 GMT"
    assert "http_date_and_time_disagree" in universal_entry_pilot_status._current_testnet_filter_evidence(payload, now)["blocking_reasons"]
    payload["source_server_time_checks"]["usd_m_futures_testnet"]["time_endpoint_http_headers"]["Date"] = "Thu, 01 Oct 2026 16:00:02 GMT"
    assert "capture_not_recent" in universal_entry_pilot_status._current_testnet_filter_evidence(payload, now.replace(hour=17))["blocking_reasons"]


def test_current_commission_and_cost_sensitivity_status_recheck_source_hashes(tmp_path, monkeypatch):
    pilot = tmp_path / "Dev/benchmarks/universal_entry_selection/pilot_development"
    pilot.mkdir(parents=True)
    fee = pilot / "sandbox_commissions_test.json"
    venue = {"expected_symbols": 26, "verified_symbols": 26, "records": []}
    fee.write_text(json.dumps({
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "capture_type": "current_account_specific_fees_not_historical",
        "read_only": True, "orders_submitted": False,
        "venues": {"spot_demo": venue, "usd_m_futures_testnet": venue},
    }), encoding="utf-8")
    outcomes = pilot / "bilateral_scaffold_outcomes.jsonl"
    outcomes.write_text("outcomes", encoding="utf-8")
    rows = pilot / "bilateral_cost_sensitivity_test.jsonl"
    rows.write_text("scenarios", encoding="utf-8")
    report = pilot / "bilateral_cost_sensitivity_test.report.json"
    report.write_text(json.dumps({
        "rows_path": str(rows), "rows_sha256": hashlib.sha256(rows.read_bytes()).hexdigest(),
        "outcomes_sha256": hashlib.sha256(outcomes.read_bytes()).hexdigest(),
        "fee_snapshot_path": str(fee), "fee_snapshot_sha256": hashlib.sha256(fee.read_bytes()).hexdigest(),
        "rows_written": 19700, "summaries": [{} for _ in range(12)],
        "funding_provenance": {"source": "public_usd_m_production_market_funding_not_testnet"},
        "is_executable_replay": False, "formula_scoring_allowed": False,
    }), encoding="utf-8")
    monkeypatch.setattr(universal_entry_pilot_status, "PILOT_ROOT", tmp_path / "Dev/benchmarks/universal_entry_selection")
    monkeypatch.setattr(universal_entry_pilot_status, "REPO_ROOT", tmp_path)

    fees_status = universal_entry_pilot_status._sandbox_commission_status()
    cost_status = universal_entry_pilot_status._cost_sensitivity_status()
    assert fees_status["capture_complete"] is True
    assert fees_status["current_account_commissions_verified"] is True
    assert fees_status["historical_fees_verified"] is False
    assert cost_status["status"] == "verified_non_executable_cost_sensitivity"
    assert cost_status["is_executable_replay"] is False
    assert cost_status["formula_scoring_allowed"] is False

    rows.write_text("tampered", encoding="utf-8")
    assert universal_entry_pilot_status._cost_sensitivity_status()["source_hashes_match"] is False


def test_latest_staging_preflight_keeps_runtime_and_offline_gates_separate(tmp_path, monkeypatch):
    pilot_root = tmp_path / "Dev/benchmarks/universal_entry_selection"
    pilot = pilot_root / "pilot_development"
    pilot.mkdir(parents=True)
    path = pilot / "staging_preflight_test.json"
    checks = {
        "environment_is_staging": True, "testnet_routing_enabled": True,
        "active_sandbox_execution_mode": True, "new_entries_not_paused": True,
        "spot_account_read_ok": True, "futures_account_read_ok": True,
        "spot_funded_for_100usdt_cap": True, "futures_funded_for_100usdt_cap": True,
        "no_spot_positions_open": True, "no_futures_positions_open": True,
        "current_filters_verified": True, "current_commissions_verified": True,
        "offline_gates_ready": False,
    }
    path.write_text(json.dumps({
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "runtime_preflight_passed": True, "offline_gates_ready": False,
        "canary_eligible": False, "orders_allowed": False, "checks": checks,
        "spot": {"open_position_count": 0}, "futures": {"open_position_count": 0},
    }), encoding="utf-8")
    monkeypatch.setattr(universal_entry_pilot_status, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(universal_entry_pilot_status, "PILOT_ROOT", pilot_root)

    status = universal_entry_pilot_status._staging_preflight_status()
    assert status["runtime_preflight_passed"] is True
    assert status["offline_gates_ready"] is False
    assert status["canary_eligible"] is False
    assert status["orders_allowed"] is False
    checks["no_spot_positions_open"] = False
    path.write_text(json.dumps({
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "runtime_preflight_passed": False, "offline_gates_ready": False,
        "canary_eligible": False, "orders_allowed": False, "checks": checks,
    }), encoding="utf-8")
    assert universal_entry_pilot_status._staging_preflight_status()["runtime_preflight_passed"] is False


def test_replay_calibration_status_is_hash_bound_and_never_operational(tmp_path, monkeypatch):
    pilot_root = tmp_path / "Dev/benchmarks/universal_entry_selection"
    pilot = pilot_root / "pilot_development"
    pilot.mkdir(parents=True)
    predictions = pilot / "bilateral_raw_predictions.jsonl"
    outcomes = pilot / "bilateral_scaffold_outcomes.jsonl"
    scaffold = pilot / "bilateral_contract_scaffold.jsonl"
    prediction_output = pilot / "calibrated.jsonl"
    for path, content in ((predictions, b"predictions"), (outcomes, b"outcomes"), (scaffold, b"scaffold"), (prediction_output, b"calibrated")):
        path.write_bytes(content)
    manifest_hashes = {"spot:BTCUSDT": "manifest"}
    parquet_hashes = {"spot:BTCUSDT:1m": "parquet"}
    scaffold.with_suffix(".report.json").write_text(json.dumps({
        "source_market_manifest_hashes": manifest_hashes,
        "source_market_parquet_hashes": parquet_hashes,
    }), encoding="utf-8")
    outcomes.with_suffix(".report.json").write_text(json.dumps({
        "output_sha256": hashlib.sha256(outcomes.read_bytes()).hexdigest(),
        "scaffold_source_market_manifest_hashes": manifest_hashes,
        "scaffold_source_market_parquet_hashes": parquet_hashes,
    }), encoding="utf-8")
    report = pilot / "bilateral_replay_calibration_test.report.json"
    report.write_text(json.dumps({
        "predictions_input_sha256": hashlib.sha256(predictions.read_bytes()).hexdigest(),
        "outcomes_input_sha256": hashlib.sha256(outcomes.read_bytes()).hexdigest(),
        "outcomes_source_market_manifest_hashes": manifest_hashes,
        "outcomes_source_market_parquet_hashes": parquet_hashes,
        "output_predictions_path": str(prediction_output),
        "output_predictions_sha256": hashlib.sha256(prediction_output.read_bytes()).hexdigest(),
        "joined_rows": 300, "train_independent_clusters": 70, "validation_independent_clusters": 30,
        "clusters_crossing_folds": 0,
        "models_by_venue": {"SPOT_LONG": {"raw_validation_brier": 0.3, "calibrated_validation_brier": 0.2,
            "raw_validation_log_loss": 0.8, "calibrated_validation_log_loss": 0.6,
            "validation_independent_clusters": 30, "converged": True}},
        "simulated_replay_validation_passed": True,
        "usable_for_formula_scoring": False, "usable_for_live_execution": False,
    }), encoding="utf-8")
    monkeypatch.setattr(universal_entry_pilot_status, "PILOT_ROOT", pilot_root)
    monkeypatch.setattr(universal_entry_pilot_status, "REPO_ROOT", tmp_path)

    status = universal_entry_pilot_status._replay_calibration_status()
    assert status["simulated_replay_calibration_valid"] is True
    assert status["usable_for_formula_scoring"] is False
    assert status["usable_for_live_execution"] is False
    predictions.write_bytes(b"changed predictions")
    assert universal_entry_pilot_status._replay_calibration_status()["simulated_replay_calibration_valid"] is False


def test_approved_prospective_scope_does_not_replace_history_or_authorize_orders():
    live_ready = universal_entry_pilot_status._prospective_pilot_scope_status(
        {"available": True, "fresh_within_300_seconds": True, "runtime_preflight_passed": True,
         "checks": {"pilot_order_and_position_limits_enforced": True},
         "spot": {"open_position_count": 0}, "futures": {"open_position_count": 0}},
        {"filters_currentness_verified": True},
        {"current_account_commissions_verified": True},
        {"manifest_valid": True, "source_integrity_verified": True, "pilot_limits_verified": True,
         "cross_venue_single_position_guard_verified": True},
        {"lineage": True, "parity": True},
    )
    assert live_ready["approved"] is True
    assert live_ready["canary_ready_now"] is True
    assert live_ready["uses_original_1154_161_cohort"] is False
    assert live_ready["historical_benchmark_replaced"] is False
    assert live_ready["formula_comparison_in_scope"] is False
    assert live_ready["profitability_claim_allowed"] is False
    assert live_ready["orders_allowed"] is False

    blocked = universal_entry_pilot_status._prospective_pilot_scope_status(
        {"available": True, "fresh_within_300_seconds": True, "runtime_preflight_passed": False,
         "checks": {"pilot_order_and_position_limits_enforced": True},
         "spot": {"open_position_count": 0}, "futures": {"open_position_count": 0}},
        {"filters_currentness_verified": True},
        {"current_account_commissions_verified": True},
        {"manifest_valid": True, "source_integrity_verified": True, "pilot_limits_verified": True,
         "cross_venue_single_position_guard_verified": True},
        {"lineage": True, "parity": True},
    )
    assert blocked["canary_ready_now"] is False

    stale_baseline = universal_entry_pilot_status._prospective_pilot_scope_status(
        {"available": True, "fresh_within_300_seconds": True, "runtime_preflight_passed": True,
         "checks": {"pilot_order_and_position_limits_enforced": True},
         "spot": {"open_position_count": 0}, "futures": {"open_position_count": 0}},
        {"filters_currentness_verified": True},
        {"current_account_commissions_verified": True},
        {"manifest_valid": False, "source_integrity_verified": False, "pilot_limits_verified": True,
         "cross_venue_single_position_guard_verified": True},
        {"lineage": True, "parity": True},
    )
    assert stale_baseline["canary_ready_now"] is False

    occupied = universal_entry_pilot_status._prospective_pilot_scope_status(
        {"available": True, "fresh_within_300_seconds": True, "runtime_preflight_passed": True,
         "checks": {"pilot_order_and_position_limits_enforced": True},
         "spot": {"open_position_count": 1}, "futures": {"open_position_count": 0}},
        {"filters_currentness_verified": True},
        {"current_account_commissions_verified": True},
        {"manifest_valid": True, "source_integrity_verified": True, "pilot_limits_verified": True,
         "cross_venue_single_position_guard_verified": True},
        {"lineage": True, "parity": True},
    )
    assert occupied["canary_ready_now"] is False

    missing_global_guard = universal_entry_pilot_status._prospective_pilot_scope_status(
        {"available": True, "fresh_within_300_seconds": True, "runtime_preflight_passed": True,
         "checks": {"pilot_order_and_position_limits_enforced": True},
         "spot": {"open_position_count": 0}, "futures": {"open_position_count": 0}},
        {"filters_currentness_verified": True},
        {"current_account_commissions_verified": True},
        {"manifest_valid": True, "source_integrity_verified": True, "pilot_limits_verified": True,
         "cross_venue_single_position_guard_verified": False},
        {"lineage": True, "parity": True},
    )
    assert missing_global_guard["canary_ready_now"] is False