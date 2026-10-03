"""candidate_scoring.py — Capa 2: composite scoring for discovery candidates.

Provides `expected_pnl_pct` and `success_probability` for candidates that do not
go through the ML predictor pipeline used by `autonomous_signals.py` (currently:
`sr_discovery`). Nothing here is a fabricated confidence value — every number is
either a real measured geometric distance or an already-calibrated system
threshold/empirical outcome:

- `expected_pnl_pct`: the actual % distance from current price to the opposing
  S/R level (the geometric target if the thesis confirms), net of round-trip
  trading costs (commission x2 + slippage, from `binance_commission`).
- `success_probability`: empirical win rate observed in `trades_closed` for
  previously closed `sr_discovery` candidates in the same regime/side, once a
  minimum sample exists; otherwise falls back to the regime's own calibrated
  `entry_decision_min_probability` floor (already used elsewhere in the system
  as the minimum acceptable bar) — never an invented per-candidate number.
- `sr_quality_score`: transparent 0-1 composite of structural strength and
  confirmed bounce/breakout/breakdown flags, used as the `strength` feature.

This module is intentionally side-effect-free (no DB writes) so it can be reused
by both the discovery path (`sr_pool_scanner.build_discovery_candidates`) and,
in a later phase, the screener's cheap ranking proxy.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from config_helpers import get_param

_MIN_HISTORICAL_SAMPLE = 20


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _round_trip_cost_pct(settings: Any, trade_type: str) -> float:
    """Real round-trip cost estimate: commission both legs + slippage buffer."""
    try:
        from binance_commission import get_config as _get_commission_config
        cfg = _get_commission_config(settings)
        rate = cfg.rate_for(trade_type)
        return (rate * 2.0 + cfg.slippage_pct) * 100.0
    except Exception:
        # Conservative fallback if commission config is unavailable — never 0.
        return 0.20


def compute_geometric_expected_pnl_pct(meta: Dict[str, Any], settings: Any) -> Optional[float]:
    """Expected % move to the opposing S/R level, net of round-trip costs.

    Returns None when the required geometry (current price + opposing level)
    is missing — callers must not substitute a fabricated number in that case.
    """
    price = _safe_float(meta.get("current_price") or meta.get("price"), 0.0)
    side = str(meta.get("expected_side") or "LONG").upper()
    trade_type = str(meta.get("trade_type") or "spot").lower()
    if price <= 0:
        return None

    if side == "LONG":
        target = meta.get("resistance_level")
    else:
        target = meta.get("support_level")
    target = _safe_float(target, 0.0)
    if target <= 0:
        return None

    raw_pct = ((target - price) / price * 100.0) if side == "LONG" else ((price - target) / price * 100.0)
    cost_pct = _round_trip_cost_pct(settings, trade_type)
    return raw_pct - cost_pct


def _historical_success_probability(
    db_client: Any,
    regime_mode: str,
    side: str,
    settings: Any,
) -> float:
    """Empirical win rate for closed sr_discovery candidates, else calibrated floor."""
    fallback_map = get_param(settings, "entry_decision_min_probability", {}) or {}
    fallback = _safe_float(fallback_map.get(str(regime_mode or "").upper()), 0.50)

    if db_client is None:
        return fallback
    try:
        rows = db_client.execute_query(
            """
            SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE tc.pnl > 0) AS wins
            FROM trades_closed tc
            JOIN ready_candidates rc ON rc.id = (tc.metadata->>'ready_candidate_id')::int
            WHERE rc.data->>'discovery_source' = 'sr_discovery'
              AND rc.regime_mode = %s
              AND (rc.data->'direction'->>'side') = %s
            """,
            (regime_mode, side),
        ) or []
    except Exception:
        return fallback

    row = rows[0] if rows else {}
    n = int(row.get("n") or 0)
    wins = int(row.get("wins") or 0)
    if n < _MIN_HISTORICAL_SAMPLE:
        return fallback
    return round(wins / n, 6)


def sr_quality_score(meta: Dict[str, Any]) -> float:
    """0-1 transparent composite of structural strength + confirmation flags."""
    support_strength = _safe_float(meta.get("support_strength"), 0.0)
    resistance_strength = _safe_float(meta.get("resistance_strength"), 0.0)
    side = str(meta.get("expected_side") or "LONG").upper()
    base_strength = support_strength if side == "LONG" else resistance_strength

    confirmed = bool(
        meta.get("sr_bounce") or meta.get("sr_breakout") or meta.get("sr_breakdown")
        or meta.get("support_bounce_buy") or meta.get("support_breakout_buy")
        or meta.get("support_breakdown_sell") or meta.get("resistance_bounce_sell")
    )
    confirmation_bonus = 0.15 if confirmed else 0.0
    return round(max(0.0, min(1.0, base_strength + confirmation_bonus)), 6)


def compute_composite_score(
    meta: Dict[str, Any],
    regime_mode: str,
    settings: Any,
    db_client: Any = None,
) -> Dict[str, Any]:
    """Populate meta in-place with expected_pnl, success_probability, strength.

    Returns the same meta dict for convenience. Does not overwrite fields that
    are already present (e.g. from an ML predictor) — only fills gaps for
    discovery sources that lack them.
    """
    side = str(meta.get("expected_side") or "LONG").upper()
    regime = str(regime_mode or "").upper()

    expected_pnl = compute_geometric_expected_pnl_pct(meta, settings)
    if expected_pnl is not None:
        # `expected_pnl`/`expected_pnl_pct` feed entry_profile_gate's
        # `_expected_return_pct()`. For RANGE_SCALP, `_range_scalp_score()`
        # treats that field as a CONTRARIAN short-horizon ML forecast (a
        # negative predictor value signals an oversold bounce), not a
        # geometric target distance — feeding our positive SR-distance number
        # into it would actively penalize good setups. Keep the real number
        # under its own name for audit/scoring elsewhere, and only feed the
        # gate-visible fields for regimes where `_expected_return_pct` is used
        # directly (non-contrarian) as the quality score.
        meta["sr_geometric_expected_pnl_pct"] = expected_pnl
        if regime != "RANGE_SCALP" and meta.get("expected_pnl") is None:
            meta["expected_pnl"] = expected_pnl
            meta["expected_pnl_pct"] = expected_pnl
            meta["expected_pnl_source"] = "sr_geometry"

    if meta.get("success_probability") is None:
        meta["success_probability"] = _historical_success_probability(
            db_client, regime_mode, side, settings,
        )
        meta["success_probability_source"] = "empirical_or_calibrated_floor"

    # entry_decision.evaluate() reads target_price/stop_price from
    # meta["entry_decision_prediction"], never from top-level SR fields. Without
    # this, RANGE_SCALP staging fails downstream with DIRECTION_TARGET_MISSING
    # even after passing entry_profile_gate. The S/R levels themselves ARE the
    # thesis's natural target/invalidation — target = opposing level, stop = the
    # level being tested (breaking it invalidates the bounce/rejection thesis).
    if meta.get("entry_decision_prediction") is None:
        target_price = meta.get("resistance_level") if side == "LONG" else meta.get("support_level")
        stop_price = meta.get("support_level") if side == "LONG" else meta.get("resistance_level")
        meta["entry_decision_prediction"] = {
            "expected_pnl": expected_pnl,
            "expected_pnl_percentage": expected_pnl,
            "success_probability": meta.get("success_probability"),
            "target_price": target_price,
            "stop_price": stop_price,
            "model": "sr_geometry_v1",
        }

    if meta.get("strength") is None or _safe_float(meta.get("strength"), 0.0) <= 0.0:
        meta["strength"] = sr_quality_score(meta)

    meta.setdefault("composite_score_version", "v1_geometry_empirical")
    return meta
