"""
Autonomous Signal Generation Module
Generates trading signals automatically using technical analysis from Binance data.
"""

from datetime import datetime, timezone
from typing import List, Dict, Optional
from binance_client import BinanceClient
from technical_indicators import TechnicalIndicators
import logging

logger = logging.getLogger(__name__)

# ── Per-symbol signal cache (TTL = SIGNAL_CACHE_TTL_SECONDS) ─────────────────
SIGNAL_CACHE_TTL_SECONDS = 600   # 10 minutes (B3: fresher signals in volatile conditions)
_signal_cache: Dict[str, Dict] = {}  # {symbol: {"signal": ..., "cached_at": datetime}}


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

    @staticmethod
    def _get_cached_signal(symbol: str) -> Optional[Dict]:
        """Return cached signal if still valid (within TTL), else None."""
        entry = _signal_cache.get(symbol)
        if not entry:
            return None
        age = (datetime.now(timezone.utc) - entry["cached_at"]).total_seconds()
        if age < SIGNAL_CACHE_TTL_SECONDS:
            logger.debug(f"Cache hit for {symbol} (age={age:.0f}s)")
            return entry["signal"]
        return None

    @staticmethod
    def _cache_signal(symbol: str, signal: Dict) -> None:
        """Store signal in cache with current timestamp."""
        _signal_cache[symbol] = {"signal": signal, "cached_at": datetime.now(timezone.utc)}

    @staticmethod
    def _invalidate_cache(symbol: str) -> None:
        """Remove symbol from cache (e.g. after a trade is executed)."""
        _signal_cache.pop(symbol, None)

    def analyze_symbol(self, symbol: str, interval: str = '1h',
                       use_multi_timeframe: bool = True,
                       news_client=None) -> Dict:
        """
        Analyze a single symbol and generate trading signal.

        Args:
            symbol: Trading pair symbol
            interval: Primary kline interval for analysis
            use_multi_timeframe: If True, apply 4h trend filter to reduce counter-trend trades

        Returns:
            Dictionary with signal information
        """
        # ── Cache check ───────────────────────────────────────────────────────
        cached = AutonomousSignalGenerator._get_cached_signal(symbol)
        if cached is not None:
            return cached

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

            # ── RSI divergence detection ──────────────────────────────────────
            try:
                from technical_indicators import TechnicalIndicators as _TI
                _rsi_vals = _TI.rsi(closes, 14)
                if _rsi_vals and len(_rsi_vals) >= 5 and len(closes) >= 5:
                    _price_hi = closes[-1] > closes[-5]   # price made higher high in last 5 bars
                    _rsi_hi   = _rsi_vals[-1] > _rsi_vals[-5]  # RSI also moved higher
                    _price_lo = closes[-1] < closes[-5]   # price made lower low
                    _rsi_lo   = _rsi_vals[-1] < _rsi_vals[-5]  # RSI also moved lower
                    # Bearish divergence: price up but RSI down → weakening momentum
                    if _price_hi and not _rsi_hi and analysis['signal'] == 'BUY':
                        analysis['strength'] = max(0.0, analysis['strength'] - 0.08)
                        analysis['reason'] += ' [-bearish RSI div]'
                        logger.debug(f"Bearish RSI divergence {symbol}: price↑ RSI↓")
                    # Bullish divergence: price down but RSI up → potential reversal
                    elif _price_lo and not _rsi_lo and analysis['signal'] == 'BUY':
                        analysis['strength'] = min(1.0, analysis['strength'] + 0.05)
                        analysis['reason'] += ' [+bullish RSI div]'
                        logger.debug(f"Bullish RSI divergence {symbol}: price↓ RSI↑")
            except Exception:
                pass

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

            # Always compute vol_spike ratio so signal payload always has a real value
            # (M4 breakout block may overwrite with its own ratio if breakout detected)
            try:
                _vs_avg = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else None
                analysis['vol_spike'] = round(volumes[-1] / _vs_avg, 4) if _vs_avg and _vs_avg > 0 else 0.0
            except Exception:
                analysis.setdefault('vol_spike', 0.0)

            # ── Enrich with EMA200 trend from analysis_cache (Mejora E) ──────
            try:
                from analysis_cache import analysis_cache
                cached_analysis = analysis_cache.get(symbol)
                if cached_analysis:
                    ema200 = cached_analysis.get('indicators', {}).get('ema200', {})
                    if isinstance(ema200, dict):
                        analysis['ema200_trend'] = ema200.get('trend', 'UNKNOWN')
                    elif isinstance(ema200, str) and 'trend' in ema200:
                        # Powershell serialization artifact — parse manually if needed
                        analysis['ema200_trend'] = 'UP' if 'UP' in ema200 else 'DOWN'
                    # If price is below EMA200 (long-term bear), penalize BUY strength
                    if analysis.get('ema200_trend') == 'DOWN' and analysis['signal'] == 'BUY':
                        analysis['strength'] = max(0.0, analysis['strength'] * 0.85)
                        analysis['reason'] += ' [EMA200 DOWN -15%]'
            except Exception:
                pass

            # ── Bollinger Band analysis (B1 + A4) ────────────────────────────
            # B1: expose bb_lower_touch so RANGE_SCALP can verify real price level
            # A4: BB breakout detector — compression → expansion setup
            try:
                from technical_indicators import TechnicalIndicators as _TI_bb
                _bb_closes = closes
                if len(_bb_closes) >= 20:
                    _bbu, _bbm, _bbl = _TI_bb.bollinger_bands(_bb_closes, period=20, std_multiplier=2.0)
                    if _bbu and _bbl and _bbm:
                        _last_price = _bb_closes[-1]
                        _bw = (_bbu[-1] - _bbl[-1]) / _bbm[-1] if _bbm[-1] > 0 else 0.05
                        # B1: price touching or below lower band → good RANGE_SCALP entry
                        _bb_lower_touch = _last_price <= _bbl[-1] * 1.005  # within 0.5% of lower band
                        analysis['bb_lower_touch'] = _bb_lower_touch
                        analysis['bb_bandwidth'] = round(_bw, 4)
                        analysis['bb_lower'] = _bbl[-1]
                        analysis['bb_upper'] = _bbu[-1]
                        # A4: BB squeeze breakout — prior compression + current vol spike
                        if len(_bbu) >= 5:
                            _prev_bw = [(_bbu[-i] - _bbl[-i]) / _bbm[-i] if _bbm[-i] > 0 else 0.05
                                        for i in range(2, 6)]  # look back 4 bars
                            _was_compressed = all(b < 0.04 for b in _prev_bw)  # compressed last 4 bars
                            _vol_avg = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else None
                            _vol_spike_now = (volumes[-1] / _vol_avg) if _vol_avg and _vol_avg > 0 else 0
                            if _was_compressed and _vol_spike_now >= 1.5 and analysis['signal'] == 'BUY':
                                analysis['strength'] = min(1.0, analysis['strength'] + 0.12)
                                analysis['reason'] += f' [+BB_squeeze_breakout bw={_bw:.3f} vol={_vol_spike_now:.1f}x]'
                                logger.info(f"BB Squeeze BREAKOUT {symbol}: bw={_bw:.3f}, vol={_vol_spike_now:.1f}x → +0.12 strength")
            except Exception:
                pass

            # ── Hourly momentum boost (breakout in progress) ─────────────────
            if analysis['signal'] == 'BUY' and len(klines) >= 2:
                try:
                    last_candle = klines[-1]
                    prev_close  = klines[-2]['close']
                    last_chg    = (last_candle['close'] - prev_close) / prev_close if prev_close > 0 else 0
                    avg_vol20   = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else None
                    vol_ratio   = last_candle['volume'] / avg_vol20 if avg_vol20 and avg_vol20 > 0 else 0
                    if last_chg >= 0.015 and vol_ratio >= 1.2:
                        analysis['strength'] = min(1.0, analysis['strength'] + 0.05)
                        analysis['reason'] += f' [+momentum {last_chg*100:.1f}% vol={vol_ratio:.1f}x]'
                        logger.info(f"Momentum BOOST {symbol}: candle={last_chg*100:.2f}%, vol={vol_ratio:.1f}x")
                except Exception:
                    pass

            # ── M4: Breakout entry detector (ruptura máximo 12h + vol>2x) ────
            if analysis['signal'] == 'BUY' and len(klines) >= 13:
                try:
                    _recent_highs = [k['high'] for k in klines[-13:-1]]  # últimas 12 velas excluyendo la actual
                    _max_12h = max(_recent_highs) if _recent_highs else 0
                    _cur_close = closes[-1]
                    _avg_vol_bo = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else None
                    _last_vol_bo = volumes[-1] if volumes else 0
                    _vs_bo = (_last_vol_bo / _avg_vol_bo) if _avg_vol_bo and _avg_vol_bo > 0 else 0
                    if _cur_close > _max_12h and _vs_bo >= 2.0:
                        analysis['strength'] = min(1.0, analysis['strength'] + 0.15)
                        analysis['reason'] += f' [+breakout_12h max={_max_12h:.4f} vol={_vs_bo:.1f}x]'
                        analysis['breakout_12h'] = True
                        analysis['vol_spike'] = round(_vs_bo, 2)
                        logger.info(f"Breakout 12h DETECTED {symbol}: close={_cur_close:.4f} > max12h={_max_12h:.4f}, vol={_vs_bo:.1f}x → +0.15 strength")
                    else:
                        analysis.setdefault('vol_spike', round(_vs_bo, 2))
                except Exception:
                    pass

            # ── Time-of-day filter for NARRATIVE tokens ───────────────────────
            if analysis['signal'] == 'BUY':
                try:
                    from datetime import datetime as _dtnow, timezone as _tz
                    _hour_utc = _dtnow.now(_tz.utc).hour
                    _narrative_syms = set()
                    try:
                        from config import Settings as _S
                        _narrative_syms = set(_S.__fields__['narrative_tokens'].default)
                    except Exception:
                        _narrative_syms = {
                            'FETUSDT','TAOUSDT','RENDERUSDT','NEARUSDT','WLDUSDT','AGIXUSDT','OCEANUSDT',
                            'ARBUSDT','MATICUSDT','ATOMUSDT','INJUSDT','APTUSDT','SUIUSDT','SEIUSDT',
                            'TIAUSDT','PYTHUSDT','JTOUSDT','BRETTUSDT','EIGENUSDT',
                        }
                    if symbol in _narrative_syms:
                        _avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else None
                        _last_vol = volumes[-1] if volumes else 0
                        _vs = (_last_vol / _avg_vol) if _avg_vol and _avg_vol > 0 else 0
                        if 0 <= _hour_utc < 8 and _vs < 1.2:
                            analysis['strength'] = max(0.0, analysis['strength'] * 0.85)
                            analysis['reason'] += f' [-low-vol night {_hour_utc:02d}h UTC ×0.85]'
                            logger.debug(f"Time-of-day penalty {symbol}: UTC hour={_hour_utc}, vol_spike={_vs:.2f}")
                        # M5: boost during peak liquidity hours (12-22 UTC)
                        elif 12 <= _hour_utc < 22 and _vs >= 1.3:
                            analysis['strength'] = min(1.0, analysis['strength'] * 1.10)
                            analysis['reason'] += f' [+peak_hours {_hour_utc:02d}h UTC ×1.10]'
                            logger.debug(f"Peak-hours BOOST {symbol}: UTC hour={_hour_utc}, vol_spike={_vs:.2f}")
                except Exception:
                    pass

            # ── Social trending boost (Fase 2 — activa solo con CRYPTOCOMPARE_ENABLED) ──
            if news_client and analysis['signal'] == 'BUY':
                try:
                    if news_client.is_social_trending(symbol):
                        analysis['strength'] = min(1.0, analysis['strength'] + 0.10)
                        analysis['reason'] += ' [+social trending]'
                        logger.info(f"Social trending BOOST {symbol}: strength → {analysis['strength']:.2f}")
                except Exception:
                    pass

            # ── News sentiment penalty (Fase 3 — activa siempre) ─────────────────────
            if news_client and analysis['signal'] == 'BUY':
                try:
                    recent_news = news_client.get_recent_news([symbol], since_hours=12)
                    neg_count = sum(1 for a in recent_news if a.get('sentiment') == 'negative')
                    if neg_count >= 2:
                        analysis['strength'] = max(0.0, analysis['strength'] - 0.10)
                        analysis['reason'] += f' [-{neg_count} neg news]'
                        logger.info(f"News sentiment PENALTY {symbol}: {neg_count} negative articles")
                except Exception:
                    pass

            # ── Store in cache ────────────────────────────────────────────────
            AutonomousSignalGenerator._cache_signal(symbol, analysis)

            return analysis

        except Exception as e:
            logger.error(f"Error analyzing {symbol}: {e}")
            return {
                'symbol': symbol, 'signal': 'HOLD', 'strength': 0,
                'reason': f'Analysis error: {str(e)}', 'indicators': {},
            }
    
    def generate_signals(self, interval: str = '1h',
                         use_multi_timeframe: bool = True,
                         news_client=None) -> List[Dict]:
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
                                             use_multi_timeframe=use_multi_timeframe,
                                             news_client=news_client)
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
                            limit: int = 5, use_multi_timeframe: bool = True,
                            news_client=None, regime_mode: str = 'SPOT_BULL') -> List[Dict]:
        """
        Get top BUY signals, filtered by min_strength.

        Args:
            interval: Primary kline interval
            min_strength: Minimum signal strength to include
            limit: Maximum number of signals to return
            use_multi_timeframe: Apply 4h trend filter (recommended)
            regime_mode: Active market regime — adjusts ranking weights:
                FUTURES_SHORT → rank by RSI (high = overbought = better short) + vol spike
                RANGE_SCALP   → rank by bb_lower_touch + RSI low
                default       → rank by strength (original behavior)

        Returns:
            List of top BUY signal dicts
        """
        all_signals = self.generate_signals(interval, use_multi_timeframe=use_multi_timeframe, news_client=news_client)
        logger.info(f"Total signals generated: {len(all_signals)}")
        logger.info(
            f"Signal breakdown: "
            f"{[(s['symbol'], s['signal'], round(s['strength'],2), s.get('higher_tf_trend','?')) for s in all_signals]}"
        )

        # BUG-E10: In FUTURES_SHORT mode, include SELL/HOLD signals as short candidates
        # (overbought symbols generating SELL/HOLD are better shorts than BUY signals)
        if regime_mode == 'FUTURES_SHORT':
            _short_min_strength = min(min_strength, 0.4)
            buy_signals = [
                s for s in all_signals
                if s['strength'] >= (min_strength if s['signal'] == 'BUY' else _short_min_strength)
            ]
            logger.info(
                f"[FUTURES_SHORT] Short candidates: {len(buy_signals)} "
                f"(BUY+SELL+HOLD with strength>={_short_min_strength})"
            )
        else:
            buy_signals = [
                s for s in all_signals
                if s['signal'] == 'BUY' and s['strength'] >= min_strength
            ]
        logger.info(f"Buy signals above threshold {min_strength}: {len(buy_signals)}")

        # ── Regime-aware ranking ─────────────────────────────────────────────
        if regime_mode == 'FUTURES_SHORT':
            # Rank by RSI descending (overbought = best short candidate)
            # SELL signals get RSI bonus (+15) to rank above neutral BUY signals
            def _short_score(s):
                rsi = float(s.get('rsi', 50) or 50)
                vol_spike = float(s.get('vol_spike', 1.0) or 1.0)
                sell_bonus = 15.0 if s.get('signal') == 'SELL' else 0.0
                return (rsi + sell_bonus) * 0.7 + min(vol_spike, 3.0) * 10 * 0.3
            buy_signals.sort(key=_short_score, reverse=True)
            logger.info(f"[Regime] FUTURES_SHORT ranking applied (RSI+SELL-weighted)")
        elif regime_mode == 'RANGE_SCALP':
            # Rank by bb_lower_touch first, then by RSI ascending (most oversold)
            def _scalp_score(s):
                bb_touch = 1 if s.get('bb_lower_touch') else 0
                rsi = float(s.get('rsi', 50) or 50)
                return bb_touch * 100 + (100 - rsi)
            buy_signals.sort(key=_scalp_score, reverse=True)
            logger.info(f"[Regime] RANGE_SCALP ranking applied (BB-lower + low RSI)")
        else:
            buy_signals.sort(key=lambda s: s.get('strength', 0), reverse=True)

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
