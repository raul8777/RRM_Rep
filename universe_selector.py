"""Shared market-universe selection for analysis and signal refresh loops.

Centralizes regime/profile-aware choice so analysis_cycle, dashboard visibility,
and trading orchestration can reason about the same market context.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


_INVALID_ANALYSIS_SYMBOLS = {
    "EONUSDT", "ADDUSDT", "MEETONEUSDT", "ATDUSDT", "EOPUSDT", "ETHWUSDT",
    "USD1USDT", "PAXGUSDT", "XAUTUSDT", "RLUSDUSDT", "USDCUSDT", "BUSDUSDT",
    "TUSDUSDT", "FDUSDUSDT", "DAIUSDT", "USDDUSDT", "GUSDUSDT", "UUSDUSDT",
    "1000PEPEUSDT",
}


_REGIME_CAPS = {
    "SPOT_BULL": 48,
    "RANGE_SCALP": 40,
    "PRESERVATION": 24,
    "FUTURES_SHORT": 36,
}

_PROFILE_MULTIPLIERS = {
    "A": 1.2,
    "B": 1.0,
    "C": 0.8,
}


def _normalize_list(values: Any) -> List[str]:
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    return [str(v).strip().upper() for v in values if str(v).strip()]


def _dedupe_preserve_order(values: List[str]) -> List[str]:
    return list(dict.fromkeys([v for v in values if v]))


def _drop_invalid_symbols(values: List[str]) -> List[str]:
    return [v for v in values if v not in _INVALID_ANALYSIS_SYMBOLS]


def _normalized_mode(value: Optional[str]) -> str:
    mode = str(value or "").strip().upper()
    return mode or "SPOT_BULL"


def _normalized_profile(value: Optional[str]) -> Optional[str]:
    profile = str(value or "").strip().upper()
    return profile or None


def _resolve_cap(settings=None, regime_mode: str = "SPOT_BULL", max_symbols: Optional[int] = None) -> int:
    if max_symbols is not None:
        try:
            return max(1, int(max_symbols))
        except Exception:
            return 1

    _regime = _normalized_mode(regime_mode)
    base_cap = _REGIME_CAPS.get(_regime, 24)
    profile = _normalized_profile(getattr(settings, "current_regime_profile_label", None))
    multiplier = _PROFILE_MULTIPLIERS.get(profile or "B", 1.0)
    cap = max(1, int(round(base_cap * multiplier)))

    # Keep analysis breadth aligned with screener breadth so top-N quality
    # filtering has enough candidates to rank in short-term windows.
    if settings is not None:
        try:
            top_n = int(getattr(settings, "screener_top_n_count", 100) or 100)
        except Exception:
            top_n = 100
        try:
            max_pairs = int(getattr(settings, "screener_max_pairs", 50) or 50)
        except Exception:
            max_pairs = 50

        adaptive_floor = min(top_n, max(24, max_pairs * 2))
        if _regime == "PRESERVATION":
            adaptive_floor = max(12, adaptive_floor // 2)
        cap = max(cap, adaptive_floor)

    return cap


def select_analysis_universe(
    settings=None,
    regime_mode: Optional[str] = None,
    profile_label: Optional[str] = None,
    db_client=None,
    max_symbols: Optional[int] = None,
) -> Dict[str, Any]:
    """Return a regime-aware universe for technical analysis refresh."""

    regime = _normalized_mode(regime_mode or getattr(settings, "current_regime_mode", None))
    profile = _normalized_profile(profile_label or getattr(settings, "current_regime_profile_label", None))
    cap = _resolve_cap(settings=settings, regime_mode=regime, max_symbols=max_symbols)

    static_symbols = _drop_invalid_symbols(_normalize_list(getattr(settings, "analysis_symbols", [])))
    futures_symbols = _drop_invalid_symbols(_normalize_list(getattr(settings, "futures_symbols", [])))
    narrative_tokens = _drop_invalid_symbols(_normalize_list(getattr(settings, "narrative_tokens", [])))

    screened: Dict[str, Any] = {"spot": [], "futures": [], "all": [], "_meta": {}}
    source = "static"
    reason = "fallback_static"
    screener_enabled = bool(getattr(settings, 'screener_enabled', True))

    if screener_enabled:
        try:
            from pair_screener import get_screened_pairs

            screened = get_screened_pairs(
                settings=settings,
                regime_mode=regime,
                profile_label=profile,
                db_client=db_client,
            )
            source = "screener"
            reason = "screener_ok"
        except Exception as exc:
            reason = f"screener_error:{exc}"
    else:
        reason = "screener_disabled"

    screened_spot = _drop_invalid_symbols(_normalize_list((screened or {}).get("spot", [])))
    screened_futures = _drop_invalid_symbols(_normalize_list((screened or {}).get("futures", [])))

    if regime == "FUTURES_SHORT":
        primary = screened_futures + futures_symbols + static_symbols
    elif regime == "PRESERVATION":
        primary = screened_spot + narrative_tokens + static_symbols[:8]
    else:
        primary = screened_spot + static_symbols

    symbols = _drop_invalid_symbols(_dedupe_preserve_order(primary))[:cap]
    if not symbols:
        symbols = _drop_invalid_symbols(_dedupe_preserve_order(static_symbols or futures_symbols or narrative_tokens))[:cap]
        if symbols:
            source = "static"
            reason = "static_fallback"

    return {
        "ok": bool(symbols),
        "symbols": symbols,
        "spot": _dedupe_preserve_order(screened_spot),
        "futures": _dedupe_preserve_order(screened_futures),
        "regime_mode": regime,
        "profile_label": profile,
        "cap": cap,
        "source": source,
        "reason": reason,
        "screened": screened,
        "static_symbols": static_symbols,
        "futures_symbols": futures_symbols,
        "narrative_tokens": narrative_tokens,
    }
