"""
Technical Indicators Module
Implements common technical analysis indicators for autonomous trading signals.

Enhanced with:
- ATR  (Average True Range)          — real volatility for dynamic SL/TP
- StochRSI                           — reduces false signals in ranging markets
- EMA cross (fast / slow)            — faster trend detection than SMA 5/20
- Volume spike detection             — confirms breakouts with unusual volume
"""

import numpy as np
from typing import List, Dict, Tuple, Optional


class TechnicalIndicators:
    """Class for calculating technical analysis indicators."""

    @staticmethod
    def sma(data: List[float], period: int) -> List[float]:
        """Calculate Simple Moving Average."""
        if len(data) < period:
            return []
        return [sum(data[i:i+period]) / period for i in range(len(data) - period + 1)]

    @staticmethod
    def ema(data: List[float], period: int) -> List[float]:
        """Calculate Exponential Moving Average."""
        if len(data) < period:
            return []
        multiplier = 2 / (period + 1)
        ema_values = [sum(data[:period]) / period]
        for price in data[period:]:
            ema_values.append((price - ema_values[-1]) * multiplier + ema_values[-1])
        return ema_values

    @staticmethod
    def rsi(data: List[float], period: int = 14) -> List[float]:
        """Calculate Relative Strength Index."""
        if len(data) < period + 1:
            return []

        deltas = [data[i] - data[i-1] for i in range(1, len(data))]
        gains  = [d if d > 0 else 0 for d in deltas]
        losses = [-d if d < 0 else 0 for d in deltas]

        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        rsi_values = []
        for i in range(period, len(deltas)):
            if avg_loss == 0:
                rsi = 100
            else:
                rs = avg_gain / avg_loss
                rsi = 100 - (100 / (1 + rs))
            rsi_values.append(rsi)
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        return rsi_values

    @staticmethod
    def macd(
        data: List[float],
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9,
    ) -> Tuple[List[float], List[float], List[float]]:
        """Calculate MACD (Moving Average Convergence Divergence)."""
        if len(data) < slow_period:
            return [], [], []

        ema_fast = TechnicalIndicators.ema(data, fast_period)
        ema_slow = TechnicalIndicators.ema(data, slow_period)

        min_len   = min(len(ema_fast), len(ema_slow))
        ema_fast  = ema_fast[-min_len:]
        ema_slow  = ema_slow[-min_len:]

        macd_line   = [ema_fast[i] - ema_slow[i] for i in range(min_len)]
        signal_line = TechnicalIndicators.ema(macd_line, signal_period)

        if len(signal_line) < len(macd_line):
            signal_line = [0] * (len(macd_line) - len(signal_line)) + signal_line

        histogram = [macd_line[i] - signal_line[i] for i in range(len(macd_line))]
        return macd_line, signal_line, histogram

    @staticmethod
    def bollinger_bands(
        data: List[float], period: int = 20, std_dev: float = 2
    ) -> Tuple[List[float], List[float], List[float]]:
        """Calculate Bollinger Bands."""
        if len(data) < period:
            return [], [], []

        sma_values = TechnicalIndicators.sma(data, period)
        upper_band, lower_band = [], []

        for i in range(len(sma_values)):
            window = data[i:i+period]
            std = np.std(window)
            upper_band.append(sma_values[i] + std * std_dev)
            lower_band.append(sma_values[i] - std * std_dev)

        return upper_band, sma_values, lower_band

    # ── New indicators ────────────────────────────────────────────────────────

    @staticmethod
    def atr(
        highs: List[float], lows: List[float], closes: List[float], period: int = 14
    ) -> List[float]:
        """
        Average True Range — measures real volatility.
        Returns a list of ATR values (same length as input minus 1).
        """
        if len(closes) < period + 1:
            return []

        true_ranges = []
        for i in range(1, len(closes)):
            high_low   = highs[i] - lows[i]
            high_close = abs(highs[i] - closes[i-1])
            low_close  = abs(lows[i]  - closes[i-1])
            true_ranges.append(max(high_low, high_close, low_close))

        if len(true_ranges) < period:
            return []

        atr_values = [sum(true_ranges[:period]) / period]
        for tr in true_ranges[period:]:
            atr_values.append((atr_values[-1] * (period - 1) + tr) / period)

        return atr_values

    @staticmethod
    def stoch_rsi(
        data: List[float], rsi_period: int = 14, stoch_period: int = 14, smooth_k: int = 3
    ) -> Tuple[List[float], List[float]]:
        """
        Stochastic RSI — oscillator (0-1) that combines RSI and Stochastic.
        Returns (k_line, d_line).  Reduces false signals in sideways markets.
        """
        rsi_vals = TechnicalIndicators.rsi(data, rsi_period)
        if len(rsi_vals) < stoch_period:
            return [], []

        k_raw = []
        for i in range(stoch_period - 1, len(rsi_vals)):
            window    = rsi_vals[i - stoch_period + 1: i + 1]
            low_rsi   = min(window)
            high_rsi  = max(window)
            denom     = high_rsi - low_rsi
            k_raw.append((rsi_vals[i] - low_rsi) / denom if denom > 0 else 0.5)

        k_smooth = TechnicalIndicators.sma(k_raw, smooth_k) if len(k_raw) >= smooth_k else k_raw
        d_smooth = TechnicalIndicators.sma(k_smooth, 3)     if len(k_smooth) >= 3      else k_smooth

        return k_smooth, d_smooth

    @staticmethod
    def ema_cross(
        data: List[float], fast: int = 9, slow: int = 21
    ) -> Tuple[List[float], List[float], List[int]]:
        """
        EMA cross (fast / slow).
        Returns (ema_fast, ema_slow, crossover_signals) where:
          +1 = bullish cross (fast crossed above slow)
          -1 = bearish cross (fast crossed below slow)
           0 = no cross
        """
        ema_f = TechnicalIndicators.ema(data, fast)
        ema_s = TechnicalIndicators.ema(data, slow)

        min_len = min(len(ema_f), len(ema_s))
        ema_f   = ema_f[-min_len:]
        ema_s   = ema_s[-min_len:]

        signals = [0] * min_len
        for i in range(1, min_len):
            prev_diff = ema_f[i-1] - ema_s[i-1]
            curr_diff = ema_f[i]   - ema_s[i]
            if prev_diff <= 0 < curr_diff:
                signals[i] = 1    # bullish cross
            elif prev_diff >= 0 > curr_diff:
                signals[i] = -1   # bearish cross

        return ema_f, ema_s, signals

    @staticmethod
    def volume_spike(
        volumes: List[float], period: int = 20, threshold: float = 1.5
    ) -> List[bool]:
        """
        Detect volume spikes: True when current volume > threshold × rolling mean.
        Returns a list of booleans the same length as volumes.
        """
        if len(volumes) < period:
            return [False] * len(volumes)

        result = [False] * period
        for i in range(period, len(volumes)):
            avg_vol = sum(volumes[i - period:i]) / period
            result.append(volumes[i] > threshold * avg_vol if avg_vol > 0 else False)

        return result

    # ── Extended signal analysis ──────────────────────────────────────────────

    @staticmethod
    def analyze_signal(
        closes: List[float],
        highs: List[float],
        lows: List[float],
        volumes: List[float],
    ) -> Dict:
        """
        Analyze price data and generate trading signal.

        Uses RSI, MACD, Bollinger Bands, EMA cross 9/21, StochRSI, and volume
        spikes for a more robust and less noisy signal.
        """
        if len(closes) < 30:
            return {"signal": "HOLD", "strength": 0, "reason": "Insufficient data"}

        # ── Compute indicators ────────────────────────────────────────────────
        rsi_values              = TechnicalIndicators.rsi(closes, 14)
        macd_line, sig_line, histo = TechnicalIndicators.macd(closes)
        upper_band, middle_band, lower_band = TechnicalIndicators.bollinger_bands(closes)
        ema_f, ema_s, ema_crosses = TechnicalIndicators.ema_cross(closes, 9, 21)
        k_line, d_line          = TechnicalIndicators.stoch_rsi(closes, 14, 14, 3)
        vol_spikes              = TechnicalIndicators.volume_spike(volumes, 20, 1.5)
        atr_vals                = TechnicalIndicators.atr(highs, lows, closes, 14)

        if not rsi_values or not macd_line or not upper_band:
            return {"signal": "HOLD", "strength": 0, "reason": "Insufficient indicator data"}

        latest_rsi       = rsi_values[-1]
        latest_macd      = macd_line[-1]
        latest_signal    = sig_line[-1]
        latest_histogram = histo[-1]
        latest_price     = closes[-1]
        latest_upper     = upper_band[-1]
        latest_lower     = lower_band[-1]
        latest_mid       = middle_band[-1]
        latest_ema_f     = ema_f[-1] if ema_f else None
        latest_ema_s     = ema_s[-1] if ema_s else None
        latest_cross     = ema_crosses[-1] if ema_crosses else 0
        latest_stoch_k   = k_line[-1] if k_line else 0.5
        latest_stoch_d   = d_line[-1] if d_line else 0.5
        latest_vol_spike = vol_spikes[-1] if vol_spikes else False
        latest_atr       = atr_vals[-1] if atr_vals else 0.0

        # ── Scoring (max 10 buy / 10 sell) ───────────────────────────────────
        buy_score = sell_score = 0

        # RSI (weight 2) — veto overbought/oversold extremes
        if latest_rsi > 72:   # overbought veto — do not add buy points
            sell_score += 2
        elif latest_rsi < 28: # oversold veto — do not add sell points
            buy_score  += 2
        elif latest_rsi < 35:   buy_score  += 2
        elif latest_rsi < 45: buy_score  += 1
        elif latest_rsi > 65: sell_score += 2
        elif latest_rsi > 55: sell_score += 1

        # MACD (weight 2)
        if latest_macd > latest_signal and latest_histogram > 0:   buy_score  += 2
        elif latest_macd > latest_signal:                           buy_score  += 1
        elif latest_macd < latest_signal and latest_histogram < 0:  sell_score += 2
        elif latest_macd < latest_signal:                           sell_score += 1

        # Bollinger Bands (weight 2)
        if latest_price < latest_lower:    buy_score  += 2
        elif latest_price < latest_mid:    buy_score  += 1
        elif latest_price > latest_upper:  sell_score += 2
        elif latest_price > latest_mid:    sell_score += 1

        # EMA cross 9/21 (weight 2)
        if latest_cross == 1:
            buy_score  += 2   # fresh bullish cross
        elif latest_cross == -1:
            sell_score += 2   # fresh bearish cross
        elif latest_ema_f is not None and latest_ema_s is not None:
            if latest_ema_f > latest_ema_s:   buy_score  += 1.5  # trending above slow EMA
            else:                              sell_score += 1.5  # trending below slow EMA

        # StochRSI confirmation (weight 1)
        if latest_stoch_k < 0.2 and latest_stoch_k > latest_stoch_d:
            buy_score  += 1   # oversold + turning up
        elif latest_stoch_k > 0.8 and latest_stoch_k < latest_stoch_d:
            sell_score += 1   # overbought + turning down

        # Volume spike confirmation (weight 1) — only adds if direction already set
        if latest_vol_spike:
            if buy_score > sell_score:   buy_score  += 1
            elif sell_score > buy_score: sell_score += 1

        # ── Determine signal ──────────────────────────────────────────────────
        # max_score=6: excludes vol spike bonus (+1) and EMA position fractional bonus
        # This means buy_score=3 → strength=0.50, buy_score=4 → 0.67, etc.
        max_score = 6
        if buy_score >= 3:
            signal   = "BUY"
            strength = min(buy_score / max_score, 1.0)
            reason   = (f"Buy signal (RSI:{latest_rsi:.1f}, MACD:{latest_macd:.4f}, "
                        f"StochK:{latest_stoch_k:.2f}, EMA_cross:{latest_cross})")
        elif sell_score >= 3:
            signal   = "SELL"
            strength = min(sell_score / max_score, 1.0)
            reason   = (f"Sell signal (RSI:{latest_rsi:.1f}, MACD:{latest_macd:.4f}, "
                        f"StochK:{latest_stoch_k:.2f}, EMA_cross:{latest_cross})")
        else:
            signal   = "HOLD"
            strength = 0
            reason   = (f"No clear signal (RSI:{latest_rsi:.1f}, "
                        f"buy={buy_score}, sell={sell_score})")

        return {
            "signal":   signal,
            "strength": strength,
            "reason":   reason,
            "indicators": {
                "rsi":          latest_rsi,
                "macd":         latest_macd,
                "signal":       latest_signal,
                "histogram":    latest_histogram,
                "upper_band":   latest_upper,
                "middle_band":  latest_mid,
                "lower_band":   latest_lower,
                "price":        latest_price,
                "ema_fast":     latest_ema_f,
                "ema_slow":     latest_ema_s,
                "ema_cross":    latest_cross,
                "stoch_rsi_k":  latest_stoch_k,
                "stoch_rsi_d":  latest_stoch_d,
                "volume_spike": latest_vol_spike,
                "atr":          latest_atr,
            },
        }
