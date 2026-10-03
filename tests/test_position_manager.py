"""
Integration tests for Position Manager
"""

import json
from decimal import Decimal
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from position_manager import PositionManager, Position, ClosedPosition
from binance_commission import get_config as _pm_test_cfg


def _spot_rate():
    return _pm_test_cfg().rate_for('spot')


class RecordingDb:
    def __init__(self, failed_context=None, fail_open_insert=False):
        self.failed_context = failed_context
        self.fail_open_insert = fail_open_insert
        self.updates = []

    def execute_query(self, sql, _params=None):
        if "entry_persistence_failed" in sql and self.failed_context:
            return [{"detail": self.failed_context}]
        return []

    def execute_update(self, sql, params=None):
        if self.fail_open_insert and "INSERT INTO trades_open" in sql:
            raise TypeError("database write failed")
        self.updates.append((sql, params))
        return 1


class BalanceClient:
    def get_account_balance(self):
        return {"USDT": 1000.0, "BTC": 1.0}

    def get_symbol_price(self, symbol):
        assert symbol == "BTCUSDT"
        return 100.0


class DriftBalanceClient:
    def __init__(self, balance_qty):
        self.balance_qty = balance_qty

    def get_account_balance(self):
        return {"BCH": self.balance_qty}

    def get_symbol_price(self, symbol):
        assert symbol == "BCHUSDT"
        return 10.0

    def get_my_trades(self, symbol, limit=5):
        return []


class DetailedBalanceClient:
    def __init__(self, details=None, error=None, trades=None):
        self.details = details or {}
        self.error = error
        self.trades = trades or []

    def get_account_balance_details(self):
        if self.error:
            raise self.error
        return self.details

    def get_symbol_price(self, symbol):
        return 5.2

    def get_my_trades(self, symbol, limit=5):
        return self.trades[:limit]


