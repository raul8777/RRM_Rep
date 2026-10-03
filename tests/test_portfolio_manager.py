"""
Unit tests for Portfolio Manager
"""

import pytest
from unittest.mock import MagicMock
from portfolio_manager import PortfolioManager, PairRanking, CapitalAllocation
from risk_calculator import RiskCalculator


class _Settings:
    portfolio_allocation_pct = 100.0
    min_pair_confidence_score = 50.0
    max_risk_per_pair_pct = 10.0
    diversification_factor = 1.0
    enable_position_closure = False
    bnb_extra_buffer_usd = 10.0
    bnb_buffer_days = 7
    bnb_min_balance_usd = 5.0
    db_url = ""


class TestRiskCalculator:
    """Test RiskCalculator functionality."""
    
    def test_calculate_volatility(self):
        """Test volatility calculation."""
        calc = RiskCalculator()
        prices = [100, 102, 101, 103, 102, 104, 103, 105]
        volatility = calc.calculate_volatility("BTCUSDT", prices, period=24)
        
        assert volatility >= 0
        assert volatility <= 100  # Should be a percentage
    
    def test_calculate_volatility_insufficient_data(self):
        """Test volatility calculation with insufficient data."""
        calc = RiskCalculator()
        prices = [100]
        volatility = calc.calculate_volatility("BTCUSDT", prices, period=24)
        
        assert volatility == 0.0
    
    def test_calculate_var(self):
        """Test Value at Risk calculation."""
        calc = RiskCalculator()
        prices = [100, 102, 101, 103, 102, 104, 103, 105]
        var = calc.calculate_var("BTCUSDT", prices, confidence_level=0.95)
        
        assert var >= 0
        assert var <= 100  # Should be a percentage
    
    def test_calculate_max_drawdown(self):
        """Test maximum drawdown calculation."""
        calc = RiskCalculator()
        prices = [100, 105, 110, 95, 90, 95, 100]
        max_dd = calc.calculate_max_drawdown("BTCUSDT", prices)
        
        assert max_dd >= 0
        assert max_dd <= 100  # Should be a percentage
    
    def test_calculate_risk_score(self):
        """Test combined risk score calculation."""
        calc = RiskCalculator()
        risk_score = calc.calculate_risk_score(
            "BTCUSDT",
            volatility=10.0,
            var=5.0,
            max_drawdown=15.0
        )
        
        assert risk_score >= 0
        assert risk_score <= 100


