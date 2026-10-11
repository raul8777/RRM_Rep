"""position_thesis.py — consolidates the "entry thesis" of a position into a single,
regime-agnostic snapshot at open time.

STAGING s130 Belief-Driven Exit Lifecycle, Fase 1 (2026-08-16).

The raw data already exists scattered across the codebase (per-position
`pnl_prediction`, `indicators_at_entry`, `metadata['entry_decision_prediction']`
for `sr_discovery` candidates, `metadata['sr_entry_context']` for futures) but is
never consolidated into one place, and is only ever *used* for RANGE_SCALP
(`range_scalp_intelligent_hold.py`). This module extracts the same fields
uniformly for all 4 regimes (SPOT_BULL, RANGE_SCALP, FUTURES_SHORT,
PRESERVATION) and both trade types (spot/futures), so later stages (Fase 2+,
`position_belief.py`) have one canonical source of "what we believed when we
opened this" regardless of how the position was sourced.

This module ONLY captures data — it never makes or influences a close/hold
decision. Nothing here is a fixed floor/threshold; every field degrades to
`None` when missing, and the caller (Fase 2+) must treat missing dimensions as
"no evidence", never as "evidence of failure".
"""

from __future__ import annotations

from typing import Any, Dict, Optional
import math


def _f(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def build_thesis_snapshot(
    *,
    metadata: Optional[Dict[str, Any]] = None,
    pnl_prediction: Optional[Dict[str, Any]] = None,
    indicators_at_entry: Optional[Dict[str, Any]] = None,
    side: Optional[str] = None,
    regime_mode: Optional[str] = None,
    entry_price: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Build a uniform entry-thesis snapshot from whatever sources are available.

    Priority per field (first non-null wins):
      - target_price/stop_price/entry_success_probability:
          metadata['entry_decision_prediction'] (sr_discovery / staged path)
          -> pnl_prediction (predictor output at entry, all regimes)
      - support_level/resistance_level/entry_range_position:
          metadata['sr_entry_context'] (futures, s94+)
          -> indicators_at_entry (spot / legacy futures)

    Returns None only if literally no dimension could be extracted (fully
    degrades — callers must never treat that as "thesis invalidated").
    """
    meta = metadata if isinstance(metadata, dict) else {}
    pred = meta.get("entry_decision_prediction")
    if not isinstance(pred, dict):
        pred = pnl_prediction if isinstance(pnl_prediction, dict) else {}

    sr_ctx = meta.get("sr_entry_context")
    if not isinstance(sr_ctx, dict):
        sr_ctx = {}
    ind = indicators_at_entry if isinstance(indicators_at_entry, dict) else {}

    target_price = _f(pred.get("target_price"))
    stop_price = _f(pred.get("stop_price"))
    entry_success_probability = _f(
        pred.get("success_probability") if pred.get("success_probability") is not None
        else pred.get("probability")
    )

    support_level = _f(sr_ctx.get("support_at_entry"))
    if support_level is None:
        support_level = _f(ind.get("support_level"))

    resistance_level = _f(sr_ctx.get("resistance_at_entry"))
    if resistance_level is None:
        resistance_level = _f(ind.get("resistance_level"))

    entry_range_position = _f(sr_ctx.get("zone_at_entry"))
    if entry_range_position is None:
        entry_range_position = _f(ind.get("graph_range_position"))
    if entry_range_position is None:
        entry_range_position = _f(ind.get("sr_zone_position"))

    discovery_source = meta.get("discovery_source")
    resolved_entry_price = _f(entry_price)
    if resolved_entry_price is None:
        resolved_entry_price = _f(meta.get("current_price") or meta.get("price") or ind.get("current_price"))

    snapshot = {
        "entry_price": resolved_entry_price,
        "target_price": target_price,
        "stop_price": stop_price,
        "support_level": support_level,
        "resistance_level": resistance_level,
        "entry_range_position": entry_range_position,
        "entry_success_probability": entry_success_probability,
        "discovery_source": discovery_source,
        "side": str(side or "").strip().upper() or None,
        "regime_at_entry": str(regime_mode or "").strip().upper() or None,
    }

    if all(v is None for k, v in snapshot.items() if k not in ("entry_price", "side", "regime_at_entry")):
        return None

    sr_path = build_sr_path_v1(
        side=snapshot["side"],
        metadata=meta,
        indicators_at_entry=ind,
        support_level=support_level,
        resistance_level=resistance_level,
        target_price=target_price,
        stop_price=stop_price,
    )
    if sr_path:
        snapshot["sr_path_v1"] = sr_path
    return snapshot


def build_sr_path_v1(
    *,
    side: Optional[str],
    metadata: Optional[Dict[str, Any]],
    indicators_at_entry: Optional[Dict[str, Any]],
    support_level: Optional[float],
    resistance_level: Optional[float],
    target_price: Optional[float],
    stop_price: Optional[float],
) -> Optional[Dict[str, Any]]:
    """Persist the forward S/R path the position is expected to travel (sr_path_v1).

    This is the contract the lifecycle monitor anchors to: the level whose loss
    invalidates the thesis (anchor), the first objective (target), the ordered
    objectives beyond the target (next_levels — so a confirmed breakout extends
    instead of expiring), the primary timeframe of the setup, and the expected
    speed of travel expressed in candles of that timeframe
    (expected_candles = distance(entry→target) / atr_per_candle) — never in
    hardcoded seconds.

    Sources (first non-null wins):
      - indicators_at_entry['sr_path_long'/'sr_path_short'] — the ordered map
        produced by support_resistance.build_sr_future_path at signal time.
      - the already-resolved support/resistance/target/stop (fallback anchor).
      - indicators_at_entry['atr'] (price units) for the speed estimate.

    Returns None when there is no usable geometry at all (never fabricated).
    """
    meta = metadata if isinstance(metadata, dict) else {}
    ind = indicators_at_entry if isinstance(indicators_at_entry, dict) else {}
    side_up = str(side or "").strip().upper()
    is_long = side_up in ("LONG", "BUY")

    # Prefer the full ordered map computed at signal time.
    raw_path = ind.get("sr_path_long") if is_long else ind.get("sr_path_short")
    if not isinstance(raw_path, dict):
        raw_path = meta.get("sr_path_long") if is_long else meta.get("sr_path_short")
    if not isinstance(raw_path, dict):
        raw_path = None

    anchor = None
    next_levels: list = []
    anchor_strength = 0.0
    target_strength = 0.0
    if raw_path:
        anchor = _f(raw_path.get("anchor"))
        anchor_strength = _f(raw_path.get("anchor_strength")) or 0.0
        target_strength = _f(raw_path.get("target_strength")) or 0.0
        nl = raw_path.get("next_levels")
        if isinstance(nl, list):
            next_levels = [x for x in (_f(v) for v in nl) if x is not None]

    # Fallback anchor from the resolved S/R levels when no path map exists.
    if anchor is None:
        anchor = support_level if is_long else resistance_level
        anchor_strength = _f(ind.get("support_strength") if is_long else ind.get("resistance_strength")) or 0.0

    target = target_price
    if target is None:
        target = resistance_level if is_long else support_level
        target_strength = _f(ind.get("resistance_strength") if is_long else ind.get("support_strength")) or 0.0

    entry_price = _f(meta.get("current_price") or meta.get("price") or ind.get("current_price"))
    atr_candle = _f(ind.get("atr"))
    if atr_candle is None:
        atr_candle = _f(meta.get("atr"))
    if atr_candle is None:
        # atr_pct_at_entry is a PERCENTAGE (e.g. 1.5 = 1.5%), not price units —
        # convert before using it as per-candle travel distance, otherwise
        # expected_candles is off by a factor of price/100 for sub-$1 assets.
        _atr_pct = _f(meta.get("atr_pct_at_entry"))
        if _atr_pct is not None and entry_price and entry_price > 0:
            atr_candle = round(_atr_pct / 100.0 * entry_price, 10)

    expected_candles = None
    if entry_price and target and atr_candle and atr_candle > 0:
        distance = abs(target - entry_price)
        if distance > 0:
            expected_candles = round(distance / atr_candle, 3)

    primary_timeframe = (
        str(meta.get("sr_primary_timeframe") or "").strip()
        or str(ind.get("interval") or "").strip()
        or str(meta.get("signal_interval") or "").strip()
        or None
    )

    if anchor is None and target is None and not next_levels:
        return None

    return {
        "side": "LONG" if is_long else "SHORT",
        "anchor_level": anchor,
        "target_level": target,
        "next_levels": next_levels,
        "anchor_strength": anchor_strength,
        "target_strength": target_strength,
        "primary_timeframe": primary_timeframe,
        "atr_per_candle": atr_candle,
        "expected_candles": expected_candles,
        "entry_price": entry_price,
    }


def evaluate_sr_path_horizon(
    sr_path: Optional[Dict[str, Any]],
    *,
    min_candles: float,
    max_candles: float,
) -> Dict[str, Any]:
    """Evaluate whether a thesis's geometric horizon falls inside the target band.

    The band is expressed in candles of the primary timeframe — never seconds —
    and is derived from measured outcome distributions (Fase A), not hardcoded
    wall-clock rules. A thesis whose target is too far for the asset's current
    ATR (expected_candles > max) is not a short-horizon trade; one whose target
    is too close (expected_candles < min) has no room to pay costs.

    Returns:
        {"in_band": bool|None, "expected_candles": float|None, "reason": str}
        in_band=None when there is no usable geometry (never blocks on missing data).
    """
    path = sr_path if isinstance(sr_path, dict) else {}
    expected = _f(path.get("expected_candles"))
    if expected is None or expected <= 0:
        return {"in_band": None, "expected_candles": expected, "reason": "no_geometry"}
    if expected < float(min_candles):
        return {"in_band": False, "expected_candles": expected, "reason": "below_min_horizon"}
    if expected > float(max_candles):
        return {"in_band": False, "expected_candles": expected, "reason": "above_max_horizon"}
    return {"in_band": True, "expected_candles": expected, "reason": "in_band"}


def structural_stop_price(
    snapshot: Optional[Dict[str, Any]],
    entry_price: Optional[float] = None,
) -> Optional[float]:
    """Return the best available structural stop for the thesis.

    Prefers the predictor's explicit `stop_price`; falls back to the S/R level
    that would invalidate the thesis if broken (support for LONG, resistance
    for SHORT) when the predictor did not provide one directly.

    AIC-20261007-05: A minimum distance floor of 1.0% from entry is enforced.
    Structural stops closer than 1.0% are indistinguishable from market noise
    (typical ATR 4h = 0.8-2.0%) and cause premature belief_exhausted closures.
    This floor is a policy assumption, not proof of effectiveness.
    Runtime outcomes require separately authorized local evidence.
    """
    if not snapshot:
        return None
    side = str(snapshot.get("side") or "").strip().upper()
    entry = _f(entry_price)
    if entry is None:
        entry = _f(snapshot.get("entry_price"))

    stop_price = _f(snapshot.get("stop_price"))

    # When entry_price is unavailable, return raw stop/target without floor
    # (backward compatibility for callers like evaluate_belief that may not
    # have entry_price yet). Still validate the value is positive.
    if entry is None or entry <= 0:
        if stop_price is not None and stop_price > 0:
            return stop_price
        if side in ("LONG", "BUY"):
            val = _f(snapshot.get("support_level"))
            return val if val is not None and val > 0 else None
        if side in ("SHORT", "SELL"):
            val = _f(snapshot.get("resistance_level"))
            return val if val is not None and val > 0 else None
        return None

    # AIC-20261007-05: minimum structural stop distance floor (1.0% of entry)
    _struct_floor_pct = 0.01  # 1.0%

    if side in ("LONG", "BUY"):
        fallback = _f(snapshot.get("support_level"))
        candidates = (stop_price, fallback)
        raw = next((price for price in candidates if price is not None and 0 < price < entry), None)
        if raw is not None:
            floor_price = entry * (1.0 - _struct_floor_pct)
            if raw > floor_price:
                raw = floor_price  # push stop further from entry
        return raw
    elif side in ("SHORT", "SELL"):
        fallback = _f(snapshot.get("resistance_level"))
        candidates = (stop_price, fallback)
        raw = next((price for price in candidates if price is not None and price > entry), None)
        if raw is not None:
            floor_price = entry * (1.0 + _struct_floor_pct)
            if raw < floor_price:
                raw = floor_price  # push stop further from entry
        return raw
    return None


def structural_target_price(
    snapshot: Optional[Dict[str, Any]],
    entry_price: Optional[float] = None,
) -> Optional[float]:
    """Return the best available structural target ("dream" level) for the thesis.

    Prefers the predictor's explicit `target_price`; falls back to the
    opposite S/R level (resistance for LONG, support for SHORT).
    """
    if not snapshot:
        return None
    side = str(snapshot.get("side") or "").strip().upper()
    entry = _f(entry_price)
    if entry is None:
        entry = _f(snapshot.get("entry_price"))

    target_price = _f(snapshot.get("target_price"))

    # When entry_price is unavailable, return raw target without validation
    # Still reject non-positive values.
    if entry is None or entry <= 0:
        if target_price is not None and target_price > 0:
            return target_price
        if side in ("LONG", "BUY"):
            val = _f(snapshot.get("resistance_level"))
            return val if val is not None and val > 0 else None
        if side in ("SHORT", "SELL"):
            val = _f(snapshot.get("support_level"))
            return val if val is not None and val > 0 else None
        return None

    if side in ("LONG", "BUY"):
        fallback = _f(snapshot.get("resistance_level"))
        candidates = (target_price, fallback)
        return next((price for price in candidates if price is not None and price > entry), None)
    elif side in ("SHORT", "SELL"):
        fallback = _f(snapshot.get("support_level"))
        candidates = (target_price, fallback)
        return next((price for price in candidates if price is not None and 0 < price < entry), None)
    return None
