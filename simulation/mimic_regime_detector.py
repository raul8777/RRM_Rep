"""MIMIC — Historical market regime detector.

Replicates the logic in `market_regime._detect_raw()` but uses historical
OHLCV data loaded from local parquet files instead of live Binance API calls.
This lets simulations experience regime switches the same way production does.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# Mirrors market_regime.py constants
_BEAR_7D_THRESHOLD = -7.0
_BEAR_30D_THRESHOLD = -15.0
_BEAR_SEVERE_30D = -15.0
_SIDEWAYS_DAY_RANGE = 1.2
_SIDEWAYS_VOL_MAX = 0.7
_BULL_DAY_MIN = 0.5
_BB_SIDEWAYS_MAX = 0.06


def _ema(values: List[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2.0 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def _bb_bandwidth(closes: List[float], period: int = 20) -> float:
    if len(closes) < period:
        return 0.05
    window = closes[-period:]
    mid = sum(window) / period
    std = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
    upper = mid + 2 * std
    lower = mid - 2 * std
    return (upper - lower) / mid if mid > 0 else 0.05


def _detect_raw_from_bars(
    bars_1d: List[Dict[str, Any]],
    bars_1h: List[Dict[str, Any]],
    bars_4h: List[Dict[str, Any]],
    clock_now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Run raw regime detection using historical BTC bars up to the simulated clock."""
    result = {
        "mode": "SPOT_BULL",
        "regime": "?",
        "color": "#888",
        "btc_7d_pct": 0.0,
        "btc_30d_pct": 0.0,
        "btc_day_pct": 0.0,
        "vol_ratio": 1.0,
        "bb_bandwidth": 0.05,
        "ema9_vs_ema21": "FLAT",
        "reason": "detection failed",
    }

    if not bars_1d or not bars_1h or not bars_4h:
        return result

    try:
        # Filter bars up to current clock
        now_ts = clock_now.timestamp() if clock_now else bars_1h[-1]["open_time"]

        def _filter_upto(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            return [b for b in bars if b.get("open_time", b.get("timestamp", 0)) <= now_ts]

        klines_1d = _filter_upto(bars_1d)
        klines_1h = _filter_upto(bars_1h)
        klines_4h = _filter_upto(bars_4h)

        if len(klines_1d) < 7 or len(klines_1h) < 25 or len(klines_4h) < 50:
            result["reason"] = "insufficient historical bars"
            return result

        closes_1d = [float(b["close"]) for b in klines_1d]
        vols_1d = [float(b.get("volume", 0)) for b in klines_1d]

        btc_7d_pct = (closes_1d[-1] - closes_1d[-7]) / closes_1d[-7] * 100 if len(closes_1d) >= 7 else 0.0
        btc_30d_pct = (closes_1d[-1] - closes_1d[-30]) / closes_1d[-30] * 100 if len(closes_1d) >= 30 else 0.0

        vols_1h = [float(b.get("volume", 0)) for b in klines_1h]
        open_1d = float(klines_1d[-1]["open"])
        cur_price = float(klines_1h[-1]["close"])
        btc_day_pct = (cur_price - open_1d) / open_1d * 100 if open_1d > 0 else 0.0

        avg_vol_1h = sum(vols_1h[:-1]) / max(len(vols_1h) - 1, 1)
        vol_ratio = vols_1h[-1] / avg_vol_1h if avg_vol_1h > 0 else 1.0

        closes_4h = [float(b["close"]) for b in klines_4h]
        ema9 = _ema(closes_4h, 9)
        ema21 = _ema(closes_4h, 21)
        ema9_vs_ema21 = (
            "UP" if ema9 > ema21 * 1.001 else
            "DOWN" if ema9 < ema21 * 0.999 else
            "FLAT"
        )
        bb_bw = _bb_bandwidth(closes_4h, 20)

        result.update({
            "btc_7d_pct": round(btc_7d_pct, 2),
            "btc_30d_pct": round(btc_30d_pct, 2),
            "btc_day_pct": round(btc_day_pct, 2),
            "vol_ratio": round(vol_ratio, 2),
            "bb_bandwidth": round(bb_bw, 4),
            "ema9_vs_ema21": ema9_vs_ema21,
        })

        is_structural_bear = btc_30d_pct <= _BEAR_30D_THRESHOLD
        is_7d_bear = btc_7d_pct <= _BEAR_7D_THRESHOLD
        is_sideways_vol = abs(btc_day_pct) < _SIDEWAYS_DAY_RANGE and vol_ratio < _SIDEWAYS_VOL_MAX
        is_sideways_bb = bb_bw < _BB_SIDEWAYS_MAX
        is_ema_down = ema9_vs_ema21 == "DOWN"

        if is_structural_bear or (is_7d_bear and is_ema_down):
            mode = "PRESERVATION"
            regime = "BAJISTA"
            color = "#e74c3c"
            reason = (f"Bear estructural: BTC 30d={btc_30d_pct:+.1f}%, 7d={btc_7d_pct:+.1f}%, "
                      f"EMA9{'<' if is_ema_down else '>'}EMA21")
        elif is_sideways_vol and is_sideways_bb:
            mode = "RANGE_SCALP"
            regime = "LATERAL"
            color = "#f39c12"
            reason = (f"Mercado lateral: día={btc_day_pct:+.1f}%, vol={vol_ratio:.1f}x, "
                      f"BB_bw={bb_bw:.3f}")
        elif is_7d_bear and not is_ema_down:
            mode = "PRESERVATION"
            regime = "DÉBIL BAJA"
            color = "#e67e22"
            reason = f"Tendencia bajista: BTC 7d={btc_7d_pct:+.1f}%, EMA9 aún UP"
        elif ema9_vs_ema21 == "UP" and (btc_day_pct > _BULL_DAY_MIN or bb_bw > _BB_SIDEWAYS_MAX):
            mode = "SPOT_BULL"
            regime = "ALCISTA"
            color = "#4ecca3"
            reason = (f"Tendencia alcista: EMA9>EMA21, día={btc_day_pct:+.1f}%, "
                      f"7d={btc_7d_pct:+.1f}%, bb_bw={bb_bw:.3f}")
        elif ema9_vs_ema21 == "UP":
            mode = "SPOT_BULL"
            regime = "DÉBIL ALZA"
            color = "#4ecca3"
            reason = f"Alza débil: EMA9>EMA21, día={btc_day_pct:+.1f}%, vol={vol_ratio:.1f}x"
        else:
            mode = "RANGE_SCALP"
            regime = "LATERAL"
            color = "#f39c12"
            reason = f"Sin tendencia clara: EMA={ema9_vs_ema21}, día={btc_day_pct:+.1f}%"

        result.update({"mode": mode, "regime": regime, "color": color, "reason": reason})

    except Exception as e:
        result["reason"] = f"detection error: {e}"

    return result


def detect_regime_at(
    btc_bars_by_interval: Dict[str, List[Dict[str, Any]]],
    clock_now: datetime,
) -> Dict[str, Any]:
    """Convenience wrapper: pass a dict with '1d', '1h', '4h' BTC bars and a clock."""
    return _detect_raw_from_bars(
        btc_bars_by_interval.get("1d", []),
        btc_bars_by_interval.get("1h", []),
        btc_bars_by_interval.get("4h", []),
        clock_now=clock_now,
    )


def compute_confidence(raw: Dict[str, Any]) -> float:
    """Mirror of market_regime._compute_confidence() for historical results."""
    mode = raw.get("mode", "SPOT_BULL")
    score = 0
    total = 5

    if mode in ("PRESERVATION", "FUTURES_SHORT"):
        if raw.get("btc_30d_pct", 0) <= _BEAR_30D_THRESHOLD:
            score += 1
        if raw.get("btc_7d_pct", 0) <= _BEAR_7D_THRESHOLD:
            score += 1
        if raw.get("ema9_vs_ema21") == "DOWN":
            score += 1
        if raw.get("vol_ratio", 1.0) < 0.8:
            score += 1
        if raw.get("btc_day_pct", 0) < -0.5:
            score += 1
    elif mode == "SPOT_BULL":
        if raw.get("btc_7d_pct", 0) > 0:
            score += 1
        if raw.get("btc_30d_pct", 0) > -5:
            score += 1
        if raw.get("ema9_vs_ema21") == "UP":
            score += 1
        if raw.get("vol_ratio", 1.0) >= 1.0:
            score += 1
        if raw.get("btc_day_pct", 0) > _BULL_DAY_MIN:
            score += 1
    elif mode == "RANGE_SCALP":
        bw = raw.get("bb_bandwidth", 0.05)
        if bw < _BB_SIDEWAYS_MAX:
            score += 1
        if abs(raw.get("btc_day_pct", 0)) < _SIDEWAYS_DAY_RANGE:
            score += 1
        if raw.get("vol_ratio", 1.0) < _SIDEWAYS_VOL_MAX:
            score += 1
        if raw.get("ema9_vs_ema21") != "UP":
            score += 1
        if raw.get("btc_7d_pct", 0) > -5:
            score += 1
    else:
        return 0.5

    return round(max(0.1, score / total), 2)
