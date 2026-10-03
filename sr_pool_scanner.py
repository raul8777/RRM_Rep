from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Sequence

from config_helpers import get_param, get_staged_entry_regime_config
from support_resistance import compute_sr_features
from technical_indicators import TechnicalIndicators


# Runtime-facing diagnostics consumed by /sr_pool/status in app.py
_LAST_POOL_ANALYSIS: List[Dict[str, Any]] = []


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fetch_klines(client: Any, symbol: str, interval: str, limit: int) -> List[List[Any]]:
    try:
        return client.client.get_klines(symbol=symbol, interval=interval, limit=limit) or []
    except Exception:
        return []


def _resolve_sr_mode_config(settings: Any, regime_mode: str) -> Dict[str, Any]:
    regime = str(regime_mode or "RANGE_SCALP").upper()
    staged_cfg = get_staged_entry_regime_config(settings, regime) if settings is not None else {}
    return {
        "lookback": int(staged_cfg.get("sr_lookback_bars", 96) or 96),
        "pivot_order": int(staged_cfg.get("sr_pivot_order", 3) or 3),
        "near_band_atr_mult": float(staged_cfg.get("sr_near_band_atr_mult", 0.25) or 0.25),
        "near_band_price_pct": float(staged_cfg.get("sr_near_band_price_pct", 0.0025) or 0.0025),
        "pool_zone_support_max": float(staged_cfg.get("sr_pool_zone_support_max", 0.20) or 0.20),
        "pool_zone_resistance_min": float(staged_cfg.get("sr_pool_zone_resistance_min", 0.80) or 0.80),
        "min_strength": float(staged_cfg.get("sr_pool_min_strength", 0.0) or 0.0),
        "interval": str(get_param(settings, "sr_pool_interval", "5m") or "5m"),
        "limit": int(get_param(settings, "sr_pool_kline_limit", 120) or 120),
    }


def _sr_snapshot(
    client: Any,
    symbol: str,
    *,
    interval: str = "5m",
    limit: int = 120,
    lookback: int = 96,
    pivot_order: int = 3,
    near_band_atr_mult: float = 0.25,
    near_band_price_pct: float = 0.0025,
) -> Dict[str, Any]:
    rows = _fetch_klines(client, symbol, interval, limit)
    if len(rows) < 40:
        return {
            "ok": False,
            "symbol": symbol,
            "interval": interval,
            "reason": "insufficient_klines",
        }

    closes = [_safe_float(r[4]) for r in rows]
    highs = [_safe_float(r[2]) for r in rows]
    lows = [_safe_float(r[3]) for r in rows]
    vols = [_safe_float(r[5]) for r in rows]
    px = closes[-1] if closes else 0.0
    sr_atr = sum((h - l) for h, l in zip(highs[-20:], lows[-20:])) / max(1, min(20, len(highs)))
    atr_values = TechnicalIndicators.atr(highs, lows, closes, period=14)
    atr = atr_values[-1] if atr_values else sr_atr
    upper_values, middle_values, lower_values = TechnicalIndicators.bollinger_bands(closes, period=20, std_dev=2.0)
    rsi_values = TechnicalIndicators.rsi(closes, period=14)
    upper = upper_values[-1] if upper_values else None
    middle = middle_values[-1] if middle_values else None
    lower = lower_values[-1] if lower_values else None
    rsi = rsi_values[-1] if rsi_values else None
    volume_baseline = sum(vols[-21:-1]) / max(1, len(vols[-21:-1]))
    vol_spike = vols[-1] / volume_baseline if volume_baseline > 0 else None
    bands = {"lower": lower, "middle": middle, "upper": upper}
    indicators = {
        "price": px,
        "current_price": px,
        "interval": interval,
        "atr": atr,
        "atr_pct": atr / px * 100.0 if px > 0 else None,
        "lower_band": lower,
        "middle_band": middle,
        "upper_band": upper,
        "bollinger_bands": bands,
        "rsi": rsi,
        "vol_spike": vol_spike,
    }
    sr = compute_sr_features(
        closes,
        highs,
        lows,
        vols,
        current_price=px,
        atr=sr_atr,
        lookback=min(max(20, int(lookback or 96)), len(closes)),
        order=max(1, int(pivot_order or 3)),
        near_band_atr_mult=max(0.0, _safe_float(near_band_atr_mult, 0.25)),
        near_band_price_pct=max(0.0, _safe_float(near_band_price_pct, 0.0025)),
    )
    return {
        "ok": True,
        "symbol": symbol,
        "interval": interval,
        "current_price": px,
        "atr": atr,
        "atr_pct": atr / px * 100.0 if px > 0 else None,
        "indicators": indicators,
        "bb_lower": lower,
        "bb_middle": middle,
        "bb_upper": upper,
        "bb_position": (
            (px - lower) / (upper - lower)
            if lower is not None and upper is not None and upper > lower
            else None
        ),
        "bollinger_bands": bands,
        "rsi": rsi,
        "vol_spike": vol_spike,
        **sr,
    }


