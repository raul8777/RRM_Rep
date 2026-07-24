from __future__ import annotations

from typing import Any, Dict, List, Tuple

SCHEMA_VERSION = "range_scalp_live_v1"
REQUIRED_FEATURES = (
    "expected_pnl_pct",
    "success_probability",
    "strength",
    "rsi",
    "rsi_4h",
    "vol_spike",
    "range_position",
    "bb_position",
    "atr",
    "trend_4h",
)
OPTIONAL_FEATURES = (
    "peak_quality",
    "composite_score",
    "fear_greed_value",
    "spread_bps_estimate",
)


def _first(meta: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = meta.get(key)
        if value is not None:
            return value
    return None


def _expected_pnl_pct(meta: Dict[str, Any]) -> Any:
    value = _first(meta, "expected_pnl_pct", "expected_pnl_percentage")
    if value is not None:
        return value
    try:
        expected = float(meta.get("expected_pnl"))
        size = float(meta.get("position_size_usdt") or meta.get("position_usdt"))
        return expected / size * 100.0 if size > 0 else None
    except (TypeError, ValueError):
        return None


def build_snapshot(meta: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str], float]:
    indicators = meta.get("indicators") if isinstance(meta.get("indicators"), dict) else {}
    snapshot = {
        "expected_pnl_pct": _expected_pnl_pct(meta),
        "success_probability": meta.get("success_probability"),
        "strength": _first(meta, "strength", "signal_strength"),
        "rsi": _first(meta, "rsi", "rsi_at_entry", "rsi_1h", "s_rsi_1h",) if _first(meta, "rsi", "rsi_at_entry", "rsi_1h", "s_rsi_1h") is not None else indicators.get("rsi"),
        "rsi_4h": _first(meta, "rsi_4h", "s_rsi_4h") if _first(meta, "rsi_4h", "s_rsi_4h") is not None else indicators.get("rsi_4h"),
        "vol_spike": _first(meta, "vol_spike", "s_vol_spike"),
        "range_position": _first(meta, "range_position", "s_range_position"),
        "bb_position": _first(meta, "bb_position", "s_bb_position"),
        "atr": _first(meta, "atr", "s_atr", "s_atr_pct"),
        "trend_4h": _first(meta, "trend_4h", "higher_tf_trend"),
        "peak_quality": meta.get("peak_quality"),
        "composite_score": meta.get("composite_score"),
        "fear_greed_value": meta.get("fear_greed_value"),
        "spread_bps_estimate": meta.get("spread_bps_estimate"),
    }
    missing = sorted(key for key in REQUIRED_FEATURES if snapshot.get(key) is None)
    completeness = (len(REQUIRED_FEATURES) - len(missing)) / len(REQUIRED_FEATURES)
    return snapshot, missing, completeness


def attach_snapshot(meta: Dict[str, Any]) -> Dict[str, Any]:
    snapshot, missing, completeness = build_snapshot(meta)
    meta["entry_feature_schema_version"] = SCHEMA_VERSION
    meta["entry_live_features"] = snapshot
    meta["entry_live_feature_missing"] = missing
    meta["entry_live_feature_completeness"] = completeness
    for key, value in snapshot.items():
        if meta.get(key) is None and value is not None:
            meta[key] = value
    return meta
