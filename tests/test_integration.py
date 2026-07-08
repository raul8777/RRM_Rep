"""
Integration tests for the complete system
"""

import pytest
from portfolio_manager import PortfolioManager
from position_manager import PositionManager
from orchestrator import Orchestrator, SystemState
from message_bus import message_bus, EVENT_SIGNAL, EVENT_ORDER


class TestIntegration:
    """Integration tests for system components."""
    
    def test_portfolio_manager_integration(self):
        """Test Portfolio Manager integration."""
        # This would require a mock BinanceClient
        # For now, we'll skip actual implementation
        pass
    
    def test_position_manager_integration(self):
        """Test Position Manager integration."""
        pm = PositionManager()
        
        # Test adding a position
        from position_manager import Position
        position = Position(
            symbol="BTCUSDT",
            side="BUY",
            quantity=0.5,
            entry_price=50000.0,
            entry_time="2024-01-01T00:00:00Z"
        )
        pm.add_position(position)
        
        assert pm.get_position("BTCUSDT") is not None
        
        # Test closing a position
        closed = pm.close_position("BTCUSDT", 52000.0, "manual")
        assert closed is not None
        assert closed.pnl > 0  # net PnL after commissions (gross=1000, net≈949)
    
    def test_orchestrator_integration(self):
        """Test Orchestrator integration."""
        orchestrator = Orchestrator()
        
        # Test state changes
        assert orchestrator.start(changed_by="test", reason="Test start")
        assert orchestrator.state == SystemState.RUNNING
        
        assert orchestrator.pause(changed_by="test", reason="Test pause")
        assert orchestrator.state == SystemState.PAUSED
        
        assert orchestrator.stop(changed_by="test", reason="Test stop")
        assert orchestrator.state == SystemState.STOPPED
    
    def test_message_bus_integration(self):
        """Test Message Bus integration."""
        messages_received = []
        
        def callback(message):
            messages_received.append(message)
        
        message_bus.subscribe(EVENT_SIGNAL, callback)
        
        # Publish a signal
        message_bus.publish(EVENT_SIGNAL, {"symbol": "BTCUSDT"})
        
        assert len(messages_received) == 1
        assert messages_received[0]["symbol"] == "BTCUSDT"
        
        # Clean up
        message_bus._handlers.clear()
    
    def test_system_health_check(self):
        """Test system health check."""
        orchestrator = Orchestrator()
        
        health = orchestrator.get_health_status()
        
        assert "system_state" in health
        assert health["system_state"] == SystemState.STOPPED.value
        assert "trade_cycle_running" in health
        assert "analysis_cycle_running" in health


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
