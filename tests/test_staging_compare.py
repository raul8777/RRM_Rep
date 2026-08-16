from app import _build_staging_compare_summary, ensure_staging_runtime_ready, record_staging_decision_compare


def test_ensure_staging_runtime_ready_bootstraps_empty_staging_db(monkeypatch):
    calls = []

    class FakeDB:
        def execute_query(self, sql, params=()):
            calls.append(("query", sql, params))
            if "information_schema.tables" in sql:
                return []
            return []

        def execute_update(self, sql, params=()):
            calls.append(("update", sql, params))
            return 1

    monkeypatch.setattr('app.db_client', FakeDB())

    result = ensure_staging_runtime_ready()

    assert result['ok'] is True
    assert any('CREATE TABLE IF NOT EXISTS staging_decision_compare' in sql for _, sql, _ in calls)
    assert any("INSERT INTO trading_runtime_state" in sql for _, sql, _ in calls)


def test_record_staging_decision_compare_persists_audit_row(monkeypatch):
    captured = {}

    class FakeDB:
        def execute_update(self, sql, params):
            captured['sql'] = sql
            captured['params'] = params
            return 1

    monkeypatch.setattr('app.db_client', FakeDB())

    result = record_staging_decision_compare(
        decision_domain='candidate_selection',
        status='rollback_needed',
        prod_selected=True,
        staging_selected=False,
        delta_total=-3.25,
        reason_prod='score_limit',
        reason_staging='new_gate',
        decision_run_id='run-101',
        parent_trade_ref='trade-42',
        market_snapshot_id='snapshot-7',
    )

    assert result['ok'] is True
    assert captured['params'][0] == 'run-101'
    assert captured['params'][1] == 'trade-42'
    assert captured['params'][2] == 'snapshot-7'
    assert captured['params'][3] == 'candidate_selection'
    assert captured['params'][4] == 'rollback_needed'
    assert captured['params'][5] is True
    assert captured['params'][6] is False
    assert captured['params'][7] == -3.25


def test_staging_compare_summary_aggregates_diff_domains_and_rollback():
    rows = [
        {
            "decision_domain": "candidate_selection",
            "status": "active",
            "prod_selected": True,
            "staging_selected": False,
            "delta_total": 2.5,
            "reason_prod": "score_limit",
            "reason_staging": "new_gate",
        },
        {
            "decision_domain": "entry_policy",
            "status": "rollback_needed",
            "prod_selected": True,
            "staging_selected": True,
            "delta_total": -1.1,
            "reason_prod": "entry_gate",
            "reason_staging": "timing_delta",
        },
        {
            "decision_domain": "candidate_selection",
            "status": "stable",
            "prod_selected": False,
            "staging_selected": True,
            "delta_total": 0.7,
            "reason_prod": "filtered",
            "reason_staging": "selected_new_rule",
        },
    ]

    summary = _build_staging_compare_summary(rows)

    assert summary["total_diff_count"] == 3
    assert summary["total_delta"] == 2.1
    assert summary["signals"]["active"] == 1
    assert summary["signals"]["rollback_needed"] == 1
    assert summary["domains"]["candidate_selection"]["diffs"] == 2
    assert summary["domains"]["candidate_selection"]["delta_total"] == 3.2
    assert summary["domains"]["entry_policy"]["status"] == "rollback_needed"


def test_staging_compare_summary_handles_missing_values_gracefully():
    summary = _build_staging_compare_summary([])

    assert summary["total_diff_count"] == 0
    assert summary["total_delta"] == 0.0
    assert summary["domains"] == {}
    assert summary["signals"] == {}
