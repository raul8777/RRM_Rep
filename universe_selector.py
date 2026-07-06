"""Shared market-universe selection for analysis and signal refresh loops.

Centralizes regime/profile-aware choice so analysis_cycle, dashboard visibility,
and trading orchestration can reason about the same market context.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


_REGIME_CAPS = {
    "SPOT_BULL": 20,
    "RANGE_SCALP": 16,
    "PRESERVATION": 8,
    "FUTURES_SHORT": 14,
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

    base_cap = _REGIME_CAPS.get(_normalized_mode(regime_mode), 16)
    profile = _normalized_profile(getattr(settings, "current_regime_profile_label", None))
    multiplier = _PROFILE_MULTIPLIERS.get(profile or "B", 1.0)
    return max(1, int(round(base_cap * multiplier)))


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

    static_symbols = _normalize_list(getattr(settings, "analysis_symbols", []))
    futures_symbols = _normalize_list(getattr(settings, "futures_symbols", []))
    narrative_tokens = _normalize_list(getattr(settings, "narrative_tokens", []))

    screened: Dict[str, Any] = {"spot": [], "futures": [], "all": [], "_meta": {}}
    source = "static"
    reason = "fallback_static"

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

    screened_spot = _normalize_list((screened or {}).get("spot", []))
    screened_futures = _normalize_list((screened or {}).get("futures", []))

    if regime == "FUTURES_SHORT":
        primary = screened_futures + futures_symbols + static_symbols
    elif regime == "PRESERVATION":
        primary = screened_spot + narrative_tokens + static_symbols[:8]
    else:
        primary = screened_spot + static_symbols

    symbols = _dedupe_preserve_order(primary)[:cap]
    if not symbols:
        symbols = _dedupe_preserve_order(static_symbols or futures_symbols or narrative_tokens)[:cap]
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
