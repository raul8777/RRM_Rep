import pytest

from pnl_accounting import mark_to_market_spot, normalize_spot_fills
from position_manager import Position, PositionManager


class Settings:
    binance_commission_rate = 0.001
    futures_commission_rate = 0.0004
    bnb_spot_commission_rate = 0.0005
    bnb_futures_commission_rate = 0.0002
    use_bnb_for_commission = False
    backtest_slippage_pct = 0.0005


def test_normalize_spot_fills_values_bnb_commission():
    payload = normalize_spot_fills([
        {"qty": "2", "price": "100", "commission": "0.001", "commissionAsset": "BNB"},
        {"qty": "1", "price": "101", "commission": "0.1", "commissionAsset": "USDT"},
    ], "BTCUSDT", price_lookup=lambda symbol: 600.0)

    assert payload["qty"] == pytest.approx(3.0)
    assert payload["quote_qty"] == pytest.approx(301.0)
    assert payload["avg_price"] == pytest.approx(301.0 / 3.0)
    assert payload["commission_usdt"] == pytest.approx(0.7)
    assert payload["commissions_by_asset"] == {"BNB": 0.001, "USDT": 0.1}


def test_mark_to_market_uses_actual_entry_commission():
    position = Position("BTCUSDT", "BUY", 1.0, 100.0, "2026-01-01T00:00:00Z", entry_commission=0.25)
    accounting = mark_to_market_spot(position, 110.0, Settings())

    assert accounting["gross_pnl_usdt"] == pytest.approx(10.0)
    assert accounting["entry_commission_usdt"] == pytest.approx(0.25)
    assert accounting["exit_commission_estimated_usdt"] == pytest.approx(0.11)
    assert accounting["net_pnl_usdt"] == pytest.approx(9.64)


def test_verified_exit_fill_controls_persisted_pnl():
    pm = PositionManager()
    pm.add_position(Position("BTCUSDT", "BUY", 1.0, 100.0, "2026-01-01T00:00:00Z", entry_commission=0.2))
    fill = {
        "binance_confirmed": True,
        "order_id": "exit-1",
        "qty": 1.0,
        "quote_qty": 110.0,
        "avg_price": 110.0,
        "commission_usdt": 0.3,
        "commissions_by_asset": {"BNB": 0.0005},
    }

    closed = pm.close_position("BTCUSDT", 109.0, "take_profit", exit_fill=fill)

    assert closed is not None
    assert closed.exit_price == pytest.approx(110.0)
    assert closed.exit_commission == pytest.approx(0.3)
    assert closed.pnl == pytest.approx(9.5)
    assert closed.metadata["pnl_accounting_mode"] == "binance_fill_verified"
    assert closed.metadata["exit_fill"]["order_id"] == "exit-1"