def scan_universe_for_pool(client: Any, universe: Sequence[str], regime_mode: str, settings: Any) -> List[str]:
    """Return symbols currently near S/R zones to monitor for confirmation breaks.

    This implementation is intentionally conservative and fail-open:
    - It only selects symbols with valid S/R snapshots.
    - It never throws; on errors it returns an empty list.
    """
    cfg = _resolve_sr_mode_config(settings, regime_mode)

    selected: List[str] = []
    for symbol in list(universe or []):
        snap = _sr_snapshot(
            client,
            symbol,
            interval=cfg["interval"],
            limit=cfg["limit"],
            lookback=cfg["lookback"],
            pivot_order=cfg["pivot_order"],
            near_band_atr_mult=cfg["near_band_atr_mult"],
            near_band_price_pct=cfg["near_band_price_pct"],
        )
        if not snap.get("ok"):
            continue
        zone = _safe_float(snap.get("sr_zone_position"), 0.5)
        near_support = zone <= cfg["pool_zone_support_max"]
        near_resistance = zone >= cfg["pool_zone_resistance_min"]
        if _safe_float(snap.get("support_strength"), 0.0) < cfg["min_strength"] and _safe_float(snap.get("resistance_strength"), 0.0) < cfg["min_strength"]:
            continue
        if near_support or near_resistance:
            selected.append(symbol)

    return selected


