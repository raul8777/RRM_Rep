"""
Integration tests for Position Manager
"""

import pytest
from position_manager import PositionManager, Position, ClosedPosition


class TestPositionManager:
    """Test Position Manager functionality."""
    
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
        assert pm.get_position("BTCUSDT").symbol == "BTCUSDT"
        assert pm.get_position("BTCUSDT").quantity == 0.5
    
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
        assert closed.pnl == 1000.0  # (52000 - 50000) * 0.5
        assert closed.pnl_percentage == 4.0  # (52000 - 50000) / 50000 * 100
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
        assert closed.pnl == 1000.0  # (50000 - 48000) * 0.5
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
    
    def test_get_position_summary(self):
        """Test getting position summary."""
        pm = PositionManager()
        
        pm.add_position(Position("BTCUSDT", "BUY", 0.5, 50000.0, "2024-01-01T00:00:00Z"))
        pm.add_position(Position("ETHUSDT", "BUY", 10.0, 3000.0, "2024-01-01T00:00:00Z"))
        
        summary = pm.get_position_summary()
        
        assert summary["open_positions_count"] == 2
        assert summary["total_value_usdt"] == 55000.0  # 0.5 * 50000 + 10 * 3000
        assert "BTCUSDT" in summary["positions"]
        assert "ETHUSDT" in summary["positions"]
    
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
