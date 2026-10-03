"""Point-in-time opportunity proposal generators for the offline pilot."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence

from support_resistance import compute_sr_features
from technical_indicators import TechnicalIndicators


PILOT_PHILOSOPHIES = (
    "mean_reversion",
    "time_series_momentum",
    "breakout_expansion",
    "volatility_expansion",
    "cross_sectional_relative_strength",
)

LOOKBACK_BARS = 24
MOMENTUM_THRESHOLD = 0.005
MEAN_REVERSION_Z_THRESHOLD = 2.0
VOLATILITY_EXPANSION_RATIO = 2.0
VOLATILITY_DIRECTIONAL_BODY_MIN = 0.005
CROSS_SECTIONAL_LOOKBACK_BARS = 24
CROSS_SECTIONAL_Z_THRESHOLD = 0.5


def _mean_std(values: Sequence[float]) -> tuple[float, float]:
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return mean, math.sqrt(variance)


def _true_range(bar: Dict[str, Any], previous_close: float) -> float:
    high = float(bar["high"])
    low = float(bar["low"])
    return max(high - low, abs(high - previous_close), abs(low - previous_close))


def generate_cross_sectional_relative_strength_proposals(
    bars_by_symbol: Dict[str, Sequence[Dict[str, Any]]],
    *,
    window_id: str,
    window_role: str,
    window_start: float,
    window_end: float,
    horizon_hours: int = 4,
    interval_seconds: int = 3600,
    minimum_symbols: int = 5,
    zscore_threshold: float = CROSS_SECTIONAL_Z_THRESHOLD,
) -> List[Dict[str, Any]]:
    """Emit point-in-time candidates from cross-sectional 24-bar returns."""
    if horizon_hours <= 0 or interval_seconds <= 0 or minimum_symbols < 3:
        raise ValueError("horizon, interval, and minimum universe size must be positive")
    if window_role not in {"development", "validation"}:
        raise ValueError("window_role must be development or validation")
    if window_start >= window_end or zscore_threshold < 0:
        raise ValueError("invalid window bounds or relative-strength threshold")

    observations_by_time: Dict[float, List[Dict[str, Any]]] = {}
    horizon_seconds = horizon_hours * 3600
    lookback_seconds = CROSS_SECTIONAL_LOOKBACK_BARS * interval_seconds
    for symbol, source_bars in sorted(bars_by_symbol.items()):
        ordered = sorted(source_bars, key=lambda bar: float(bar["open_time"]))
        if len(ordered) <= CROSS_SECTIONAL_LOOKBACK_BARS + 1:
            continue
        times = [float(bar["open_time"]) for bar in ordered]
        closes = [float(bar["close"]) for bar in ordered]
        for index in range(CROSS_SECTIONAL_LOOKBACK_BARS, len(ordered) - 1):
            decision_time = times[index] + interval_seconds
            entry_time = times[index + 1]
            if entry_time != decision_time or not window_start <= decision_time < window_end:
                continue
            if times[index] - times[index - CROSS_SECTIONAL_LOOKBACK_BARS] != lookback_seconds:
                continue
            if times[-1] + interval_seconds < entry_time + horizon_seconds:
                continue
            prior_close = closes[index - CROSS_SECTIONAL_LOOKBACK_BARS]
            close = closes[index]
            entry_price = float(ordered[index + 1]["open"])
            if prior_close <= 0 or close <= 0 or entry_price <= 0:
                continue
            observations_by_time.setdefault(decision_time, []).append({
                "symbol": symbol,
                "return_24h": close / prior_close - 1.0,
                "close": close,
                "entry_price": entry_price,
                "entry_time": entry_time,
                "source_candle_open_time": times[index],
            })

    proposals: List[Dict[str, Any]] = []
    for decision_time, observations in sorted(observations_by_time.items()):
        if len(observations) < minimum_symbols:
            continue
        returns = [float(row["return_24h"]) for row in observations]
        cross_section_mean, cross_section_std = _mean_std(returns)
        if cross_section_std <= 0:
            continue
        for row in sorted(observations, key=lambda item: str(item["symbol"])):
            relative_zscore = (float(row["return_24h"]) - cross_section_mean) / cross_section_std
            if abs(relative_zscore) < zscore_threshold:
                continue
            side = "LONG" if relative_zscore > 0 else "SHORT"
            symbol = str(row["symbol"])
            variant = "relative_return_24h_zscore_0_5"
            proposals.append({
                "window_id": window_id,
                "window_role": window_role,
                "symbol": symbol,
                "philosophy_family": "cross_sectional_relative_strength",
                "philosophy_variant": variant,
                "thesis_type": f"{side}_CROSS_SECTIONAL_RELATIVE_STRENGTH",
                "side": side,
                "source_candle_open_time": row["source_candle_open_time"],
                "decision_time": decision_time,
                "feature_available_at": decision_time,
                "entry_time": row["entry_time"],
                "entry_price": row["entry_price"],
                "ev_horizon_minutes": horizon_hours * 60,
                "opportunity_event_id": (
                    f"{window_id}:{symbol}:cross_sectional_relative_strength:"
                    f"{variant}:{int(decision_time)}:{side}"
                ),
                "dedupe_cluster_id": f"{window_id}:{symbol}:{int(decision_time)}",
                "feature_snapshot": {
                    "close": row["close"],
                    "return_24h": row["return_24h"],
                    "cross_section_mean_return_24h": cross_section_mean,
                    "cross_section_std_return_24h": cross_section_std,
                    "relative_strength_zscore_24h": relative_zscore,
                    "cross_section_size": len(observations),
                    "source_candle_count": CROSS_SECTIONAL_LOOKBACK_BARS + 1,
                },
                "label_status": "pending_contract_and_outcome_replay",
            })
    return proposals


def generate_pilot_proposals(
    symbol: str,
    bars: Sequence[Dict[str, Any]],
    *,
    window_id: str,
    window_role: str,
    window_start: float,
    window_end: float,
    horizon_hours: int = 4,
    interval_seconds: int = 3600,
) -> List[Dict[str, Any]]:
    """Generate candle-based proposals without labels, contracts, or side effects.

    Signals use a fully closed source bar. The next contiguous bar's open is the
    assumed entry observation; future bars are used only to enforce label maturity.
    """
    if horizon_hours <= 0 or interval_seconds <= 0:
        raise ValueError("horizon and interval must be positive")
    if window_role not in {"development", "validation"}:
        raise ValueError("window_role must be development or validation")
    if window_start >= window_end:
        raise ValueError("window_start must be before window_end")

    ordered = sorted(bars, key=lambda bar: float(bar["open_time"]))
    if len(ordered) < LOOKBACK_BARS + 3:
        return []
    times = [float(bar["open_time"]) for bar in ordered]
    closes = [float(bar["close"]) for bar in ordered]
    true_ranges = [0.0]
    for index in range(1, len(ordered)):
        true_ranges.append(_true_range(ordered[index], closes[index - 1]))

    proposals: List[Dict[str, Any]] = []
    horizon_seconds = horizon_hours * 3600
    for index in range(LOOKBACK_BARS + 1, len(ordered) - 1):
        source_bar = ordered[index]
        next_bar = ordered[index + 1]
        source_open_time = times[index]
        decision_time = source_open_time + interval_seconds
        entry_time = times[index + 1]
        if entry_time != decision_time:
            continue
        if not window_start <= decision_time < window_end:
            continue
        if times[-1] + interval_seconds < entry_time + horizon_seconds:
            continue

        close = closes[index]
        if close <= 0:
            continue
        previous_returns = [
            closes[position] / closes[position - 4] - 1.0
            for position in range(max(4, index - LOOKBACK_BARS + 1), index)
            if closes[position - 4] > 0
        ]
        momentum_return = closes[index] / closes[index - 4] - 1.0 if closes[index - 4] > 0 else 0.0
        rolling_closes = closes[index - LOOKBACK_BARS + 1:index + 1]
        rolling_mean, rolling_std = _mean_std(rolling_closes)
        zscore = (close - rolling_mean) / rolling_std if rolling_std > 0 else 0.0
        previous_closes = closes[index - LOOKBACK_BARS:index]
        previous_high = max(float(bar["high"]) for bar in ordered[index - LOOKBACK_BARS:index])
        previous_low = min(float(bar["low"]) for bar in ordered[index - LOOKBACK_BARS:index])
        previous_atr = sum(true_ranges[index - LOOKBACK_BARS:index]) / LOOKBACK_BARS
        volatility_ratio = true_ranges[index] / previous_atr if previous_atr > 0 else 0.0
        body_return = (float(source_bar["close"]) - float(source_bar["open"])) / close

        closed_closes = closes[:index + 1]
        closed_highs = [float(bar["high"]) for bar in ordered[:index + 1]]
        closed_lows = [float(bar["low"]) for bar in ordered[:index + 1]]
        closed_volumes = [float(bar.get("volume", 0.0)) for bar in ordered[:index + 1]]
        rsi_values = TechnicalIndicators.rsi(closed_closes, 14)
        macd_line, macd_signal, macd_hist = TechnicalIndicators.macd(closed_closes)
        upper_bands, middle_bands, lower_bands = TechnicalIndicators.bollinger_bands(closed_closes, 20, 2.0)
        atr_values = TechnicalIndicators.atr(closed_highs, closed_lows, closed_closes, 14)
        ema_values = TechnicalIndicators.ema(closed_closes, 9)
        sma_values = TechnicalIndicators.sma(closed_closes, 20)
        volume_window = closed_volumes[-20:]
        average_volume = sum(volume_window) / len(volume_window) if volume_window else 0.0
        current_atr = atr_values[-1] if atr_values else previous_atr
        trend_fast = TechnicalIndicators.ema(closed_closes, 4)
        trend_slow = TechnicalIndicators.ema(closed_closes, 12)
        weekly_window = closed_closes[-168:]
        weekly_low = min(weekly_window) if weekly_window else previous_low
        weekly_high = max(weekly_window) if weekly_window else previous_high
        sr_range = previous_high - previous_low
        indicator_snapshot = {
            "rsi": {"value": rsi_values[-1] if rsi_values else 50.0},
            "macd": {
                "macd": macd_line[-1] if macd_line else 0.0,
                "signal": macd_signal[-1] if macd_signal else 0.0,
                "histogram": macd_hist[-1] if macd_hist else 0.0,
            },
            "ema": {"value": ema_values[-1] if ema_values else close},
            "sma": {"value": sma_values[-1] if sma_values else close},
            "bollinger_bands": {
                "upper": upper_bands[-1] if upper_bands else previous_high,
                "middle": middle_bands[-1] if middle_bands else rolling_mean,
                "lower": lower_bands[-1] if lower_bands else previous_low,
            },
            "vol_spike": {"value": (closed_volumes[-1] / average_volume) if average_volume > 0 else 1.0},
            "atr": {"value": current_atr},
            "atr_pct": {"value": current_atr / close * 100.0 if close > 0 else 0.0},
            "trend_4h": {"value": 1.0 if trend_fast and trend_slow and trend_fast[-1] >= trend_slow[-1] else -1.0},
            "weekly_range": {"value": (close - weekly_low) / (weekly_high - weekly_low) if weekly_high > weekly_low else 0.5},
            "sr_zone_position": (close - previous_low) / sr_range if sr_range > 0 else 0.5,
        }

        signals = []
        if zscore <= -MEAN_REVERSION_Z_THRESHOLD:
            signals.append(("mean_reversion", "zscore_24h_2", "LONG", "LONG_MEAN_REVERSION"))
        elif zscore >= MEAN_REVERSION_Z_THRESHOLD:
            signals.append(("mean_reversion", "zscore_24h_2", "SHORT", "SHORT_MEAN_REVERSION"))

        if momentum_return >= MOMENTUM_THRESHOLD and (not previous_returns or max(previous_returns) < MOMENTUM_THRESHOLD):
            signals.append(("time_series_momentum", "return_4h_0_5pct", "LONG", "LONG_MOMENTUM_CONTINUATION"))
        elif momentum_return <= -MOMENTUM_THRESHOLD and (not previous_returns or min(previous_returns) > -MOMENTUM_THRESHOLD):
            signals.append(("time_series_momentum", "return_4h_0_5pct", "SHORT", "SHORT_MOMENTUM_CONTINUATION"))

        if close > previous_high:
            signals.append(("breakout_expansion", "prior_24h_high_low_break", "LONG", "LONG_RESISTANCE_BREAKOUT"))
        elif close < previous_low:
            signals.append(("breakout_expansion", "prior_24h_high_low_break", "SHORT", "SHORT_SUPPORT_BREAKDOWN"))

        if volatility_ratio >= VOLATILITY_EXPANSION_RATIO and abs(body_return) >= VOLATILITY_DIRECTIONAL_BODY_MIN:
            if body_return > 0:
                signals.append(("volatility_expansion", "true_range_24h_2x", "LONG", "LONG_VOLATILITY_EXPANSION"))
            else:
                signals.append(("volatility_expansion", "true_range_24h_2x", "SHORT", "SHORT_VOLATILITY_EXPANSION"))

        features = {
            "close": close,
            "return_4h": momentum_return,
            "zscore_24h": zscore,
            "rolling_mean_24h": rolling_mean,
            "rolling_std_24h": rolling_std,
            "prior_24h_high": previous_high,
            "prior_24h_low": previous_low,
            "atr_24h": previous_atr,
            "true_range_expansion_ratio": volatility_ratio,
            "source_candle_count": LOOKBACK_BARS + 1,
            "predictor_indicators": indicator_snapshot,
        }
        for family, variant, side, thesis_type in signals:
            proposals.append({
                "window_id": window_id,
                "window_role": window_role,
                "symbol": symbol,
                "philosophy_family": family,
                "philosophy_variant": variant,
                "thesis_type": thesis_type,
                "side": side,
                "source_candle_open_time": source_open_time,
                "decision_time": decision_time,
                "feature_available_at": decision_time,
                "entry_time": entry_time,
                "entry_price": float(next_bar["open"]),
                "ev_horizon_minutes": horizon_hours * 60,
                "opportunity_event_id": f"{window_id}:{symbol}:{family}:{variant}:{int(decision_time)}:{side}",
                "dedupe_cluster_id": f"{window_id}:{symbol}:{int(decision_time)}",
                "feature_snapshot": dict(features),
                "label_status": "pending_contract_and_outcome_replay",
            })
    return proposals


def generate_sr_event_proposals(
    symbol: str,
    bars: Sequence[Dict[str, Any]],
    *,
    window_id: str,
    window_role: str,
    window_start: float,
    window_end: float,
    horizon_hours: int = 4,
    interval_seconds: int = 3600,
    lookback_bars: int = 168,
    sr_order: int = 3,
    near_band_atr_mult: float = 0.25,
    near_band_price_pct: float = 0.0025,
) -> List[Dict[str, Any]]:
    """Emit point-in-time proposals from confirmed pivot-based S/R events."""
    if horizon_hours <= 0 or interval_seconds <= 0 or lookback_bars <= 0 or sr_order <= 0:
        raise ValueError("horizon, interval, lookback, and pivot order must be positive")
    if window_role not in {"development", "validation"}:
        raise ValueError("window_role must be development or validation")
    if window_start >= window_end:
        raise ValueError("window_start must be before window_end")

    ordered = sorted(bars, key=lambda bar: float(bar["open_time"]))
    if len(ordered) < max(lookback_bars, sr_order * 2 + 1) + 1:
        return []
    times = [float(bar["open_time"]) for bar in ordered]
    proposals: List[Dict[str, Any]] = []
    horizon_seconds = horizon_hours * 3600
    variant = f"confirmed_sr_pivot_order_{sr_order}_lookback_{lookback_bars}"

    for index in range(lookback_bars - 1, len(ordered) - 1):
        decision_time = times[index] + interval_seconds
        entry_time = times[index + 1]
        if entry_time != decision_time or not window_start <= decision_time < window_end:
            continue
        if times[-1] + interval_seconds < entry_time + horizon_seconds:
            continue

        start_index = index - lookback_bars + 1
        history = ordered[start_index:index + 1]
        history_times = times[start_index:index + 1]
        if any(right - left != interval_seconds for left, right in zip(history_times, history_times[1:])):
            continue

        closes = [float(bar["close"]) for bar in history]
        highs = [float(bar["high"]) for bar in history]
        lows = [float(bar["low"]) for bar in history]
        volumes = [float(bar.get("volume", 0.0)) for bar in history]
        true_ranges = []
        for bar_index, bar in enumerate(history):
            previous_close = closes[max(0, bar_index - 1)]
            true_ranges.append(max(
                highs[bar_index] - lows[bar_index],
                abs(highs[bar_index] - previous_close),
                abs(lows[bar_index] - previous_close),
            ))
        atr = sum(true_ranges[-14:]) / min(14, len(true_ranges))
        close = closes[-1]
        if close <= 0:
            continue

        sr = compute_sr_features(
            closes,
            highs,
            lows,
            volumes,
            current_price=close,
            atr=atr,
            lookback=lookback_bars,
            order=sr_order,
            near_band_atr_mult=near_band_atr_mult,
            near_band_price_pct=near_band_price_pct,
        )
        events = []
        if sr.get("support_bounce_buy"):
            events.append(("mean_reversion", "LONG", "LONG_MEAN_REVERSION_SR_BOUNCE", f"{variant}_support_bounce"))
        if sr.get("resistance_bounce_sell"):
            events.append(("mean_reversion", "SHORT", "SHORT_MEAN_REVERSION_SR_REJECTION", f"{variant}_resistance_rejection"))

        prior_sr = compute_sr_features(
            closes[:-1],
            highs[:-1],
            lows[:-1],
            volumes[:-1],
            current_price=close,
            atr=atr,
            lookback=lookback_bars - 1,
            order=sr_order,
            near_band_atr_mult=near_band_atr_mult,
            near_band_price_pct=near_band_price_pct,
        )
        previous_close = closes[-2]
        prior_resistance = float(prior_sr.get("resistance_level") or 0.0)
        prior_support = float(prior_sr.get("support_level") or 0.0)
        prior_pivot_breakout = close > prior_resistance and previous_close <= prior_resistance
        prior_pivot_breakdown = close < prior_support and previous_close >= prior_support
        if prior_pivot_breakout:
            events.append(("breakout_expansion", "LONG", "LONG_RESISTANCE_BREAKOUT", f"{variant}_prior_pivot_breakout"))
        if prior_pivot_breakdown:
            events.append(("breakout_expansion", "SHORT", "SHORT_SUPPORT_BREAKDOWN", f"{variant}_prior_pivot_breakdown"))

        for family, side, thesis_type, event_variant in events:
            proposals.append({
                "window_id": window_id,
                "window_role": window_role,
                "symbol": str(symbol).upper(),
                "philosophy_family": family,
                "philosophy_variant": event_variant,
                "thesis_type": thesis_type,
                "side": side,
                "source_candle_open_time": times[index],
                "decision_time": decision_time,
                "feature_available_at": decision_time,
                "entry_time": entry_time,
                "entry_price": float(ordered[index + 1]["open"]),
                "ev_horizon_minutes": horizon_hours * 60,
                "opportunity_event_id": (
                    f"{window_id}:{str(symbol).upper()}:{family}:{variant}:"
                    f"{int(decision_time)}:{side}"
                ),
                "dedupe_cluster_id": f"{window_id}:{str(symbol).upper()}:{int(decision_time)}",
                "feature_snapshot": {
                    "source_close": close,
                    "source_atr_14": atr,
                    "source_candle_count": len(history),
                    "sr_features": dict(sr),
                    "prior_pivot_support": prior_support,
                    "prior_pivot_resistance": prior_resistance,
                    "prior_pivot_breakout": prior_pivot_breakout,
                    "prior_pivot_breakdown": prior_pivot_breakdown,
                },
                "label_status": "pending_contract_and_outcome_replay",
            })
    return proposals


def generate_trend_pullback_proposals(
    symbol: str,
    bars: Sequence[Dict[str, Any]],
    *,
    window_id: str,
    window_role: str,
    window_start: float,
    window_end: float,
    horizon_hours: int = 4,
    interval_seconds: int = 3600,
    trend_horizon_bars: int = 24,
    minimum_trend_return: float = 0.01,
    swing_lookback_bars: int = 12,
    pullback_min_atr: float = 0.5,
    pullback_max_atr: float = 1.5,
    minimum_volume_ratio: float = 1.0,
) -> List[Dict[str, Any]]:
    """Emit continuation entries after a volatility-scaled pullback resumes."""
    if min(horizon_hours, interval_seconds, trend_horizon_bars, swing_lookback_bars) <= 0:
        raise ValueError("horizon, interval, and lookback values must be positive")
    if window_role not in {"development", "validation"}:
        raise ValueError("window_role must be development or validation")
    if window_start >= window_end or minimum_trend_return < 0 or minimum_volume_ratio < 0:
        raise ValueError("invalid window bounds or thresholds")
    if pullback_min_atr < 0 or pullback_max_atr < pullback_min_atr:
        raise ValueError("invalid pullback ATR bounds")

    ordered = sorted(bars, key=lambda bar: float(bar["open_time"]))
    warmup = max(trend_horizon_bars, swing_lookback_bars, 20, 14) + 1
    if len(ordered) < warmup + 1:
        return []
    times = [float(bar["open_time"]) for bar in ordered]
    closes = [float(bar["close"]) for bar in ordered]
    highs = [float(bar["high"]) for bar in ordered]
    lows = [float(bar["low"]) for bar in ordered]
    volumes = [float(bar.get("volume", 0.0)) for bar in ordered]
    true_ranges = [0.0]
    for index in range(1, len(ordered)):
        true_ranges.append(_true_range(ordered[index], closes[index - 1]))

    proposals: List[Dict[str, Any]] = []
    horizon_seconds = horizon_hours * 3600
    variant = "trend_24h_pullback_0_5_1_5_atr_confirmation"
    for index in range(warmup, len(ordered) - 1):
        decision_time = times[index] + interval_seconds
        entry_time = times[index + 1]
        if entry_time != decision_time or not window_start <= decision_time < window_end:
            continue
        if times[-1] + interval_seconds < entry_time + horizon_seconds:
            continue
        if times[index] - times[index - trend_horizon_bars] != trend_horizon_bars * interval_seconds:
            continue
        if times[index] - times[index - swing_lookback_bars] != swing_lookback_bars * interval_seconds:
            continue
        if any(right - left != interval_seconds for left, right in zip(times[index - 20:index + 1], times[index - 19:index + 1])):
            continue

        close = closes[index]
        previous_close = closes[index - 1]
        atr = sum(true_ranges[index - 13:index + 1]) / 14
        if close <= 0 or previous_close <= 0 or atr <= 0:
            continue
        volume_window = volumes[index - 20:index]
        average_volume = sum(volume_window) / len(volume_window) if volume_window else 0.0
        volume_ratio = volumes[index] / average_volume if average_volume > 0 else 0.0
        trend_return = close / closes[index - trend_horizon_bars] - 1.0
        prior_high = max(highs[index - swing_lookback_bars:index])
        prior_low = min(lows[index - swing_lookback_bars:index])
        source_bar = ordered[index]

        setups = []
        long_pullback = (prior_high - close) / atr
        if (
            trend_return >= minimum_trend_return
            and pullback_min_atr <= long_pullback <= pullback_max_atr
            and close > float(source_bar["open"])
            and close > previous_close
            and volume_ratio >= minimum_volume_ratio
        ):
            setups.append(("LONG", "LONG_TREND_PULLBACK_CONTINUATION", long_pullback))

        short_pullback = (close - prior_low) / atr
        if (
            trend_return <= -minimum_trend_return
            and pullback_min_atr <= short_pullback <= pullback_max_atr
            and close < float(source_bar["open"])
            and close < previous_close
            and volume_ratio >= minimum_volume_ratio
        ):
            setups.append(("SHORT", "SHORT_TREND_PULLBACK_CONTINUATION", short_pullback))

        for side, thesis_type, pullback_atr in setups:
            normalized_symbol = str(symbol).upper()
            proposals.append({
                "window_id": window_id,
                "window_role": window_role,
                "symbol": normalized_symbol,
                "philosophy_family": "time_series_momentum",
                "philosophy_variant": variant,
                "thesis_type": thesis_type,
                "side": side,
                "source_candle_open_time": times[index],
                "decision_time": decision_time,
                "feature_available_at": decision_time,
                "entry_time": entry_time,
                "entry_price": float(ordered[index + 1]["open"]),
                "ev_horizon_minutes": horizon_hours * 60,
                "opportunity_event_id": (
                    f"{window_id}:{normalized_symbol}:time_series_momentum:"
                    f"{variant}:{int(decision_time)}:{side}"
                ),
                "dedupe_cluster_id": f"{window_id}:{normalized_symbol}:{int(decision_time)}",
                "feature_snapshot": {
                    "return_24h": trend_return,
                    "pullback_atr": pullback_atr,
                    "atr_14": atr,
                    "volume_ratio_20": volume_ratio,
                    "source_candle_count": trend_horizon_bars + 1,
                },
                "label_status": "pending_contract_and_outcome_replay",
            })
    return proposals