"""
PnL Predictor Module

Calculates expected PnL and success probability before opening positions
using technical indicators and market analysis.
"""

import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import numpy as np

logger = logging.getLogger(__name__)


# Default fixed weights (used when no learned weights are available)
# vol_spike, atr_pct, trend_4h, weekly_range added as validated predictors (jun-16-2026)
_DEFAULT_WEIGHTS = {
    "rsi":          0.12,
    "macd":         0.10,
    "bb":           0.08,
    "ema":          0.08,
    "volume":       0.04,
    "vol_spike":    0.15,   # volume breakout — strongest single predictor
    "atr_pct":      0.08,   # volatility regime — higher ATR = wider expected move
    "trend_4h":     0.15,   # macro trend EMA9>EMA21 on 4h
    "weekly_range": 0.08,   # price position vs 7d range (low = support)
    "bias_correction": 1.0,
}


class PnLPredictor:
    """Predicts expected PnL and success probability for trading positions."""

    def __init__(self):
        """Initialize PnL Predictor."""
        self._weight_cache: Dict[str, Dict] = {}  # symbol -> weights
        logger.info("PnL Predictor initialized")

    def load_learned_weights(self, symbol: str) -> Dict:
        """
        Load indicator weights from LearningEngine for *symbol*.
        Falls back to __global__ then defaults.  Results are in-process cached.
        """
        if symbol in self._weight_cache:
            return self._weight_cache[symbol]
        try:
            from learning_engine import get_learning_engine
            weights = get_learning_engine().get_weights(symbol)
        except Exception:
            weights = _DEFAULT_WEIGHTS.copy()
        self._weight_cache[symbol] = weights
        return weights

    def invalidate_cache(self, symbol: str = None) -> None:
        """Clear weight cache so next predict_pnl loads fresh weights."""
        if symbol:
            self._weight_cache.pop(symbol, None)
        else:
            self._weight_cache.clear()
    
    def predict_pnl(
        self,
        symbol: str,
        current_price: float,
        indicators: Dict,
        cycle_duration_minutes: int = 5,
        position_size_usdt: float = 15.0,
        trade_type: str = 'spot',
        settings = None,
    ) -> Dict:
        """
        Predict expected PnL for a position.

        Args:
            symbol: Trading pair symbol
            current_price: Current market price
            indicators: Dictionary of technical indicators
            cycle_duration_minutes: Duration of trading cycle in minutes
            position_size_usdt: Size of position in USDT
            trade_type: 'spot' or 'futures' — used to subtract Binance fees
            settings: Settings object for commission rates

        Returns:
            Dictionary with prediction results
        """
        try:
            weights = self.load_learned_weights(symbol)
            # Detect bootstrap: if all weights match defaults, no learning has happened
            _default_items = {k: v for k, v in _DEFAULT_WEIGHTS.items()}
            _weight_items  = {k: weights.get(k) for k in _DEFAULT_WEIGHTS}
            is_default = (_weight_items == _default_items)

            # Calculate success probability based on indicators + learned weights
            success_probability = self._calculate_success_probability(indicators, weights)

            # Estimate price range for the cycle
            price_range = self._estimate_price_range(current_price, indicators, cycle_duration_minutes)

            # Calculate expected PnL
            expected_pnl = self._calculate_expected_pnl(
                current_price,
                price_range,
                success_probability,
                position_size_usdt
            )

            # Subtract Binance round-trip commission so expected_pnl is net
            from binance_commission import expected_pnl_after_fees
            expected_pnl = expected_pnl_after_fees(expected_pnl, position_size_usdt, trade_type, settings)

            # Apply learned bias correction
            bias = weights.get("bias_correction", 1.0)
            expected_pnl = expected_pnl * bias

            # Calculate risk metrics
            risk_metrics = self._calculate_risk_metrics(
                current_price,
                price_range,
                position_size_usdt
            )
            
            prediction = {
                'symbol': symbol,
                'current_price': current_price,
                'expected_pnl': expected_pnl,
                'expected_pnl_percentage': (expected_pnl / position_size_usdt) * 100 if position_size_usdt > 0 else 0,
                'success_probability': success_probability,
                'price_range': price_range,
                'risk_metrics': risk_metrics,
                'indicators_used': indicators,
                'prediction_time': datetime.utcnow().isoformat(),
                'cycle_duration_minutes': cycle_duration_minutes,
                'position_size_usdt': position_size_usdt,
                'bootstrap_mode': is_default,
            }
            
            logger.info(
                f"PnL Prediction for {symbol}: "
                f"Expected PnL={expected_pnl:.2f} ({prediction['expected_pnl_percentage']:.2f}%), "
                f"Success Prob={success_probability:.2f}"
            )
            
            return prediction
            
        except Exception as e:
            logger.error(f"Error predicting PnL for {symbol}: {e}")
            return {
                'symbol': symbol,
                'current_price': current_price,
                'expected_pnl': 0.0,
                'expected_pnl_percentage': 0.0,
                'success_probability': 0.5,
                'price_range': {'min': current_price * 0.95, 'max': current_price * 1.05},
                'risk_metrics': {},
                'indicators_used': indicators,
                'prediction_time': datetime.utcnow().isoformat(),
                'cycle_duration_minutes': cycle_duration_minutes,
                'position_size_usdt': position_size_usdt,
                'error': str(e)
            }
    
    def _calculate_success_probability(self, indicators: Dict, weights: Dict = None) -> float:
        """
        Calculate success probability using adaptive indicator weights.

        Args:
            indicators: Dictionary of technical indicators
            weights:    Learned weight dict (falls back to defaults)

        Returns:
            Success probability between 0.0 and 1.0
        """
        if weights is None:
            weights = _DEFAULT_WEIGHTS

        w_rsi         = weights.get("rsi",          0.12)
        w_macd        = weights.get("macd",         0.10)
        w_bb          = weights.get("bb",           0.08)
        w_ema         = weights.get("ema",          0.08)
        w_vol_spike   = weights.get("vol_spike",    0.15)
        w_atr         = weights.get("atr_pct",      0.08)
        w_trend_4h    = weights.get("trend_4h",     0.15)
        w_weekly_rng  = weights.get("weekly_range", 0.08)

        def _num(v, default=0.0) -> float:
            try:
                return float(v)
            except Exception:
                return default

        probability = 0.5  # Base probability

        # ── RSI analysis ────────────────────────────────────────────────────
        rsi = indicators.get('rsi', {})
        if rsi:
            rsi_value = _num(rsi.get('value', 50), 50.0)
            if rsi_value < 30:
                probability += w_rsi
            elif rsi_value < 40:
                probability += w_rsi * 0.67
            elif rsi_value > 70:
                probability -= w_rsi
            elif rsi_value > 60:
                probability -= w_rsi * 0.67

        # ── MACD analysis ───────────────────────────────────────────────────
        macd = indicators.get('macd', {})
        if macd:
            macd_value = _num(macd.get('macd', 0), 0.0)
            signal_value = _num(macd.get('signal', 0), 0.0)
            histogram = _num(macd.get('histogram', 0), 0.0)
            if macd_value > signal_value:
                probability += w_macd
            if histogram > 0:
                probability += w_macd * 0.33
            elif histogram < 0:
                probability -= w_macd * 0.33

        # ── EMA vs SMA ──────────────────────────────────────────────────────
        sma = indicators.get('sma', {})
        ema = indicators.get('ema', {})
        if sma and ema:
            if _num(ema.get('value', 0), 0.0) > _num(sma.get('value', 0), 0.0):
                probability += w_ema
            else:
                probability -= w_ema * 0.5

        # ── Bollinger Bands ─────────────────────────────────────────────────
        bollinger = indicators.get('bollinger_bands', {})
        if bollinger:
            upper  = _num(bollinger.get('upper', 0), 0.0)
            lower  = _num(bollinger.get('lower', 0), 0.0)
            middle = _num(bollinger.get('middle', 0), 0.0)
            if lower > 0 and middle > 0 and upper > lower:
                position_in_band = (middle - lower) / (upper - lower)
                if position_in_band < 0.2:
                    probability += w_bb
                elif position_in_band > 0.8:
                    probability -= w_bb

        # ── Vol Spike (strongest validated predictor) ───────────────────────
        vol_spike_d = indicators.get('vol_spike', {})
        if vol_spike_d:
            spike = _num(vol_spike_d.get('value', 1.0), 1.0)
            if spike >= 2.0:
                probability += w_vol_spike          # strong breakout confirmation
            elif spike >= 1.5:
                probability += w_vol_spike * 0.60
            elif spike < 0.5:
                probability -= w_vol_spike * 0.80   # very thin volume = avoid
            elif spike < 0.8:
                probability -= w_vol_spike * 0.40

        # ── ATR% — adjust for volatility regime ─────────────────────────────
        atr_d = indicators.get('atr_pct', {})
        if atr_d:
            atr_pct = _num(atr_d.get('value', 2.0), 2.0)
            if atr_pct >= 3.0:
                probability += w_atr * 0.50    # high vol = bigger potential move
            elif atr_pct < 0.5:
                probability -= w_atr * 0.50    # very compressed = low reward

        # ── Trend 4h (macro context) ─────────────────────────────────────────
        trend_d = indicators.get('trend_4h', {})
        if trend_d:
            trend_raw = trend_d.get('value', 0)
            if isinstance(trend_raw, str):
                _t = trend_raw.strip().upper()
                trend = 1.0 if _t in ('UP', 'BULL', 'BULLISH') else -1.0 if _t in ('DOWN', 'BEAR', 'BEARISH') else 0.0
            else:
                try:
                    trend = float(trend_raw)
                except Exception:
                    trend = 0.0
            if trend > 0:                        # EMA9 > EMA21 on 4h → bullish
                probability += w_trend_4h
            elif trend < 0:                      # bearish 4h context
                probability -= w_trend_4h * 0.80

        # ── Weekly range position (buy at support, not resistance) ────────────
        wr_d = indicators.get('weekly_range', {})
        if wr_d:
            wr = _num(wr_d.get('value', 0.5), 0.5)
            if wr <= 0.30:
                probability += w_weekly_rng         # near weekly support
            elif wr >= 0.85:
                probability -= w_weekly_rng * 0.90  # near weekly resistance

        return max(0.0, min(1.0, probability))
    
    def _estimate_price_range(
        self,
        current_price: float,
        indicators: Dict,
        cycle_duration_minutes: int
    ) -> Dict:
        """
        Estimate price range for the trading cycle.
        
        Args:
            current_price: Current market price
            indicators: Dictionary of technical indicators
            cycle_duration_minutes: Duration of trading cycle
            
        Returns:
            Dictionary with min and max estimated prices
        """
        # Calculate volatility based on Bollinger Bands
        bollinger = indicators.get('bollinger_bands', {})
        volatility = 0.02  # Default 2% volatility
        
        if bollinger:
            upper = bollinger.get('upper', 0)
            lower = bollinger.get('lower', 0)
            if upper > 0 and lower > 0:
                band_width = (upper - lower) / current_price
                volatility = band_width / 2  # Use half the band width as volatility estimate
        
        # Adjust volatility based on cycle duration
        # Longer cycles = more potential movement
        time_factor = np.sqrt(cycle_duration_minutes / 60)  # Square root of hours
        adjusted_volatility = volatility * time_factor

        # Cap adjusted volatility to realistic intraday range (max 5% per 4h horizon)
        # This prevents the price_range from inflating PnL estimates unrealistically
        adjusted_volatility = min(adjusted_volatility, 0.05)
        
        # Estimate price range
        min_price = current_price * (1 - adjusted_volatility)
        max_price = current_price * (1 + adjusted_volatility)
        
        return {
            'min': min_price,
            'max': max_price,
            'volatility': volatility,
            'adjusted_volatility': adjusted_volatility
        }
    
    def _calculate_expected_pnl(
        self,
        current_price: float,
        price_range: Dict,
        success_probability: float,
        position_size_usdt: float
    ) -> float:
        """
        Calculate expected PnL.
        
        Args:
            current_price: Current market price
            price_range: Estimated price range
            success_probability: Probability of successful trade
            position_size_usdt: Size of position in USDT
            
        Returns:
            Expected PnL in USDT
        """
        # Calculate quantity
        quantity = position_size_usdt / current_price
        
        # Expected price if successful (weighted towards max of range)
        expected_exit_price = current_price + (price_range['max'] - current_price) * success_probability
        
        # Expected PnL
        expected_pnl = (expected_exit_price - current_price) * quantity
        
        # Apply success probability
        expected_pnl = expected_pnl * success_probability
        
        # Subtract expected loss (failure case)
        expected_loss = (current_price - price_range['min']) * quantity * (1 - success_probability)
        expected_pnl = expected_pnl - expected_loss
        
        return expected_pnl
    
    def _calculate_risk_metrics(
        self,
        current_price: float,
        price_range: Dict,
        position_size_usdt: float
    ) -> Dict:
        """
        Calculate risk metrics for the position.
        
        Args:
            current_price: Current market price
            price_range: Estimated price range
            position_size_usdt: Size of position in USDT
            
        Returns:
            Dictionary with risk metrics
        """
        quantity = position_size_usdt / current_price
        
        # Maximum potential loss
        max_loss = (current_price - price_range['min']) * quantity
        
        # Maximum potential gain
        max_gain = (price_range['max'] - current_price) * quantity
        
        # Risk-reward ratio
        risk_reward_ratio = max_gain / max_loss if max_loss > 0 else 0
        
        # Percentage risk
        percentage_risk = (max_loss / position_size_usdt) * 100 if position_size_usdt > 0 else 0
        
        # Percentage reward
        percentage_reward = (max_gain / position_size_usdt) * 100 if position_size_usdt > 0 else 0
        
        return {
            'max_loss_usdt': max_loss,
            'max_gain_usdt': max_gain,
            'risk_reward_ratio': risk_reward_ratio,
            'percentage_risk': percentage_risk,
            'percentage_reward': percentage_reward
        }
    
    def validate_prediction(
        self,
        prediction: Dict,
        min_expected_pnl: float = 0.0,
        min_success_probability: float = 0.5,
        max_risk_percentage: float = 10.0
    ) -> Tuple[bool, str]:
        """
        Validate if a prediction meets criteria for opening a position.
        
        Args:
            prediction: Prediction dictionary
            min_expected_pnl: Minimum expected PnL in USDT
            min_success_probability: Minimum success probability
            max_risk_percentage: Maximum acceptable risk percentage
            
        Returns:
            Tuple of (is_valid, reason)
        """
        expected_pnl = prediction.get('expected_pnl', 0)
        success_prob = prediction.get('success_probability', 0)
        risk_metrics = prediction.get('risk_metrics', {})
        percentage_risk = risk_metrics.get('percentage_risk', 0)
        
        # Check expected PnL
        if expected_pnl < min_expected_pnl:
            return False, f"Expected PnL too low: {expected_pnl:.2f} < {min_expected_pnl}"
        
        # Check success probability
        if success_prob < min_success_probability:
            return False, f"Success probability too low: {success_prob:.2f} < {min_success_probability}"
        
        # Check risk
        if percentage_risk > max_risk_percentage:
            return False, f"Risk too high: {percentage_risk:.2f}% > {max_risk_percentage}%"
        
        return True, "Prediction meets criteria"
