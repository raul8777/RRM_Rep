"""
Backtester Module

Validates trading strategies using historical data and simulates Portfolio Manager
capital management.  Enhanced with:
- fetch_klines_from_binance(): pull real OHLCV from Binance
- run_backtest_with_params(): walk-forward simulation using TechnicalIndicators
- Additional metrics: Sortino ratio, Profit Factor, avg hold time
- persist_result(): store backtest result in backtest_results table
"""

import json
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from dataclasses import dataclass, field
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class PredictionAnalysis:
    """Represents the analysis of PnL prediction accuracy."""
    total_predictions: int
    accurate_predictions: int
    prediction_accuracy: float
    avg_prediction_error: float
    prediction_bias: float
    signal_quality_score: float
    metadata: Optional[Dict] = None


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
    sortino_ratio: float = 0.0
    profit_factor: float = 0.0
    avg_hold_bars: float = 0.0
    params: Optional[Dict] = None
    metadata: Optional[Dict] = None


class Backtester:
    """Backtests trading strategies with Portfolio Manager capital management."""

    def __init__(self, initial_capital: float = 10000.0, db_client=None):
        self.initial_capital = initial_capital
        self.db = db_client
        logger.info(f"Backtester initialized with initial capital: ${initial_capital}")

    # ── Real kline fetch ──────────────────────────────────────────────────────

    def fetch_klines_from_binance(
        self,
        symbol: str,
        interval: str = '1h',
        days_back: int = 30,
        client=None,
    ) -> List[Dict]:
        """
        Download OHLCV klines directly from Binance.

        Returns list of dicts with keys: open_time, open, high, low, close, volume.
        """
        if client is None:
            try:
                from init_components import get_components
                client = get_components().binance_client
            except Exception as e:
                raise RuntimeError(f"No Binance client available: {e}")

        limit = min(1000, days_back * 24)  # rough estimate for 1h bars
        raw = client.client.get_klines(symbol=symbol, interval=interval, limit=limit)
        klines = []
        for k in raw:
            klines.append({
                'open_time':  datetime.fromtimestamp(k[0] / 1000),
                'open':       float(k[1]),
                'high':       float(k[2]),
                'low':        float(k[3]),
                'close':      float(k[4]),
                'volume':     float(k[5]),
                'close_time': datetime.fromtimestamp(k[6] / 1000),
                'symbol':     symbol,
            })
        logger.info(f"Fetched {len(klines)} klines for {symbol} ({interval}, {days_back}d)")
        return klines

    # ── Walk-forward backtest with configurable params ────────────────────────

    def run_backtest_with_params(
        self,
        klines: List[Dict],
        params: Dict,
        symbol: str = 'UNKNOWN',
        commission_rate: float = 0.001,
    ) -> 'BacktestResult':
        """
        Walk-forward simulation that re-runs TechnicalIndicators on every bar
        using the supplied *params* (rsi_period, macd_*, bb_*, etc.).

        This is the core method used by ParameterOptimizer.
        """
        from technical_indicators import TechnicalIndicators

        closes  = [k['close']  for k in klines]
        highs   = [k['high']   for k in klines]
        lows    = [k['low']    for k in klines]
        volumes = [k['volume'] for k in klines]

        rsi_period   = params.get('rsi_period', 14)
        macd_fast    = params.get('macd_fast', 12)
        macd_slow    = params.get('macd_slow', 26)
        macd_signal  = params.get('macd_signal', 9)
        bb_period    = params.get('bb_period', 20)
        bb_std       = params.get('bb_std', 2.0)
        threshold    = params.get('buy_score_threshold', 2)
        ema_fast     = params.get('ema_fast_period', 9)
        ema_slow     = params.get('ema_slow_period', 21)

        warmup = max(rsi_period + 1, macd_slow + macd_signal, bb_period, ema_slow) + 5

        capital   = self.initial_capital
        position  = None   # dict or None
        trades    = []
        equity    = [capital]
        max_eq    = capital
        max_dd    = 0.0

        for i in range(warmup, len(closes)):
            window_c = closes[:i + 1]
            window_h = highs[:i + 1]
            window_l = lows[:i + 1]
            window_v = volumes[:i + 1]

            rsi_vals   = TechnicalIndicators.rsi(window_c, rsi_period)
            macd_line, sig_line, histo = TechnicalIndicators.macd(
                window_c, macd_fast, macd_slow, macd_signal)
            upper, middle, lower = TechnicalIndicators.bollinger_bands(
                window_c, bb_period, bb_std)
            ema_f = TechnicalIndicators.ema(window_c, ema_fast)
            ema_s = TechnicalIndicators.ema(window_c, ema_slow)

            if not (rsi_vals and macd_line and upper and ema_f and ema_s):
                continue

            rsi  = rsi_vals[-1]
            macd = macd_line[-1]
            sig  = sig_line[-1]
            hist = histo[-1]
            up   = upper[-1]
            mid  = middle[-1]
            lo   = lower[-1]
            ef   = ema_f[-1]
            es   = ema_s[-1]
            price = closes[i]

            buy_score = sell_score = 0

            if rsi < 35:   buy_score  += 2
            elif rsi < 45: buy_score  += 1
            elif rsi > 65: sell_score += 2
            elif rsi > 55: sell_score += 1

            if macd > sig and hist > 0:   buy_score  += 2
            elif macd > sig:              buy_score  += 1
            elif macd < sig and hist < 0: sell_score += 2
            elif macd < sig:              sell_score += 1

            if price < lo:   buy_score  += 2
            elif price < mid: buy_score += 1
            elif price > up:  sell_score += 2
            elif price > mid: sell_score += 1

            if ef > es:  buy_score  += 1
            elif ef < es: sell_score += 1

            alloc = capital * 0.20  # 20% per trade
            qty   = alloc / price if price > 0 else 0

            if buy_score >= threshold and position is None and qty > 0:
                cost = qty * price * (1 + commission_rate)
                if cost <= capital:
                    capital -= cost
                    position = {'qty': qty, 'entry': price, 'bar': i}

            elif (sell_score >= threshold or buy_score < threshold - 1) and position is not None:
                proceeds = position['qty'] * price * (1 - commission_rate)
                pnl = proceeds - position['qty'] * position['entry']
                capital += proceeds
                hold = i - position['bar']
                trades.append({'pnl': pnl, 'entry': position['entry'],
                                'exit': price, 'hold_bars': hold})
                position = None

            cur_eq = capital + (position['qty'] * price if position else 0)
            equity.append(cur_eq)
            if cur_eq > max_eq: max_eq = cur_eq
            dd = (max_eq - cur_eq) / max_eq
            if dd > max_dd: max_dd = dd

        if position and closes:
            price = closes[-1]
            proceeds = position['qty'] * price * (1 - commission_rate)
            pnl = proceeds - position['qty'] * position['entry']
            capital += proceeds
            trades.append({'pnl': pnl, 'entry': position['entry'],
                           'exit': price, 'hold_bars': len(closes) - position['bar']})

        return self._build_result(
            trades, equity, capital, max_dd, klines, params, symbol
        )

    def _build_result(
        self, trades, equity, final_cap, max_dd, klines, params, symbol
    ) -> 'BacktestResult':
        n = len(trades)
        wins = [t for t in trades if t['pnl'] > 0]
        losses = [t for t in trades if t['pnl'] <= 0]
        total_pnl = sum(t['pnl'] for t in trades)
        win_rate = len(wins) / n if n else 0.0

        gross_profit = sum(t['pnl'] for t in wins)
        gross_loss   = abs(sum(t['pnl'] for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (gross_profit or 0.0)

        avg_hold = sum(t['hold_bars'] for t in trades) / n if n else 0.0

        series = pd.Series(equity).pct_change().dropna()
        sharpe = float(series.mean() / series.std() * np.sqrt(252)) if series.std() > 0 else 0.0
        neg = series[series < 0]
        sortino = float(series.mean() / neg.std() * np.sqrt(252)) if len(neg) > 1 and neg.std() > 0 else 0.0

        start = str(klines[0]['open_time']) if klines else ''
        end   = str(klines[-1]['open_time']) if klines else ''

        return BacktestResult(
            strategy_name=f"walkforward_{symbol}",
            total_trades=n,
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=win_rate,
            total_pnl=total_pnl,
            total_pnl_percentage=(total_pnl / self.initial_capital) * 100,
            max_drawdown=max_dd * 100,
            sharpe_ratio=round(sharpe, 4),
            sortino_ratio=round(sortino, 4),
            profit_factor=round(profit_factor, 4),
            avg_hold_bars=round(avg_hold, 1),
            final_capital=round(final_cap, 4),
            initial_capital=self.initial_capital,
            start_date=start,
            end_date=end,
            params=params,
            metadata={'symbol': symbol, 'trades': trades[:50]},
        )

    # ── Persist backtest result ───────────────────────────────────────────────

    def persist_result(
        self,
        result: 'BacktestResult',
        symbol: str,
        interval: str = '1h',
        days_back: int = 30,
    ) -> Optional[str]:
        """Save backtest result to backtest_results table. Returns run_id."""
        if self.db is None:
            return None
        try:
            sql = """
            INSERT INTO backtest_results
                (symbol, interval, days_back, strategy_name,
                 total_trades, winning_trades, losing_trades, win_rate,
                 total_pnl, total_pnl_pct, max_drawdown, sharpe_ratio,
                 sortino_ratio, profit_factor,
                 initial_capital, final_capital,
                 start_date, end_date, params, metadata, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'completed')
            RETURNING run_id::text
            """
            params_json = json.dumps(result.params or {})
            meta_json   = json.dumps({k: v for k, v in (result.metadata or {}).items()
                                      if k != 'trades'})
            rows = self.db.execute_query(sql, (
                symbol, interval, days_back, result.strategy_name,
                result.total_trades, result.winning_trades, result.losing_trades,
                result.win_rate, result.total_pnl, result.total_pnl_percentage,
                result.max_drawdown, result.sharpe_ratio,
                result.sortino_ratio, result.profit_factor,
                result.initial_capital, result.final_capital,
                result.start_date or None, result.end_date or None,
                params_json, meta_json,
            ), fetch=True)
            run_id = str(rows[0][0]) if rows else None
            logger.info(f"Backtest result persisted for {symbol}: run_id={run_id}")
            return run_id
        except Exception as e:
            logger.warning(f"Could not persist backtest result: {e}")
            return None

    def get_results(self, symbol: str = None, limit: int = 10) -> List[Dict]:
        """Return recent backtest results from DB."""
        if self.db is None:
            return []
        try:
            where = "WHERE symbol = %s" if symbol else ""
            args  = (symbol, limit) if symbol else (limit,)
            sql = f"""
            SELECT run_id, symbol, interval, days_back, strategy_name,
                   total_trades, win_rate, total_pnl, sharpe_ratio,
                   sortino_ratio, profit_factor, max_drawdown,
                   status, created_at
            FROM backtest_results
            {where}
            ORDER BY created_at DESC LIMIT %s
            """
            rows = self.db.execute_query(sql, args, fetch=True)
            return [
                {
                    'run_id': str(r[0]), 'symbol': r[1], 'interval': r[2],
                    'days_back': r[3], 'strategy_name': r[4],
                    'total_trades': r[5], 'win_rate': r[6], 'total_pnl': r[7],
                    'sharpe_ratio': r[8], 'sortino_ratio': r[9],
                    'profit_factor': r[10], 'max_drawdown': r[11],
                    'status': r[12], 'created_at': str(r[13]),
                }
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"Could not load backtest results: {e}")
            return []
    
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
    
    def analyze_prediction_accuracy(self, closed_positions: List[Any]) -> PredictionAnalysis:
        """
        Analyze the accuracy of PnL predictions from closed positions.
        
        Args:
            closed_positions: List of ClosedPosition objects with prediction data
            
        Returns:
            PredictionAnalysis with accuracy metrics
        """
        positions_with_predictions = [
            pos for pos in closed_positions 
            if hasattr(pos, 'pnl_prediction') and pos.pnl_prediction is not None
        ]
        
        if not positions_with_predictions:
            logger.warning("No positions with predictions found for analysis")
            return PredictionAnalysis(
                total_predictions=0,
                accurate_predictions=0,
                prediction_accuracy=0.0,
                avg_prediction_error=0.0,
                prediction_bias=0.0,
                signal_quality_score=0.0,
                metadata={'message': 'No predictions found'}
            )
        
        total_predictions = len(positions_with_predictions)
        prediction_errors = []
        prediction_bias_sum = 0.0
        accurate_predictions = 0
        
        for pos in positions_with_predictions:
            predicted_pnl = pos.pnl_prediction.get('expected_pnl', 0)
            actual_pnl = pos.pnl
            
            # Calculate prediction error
            if predicted_pnl != 0:
                error = abs(actual_pnl - predicted_pnl) / abs(predicted_pnl)
                prediction_errors.append(error)
                
                # Calculate bias (actual - predicted)
                bias = actual_pnl - predicted_pnl
                prediction_bias_sum += bias
                
                # Count as accurate if error < 20%
                if error < 0.2:
                    accurate_predictions += 1
        
        # Calculate metrics
        avg_prediction_error = np.mean(prediction_errors) if prediction_errors else 0.0
        prediction_accuracy = (accurate_predictions / total_predictions) if total_predictions > 0 else 0.0
        prediction_bias = prediction_bias_sum / total_predictions if total_predictions > 0 else 0.0
        
        # Calculate signal quality score (combination of accuracy and bias)
        signal_quality_score = prediction_accuracy * 0.7 + (1 - min(avg_prediction_error, 1.0)) * 0.3
        
        logger.info(
            f"Prediction Analysis: {total_predictions} predictions, "
            f"Accuracy: {prediction_accuracy:.2%}, "
            f"Avg Error: {avg_prediction_error:.2%}, "
            f"Bias: {prediction_bias:.2f} USDT"
        )
        
        return PredictionAnalysis(
            total_predictions=total_predictions,
            accurate_predictions=accurate_predictions,
            prediction_accuracy=prediction_accuracy,
            avg_prediction_error=avg_prediction_error,
            prediction_bias=prediction_bias,
            signal_quality_score=signal_quality_score,
            metadata={
                'prediction_errors': prediction_errors,
                'positions_analyzed': total_predictions
            }
        )
    
    def calculate_prediction_bias(self, closed_positions: List[Any]) -> Dict[str, Any]:
        """
        Calculate prediction bias (systematic over/under prediction).
        
        Args:
            closed_positions: List of ClosedPosition objects with prediction data
            
        Returns:
            Dictionary with bias analysis
        """
        positions_with_predictions = [
            pos for pos in closed_positions 
            if hasattr(pos, 'pnl_prediction') and pos.pnl_prediction is not None
        ]
        
        if not positions_with_predictions:
            return {
                'mean_bias': 0.0,
                'median_bias': 0.0,
                'over_prediction_count': 0,
                'under_prediction_count': 0,
                'total_predictions': 0
            }
        
        biases = []
        over_predictions = 0
        under_predictions = 0
        
        for pos in positions_with_predictions:
            predicted_pnl = pos.pnl_prediction.get('expected_pnl', 0)
            actual_pnl = pos.pnl
            bias = actual_pnl - predicted_pnl
            biases.append(bias)
            
            if bias > 0:
                under_predictions += 1  # Predicted less than actual
            else:
                over_predictions += 1  # Predicted more than actual
        
        return {
            'mean_bias': np.mean(biases) if biases else 0.0,
            'median_bias': np.median(biases) if biases else 0.0,
            'std_bias': np.std(biases) if biases else 0.0,
            'over_prediction_count': over_predictions,
            'under_prediction_count': under_predictions,
            'total_predictions': len(positions_with_predictions)
        }
    
    def validate_pnl_predictor(self, closed_positions: List[Any]) -> Dict[str, Any]:
        """
        Validate the PnL predictor performance.
        
        Args:
            closed_positions: List of ClosedPosition objects with prediction data
            
        Returns:
            Dictionary with validation results
        """
        prediction_analysis = self.analyze_prediction_accuracy(closed_positions)
        bias_analysis = self.calculate_prediction_bias(closed_positions)
        
        # Determine if predictor is performing well
        is_performing_well = (
            prediction_analysis.prediction_accuracy >= 0.5 and
            prediction_analysis.avg_prediction_error <= 0.3 and
            abs(bias_analysis['mean_bias']) < 1.0  # Bias less than $1 USDT
        )
        
        return {
            'is_performing_well': is_performing_well,
            'prediction_analysis': {
                'accuracy': prediction_analysis.prediction_accuracy,
                'avg_error': prediction_analysis.avg_prediction_error,
                'signal_quality': prediction_analysis.signal_quality_score
            },
            'bias_analysis': bias_analysis,
            'recommendation': self._get_predictor_recommendation(prediction_analysis, bias_analysis),
            'timestamp': datetime.utcnow().isoformat()
        }
    
    def _get_predictor_recommendation(
        self, 
        prediction_analysis: PredictionAnalysis, 
        bias_analysis: Dict[str, Any]
    ) -> str:
        """
        Get recommendation for predictor improvement.
        
        Args:
            prediction_analysis: Prediction analysis results
            bias_analysis: Bias analysis results
            
        Returns:
            Recommendation string
        """
        if prediction_analysis.prediction_accuracy < 0.4:
            return "Predictor accuracy is low (<40%). Consider improving prediction model or adjusting indicators."
        elif prediction_analysis.avg_prediction_error > 0.5:
            return "Prediction error is high (>50%). Consider adding more indicators or improving volatility estimation."
        elif abs(bias_analysis['mean_bias']) > 2.0:
            return f"Significant bias detected ({bias_analysis['mean_bias']:.2f} USDT). Adjust prediction model to reduce bias."
        elif prediction_analysis.signal_quality_score < 0.5:
            return "Signal quality score is low. Consider improving indicator weighting or prediction logic."
        else:
            return "Predictor is performing within acceptable parameters."
