from typing import Dict, Tuple
import numpy as np
import pandas as pd
from binance_client import BinanceClient

try:
    import pandas_ta as ta
    _TA_AVAILABLE = True
except ImportError:
    _TA_AVAILABLE = False


def _fetch_candles(client: BinanceClient, symbol: str, interval: str = '1h', limit: int = 200) -> Dict:
    """Fetch OHLCV candle data from Binance."""
    try:
        klines = client.client.get_klines(symbol=symbol, interval=interval, limit=limit)
        return {
            'open': np.array([float(k[1]) for k in klines]),
            'high': np.array([float(k[2]) for k in klines]),
            'low': np.array([float(k[3]) for k in klines]),
            'close': np.array([float(k[4]) for k in klines]),
            'volume': np.array([float(k[7]) for k in klines]),
        }
    except Exception as e:
        raise RuntimeError(f"Cannot fetch candles for {symbol}: {e}")


def _rsi_pure(prices: np.ndarray, period: int = 14) -> float:
    """Pure-numpy RSI fallback (Wilder smoothing)."""
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices.astype(float))
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1.0 + rs))


def _ema_pure(prices: np.ndarray, period: int) -> np.ndarray:
    """Pure-numpy EMA."""
    result = np.empty(len(prices))
    result[:] = np.nan
    if len(prices) < period:
        return result
    k = 2.0 / (period + 1)
    result[period - 1] = np.mean(prices[:period])
    for i in range(period, len(prices)):
        result[i] = prices[i] * k + result[i - 1] * (1 - k)
    return result


def calculate_rsi(prices: np.ndarray, period: int = 14) -> float:
    """Calculate RSI indicator. Returns last RSI value (0-100)."""
    try:
        if _TA_AVAILABLE:
            s = pd.Series(prices.astype(float))
            rsi = ta.rsi(s, length=period)
            val = rsi.dropna().iloc[-1] if rsi is not None and not rsi.dropna().empty else None
            if val is not None and not np.isnan(val):
                return float(val)
        return _rsi_pure(prices, period)
    except Exception:
        return _rsi_pure(prices, period)


def calculate_ema(prices: np.ndarray, period: int = 20) -> Tuple[float, str]:
    """Calculate EMA and return (value, trend). Trend: 'UP', 'DOWN', or 'NEUTRAL'."""
    try:
        if _TA_AVAILABLE:
            s = pd.Series(prices.astype(float))
            ema_series = ta.ema(s, length=period)
            if ema_series is not None:
                val = ema_series.dropna().iloc[-1] if not ema_series.dropna().empty else None
                if val is not None and not np.isnan(val):
                    current_ema = float(val)
                    current_price = float(prices[-1])
                    trend = 'UP' if current_price > current_ema else ('DOWN' if current_price < current_ema else 'NEUTRAL')
                    return current_ema, trend
        ema_arr = _ema_pure(prices, period)
        valid = ema_arr[~np.isnan(ema_arr)]
        if len(valid) == 0:
            return 0.0, 'NEUTRAL'
        current_ema = float(valid[-1])
        current_price = float(prices[-1])
        trend = 'UP' if current_price > current_ema else ('DOWN' if current_price < current_ema else 'NEUTRAL')
        return current_ema, trend
    except Exception:
        return 0.0, 'NEUTRAL'


def calculate_macd(prices: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9) -> Dict[str, float]:
    """Calculate MACD and return signal summary."""
    try:
        if _TA_AVAILABLE:
            s = pd.Series(prices.astype(float))
            macd_df = ta.macd(s, fast=fast, slow=slow, signal=signal)
            if macd_df is not None and not macd_df.empty:
                last = macd_df.dropna().iloc[-1] if not macd_df.dropna().empty else None
                if last is not None:
                    hist = float(last.iloc[1])
                    return {
                        'macd': float(last.iloc[0]),
                        'signal': float(last.iloc[2]),
                        'histogram': hist,
                        'trend': 'UP' if hist > 0 else 'DOWN',
                    }
        # Pure-numpy MACD fallback
        ema_fast = _ema_pure(prices, fast)
        ema_slow = _ema_pure(prices, slow)
        macd_line = ema_fast - ema_slow
        valid_macd = macd_line[~np.isnan(macd_line)]
        if len(valid_macd) < signal:
            return {'macd': 0.0, 'signal': 0.0, 'histogram': 0.0, 'trend': 'NEUTRAL'}
        signal_line = _ema_pure(valid_macd, signal)
        valid_sig = signal_line[~np.isnan(signal_line)]
        if len(valid_sig) == 0:
            return {'macd': 0.0, 'signal': 0.0, 'histogram': 0.0, 'trend': 'NEUTRAL'}
        m = float(valid_macd[-1])
        s_val = float(valid_sig[-1])
        hist = m - s_val
        return {'macd': m, 'signal': s_val, 'histogram': hist, 'trend': 'UP' if hist > 0 else 'DOWN'}
    except Exception:
        return {'macd': 0.0, 'signal': 0.0, 'histogram': 0.0, 'trend': 'NEUTRAL'}


def get_indicators(client: BinanceClient, symbol: str) -> Dict:
    """Fetch candle data and calculate all technical indicators."""
    candles = _fetch_candles(client, symbol)
    close_prices = candles['close']

    rsi = calculate_rsi(close_prices)
    ema20, ema20_trend = calculate_ema(close_prices, 20)
    ema50, ema50_trend = calculate_ema(close_prices, 50)
    ema200, ema200_trend = calculate_ema(close_prices, 200)
    macd = calculate_macd(close_prices)

    return {
        'symbol': symbol,
        'rsi': rsi,
        'ema20': {'value': ema20, 'trend': ema20_trend},
        'ema50': {'value': ema50, 'trend': ema50_trend},
        'ema200': {'value': ema200, 'trend': ema200_trend},
        'macd': macd,
        'current_price': float(close_prices[-1]),
    }


def score_technical(indicators: Dict) -> float:
    """Score technical indicators on a scale of 0-100. Higher = more bullish."""
    score = 50.0

    rsi = indicators.get('rsi', 50)
    if rsi < 30:
        score += 20
    elif rsi > 70:
        score -= 20
    elif 40 <= rsi <= 60:
        score += 5

    ema20 = indicators.get('ema20', {})
    if ema20.get('trend') == 'UP':
        score += 10
    elif ema20.get('trend') == 'DOWN':
        score -= 10

    ema50 = indicators.get('ema50', {})
    if ema50.get('trend') == 'UP':
        score += 8
    elif ema50.get('trend') == 'DOWN':
        score -= 8

    ema200 = indicators.get('ema200', {})
    if ema200.get('trend') == 'UP':
        score += 5
    elif ema200.get('trend') == 'DOWN':
        score -= 5

    macd = indicators.get('macd', {})
    if macd.get('trend') == 'UP':
        score += 7
    elif macd.get('trend') == 'DOWN':
        score -= 7

    return max(0.0, min(100.0, score))
