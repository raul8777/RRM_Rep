"""
Autonomous Signal Generation Module
Generates trading signals automatically using technical analysis from Binance data.
"""

from datetime import datetime, timezone
from typing import List, Dict, Optional
from binance_client import BinanceClient
from technical_indicators import TechnicalIndicators
import peak_quality
from external_data_client import get_external_data_client
from technical_graph_analyst import extract_graph_features
import logging

logger = logging.getLogger(__name__)

# ── Per-symbol signal cache (TTL dinámico desde settings) ───────────────────
SIGNAL_CACHE_TTL_SECONDS = 240   # P1.3: 600→240s (4 min default = 1 ciclo FUTURES_SHORT)
_signal_cache: Dict[str, Dict] = {}  # {symbol: {"signal": ..., "cached_at": datetime}}


def _get_signal_ttl(settings=None) -> int:
    """Return signal cache TTL from settings (P1.3) or module default."""
    if settings is not None:
        return int(getattr(settings, 'signal_cache_ttl_seconds', SIGNAL_CACHE_TTL_SECONDS))
    return SIGNAL_CACHE_TTL_SECONDS


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
        self._btc_1h_klines = None
    
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
                limit=limit,
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
            _err_cls = type(e).__name__
            if _err_cls in ('ReadTimeout', 'ConnectTimeout', 'ConnectionError', 'ReadTimeoutError'):
                logger.warning(f"Klines network timeout for {symbol} ({_err_cls}) — returning empty")
            else:
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
    def _get_cached_signal(symbol: str, settings=None) -> Optional[Dict]:
        """Return cached signal if still valid (within TTL from settings), else None."""
        entry = _signal_cache.get(symbol)
        if not entry:
            return None
        age = (datetime.now(timezone.utc) - entry["cached_at"]).total_seconds()
        ttl = _get_signal_ttl(settings)
        if age < ttl:
            logger.debug(f"Cache hit for {symbol} (age={age:.0f}s ttl={ttl}s)")
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

    def analyze_range_scalp_timing(self, symbol: str) -> Dict:
        klines = self.fetch_klines(symbol, '15m', limit=100)
        if len(klines) < 30:
            return {"interval": "15m", "data_complete": False, "reason": "insufficient_klines"}
        closes = [k['close'] for k in klines]
        highs = [k['high'] for k in klines]
        lows = [k['low'] for k in klines]
        volumes = [k['volume'] for k in klines]
        analysis = TechnicalIndicators.analyze_signal(closes, highs, lows, volumes)
        rsi_values = TechnicalIndicators.rsi(closes, 14)
        upper, middle, lower = TechnicalIndicators.bollinger_bands(closes, period=20, std_dev=2.0)
        span = upper[-1] - lower[-1] if upper and lower else 0.0
        volume_base = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else 0.0
        return {
            "interval": "15m",
            "data_complete": bool(rsi_values and upper and middle and lower and span > 0 and volume_base > 0),
            "timestamp": klines[-1]['close_time'],
            "signal": analysis.get('signal'),
            "strength": analysis.get('strength'),
            "rsi_15m": round(float(rsi_values[-1]), 4) if rsi_values else None,
            "bb_position_15m": round(float((closes[-1] - lower[-1]) / span), 4) if span > 0 else None,
            "vol_spike_15m": round(volumes[-1] / volume_base, 4) if volume_base > 0 else None,
            "return_15m_pct": round((closes[-1] / closes[-2] - 1.0) * 100.0, 4) if closes[-2] > 0 else None,
            "return_1h_pct": round((closes[-1] / closes[-5] - 1.0) * 100.0, 4) if closes[-5] > 0 else None,
        }

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
            if not isinstance(analysis.get('indicators'), dict):
                analysis['indicators'] = {}

            # ── Load learned indicator weights (GAP-A) ────────────────────────
            # Scale boosts/penalties proportionally to the empirical importance of
            # each indicator for this symbol. Falls back to 1.0 (no change) on
            # bootstrap or error — existing behavior is fully preserved.
            _lw_scale = {
                'rsi': 1.0, 'bb': 1.0, 'vol_spike': 1.0,
                'trend_4h': 1.0, 'macd': 1.0, 'ema': 1.0,
            }
            try:
                from learning_engine import get_learning_engine as _get_le
                _regime_lw = getattr(self, '_regime_mode', None) or '__any__'
                _lw = _get_le().get_weights(symbol, regime=_regime_lw)
                _DEFAULT_LW = {
                    'rsi': 0.12, 'bb': 0.08, 'vol_spike': 0.15,
                    'trend_4h': 0.15, 'macd': 0.10, 'ema': 0.08,
                }
                _is_bootstrap = all(abs(_lw.get(k, v) - v) < 1e-6 for k, v in _DEFAULT_LW.items())
                if not _is_bootstrap:
                    for _k, _dv in _DEFAULT_LW.items():
                        _lw_scale[_k] = _lw.get(_k, _dv) / _dv if _dv > 0 else 1.0
                    logger.debug(f"[Weights] {symbol}/{_regime_lw}: {_lw_scale}")
            except Exception:
                pass

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
                        _rsi_pen = round(0.08 * _lw_scale.get('rsi', 1.0), 4)
                        analysis['strength'] = max(0.0, analysis['strength'] - _rsi_pen)
                        analysis['reason'] += ' [-bearish RSI div]'
                        logger.debug(f"Bearish RSI divergence {symbol}: price↑ RSI↓ (pen={_rsi_pen:.3f})")
                    # Bullish divergence: price down but RSI up → potential reversal
                    elif _price_lo and not _rsi_lo and analysis['signal'] == 'BUY':
                        _rsi_boost = round(0.05 * _lw_scale.get('rsi', 1.0), 4)
                        analysis['strength'] = min(1.0, analysis['strength'] + _rsi_boost)
                        analysis['reason'] += ' [+bullish RSI div]'
                        logger.debug(f"Bullish RSI divergence {symbol}: price↓ RSI↑ (boost={_rsi_boost:.3f})")
            except Exception:
                pass

            # ── Multi-timeframe filter ────────────────────────────────────────
            higher_trend = 'FLAT'
            if use_multi_timeframe:
                higher_trend = self._get_trend_from_higher_tf(symbol, '4h')
                logger.info(f"Higher-TF (4h) trend for {symbol}: {higher_trend}")

                # s72 A4: In RANGE_SCALP, 4h DOWN is normal (price at low end of range) — skip penalty
                _regime_ctx = getattr(self, '_regime_mode', None) or getattr(self, 'regime_mode', None)
                _is_range_scalp = str(_regime_ctx or '').upper() == 'RANGE_SCALP'
                if analysis['signal'] == 'BUY' and higher_trend == 'DOWN' and not _is_range_scalp:
                    _4h_penalty = max(0.30, 1.0 - 0.50 * _lw_scale.get('trend_4h', 1.0))
                    analysis['strength'] = max(0.0, analysis['strength'] * _4h_penalty)
                    analysis['reason'] += f' [4h trend DOWN ×{_4h_penalty:.2f}]'
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
                _vs_boost = 1.0 + 0.15 * _lw_scale.get('vol_spike', 1.0)
                analysis['strength'] = min(1.0, analysis['strength'] * _vs_boost)
                analysis['reason'] += f' [+vol spike ×{_vs_boost:.2f}]'

            analysis['symbol']           = symbol
            analysis['interval']         = interval
            analysis['timestamp']        = klines[-1]['close_time']
            analysis['current_price']    = closes[-1]
            analysis['higher_tf_trend']  = higher_trend
            analysis['volume_confirmed'] = volume_confirmed

            # ── Universal-model feature fixes (GAP-U) ─────────────────────────
            # The universal quality model was trained on rsi_1h, rsi_4h,
            # range_position, trend_1h and trend_4h. Expose them live so the
            # model does not fall back to neutral defaults.
            try:
                _rsi_1h_vals = TechnicalIndicators.rsi(closes, 14)
                if _rsi_1h_vals:
                    analysis['rsi_1h'] = round(float(_rsi_1h_vals[-1]), 4)
            except Exception:
                pass

            try:
                _4h_klines = self.fetch_klines(symbol, '4h', limit=80)
                if _4h_klines and len(_4h_klines) >= 30:
                    _4h_closes = [k['close'] for k in _4h_klines]
                    _rsi_4h_vals = TechnicalIndicators.rsi(_4h_closes, 14)
                    if _rsi_4h_vals:
                        analysis['rsi_4h'] = round(float(_rsi_4h_vals[-1]), 4)
            except Exception:
                pass

            try:
                _lookback = min(168, len(closes))
                _recent_closes = closes[-_lookback:]
                _range_low = min(_recent_closes)
                _range_high = max(_recent_closes)
                if _range_high > _range_low:
                    analysis['range_position'] = round((closes[-1] - _range_low) / (_range_high - _range_low), 4)
                else:
                    analysis['range_position'] = 0.5
            except Exception:
                analysis.setdefault('range_position', 0.5)

            try:
                _ema_f, _ema_s, _ = TechnicalIndicators.ema_cross(closes, fast=9, slow=21)
                if _ema_f and _ema_s and _ema_f[-1] and _ema_s[-1]:
                    analysis['trend_1h'] = 1.0 if _ema_f[-1] > _ema_s[-1] else -1.0
            except Exception:
                pass

            try:
                analysis['trend_4h'] = 1.0 if str(higher_trend).upper() == 'UP' else (
                    -1.0 if str(higher_trend).upper() == 'DOWN' else 0.0
                )
            except Exception:
                pass

            # ── Support/Resistance features (shadow, no-look-ahead) ───────────
            try:
                from support_resistance import compute_sr_features as _compute_sr
                _atr_for_sr = analysis.get('indicators', {}).get('atr')
                _sr = _compute_sr(
                    closes, highs, lows, volumes,
                    current_price=closes[-1],
                    atr=_atr_for_sr,
                    lookback=min(96, len(closes)),
                )
                for _k, _v in _sr.items():
                    analysis.setdefault(_k, _v)
                # Model feature aliases
                _cp = float(closes[-1])
                if analysis.get('support_level') is not None and _cp > 0:
                    analysis.setdefault(
                        'sr_support_dist_pct',
                        round((_cp - float(analysis['support_level'])) / _cp * 100.0, 6)
                    )
                if analysis.get('resistance_level') is not None and _cp > 0:
                    analysis.setdefault(
                        'sr_resistance_dist_pct',
                        round((float(analysis['resistance_level']) - _cp) / _cp * 100.0, 6)
                    )
                if 'sr_zone_position' not in analysis:
                    analysis.setdefault('sr_zone_position', 0.5)
            except Exception as _sr_err:
                logger.debug(f"SR features failed for {symbol}: {_sr_err}")

            # ── SR entry advisory (no-look-ahead) ─────────────────────────────
            # Reject/dampen BUY signals that are already pressing against the
            # nearest resistance zone. Operates in shadow unless configured active.
            if analysis.get('signal') == 'BUY':
                try:
                    _cfg = getattr(self, '_settings', None) or getattr(self, 'settings', None)
                    _sr_mode = getattr(_cfg, 'sr_entry_filter_mode', 'shadow') if _cfg is not None else 'shadow'
                    _sr_zone = analysis.get('sr_zone_position')
                    _res_dist = analysis.get('nearest_resistance_dist_pct')
                    _atr = analysis.get('indicators', {}).get('atr')
                    _atr_pct = (_atr / analysis['current_price'] * 100.0) if _atr and analysis.get('current_price') else None
                    _at_resistance = (
                        (_sr_zone is not None and _sr_zone > 0.85)
                        or (_res_dist is not None and _atr_pct is not None and _res_dist <= _atr_pct)
                    )
                    if _at_resistance:
                        if _sr_mode == 'active':
                            analysis['signal'] = 'HOLD'
                            analysis['strength'] = 0
                            analysis['reason'] += f' [SR entry rejected: zone={_sr_zone:.2f}, res_dist={_res_dist:.2f}%]'
                            logger.info(f"SR entry REJECTED {symbol}: zone={_sr_zone:.2f}, res_dist={_res_dist:.2f}%")
                        else:
                            analysis['sr_entry_shadow_rejected'] = True
                            analysis['reason'] += f' [SR shadow reject: zone={_sr_zone:.2f}, res_dist={_res_dist:.2f}%]'
                            logger.debug(f"SR entry SHADOW-REJECTED {symbol}: zone={_sr_zone:.2f}, res_dist={_res_dist:.2f}%")
                except Exception:
                    pass

            # ── SR event entry signals ────────────────────────────────────────────
            # A confirmed bounce at support or breakout above resistance becomes an
            # actionable BUY; a breakdown below support becomes an actionable SELL.
            if analysis.get('signal') != 'BUY':
                _sr_zone_pos = float(analysis.get('sr_zone_position', 0.5))
                if analysis.get('support_bounce_buy') and _sr_zone_pos <= 0.45:
                    analysis['signal'] = 'BUY'
                    analysis['strength'] = max(float(analysis.get('strength', 0.5)), 0.65)
                    analysis['reason'] += ' [SR bounce buy]'
                    logger.info(f"SR BOUNCE BUY {symbol}: zone={_sr_zone_pos:.2f}")
                elif analysis.get('support_breakout_buy') and _sr_zone_pos >= 0.55:
                    analysis['signal'] = 'BUY'
                    analysis['strength'] = max(float(analysis.get('strength', 0.5)), 0.70)
                    analysis['reason'] += ' [SR breakout buy]'
                    logger.info(f"SR BREAKOUT BUY {symbol}: zone={_sr_zone_pos:.2f}")
            if analysis.get('signal') != 'SELL':
                _sr_zone_pos = float(analysis.get('sr_zone_position', 0.5))
                # Breakdown is a continuation SELL: price already closed below a
                # confirmed support level, which only happens in the LOWER part
                # of the range (mirror of support_breakout_buy's zone>=0.55
                # gate). The previous `>= 0.55` gate here was backwards — it
                # required the breakdown to also be near resistance, which is
                # geometrically near-impossible (breakdown implies zone near 0)
                # and is why support_breakdown_sell never promoted a signal.
                if analysis.get('support_breakdown_sell') and _sr_zone_pos <= 0.45:
                    analysis['signal'] = 'SELL'
                    analysis['strength'] = max(float(analysis.get('strength', 0.5)), 0.65)
                    analysis['reason'] += ' [SR breakdown sell]'
                    logger.info(f"SR BREAKDOWN SELL {symbol}: zone={_sr_zone_pos:.2f}")
                elif analysis.get('resistance_bounce_sell') and _sr_zone_pos >= 0.55:
                    # Rejection SELL: mirror of support_bounce_buy — a reversal
                    # candle confirmed near resistance becomes an actionable SELL.
                    analysis['signal'] = 'SELL'
                    analysis['strength'] = max(float(analysis.get('strength', 0.5)), 0.65)
                    analysis['reason'] += ' [SR rejection sell]'
                    logger.info(f"SR REJECTION SELL {symbol}: zone={_sr_zone_pos:.2f}")

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
                        # s70: tolerance configurable from settings; default 0.5% for SPOT_BULL
                        _s70_cfg = getattr(self, '_settings', None) or getattr(self, 'settings', None)
                        _bb_tol_default = getattr(_s70_cfg, 'bb_lower_touch_tolerance_pct', 0.5) / 100.0 if _s70_cfg else 0.005
                        _bb_lower_touch = _last_price <= _bbl[-1] * (1.0 + _bb_tol_default)
                        analysis['bb_lower_touch'] = _bb_lower_touch
                        # s70: bb_near_lower — wider tolerance for RANGE_SCALP (1.5% default)
                        _bb_tol_rs = getattr(_s70_cfg, 'bb_lower_touch_tolerance_range_scalp_pct', 1.5) / 100.0 if _s70_cfg else 0.015
                        analysis['bb_near_lower'] = _last_price <= _bbl[-1] * (1.0 + _bb_tol_rs)
                        analysis['bb_bandwidth'] = round(_bw, 4)
                        analysis['bb_lower'] = _bbl[-1]
                        analysis['bb_upper'] = _bbu[-1]
                        analysis['bb_middle'] = _bbm[-1]
                        # s72 A5: bb_position = relative position in range (0=lower band, 1=upper band)
                        _bb_range = _bbu[-1] - _bbl[-1]
                        analysis['bb_position'] = round((_last_price - _bbl[-1]) / _bb_range, 4) if _bb_range > 0 else 0.5
                        # A4: BB squeeze breakout — prior compression + current vol spike
                        if len(_bbu) >= 5:
                            _prev_bw = [(_bbu[-i] - _bbl[-i]) / _bbm[-i] if _bbm[-i] > 0 else 0.05
                                        for i in range(2, 6)]  # look back 4 bars
                            _was_compressed = all(b < 0.04 for b in _prev_bw)  # compressed last 4 bars
                            _vol_avg = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else None
                            _vol_spike_now = (volumes[-1] / _vol_avg) if _vol_avg and _vol_avg > 0 else 0
                            if _was_compressed and _vol_spike_now >= 1.5 and analysis['signal'] == 'BUY':
                                _bb_sq_boost = round(0.12 * _lw_scale.get('bb', 1.0), 4)
                                analysis['strength'] = min(1.0, analysis['strength'] + _bb_sq_boost)
                                analysis['reason'] += f' [+BB_squeeze_breakout bw={_bw:.3f} vol={_vol_spike_now:.1f}x +{_bb_sq_boost:.3f}]'
                                logger.info(f"BB Squeeze BREAKOUT {symbol}: bw={_bw:.3f}, vol={_vol_spike_now:.1f}x → +{_bb_sq_boost:.3f} strength")
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
                        _bo_boost = round(0.15 * _lw_scale.get('vol_spike', 1.0), 4)
                        analysis['strength'] = min(1.0, analysis['strength'] + _bo_boost)
                        analysis['reason'] += f' [+breakout_12h max={_max_12h:.4f} vol={_vs_bo:.1f}x +{_bo_boost:.3f}]'
                        analysis['breakout_12h'] = True
                        analysis['vol_spike'] = round(_vs_bo, 2)
                        logger.info(f"Breakout 12h DETECTED {symbol}: close={_cur_close:.4f} > max12h={_max_12h:.4f}, vol={_vs_bo:.1f}x → +{_bo_boost:.3f} strength")
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

            # ── Peak-quality features (used by peak_quality predictor) ───────
            try:
                if self._btc_1h_klines is None:
                    self._btc_1h_klines = self.fetch_klines('BTCUSDT', '1h', 200) or []
                analysis['peak_quality_features'] = peak_quality.compute_peak_features(
                    klines=klines,
                    signal_meta=analysis,
                    btc_klines=self._btc_1h_klines,
                )
            except Exception as _pq_err:
                logger.debug(f"peak_quality features failed for {symbol}: {_pq_err}")
                analysis['peak_quality_features'] = {}

            # ── C10: technical chart analyst features ────────────────────────
            try:
                _graph = extract_graph_features(klines)
                analysis.update(_graph)
                analysis['indicators'].update(_graph)
            except Exception as _graph_err:
                logger.debug(f"technical_graph_analyst failed for {symbol}: {_graph_err}")

            # ── C9: optional external data enrichments (fail-open) ───────────
            try:
                _cfg = getattr(self, '_settings', None) or getattr(self, 'settings', None)
                _external = get_external_data_client(settings=_cfg).get_market_features(symbol)
                analysis.update(_external)
                analysis['indicators'].update({
                    'funding_rate_annualized': _external.get('funding_rate_annualized'),
                    'oi_change_1h_pct': _external.get('oi_change_1h_pct'),
                    'coinbase_premium_pct': _external.get('coinbase_premium_pct'),
                    'liquidation_imbalance_1h': _external.get('liquidation_imbalance_1h'),
                })
            except Exception as _ext_err:
                logger.debug(f"external data enrichment failed for {symbol}: {_ext_err}")

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

    def get_top_buy_signals(self, interval: str = '1h', min_strength: float = None,
                            limit: int = 5, use_multi_timeframe: bool = True,
                            news_client=None, regime_mode: str = 'SPOT_BULL',
                            settings=None, profile_label: str = None) -> List[Dict]:
        """
        Get top BUY signals, filtered by min_strength.

        Args:
            interval: Primary kline interval
            min_strength: Minimum signal strength to include. When None, reads from
                settings.min_signal_strength (via get_param) or falls back to 0.6.
            limit: Maximum number of signals to return
            use_multi_timeframe: Apply 4h trend filter (recommended)
            regime_mode: Active market regime — adjusts ranking weights:
                FUTURES_SHORT → rank by RSI (high = overbought = better short) + vol spike
                RANGE_SCALP   → rank by bb_lower_touch + RSI low
                PRESERVATION  → defensive ranking (quality/confirmation first)
                SPOT_BULL     → trend-following ranking (strength first)
            settings: Optional settings object — used to read min_signal_strength.
            profile_label: Active profile A/B/C — adjusts effective min_strength:
                A (aggressive) → -0.05 (more permissive)
                C (defensive)  → +0.05 (more selective)
                B or None      → no adjustment

        Returns:
            List of top BUY signal dicts
        """
        # BUG-5: resolve min_strength from settings instead of using stale hardcode
        if min_strength is None:
            if settings is not None:
                try:
                    from config_helpers import get_param as _gp
                    min_strength = float(_gp(settings, 'min_signal_strength'))
                except Exception:
                    min_strength = 0.6
            else:
                min_strength = 0.6

        # GAP-D: profile-aware threshold adjustment
        _profile = str(profile_label or '').upper().strip()
        if _profile == 'A':
            min_strength = max(0.40, min_strength - 0.05)
        elif _profile == 'C':
            min_strength = min(0.90, min_strength + 0.05)

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
            for _sig in buy_signals:
                _sig['expected_side'] = 'SHORT'
                _sig['trade_type'] = 'futures'
                _sig['routing_intent'] = 'FUTURES_SHORT'
            logger.info(
                f"[{regime_mode}] SHORT-first candidates: {len(buy_signals)} "
                f"(BUY+SELL+HOLD with strength>={_short_min_strength})"
            )
        elif regime_mode == 'RANGE_SCALP':
            _short_min_strength = min(min_strength, 0.4)
            buy_signals = [
                s for s in all_signals
                if s['strength'] >= (min_strength if s['signal'] == 'BUY' else _short_min_strength)
            ]
            short_count = 0
            for _sig in buy_signals:
                # s106b: explicit None-check, not `or` — 0.0 is a legitimate value
                # (e.g. price sitting exactly at support) that `x or default`
                # would silently treat as missing and replace with the neutral
                # default, corrupting the strongest possible SR/RSI signal.
                _zone_raw = _sig.get('sr_zone_position')
                _zone = float(_zone_raw) if _zone_raw is not None else 0.5
                _rsi_raw = _sig.get('rsi')
                _rsi = float(_rsi_raw) if _rsi_raw is not None else 50.0
                _short_setup = bool(
                    _sig.get('signal') in ('SELL', 'HOLD')
                    or _sig.get('support_breakdown_sell') or _sig.get('sr_breakdown')
                    or _zone >= 0.65
                    or _sig.get('bb_upper_touch') or _sig.get('bb_near_upper')
                    or _rsi >= 60.0
                )
                if _short_setup:
                    _sig['expected_side'] = 'SHORT'
                    _sig['trade_type'] = 'futures'
                    _sig['routing_intent'] = 'FUTURES_SHORT'
                    short_count += 1
                else:
                    _sig['expected_side'] = 'LONG'
                    _sig['trade_type'] = 'futures'
                    _sig['routing_intent'] = 'FUTURES_LONG_ANALYSIS'
            logger.info(
                f"[RANGE_SCALP] futures-short cohort={short_count} "
                f"futures-long-analysis cohort={len(buy_signals) - short_count}"
            )
        else:
            buy_signals = [
                s for s in all_signals
                if s['signal'] == 'BUY' and s['strength'] >= min_strength
            ]
            for _sig in buy_signals:
                _sig['expected_side'] = 'LONG'
                _sig['trade_type'] = 'spot'
        logger.info(f"Buy signals above threshold {min_strength}: {len(buy_signals)}")

        # ── Entry-profile gate: A/B/C profile assignment based on realized outcomes ─
        # The legacy peak_quality model is superseded by entry_profile_gate; the feature
        # dict is still attached for diagnostics but we no longer depend on the joblib
        # model (which is frequently absent and generates noisy warnings).
        _pq_regime = str(regime_mode or 'SPOT_BULL').strip().upper()
        for _sig in buy_signals:
            if _pq_regime == 'RANGE_SCALP':
                try:
                    from range_scalp_data_contract import attach_snapshot
                    attach_snapshot(_sig)
                except Exception as _contract_err:
                    logger.warning(f"RANGE_SCALP feature contract failed for {_sig.get('symbol')}: {_contract_err}")
            # New calibrated entry-profile gate: A/B/C based on realized outcomes.
            # If the signal cannot reach profile C it is marked as discarded.
            try:
                import entry_profile_gate
                _profile, _pinfo = entry_profile_gate.classify(
                    _sig, _pq_regime, _sig.get('expected_side', 'LONG'), _sig.get('symbol', ''),
                    range_scalp_model_mode=getattr(settings, 'range_scalp_quality_model_mode', 'shadow'),
                    range_scalp_model_path=getattr(settings, 'range_scalp_quality_model_path', None),
                )
                _sig['peak_quality_profile'] = _profile if _profile else 'discarded'
                _sig['peak_quality_score'] = _pinfo['score']
                _sig['entry_profile_gate_info'] = _pinfo
            except Exception as _gate_err:
                logger.warning(f"entry_profile_gate failed for {_sig.get('symbol')}: {_gate_err}")
                _sig.setdefault('peak_quality_profile', 'discarded')
                _sig.setdefault('peak_quality_score', 0.0)

        def _regime_signal_score(signal: Dict, regime: str) -> float:
            _regime = str(regime or 'SPOT_BULL').strip().upper()
            _strength = float(signal.get('strength', 0.0) or 0.0)
            _rsi = float(signal.get('rsi', 50.0) or 50.0)
            _vol_spike = float(signal.get('vol_spike', 1.0) or 1.0)
            _trend = str(signal.get('higher_tf_trend', 'FLAT') or 'FLAT').strip().upper()

            if _regime == 'FUTURES_SHORT':
                # Overbought bounces / breakdowns with volume are preferred short setups.
                _sell_bonus = 15.0 if signal.get('signal') == 'SELL' else 0.0
                _sr_breakdown_bonus = 25.0 if signal.get('support_breakdown_sell') or signal.get('sr_breakdown') else 0.0
                _trend_short_bonus = 2.0 if _trend == 'DOWN' else 0.0
                return (_rsi + _sell_bonus) * 0.7 + min(_vol_spike, 3.0) * 10.0 * 0.3 + _sr_breakdown_bonus + _trend_short_bonus

            if _regime == 'RANGE_SCALP':
                _routing_intent = str(signal.get('routing_intent') or '').upper()
                # s106b: explicit None-check, not `or` — see comment above.
                _zone_raw = signal.get('sr_zone_position')
                _zone = float(_zone_raw) if _zone_raw is not None else 0.5
                _short_score = (
                    120.0 * float(signal.get('signal') == 'SELL')
                    + 100.0 * float(bool(signal.get('support_breakdown_sell') or signal.get('sr_breakdown')))
                    + 80.0 * float(_zone >= 0.65)
                    + 70.0 * float(bool(signal.get('bb_upper_touch') or signal.get('bb_near_upper')))
                    + max(0.0, _rsi - 55.0) * 2.0
                    + 20.0 * float(_trend in ('DOWN', 'BEARISH', '-1', '-1.0'))
                    + min(_vol_spike, 3.0) * 5.0
                    + _strength
                )
                _bb_touch = 2 if signal.get('bb_lower_touch') else (1 if signal.get('bb_near_lower') else 0)
                _support_score = (
                    _bb_touch * 100.0
                    + (100.0 - _rsi)
                    + min(_vol_spike, 2.0)
                    + 80.0 * float(bool(signal.get('support_bounce_buy') or signal.get('sr_bounce')))
                    + _strength
                )
                return (
                    1 if _routing_intent == 'FUTURES_SHORT' else 0,
                    _short_score if _routing_intent == 'FUTURES_SHORT' else _support_score,
                )

            if _regime == 'PRESERVATION':
                # Defensive ranking: support bounce + quality/confirmation first.
                _volume_confirmed = 1.0 if signal.get('volume_confirmed') else 0.0
                _trend_bonus = 1.0 if _trend == 'UP' else (0.3 if _trend == 'FLAT' else -0.8)
                _rsi_distance = abs(_rsi - 50.0)
                _rsi_penalty = min(20.0, _rsi_distance) / 50.0
                _sr_bounce_bonus = 1.5 if signal.get('support_bounce_buy') or signal.get('sr_bounce') else 0.0
                return _strength * 2.0 + _volume_confirmed * 1.2 + _trend_bonus - _rsi_penalty + _sr_bounce_bonus

            # SPOT_BULL and unknown: confirmed breakout at resistance + trend + strength.
            _trend_bonus = 0.05 if _trend == 'UP' else (-0.05 if _trend == 'DOWN' else 0.0)
            _sr_bonus = 0.0
            if signal.get('support_breakout_buy') or signal.get('sr_breakout'):
                _sr_bonus = 0.5
            elif signal.get('support_bounce_buy') or signal.get('sr_bounce'):
                _sr_bonus = 0.3
            return _strength * 2.0 + min(_vol_spike, 2.5) * 0.05 + _trend_bonus + _sr_bonus

        _regime = str(regime_mode or 'SPOT_BULL').strip().upper() or 'SPOT_BULL'
        buy_signals.sort(key=lambda s: _regime_signal_score(s, _regime), reverse=True)
        logger.info(f"[Regime] {_regime} ranking applied (contract-v1)")
        selected = buy_signals[:limit]
        if _regime == 'RANGE_SCALP':
            for signal in selected:
                try:
                    signal['range_scalp_timing_shadow'] = self.analyze_range_scalp_timing(signal['symbol'])
                except Exception as exc:
                    signal['range_scalp_timing_shadow'] = {
                        "interval": "15m", "data_complete": False, "reason": str(exc),
                    }

        return selected

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
