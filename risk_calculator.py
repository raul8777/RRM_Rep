"""
Risk Calculator Module

Calculates risk metrics for trading pairs including volatility, VaR, and drawdown.
"""

import numpy as np
from typing import Dict, List, Optional
from datetime import datetime, timedelta


class RiskCalculator:
    """Calculates risk metrics for trading pairs."""
    
    def __init__(self):
        self.cache: Dict[str, Dict] = {}
    
    def calculate_volatility(
        self, 
        symbol: str, 
        prices: List[float], 
        period: int = 24
    ) -> float:
        """
        Calculate historical volatility for a symbol.
        
        Args:
            symbol: Trading pair symbol (e.g., "BTCUSDT")
            prices: List of historical prices
            period: Period in hours for volatility calculation
            
        Returns:
            Volatility as a percentage (0-100)
        """
        if len(prices) < 2:
            return 0.0
        
        # Calculate log returns
        returns = np.diff(np.log(prices))
        
        # Calculate standard deviation of returns
        volatility = np.std(returns) * np.sqrt(period) * 100
        
        return float(volatility)
    
    def calculate_var(
        self,
        symbol: str,
        prices: List[float],
        confidence_level: float = 0.95
    ) -> float:
        """
        Calculate Value at Risk (VaR) for a symbol.
        
        Args:
            symbol: Trading pair symbol
            prices: List of historical prices
            confidence_level: Confidence level for VaR (default 0.95)
            
        Returns:
            VaR as a percentage of current price
        """
        if len(prices) < 2:
            return 0.0
        
        # Calculate log returns
        returns = np.diff(np.log(prices))
        
        # Calculate VaR using historical method
        var_percentile = (1 - confidence_level) * 100
        var = np.percentile(returns, var_percentile)
        
        # Convert to percentage
        var_pct = abs(var) * 100
        
        return float(var_pct)
    
    def calculate_max_drawdown(
        self,
        symbol: str,
        prices: List[float]
    ) -> float:
        """
        Calculate maximum drawdown for a symbol.
        
        Args:
            symbol: Trading pair symbol
            prices: List of historical prices
            
        Returns:
            Maximum drawdown as a percentage (0-100)
        """
        if len(prices) < 2:
            return 0.0
        
        prices_array = np.array(prices)
        
        # Calculate cumulative maximum
        cummax = np.maximum.accumulate(prices_array)
        
        # Calculate drawdown
        drawdown = (prices_array - cummax) / cummax * 100
        
        # Get maximum drawdown
        max_dd = np.min(drawdown)
        
        return float(abs(max_dd))
    
    def calculate_risk_score(
        self,
        symbol: str,
        volatility: float,
        var: float,
        max_drawdown: float
    ) -> float:
        """
        Calculate combined risk score for a symbol.
        
        Args:
            symbol: Trading pair symbol
            volatility: Volatility percentage
            var: Value at Risk percentage
            max_drawdown: Maximum drawdown percentage
            
        Returns:
            Risk score (0-100, higher = riskier)
        """
        # Normalize each metric to 0-100 scale
        # Volatility: 0-50% -> 0-100 score
        vol_score = min(volatility * 2, 100)
        
        # VaR: 0-20% -> 0-100 score
        var_score = min(var * 5, 100)
        
        # Max Drawdown: 0-30% -> 0-100 score
        dd_score = min(max_drawdown * 3.33, 100)
        
        # Combined score (weighted average)
        risk_score = (vol_score * 0.4 + var_score * 0.3 + dd_score * 0.3)
        
        return float(risk_score)
    
    def get_risk_metrics(
        self,
        symbol: str,
        prices: List[float],
        period: int = 24,
        confidence_level: float = 0.95
    ) -> Dict[str, float]:
        """
        Get all risk metrics for a symbol.
        
        Args:
            symbol: Trading pair symbol
            prices: List of historical prices
            period: Period for volatility calculation
            confidence_level: Confidence level for VaR
            
        Returns:
            Dictionary with all risk metrics
        """
        volatility = self.calculate_volatility(symbol, prices, period)
        var = self.calculate_var(symbol, prices, confidence_level)
        max_dd = self.calculate_max_drawdown(symbol, prices)
        risk_score = self.calculate_risk_score(symbol, volatility, var, max_dd)
        
        return {
            "symbol": symbol,
            "volatility": volatility,
            "var": var,
            "max_drawdown": max_dd,
            "risk_score": risk_score,
            "timestamp": datetime.utcnow().isoformat()
        }
