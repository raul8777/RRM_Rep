from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pivot_levels(
    highs: Sequence[float],
    lows: Sequence[float],
    volumes: Sequence[float],
    order: int,
) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
    n = min(len(highs), len(lows), len(volumes))
    if n == 0:
        return [], []
    k = max(1, int(order or 1))
    resistance: List[Tuple[float, float]] = []
    support: List[Tuple[float, float]] = []
    for i in range(k, n - k):
        h = highs[i]
        l = lows[i]
        left_h = highs[i - k:i]
        right_h = highs[i + 1:i + 1 + k]
        left_l = lows[i - k:i]
        right_l = lows[i + 1:i + 1 + k]
        w = max(volumes[i], 0.0)
        if left_h and right_h and h >= max(left_h) and h >= max(right_h):
            resistance.append((h, w))
        if left_l and right_l and l <= min(left_l) and l <= min(right_l):
            support.append((l, w))
    return support, resistance


def _weighted_strength(levels: Sequence[Tuple[float, float]]) -> float:
    if not levels:
        return 0.0
    vols = [max(v, 0.0) for _, v in levels]
    total = sum(vols)
    if total <= 0:
        return min(1.0, len(levels) / 4.0)
    # Heuristic: more repeated pivots + above-average pivot volume => stronger zone.
    avg = total / len(vols)
    weighted = sum(min(2.0, (v / avg) if avg > 0 else 0.0) for v in vols) / max(1, len(vols))
    return max(0.0, min(1.0, 0.5 * min(1.0, len(levels) / 4.0) + 0.5 * min(1.0, weighted / 1.2)))


def _nearest_levels(
    current_price: float,
    supports: Sequence[Tuple[float, float]],
    resistances: Sequence[Tuple[float, float]],
    lows: Sequence[float],
    highs: Sequence[float],
) -> Tuple[float, float]:
    support_candidates = [p for p, _ in supports if p <= current_price]
    resistance_candidates = [p for p, _ in resistances if p >= current_price]

    support_level = max(support_candidates) if support_candidates else (min(lows) if lows else current_price)
    resistance_level = min(resistance_candidates) if resistance_candidates else (max(highs) if highs else current_price)

    # Guard against inverted/touching levels from sparse pivots.
    if resistance_level <= support_level:
        span = max(current_price * 0.002, 1e-9)
        support_level = min(support_level, current_price - span)
        resistance_level = max(resistance_level, current_price + span)

    return support_level, resistance_level


def build_sr_future_path(
    current_price: float,
    supports: Sequence[Tuple[float, float]],
    resistances: Sequence[Tuple[float, float]],
    side: str,
    *,
    max_next_levels: int = 2,
) -> Dict[str, Any]:
    """Build the forward S/R map for one side of a trade thesis.

    Unlike `_nearest_levels` (which collapses the pivot lists to the single
    nearest level), this preserves the ORDERED map of future levels so the
    lifecycle can track progress toward the target and extend to the next
    level when a breakout/breakdown confirms continuation.

    Args:
        current_price: Price at signal/entry time.
        supports: (price, weight) pivot supports, any order.
        resistances: (price, weight) pivot resistances, any order.
        side: 'LONG' (anchor=support, target=resistance) or 'SHORT' (mirror).
        max_next_levels: How many levels beyond the target to keep.

    Returns:
        {
          "side": "LONG"|"SHORT",
          "anchor": float|None,          # level whose loss invalidates the thesis
          "target": float|None,          # first objective (opposing level)
          "next_levels": [float, ...],   # ordered objectives beyond the target
          "anchor_strength": float,
          "target_strength": float,
        }
    All prices are absolute; empty when the map cannot be built (caller treats
    missing as "no evidence", never as invalidation).
    """
    try:
        price = float(current_price)
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        return {"side": side, "anchor": None, "target": None, "next_levels": [],
                "anchor_strength": 0.0, "target_strength": 0.0}

    is_long = str(side or "").strip().upper() in ("LONG", "BUY")
    sup_sorted = sorted([(p, w) for p, w in supports if p > 0], key=lambda x: x[0], reverse=True)
    res_sorted = sorted([(p, w) for p, w in resistances if p > 0], key=lambda x: x[0])

    def _strength_for(level: Optional[float], pool: Sequence[Tuple[float, float]]) -> float:
        if level is None:
            return 0.0
        for p, w in pool:
            if abs(p - level) <= max(level * 0.001, 1e-12):
                return float(w or 0.0)
        return 0.0

    if is_long:
        below = [p for p, _ in sup_sorted if p <= price]
        above = [p for p, _ in res_sorted if p >= price]
        anchor = below[0] if below else None
        target = above[0] if above else None
        beyond = [p for p in above if target is not None and p > target][:max_next_levels]
        anchor_strength = _strength_for(anchor, sup_sorted)
        target_strength = _strength_for(target, res_sorted)
    else:
        above = [p for p, _ in res_sorted if p >= price]
        below = [p for p, _ in sup_sorted if p <= price]
        anchor = above[0] if above else None
        target = below[0] if below else None
        beyond = [p for p in below if target is not None and p < target]
        beyond = sorted(beyond, reverse=True)[:max_next_levels]
        anchor_strength = _strength_for(anchor, res_sorted)
        target_strength = _strength_for(target, sup_sorted)

    return {
        "side": "LONG" if is_long else "SHORT",
        "anchor": anchor,
        "target": target,
        "next_levels": beyond,
        "anchor_strength": anchor_strength,
        "target_strength": target_strength,
    }


