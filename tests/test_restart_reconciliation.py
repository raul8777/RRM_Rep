import json
from unittest.mock import patch

from pair_screener import get_screened_pairs, invalidate_cache
from position_manager import PositionManager, Position


class FakeDBClient:
    def __init__(self):
        self.rows = {}
        self.updates = []

    def execute_update(self, sql, params=None):
        self.updates.append((sql, params))
        if params and len(params) >= 2 and params[0] == 'screener_snapshot':
            self.rows['screener_snapshot'] = params[1]
        return 1

    def execute_query(self, sql, params=None):
        if params and params[0] == 'screener_snapshot' and 'trading_runtime_state' in sql:
            value = self.rows.get('screener_snapshot')
            if value is None:
                return []
            return [{"value": value}]
        return []


class FakeBinanceClient:
    def __init__(self, balances, prices):
        self._balances = balances
        self._prices = prices

    def get_account_balance(self):
        return self._balances

    def get_symbol_price(self, symbol):
        return self._prices[symbol]


def _mock_response(data):
    class _Resp:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode()

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    return _Resp(data)


def test_spot_sync_closes_missing_exchange_balance():
    pm = PositionManager()
    pm.add_position(Position(
        symbol='BTCUSDT',
        side='BUY',
        quantity=0.1,
        entry_price=50000.0,
        entry_time='2026-06-29T00:00:00Z',
        stop_loss=48000.0,
        take_profit=52000.0,
    ))

    import types
    _settings = types.SimpleNamespace(spot_reconcile_missing_confirm_cycles=1)
    binance = FakeBinanceClient(balances={'USDT': 1000.0}, prices={'BTCUSDT': 51000.0})
    imported = pm.sync_with_exchange_balance(binance, settings=_settings, min_value_usdt=5.0)

    assert imported == []
    assert pm.get_position('BTCUSDT') is None
    assert pm.get_all_positions() == {}


def test_screener_uses_snapshot_fallback_on_binance_failure():
    db = FakeDBClient()
    tickers = [
        {"symbol": "BTCUSDT", "lastPrice": "65000", "quoteVolume": "500000000", "priceChangePercent": "2.5"},
        {"symbol": "ETHUSDT", "lastPrice": "3500", "quoteVolume": "300000000", "priceChangePercent": "1.2"},
    ]

    with patch('pair_screener.urllib.request.urlopen', return_value=_mock_response(tickers)):
        snapshot = get_screened_pairs(spot_min_vol=10_000_000, regime_mode='SPOT_BULL', db_client=db)

    invalidate_cache()

    def _failing_urlopen(*args, **kwargs):
        raise RuntimeError('binance down')

    with patch('pair_screener.urllib.request.urlopen', _failing_urlopen):
        fallback = get_screened_pairs(spot_min_vol=10_000_000, regime_mode='SPOT_BULL', db_client=db)

    assert fallback['spot'] == snapshot['spot']
    assert fallback['futures'] == snapshot['futures']
    assert fallback['_meta']['regime_mode'] == snapshot['_meta']['regime_mode']
