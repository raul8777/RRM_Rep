import pytest
from binance_commission import (
    CommissionConfig,
    get_config,
    entry_commission,
    exit_commission,
    total_commission,
    net_pnl,
    expected_pnl_after_fees,
    kelly_ev_after_fees,
    net_return_pct,
)


class FakeSettings:
    binance_commission_rate = 0.001
    futures_commission_rate = 0.0004
    bnb_spot_commission_rate = 0.0005
    bnb_futures_commission_rate = 0.0002
    use_bnb_for_commission = False
    backtest_slippage_pct = 0.0005


class BNBSettings:
    binance_commission_rate = 0.001
    futures_commission_rate = 0.0004
    bnb_spot_commission_rate = 0.0005
    bnb_futures_commission_rate = 0.0002
    use_bnb_for_commission = True
    backtest_slippage_pct = 0.0005


def test_get_config_default():
    cfg = get_config(FakeSettings())
    assert cfg.spot_rate == 0.001
    assert cfg.futures_rate == 0.0004
    assert cfg.use_bnb is False
    assert cfg.rate_for('spot') == 0.001
    assert cfg.rate_for('futures') == 0.0004


def test_get_config_bnb():
    cfg = get_config(BNBSettings())
    assert cfg.use_bnb is True
    assert cfg.rate_for('spot') == 0.0005
    assert cfg.rate_for('futures') == 0.0002


def test_entry_exit_spot():
    assert entry_commission(100, 10, 'spot', FakeSettings()) == 1.0
    assert exit_commission(100, 11, 'spot', FakeSettings()) == 1.1


def test_total_commission_futures():
    assert total_commission(10, 100, 101, 'futures', FakeSettings()) == pytest.approx(0.804, abs=1e-9)


def test_net_pnl_spot():
    gross = (11 - 10) * 100  # 100 USDT profit
    assert net_pnl(gross, 100, 10, 11, 'spot', FakeSettings()) == pytest.approx(97.9, abs=1e-9)


def test_net_pnl_futures_loss():
    gross = -(105 - 100) * 10  # -50 USDT loss on SHORT
    assert net_pnl(gross, 10, 100, 105, 'futures', FakeSettings()) == pytest.approx(-50.82, abs=1e-9)


def test_expected_pnl_after_fees():
    gross = 5.0
    size = 100.0
    assert expected_pnl_after_fees(gross, size, 'spot', FakeSettings()) == pytest.approx(4.8, abs=1e-9)
    assert expected_pnl_after_fees(gross, size, 'futures', FakeSettings()) == pytest.approx(4.92, abs=1e-9)


def test_kelly_ev_after_fees():
    # prob=0.6, tp=2.5%, sl=1.5%, size=100 -> gross EV = 0.6*2.5 - 0.4*1.5 = 0.9
    ev = kelly_ev_after_fees(0.6, 0.025, 0.015, 100, 'spot', FakeSettings())
    assert ev == pytest.approx(0.7, abs=1e-9)  # 0.9 - 0.2 fee


def test_kelly_ev_after_fees_negative():
    # Small edge, fees eat it
    ev = kelly_ev_after_fees(0.52, 0.01, 0.01, 100, 'spot', FakeSettings())
    assert ev < 0


def test_net_return_pct():
    assert net_return_pct(3.5, 'spot', FakeSettings()) == pytest.approx(3.3, abs=1e-9)


def test_invalid_trade_type():
    with pytest.raises(ValueError):
        get_config(FakeSettings()).rate_for('margin')
