"""
market_regime.py — Market Regime Detection Engine

Detects the current market regime (SPOT_BULL / RANGE_SCALP / PRESERVATION)
using BTC technical indicators with a 2-cycle anti-whipsaw confirmation.

Regimes:
  SPOT_BULL    — BTC in uptrend, momentum present → buy breakouts with full Kelly
  RANGE_SCALP  — Sideways market, low bandwidth → buy BB support, sell BB resistance
  PRESERVATION — Bear market, BTC declining structurally → minimize exposure, NARRATIVE only

Usage:
    from market_regime import get_regime, MarketRegime
    regime = get_regime(binance_client)
    print(regime.mode)  # "SPOT_BULL" | "RANGE_SCALP" | "PRESERVATION"
"""

import time
from dataclasses import dataclass, field
from typing import Optional


# ── Constants ────────────────────────────────────────────────────────────────
_CACHE_TTL_SECONDS  = 240        # Re-detect every 4 minutes
_CONFIRM_CYCLES     = 2          # Anti-whipsaw: 2 consecutive detections before switching
_BEAR_7D_THRESHOLD  = -7.0       # BTC 7d < -7% → potential bear
_BEAR_30D_THRESHOLD = -15.0      # BTC 30d < -15% → structural bear (faster confirmation)
_SIDEWAYS_DAY_RANGE = 1.2        # |BTC day change| < 1.2% → not trending today
_SIDEWAYS_VOL_MAX   = 0.7        # vol_ratio < 0.7 → low conviction (sideways)
_BULL_DAY_MIN       = 0.5        # BTC day change > 0.5% → mild bull
_BB_SIDEWAYS_MAX    = 0.06       # BB bandwidth < 6% → lateral compression


@dataclass
class MarketRegime:
    mode: str                    # "SPOT_BULL" | "RANGE_SCALP" | "PRESERVATION"
    regime: str                  # Descriptive regime: "ALCISTA" | "LATERAL" | "BAJISTA" etc.
    color: str                   # Hex color for UI
    btc_7d_pct: float            # BTC 7-day % change
    btc_30d_pct: float           # BTC 30-day % change
    btc_day_pct: float           # BTC intraday % change
    vol_ratio: float             # Current vol vs 24h avg
    bb_bandwidth: float          # Bollinger bandwidth (normalized)
    ema9_vs_ema21: str           # "UP" | "DOWN" | "FLAT"
    confirmed: bool              # True if 2 consecutive cycles agreed
    reason: str                  # Human-readable explanation
    # Mode-specific parameter overrides (applied by run_trades.py)
    sl_pct: float = 1.5          # Stop-loss %
    tp_pct: float = 2.5          # Take-profit %
    kelly_multiplier: float = 1.0
    min_rsi_entry: float = 0.0   # Min RSI to enter (RANGE_SCALP uses this)
    max_rsi_entry: float = 100.0 # Max RSI to enter
    require_bb_lower: bool = False  # RANGE_SCALP: only enter at BB lower band
    narrative_only: bool = False    # PRESERVATION: only NARRATIVE tokens


# ── Module-level state ───────────────────────────────────────────────────────
_cache: dict = {
    "regime": None,
    "ts": 0.0,
    "pending_mode": None,      # mode detected last cycle (not yet confirmed)
    "pending_count": 0,        # consecutive cycles with pending_mode
}


def _ema(values: list, period: int) -> float:
    """Compute EMA of a list, return last value."""
    if not values:
        return 0.0
    k = 2.0 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def _bb_bandwidth(closes: list, period: int = 20) -> float:
    """Bollinger Bandwidth = (upper - lower) / middle, normalized."""
    if len(closes) < period:
        return 0.05
    window = closes[-period:]
    mid = sum(window) / period
    std = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
    upper = mid + 2 * std
    lower = mid - 2 * std
    return (upper - lower) / mid if mid > 0 else 0.05


