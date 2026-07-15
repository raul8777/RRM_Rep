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
    def __init__(self, balances, prices, trades=None):
        self._balances = balances
        self._prices = prices
        self._trades = trades or []

    def get_account_balance(self):
        return self._balances

    def get_symbol_price(self, symbol):
        return self._prices[symbol]

    def get_my_trades(self, symbol, limit=100):
        return [t for t in self._trades if t.get("symbol") == symbol][:limit]


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


def test_spot_sync_closes_ghost_when_balance_ratio_below_threshold():
    """Binance balance << DB qty (<0.1%) with a recent SELL must close the DB position as ghost."""
    import time
    pm = PositionManager()
    pm.add_position(Position(
        symbol='ADAUSDT',
        side='BUY',
        quantity=37.5,
        entry_price=0.1678,
        entry_time='2026-07-11T08:06:00Z',
    ))

    import types
    _settings = types.SimpleNamespace(
        spot_reconcile_missing_confirm_cycles=2,
        binance_commission_rate=0.001,
    )
    # Balance is 0.007 vs 37.5 -> ratio ~0.000187 -> ghost
    # A recent SELL trade confirms the position was disposed on Binance.
    _now_ms = int(time.time() * 1000)
    binance = FakeBinanceClient(
        balances={'USDT': 1000.0, 'ADA': 0.007},
        prices={'ADAUSDT': 0.1711},
        trades=[{
            "symbol": "ADAUSDT",
            "id": 1,
            "orderId": 1,
            "qty": "37.5",
            "price": "0.1711",
            "commission": "0.01",
            "commissionAsset": "USDT",
            "time": _now_ms,
            "isBuyer": False,
            "isMaker": False,
        }],
    )
    imported = pm.sync_with_exchange_balance(binance, settings=_settings, min_value_usdt=5.0)

    assert imported == []
    assert pm.get_position('ADAUSDT') is None
    closed = [c for c in pm.get_closed_positions() if c.symbol == 'ADAUSDT']
    assert len(closed) == 1
    assert closed[0].close_reason == 'ghost_balance_mismatch'


def test_spot_sync_keeps_position_when_balance_ratio_above_threshold():
    """Binance balance within tolerance should keep the position open and validate metadata."""
    pm = PositionManager()
    pm.add_position(Position(
        symbol='SOLUSDT',
        side='BUY',
        quantity=5.474,
        entry_price=78.17,
        entry_time='2026-07-11T18:23:00Z',
    ))

    import types
    _settings = types.SimpleNamespace(
        spot_reconcile_missing_confirm_cycles=2,
        binance_commission_rate=0.001,
    )
    binance = FakeBinanceClient(
        balances={'USDT': 1000.0, 'SOL': 5.474069},
        prices={'SOLUSDT': 78.18},
    )
    imported = pm.sync_with_exchange_balance(binance, settings=_settings, min_value_usdt=5.0)

    assert imported == []
    assert pm.get_position('SOLUSDT') is not None
    assert pm.get_position('SOLUSDT')[0].quantity == 5.474


def test_ghost_balance_mismatch_deferred_without_recent_sell():
    """Binance balance << DB qty with NO recent SELL must NOT close the position."""
    import time
    pm = PositionManager()
    pm.add_position(Position(
        symbol='TRXUSDT',
        side='BUY',
        quantity=1000.0,
        entry_price=0.12,
        entry_time='2026-07-11T08:06:00Z',
    ))

    import types
    _settings = types.SimpleNamespace(
        spot_reconcile_missing_confirm_cycles=2,
        binance_commission_rate=0.001,
    )
    binance = FakeBinanceClient(
        balances={'USDT': 1000.0, 'TRX': 0.0001},
        prices={'TRXUSDT': 0.12},
        trades=[],  # No recent SELL
    )
    imported = pm.sync_with_exchange_balance(binance, settings=_settings, min_value_usdt=5.0)

    assert imported == []
    assert pm.get_position('TRXUSDT') is not None
    closed = [c for c in pm.get_closed_positions() if c.symbol == 'TRXUSDT']
    assert len(closed) == 0


def test_trx_imported_from_balance_after_removing_exclusion():
    """TRX balance above min value should be imported now that it is not excluded."""
    pm = PositionManager()

    import types
    _settings = types.SimpleNamespace(
        spot_reconcile_missing_confirm_cycles=2,
        binance_commission_rate=0.001,
    )
    binance = FakeBinanceClient(
        balances={'USDT': 1000.0, 'TRX': 500.0},
        prices={'TRXUSDT': 0.12},
    )
    imported = pm.sync_with_exchange_balance(binance, settings=_settings, min_value_usdt=5.0)

    assert 'TRXUSDT' in imported
    assert pm.get_position('TRXUSDT') is not None


def test_bnb_imported_when_commission_reserve_disabled():
    pm = PositionManager()

    import types
    _settings = types.SimpleNamespace(
        spot_reconcile_missing_confirm_cycles=2,
        binance_commission_rate=0.001,
        use_bnb_for_commission=False,
        auto_bnb_for_commission=False,
    )
    binance = FakeBinanceClient(
        balances={'USDT': 1000.0, 'BNB': 0.2},
        prices={'BNBUSDT': 600.0},
    )
    imported = pm.sync_with_exchange_balance(binance, settings=_settings, min_value_usdt=5.0)

    assert 'BNBUSDT' in imported
    assert pm.get_position('BNBUSDT') is not None


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
