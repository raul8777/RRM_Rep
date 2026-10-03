from types import SimpleNamespace
from unittest.mock import patch
import json
from urllib.parse import parse_qs, urlparse

from futures_client import FuturesClient
from run_trades import _apply_dry_run_balance_envelope, _fund_selected_spot_entry_from_futures
import run_trades


class _SpotClient:
    def __init__(self, balance):
        self.balance = balance
        self.reads = 0

    def get_account_balance(self):
        self.reads += 1
        return {"USDT": self.balance}


class _FuturesClient:
    def __init__(self, available, spot_client=None, transfer_ok=True):
        self.available = available
        self.spot_client = spot_client
        self.transfer_ok = transfer_ok
        self.transfers = []

    def get_available_usdt(self):
        return self.available

    def transfer_to_spot(self, amount, **kwargs):
        self.transfers.append((amount, kwargs))
        if self.transfer_ok and self.spot_client is not None:
            self.spot_client.balance += amount
        return self.transfer_ok


class _AuditDB:
    def __init__(self):
        self.events = []

    def execute_update(self, query, params):
        self.events.append(params)


class _BalanceClient:
    def __init__(self, balance, dry_run=False):
        self.balance = balance
        self.dry_run = dry_run

    def get_account_balance(self):
        return {"USDT": self.balance}