def _detect_raw(binance_client) -> dict:
    """
    Run raw detection logic. Returns a dict with all metrics + proposed mode.
    Does NOT apply anti-whipsaw.
    """
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

    try:
        bc = binance_client.client  # python-binance Client

        # ── 1. Daily klines (30d) ────────────────────────────────────────────
        klines_1d = bc.get_klines(symbol="BTCUSDT", interval="1d", limit=32)
        closes_1d = [float(k[4]) for k in klines_1d]
        vols_1d   = [float(k[5]) for k in klines_1d]

        btc_7d_pct  = (closes_1d[-1] - closes_1d[-7])  / closes_1d[-7]  * 100 if len(closes_1d) >= 7  else 0.0
        btc_30d_pct = (closes_1d[-1] - closes_1d[-30]) / closes_1d[-30] * 100 if len(closes_1d) >= 30 else 0.0

        # ── 2. Intraday: 1h klines (25 bars) ────────────────────────────────
        klines_1h = bc.get_klines(symbol="BTCUSDT", interval="1h", limit=25)
        vols_1h   = [float(k[5]) for k in klines_1h]
        open_1d   = float(klines_1d[-1][1])
        cur_price = float(klines_1h[-1][4])
        btc_day_pct = (cur_price - open_1d) / open_1d * 100 if open_1d > 0 else 0.0

        avg_vol_1h = sum(vols_1h[:-1]) / max(len(vols_1h) - 1, 1)
        vol_ratio  = vols_1h[-1] / avg_vol_1h if avg_vol_1h > 0 else 1.0

        # ── 3. 4h klines for EMA9/21 trend + BB ─────────────────────────────
        klines_4h = bc.get_klines(symbol="BTCUSDT", interval="4h", limit=50)
        closes_4h = [float(k[4]) for k in klines_4h]

        ema9  = _ema(closes_4h, 9)
        ema21 = _ema(closes_4h, 21)
        ema9_vs_ema21 = "UP" if ema9 > ema21 * 1.001 else "DOWN" if ema9 < ema21 * 0.999 else "FLAT"

        bb_bw = _bb_bandwidth(closes_4h, 20)

        result.update({
            "btc_7d_pct":    round(btc_7d_pct,  2),
            "btc_30d_pct":   round(btc_30d_pct, 2),
            "btc_day_pct":   round(btc_day_pct, 2),
            "vol_ratio":     round(vol_ratio,    2),
            "bb_bandwidth":  round(bb_bw,        4),
            "ema9_vs_ema21": ema9_vs_ema21,
        })

        # ── 4. Classify regime ───────────────────────────────────────────────
        is_structural_bear = btc_30d_pct <= _BEAR_30D_THRESHOLD
        is_7d_bear         = btc_7d_pct  <= _BEAR_7D_THRESHOLD
        is_sideways_vol    = abs(btc_day_pct) < _SIDEWAYS_DAY_RANGE and vol_ratio < _SIDEWAYS_VOL_MAX
        is_sideways_bb     = bb_bw < _BB_SIDEWAYS_MAX
        is_ema_down        = ema9_vs_ema21 == "DOWN"

        if is_structural_bear or (is_7d_bear and is_ema_down):
            mode   = "PRESERVATION"
            regime = "BAJISTA"
            color  = "#e74c3c"
            reason = (f"Bear estructural: BTC 30d={btc_30d_pct:+.1f}%, 7d={btc_7d_pct:+.1f}%, "
                      f"EMA9{'<' if is_ema_down else '>'}EMA21")
        elif is_sideways_vol and is_sideways_bb:
            mode   = "RANGE_SCALP"
            regime = "LATERAL"
            color  = "#f39c12"
            reason = (f"Mercado lateral: día={btc_day_pct:+.1f}%, vol={vol_ratio:.1f}x, "
                      f"BB_bw={bb_bw:.3f}")
        elif is_7d_bear and not is_ema_down:
            # 7d bajista pero EMA todavía alcista → transición, modo defensivo
            mode   = "PRESERVATION"
            regime = "DÉBIL BAJA"
            color  = "#e67e22"
            reason = f"Tendencia bajista: BTC 7d={btc_7d_pct:+.1f}%, EMA9 aún UP"
        elif ema9_vs_ema21 == "UP" and btc_day_pct > _BULL_DAY_MIN:
            mode   = "SPOT_BULL"
            regime = "ALCISTA"
            color  = "#4ecca3"
            reason = (f"Tendencia alcista: EMA9>EMA21, día={btc_day_pct:+.1f}%, "
                      f"7d={btc_7d_pct:+.1f}%")
        elif ema9_vs_ema21 == "UP":
            mode   = "SPOT_BULL"
            regime = "DÉBIL ALZA"
            color  = "#4ecca3"
            reason = f"Alza débil: EMA9>EMA21, día={btc_day_pct:+.1f}%, vol={vol_ratio:.1f}x"
        else:
            mode   = "RANGE_SCALP"
            regime = "LATERAL"
            color  = "#f39c12"
            reason = f"Sin tendencia clara: EMA={ema9_vs_ema21}, día={btc_day_pct:+.1f}%"

        result.update({"mode": mode, "regime": regime, "color": color, "reason": reason})

    except Exception as e:
        result["reason"] = f"detection error: {e}"

    return result