class TestPositionManager:
    """Test Position Manager functionality."""
    
    def test_add_position_serializes_decimal_entry_context_before_persisting(self):
        db = RecordingDb()
        pm = PositionManager(db)
        position = Position(
            symbol="BTCUSDT",
            side="BUY",
            quantity=Decimal("0.5"),
            entry_price=Decimal("50000"),
            entry_time="2026-07-21T00:00:00Z",
            metadata={
                "entry_regime": "SPOT_BULL",
                "entry_profile_label": "A",
                "entry_order_id": "order-1",
                "binance_entry_confirmed": True,
                "nested": {"value": Decimal("1.25")},
            },
            pnl_prediction={"expected_pnl": Decimal("2.5")},
        )

        assert pm.add_position(position) is True

        open_insert = next(params for sql, params in db.updates if "INSERT INTO trades_open" in sql)
        metadata = json.loads(open_insert[8])
        prediction = json.loads(open_insert[10])
        assert metadata["regime_at_entry"] == "SPOT_BULL"
        assert metadata["profile_at_entry"] == "A"
        assert metadata["nested"]["value"] == 1.25
        assert prediction["expected_pnl"] == 2.5

    def test_add_position_fails_closed_when_canonical_persistence_fails(self):
        db = RecordingDb(fail_open_insert=True)
        pm = PositionManager(db)
        position = Position(
            symbol="BTCUSDT", side="BUY", quantity=0.5, entry_price=50000.0,
            entry_time="2026-07-21T00:00:00Z",
            metadata={"entry_order_id": "order-2", "binance_entry_confirmed": True},
        )

        assert pm.add_position(position) is False
        assert pm.get_position("BTCUSDT") is None
        audit_params = [params for sql, params in db.updates if "trades_audit" in sql]
        assert any(params[2] == "entry_persistence_failed" for params in audit_params)
        assert all(params[2] != "open" for params in audit_params)

    def test_reconciliation_restores_only_verified_failed_entry_context(self):
        failed_context = {
            "quantity": 1.0,
            "entry_metadata": {
                "entry_order_id": "order-3",
                "binance_entry_confirmed": True,
                "regime_at_entry": "SPOT_BULL",
                "profile_at_entry": "A",
                "trade_type": "spot",
            },
        }
        db = RecordingDb(failed_context=failed_context)
        pm = PositionManager(db)
        settings = SimpleNamespace(
            token_categories={}, category_thresholds={}, use_bnb_for_commission=False,
            auto_bnb_for_commission=False, binance_commission_rate=0.001,
        )

        assert pm.sync_with_exchange_balance(BalanceClient(), settings, min_value_usdt=5.0) == ["BTCUSDT"]

        restored = pm.get_position("BTCUSDT")[0].metadata
        assert restored["source"] == "reconcile_canonical_restored"
        assert restored["entry_context_status"] == "restored_from_failed_persistence"
        assert restored["regime_at_entry"] == "SPOT_BULL"
        assert restored["profile_at_entry"] == "A"

    def test_add_position(self):
        """Test adding a position."""
        pm = PositionManager()
        position = Position(
            symbol="BTCUSDT",
            side="BUY",
            quantity=0.5,
            entry_price=50000.0,
            entry_time="2024-01-01T00:00:00Z"
        )
        
        pm.add_position(position)
        
        assert pm.get_position("BTCUSDT") is not None
        assert pm.get_position("BTCUSDT")[0].symbol == "BTCUSDT"
        assert pm.get_position("BTCUSDT")[0].quantity == 0.5
    
    def test_get_position_not_found(self):
        """Test getting a position that doesn't exist."""
        pm = PositionManager()
        assert pm.get_position("BTCUSDT") is None
    
    def test_close_position_buy(self):
        """Test closing a BUY position."""
        pm = PositionManager()
        position = Position(
            symbol="BTCUSDT",
            side="BUY",
            quantity=0.5,
            entry_price=50000.0,
            entry_time="2024-01-01T00:00:00Z"
        )
        
        pm.add_position(position)
        closed = pm.close_position("BTCUSDT", 52000.0, "manual")
        
        assert closed is not None
        assert closed.symbol == "BTCUSDT"
        # PnL neto de comisiones usando tasas activas del sistema
        _r = _spot_rate()
        _expected_pnl = 1000.0 - (0.5 * 50000.0 * _r) - (0.5 * 52000.0 * _r)
        _expected_pct = (_expected_pnl / (0.5 * 50000.0)) * 100
        assert closed.pnl == pytest.approx(_expected_pnl, abs=0.001)
        assert closed.pnl_percentage == pytest.approx(_expected_pct, abs=0.001)
        assert closed.close_reason == "manual"
        assert pm.get_position("BTCUSDT") is None
    
    def test_close_position_sell(self):
        """Test closing a SELL position."""
        pm = PositionManager()
        position = Position(
            symbol="BTCUSDT",
            side="SELL",
            quantity=0.5,
            entry_price=50000.0,
            entry_time="2024-01-01T00:00:00Z"
        )
        
        pm.add_position(position)
        closed = pm.close_position("BTCUSDT", 48000.0, "manual")
        
        assert closed is not None
        # PnL neto de comisiones usando tasas activas del sistema
        _r = _spot_rate()
        _expected_pnl = 1000.0 - (0.5 * 50000.0 * _r) - (0.5 * 48000.0 * _r)
        assert closed.pnl == pytest.approx(_expected_pnl, abs=0.001)
        assert closed.close_reason == "manual"
        assert pm.get_position("BTCUSDT") is None
    
    def test_get_all_positions(self):
        """Test getting all positions."""
        pm = PositionManager()
        
        pm.add_position(Position("BTCUSDT", "BUY", 0.5, 50000.0, "2024-01-01T00:00:00Z"))
        pm.add_position(Position("ETHUSDT", "BUY", 10.0, 3000.0, "2024-01-01T00:00:00Z"))
        
        all_positions = pm.get_all_positions()
        
        assert len(all_positions) == 2
        assert "BTCUSDT" in all_positions
        assert "ETHUSDT" in all_positions

    def test_set_sl_by_usd_loss_handles_multiple_entries(self):
        """Test setting a stop loss from a maximum USD loss for a multi-entry position."""
        pm = PositionManager()
        pm.add_position(Position("KAITOUSDT", "BUY", 10.0, 1.00, "2024-01-01T00:00:00Z"))
        pm.add_position(Position("KAITOUSDT", "BUY", 5.0, 1.20, "2024-01-01T00:00:00Z"))

        result = pm.set_stop_loss_by_usd_loss("KAITOUSDT", 5.0)

        assert result["ok"] is True
        assert result["target_loss_usd"] == 5.0
        assert result["stop_loss_price"] == pytest.approx(0.7333333333)
        entries = pm.get_position("KAITOUSDT")
        assert len(entries) == 2
        assert all(entry.stop_loss == pytest.approx(0.7333333333) for entry in entries)
    
    def test_get_position_summary(self):
        """Test getting position summary."""
        pm = PositionManager()
        
        pm.add_position(Position("BTCUSDT", "BUY", 0.5, 50000.0, "2024-01-01T00:00:00Z"))
        pm.add_position(Position("ETHUSDT", "BUY", 10.0, 3000.0, "2024-01-01T00:00:00Z"))
        
        summary = pm.get_position_summary()
        
        assert summary["open_positions_count"] == 2
        assert summary["total_value_usdt"] == 55000.0  # 0.5 * 50000 + 10 * 3000
        symbols = [p["symbol"] for p in summary["positions"]]
        assert "BTCUSDT" in symbols
        assert "ETHUSDT" in symbols
    
    def test_check_stop_loss_take_profit(self):
        """Test stop loss and take profit checking."""
        pm = PositionManager()
        
        position = Position(
            symbol="BTCUSDT",
            side="BUY",
            quantity=0.5,
            entry_price=50000.0,
            entry_time="2024-01-01T00:00:00Z",
            stop_loss=48000.0,
            take_profit=52000.0
        )
        
        pm.add_position(position)
        
        # Test stop loss trigger
        closed = pm.check_stop_loss_take_profit({"BTCUSDT": 47000.0})
        assert len(closed) == 1
        assert closed[0].close_reason == "stop_loss"
        
        # Re-add position for take profit test
        pm.add_position(position)
        
        # Test take profit trigger
        closed = pm.check_stop_loss_take_profit({"BTCUSDT": 53000.0})
        assert len(closed) == 1
        assert closed[0].close_reason == "take_profit"

    def test_partial_close_clamps_quantity_and_preserves_residual(self):
        """Partial close must record real sold quantity and keep residual open."""
        pm = PositionManager()
        position = Position(
            symbol="XRPUSDT",
            side="BUY",
            quantity=1000.0,
            entry_price=0.50,
            entry_time="2024-01-01T00:00:00Z",
            entry_commission=0.5,
            metadata={'trade_type': 'spot'},
        )
        pm.add_position(position)
        closed = pm.close_position("XRPUSDT", 0.55, "profit_realization_supervisor_partial", qty_closed=400.0)

        assert closed is not None
        assert closed.quantity == 400.0
        assert closed.metadata.get('partial_close') is True
        assert closed.metadata.get('partial_qty_closed') == 400.0
        assert closed.metadata.get('partial_qty_remaining') == 600.0

        residual = pm.get_position("XRPUSDT")
        assert residual is not None
        assert residual[0].quantity == 600.0
        assert residual[0].metadata.get('partial_close') is True
        assert residual[0].metadata.get('partial_qty_remaining') == 600.0

    def test_partial_close_commission_proportional(self):
        """Partial close should allocate entry commission proportionally."""
        pm = PositionManager()
        position = Position(
            symbol="ENAUSDT",
            side="BUY",
            quantity=500.0,
            entry_price=1.0,
            entry_time="2024-01-01T00:00:00Z",
            entry_commission=1.0,
            metadata={'trade_type': 'spot'},
        )
        pm.add_position(position)
        closed = pm.close_position("ENAUSDT", 1.10, "partial_close", qty_closed=200.0)

        # 40% of position closed -> 40% of entry commission assigned to closed part
        assert closed is not None
        assert closed.entry_commission == pytest.approx(0.4, abs=0.001)
        residual = pm.get_position("ENAUSDT")
        assert residual[0].entry_commission == pytest.approx(0.6, abs=0.001)

    def test_sync_with_exchange_balance_trims_qty_drift_to_exchange_balance(self):
        """A DB spot position should be partially closed to match Binance balance when drift is material."""
        pm = PositionManager()
        pm.add_position(Position(
            symbol="BCHUSDT",
            side="BUY",
            quantity=0.065,
            entry_price=10.0,
            entry_time="2024-01-01T00:00:00Z",
            metadata={"trade_type": "spot"},
        ))
        settings = SimpleNamespace(
            token_categories={},
            category_thresholds={},
            use_bnb_for_commission=False,
            auto_bnb_for_commission=False,
            binance_commission_rate=0.001,
            spot_reconcile_missing_confirm_cycles=2,
            spot_ghost_sell_confirmation_hours=24,
        )

        pm.sync_with_exchange_balance(DriftBalanceClient(0.001), settings, min_value_usdt=5.0)

        remaining = pm.get_position("BCHUSDT")
        assert remaining is not None
        # DB should converge to the Binance balance in one pass.
        assert remaining[0].quantity == pytest.approx(0.001)
        closed = [c for c in pm.get_closed_positions(limit=10) if c.close_reason == "reconcile_qty_drift"]
        assert closed
        assert closed[0].quantity == pytest.approx(0.064)

    def test_sync_keeps_position_when_balance_is_locked_not_missing(self):
        pm = PositionManager()
        position = Position(
            symbol="NEARUSDT", side="BUY", quantity=13.9, entry_price=5.209,
            entry_time="2026-09-27T17:24:41Z", metadata={"trade_type": "spot"},
        )
        pm.add_position(position)
        settings = SimpleNamespace(
            token_categories={}, category_thresholds={}, use_bnb_for_commission=False,
            auto_bnb_for_commission=False, binance_commission_rate=0.001,
            spot_reconcile_missing_confirm_cycles=2,
        )
        client = DetailedBalanceClient({"NEAR": {"free": 0.0, "locked": 13.9, "total": 13.9}})

        pm.sync_with_exchange_balance(client, settings)

        open_position = pm.get_position("NEARUSDT")
        assert open_position is not None
        assert open_position[0].quantity == pytest.approx(13.9)
        assert pm.get_closed_positions(limit=10) == []

    def test_sync_keeps_position_when_balance_api_fails(self):
        pm = PositionManager()
        position = Position(
            symbol="NEARUSDT", side="BUY", quantity=13.9, entry_price=5.209,
            entry_time="2026-09-27T17:24:41Z", metadata={"trade_type": "spot"},
        )
        pm.add_position(position)
        settings = SimpleNamespace(
            token_categories={}, category_thresholds={}, use_bnb_for_commission=False,
            auto_bnb_for_commission=False, binance_commission_rate=0.001,
            spot_reconcile_missing_confirm_cycles=2,
        )
        client = DetailedBalanceClient(error=RuntimeError("temporary API timeout"))

        pm.sync_with_exchange_balance(client, settings)

        open_position = pm.get_position("NEARUSDT")
        assert open_position is not None
        assert open_position[0].quantity == pytest.approx(13.9)
        assert pm.get_closed_positions(limit=10) == []

    def test_sync_requires_repeated_valid_zero_balance_before_reconcile_close(self):
        pm = PositionManager()
        pm.add_position(Position(
            symbol="NEARUSDT", side="BUY", quantity=13.9, entry_price=5.209,
            entry_time="2026-09-27T17:24:41Z", metadata={"trade_type": "spot"},
        ))
        settings = SimpleNamespace(
            token_categories={}, category_thresholds={}, use_bnb_for_commission=False,
            auto_bnb_for_commission=False, binance_commission_rate=0.001,
            spot_reconcile_missing_confirm_cycles=2,
        )
        client = DetailedBalanceClient({}, trades=[{
            "time": int(datetime.now(timezone.utc).timestamp() * 1000),
            "isBuyer": False,
            "qty": "13.9",
        }])

        pm.sync_with_exchange_balance(client, settings)
        assert pm.get_position("NEARUSDT") is not None
        assert pm.get_closed_positions(limit=10) == []

        pm.sync_with_exchange_balance(client, settings)
        assert pm.get_position("NEARUSDT") is None
        closed = pm.get_closed_positions(limit=10)
        assert len(closed) == 1
        assert closed[0].close_reason == "reconcile_missing_balance"

    def test_sync_keeps_position_when_zero_spot_balance_has_no_sell_proof(self):
        pm = PositionManager()
        pm.add_position(Position(
            symbol="NEARUSDT", side="BUY", quantity=13.9, entry_price=5.209,
            entry_time="2026-09-27T17:24:41Z", metadata={"trade_type": "spot"},
        ))
        settings = SimpleNamespace(
            token_categories={}, category_thresholds={}, use_bnb_for_commission=False,
            auto_bnb_for_commission=False, binance_commission_rate=0.001,
            spot_reconcile_missing_confirm_cycles=2,
        )
        client = DetailedBalanceClient({})

        pm.sync_with_exchange_balance(client, settings)
        pm.sync_with_exchange_balance(client, settings)

        assert pm.get_position("NEARUSDT") is not None
        assert pm.get_position("NEARUSDT")[0].quantity == pytest.approx(13.9)
        assert pm.get_closed_positions(limit=10) == []

    def test_sync_imports_only_untracked_surplus_with_unknown_basis(self):
        pm = PositionManager()
        pm.add_position(Position(
            symbol="NEARUSDT", side="BUY", quantity=10.0, entry_price=5.0,
            entry_time="2026-09-27T17:24:41Z", metadata={"trade_type": "spot"},
        ))
        settings = SimpleNamespace(
            token_categories={}, category_thresholds={}, use_bnb_for_commission=False,
            auto_bnb_for_commission=False, binance_commission_rate=0.001,
            stop_loss_pct=2.0, take_profit_pct=4.0, spot_reconcile_missing_confirm_cycles=2,
        )
        client = DetailedBalanceClient({"NEAR": {"free": 10.0, "locked": 3.9, "total": 13.9}})

        pm.sync_with_exchange_balance(client, settings)

        positions = pm.get_position("NEARUSDT")
        assert len(positions) == 2
        imported = next(position for position in positions if position.quantity == pytest.approx(3.9))
        assert sum(position.quantity for position in positions) == pytest.approx(13.9)
        assert imported.metadata["entry_context_status"] == "unknown_reconciled"
        assert imported.metadata["cost_basis_status"] == "unknown"
        assert imported.metadata["entry_price_source"] == "balance_sync_mark_not_acquisition_cost"

        pm.sync_with_exchange_balance(client, settings)
        assert len(pm.get_position("NEARUSDT")) == 2

    def test_reloaded_position_keeps_persisted_identity_for_confirmed_fill_close(self):
        class ReloadingDb:
            def __init__(self):
                self.transactions = []
                self.row = {
                    "id": 347,
                    "position_id": "sui-position-stable-id",
                    "symbol": "SUIUSDT",
                    "side": "BUY",
                    "quantity": 34.0,
                    "entry_price": 1.1612,
                    "strategy_id": None,
                    "entry_time": "2026-09-29T00:44:14+00:00",
                    "stop_loss": 1.1231,
                    "take_profit": 1.2006,
                    "metadata": {"trade_type": "spot", "position_id": "sui-position-stable-id"},
                    "entry_commission": 0.03,
                    "pnl_prediction": None,
                    "indicators_at_entry": None,
                }

            def execute_query(self, sql, params=None):
                if "FROM trades_open" in sql and "COUNT(*)" not in sql:
                    return [dict(self.row)]
                return []

            def execute_transaction(self, statements):
                self.transactions.append(statements)
                return True

            def execute_update(self, sql, params=None):
                return 1

        db = ReloadingDb()
        pm = PositionManager()
        pm.db_client = db

        first = pm.get_all_positions()["SUIUSDT"][0]
        second = pm.get_all_positions()["SUIUSDT"][0]

        assert first.position_id == second.position_id == "sui-position-stable-id"
        assert second.db_id == 347
        closed = pm.close_position(
            "SUIUSDT",
            1.1228,
            "hard_risk_stop",
            position_id="sui-position-stable-id",
            exit_fill={
                "binance_confirmed": True,
                "order_id": "1121928740",
                "qty": 34.0,
                "avg_price": 1.1228,
                "commission_usdt": 0.03,
            },
        )

        assert closed is not None
        assert closed.exit_order_id == "1121928740"
        assert closed.exit_price == pytest.approx(1.1228)
        assert closed.metadata["pnl_accounting_mode"] == "binance_fill_verified"
        assert db.transactions


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