class TestPortfolioManager:
    """Test Portfolio Manager functionality."""
    
    def test_get_total_balance_mock(self):
        """Test total balance calculation with mock client."""
        # This would require mocking BinanceClient
        # For now, we'll skip actual implementation
        pass
    
    def test_rank_pairs_basic(self):
        """Test basic pair ranking."""
        # This would require proper setup with signals, technical scores, etc.
        # For now, we'll skip actual implementation
        pass
    
    def test_calculate_optimal_pair_count(self):
        """Test optimal pair count calculation."""
        # This would require proper setup
        # For now, we'll skip actual implementation
        pass

    def test_get_total_balance_respects_bnb_reserve(self):
        """usdt_available_after_reserve subtracts BNB buffer."""
        client = MagicMock()
        client.get_account_balance.return_value = {'USDT': 100.0, 'BNB': 0.0}
        pm = PortfolioManager(client, _Settings())
        bal = pm.get_total_balance(use_cache=False)
        assert 'usdt_available_after_reserve' in bal
        assert 'bnb_reserve_usdt' in bal
        assert bal['usdt_available_after_reserve'] <= bal['usdt_available']
        assert bal['usdt_available_after_reserve'] >= 0

    def test_proactive_targets_cover_max_entry_plus_buffer_per_wallet(self):
        class _TargetSettings(_Settings):
            max_per_trade_usdt = 1500.0
            kelly_max_usdt = 2500.0
            futures_kelly_max_usdt = 2500.0
            futures_usdt_per_trade = 150.0
            range_scalp_short_usdt_per_trade = 1000.0
            portfolio_liquidity_buffer_usdt = 10.0

        pm = PortfolioManager(MagicMock(), _TargetSettings())
        targets = pm.proactive_liquidity_targets()

        assert targets["spot_entry_max_usdt"] == 1500.0
        assert targets["spot_target_usdt"] == 1510.0
        assert targets["futures_entry_max_usdt"] == 2500.0
        assert targets["futures_target_usdt"] == 2510.0

    def test_converter_can_liquidate_only_bnb_above_fee_reserve(self):
        class _BnbSpotClient:
            testnet = True
            dry_run = False

            def __init__(self):
                self.balances = {"USDT": 1000.0, "BNB": 0.1}
                self.orders = []

            def get_account_balance(self):
                return dict(self.balances)

            def get_symbol_price(self, symbol):
                assert symbol == "BNBUSDT"
                return 600.0

            def get_symbol_filters(self, symbol):
                assert symbol == "BNBUSDT"
                return {"step": 0.001, "market_step": 0.001, "min_qty": 0.001, "min_notional": 10.0}

            def get_order_book(self, symbol, limit=20):
                assert symbol == "BNBUSDT"
                return {"bids": [["599.9", "10"]], "asks": [["600.1", "10"]]}

            def create_market_order(self, symbol, side, quantity):
                assert (symbol, side) == ("BNBUSDT", "SELL")
                self.orders.append(quantity)
                self.balances["BNB"] -= quantity * 1.0005
                self.balances["USDT"] += quantity * 600.0
                return {"orderId": "bnb-surplus-1", "quantity": quantity}

            def verify_and_fetch_fills(self, order, _context):
                qty = float(order["quantity"])
                return {
                    "binance_confirmed": True,
                    "order_id": order["orderId"],
                    "qty": qty,
                    "quote_qty": qty * 600.0,
                    "avg_price": 600.0,
                    "commission_usdt": 0.0,
                }

        class _EmptyPositionManager:
            def get_all_positions(self):
                return {}

        class _AuditDB:
            def __init__(self):
                self.events = []

            def execute_update(self, query, params):
                self.events.append(params)

        settings = _Settings()
        settings.binance_use_testnet = True
        settings.portfolio_spot_asset_auto_convert_enabled = True
        settings.bnb_max_reserve_usd = 30.0
        client = _BnbSpotClient()
        db = _AuditDB()
        pm = PortfolioManager(client, settings, position_manager=_EmptyPositionManager())

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                "binance_commission.get_bnb_reserve_usdt",
                lambda _settings: {"spot_reserve_usdt": 30.0},
            )
            assert pm.ensure_usdt_liquidity(
                900.0,
                decision_id="liquidity-reconcile-1",
                db_client=db,
                liquidate_bnb_surplus=True,
            )

        assert client.orders
        assert client.balances["BNB"] >= 30.0 / 600.0
        assert client.balances["BNB"] < 0.1
        assert [event[2] for event in db.events] == [
            "spot_asset_conversion_authorized",
            "spot_asset_conversion_result",
        ]

    def test_converter_never_liquidates_bnb_backing_open_spot_position(self):
        class _BnbSpotClient:
            testnet = True
            dry_run = False

            def __init__(self):
                self.balances = {"USDT": 0.0, "BNB": 0.1}
                self.orders = []

            def get_account_balance(self):
                return dict(self.balances)

            def get_symbol_price(self, symbol):
                return 600.0

            def get_symbol_filters(self, symbol):
                return {"step": 0.001, "market_step": 0.001, "min_qty": 0.001, "min_notional": 10.0}

            def get_order_book(self, symbol, limit=20):
                return {"bids": [["599.9", "10"]], "asks": [["600.1", "10"]]}

            def create_market_order(self, symbol, side, quantity):
                self.orders.append((symbol, side, quantity))
                return {"orderId": "unexpected-bnb-sale", "quantity": quantity}

        class _BnbPositionManager:
            def get_all_positions(self):
                return {"BNBUSDT": [object()]}

        settings = _Settings()
        settings.binance_use_testnet = True
        settings.portfolio_spot_asset_auto_convert_enabled = True
        settings.bnb_max_reserve_usd = 30.0
        client = _BnbSpotClient()
        pm = PortfolioManager(client, settings, position_manager=_BnbPositionManager())

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                "binance_commission.get_bnb_reserve_usdt",
                lambda _settings: {"spot_reserve_usdt": 30.0},
            )
            assert not pm.ensure_usdt_liquidity(
                50.0,
                decision_id="protected-bnb-position",
                db_client=MagicMock(),
                liquidate_bnb_surplus=True,
            )

        assert client.orders == []

    def test_converter_preserves_bnb_when_managed_position_lookup_fails(self):
        class _BnbSpotClient:
            testnet = True
            dry_run = False

            def __init__(self):
                self.balances = {"USDT": 0.0, "BNB": 0.1}
                self.orders = []

            def get_account_balance(self):
                return dict(self.balances)

            def get_symbol_price(self, _symbol):
                return 600.0

            def get_symbol_filters(self, _symbol):
                return {"step": 0.001, "market_step": 0.001, "min_qty": 0.001, "min_notional": 10.0}

            def get_order_book(self, _symbol, limit=20):
                return {"bids": [["599.9", "10"]], "asks": [["600.1", "10"]]}

            def create_market_order(self, symbol, side, quantity):
                self.orders.append((symbol, side, quantity))
                return {"orderId": "unexpected-bnb-sale", "quantity": quantity}

        class _BrokenPositionManager:
            def get_all_positions(self):
                raise RuntimeError("position store unavailable")

        settings = _Settings()
        settings.binance_use_testnet = True
        settings.portfolio_spot_asset_auto_convert_enabled = True
        settings.bnb_max_reserve_usd = 30.0
        client = _BnbSpotClient()
        pm = PortfolioManager(client, settings, position_manager=_BrokenPositionManager())
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                "binance_commission.get_bnb_reserve_usdt",
                lambda _settings: {"spot_reserve_usdt": 30.0},
            )
            assert not pm.ensure_usdt_liquidity(
                50.0,
                decision_id="unknown-position-state",
                db_client=MagicMock(),
                liquidate_bnb_surplus=True,
            )
        assert client.orders == []

    def test_proactive_reconcile_prepares_both_wallets_without_spending_bnb_reserve(self):
        class _SpotClient:
            testnet = True
            dry_run = False

            def __init__(self):
                self.balances = {"USDT": 0.0, "USDC": 300.0, "BNB": 0.1}
                self.orders = []
                self.client = MagicMock()

            def get_account_balance(self):
                return dict(self.balances)

            def get_symbol_price(self, symbol):
                return {"BNBUSDT": 600.0, "USDCUSDT": 1.0}[symbol]

            def get_symbol_filters(self, symbol):
                step = 0.001 if symbol == "BNBUSDT" else 0.01
                return {"step": step, "market_step": step, "min_qty": step, "min_notional": 10.0}

            def get_order_book(self, symbol, limit=20):
                price = self.get_symbol_price(symbol)
                return {"bids": [[str(price * 0.9999), "10000"]], "asks": [[str(price * 1.0001), "10000"]]}

            def create_market_order(self, symbol, side, quantity):
                self.orders.append((symbol, side, quantity))
                base_asset = symbol.removesuffix("USDT")
                self.balances[base_asset] -= quantity
                self.balances["USDT"] += quantity * self.get_symbol_price(symbol)
                return {"orderId": f"convert-{len(self.orders)}", "quantity": quantity}

            def verify_and_fetch_fills(self, order, _context):
                quantity = float(order["quantity"])
                symbol = self.orders[-1][0]
                return {
                    "binance_confirmed": True,
                    "order_id": order["orderId"],
                    "qty": quantity,
                    "quote_qty": quantity * self.get_symbol_price(symbol),
                    "avg_price": self.get_symbol_price(symbol),
                    "commission_usdt": 0.0,
                }

        class _FuturesClient:
            dry_run = False
            _use_testnet = True

            def __init__(self, spot):
                self.spot = spot
                self.balance = 0.0
                self.bnb = 0.2
                self.transfers = []

            def get_available_usdt(self):
                return self.balance

            def get_account_info(self):
                return {"assets": [
                    {"asset": "USDT", "availableBalance": self.balance},
                    {"asset": "BNB", "walletBalance": self.bnb, "availableBalance": 0.0},
                ]}

            def transfer_to_futures(self, amount, **_kwargs):
                self.transfers.append(amount)
                self.balance += amount
                self.spot.balances["USDT"] -= amount
                return True

        class _PositionManager:
            def get_all_positions(self):
                return {}

        class _AuditDB:
            def __init__(self):
                self.events = []

            def execute_query(self, _query, _params=None):
                return []

            def execute_update(self, _query, params):
                self.events.append(params)

        class _TargetSettings(_Settings):
            max_per_trade_usdt = 80.0
            kelly_max_usdt = 80.0
            futures_kelly_max_usdt = 40.0
            futures_usdt_per_trade = 30.0
            range_scalp_short_usdt_per_trade = 25.0
            portfolio_liquidity_buffer_usdt = 10.0
            portfolio_proactive_liquidity_enabled = True
            portfolio_spot_asset_auto_convert_enabled = True
            portfolio_liquidity_max_conversion_slippage_bps = 25.0
            binance_use_testnet = True
            dry_run = False
            bnb_max_reserve_usd = 30.0
            use_bnb_for_commission = True
            bnb_spot_commission_rate = 0.0005

        spot = _SpotClient()
        futures = _FuturesClient(spot)
        def _universal_transfer(*, type, asset, amount):
            assert (type, asset) == ("UMFUTURE_MAIN", "BNB")
            futures.bnb -= amount
            spot.balances["BNB"] += amount
        spot.client.universal_transfer.side_effect = _universal_transfer
        db = _AuditDB()
        pm = PortfolioManager(spot, _TargetSettings(), position_manager=_PositionManager())

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                "binance_commission.get_bnb_reserve_usdt",
                lambda _settings: {"spot_reserve_usdt": 10.0, "futures_reserve_usdt": 5.0},
            )
            result = pm.reconcile_proactive_liquidity(
                futures_client=futures,
                db_client=db,
                trigger="test_startup",
            )

        assert result["status"] == "shortfall"
        assert result["targets"]["spot_target_usdt"] == 90.0
        assert result["targets"]["futures_target_usdt"] == 50.0
        assert spot.balances["USDT"] - 10.0 >= 90.0
        assert futures.balance == 0.0
        assert spot.balances["BNB"] >= 10.0 / 600.0
        assert futures.bnb >= 5.0 / 600.0
        assert futures.transfers == []
        assert result["cross_wallet_transfer_supported"] is False
        assert result["cross_wallet_transfer_reason"] == "unsupported_in_testnet_environment"
        assert any(event[2] == "liquidity_transfer_blocked" for event in db.events)
        assert db.events[-1][2] == "liquidity_reconcile_result"

    def test_proactive_reconcile_does_not_move_assets_during_entry_pause(self):
        class _AuditDB:
            def execute_query(self, _query, _params=None):
                return [{"value": {"paused": True, "reason": "manual_test_pause"}}]

            def execute_update(self, *_args):
                raise AssertionError("pause path must not create move authorizations")

        client = MagicMock()
        settings = _Settings()
        settings.portfolio_proactive_liquidity_enabled = True
        settings.binance_use_testnet = True
        settings.dry_run = False
        client.testnet = True
        client.dry_run = False
        pm = PortfolioManager(client, settings)

        result = pm.reconcile_proactive_liquidity(db_client=_AuditDB(), trigger="paused_test")

        assert result["status"] == "skipped"
        assert result["reason"] == "new_entries_paused"
        client.get_account_balance.assert_not_called()

    def test_recent_failed_futures_transfer_does_not_trigger_more_spot_liquidation(self):
        from datetime import datetime, timezone

        class _SpotClient:
            testnet = True
            dry_run = False

            def __init__(self):
                self.orders = []

            def get_account_balance(self):
                return {"USDT": 1525.0, "USDC": 1000.0, "BNB": 0.02}

            def get_symbol_price(self, _symbol):
                return 600.0

            def get_symbol_filters(self, _symbol):
                return {"step": 0.01, "market_step": 0.01, "min_qty": 0.01, "min_notional": 5.0}

            def get_order_book(self, _symbol, limit=20):
                return {"bids": [["0.9999", "10000"]], "asks": [["1.0001", "10000"]]}

            def create_market_order(self, *args, **kwargs):
                self.orders.append((args, kwargs))
                raise AssertionError("cooldown must prevent further asset sales")

        class _FuturesClient:
            dry_run = False
            _use_testnet = True

            def __init__(self):
                self.transfers = []

            def get_available_usdt(self):
                return 45.0

            def get_account_info(self):
                return {"assets": [
                    {"asset": "USDT", "availableBalance": 45.0},
                    {"asset": "BNB", "walletBalance": 0.0, "availableBalance": 0.0},
                ]}

            def transfer_to_spot(self, *_args, **_kwargs):
                self.transfers.append("futures_to_spot")
                return False

            def transfer_to_futures(self, *_args, **_kwargs):
                self.transfers.append("spot_to_futures")
                return False

        class _EmptyPositionManager:
            def get_all_positions(self):
                return {}

        class _CooldownAuditDB:
            def __init__(self):
                self.events = []

            def execute_query(self, query, _params=None):
                if "pause_new_entries" in query:
                    return []
                return [{
                    "ts": datetime.now(timezone.utc),
                    "detail": {
                        "direction": "SPOT_TO_FUTURES",
                        "route": "universal_asset_transfer",
                        "status": "failed",
                    },
                }]

            def execute_update(self, _query, params):
                self.events.append(params)

        class _TargetSettings(_Settings):
            max_per_trade_usdt = 80.0
            kelly_max_usdt = 80.0
            futures_kelly_max_usdt = 40.0
            futures_usdt_per_trade = 30.0
            range_scalp_short_usdt_per_trade = 25.0
            portfolio_liquidity_buffer_usdt = 10.0
            portfolio_liquidity_transfer_cooldown_minutes = 30.0
            portfolio_proactive_liquidity_enabled = True
            portfolio_spot_asset_auto_convert_enabled = True
            portfolio_liquidity_max_conversion_slippage_bps = 25.0
            binance_use_testnet = True
            dry_run = False
            bnb_max_reserve_usd = 30.0

        spot = _SpotClient()
        futures = _FuturesClient()
        db = _CooldownAuditDB()
        pm = PortfolioManager(spot, _TargetSettings(), position_manager=_EmptyPositionManager())

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                "binance_commission.get_bnb_reserve_usdt",
                lambda _settings: {"spot_reserve_usdt": 15.0, "futures_reserve_usdt": 5.0},
            )
            result = pm.reconcile_proactive_liquidity(
                futures_client=futures,
                db_client=db,
                trigger="cycle_after_transfer_failure",
            )

        assert result["futures_transfer_cooling_down"] is False
        assert result["status"] == "minimum_ready"
        assert result["cross_wallet_transfer_reason"] == "unsupported_in_testnet_environment"
        assert spot.orders == []
        assert futures.transfers == []
        assert any(event[2] == "liquidity_transfer_blocked" for event in db.events)

    def test_idle_usdc_is_deployable_but_bnb_and_managed_assets_are_not(self):
        class _SpotClient:
            testnet = True
            dry_run = False

            def __init__(self):
                self.balances = {"USDT": 10.0, "USDC": 50.0, "BNB": 0.1, "SOL": 2.0}
                self.sold = []

            def get_account_balance(self):
                return dict(self.balances)

            def get_symbol_price(self, symbol):
                return {"USDCUSDT": 1.0, "BNBUSDT": 500.0, "SOLUSDT": 100.0}[symbol]

            def get_symbol_filters(self, symbol):
                assert symbol == "USDCUSDT"
                return {"step": 0.01, "market_step": 0.01, "min_qty": 0.01, "min_notional": 5.0}

            def get_order_book(self, symbol, limit=20):
                assert symbol == "USDCUSDT"
                return {"bids": [["0.9999", "10000"]], "asks": [["1.0001", "10000"]]}

            def create_market_order(self, symbol, side, quantity):
                self.sold.append((symbol, side, quantity))
                self.balances["USDC"] -= quantity
                self.balances["USDT"] += quantity
                return {"orderId": "conversion-1", "quantity": quantity}

            def verify_and_fetch_fills(self, order, _context):
                quantity = float(order["quantity"])
                return {
                    "binance_confirmed": True,
                    "order_id": order["orderId"],
                    "qty": quantity,
                    "quote_qty": quantity,
                    "avg_price": 1.0,
                    "commission_usdt": 0.0,
                }

        class _PositionManager:
            def get_all_positions(self):
                return {"SOLUSDT": [object()]}

        client = _SpotClient()
        settings = _Settings()
        settings.binance_use_testnet = True
        settings.portfolio_spot_asset_auto_convert_enabled = True
        db = MagicMock()
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                "binance_commission.get_bnb_reserve_usdt",
                lambda _settings: {"spot_reserve_usdt": 5.0},
            )
            pm = PortfolioManager(client, settings, position_manager=_PositionManager())
            assert pm.ensure_usdt_liquidity(20.0, decision_id="spot-decision", db_client=db)

        assert len(client.sold) == 1
        assert client.sold[0][0:2] == ("USDCUSDT", "SELL")
        assert client.balances["BNB"] == 0.1
        assert client.balances["SOL"] == 2.0

    def test_asset_conversion_blocks_when_order_book_slippage_exceeds_limit(self):
        class _HighSlippageClient:
            testnet = True
            dry_run = False

            def __init__(self):
                self.balances = {"USDT": 0.0, "USDC": 100.0}
                self.orders = []

            def get_account_balance(self):
                return dict(self.balances)

            def get_symbol_price(self, _symbol):
                return 1.0

            def get_symbol_filters(self, _symbol):
                return {"step": 0.01, "market_step": 0.01, "min_qty": 0.01, "min_notional": 5.0}

            def get_order_book(self, _symbol, limit=20):
                return {"bids": [["0.997", "1000"]], "asks": [["1.003", "1000"]]}

            def create_market_order(self, *args, **kwargs):
                self.orders.append((args, kwargs))
                raise AssertionError("high-slippage conversion must not submit")

        class _EmptyPositionManager:
            def get_all_positions(self):
                return {}

        class _AuditDB:
            def __init__(self):
                self.events = []

            def execute_update(self, _query, params):
                self.events.append(params)

        settings = _Settings()
        settings.binance_use_testnet = True
        settings.portfolio_spot_asset_auto_convert_enabled = True
        settings.portfolio_liquidity_max_conversion_slippage_bps = 25.0
        client = _HighSlippageClient()
        db = _AuditDB()
        pm = PortfolioManager(client, settings, position_manager=_EmptyPositionManager())

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                "binance_commission.get_bnb_reserve_usdt",
                lambda _settings: {"spot_reserve_usdt": 10.0},
            )
            assert not pm.ensure_usdt_liquidity(50.0, decision_id="slippage-block", db_client=db)

        assert client.orders == []
        assert [event[2] for event in db.events] == ["spot_asset_conversion_blocked"]
    
    def test_allocate_capital_basic(self):
        """Test basic capital allocation."""
        # This would require proper setup
        # For now, we'll skip actual implementation
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
