from dashboard_truth import (
    verify_futures_dashboard_row,
    verify_open_dashboard_row,
    summarize_closed_position_verification,
)


def test_verify_futures_dashboard_row_detects_mismatch():
    row = {
        "trade_type": "futures",
        "quantity": 0.39,
        "unrealized_pnl": 0.02,
        "binance_quantity": 0.39,
        "binance_unrealized_pnl": 0.02,
        "db_entries_total_qty": 1579.14,
        "db_entries_total_unrealized_pnl": 53.13,
    }
    out = verify_futures_dashboard_row(row)
    assert out["truth_source"] == "binance_live"
    assert out["verification_status"] == "warning_mismatch"
    assert out["verification_mismatch"] is True
    assert out["operable_quantity"] == 0.39
    assert out["operable_unrealized_pnl"] == 0.02


def test_verify_futures_dashboard_row_verified_when_close():
    row = {
        "trade_type": "futures",
        "quantity": 2.0,
        "unrealized_pnl": -1.25,
        "binance_quantity": 2.0,
        "binance_unrealized_pnl": -1.25,
        "db_entries_total_qty": 2.0,
        "db_entries_total_unrealized_pnl": -1.3,
    }
    out = verify_futures_dashboard_row(row, pnl_abs_tol=0.1)
    assert out["verification_status"] == "verified"
    assert out["verification_mismatch"] is False


def test_verify_open_dashboard_row_spot_unverified_without_live_qty():
    row = {
        "trade_type": "spot",
        "quantity": 125.0,
        "entry_regime_mode": "SPOT_BULL",
    }
    out = verify_open_dashboard_row(row)
    assert out["verification_trade_type"] == "spot"
    assert out["verification_mode"] == "SPOT_BULL"
    assert out["verification_status"] == "estimated_or_unverified"
    assert out["verification_mismatch"] is False


def test_verify_open_dashboard_row_futures_includes_mode_scope():
    row = {
        "trade_type": "futures",
        "quantity": 1.0,
        "unrealized_pnl": 0.1,
        "binance_quantity": 1.0,
        "binance_unrealized_pnl": 0.1,
        "db_entries_total_qty": 1.0,
        "db_entries_total_unrealized_pnl": 0.1,
        "current_regime_mode": "FUTURES_SHORT",
    }
    out = verify_open_dashboard_row(row)
    assert out["verification_trade_type"] == "futures"
    assert out["verification_mode"] == "FUTURES_SHORT"
    assert out["truth_scope"] == "open_position"
    assert out["verification_status"] == "verified"


def test_summarize_closed_position_verification_exchange_confirmed():
    meta = {
        "pnl_accounting_mode": "binance_fill_verified",
        "close_contract": {"exchange_confirmed": True, "close_source": "lifecycle"},
    }
    out = summarize_closed_position_verification(meta, "take_profit")
    assert out["exchange_verified"] is True
    assert out["close_verification_status"] == "exchange_confirmed"


def test_summarize_closed_position_verification_reconciled():
    meta = {
        "close_contract": {"exchange_confirmed": False, "close_source": "reconciliation", "synthetic": True},
    }
    out = summarize_closed_position_verification(meta, "reconcile_missing_balance")
    assert out["exchange_verified"] is False
    assert out["close_verification_status"] == "reconciled"
