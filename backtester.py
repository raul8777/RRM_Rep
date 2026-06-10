"""
Backtester Module

Validates trading strategies using historical data and simulates Portfolio Manager capital management.
"""

import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from dataclasses import dataclass
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    """Represents the result of a backtest."""
    strategy_name: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    total_pnl_percentage: float
    max_drawdown: float
    sharpe_ratio: float
    final_capital: float
    initial_capital: float
    start_date: str
    end_date: str
    metadata: Optional[Dict] = None


class Backtester:
    """Backtests trading strategies with Portfolio Manager capital management."""
    
    def __init__(self, initial_capital: float = 10000.0):
        """
        Initialize Backtester.
        
        Args:
            initial_capital: Initial capital for backtesting
        """
        self.initial_capital = initial_capital
        logger.info(f"Backtester initialized with initial capital: ${initial_capital}")
    
    def run_backtest(
        self,
        historical_data: List[Dict],
        signals: List[Dict],
        portfolio_manager_config: Dict = None
    ) -> BacktestResult:
        """
        Run a backtest on historical data.
        
        Args:
            historical_data: List of historical price data points
            signals: List of trading signals
            portfolio_manager_config: Configuration for Portfolio Manager simulation
            
        Returns:
            BacktestResult with performance metrics
        """
        # Convert to DataFrame for easier manipulation
        df = pd.DataFrame(historical_data)
        df = df.sort_values('timestamp')
        
        # Initialize tracking variables
        capital = self.initial_capital
        positions = {}
        trades = []
        equity_curve = [capital]
        max_equity = capital
        max_drawdown = 0.0
        
        # Simulate Portfolio Manager capital allocation
        if portfolio_manager_config:
            allocation_pct = portfolio_manager_config.get('allocation_pct', 1.0)
            max_risk_per_pair = portfolio_manager_config.get('max_risk_per_pair', 0.1)
        else:
            allocation_pct = 1.0
            max_risk_per_pair = 0.1
        
        # Process signals and historical data
        for _, row in df.iterrows():
            timestamp = row['timestamp']
            symbol = row['symbol']
            price = row['close']
            
            # Check for signals at this timestamp
            relevant_signals = [
                s for s in signals 
                if s.get('symbol') == symbol and self._signal_at_time(s, timestamp)
            ]
            
            # Process buy signals
            for signal in relevant_signals:
                if signal.get('action') == 'BUY':
                    # Calculate position size using Portfolio Manager logic
                    available_capital = capital * allocation_pct
                    position_size = min(available_capital * max_risk_per_pair, capital * 0.2)
                    quantity = position_size / price
                    
                    if quantity > 0:
                        positions[symbol] = {
                            'quantity': quantity,
                            'entry_price': price,
                            'entry_time': timestamp
                        }
                        capital -= position_size
                
                elif signal.get('action') == 'SELL' and symbol in positions:
                    # Close position
                    position = positions[symbol]
                    entry_price = position['entry_price']
                    quantity = position['quantity']
                    
                    pnl = (price - entry_price) * quantity
                    pnl_percentage = ((price - entry_price) / entry_price) * 100
                    
                    capital += quantity * price
                    trades.append({
                        'symbol': symbol,
                        'entry_price': entry_price,
                        'exit_price': price,
                        'quantity': quantity,
                        'pnl': pnl,
                        'pnl_percentage': pnl_percentage,
                        'entry_time': position['entry_time'],
                        'exit_time': timestamp
                    })
                    
                    del positions[symbol]
            
            # Update equity curve
            current_equity = capital
            for symbol, pos in positions.items():
                # Simplified: use current price for unrealized PnL
                current_equity += pos['quantity'] * price
            
            equity_curve.append(current_equity)
            
            # Track max drawdown
            if current_equity > max_equity:
                max_equity = current_equity
            
            drawdown = (max_equity - current_equity) / max_equity
            if drawdown > max_drawdown:
                max_drawdown = drawdown
        
        # Calculate metrics
        winning_trades = sum(1 for t in trades if t['pnl'] > 0)
        losing_trades = sum(1 for t in trades if t['pnl'] <= 0)
        total_trades = len(trades)
        win_rate = winning_trades / total_trades if total_trades > 0 else 0.0
        
        total_pnl = sum(t['pnl'] for t in trades)
        total_pnl_percentage = (total_pnl / self.initial_capital) * 100 if self.initial_capital > 0 else 0.0
        
        # Calculate Sharpe ratio (simplified)
        if len(equity_curve) > 1:
            returns = pd.Series(equity_curve).pct_change().dropna()
            sharpe_ratio = returns.mean() / returns.std() if returns.std() > 0 else 0.0
        else:
            sharpe_ratio = 0.0
        
        # Get date range
        start_date = df['timestamp'].min() if not df.empty else datetime.now().isoformat()
        end_date = df['timestamp'].max() if not df.empty else datetime.now().isoformat()
        
        return BacktestResult(
            strategy_name="backtest_strategy",
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=win_rate,
            total_pnl=total_pnl,
            total_pnl_percentage=total_pnl_percentage,
            max_drawdown=max_drawdown * 100,  # Convert to percentage
            sharpe_ratio=sharpe_ratio,
            final_capital=capital,
            initial_capital=self.initial_capital,
            start_date=start_date,
            end_date=end_date,
            metadata={
                'trades': trades,
                'equity_curve': equity_curve,
                'portfolio_config': portfolio_manager_config
            }
        )
    
    def _signal_at_time(self, signal: Dict, timestamp: datetime) -> bool:
        """
        Check if a signal is relevant at a given timestamp.
        
        Args:
            signal: Trading signal
            timestamp: Timestamp to check
            
        Returns:
            True if signal is relevant at this timestamp
        """
        signal_time = signal.get('timestamp')
        if not signal_time:
            return False
        
        # Simplified: check if signal is within 1 hour of timestamp
        if isinstance(signal_time, str):
            signal_time = datetime.fromisoformat(signal_time)
        
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)
        
        time_diff = abs((timestamp - signal_time).total_seconds())
        return time_diff <= 3600  # 1 hour window
    
    def compare_strategies(
        self,
        historical_data: List[Dict],
        strategies: Dict[str, List[Dict]],
        portfolio_manager_config: Dict = None
    ) -> Dict[str, BacktestResult]:
        """
        Compare multiple strategies using the same historical data.
        
        Args:
            historical_data: Historical price data
            strategies: Dictionary of strategy_name -> signals
            portfolio_manager_config: Portfolio Manager configuration
            
        Returns:
            Dictionary of strategy_name -> BacktestResult
        """
        results = {}
        
        for strategy_name, signals in strategies.items():
            logger.info(f"Backtesting strategy: {strategy_name}")
            result = self.run_backtest(historical_data, signals, portfolio_manager_config)
            results[strategy_name] = result
        
        return results
    
    def validate_portfolio_manager(
        self,
        historical_data: List[Dict],
        portfolio_configs: List[Dict]
    ) -> Dict[str, BacktestResult]:
        """
        Validate different Portfolio Manager configurations.
        
        Args:
            historical_data: Historical price data
            portfolio_configs: List of Portfolio Manager configurations to test
            
        Returns:
            Dictionary of config_name -> BacktestResult
        """
        results = {}
        
        for i, config in enumerate(portfolio_configs):
            config_name = f"config_{i}_{config.get('allocation_pct', 1.0)}"
            logger.info(f"Validating Portfolio Manager config: {config_name}")
            
            # Use a simple signal strategy for validation
            signals = self._generate_simple_signals(historical_data)
            
            result = self.run_backtest(historical_data, signals, config)
            results[config_name] = result
        
        return results
    
    def simulate_capital_management(
        self,
        historical_data: List[Dict],
        signals: List[Dict],
        portfolio_manager
    ) -> BacktestResult:
        """
        Simulate Portfolio Manager capital management on historical data.
        
        Args:
            historical_data: Historical price data
            signals: Trading signals
            portfolio_manager: Portfolio Manager instance
            
        Returns:
            BacktestResult with Portfolio Manager capital management
        """
        # Extract Portfolio Manager configuration
        config = {
            'allocation_pct': getattr(portfolio_manager, 'allocation_pct', 1.0),
            'max_risk_per_pair': getattr(portfolio_manager, 'max_risk_per_pair', 0.1),
            'diversification_factor': getattr(portfolio_manager, 'diversification_factor', 1.0),
            'min_confidence': getattr(portfolio_manager, 'min_confidence', 50.0)
        }
        
        logger.info(f"Simulating Portfolio Manager capital management with config: {config}")
        
        result = self.run_backtest(historical_data, signals, config)
        result.strategy_name = "portfolio_manager_simulation"
        
        return result
    
    def _generate_simple_signals(self, historical_data: List[Dict]) -> List[Dict]:
        """
        Generate simple buy/sell signals for testing.
        
        Args:
            historical_data: Historical price data
            
        Returns:
            List of simple trading signals
        """
        signals = []
        df = pd.DataFrame(historical_data)
        df = df.sort_values('timestamp')
        
        # Simple moving average crossover strategy
        if len(df) >= 20:
            df['ma_short'] = df['close'].rolling(window=5).mean()
            df['ma_long'] = df['close'].rolling(window=20).mean()
            
            for _, row in df.iterrows():
                if pd.notna(row['ma_short']) and pd.notna(row['ma_long']):
                    if row['ma_short'] > row['ma_long']:
                        signals.append({
                            'symbol': row['symbol'],
                            'action': 'BUY',
                            'timestamp': row['timestamp'],
                            'confidence': 0.7
                        })
                    elif row['ma_short'] < row['ma_long']:
                        signals.append({
                            'symbol': row['symbol'],
                            'action': 'SELL',
                            'timestamp': row['timestamp'],
                            'confidence': 0.7
                        })
        
        return signals
