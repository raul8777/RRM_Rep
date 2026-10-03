"""
PnL Predictor Module

Calculates expected PnL and success probability before opening positions
using technical indicators and market analysis.
"""

import hashlib
import json
import logging
import re
import traceback
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import numpy as np

logger = logging.getLogger(__name__)


_PREDICTOR_ERROR_CODES = {
    "input_contract": "invalid_input_contract",
    "learned_weights": "invalid_or_unavailable_weights",
    "probability": "probability_calculation_failed",
    "micro_momentum": "micro_momentum_calculation_failed",
    "price_range": "price_range_calculation_failed",
    "expected_pnl": "expected_pnl_calculation_failed",
    "cost_model": "cost_model_calculation_failed",
    "bias_correction": "bias_correction_failed",
    "risk_metrics": "risk_metrics_calculation_failed",
}


class PredictorInputError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _sanitize_exception_message(exc: Exception) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ")[:500]
    return re.sub(
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        message,
    )


def _exception_fingerprint(stage: str, exc: Exception) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    origin = frames[-1].name if frames else "unknown"
    identity = f"{stage}|{type(exc).__name__}|{origin}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _weights_fingerprint(weights: Dict) -> str:
    payload = json.dumps(weights or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    "sr_zone_position": 0.10, # position between support and resistance
    "sr_bounce":    0.06,   # confirmed bounce at support
    "sr_breakout":  0.06,   # confirmed breakout above resistance
    "sr_breakdown": 0.06,   # confirmed breakdown below support (shorts)
    "support_strength": 0.04, # historical volume/touches at support
    "bias_correction": 1.0,
}


