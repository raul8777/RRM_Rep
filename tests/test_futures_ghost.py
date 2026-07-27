"""Tests for futures ghost-prevention verification logic."""
import sys
sys.path.insert(0, sys.path[0] + "/..")

from unittest.mock import MagicMock
from futures_client import FuturesClient


def test_verify_entry_filled_false_when_no_position():
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=True)
    # In dry_run, verification returns True
    assert fc._verify_entry_filled("BTCUSDT", "LONG") is True


def test_verify_position_closed_false_when_position_exists():
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    fc._get = MagicMock(return_value=[{"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "0.01"}])
    assert fc._verify_position_closed("BTCUSDT", "LONG") is False
    assert fc._get_position_qty_real("BTCUSDT", "LONG") == 0.01


def test_verify_position_closed_true_when_position_zero():
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    fc._get = MagicMock(return_value=[{"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "0.0"}])
    assert fc._verify_position_closed("BTCUSDT", "LONG") is True


def test_entry_filled_true_when_position_exists():
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    fc._get = MagicMock(return_value=[{"symbol": "BTCUSDT", "positionSide": "SHORT", "positionAmt": "-0.05"}])
    assert fc._verify_entry_filled("BTCUSDT", "SHORT") is True


def test_entry_filled_false_when_api_error():
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    fc._get = MagicMock(side_effect=Exception("API down"))
    # Should NOT assume filled on API error
    assert fc._verify_entry_filled("BTCUSDT", "LONG") is False


def test_external_close_reconciliation_aggregates_partial_fills():
    fc = FuturesClient(api_key="k", api_secret="s", dry_run=False)
    closing_fills = [
        {"time": 2000, "positionSide": "SHORT", "qty": "4", "price": "12", "realizedPnl": "-4", "commission": "0.1"},
        {"time": 2000, "positionSide": "SHORT", "qty": "6", "price": "12", "realizedPnl": "-6", "commission": "0.2"},
        {"time": 1000, "positionSide": "SHORT", "qty": "10", "price": "10", "realizedPnl": "0", "commission": "0.3"},
    ]
    fc._get = MagicMock(side_effect=[closing_fills, []])

    result = fc._get_realized_pnl_from_trades("BTCUSDT", "SHORT")

    assert result["exit_price"] == 12.0
    assert result["realized_pnl"] == -10.0
    assert result["commission"] == 0.6
    assert result["pnl"] == -10.6
    assert result["reason"] == "stop_loss_binance"
