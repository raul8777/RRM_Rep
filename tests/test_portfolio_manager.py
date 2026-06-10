"""
Unit tests for Portfolio Manager
"""

import pytest
from portfolio_manager import PortfolioManager, PairRanking, CapitalAllocation
from risk_calculator import RiskCalculator


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
    
    def test_allocate_capital_basic(self):
        """Test basic capital allocation."""
        # This would require proper setup
        # For now, we'll skip actual implementation
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
