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
_CACHE_TTL_SECONDS        = 240   # Re-detect every 4 minutes
_CONFIRM_CYCLES           = 2    # Anti-whipsaw: 2 consecutive detections before switching
_CONFIRM_CYCLES_FAST      = 2    # Was 1 (caused whipsaw every 5 min in bear+7d rebound)
_BEAR_7D_THRESHOLD        = -7.0  # BTC 7d < -7% → potential bear
_BEAR_30D_THRESHOLD       = -15.0 # BTC 30d < -15% → structural bear (faster confirmation)
_BEAR_SEVERE_30D          = -15.0 # Accelerate FUTURES_SHORT confirmation when 30d < this
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
    confidence: float = 0.5      # P3.1: 0.0-1.0 regime confidence score
    # Mode-specific parameter overrides (applied by run_trades.py)
    sl_pct: float = 1.8          # Stop-loss %
    tp_pct: float = 3.5          # Take-profit %
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
        elif ema9_vs_ema21 == "UP" and (btc_day_pct > _BULL_DAY_MIN or bb_bw > _BB_SIDEWAYS_MAX):
            mode   = "SPOT_BULL"
            regime = "ALCISTA"
            color  = "#4ecca3"
            reason = (f"Tendencia alcista: EMA9>EMA21, día={btc_day_pct:+.1f}%, "
                      f"7d={btc_7d_pct:+.1f}%, bb_bw={bb_bw:.3f}")
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


def _compute_confidence(raw: dict) -> float:
    """P3.1: Compute regime confidence [0-1] by counting aligned indicators.
    More indicators pointing to the detected mode → higher confidence.
    """
    mode = raw.get("mode", "SPOT_BULL")
    score = 0
    total = 5  # number of indicator checks

    if mode in ("PRESERVATION", "FUTURES_SHORT"):
        if raw.get("btc_30d_pct", 0) <= _BEAR_30D_THRESHOLD:    score += 1
        if raw.get("btc_7d_pct",  0) <= _BEAR_7D_THRESHOLD:     score += 1
        if raw.get("ema9_vs_ema21") == "DOWN":                   score += 1
        if raw.get("vol_ratio", 1.0) < 0.8:                      score += 1  # vol drying up
        if raw.get("btc_day_pct", 0) < -0.5:                     score += 1
    elif mode == "SPOT_BULL":
        if raw.get("btc_7d_pct",  0) >  0:                       score += 1
        if raw.get("btc_30d_pct", 0) > -5:                       score += 1
        if raw.get("ema9_vs_ema21") == "UP":                      score += 1
        if raw.get("vol_ratio", 1.0) >= 1.0:                     score += 1
        if raw.get("btc_day_pct", 0) > _BULL_DAY_MIN:            score += 1
    elif mode == "RANGE_SCALP":
        bw = raw.get("bb_bandwidth", 0.05)
        if bw < _BB_SIDEWAYS_MAX:                                 score += 1
        if abs(raw.get("btc_day_pct", 0)) < _SIDEWAYS_DAY_RANGE: score += 1
        if raw.get("vol_ratio", 1.0) < _SIDEWAYS_VOL_MAX:       score += 1
        if raw.get("ema9_vs_ema21") != "UP":                      score += 1
        if raw.get("btc_7d_pct", 0) > -5:                        score += 1
    else:
        return 0.5  # unknown mode

    raw_conf = score / total
    # Clamp: confirmed regime gets a floor of 0.5; unconfirmed gets a cap of 0.7
    return round(max(0.1, raw_conf), 2)