def _build_regime(raw: dict, confirmed: bool) -> MarketRegime:
    """Convert raw detection dict to MarketRegime with mode-specific parameters."""
    mode = raw["mode"]

    # Default parameters (SPOT_BULL)
    sl_pct           = 1.5
    tp_pct           = 2.5
    kelly_mult       = 1.0
    min_rsi          = 0.0
    max_rsi          = 100.0
    require_bb_lower = False
    narrative_only   = False

    if mode == "RANGE_SCALP":
        # Tighter TP/SL for range trades; only enter at oversold levels
        sl_pct           = 1.2   # tighter SL (range has defined support)
        tp_pct           = 1.5   # small TP at BB upper
        kelly_mult       = 0.7   # reduce size in range market
        min_rsi          = 0.0
        max_rsi          = 40.0  # only enter when RSI < 40 (near BB lower)
        require_bb_lower = True  # signal must come from BB lower band touch

    elif mode == "PRESERVATION":
        # Minimal risk: small positions, NARRATIVE only, strict vol requirement
        sl_pct         = 1.5
        tp_pct         = 3.0    # wider TP to compensate fewer entries
        kelly_mult     = 0.5    # half size
        max_rsi        = 65.0
        narrative_only = True   # only NARRATIVE tokens allowed

    return MarketRegime(
        mode             = mode,
        regime           = raw["regime"],
        color            = raw["color"],
        btc_7d_pct       = raw["btc_7d_pct"],
        btc_30d_pct      = raw["btc_30d_pct"],
        btc_day_pct      = raw["btc_day_pct"],
        vol_ratio        = raw["vol_ratio"],
        bb_bandwidth     = raw["bb_bandwidth"],
        ema9_vs_ema21    = raw["ema9_vs_ema21"],
        confirmed        = confirmed,
        reason           = raw["reason"],
        sl_pct           = sl_pct,
        tp_pct           = tp_pct,
        kelly_multiplier = kelly_mult,
        min_rsi_entry    = min_rsi,
        max_rsi_entry    = max_rsi,
        require_bb_lower = require_bb_lower,
        narrative_only   = narrative_only,
    )


def get_regime(binance_client, force_refresh: bool = False) -> MarketRegime:
    """
    Detect current market regime with anti-whipsaw (2-cycle confirmation).
    Cached for _CACHE_TTL_SECONDS.

    Returns a MarketRegime with confirmed=True only after 2 consecutive
    cycles detect the same mode. During confirmation, returns current
    confirmed regime (or SPOT_BULL default if no prior regime).
    """
    global _cache
    now = time.monotonic()

    if not force_refresh and _cache["regime"] and (now - _cache["ts"]) < _CACHE_TTL_SECONDS:
        return _cache["regime"]

    raw = _detect_raw(binance_client)
    proposed_mode = raw["mode"]

    # ── Anti-whipsaw logic ───────────────────────────────────────────────────
    current_regime = _cache.get("regime")
    current_mode   = current_regime.mode if current_regime else "SPOT_BULL"

    if proposed_mode == current_mode:
        # Same as current — reset pending, confirm current
        _cache["pending_mode"]  = None
        _cache["pending_count"] = 0
        confirmed_regime = _build_regime(raw, confirmed=True)
        print(f"[MarketRegime] {proposed_mode} confirmed (stable)")

    elif proposed_mode == _cache["pending_mode"]:
        # Same as pending — increment counter
        _cache["pending_count"] += 1
        if _cache["pending_count"] >= _CONFIRM_CYCLES:
            # Confirmed switch
            confirmed_regime = _build_regime(raw, confirmed=True)
            _cache["pending_mode"]  = None
            _cache["pending_count"] = 0
            print(f"[MarketRegime] SWITCH confirmed: {current_mode} → {proposed_mode} | {raw['reason']}")
        else:
            # Not yet confirmed — return current regime (anti-whipsaw holding)
            confirmed_regime = current_regime if current_regime else _build_regime(raw, confirmed=False)
            print(f"[MarketRegime] Pending {proposed_mode} ({_cache['pending_count']}/{_CONFIRM_CYCLES}) | holding {current_mode}")

    else:
        # New different mode — start pending counter
        _cache["pending_mode"]  = proposed_mode
        _cache["pending_count"] = 1
        confirmed_regime = current_regime if current_regime else _build_regime(raw, confirmed=False)
        print(f"[MarketRegime] Proposed {proposed_mode} (1/{_CONFIRM_CYCLES}) | holding {current_mode} | {raw['reason']}")

    _cache["regime"] = confirmed_regime
    _cache["ts"]     = now
    return confirmed_regime


def reset_cache() -> None:
    """Force next call to re-detect from scratch. Useful for testing."""
    global _cache
    _cache = {"regime": None, "ts": 0.0, "pending_mode": None, "pending_count": 0}