class PnLPredictor:
    """Predicts expected PnL and success probability for trading positions."""

    def __init__(self):
        """Initialize PnL Predictor."""
        self._weight_cache: Dict[str, Dict] = {}  # (symbol, regime) -> weights
        self._weight_sources: Dict[Tuple[str, str], str] = {}
        logger.info("PnL Predictor initialized")

    def load_learned_weights(self, symbol: str, regime: str = '__any__') -> Dict:
        """
        Load indicator weights from LearningEngine for *symbol* + *regime*.
        Falls back to __global__ then defaults.  Results are in-process cached
        using a (symbol, regime) key so weights are invalidated on regime change.
        """
        _cache_key = (symbol, regime or '__any__')
        if _cache_key in self._weight_cache:
            return self._weight_cache[_cache_key]
        try:
            from learning_engine import get_learning_engine
            weights = get_learning_engine().get_weights(symbol, regime=regime)
            self._weight_sources[_cache_key] = "learned"
        except Exception as exc:
            weights = _DEFAULT_WEIGHTS.copy()
            self._weight_sources[_cache_key] = "bootstrap_after_load_error"
            logger.exception(
                "predictor_weights_fallback code=weights_load_failed exception=%s fingerprint=%s symbol=%s regime=%s",
                type(exc).__name__,
                _exception_fingerprint("learned_weights", exc),
                symbol,
                regime,
            )
        self._weight_cache[_cache_key] = weights
        return weights

    def invalidate_cache(self, symbol: str = None, regime: str = None) -> None:
        """Clear weight cache so next predict_pnl loads fresh weights."""
        if symbol:
            _key = (symbol, regime or '__any__')
            self._weight_cache.pop(_key, None)
            self._weight_sources.pop(_key, None)
        else:
            self._weight_cache.clear()
            self._weight_sources.clear()

    @staticmethod
    def _validate_inputs(
        symbol: str,
        current_price: float,
        indicators: Dict,
        cycle_duration_minutes: int,
        position_size_usdt: float,
        trade_side: str,
        candles: Optional[Dict],
        interval_minutes: int,
    ) -> None:
        if not isinstance(symbol, str) or not symbol.strip():
            raise PredictorInputError("missing_required_field", "symbol is required")
        if not isinstance(indicators, dict):
            raise PredictorInputError("invalid_type", "indicators must be a dictionary")

        numeric_fields = {
            "current_price": current_price,
            "position_size_usdt": position_size_usdt,
            "cycle_duration_minutes": cycle_duration_minutes,
            "interval_minutes": interval_minutes,
        }
        for field, value in numeric_fields.items():
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise PredictorInputError("invalid_type", f"{field} must be numeric") from exc
            if not np.isfinite(number):
                raise PredictorInputError("non_finite_value", f"{field} must be finite")
            if number <= 0:
                raise PredictorInputError("invalid_range", f"{field} must be positive")

        side = str(trade_side or "").strip().upper()
        if side not in ("LONG", "SHORT"):
            raise PredictorInputError("unexpected_side", "trade_side must be LONG or SHORT")

        if candles is None:
            return
        if not isinstance(candles, dict):
            raise PredictorInputError("invalid_type", "candles must be a dictionary")

        required_series = ("high", "low", "close")
        if any(key not in candles for key in required_series):
            raise PredictorInputError("missing_required_field", "candles require high, low and close")
        try:
            series = {key: np.asarray(candles[key], dtype=float) for key in required_series}
            if "volume" in candles:
                series["volume"] = np.asarray(candles["volume"], dtype=float)
        except (TypeError, ValueError) as exc:
            raise PredictorInputError("invalid_type", "candle values must be numeric") from exc

        lengths = {len(values) for values in series.values()}
        if len(lengths) != 1:
            raise PredictorInputError("unequal_array_lengths", "candle arrays must have equal lengths")
        if not lengths or next(iter(lengths)) < 3:
            raise PredictorInputError("empty_or_insufficient_window", "at least three candles are required")
        if any(not np.all(np.isfinite(values)) for values in series.values()):
            raise PredictorInputError("non_finite_value", "candle values must be finite")

        highs, lows, closes = series["high"], series["low"], series["close"]
        if np.any(highs <= 0) or np.any(lows <= 0) or np.any(closes <= 0):
            raise PredictorInputError("invalid_range", "candle prices must be positive")
        if np.any(lows > highs) or np.any(closes < lows) or np.any(closes > highs):
            raise PredictorInputError("invalid_high_low_close", "candle OHLC geometry is invalid")
    
    def predict_pnl(
        self,
        symbol: str,
        current_price: float,
        indicators: Dict,
        cycle_duration_minutes: int = 5,
        position_size_usdt: float = 15.0,
        trade_type: str = 'spot',
        trade_side: str = 'LONG',
        settings = None,
        candles: Optional[Dict] = None,
        interval_minutes: int = 5,
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
            trade_side: 'LONG' or 'SHORT' — direction-aware scoring/EV
            settings: Settings object for commission rates
            candles: Optional OHLCV dict {'open':..., 'high':..., 'low':..., 'close':..., 'volume':...}
                from the target micro timeframe (e.g. 5m). When provided, the predictor
                uses the realized range/drift over the prediction horizon instead of
                extrapolating volatility from the 1h Bollinger Bands.
            interval_minutes: Candle interval in minutes (e.g. 5 for 5m candles).

        Returns:
            Dictionary with prediction results
        """
        error_stage = "input_contract"
        try:
            self._validate_inputs(
                symbol,
                current_price,
                indicators,
                cycle_duration_minutes,
                position_size_usdt,
                trade_side,
                candles,
                interval_minutes,
            )
            _regime_ctx = (settings and getattr(settings, 'current_regime_mode', '__any__')) or '__any__'
            error_stage = "learned_weights"
            weights = self.load_learned_weights(symbol, regime=_regime_ctx)
            weight_source = self._weight_sources.get((symbol, _regime_ctx), "unknown")
            weights_hash = _weights_fingerprint(weights)
            # Detect bootstrap: if all weights match defaults, no learning has happened
            _default_items = {k: v for k, v in _DEFAULT_WEIGHTS.items()}
            _weight_items  = {k: weights.get(k) for k in _DEFAULT_WEIGHTS}
            is_default = (_weight_items == _default_items)

            _side = str(trade_side or 'LONG').strip().upper()
            if _side not in ('LONG', 'SHORT'):
                _side = 'LONG'

            # Calculate success probability based on indicators + learned weights
            error_stage = "probability"
            success_probability = self._calculate_success_probability(indicators, weights, trade_side=_side, current_price=current_price)

            # Micro-momentum adjustment using real 1m/5m candles
            if candles and self._has_candles(candles):
                error_stage = "micro_momentum"
                micro_adj = self._micro_momentum_adjustment(
                    candles, interval_minutes, cycle_duration_minutes, _side
                )
                success_probability = max(0.0, min(1.0, success_probability + micro_adj))

            # Estimate price range for the cycle
            error_stage = "price_range"
            if candles and self._has_candles(candles):
                price_range = self._estimate_price_range_from_candles(
                    current_price,
                    candles,
                    cycle_duration_minutes,
                    interval_minutes=interval_minutes,
                    trade_side=_side,
                    indicators=indicators,
                )
            else:
                price_range = self._estimate_price_range(current_price, indicators, cycle_duration_minutes, trade_side=_side)

            # Calculate expected PnL
            error_stage = "expected_pnl"
            expected_pnl = self._calculate_expected_pnl(
                current_price,
                price_range,
                success_probability,
                position_size_usdt,
                trade_side=_side,
                settings=settings,
                trade_type=trade_type,
            )

            # Subtract Binance round-trip commission so expected_pnl is net
            error_stage = "cost_model"
            from binance_commission import expected_pnl_after_fees
            expected_pnl = expected_pnl_after_fees(expected_pnl, position_size_usdt, trade_type, settings)

            # Apply learned bias correction
            error_stage = "bias_correction"
            bias = weights.get("bias_correction", 1.0)
            expected_pnl = expected_pnl * bias

            # Calculate risk metrics
            error_stage = "risk_metrics"
            risk_metrics = self._calculate_risk_metrics(
                current_price,
                price_range,
                position_size_usdt,
                trade_side=_side,
                settings=settings,
                trade_type=trade_type,
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
                'trade_side': _side,
                'bootstrap_mode': is_default,
                'weights_source': weight_source,
                'weights_hash': weights_hash,
                'weights_regime': _regime_ctx,
            }
            
            logger.info(
                f"PnL Prediction for {symbol}: "
                f"Expected PnL={expected_pnl:.2f} ({prediction['expected_pnl_percentage']:.2f}%), "
                f"Success Prob={success_probability:.2f}"
            )
            
            return prediction
            
        except Exception as exc:
            error_code = getattr(
                exc,
                "code",
                _PREDICTOR_ERROR_CODES.get(error_stage, "unexpected_programming_error"),
            )
            exception_class = type(exc).__name__
            sanitized_message = _sanitize_exception_message(exc)
            stack_fingerprint = _exception_fingerprint(error_stage, exc)
            logger.exception(
                "predictor_failed stage=%s code=%s exception=%s fingerprint=%s symbol=%s",
                error_stage,
                error_code,
                exception_class,
                stack_fingerprint,
                symbol,
            )
            return {
                'symbol': symbol,
                'current_price': current_price,
                'expected_pnl': 0.0,
                'expected_pnl_percentage': 0.0,
                'success_probability': 0.0,
                'price_range': {'min': current_price * 0.95, 'max': current_price * 1.05},
                'risk_metrics': {},
                'indicators_used': indicators,
                'prediction_time': datetime.utcnow().isoformat(),
                'cycle_duration_minutes': cycle_duration_minutes,
                'position_size_usdt': position_size_usdt,
                'trade_side': str(trade_side or 'LONG').strip().upper() or 'LONG',
                'valid': False,
                'prediction_status': 'error',
                'error': sanitized_message,
                'error_stage': error_stage,
                'error_code': error_code,
                'exception_class': exception_class,
                'stack_fingerprint': stack_fingerprint,
            }
    
    def _calculate_success_probability(self, indicators: Dict, weights: Dict = None, trade_side: str = 'LONG', current_price: Optional[float] = None) -> float:
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

        def _sr_val(indicators: Dict, keys: List[str], default=None):
            for k in keys:
                if k in indicators:
                    return indicators[k]
                sr = indicators.get('support_resistance') or {}
                if isinstance(sr, dict) and k in sr:
                    return sr[k]
            return default

        probability = 0.5  # Base probability
        _is_short = str(trade_side or 'LONG').strip().upper() == 'SHORT'

        # ── RSI analysis ────────────────────────────────────────────────────
        rsi = indicators.get('rsi', {})
        if rsi:
            rsi_value = _num(rsi.get('value', 50), 50.0)
            if _is_short:
                if rsi_value > 70:
                    probability += w_rsi
                elif rsi_value > 60:
                    probability += w_rsi * 0.67
                elif rsi_value < 30:
                    probability -= w_rsi
                elif rsi_value < 40:
                    probability -= w_rsi * 0.67
            else:
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
            if _is_short:
                if macd_value < signal_value:
                    probability += w_macd
                if histogram < 0:
                    probability += w_macd * 0.33
                elif histogram > 0:
                    probability -= w_macd * 0.33
            else:
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
                probability += (-w_ema if _is_short else w_ema)
            else:
                probability += (w_ema if _is_short else -w_ema)

        # ── Bollinger Bands ─────────────────────────────────────────────────
        bollinger = indicators.get('bollinger_bands', {})
        if bollinger:
            upper  = _num(bollinger.get('upper', 0), 0.0)
            lower  = _num(bollinger.get('lower', 0), 0.0)
            middle = _num(bollinger.get('middle', 0), 0.0)
            if lower > 0 and middle > 0 and upper > lower:
                price_for_band = float(current_price) if current_price is not None else middle
                position_in_band = (price_for_band - lower) / (upper - lower)
                if _is_short:
                    if position_in_band > 0.8:
                        probability += w_bb
                    elif position_in_band < 0.2:
                        probability -= w_bb
                else:
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
                probability += (-w_trend_4h if _is_short else w_trend_4h)
            elif trend < 0:                      # bearish 4h context
                probability += (w_trend_4h if _is_short else -w_trend_4h * 0.80)

        # ── Weekly range position (buy at support, not resistance) ────────────
        wr_d = indicators.get('weekly_range', {})
        if wr_d:
            wr = _num(wr_d.get('value', 0.5), 0.5)
            if _is_short:
                if wr >= 0.85:
                    probability += w_weekly_rng      # near weekly resistance favors short
                elif wr <= 0.30:
                    probability -= w_weekly_rng * 0.90
            else:
                if wr <= 0.30:
                    probability += w_weekly_rng         # near weekly support
                elif wr >= 0.85:
                    probability -= w_weekly_rng * 0.90  # near weekly resistance

        # ── Support / Resistance levels and events ─────────────────────────
        w_sr_zone = weights.get("sr_zone_position", 0.10)
        w_sr_bounce = weights.get("sr_bounce", 0.06)
        w_sr_breakout = weights.get("sr_breakout", 0.06)
        w_sr_breakdown = weights.get("sr_breakdown", 0.06)
        w_sr_strength = weights.get("support_strength", 0.04)

        sr_zone = _num(_sr_val(indicators, ['sr_zone_position']), 0.5)
        sr_bounce = bool(_sr_val(indicators, ['support_bounce_buy', 'sr_bounce'], False))
        sr_breakout = bool(_sr_val(indicators, ['support_breakout_buy', 'sr_breakout'], False))
        sr_breakdown = bool(_sr_val(indicators, ['support_breakdown_sell', 'sr_breakdown'], False))
        support_strength = _num(_sr_val(indicators, ['support_strength']), 0.0)
        support_distance_atr = _num(_sr_val(indicators, ['support_distance_atr']), 999.0)

        strength_bonus = w_sr_strength * min(1.0, support_strength)
        if _is_short:
            if sr_breakdown:
                probability += w_sr_breakdown * 0.80 + strength_bonus
            if sr_zone >= 0.75:
                probability += w_sr_zone
            elif sr_zone <= 0.25:
                probability -= w_sr_zone * 0.80
            if support_distance_atr < 0.3:
                probability -= w_sr_zone * 0.40  # too close to support for short
        else:
            if sr_bounce:
                probability += w_sr_bounce * 0.80 + strength_bonus
            if sr_breakout:
                probability += w_sr_breakout * 0.60
            if sr_zone <= 0.25:
                probability += w_sr_zone
            elif sr_zone >= 0.75:
                probability -= w_sr_zone * 0.80
            if support_distance_atr < 0.3:
                probability += w_sr_zone * 0.40  # very close to support

        return max(0.0, min(1.0, probability))
    
    @staticmethod
    def _micro_momentum_adjustment(
        candles: Dict,
        interval_minutes: int,
        horizon_minutes: int,
        trade_side: str,
    ) -> float:
        """
        Compute a small probability adjustment (-0.04 .. +0.04) from the
        realized drift and volume profile over the prediction window.
        """
        closes = np.asarray(candles.get('close', []), dtype=float)
        volumes = np.asarray(candles.get('volume', []), dtype=float)
        if len(closes) < 3:
            return 0.0

        n = max(1, min(int(np.ceil(horizon_minutes / max(interval_minutes, 1))), len(closes)))
        recent = closes[-n:]
        drift = (recent[-1] - recent[0]) / recent[0] if recent[0] > 0 else 0.0

        adj = 0.0
        _is_short = str(trade_side or 'LONG').strip().upper() == 'SHORT'
        if _is_short:
            adj += -0.04 if drift > 0.001 else 0.02 if drift < -0.001 else 0.0
        else:
            adj += 0.04 if drift > 0.001 else -0.02 if drift < -0.001 else 0.0

        if len(volumes) >= n + 1:
            recent_vol = volumes[-n:].mean()
            older_vol = volumes[-(2 * n):-n].mean() if len(volumes) >= 2 * n else volumes[:n].mean()
            if older_vol > 0:
                vol_ratio = recent_vol / older_vol
                adj += 0.02 * min(max(vol_ratio - 1.0, -1.0), 1.0)

        return max(-0.06, min(0.06, adj))

    @staticmethod
    def _has_candles(candles: Dict) -> bool:
        """Check that an OHLCV dict contains enough non-empty arrays."""
        if not isinstance(candles, dict):
            return False
        for key in ('high', 'low', 'close'):
            arr = candles.get(key)
            if arr is None or len(arr) < 3:
                return False
        return True

    def _estimate_price_range_from_candles(
        self,
        current_price: float,
        candles: Dict,
        cycle_duration_minutes: int,
        interval_minutes: int = 5,
        trade_side: str = 'LONG',
        indicators: Optional[Dict] = None,
    ) -> Dict:
        """
        Estimate price range using realized OHLCV candles instead of ATR extrapolation.
        Uses the last N candles that fit inside the prediction horizon.
        """
        highs = np.asarray(candles.get('high', []), dtype=float)
        lows = np.asarray(candles.get('low', []), dtype=float)
        closes = np.asarray(candles.get('close', []), dtype=float)
        volumes = np.asarray(candles.get('volume', []), dtype=float)

        if len(closes) < 3:
            return self._estimate_price_range(current_price, indicators or {}, cycle_duration_minutes, trade_side=trade_side)

        n_candles = max(1, int(np.ceil(cycle_duration_minutes / max(interval_minutes, 1))))
        n_candles = min(n_candles, len(closes))

        recent_highs = highs[-n_candles:]
        recent_lows = lows[-n_candles:]
        recent_closes = closes[-n_candles:]
        recent_volumes = volumes[-n_candles:] if len(volumes) >= n_candles else None

        realized_range = max(recent_highs.max(), recent_closes.max()) - min(recent_lows.min(), recent_closes.min())
        realized_range = max(realized_range, current_price * 0.001)  # at least 10 bps

        # Net drift over the window (price direction)
        drift = recent_closes[-1] - recent_closes[0]

        # Realized volatility using log returns over the window
        log_prices = np.log(recent_closes)
        returns_log = np.diff(log_prices)
        realized_vol = float(np.std(returns_log)) if len(returns_log) > 1 else 0.0
        vol_amplitude = current_price * max(realized_vol * np.sqrt(n_candles), 0.001)

        # Volume confirmation: recent avg volume vs older window avg
        volume_boost = 0.0
        if recent_volumes is not None and len(recent_volumes) >= 3:
            recent_avg_vol = recent_volumes.mean()
            older = volumes[:-n_candles] if len(volumes) > n_candles * 2 else volumes[:n_candles]
            older_avg_vol = older.mean() if len(older) > 0 else recent_avg_vol
            if older_avg_vol > 0:
                volume_ratio = recent_avg_vol / older_avg_vol
                volume_boost = 0.2 * min(max(volume_ratio - 1.0, -1.0), 1.0)

        _is_short = str(trade_side or 'LONG').strip().upper() == 'SHORT'
        # Project max/min from current price using drift + half realized range, then expand with vol amplitude
        if _is_short:
            # SHORT wins when price falls
            max_price = current_price + max(0.0, drift) * 0.5 + realized_range * 0.3 + vol_amplitude
            min_price = current_price + min(0.0, drift) - realized_range * 0.7 - vol_amplitude
        else:
            max_price = current_price + max(0.0, drift) + realized_range * 0.7 + vol_amplitude
            min_price = current_price + min(0.0, drift) * 0.5 - realized_range * 0.3 - vol_amplitude

        # Soft cap to a realistic intraday move
        max_move_pct = min(0.05, max(0.005, (realized_range / current_price) * 2.0 + volume_boost))
        max_price = min(max_price, current_price * (1.0 + max_move_pct))
        min_price = max(min_price, current_price * (1.0 - max_move_pct))

        # Anchor to support/resistance when available
        indicators = indicators or {}
        sr_support = indicators.get('support_level')
        sr_resistance = indicators.get('resistance_level')
        if sr_support is None:
            sr = indicators.get('support_resistance') or {}
            sr_support = sr.get('support_level')
            sr_resistance = sr.get('resistance_level')
        if sr_support is not None and sr_support > 0:
            if _is_short:
                min_price = max(min_price, sr_support * 1.005)
            else:
                min_price = max(min_price, sr_support * 0.998)
        if sr_resistance is not None and sr_resistance > 0:
            if _is_short:
                max_price = min(max_price, sr_resistance * 1.002)
            else:
                max_price = min(max_price, sr_resistance * 0.995)

        return {
            'min': float(min_price),
            'max': float(max_price),
            'volatility': float(realized_range / current_price),
            'adjusted_volatility': float((max_price - min_price) / (2.0 * current_price)),
            'realized_range': float(realized_range),
            'drift': float(drift),
            'volume_boost': float(volume_boost),
            'candle_based': True,
        }

    def _estimate_price_range(
        self,
        current_price: float,
        indicators: Dict,
        cycle_duration_minutes: int,
        trade_side: str = 'LONG'
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

        # Anchor to nearest support/resistance when available.
        _is_short = str(trade_side or 'LONG').strip().upper() == 'SHORT'
        sr_support = indicators.get('support_level')
        sr_resistance = indicators.get('resistance_level')
        if sr_support is None:
            sr = indicators.get('support_resistance') or {}
            sr_support = sr.get('support_level')
            sr_resistance = sr.get('resistance_level')
        if sr_support is not None and sr_support > 0:
            if _is_short:
                # Stop above support for short; do not let min fall through strong support.
                min_price = max(min_price, sr_support * 1.005)
            else:
                # Floor at support for long.
                min_price = max(min_price, sr_support * 0.998)
        if sr_resistance is not None and sr_resistance > 0:
            if _is_short:
                # Ceiling at resistance for short.
                max_price = min(max_price, sr_resistance * 1.002)
            else:
                # Cap target near resistance for long.
                max_price = min(max_price, sr_resistance * 0.995)

        return {
            'min': min_price,
            'max': max_price,
            'volatility': volatility,
            'adjusted_volatility': adjusted_volatility
        }
    
    def _get_sl_tp_pct(self, settings, trade_type: str, is_short: bool):
        """Return configured SL/TP percentages for the active trade type/side."""
        if settings is None:
            return None, None
        sl_pct = None
        tp_pct = None
        if str(trade_type).lower() == 'futures' and is_short:
            sl_pct = float(getattr(settings, 'futures_sl_pct', None) or 0.0)
            if not sl_pct:
                sl_pct = float(getattr(settings, 'range_scalp_short_sl_pct', 2.0) or 0.0)
            tp_pct = float(getattr(settings, 'futures_tp_pct', None) or 0.0)
            if not tp_pct:
                tp_pct = float(getattr(settings, 'range_scalp_short_tp_pct', 4.0) or 0.0)
        elif str(trade_type).lower() == 'futures':
            sl_pct = float(getattr(settings, 'futures_long_sl_pct', 2.5) or 0.0)
            tp_pct = float(getattr(settings, 'futures_long_tp_pct', 5.0) or 0.0)
        else:
            sl_pct = float(getattr(settings, 'stop_loss_pct', 2.5) or 0.0)
            tp_pct = float(getattr(settings, 'take_profit_pct', 4.14) or 0.0)
        return (sl_pct if sl_pct > 0 else None), (tp_pct if tp_pct > 0 else None)

    def _calculate_expected_pnl(
        self,
        current_price: float,
        price_range: Dict,
        success_probability: float,
        position_size_usdt: float,
        trade_side: str = 'LONG',
        settings=None,
        trade_type: str = 'spot',
    ) -> float:
        """
        Calculate expected PnL.

        Args:
            current_price: Current market price
            price_range: Estimated price range
            success_probability: Probability of successful trade
            position_size_usdt: Size of position in USDT
            settings: Settings object to read configured SL/TP percentages
            trade_type: 'spot' or 'futures'
            trade_side: 'LONG' or 'SHORT'

        Returns:
            Expected PnL in USDT
        """
        # Calculate quantity
        quantity = position_size_usdt / current_price
        _is_short = str(trade_side or 'LONG').strip().upper() == 'SHORT'

        _min = float(price_range.get('min', 0.0) or 0.0)
        _max = float(price_range.get('max', 0.0) or 0.0)
        if _min <= 0 or _max <= 0:
            return 0.0

        # Cap the expected move by configured SL/TP so the predictor reflects the
        # actual position risk/reward instead of the full Bollinger/candle range.
        # For prob > sl/(sl+tp) the expected PnL is positive (e.g. SHORT SL2%/TP4%
        # needs prob > 33%).
        effective_min = _min
        effective_max = _max
        sl_pct, tp_pct = self._get_sl_tp_pct(settings, trade_type, _is_short)
        if sl_pct and tp_pct:
            sl_mult = sl_pct / 100.0
            tp_mult = tp_pct / 100.0
            if _is_short:
                # SHORT: TP is price fall, SL is price rise.
                gain_price = current_price * (1.0 - tp_mult)
                loss_price = current_price * (1.0 + sl_mult)
                effective_min = gain_price
                effective_max = loss_price
            else:
                # LONG: TP is price rise, SL is price fall.
                gain_price = current_price * (1.0 + tp_mult)
                loss_price = current_price * (1.0 - sl_mult)
                effective_min = loss_price
                effective_max = gain_price
            # Fallback to price_range if configured SL/TP produce an inverted range.
            if effective_max <= effective_min:
                effective_min, effective_max = _min, _max

        if _is_short:
            # SHORT wins when price falls.
            expected_gain = max(0.0, (current_price - effective_min)) * quantity * success_probability
            expected_loss = max(0.0, (effective_max - current_price)) * quantity * (1 - success_probability)
        else:
            # LONG wins when price rises.
            expected_gain = max(0.0, (effective_max - current_price)) * quantity * success_probability
            expected_loss = max(0.0, (current_price - effective_min)) * quantity * (1 - success_probability)
        expected_pnl = expected_gain - expected_loss

        return expected_pnl
    
    def predict_range_reversion(
        self,
        current_price: float,
        indicators: Dict,
        position_size_usdt: float,
        settings=None,
        take_profit_pct: Optional[float] = None,
        stop_loss_pct: Optional[float] = None,
        trade_type: str = "spot",
        trade_side: str = "LONG",
    ) -> Dict:
        """Evaluate a range-reversion (mean-reversion) setup for either side.

        LONG: reversion off support, target toward the middle band, stop below
        support. SHORT: reversion off resistance, target toward the middle
        band from above, stop above resistance. The two branches are exact
        mirrors of each other around the middle band so neither side is
        favored by construction; `trade_side` must reflect the actual
        candidate side being evaluated (previously this function always
        evaluated the LONG case regardless of the caller's side, which made
        `valid`/`band_position` meaningless for SHORT candidates).
        """
        is_short = str(trade_side or "LONG").strip().upper() == "SHORT"
        bollinger = indicators.get("bollinger_bands") or {}
        lower = float(bollinger.get("lower", 0.0) or 0.0)
        middle = float(bollinger.get("middle", 0.0) or 0.0)
        upper = float(bollinger.get("upper", 0.0) or 0.0)
        if current_price <= 0 or lower <= 0 or middle <= lower or upper <= middle or position_size_usdt <= 0:
            return {"valid": False, "reason": "invalid_bollinger_or_position"}

        band_position = max(0.0, min(1.0, (current_price - lower) / (upper - lower)))
        # Favorability of the current position for the side being evaluated:
        # 0.0 = ideal entry (support for LONG, resistance for SHORT), mirrored
        # around the middle band so both sides reuse the same scoring curve.
        directional_position = (1.0 - band_position) if is_short else band_position

        configured_tp = float(take_profit_pct if take_profit_pct is not None else getattr(settings, "take_profit_pct", 4.14)) / 100.0
        configured_sl = float(stop_loss_pct if stop_loss_pct is not None else getattr(settings, "range_scalp_sl_pct", 0.8)) / 100.0

        # Anchor target / stop to nearest support/resistance when available.
        sr_support = indicators.get("support_level")
        sr_resistance = indicators.get("resistance_level")
        if sr_support is None:
            sr = indicators.get("support_resistance") or {}
            sr_support = sr.get("support_level")
            sr_resistance = sr.get("resistance_level")

        if is_short:
            # SHORT wins when price falls: target toward/through the middle
            # band from above, stop toward the upper band / above resistance.
            target_price = max(middle, current_price * (1.0 - max(configured_tp, 0.0)))
            structural_stop = upper
            if sr_resistance is not None and sr_resistance > 0:
                structural_stop = min(structural_stop, sr_resistance * 1.002)
            if sr_support is not None and sr_support > 0:
                target_price = max(target_price, sr_support * 1.005)
            configured_stop = current_price * (1.0 + max(configured_sl, 0.0))
            stop_price = min(structural_stop, configured_stop)
            if target_price >= current_price or stop_price <= current_price:
                return {"valid": False, "reason": "invalid_reversion_payoff", "band_position": band_position}
        else:
            # LONG wins when price rises: target toward/through the middle
            # band from below, stop toward the lower band / below support.
            target_price = min(middle, current_price * (1.0 + max(configured_tp, 0.0)))
            structural_stop = lower
            if sr_support is not None and sr_support > 0:
                structural_stop = max(structural_stop, sr_support * 0.998)
            if sr_resistance is not None and sr_resistance > 0:
                target_price = min(target_price, sr_resistance * 0.995)
            configured_stop = current_price * (1.0 - max(configured_sl, 0.0))
            stop_price = max(structural_stop, configured_stop)
            if target_price <= current_price or stop_price >= current_price:
                return {"valid": False, "reason": "invalid_reversion_payoff", "band_position": band_position}

        rsi = float((indicators.get("rsi") or {}).get("value", 50.0) or 50.0)
        volume = float((indicators.get("vol_spike") or {}).get("value", 1.0) or 1.0)
        trend_raw = (indicators.get("trend_4h") or {}).get("value", 0.0)
        if isinstance(trend_raw, str):
            trend = 1.0 if trend_raw.strip().upper() in ("UP", "BULL", "BULLISH") else -1.0 if trend_raw.strip().upper() in ("DOWN", "BEAR", "BEARISH") else 0.0
        else:
            trend = float(trend_raw or 0.0)
        # Trend bonus mirrors the side: a downtrend favors SHORT reversion
        # from resistance the same way an uptrend favors LONG from support.
        directional_trend = -trend if is_short else trend
        strength = float(indicators.get("signal_strength", indicators.get("strength", 0.5)) or 0.5)
        probability = 0.50
        probability += max(-0.20, min(0.20, (0.55 - directional_position) * 0.50))
        probability += 0.12 if 45.0 <= rsi <= 56.0 else 0.04 if 35.0 <= rsi <= 65.0 else -0.10
        probability += 0.10 if 1.5 <= volume <= 2.5 else 0.03 if volume >= 1.0 else -0.10
        probability += 0.05 if directional_trend > 0 else -0.10 if directional_trend < 0 else 0.0
        probability += max(-0.08, min(0.08, (strength - 0.5) * 0.32))
        probability = max(0.05, min(0.95, probability))

        quantity = position_size_usdt / current_price
        if is_short:
            gross_gain = (current_price - target_price) * quantity
            gross_loss = (stop_price - current_price) * quantity
        else:
            gross_gain = (target_price - current_price) * quantity
            gross_loss = (current_price - stop_price) * quantity
        expected_pnl = probability * gross_gain - (1.0 - probability) * gross_loss
        from binance_commission import expected_pnl_after_fees
        expected_pnl = expected_pnl_after_fees(expected_pnl, position_size_usdt, trade_type, settings)
        return {
            "valid": True,
            "model": "range_reversion_v1",
            "trade_side": "SHORT" if is_short else "LONG",
            "expected_pnl": expected_pnl,
            "expected_pnl_percentage": expected_pnl / position_size_usdt * 100.0,
            "success_probability": probability,
            "target_price": target_price,
            "stop_price": stop_price,
            "band_position": band_position,
            "gross_gain": gross_gain,
            "gross_loss": gross_loss,
        }

    def _calculate_risk_metrics(
        self,
        current_price: float,
        price_range: Dict,
        position_size_usdt: float,
        trade_side: str = 'LONG',
        settings=None,
        trade_type: str = 'spot',
    ) -> Dict:
        """
        Calculate risk metrics for the position.

        Args:
            current_price: Current market price
            price_range: Estimated price range
            position_size_usdt: Size of position in USDT
            trade_side: 'LONG' or 'SHORT'
            settings: Settings object to read configured SL/TP percentages
            trade_type: 'spot' or 'futures'

        Returns:
            Dictionary with risk metrics
        """
        quantity = position_size_usdt / current_price
        _is_short = str(trade_side or 'LONG').strip().upper() == 'SHORT'

        _min = float(price_range.get('min', 0.0) or 0.0)
        _max = float(price_range.get('max', 0.0) or 0.0)

        # Use configured SL/TP as the actual position risk/reward when available,
        # otherwise fall back to the estimated price range.
        sl_pct, tp_pct = self._get_sl_tp_pct(settings, trade_type, _is_short)
        if sl_pct and tp_pct:
            sl_mult = sl_pct / 100.0
            tp_mult = tp_pct / 100.0
            if _is_short:
                target_price = current_price * (1.0 - tp_mult)
                stop_price = current_price * (1.0 + sl_mult)
            else:
                target_price = current_price * (1.0 + tp_mult)
                stop_price = current_price * (1.0 - sl_mult)
            if stop_price > 0 and target_price > 0 and (
                (_is_short and target_price < stop_price) or
                (not _is_short and target_price > stop_price)
            ):
                if _is_short:
                    max_loss = max(0.0, (stop_price - current_price)) * quantity
                    max_gain = max(0.0, (current_price - target_price)) * quantity
                else:
                    max_loss = max(0.0, (current_price - stop_price)) * quantity
                    max_gain = max(0.0, (target_price - current_price)) * quantity
            else:
                # Fallback to estimated price range if SL/TP are invalid.
                sl_pct, tp_pct = None, None

        if not (sl_pct and tp_pct):
            if _is_short:
                max_loss = max(0.0, (_max - current_price)) * quantity
                max_gain = max(0.0, (current_price - _min)) * quantity
            else:
                max_loss = max(0.0, (current_price - _min)) * quantity
                max_gain = max(0.0, (_max - current_price)) * quantity

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
