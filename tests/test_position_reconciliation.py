"""Regression tests for SPOT/FUTURES reconciliation parity (Fase 2.7)."""

import pytest
from unittest.mock import MagicMock

from position_manager import PositionManager, Position
from binance_truth_guard import evaluate_spot_parity, evaluate_futures_parity
from futures_client import FuturesPosition


class TestSpotReconciliation:
    def _make_pm(self, symbols_quantities):
        pm = PositionManager()
        for sym, qty in symbols_quantities.items():
            pm.add_position(
                Position(
                    symbol=sym,
                    side="BUY",
                    quantity=qty,
                    entry_price=100.0,
                    entry_time="2026-01-01T00:00:00Z",
                )
            )
        return pm

    def _make_binance_client(self, balances):
        client = MagicMock()
        client.get_account_balance.return_value = balances
        return client

    def test_spot_parity_aligned(self):
        pm = self._make_pm({"BTCUSDT": 0.5, "ETHUSDT": 2.0})
        client = self._make_binance_client({"BTC": 0.5, "ETH": 2.0})
        result = evaluate_spot_parity(pm, client)
        assert result["severity"] == "ok"
        assert result["mismatch_count"] == 0
        assert result["ok"] is True

    def test_spot_ghost_detected(self):
        pm = self._make_pm({"BTCUSDT": 0.5})
        client = self._make_binance_client({"ETH": 2.0})
        result = evaluate_spot_parity(pm, client)
        assert result["severity"] == "high"
        assert result["ok"] is False
        assert any(m["code"] == "SPOT_GHOST_IN_DB" for m in result["mismatches"])

    def test_spot_orphan_detected(self):
        pm = self._make_pm({"BTCUSDT": 0.5})
        client = self._make_binance_client({"BTC": 0.5, "ETH": 2.0})
        result = evaluate_spot_parity(pm, client)
        assert result["severity"] == "warning"
        assert result["ok"] is False
        assert any(m["code"] == "SPOT_ORPHAN_IN_BINANCE" for m in result["mismatches"])

    def test_spot_qty_drift_detected(self):
        pm = self._make_pm({"BTCUSDT": 0.5})
        client = self._make_binance_client({"BTC": 0.55})
        result = evaluate_spot_parity(pm, client)
        assert result["severity"] == "warning"
        assert result["ok"] is False
        assert any(m["code"] == "SPOT_QTY_DRIFT" for m in result["mismatches"])


class TestFuturesReconciliation:
    def _make_futures_client(self, runtime_positions, binance_positions):
        client = MagicMock()
        client._open_positions = {}
        for pos in runtime_positions:
            key = f"{pos.symbol}_{pos.side}"
            client._open_positions[key] = pos
        client.get_positions.return_value = binance_positions
        return client

    def test_futures_parity_aligned(self):
        pos = FuturesPosition(
            symbol="BTCUSDT",
            side="SHORT",
            qty=0.1,
            entry_price=50000.0,
            sl_price=51000.0,
            tp_price=48000.0,
            order_id="o1",
            sl_order_id="sl1",
            tp_order_id="tp1",
        )
        bn = [{"symbol": "BTCUSDT", "positionAmt": "-0.1", "entryPrice": "50000.0"}]
        client = self._make_futures_client([pos], bn)
        result = evaluate_futures_parity(client)
        assert result["severity"] == "ok"
        assert result["mismatch_count"] == 0
        assert result["ok"] is True

    def test_futures_parity_detects_runtime_missing(self):
        bn = [{"symbol": "BTCUSDT", "positionAmt": "-0.1", "entryPrice": "50000.0"}]
        client = self._make_futures_client([], bn)
        result = evaluate_futures_parity(client)
        assert result["severity"] == "critical"
        assert result["ok"] is False
        assert any(m["code"] == "BINANCE_MISMATCH_RUNTIME_MISSING" for m in result["mismatches"])

    def test_futures_parity_detects_ghost_runtime(self):
        pos = FuturesPosition(
            symbol="BTCUSDT",
            side="SHORT",
            qty=0.1,
            entry_price=50000.0,
            sl_price=51000.0,
            tp_price=48000.0,
            order_id="o1",
            sl_order_id="sl1",
            tp_order_id="tp1",
        )
        client = self._make_futures_client([pos], [])
        result = evaluate_futures_parity(client)
        assert result["severity"] == "warning"
        assert result["ok"] is False
        assert any(m["code"] == "BINANCE_MISMATCH_GHOST_RUNTIME" for m in result["mismatches"])

    def test_futures_parity_detects_qty_drift(self):
        pos = FuturesPosition(
            symbol="BTCUSDT",
            side="SHORT",
            qty=0.1,
            entry_price=50000.0,
            sl_price=51000.0,
            tp_price=48000.0,
            order_id="o1",
            sl_order_id="sl1",
            tp_order_id="tp1",
        )
        bn = [{"symbol": "BTCUSDT", "positionAmt": "-0.2", "entryPrice": "50000.0"}]
        client = self._make_futures_client([pos], bn)
        result = evaluate_futures_parity(client)
        assert result["severity"] == "critical"
        assert result["ok"] is False
        assert any(m["code"] == "BINANCE_MISMATCH_QTY" for m in result["mismatches"])

    def test_futures_parity_detects_entry_price_drift(self):
        pos = FuturesPosition(
            symbol="BTCUSDT",
            side="SHORT",
            qty=0.1,
            entry_price=50000.0,
            sl_price=51000.0,
            tp_price=48000.0,
            order_id="o1",
            sl_order_id="sl1",
            tp_order_id="tp1",
        )
        bn = [{"symbol": "BTCUSDT", "positionAmt": "-0.1", "entryPrice": "51000.0"}]
        client = self._make_futures_client([pos], bn)
        result = evaluate_futures_parity(client)
        assert result["severity"] == "warning"
        assert result["ok"] is False
        assert any(m["code"] == "BINANCE_MISMATCH_ENTRY_PRICE" for m in result["mismatches"])