def detect_sr_pool_breaks(
    client: Any,
    symbols: Sequence[str],
    regime_mode: str,
    settings: Any,
    *,
    intra_cycle: bool = True,
    pool_symbols: Sequence[str] | None = None,
) -> List[Dict[str, Any]]:
    """Detect confirmed SR break/bounce events for pool symbols.

    Returns signal dictionaries compatible with run_trades consumers.
    """
    del intra_cycle, pool_symbols

    cfg = _resolve_sr_mode_config(settings, regime_mode)

    out: List[Dict[str, Any]] = []
    analysis_rows: List[Dict[str, Any]] = []
    ts = datetime.now(timezone.utc).isoformat()

    for symbol in list(symbols or []):
        snap = _sr_snapshot(
            client,
            symbol,
            interval=cfg["interval"],
            limit=cfg["limit"],
            lookback=cfg["lookback"],
            pivot_order=cfg["pivot_order"],
            near_band_atr_mult=cfg["near_band_atr_mult"],
            near_band_price_pct=cfg["near_band_price_pct"],
        )
        if not snap.get("ok"):
            analysis_rows.append(snap)
            continue

        zone = _safe_float(snap.get("sr_zone_position"), 0.5)
        if _safe_float(snap.get("support_strength"), 0.0) < cfg["min_strength"] and _safe_float(snap.get("resistance_strength"), 0.0) < cfg["min_strength"]:
            analysis_rows.append({
                "symbol": symbol,
                "regime": regime_mode,
                "zone": round(zone, 6),
                "side": None,
                "score": 0.0,
                "reason": "LOW_SR_STRENGTH",
                "timestamp": ts,
            })
            continue
        bounce = bool(snap.get("support_bounce_buy") or snap.get("sr_bounce"))
        breakout = bool(snap.get("support_breakout_buy") or snap.get("sr_breakout"))
        breakdown = bool(snap.get("support_breakdown_sell") or snap.get("sr_breakdown"))
        rejection = bool(snap.get("resistance_bounce_sell") or snap.get("sr_rejection"))

        side = None
        reason = ""
        score = 0.0
        if bounce or breakout:
            side = "LONG"
            reason = "SR_POOL_LONG_CONFIRM"
            score = max(0.55, min(0.95, 0.60 + (0.50 - zone) * 0.40))
        elif breakdown or rejection:
            side = "SHORT"
            reason = "SR_POOL_SHORT_CONFIRM"
            score = max(0.55, min(0.95, 0.60 + (zone - 0.50) * 0.40))

        row = {
            "symbol": symbol,
            "regime": regime_mode,
            "zone": round(zone, 6),
            "support": snap.get("support_level"),
            "resistance": snap.get("resistance_level"),
            "bounce": bounce,
            "breakout": breakout,
            "breakdown": breakdown,
            "rejection": rejection,
            "side": side,
            "score": round(score, 6),
            "timestamp": ts,
        }
        analysis_rows.append(row)

        if side:
            out.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "signal": "BUY" if side == "LONG" else "SELL",
                    "strength": round(score, 6),
                    "reason": reason,
                    "category": "sr_pool",
                    "regime": regime_mode,
                    "timestamp": ts,
                    "sr_quality_score": round(score, 6),
                    "sr_pool_profile": "SR_POOL_BREAK",
                    "near_support": zone <= cfg["pool_zone_support_max"],
                    "near_resistance": zone >= cfg["pool_zone_resistance_min"],
                    "current_price": snap.get("current_price"),
                    "price": snap.get("current_price"),
                    "interval": cfg["interval"],
                    "signal_interval": cfg["interval"],
                    "sr_primary_timeframe": cfg["interval"],
                    "atr": snap.get("atr"),
                    "atr_pct": snap.get("atr_pct"),
                    "indicators": snap.get("indicators"),
                    "bb_lower": snap.get("bb_lower"),
                    "bb_middle": snap.get("bb_middle"),
                    "bb_upper": snap.get("bb_upper"),
                    "bb_position": snap.get("bb_position"),
                    "bollinger_bands": snap.get("bollinger_bands"),
                    "rsi": snap.get("rsi"),
                    "vol_spike": snap.get("vol_spike"),
                    "support_level": snap.get("support_level"),
                    "resistance_level": snap.get("resistance_level"),
                    "support_strength": snap.get("support_strength"),
                    "resistance_strength": snap.get("resistance_strength"),
                    "sr_zone_position": snap.get("sr_zone_position"),
                    "sr_path_long": snap.get("sr_path_long"),
                    "sr_path_short": snap.get("sr_path_short"),
                    "support_bounce_buy": snap.get("support_bounce_buy"),
                    "support_breakout_buy": snap.get("support_breakout_buy"),
                    "support_breakdown_sell": snap.get("support_breakdown_sell"),
                    "resistance_bounce_sell": snap.get("resistance_bounce_sell"),
                    "sr_bounce": snap.get("sr_bounce"),
                    "sr_breakout": snap.get("sr_breakout"),
                    "sr_breakdown": snap.get("sr_breakdown"),
                    "sr_rejection": snap.get("sr_rejection"),
                }
            )

    _LAST_POOL_ANALYSIS.extend(analysis_rows)
    if len(_LAST_POOL_ANALYSIS) > 400:
        del _LAST_POOL_ANALYSIS[:-400]

    return out
