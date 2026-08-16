from __future__ import annotations

from typing import Any, Dict, List, Sequence


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _lin_slope(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    num = 0.0
    den = 0.0
    for i, y in enumerate(values):
        dx = i - x_mean
        num += dx * (y - y_mean)
        den += dx * dx
    return num / den if den > 0 else 0.0


def extract_graph_features(klines: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return lightweight chart-geometry features for signal enrichment."""
    if not klines:
        return {
            "graph_trend_slope": 0.0,
            "graph_momentum_5": 0.0,
            "graph_momentum_20": 0.0,
            "graph_volatility_pct": 0.0,
            "graph_range_position": 0.5,
            "graph_breakout_bias": 0.0,
        }

    closes = [_f(k.get("close")) for k in klines]
    highs = [_f(k.get("high"), c) for k, c in zip(klines, closes)]
    lows = [_f(k.get("low"), c) for k, c in zip(klines, closes)]

    n = len(closes)
    last = closes[-1] if closes else 0.0
    c5 = closes[-5:] if n >= 5 else closes
    c20 = closes[-20:] if n >= 20 else closes

    slope = _lin_slope(c20)
    mom5 = ((last / c5[0]) - 1.0) if len(c5) >= 2 and c5[0] > 0 else 0.0
    mom20 = ((last / c20[0]) - 1.0) if len(c20) >= 2 and c20[0] > 0 else 0.0

    window_h = highs[-20:] if n >= 20 else highs
    window_l = lows[-20:] if n >= 20 else lows
    range_high = max(window_h) if window_h else last
    range_low = min(window_l) if window_l else last
    span = max(range_high - range_low, 1e-9)
    range_position = (last - range_low) / span if span > 0 else 0.5

    bar_ranges = [max(0.0, h - l) for h, l in zip(window_h, window_l)]
    avg_range = sum(bar_ranges) / len(bar_ranges) if bar_ranges else 0.0
    vol_pct = (avg_range / last * 100.0) if last > 0 else 0.0

    # Positive when price is in upper range and momentum confirms continuation.
    breakout_bias = max(-1.0, min(1.0, (range_position - 0.5) * 2.0 + (mom5 * 5.0)))

    return {
        "graph_trend_slope": round(slope, 8),
        "graph_momentum_5": round(mom5, 8),
        "graph_momentum_20": round(mom20, 8),
        "graph_volatility_pct": round(vol_pct, 6),
        "graph_range_position": round(max(0.0, min(1.0, range_position)), 6),
        "graph_breakout_bias": round(breakout_bias, 6),
    }