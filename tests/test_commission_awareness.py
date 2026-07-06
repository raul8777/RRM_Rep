"""Regression tests for commission-aware calculations across the system."""
import pytest
from dataclasses import dataclass


class FakeSettings:
    """Minimal settings stand-in for commission config."""
    def __init__(self, spot_rate=0.001, futures_rate=0.0004, bnb=False):
        self.spot_commission_rate = spot_rate
        self.futures_commission_rate = futures_rate
        self.use_bnb_for_commission = bnb


from binance_commission import get_config as _comm_cfg


def _rate(tt: str) -> float:
    return _comm_cfg().rate_for(tt)


class TestBinanceCommissionHelpers:
    """Verify centralized commission math."""

    def test_total_commission_spot_round_trip(self):
        from binance_commission import total_commission
        qty, entry, exit = 10.0, 100.0, 110.0
        comm = total_commission(qty, entry, exit, 'spot', None)
        r = _rate('spot')
        expected = qty * entry * r + qty * exit * r
        assert comm == pytest.approx(expected, abs=0.0001)

    def test_total_commission_futures_round_trip(self):
        from binance_commission import total_commission
        qty, entry, exit = 1.0, 50000.0, 51000.0
        comm = total_commission(qty, entry, exit, 'futures', None)
        r = _rate('futures')
        expected = qty * entry * r + qty * exit * r
        assert comm == pytest.approx(expected, abs=0.0001)

    def test_net_pnl_rejects_both_sides(self):
        from binance_commission import net_pnl, total_commission
        gross = (110.0 - 100.0) * 10.0
        net = net_pnl(gross, 10.0, 100.0, 110.0, 'spot', None)
        expected_comm = total_commission(10.0, 100.0, 110.0, 'spot', None)
        assert net == pytest.approx(gross - expected_comm, abs=0.0001)

    def test_expected_pnl_after_fees_subtracts_round_trip(self):
        from binance_commission import expected_pnl_after_fees
        gross_pnl = 5.0
        size = 100.0
        net = expected_pnl_after_fees(gross_pnl, size, 'spot', None)
        # round trip spot fee = 2 * spot_rate * size
        expected = gross_pnl - 2 * _rate('spot') * size
        assert net == pytest.approx(expected, abs=0.0001)


class TestPositionManagerNetPnl:
    """Verify closed positions are stored net of fees."""

    def test_close_position_buy_net_pnl(self):
        from position_manager import PositionManager, Position
        pm = PositionManager()
        position = Position(
            symbol="BTCUSDT",
            side="BUY",
            quantity=0.5,
            entry_price=50000.0,
            entry_time="2024-01-01T00:00:00Z",
        )
        pm.add_position(position)
        closed = pm.close_position("BTCUSDT", 52000.0, "manual")
        assert closed is not None
        r = _rate('spot')
        expected = 1000.0 - (0.5 * 50000.0 * r) - (0.5 * 52000.0 * r)
        assert closed.pnl == pytest.approx(expected, abs=0.001)


class TestFuturesNetPnl:
    """Verify futures net PnL helper used in runtime."""

    def test_net_pnl_futures(self):
        from binance_commission import net_pnl
        gross = (50000.0 - 49000.0) * 1.0
        net = net_pnl(gross, 1.0, 50000.0, 49000.0, 'futures', None)
        r = _rate('futures')
        expected_comm = 50000.0 * r + 49000.0 * r
        assert net == pytest.approx(gross - expected_comm, abs=0.0001)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
