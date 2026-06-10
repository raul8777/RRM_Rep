from typing import Dict, Tuple
import numpy as np
from binance_client import BinanceClient

try:
    import talib
except ImportError:
    talib = None


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


def calculate_rsi(prices: np.ndarray, period: int = 14) -> float:
    """Calculate RSI indicator. Returns last RSI value (0-100)."""
    if talib is None:
        return 50.0
    try:
        rsi = talib.RSI(prices, timeperiod=period)
        return float(rsi[-1])
    except Exception:
        return 50.0


def calculate_ema(prices: np.ndarray, period: int = 20) -> Tuple[float, str]:
    """Calculate EMA and return (value, trend). Trend: 'UP', 'DOWN', or 'NEUTRAL'."""
    if talib is None:
        return 0.0, 'NEUTRAL'
    try:
        ema = talib.EMA(prices, timeperiod=period)
        current_ema = float(ema[-1])
        current_price = float(prices[-1])

        if current_price > current_ema:
            trend = 'UP'
        elif current_price < current_ema:
            trend = 'DOWN'
        else:
            trend = 'NEUTRAL'

        return current_ema, trend
    except Exception:
        return 0.0, 'NEUTRAL'


def calculate_macd(prices: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9) -> Dict[str, float]:
    """Calculate MACD and return signal summary."""
    if talib is None:
        return {'macd': 0.0, 'signal': 0.0, 'histogram': 0.0, 'trend': 'NEUTRAL'}
    try:
        macd, signal_line, histogram = talib.MACD(prices, fastperiod=fast, slowperiod=slow, signalperiod=signal)
        return {
            'macd': float(macd[-1]),
            'signal': float(signal_line[-1]),
            'histogram': float(histogram[-1]),
            'trend': 'UP' if histogram[-1] > 0 else 'DOWN',
        }
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
