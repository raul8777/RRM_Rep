import pytest
from unittest.mock import MagicMock
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
    auto_manage_bnb_discount,
    get_bnb_reserve_usdt,
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


class BNBAutoSettings:
    binance_commission_rate = 0.001
    futures_commission_rate = 0.0004
    bnb_spot_commission_rate = 0.0005
    bnb_futures_commission_rate = 0.0002
    use_bnb_for_commission = True
    auto_bnb_for_commission = True
    bnb_buffer_days = 7
    bnb_min_balance_usd = 5.0
    bnb_extra_buffer_usd = 10.0
    db_url = ""


class FakeSpotClient:
    def __init__(self, bnb=0.0, usdt=1000.0):
        self._bnb = bnb
        self._usdt = usdt
        self.orders = []

    def get_symbol_price(self, symbol):
        return 600.0 if symbol == 'BNBUSDT' else 1.0

    def get_account_balance(self):
        return {'BNB': self._bnb, 'USDT': self._usdt}

    def create_market_order(self, symbol, side, quantity):
        if side == 'BUY' and symbol == 'BNBUSDT':
            self._usdt -= quantity * 600.0
            self._bnb += quantity
            self.orders.append(('BUY', quantity))
            return {'orderId': 1}
        raise Exception("bad order")

    class client:
        @staticmethod
        def universal_transfer(type, asset, amount):
            if type == 'MAIN_UMFUTURE' and asset == 'BNB':
                return {'tranId': 1}
            raise Exception("bad transfer")


class FakeFuturesClient:
    def __init__(self, bnb=0.0):
        self._bnb = bnb

    def get_price(self, symbol):
        return 600.0

    def get_account_info(self):
        return {'assets': [{'asset': 'BNB', 'availableBalance': self._bnb}]}

    def get_available_usdt(self):
        return 0.0

    def get_total_wallet_usdt(self):
        return 0.0


def test_get_bnb_reserve_usdt():
    cfg = get_bnb_reserve_usdt(BNBAutoSettings())
    assert cfg['spot_reserve_usdt'] >= 10.0
    assert cfg['futures_reserve_usdt'] >= 10.0
    assert cfg['total_reserve_usdt'] == cfg['spot_reserve_usdt'] + cfg['futures_reserve_usdt']


def test_auto_manage_bnb_discount_buy_and_transfer():
    spot = FakeSpotClient(bnb=0.0, usdt=1000.0)
    fut = FakeFuturesClient(bnb=0.0)
    db = MagicMock()
    res = auto_manage_bnb_discount(BNBAutoSettings(), spot_client=spot, futures_client=fut, db_client=db)
    assert res['spot_bnb'] > 0
    assert res['futures_bnb'] > 0 or 'transferred' in res['futures_action']
    assert db.execute_update.called


def test_auto_manage_bnb_discount_skips_when_insufficient_usdt():
    spot = FakeSpotClient(bnb=0.0, usdt=1.0)
    fut = FakeFuturesClient(bnb=0.0)
    db = MagicMock()
    res = auto_manage_bnb_discount(BNBAutoSettings(), spot_client=spot, futures_client=fut, db_client=db)
    assert res['spot_action'] == 'buy_skipped_insufficient_usdt'
    assert res['shortfall_usdt'] > 0
    events = [c.args[1][2] for c in db.execute_update.call_args_list if len(c.args) >= 2 and len(c.args[1]) >= 3]
    assert any('bnb_liquidity_shortfall' in e for e in events)


def test_auto_manage_bnb_discount_no_action_when_funded():
    spot = FakeSpotClient(bnb=1.0, usdt=1000.0)
    fut = FakeFuturesClient(bnb=1.0)
    db = MagicMock()
    res = auto_manage_bnb_discount(BNBAutoSettings(), spot_client=spot, futures_client=fut, db_client=db)
    assert res['spot_action'] == 'none'
    assert res['futures_action'] == 'none'
    assert res['use_bnb'] is True