def _build_regime(raw: dict, confirmed: bool) -> MarketRegime:
    """Convert raw detection dict to MarketRegime with mode-specific parameters."""
    mode = raw["mode"]

    # Default parameters (SPOT_BULL) — aligned with config.py production defaults
    sl_pct           = 1.8
    tp_pct           = 3.5
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
        max_rsi          = 55.0  # only enter when RSI < 55 (bb_lower_touch is primary gate)
        require_bb_lower = True  # signal must come from BB lower band touch

    elif mode == "PRESERVATION":
        # Minimal risk: small positions, NARRATIVE only, strict vol requirement
        sl_pct         = 1.8
        tp_pct         = 3.0    # wider TP to compensate fewer entries
        kelly_mult     = 0.5    # half size
        max_rsi        = 65.0
        narrative_only = True   # only NARRATIVE tokens allowed

    elif mode == "FUTURES_SHORT":
        # Active bear market shorting via futures (2x leverage)
        sl_pct         = 3.0    # 3% above entry → 6% loss on margin at 2x
        tp_pct         = 7.0    # 7% below entry → 14% gain on margin at 2x (v4 OPT jun20)
        kelly_mult     = 0.5    # K2: base para no-confirmado (era 0.3 fijo)
        max_rsi        = 70.0   # enter shorts when RSI not yet extremely oversold

    confidence = _compute_confidence(raw)
    if not confirmed:
        confidence = min(confidence, 0.65)  # unconfirmed → cap at 65%

    # K2: Kelly dinámico por régimen+confidence — FUTURES_SHORT escala con convicción
    # Con SSO, trailing SL, breakeven y 9+ filtros de entrada, el 0.3 fijo era innecesariamente
    # conservador. Ahora el multiplier refleja la confianza real en el régimen bajista.
    if mode == "FUTURES_SHORT" and confirmed:
        if confidence >= 0.80:
            kelly_mult = 1.0    # Bear estructural fuerte + todos indicadores alineados
        elif confidence >= 0.60:
            kelly_mult = 0.8    # Bear confirmado con buena convicción
        else:
            kelly_mult = 0.6    # Bear confirmado pero con señales mixtas

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
        confidence       = confidence,
        sl_pct           = sl_pct,
        tp_pct           = tp_pct,
        kelly_multiplier = kelly_mult,
        min_rsi_entry    = min_rsi,
        max_rsi_entry    = max_rsi,
        require_bb_lower = require_bb_lower,
        narrative_only   = narrative_only,
    )


