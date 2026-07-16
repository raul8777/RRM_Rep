"""lifecycle/profile_classifier.py — assign a candidate position to profile A/B/C/None.

The classifier is intentionally deterministic and transparent: it evaluates each
candidate against per-profile thresholds in preference order (A → B → C).  The
first profile whose thresholds are met wins; if none match, the candidate is
rejected.

Thresholds are keyed by the same names stored in Signal.features so that the
learning pipeline can update them from Super MIMIC sweep data.
"""

from __future__ import annotations

from typing import Dict, Optional

from lifecycle.base import MarketContext, ProfileThresholds, Signal


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def classify_profile(
    signal: Signal,
    ctx: MarketContext,
    profile_order: tuple,
    thresholds: Dict[str, ProfileThresholds],
) -> Optional[str]:
    """Return the first matching profile label or None if the candidate should be rejected."""
    feats = signal.features or {}
    profile_override = feats.get("peak_quality_profile")
    if profile_override in profile_order:
        return str(profile_override)

    composite = signal.composite_score
    success = signal.success_probability
    strength = signal.signal_strength
    expected = signal.expected_pnl
    rsi = _as_float(feats.get("rsi_4h"), 50.0)
    vol_spike = _as_float(feats.get("vol_spike"), 1.0)
    range_score = _as_float(feats.get("range_score"), 0.0)
    confluence = _as_float(feats.get("confluence"), 0.0)
    peak_quality = _as_float(feats.get("peak_quality"), 0.0)

    short_term_score = confluence * max(0.0, expected) * max(0.0, success)

    for profile in profile_order:
        t = thresholds.get(profile)
        if not t:
            continue
        if composite < t.min_composite_score:
            continue
        if success < t.min_success_probability:
            continue
        if strength < t.min_signal_strength:
            continue
        if expected < t.min_expected_pnl:
            continue
        if t.max_rsi_4h is not None and rsi > t.max_rsi_4h:
            continue
        if vol_spike < t.min_vol_spike:
            continue
        if range_score < t.min_range_score:
            continue
        if confluence < t.min_confluence:
            continue
        if short_term_score < t.min_short_term_score:
            continue
        if peak_quality < t.min_peak_quality:
            continue
        return profile
    return None
