"""
Portfolio Manager Module

Manages intelligent capital allocation and pair selection for trading.
"""

import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from dataclasses import dataclass

from risk_calculator import RiskCalculator
from config import Settings

logger = logging.getLogger(__name__)


@dataclass
class PairRanking:
    """Represents a ranked trading pair."""
    symbol: str
    confidence_score: float
    signal_score: float
    technical_score: float
    news_score: float
    risk_score: float
    combined_score: float


@dataclass
class CapitalAllocation:
    """Represents capital allocation for a pair."""
    symbol: str
    capital_usdt: float
    percentage: float
    risk_adjusted: bool


class PortfolioManager:
    """Manages intelligent capital allocation and pair selection."""
    
    def __init__(self, binance_client, settings: Settings, position_manager=None):
        """
        Initialize Portfolio Manager.
        
        Args:
            binance_client: BinanceClient instance
            settings: Application settings
            position_manager: PositionManager instance (optional)
        """
        self.binance_client = binance_client
        self.settings = settings
        self.risk_calculator = RiskCalculator()
        self.position_manager = position_manager
        
        # Portfolio configuration
        self.allocation_pct = settings.portfolio_allocation_pct / 100.0
        self.min_confidence = settings.min_pair_confidence_score
        self.max_risk_per_pair = settings.max_risk_per_pair_pct / 100.0
        self.diversification_factor = settings.diversification_factor
        self.enable_closure = settings.enable_position_closure
        
        logger.info(
            f"Portfolio Manager initialized: allocation_pct={self.allocation_pct:.2%}, "
            f"min_confidence={self.min_confidence}, max_risk_per_pair={self.max_risk_per_pair:.2%}"
        )
    
    def get_total_balance(self) -> Dict[str, float]:
        """
        Get total balance from Binance account.
        
        Returns:
            Dictionary with balance information
        """
        try:
            balances = self.binance_client.get_account_balance()
            
            # Calculate total USDT value
            total_usdt = 0.0
            usdt_balance = float(balances.get("USDT", 0))
            total_usdt += usdt_balance
            
            # For simplicity, we'll just use USDT balance for now
            # In production, we should convert other assets to USDT value
            
            return {
                "usdt_available": usdt_balance,
                "total_usdt": total_usdt,
                "allocation_usdt": total_usdt * self.allocation_pct,
                "timestamp": datetime.utcnow().isoformat()
            }
        except Exception as e:
            logger.error(f"Error getting total balance: {e}")
            return {
                "usdt_available": 0.0,
                "total_usdt": 0.0,
                "allocation_usdt": 0.0,
                "timestamp": datetime.utcnow().isoformat()
            }
    
    def rank_pairs(
        self,
        signals: Dict[str, float],
        technical_scores: Dict[str, float],
        news_scores: Dict[str, float],
        risk_metrics: Dict[str, Dict[str, float]]
    ) -> List[PairRanking]:
        """
        Rank trading pairs using combined scoring.
        
        Args:
            signals: Dictionary of signal scores per symbol
            technical_scores: Dictionary of technical analysis scores per symbol
            news_scores: Dictionary of news sentiment scores per symbol
            risk_metrics: Dictionary of risk metrics per symbol
            
        Returns:
            List of ranked pairs (highest score first)
        """
        rankings = []
        
        # Get all unique symbols from all sources
        all_symbols = set(signals.keys()) | set(technical_scores.keys()) | set(news_scores.keys())
        
        for symbol in all_symbols:
            # Get scores (default to 0 if not available)
            signal_score = signals.get(symbol, 0.0)
            technical_score = technical_scores.get(symbol, 0.0)
            news_score = news_scores.get(symbol, 0.0)
            
            # Get risk score
            risk_data = risk_metrics.get(symbol, {})
            risk_score = risk_data.get("risk_score", 50.0)  # Default medium risk
            
            # Calculate combined score (weighted average)
            # Higher is better for signal/technical/news, lower is better for risk
            # We invert risk score so lower risk = higher contribution
            risk_contribution = (100 - risk_score) / 100.0
            
            combined_score = (
                signal_score * 0.4 +
                technical_score * 0.3 +
                news_score * 0.2 +
                risk_contribution * 0.1
            )
            
            # Only include if above minimum confidence
            if combined_score >= self.min_confidence:
                rankings.append(PairRanking(
                    symbol=symbol,
                    confidence_score=combined_score,
                    signal_score=signal_score,
                    technical_score=technical_score,
                    news_score=news_score,
                    risk_score=risk_score,
                    combined_score=combined_score
                ))
        
        # Sort by combined score (descending)
        rankings.sort(key=lambda x: x.combined_score, reverse=True)
        
        logger.info(f"Ranked {len(rankings)} pairs above minimum confidence {self.min_confidence}")
        
        return rankings
    
    def calculate_optimal_pair_count(self, ranked_pairs: List[PairRanking]) -> int:
        """
        Calculate optimal number of pairs to trade.
        
        Args:
            ranked_pairs: List of ranked pairs
            
        Returns:
            Optimal number of pairs
        """
        if not ranked_pairs:
            return 0
        
        # Dynamic calculation based on diversification factor and available capital
        # More capital = can handle more pairs
        balance = self.get_total_balance()
        allocation_usdt = balance["allocation_usdt"]
        
        # Base calculation: diversification factor * log of capital
        # This means more capital allows more diversification
        import math
        base_count = max(1, int(self.diversification_factor * math.log(max(allocation_usdt, 1000))))
        
        # Limit by number of available pairs above threshold
        max_pairs = len(ranked_pairs)
        
        # Also limit by settings.max_positions if available
        max_positions = getattr(self.settings, "max_positions", 10)
        
        optimal_count = min(base_count, max_pairs, max_positions)
        
        logger.info(f"Optimal pair count: {optimal_count} (base={base_count}, available={max_pairs}, max={max_positions})")
        
        return optimal_count
    
    def allocate_capital(
        self,
        ranked_pairs: List[PairRanking],
        total_capital: float
    ) -> List[CapitalAllocation]:
        """
        Allocate capital to selected pairs.
        
        Args:
            ranked_pairs: List of ranked pairs
            total_capital: Total capital to allocate
            
        Returns:
            List of capital allocations per pair
        """
        if not ranked_pairs or total_capital <= 0:
            return []
        
        # Calculate optimal number of pairs
        optimal_count = self.calculate_optimal_pair_count(ranked_pairs)
        selected_pairs = ranked_pairs[:optimal_count]
        
        allocations = []
        
        # Calculate weights based on combined scores
        total_score = sum(p.combined_score for p in selected_pairs)
        
        for pair in selected_pairs:
            # Weight based on score
            weight = pair.combined_score / total_score if total_score > 0 else 1.0 / len(selected_pairs)
            
            # Calculate capital allocation
            capital_usdt = total_capital * weight
            
            # Risk adjustment: reduce allocation if risk is high
            risk_adjustment = 1.0
            if pair.risk_score > 70:  # High risk
                risk_adjustment = 0.7
            elif pair.risk_score > 50:  # Medium risk
                risk_adjustment = 0.85
            
            capital_usdt *= risk_adjustment
            
            # Ensure allocation doesn't exceed max risk per pair
            max_capital_per_pair = total_capital * self.max_risk_per_pair
            capital_usdt = min(capital_usdt, max_capital_per_pair)
            
            percentage = (capital_usdt / total_capital) * 100 if total_capital > 0 else 0.0
            
            allocations.append(CapitalAllocation(
                symbol=pair.symbol,
                capital_usdt=capital_usdt,
                percentage=percentage,
                risk_adjusted=risk_adjustment < 1.0
            ))
        
        # Normalize to ensure total equals 100%
        total_allocated = sum(a.capital_usdt for a in allocations)
        if total_allocated > 0 and total_allocated != total_capital:
            scale_factor = total_capital / total_allocated
            for alloc in allocations:
                alloc.capital_usdt *= scale_factor
                alloc.percentage = (alloc.capital_usdt / total_capital) * 100
        
        logger.info(
            f"Allocated {total_capital:.2f} USDT to {len(allocations)} pairs: "
            f"{', '.join(f'{a.symbol}({a.percentage:.1f}%)' for a in allocations)}"
        )
        
        return allocations
    
    def decide_position_closures(
        self,
        current_positions: Dict[str, Dict],
        new_allocations: List[CapitalAllocation]
    ) -> List[str]:
        """
        Decide which positions to close for capital reallocation.
        
        Args:
            current_positions: Current open positions
            new_allocations: New capital allocations
            
        Returns:
            List of symbols to close
        """
        if not self.enable_closure:
            return []
        
        symbols_to_close = []
        
        # Get symbols in new allocations
        new_symbols = set(a.symbol for a in new_allocations)
        
        # Get symbols in current positions
        current_symbols = set(current_positions.keys())
        
        # Find symbols that are in current positions but not in new allocations
        # These should be closed to free up capital
        symbols_to_close = list(current_symbols - new_symbols)
        
        if symbols_to_close:
            logger.info(f"Decided to close positions for reallocation: {symbols_to_close}")
            
            # Close positions in Position Manager if available
            if self.position_manager:
                for symbol in symbols_to_close:
                    try:
                        # Get current price for closing
                        price = self.binance_client.get_symbol_price(symbol)
                        self.position_manager.close_position(symbol, price, "portfolio_rebalancing")
                    except Exception as e:
                        logger.error(f"Failed to close position {symbol} in Position Manager: {e}")
        
        return symbols_to_close
    
    def risk_adjusted_sizing(
        self,
        symbol: str,
        capital: float,
        risk_score: float
    ) -> float:
        """
        Calculate risk-adjusted position size.
        
        Args:
            symbol: Trading pair symbol
            capital: Allocated capital
            risk_score: Risk score (0-100)
            
        Returns:
            Risk-adjusted position size
        """
        # Base size is the allocated capital
        size = capital
        
        # Adjust based on risk
        if risk_score > 70:  # High risk
            size *= 0.7
        elif risk_score > 50:  # Medium risk
            size *= 0.85
        
        # Ensure minimum size
        min_size = getattr(self.settings, "default_trade_size", 10.0)
        size = max(size, min_size)
        
        return size
    
    def get_portfolio_summary(self) -> Dict:
        """
        Get summary of current portfolio state.
        
        Returns:
            Dictionary with portfolio summary
        """
        balance = self.get_total_balance()
        
        return {
            "total_balance_usdt": balance["total_usdt"],
            "available_usdt": balance["usdt_available"],
            "allocated_usdt": balance["allocation_usdt"],
            "allocation_percentage": self.allocation_pct * 100,
            "min_confidence_threshold": self.min_confidence,
            "max_risk_per_pair": self.max_risk_per_pair * 100,
            "diversification_factor": self.diversification_factor,
            "position_closure_enabled": self.enable_closure,
            "timestamp": datetime.utcnow().isoformat()
        }