def test_testnet_live_orders_keep_exchange_balance_for_preflight(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    client = _BalanceClient(78.67)
    settings = SimpleNamespace(
        binance_use_testnet=True,
        dry_run=False,
        dry_run_simulated_capital_usdt=2611.0,
    )

    _apply_dry_run_balance_envelope(client, settings)

    assert client.get_account_balance() == {"USDT": 78.67}


def test_explicit_dry_run_uses_configured_simulated_balance(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    client = _BalanceClient(78.67)
    settings = SimpleNamespace(
        binance_use_testnet=True,
        dry_run=True,
        dry_run_simulated_capital_usdt=2611.0,
    )

    _apply_dry_run_balance_envelope(client, settings)

    assert client.get_account_balance() == {"USDT": 2611.0}


def test_selected_spot_entry_uses_exact_shortfall_after_bnb_reserves():
    spot = _SpotClient(30.0)
    futures = _FuturesClient(80.0, spot_client=spot)
    settings = SimpleNamespace(binance_api_key="key", binance_api_secret="secret")
    audit_db = _AuditDB()
    reserves = {"spot_reserve_usdt": 10.0, "futures_reserve_usdt": 15.0}
    with patch("binance_commission.get_bnb_reserve_usdt", return_value=reserves):
        assert _fund_selected_spot_entry_from_futures(
            spot, futures, settings, 50.0, "decision-1", audit_db,
        )
    assert len(futures.transfers) == 1
    assert futures.transfers[0][0] == 30.0
    assert futures.transfers[0][1]["safety_buffer_usdt"] == 15.0
    assert [event[2] for event in audit_db.events] == ["capital_transfer_authorized", "capital_transfer_result"]


def test_selected_spot_entry_does_not_transfer_when_spot_is_funded():
    spot = _SpotClient(75.0)
    futures = _FuturesClient(80.0)
    reserves = {"spot_reserve_usdt": 10.0, "futures_reserve_usdt": 15.0}
    with patch("binance_commission.get_bnb_reserve_usdt", return_value=reserves):
        assert _fund_selected_spot_entry_from_futures(
            spot, futures, SimpleNamespace(), 50.0, "decision-2",
        )
    assert futures.transfers == []


def test_selected_spot_entry_blocks_without_touching_protected_futures_reserve():
    spot = _SpotClient(30.0)
    futures = _FuturesClient(40.0)
    reserves = {"spot_reserve_usdt": 10.0, "futures_reserve_usdt": 35.0}
    with patch("binance_commission.get_bnb_reserve_usdt", return_value=reserves):
        assert not _fund_selected_spot_entry_from_futures(
            spot, futures, SimpleNamespace(), 50.0, "decision-3",
        )
    assert futures.transfers == []


def test_selected_spot_and_futures_entries_share_deployable_reserved_pool(monkeypatch):
    monkeypatch.setattr(run_trades, "_CAPITAL_RESERVATIONS", {
        "spot:ATOMUSDT": {"bucket": "spot", "amount": 40.0},
        "futures:BTCUSDT": {"bucket": "futures", "amount": 25.0},
    })
    balance = {
        "usdt_available_after_reserve": 10.0,
        "spot_deployable_usdt_after_reserve": 100.0,
        "futures_available_after_reserve": 80.0,
    }

    spot_state = run_trades._capital_routing_state(balance, "SPOT_LONG")
    futures_state = run_trades._capital_routing_state(balance, "FUTURES_SHORT")

    assert spot_state["spot_available_after_reserve"] == 10.0
    assert spot_state["bucket_available_after_reserve"] == 180.0
    assert spot_state["bucket_free_after_reserve"] == 115.0
    assert futures_state["bucket_free_after_reserve"] == 115.0


def test_selected_spot_entry_converts_assets_before_requesting_futures_transfer():
    spot = _SpotClient(0.0)
    futures = _FuturesClient(0.0, spot_client=spot)
    settings = SimpleNamespace(binance_use_testnet=True)
    audit_db = _AuditDB()

    class _AssetConverter:
        def __init__(self, binance_client, _settings, position_manager=None):
            self.binance_client = binance_client

        def ensure_usdt_liquidity(self, required, *, decision_id, db_client):
            assert required == 50.0
            assert decision_id == "spot-usdc-entry"
            assert db_client is audit_db
            self.binance_client.balance = 60.0
            return True

    reserves = {"spot_reserve_usdt": 10.0, "futures_reserve_usdt": 10.0}
    with (
        patch("binance_commission.get_bnb_reserve_usdt", return_value=reserves),
        patch("portfolio_manager.PortfolioManager", _AssetConverter),
    ):
        assert _fund_selected_spot_entry_from_futures(
            spot,
            futures,
            settings,
            50.0,
            "spot-usdc-entry",
            audit_db,
            position_manager=object(),
        )

    assert futures.transfers == []
    assert spot.balance == 60.0


def test_testnet_transfer_is_blocked_before_network_io():
    client = FuturesClient.__new__(FuturesClient)
    client.api_key = "test-key"
    client.api_secret = "test-secret"
    client.dry_run = False
    client._use_testnet = True
    client._cross_wallet_transfer_enabled = True
    client._sapi_base = "https://demo-api.binance.com"
    client.get_available_usdt = lambda: 100.0
    with patch("futures_client.urllib.request.urlopen") as urlopen:
        assert not client.transfer_to_futures(5.0)
        assert not client.transfer_to_spot(20.0, safety_buffer_usdt=10.0)
    urlopen.assert_not_called()


def test_futures_jit_audits_and_transfers_only_reserved_shortfall():
    client = FuturesClient.__new__(FuturesClient)
    client.settings = SimpleNamespace(binance_use_testnet=True)
    client._cached_settings = client.settings
    client._use_testnet = True
    client.api_key = "test-key"
    client.api_secret = "test-secret"
    client.dry_run = False
    client.db_client = _AuditDB()
    client.balance = 40.0
    transfers = []

    def _get_available():
        return client.balance

    def _transfer(amount, *_args, **_kwargs):
        transfers.append(amount)
        client.balance += amount
        return True

    client.get_available_usdt = _get_available
    client.transfer_to_futures = _transfer
    spot_client = _SpotClient(100.0)
    reserves = {"spot_reserve_usdt": 15.0, "futures_reserve_usdt": 10.0}
    with (
        patch("binance_commission.get_bnb_reserve_usdt", return_value=reserves),
        patch("binance_client.BinanceClient", return_value=spot_client),
        patch("futures_client.time.sleep"),
    ):
        assert client.check_and_fund(50.0, decision_id="decision-futures")
    assert transfers == [20.0]
    assert [event[2] for event in client.db_client.events] == [
        "capital_transfer_authorized", "capital_transfer_result",
    ]
    assert json.loads(client.db_client.events[0][3])["decision_id"] == "decision-futures"


def test_futures_jit_converts_spot_assets_before_transfer():
    client = FuturesClient.__new__(FuturesClient)
    client.settings = SimpleNamespace(
        binance_use_testnet=True,
        binance_api_key="test-key",
        binance_api_secret="test-secret",
        portfolio_allocation_pct=100.0,
        min_pair_confidence_score=50.0,
        max_risk_per_pair_pct=10.0,
        diversification_factor=1.0,
        enable_position_closure=False,
    )
    client._cached_settings = client.settings
    client._use_testnet = True
    client.api_key = "test-key"
    client.api_secret = "test-secret"
    client.dry_run = False
    client.db_client = _AuditDB()
    client.balance = 40.0
    spot_client = _SpotClient(0.0)
    transfers = []

    class _AssetConverter:
        def __init__(self, binance_client, settings, position_manager=None):
            self.binance_client = binance_client
            self.position_manager = position_manager

        def ensure_usdt_liquidity(self, required, *, decision_id, db_client):
            assert required == 20.0
            assert decision_id == "decision-futures-usdc"
            assert db_client is client.db_client
            self.binance_client.balance = 35.0  # $20 spendable plus the $15 BNB reserve.
            return True

    def _transfer(amount, *_args, **_kwargs):
        transfers.append(amount)
        spot_client.balance -= amount
        client.balance += amount
        return True

    client.get_available_usdt = lambda: client.balance
    client.transfer_to_futures = _transfer
    reserves = {"spot_reserve_usdt": 15.0, "futures_reserve_usdt": 10.0}
    position_manager = object()
    with (
        patch("binance_commission.get_bnb_reserve_usdt", return_value=reserves),
        patch("binance_client.BinanceClient", return_value=spot_client),
        patch("portfolio_manager.PortfolioManager", _AssetConverter),
        patch("futures_client.time.sleep"),
    ):
        assert client.check_and_fund(
            50.0,
            decision_id="decision-futures-usdc",
            position_manager=position_manager,
        )

    assert transfers == [20.0]
    assert spot_client.balance == 15.0
    assert client.balance == 60.0


def test_futures_funding_check_uses_resolved_testnet_setting_without_transfer():
    client = FuturesClient.__new__(FuturesClient)
    client.settings = SimpleNamespace(
        binance_use_testnet=True,
        binance_api_key="test-key",
        binance_api_secret="test-secret",
    )
    client._cached_settings = client.settings
    client._use_testnet = True
    client.api_key = "test-key"
    client.api_secret = "test-secret"
    client.dry_run = False
    client.db_client = _AuditDB()
    client.get_available_usdt = lambda: 80.0
    spot_client = _SpotClient(100.0)
    reserves = {"spot_reserve_usdt": 15.0, "futures_reserve_usdt": 10.0}

    with (
        patch("binance_commission.get_bnb_reserve_usdt", return_value=reserves),
        patch("binance_client.BinanceClient", return_value=spot_client) as make_spot_client,
    ):
        assert client.check_and_fund(50.0, decision_id="funded-testnet-entry")

    assert make_spot_client.call_args.kwargs["testnet"] is True
    assert spot_client.reads == 1
    assert client.db_client.events == []


def test_periodic_capbridge_cannot_transfer_even_if_caller_requests_it():
    client = FuturesClient.__new__(FuturesClient)
    result = client.smart_rebalance_capital(
        spot_usdt=1000.0,
        open_shorts=0,
        max_shorts=5,
        kelly_margin=200.0,
        open_spot_count=0,
        kelly_spot_amount=100.0,
        regime="RANGE_SCALP",
        execute_transfers=True,
    )
    assert result["planned_only"] is True
    assert result["executed"] is False
    assert result["success"] is False
    assert "accepted entry decision" in result["error"]