def get_regime(binance_client, force_refresh: bool = False, settings=None) -> MarketRegime:
    """
    Detect current market regime with anti-whipsaw (2-cycle confirmation).
    Cached for _CACHE_TTL_SECONDS.

    If settings.futures_enabled=True and regime is PRESERVATION,
    upgrades mode to FUTURES_SHORT (active shorting instead of just preserving).

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

    # ── Choose confirm threshold: accelerate in severe structural bear ────────
    _is_severe_bear = raw.get("btc_30d_pct", 0.0) <= _BEAR_SEVERE_30D
    _confirm_needed = _CONFIRM_CYCLES_FAST if _is_severe_bear else _CONFIRM_CYCLES
    if _is_severe_bear:
        print(f"[MarketRegime] Severe bear (30d={raw.get('btc_30d_pct',0):.1f}%) — fast confirm ({_confirm_needed} cycle)")

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
        if _cache["pending_count"] >= _confirm_needed:
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

    # ── Upgrade PRESERVATION → FUTURES_SHORT when futures enabled ────────────
    futures_on = settings is not None and getattr(settings, 'futures_enabled', True)
    if futures_on and confirmed_regime.mode == "PRESERVATION" and confirmed_regime.confirmed:
        raw_upgraded = dict(
            mode          = "FUTURES_SHORT",
            regime        = confirmed_regime.regime,
            color         = "#9b59b6",
            btc_7d_pct    = confirmed_regime.btc_7d_pct,
            btc_30d_pct   = confirmed_regime.btc_30d_pct,
            btc_day_pct   = confirmed_regime.btc_day_pct,
            vol_ratio     = confirmed_regime.vol_ratio,
            bb_bandwidth  = confirmed_regime.bb_bandwidth,
            ema9_vs_ema21 = confirmed_regime.ema9_vs_ema21,
            reason        = confirmed_regime.reason + " [FUTURES SHORT activo]",
        )
        confirmed_regime = _build_regime(raw_upgraded, confirmed=True)
        print(f"[MarketRegime] FUTURES_SHORT mode active (futures_enabled=True)")

    _cache["regime"] = confirmed_regime
    _cache["ts"]     = now

    # P3.1: Persist regime_confidence to trading_runtime_state
    try:
        import json as _json
        from init_components import get_components as _gc
        _db = _gc().db_client
        if _db:
            _payload = _json.dumps({
                'mode':       confirmed_regime.mode,
                'confidence': confirmed_regime.confidence,
                'confirmed':  confirmed_regime.confirmed,
                'reason':     confirmed_regime.reason,
                'btc_7d_pct': confirmed_regime.btc_7d_pct,
                'btc_30d_pct':confirmed_regime.btc_30d_pct,
            })
            _db.execute_update(
                "INSERT INTO trading_runtime_state (key, value, updated_at) "
                "VALUES ('regime_confidence', %s, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                (_payload, _payload)
            )
    except Exception:
        pass  # non-fatal — regime detection must never fail because of DB

    return confirmed_regime


def reset_cache() -> None:
    """Force next call to re-detect from scratch. Useful for testing."""
    global _cache
    _cache = {"regime": None, "ts": 0.0, "pending_mode": None, "pending_count": 0}


def prime_cache_from_db(db_client) -> None:
    """Initialize the in-memory regime cache from the last persisted regime_confidence row.

    Called once at the start of execute_once(). If the DB shows a confirmed regime
    that matches what the current raw detection would propose, we skip the 2-cycle
    anti-whipsaw delay so the *first* cycle after a restart can already trade.

    The anti-whipsaw is preserved for *regime changes* — it only relaxes when the
    cached mode equals the proposed mode (stable market, no flip).
    """
    global _cache
    if _cache.get("regime") is not None:
        return  # already initialized in this process — nothing to do
    if db_client is None:
        return
    try:
        import json as _json
        rows = db_client.execute_query(
            "SELECT value FROM trading_runtime_state WHERE key='regime_confidence'"
        )
        if not rows:
            return
        payload = rows[0].get("value") or {}
        if isinstance(payload, str):
            payload = _json.loads(payload)
        if not isinstance(payload, dict):
            return
        db_mode      = str(payload.get("mode") or "").strip()
        db_confirmed = bool(payload.get("confirmed", False))
        if not db_mode or not db_confirmed:
            return
        # Pre-load pending counter so the next get_regime() call confirms this mode
        # (mirrors what prime_cache() does for the severe-bear fast-path)
        _cache["pending_mode"]  = db_mode
        _cache["pending_count"] = _CONFIRM_CYCLES  # triggers confirmation on next call
        _cache["ts"] = -9999.0  # force re-detection so fresh data is used
        print(f"[MarketRegime] prime_cache_from_db: restored {db_mode} (confirmed in DB) — anti-whipsaw bypass for restart")
    except Exception as _e:
        print(f"[MarketRegime] prime_cache_from_db failed (non-fatal): {_e}")


def prime_cache(mode: str) -> None:
    """Pre-load the pending counter so the *next* get_regime() call confirms *mode*.
    Used by execute_once() to bypass the 2-cycle anti-whipsaw when market conditions
    are unambiguously severe (e.g. structural bear with BTC 30d < -15%).
    Only primes if the cache currently holds a different mode or is empty."""
    global _cache
    current = _cache.get("regime")
    already_confirmed = current is not None and current.mode == mode and current.confirmed
    if not already_confirmed:
        _cache["pending_mode"]  = mode
        _cache["pending_count"] = _CONFIRM_CYCLES  # will trigger confirmation on next call
        _cache["ts"] = -9999.0  # force cache expiry regardless of process uptime
        print(f"[MarketRegime] prime_cache: pre-loading {mode} confirmation (severe bear fast-path)")
