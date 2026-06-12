"""
Autonomous Signal Generation Module
Generates trading signals automatically using technical analysis from Binance data.
"""

from typing import List, Dict
from binance_client import BinanceClient
from technical_indicators import TechnicalIndicators
import logging

logger = logging.getLogger(__name__)


class AutonomousSignalGenerator:
    """Generates autonomous trading signals using technical analysis."""

    def __init__(self, binance_client: BinanceClient, symbols: List[str] = None):
        """
        Initialize the autonomous signal generator.

        Args:
            binance_client: Binance client for fetching market data
            symbols: List of symbols to analyze (default: major pairs)
        """
        self.binance_client = binance_client
        self.symbols = symbols or [
            'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT',
            'ADAUSDT', 'DOGEUSDT', 'AVAXUSDT', 'DOTUSDT', 'LINKUSDT'
        ]
    
    def get_btc_market_trend(self, interval: str = '4h') -> str:
        """
        Get BTC macro trend on the given timeframe.
        Returns 'UP', 'DOWN', or 'FLAT'.
        Used to scale position sizes: DOWN -> reduce exposure, UP -> normal/aggressive.
        """
        try:
            klines = self.fetch_klines('BTCUSDT', interval, limit=50)
            if not klines or len(klines) < 22:
                return 'FLAT'
            closes = [k['close'] for k in klines]
            ema_f, ema_s, _ = TechnicalIndicators.ema_cross(closes, fast=9, slow=21)
            if not ema_f or not ema_s:
                return 'FLAT'
            return 'UP' if ema_f[-1] > ema_s[-1] else 'DOWN'
        except Exception as e:
            logger.debug(f"BTC market trend error: {e}")
            return 'FLAT'

    def fetch_klines(self, symbol: str, interval: str = '1h', limit: int = 100) -> List[Dict]:
        """
        Fetch kline data from Binance.
        
        Args:
            symbol: Trading pair symbol
            interval: Kline interval (1m, 5m, 15m, 30m, 1h, 4h, 1d)
            limit: Number of klines to fetch
            
        Returns:
            List of kline data
        """
        try:
            klines = self.binance_client.client.get_klines(
                symbol=symbol,
                interval=interval,
                limit=limit
            )
            
            # Parse klines into structured data
            parsed_klines = []
            for kline in klines:
                parsed_klines.append({
                    'open_time': kline[0],
                    'open': float(kline[1]),
                    'high': float(kline[2]),
                    'low': float(kline[3]),
                    'close': float(kline[4]),
                    'volume': float(kline[5]),
                    'close_time': kline[6],
                    'quote_volume': float(kline[7]),
                    'trades': kline[8],
                    'taker_buy_base': float(kline[9]),
                    'taker_buy_quote': float(kline[10])
                })
            
            return parsed_klines
            
        except Exception as e:
            logger.error(f"Error fetching klines for {symbol}: {e}")
            return []
    
    def _get_trend_from_higher_tf(self, symbol: str, higher_interval: str = '4h') -> str:
        """
        Determine macro trend using a higher timeframe (default 4h).

        Returns:
            'UP'   — EMA9 > EMA21 on higher TF  (bullish bias)
            'DOWN' — EMA9 < EMA21 on higher TF  (bearish bias)
            'FLAT' — insufficient data or unclear
        """
        try:
            klines = self.fetch_klines(symbol, higher_interval, limit=50)
            if not klines or len(klines) < 22:
                return 'FLAT'
            closes = [k['close'] for k in klines]
            ema_f, ema_s, _ = TechnicalIndicators.ema_cross(closes, fast=9, slow=21)
            if not ema_f or not ema_s:
                return 'FLAT'
            return 'UP' if ema_f[-1] > ema_s[-1] else 'DOWN'
        except Exception as e:
            logger.debug(f"Higher-TF trend error for {symbol}: {e}")
            return 'FLAT'

    def analyze_symbol(self, symbol: str, interval: str = '1h',
                       use_multi_timeframe: bool = True) -> Dict:
        """
        Analyze a single symbol and generate trading signal.

        Args:
            symbol: Trading pair symbol
            interval: Primary kline interval for analysis
            use_multi_timeframe: If True, apply 4h trend filter to reduce counter-trend trades

        Returns:
            Dictionary with signal information
        """
        try:
            logger.info(f"Analyzing {symbol}...")
            klines = self.fetch_klines(symbol, interval, limit=100)
            logger.info(f"Fetched {len(klines)} klines for {symbol}")

            if not klines or len(klines) < 30:
                logger.warning(f"Insufficient data for {symbol}: {len(klines)} klines")
                return {
                    'symbol': symbol, 'signal': 'HOLD', 'strength': 0,
                    'reason': 'Insufficient data', 'indicators': {},
                }

            closes  = [k['close']  for k in klines]
            highs   = [k['high']   for k in klines]
            lows    = [k['low']    for k in klines]
            volumes = [k['volume'] for k in klines]

            logger.info(f"Price range for {symbol}: {min(closes):.2f} - {max(closes):.2f}")

            analysis = TechnicalIndicators.analyze_signal(closes, highs, lows, volumes)
            logger.info(f"Analysis result for {symbol}: {analysis['signal']} (strength: {analysis['strength']})")

            # ── Multi-timeframe filter ────────────────────────────────────────
            higher_trend = 'FLAT'
            if use_multi_timeframe:
                higher_trend = self._get_trend_from_higher_tf(symbol, '4h')
                logger.info(f"Higher-TF (4h) trend for {symbol}: {higher_trend}")

                if analysis['signal'] == 'BUY' and higher_trend == 'DOWN':
                    analysis['strength'] = max(0.0, analysis['strength'] * 0.5)
                    analysis['reason'] += ' [4h trend DOWN — strength halved]'
                    if analysis['strength'] < 0.3:
                        analysis['signal'] = 'HOLD'
                        analysis['reason'] += ' → downgraded to HOLD'

                elif analysis['signal'] == 'SELL' and higher_trend == 'UP':
                    analysis['strength'] = max(0.0, analysis['strength'] * 0.5)
                    analysis['reason'] += ' [4h trend UP — strength halved]'

            # ── Volume confirmation ───────────────────────────────────────────
            vol_spikes = TechnicalIndicators.volume_spike(volumes, period=20, threshold=1.5)
            volume_confirmed = bool(vol_spikes[-1]) if vol_spikes else False
            if volume_confirmed and analysis['signal'] == 'BUY':
                analysis['strength'] = min(1.0, analysis['strength'] * 1.15)
                analysis['reason'] += ' [+vol spike]'

            analysis['symbol']           = symbol
            analysis['interval']         = interval
            analysis['timestamp']        = klines[-1]['close_time']
            analysis['current_price']    = closes[-1]
            analysis['higher_tf_trend']  = higher_trend
            analysis['volume_confirmed'] = volume_confirmed

            return analysis

        except Exception as e:
            logger.error(f"Error analyzing {symbol}: {e}")
            return {
                'symbol': symbol, 'signal': 'HOLD', 'strength': 0,
                'reason': f'Analysis error: {str(e)}', 'indicators': {},
            }
    
    def generate_signals(self, interval: str = '1h',
                         use_multi_timeframe: bool = True) -> List[Dict]:
        """
        Generate trading signals for all configured symbols.

        Args:
            interval: Primary kline interval for analysis
            use_multi_timeframe: Apply 4h trend filter

        Returns:
            List of signal dicts sorted by strength (BUY first, then SELL, then HOLD)
        """
        signals = []

        for symbol in self.symbols:
            try:
                signal = self.analyze_symbol(symbol, interval,
                                             use_multi_timeframe=use_multi_timeframe)
                signals.append(signal)
                vol_tag = ' 🔊' if signal.get('volume_confirmed') else ''
                tf_tag  = f" [{signal.get('higher_tf_trend','?')}]" if use_multi_timeframe else ''
                logger.info(
                    f"Analyzed {symbol}: {signal['signal']}"
                    f" (strength:{signal['strength']:.2f}{vol_tag}{tf_tag})"
                )
            except Exception as e:
                logger.error(f"Error generating signal for {symbol}: {e}")
                continue

        def signal_priority(s):
            if s['signal'] == 'BUY':
                return (0, -s['strength'])
            elif s['signal'] == 'SELL':
                return (1, -s['strength'])
            return (2, 0)

        signals.sort(key=signal_priority)
        return signals

    def get_top_buy_signals(self, interval: str = '1h', min_strength: float = 0.6,
                            limit: int = 5, use_multi_timeframe: bool = True) -> List[Dict]:
        """
        Get top BUY signals, filtered by min_strength.

        Args:
            interval: Primary kline interval
            min_strength: Minimum signal strength to include
            limit: Maximum number of signals to return
            use_multi_timeframe: Apply 4h trend filter (recommended)

        Returns:
            List of top BUY signal dicts
        """
        all_signals = self.generate_signals(interval, use_multi_timeframe=use_multi_timeframe)
        logger.info(f"Total signals generated: {len(all_signals)}")
        logger.info(
            f"Signal breakdown: "
            f"{[(s['symbol'], s['signal'], round(s['strength'],2), s.get('higher_tf_trend','?')) for s in all_signals]}"
        )

        buy_signals = [
            s for s in all_signals
            if s['signal'] == 'BUY' and s['strength'] >= min_strength
        ]
        logger.info(f"Buy signals above threshold {min_strength}: {len(buy_signals)}")
        return buy_signals[:limit]

    def get_top_sell_signals(self, interval: str = '1h', min_strength: float = 0.6,
                             limit: int = 5, use_multi_timeframe: bool = True) -> List[Dict]:
        """
        Get top SELL signals, filtered by min_strength.

        Args:
            interval: Primary kline interval
            min_strength: Minimum signal strength to include
            limit: Maximum number of signals to return
            use_multi_timeframe: Apply 4h trend filter (recommended)

        Returns:
            List of top SELL signal dicts
        """
        all_signals = self.generate_signals(interval, use_multi_timeframe=use_multi_timeframe)
        sell_signals = [
            s for s in all_signals
            if s['signal'] == 'SELL' and s['strength'] >= min_strength
        ]
        return sell_signals[:limit]