def compute_sr_features(
    closes: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    volumes: Sequence[float],
    *,
    current_price: Optional[float] = None,
    atr: Optional[float] = None,
    lookback: Optional[int] = None,
    order: int = 3,
    near_band_atr_mult: float = 0.25,
    near_band_price_pct: float = 0.0025,
) -> Dict[str, Any]:
    """Compute support/resistance features consumed across the trading runtime."""
    n = min(len(closes), len(highs), len(lows), len(volumes))
    if n <= 0:
        return {
            "state": "unavailable",
            "reason": "no_candles",
            "support_level": None,
            "resistance_level": None,
            "sr_zone_position": 0.5,
            "sr_bounce": False,
            "sr_breakout": False,
            "sr_breakdown": False,
        }

    lb = min(n, int(lookback or n))
    c = [_safe_float(x) for x in closes[-lb:]]
    h = [_safe_float(x) for x in highs[-lb:]]
    l = [_safe_float(x) for x in lows[-lb:]]
    v = [max(_safe_float(x), 0.0) for x in volumes[-lb:]]

    px = _safe_float(current_price, c[-1] if c else 0.0)
    if px <= 0:
        px = c[-1] if c and c[-1] > 0 else 1e-9

    supports, resistances = _pivot_levels(h, l, v, order=order)
    support_level, resistance_level = _nearest_levels(px, supports, resistances, l, h)

    span = max(resistance_level - support_level, 1e-9)
    zone = (px - support_level) / span
    zone = max(0.0, min(1.0, zone))

    atr_val = _safe_float(atr, 0.0)
    if atr_val <= 0:
        atr_val = sum((hi - lo) for hi, lo in zip(h[-20:], l[-20:])) / max(1, min(20, len(h)))
    near_band = max(
        atr_val * max(0.0, _safe_float(near_band_atr_mult, 0.25)),
        px * max(0.0, _safe_float(near_band_price_pct, 0.0025)),
    )

    prev_close = c[-2] if len(c) >= 2 else px
    last_close = c[-1] if c else px
    prev_high = h[-2] if len(h) >= 2 else resistance_level
    prev_low = l[-2] if len(l) >= 2 else support_level

    near_support = abs(px - support_level) <= near_band
    near_resistance = abs(px - resistance_level) <= near_band

    sr_bounce = near_support and last_close >= prev_close and last_close >= support_level
    sr_breakout = last_close > resistance_level and prev_high <= resistance_level
    sr_breakdown = last_close < support_level and prev_low >= support_level
    sr_rejection = near_resistance and last_close <= prev_close and last_close <= resistance_level

    support_strength = _weighted_strength(supports)
    resistance_strength = _weighted_strength(resistances)

    mean_vol = sum(v[-20:]) / max(1, min(20, len(v)))
    last_vol = v[-1] if v else 0.0
    vol_ratio = (last_vol / mean_vol) if mean_vol > 0 else 1.0

    nearest_support_dist_pct = ((px - support_level) / px * 100.0) if px > 0 else 0.0
    nearest_resistance_dist_pct = ((resistance_level - px) / px * 100.0) if px > 0 else 0.0

    return {
        "state": "ok",
        "support_level": support_level,
        "resistance_level": resistance_level,
        "sr_zone_position": zone,
        "sr_bounce": bool(sr_bounce),
        "sr_breakout": bool(sr_breakout),
        "sr_breakdown": bool(sr_breakdown),
        "sr_rejection": bool(sr_rejection),
        "support_bounce_buy": bool(sr_bounce),
        "support_breakout_buy": bool(sr_breakout),
        "support_breakdown_sell": bool(sr_breakdown),
        "resistance_bounce_sell": bool(sr_rejection),
        "support_strength": float(support_strength),
        "resistance_strength": float(resistance_strength),
        "support_volume_ratio": float(vol_ratio if near_support else min(1.5, vol_ratio)),
        "resistance_volume_ratio": float(vol_ratio if near_resistance else min(1.5, vol_ratio)),
        "nearest_support_dist_pct": float(nearest_support_dist_pct),
        "nearest_resistance_dist_pct": float(nearest_resistance_dist_pct),
        "atr": float(atr_val),
        "lookback_used": lb,
        "order_used": int(max(1, order)),
        "near_band_used": float(near_band),
        "near_band_atr_mult": float(max(0.0, _safe_float(near_band_atr_mult, 0.25))),
        "near_band_price_pct": float(max(0.0, _safe_float(near_band_price_pct, 0.0025))),
        # Forward S/R path maps (ordered future levels) for thesis-anchored lifecycle.
        "sr_path_long": build_sr_future_path(px, supports, resistances, "LONG"),
        "sr_path_short": build_sr_future_path(px, supports, resistances, "SHORT"),
    }