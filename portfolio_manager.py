"""
Portfolio Manager Module

Manages intelligent capital allocation and pair selection for trading.
"""

import logging
import time
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from dataclasses import dataclass

from risk_calculator import RiskCalculator
from config import Settings
from pnl_predictor import PnLPredictor

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
        self.pnl_predictor = PnLPredictor()
        
        # Portfolio configuration
        self.allocation_pct = settings.portfolio_allocation_pct / 100.0
        self.min_confidence = settings.min_pair_confidence_score
        self.max_risk_per_pair = settings.max_risk_per_pair_pct / 100.0
        self.diversification_factor = settings.diversification_factor
        self.enable_closure = settings.enable_position_closure
        
        # Prediction-based closure configuration
        self.min_pnl_improvement_threshold = getattr(settings, 'min_pnl_improvement_threshold', 0.5)
        self.min_success_probability_improvement = getattr(settings, 'min_success_probability_improvement', 0.2)
        self.enable_prediction_based_closing = getattr(settings, 'enable_prediction_based_closing', True)

        # Balance cache: (result_dict, monotonic_timestamp)
        self._balance_cache: Optional[tuple] = None
        self._balance_cache_ttl = 30.0  # seconds
        
        logger.info(
            f"Portfolio Manager initialized: allocation_pct={self.allocation_pct:.2%}, "
            f"min_confidence={self.min_confidence}, max_risk_per_pair={self.max_risk_per_pair:.2%}"
        )
    
    def get_total_balance(self, use_cache: bool = True) -> Dict[str, float]:
        """
        Get total balance from Binance account (cached 30 s by default).

        Args:
            use_cache: Set False to force a fresh Binance API call.

        Returns:
            Dictionary with balance information
        """
        now_mono = time.monotonic()
        if use_cache and self._balance_cache is not None:
            cached_result, cached_ts = self._balance_cache
            if now_mono - cached_ts < self._balance_cache_ttl:
                return cached_result

        try:
            balances = self.binance_client.get_account_balance()
            
            # Calculate total USDT value including all assets
            total_usdt = 0.0
            usdt_balance = float(balances.get("USDT", 0))
            total_usdt += usdt_balance
            
            # Convert other assets to USDT value (price cache in BinanceClient prevents extra calls)
            for asset, amount in balances.items():
                if asset.upper() == "USDT" or amount <= 0:
                    continue
                try:
                    symbol = f"{asset.upper()}USDT"
                    price = self.binance_client.get_symbol_price(symbol)
                    total_usdt += amount * price
                except Exception:
                    pass
            
            result = {
                "usdt_available": usdt_balance,
                "total_usdt": total_usdt,
                "allocation_usdt": total_usdt * self.allocation_pct,
                "timestamp": datetime.utcnow().isoformat()
            }
            self._balance_cache = (result, now_mono)
            return result
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
    
    def get_position_predictions(self, current_positions: Dict[str, Dict]) -> Dict[str, Dict]:
        """
        Get PnL predictions for current open positions.
        
        Args:
            current_positions: Current open positions (symbol -> position data)
            
        Returns:
            Dictionary mapping symbol to prediction data
        """
        predictions = {}
        
        if not self.position_manager:
            logger.warning("Position Manager not available, cannot get predictions")
            return predictions
        
        try:
            # Import AutonomousSignalGenerator for technical indicators
            from autonomous_signals import AutonomousSignalGenerator
            signal_gen = AutonomousSignalGenerator(self.binance_client)
            
            for symbol, position_data in current_positions.items():
                try:
                    # Get current price
                    current_price = self.binance_client.get_symbol_price(symbol)
                    
                    # Get technical indicators
                    signal_data = signal_gen.analyze_symbol(symbol, interval='1h')
                    indicators = signal_data.get('indicators', {})
                    
                    # Format indicators for predictor
                    formatted_indicators = {
                        'rsi': {'value': indicators.get('rsi', 50)},
                        'macd': {
                            'macd': indicators.get('macd', 0),
                            'signal': indicators.get('signal', 0),
                            'histogram': indicators.get('histogram', 0)
                        },
                        'sma': {'value': indicators.get('middle_band', current_price)},
                        'ema': {'value': indicators.get('middle_band', current_price)},
                        'bollinger_bands': {
                            'upper': indicators.get('upper_band', current_price * 1.02),
                            'middle': indicators.get('middle_band', current_price),
                            'lower': indicators.get('lower_band', current_price * 0.98)
                        }
                    }
                    
                    # Calculate prediction
                    cycle_duration = getattr(self.settings, 'trade_cycle_minutes', 5)
                    position_size = position_data.get('quantity', 0) * current_price
                    
                    prediction = self.pnl_predictor.predict_pnl(
                        symbol=symbol,
                        current_price=current_price,
                        indicators=formatted_indicators,
                        cycle_duration_minutes=cycle_duration,
                        position_size_usdt=position_size
                    )
                    
                    predictions[symbol] = prediction
                    logger.debug(f"Prediction for {symbol}: {prediction['expected_pnl']:.2f} USDT")
                    
                except Exception as e:
                    logger.error(f"Error getting prediction for {symbol}: {e}")
                    continue
                    
        except Exception as e:
            logger.error(f"Error in get_position_predictions: {e}")
        
        return predictions
    
    def compare_predictions(
        self,
        current_predictions: Dict[str, Dict],
        new_predictions: Dict[str, Dict]
    ) -> List[Tuple[str, str, float]]:
        """
        Compare current position predictions with new opportunity predictions.
        
        Args:
            current_predictions: Predictions for current positions
            new_predictions: Predictions for new opportunities
            
        Returns:
            List of tuples (current_symbol, new_symbol, improvement_score) sorted by improvement
        """
        comparisons = []
        
        for current_symbol, current_pred in current_predictions.items():
            for new_symbol, new_pred in new_predictions.items():
                # Skip if same symbol
                if current_symbol == new_symbol:
                    continue
                
                # Calculate improvement score
                current_pnl = current_pred.get('expected_pnl', 0)
                new_pnl = new_pred.get('expected_pnl', 0)
                
                current_prob = current_pred.get('success_probability', 0.5)
                new_prob = new_pred.get('success_probability', 0.5)
                
                current_risk = current_pred.get('risk_metrics', {}).get('percentage_risk', 10.0)
                new_risk = new_pred.get('risk_metrics', {}).get('percentage_risk', 10.0)
                
                # Calculate improvement factors
                pnl_improvement = (new_pnl - current_pnl) / abs(current_pnl) if current_pnl != 0 else 0
                prob_improvement = new_prob - current_prob
                risk_improvement = (current_risk - new_risk) / current_risk if current_risk > 0 else 0
                
                # Combined improvement score (weighted)
                improvement_score = (
                    pnl_improvement * 0.5 +
                    prob_improvement * 0.3 +
                    risk_improvement * 0.2
                )
                
                # Only include if improvement is positive
                if improvement_score > 0:
                    comparisons.append((current_symbol, new_symbol, improvement_score))
        
        # Sort by improvement score (descending)
        comparisons.sort(key=lambda x: x[2], reverse=True)
        
        return comparisons
    
    def decide_position_closures(
        self,
        current_positions: Dict[str, Dict],
        new_allocations: List[CapitalAllocation],
        new_predictions: Optional[Dict[str, Dict]] = None
    ) -> List[str]:
        """
        Decide which positions to close for capital reallocation.
        
        Args:
            current_positions: Current open positions
            new_allocations: New capital allocations
            new_predictions: Optional predictions for new opportunities
            
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
        
        # If prediction-based closing is enabled and we have new predictions
        if self.enable_prediction_based_closing and new_predictions:
            try:
                # Get predictions for current positions
                current_predictions = self.get_position_predictions(current_positions)
                
                if current_predictions:
                    # Compare predictions
                    comparisons = self.compare_predictions(current_predictions, new_predictions)
                    
                    # Decide which positions to close based on predictions
                    for current_symbol, new_symbol, improvement_score in comparisons:
                        # Check if improvement meets threshold
                        if improvement_score >= self.min_pnl_improvement_threshold:
                            # Check if new symbol is in new allocations
                            if new_symbol in new_symbols:
                                symbols_to_close.append(current_symbol)
                                logger.info(
                                    f"Closing {current_symbol} for {new_symbol} (improvement: {improvement_score:.2f})"
                                )
                                break  # Only close each position once
                    
                    if symbols_to_close:
                        logger.info(f"Prediction-based closures: {symbols_to_close}")
            except Exception as e:
                logger.error(f"Error in prediction-based closure: {e}")
        
        # Fallback: close positions not in new allocations
        if not symbols_to_close:
            symbols_to_close = list(current_symbols - new_symbols)
            if symbols_to_close:
                logger.info(f"Closing positions not in new allocations: {symbols_to_close}")
        
        # Close positions in Position Manager if available
        if symbols_to_close and self.position_manager:
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
