"""entry_profile_gate.py — tiered A/B/C entry gate.

The gate replaces the fragmented predictor/peak-quality/override logic with a
single, calibrated decision surface:

  A : top quality — only the best expected risk-adjusted setups are accepted.
  B : second tier — lower expected edge than A, but still clearly positive.
  C : minimum acceptable quality — must guarantee a small positive expected
      net return after costs.  Anything below C is discarded.

The quality score is a per-regime composite z-score computed from the same
features used by the Super MIMIC predictive pipeline.  Thresholds are learned
from historical peak/outcome data and stored in:

    Dev/benchmarks/entry_profile_calibration/entry_profile_thresholds.json

"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_STATS_PATH = Path("Dev/benchmarks/super_mimic/predictive_sweep/peak_quality_stats.json")
_DEFAULT_THRESHOLDS_PATH = Path("Dev/benchmarks/entry_profile_calibration/entry_profile_thresholds.json")
_QUALITY_MODEL_PATH = Path("Dev/benchmarks/entry_profile_calibration/quality_score_model_spot_bull.pkl")
_QUALITY_MODEL_PATHS = {
    "SPOT_BULL": _QUALITY_MODEL_PATH,
    "RANGE_SCALP": Path("Dev/benchmarks/entry_profile_calibration/quality_score_model_range_scalp.pkl"),
}

_UNIVERSAL_MODEL_PATH = Path("Dev/benchmarks/research_data/quality_score_model_universal_proposal.pkl")

_CATEGORIES = {
    "LARGE_CAP": {"bonus": -0.02},
    "NARRATIVE": {"bonus": 0.02},
    "MIDCAP_ALT": {"bonus": 0.0},
}

_stats_cache: Optional[Dict[str, Any]] = None
_thresholds_cache: Optional[Dict[str, Any]] = None
_model_cache: Dict[str, Optional[Dict[str, Any]]] = {}
_model_load_attempted: Dict[str, bool] = {}
_universal_model_cache: Optional[Dict[str, Any]] = None
_universal_model_attempted: bool = False


_UNIVERSAL_FEATURES = [
    "rsi_1h", "rsi_4h", "bb_position", "atr_pct",
    "vol_spike", "range_position", "trend_1h", "trend_4h",
    "sr_zone_position", "sr_support_dist_pct", "sr_resistance_dist_pct",
]

_QUALITY_SCORE_FEATURES = [
    "expected_pnl_pct",
    "peak_quality",
    "composite_score",
    "confluence",
    "short_term_score",
    "success_probability",
    "signal_strength",
    "range_score",
    "vol_score",
    "rsi_score",
    "pnl_score",
    "symbol_quality_score",
    "rsi_4h",
    "vol_spike",
    "range_position",
    "trend_4h",
    "atr",
]


def _load_quality_score_artifact(regime: str, path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    global _model_cache, _model_load_attempted
    regime = str(regime or "SPOT_BULL").upper()
    if not isinstance(_model_cache, dict):
        _model_cache = {}
    if not isinstance(_model_load_attempted, dict):
        _model_load_attempted = {}
    cache_key = f"{regime}:{path or ''}"
    if _model_load_attempted.get(cache_key):
        return _model_cache.get(cache_key)
    _model_load_attempted[cache_key] = True
    model_path = Path(path) if path else (_QUALITY_MODEL_PATH if regime == "SPOT_BULL" else _QUALITY_MODEL_PATHS.get(regime))
    if model_path is None or not model_path.exists():
        _model_cache[cache_key] = None
        return None
    try:
        import joblib
        artifact = joblib.load(model_path)
        if not isinstance(artifact, dict) or artifact.get("model") is None:
            artifact = {"model": artifact, "features": _QUALITY_SCORE_FEATURES, "regime": regime}
        artifact.setdefault("features", _QUALITY_SCORE_FEATURES)
        artifact.setdefault("regime", regime)
        _model_cache[cache_key] = artifact
        return artifact
    except Exception as e:
        message = f"Could not load quality score model: {e}" if regime == "SPOT_BULL" else f"Could not load {regime} quality score model: {e}"
        logger.warning(message)
        _model_cache[cache_key] = None
        return None


def _load_quality_score_model(regime: Optional[str] = None, path: Optional[str] = None) -> Optional[Any]:
    artifact = _load_quality_score_artifact(regime or "SPOT_BULL", path)
    if regime is None and path is None:
        return artifact.get("model") if artifact else None
    return artifact


def _has_quality_signal_features(signal_meta: Dict[str, Any]) -> bool:
    """Return True when the signal has the predictor output and enough quality features
    for the learned model to be meaningful.  This avoids forcing synthetic test signals
    through a model trained on full Super-MIMIC feature vectors.
    """
    has_predictor = (
        signal_meta.get("expected_pnl_pct") is not None
        or signal_meta.get("expected_pnl") is not None
        or signal_meta.get("expected_pnl_percentage") is not None
    )
    if not has_predictor:
        return False
    quality_keys = {
        "peak_quality",
        "composite_score",
        "confluence",
        "short_term_score",
        "range_score",
        "vol_score",
        "rsi_score",
        "symbol_quality_score",
    }
    return any(k in signal_meta and signal_meta.get(k) is not None for k in quality_keys)


def _model_quality_score(
    signal_meta: Dict[str, Any], regime: str = "SPOT_BULL", path: Optional[str] = None,
) -> Tuple[Optional[float], List[str], Optional[str]]:
    artifact = _load_quality_score_artifact(regime, path)
    if artifact is None:
        return None, [], None
    if not _has_quality_signal_features(signal_meta):
        return None, list(artifact.get("features") or _QUALITY_SCORE_FEATURES), artifact.get("trained_at")
    try:
        import numpy as np
    except Exception:
        return None, [], artifact.get("trained_at")
    values = []
    missing = []
    features = list(artifact.get("features") or _QUALITY_SCORE_FEATURES)
    for key in features:
        value = _expected_return_pct(signal_meta) if key == "expected_pnl_pct" else signal_meta.get(key)
        if value is None:
            missing.append(key)
            value = 0.0
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            missing.append(key)
            values.append(0.0)
    if missing:
        return None, sorted(set(missing)), artifact.get("trained_at")
    try:
        score = float(artifact["model"].predict(np.array([values]))[0])
    except Exception as e:
        logger.debug(f"{regime} model prediction failed: {e}")
        return None, [], artifact.get("trained_at")
    return score, [], artifact.get("trained_at")


def _as_float(v: Any, default: float = 0.0) -> float:
    try:
        if isinstance(v, dict):
            v = v.get("value", v.get("v"))
        return float(v)
    except Exception:
        return default


def _first_present(*candidates: Any) -> Any:
    """Return the first candidate that is not None.

    Plain `a or b or c` chaining is wrong for numeric features: a falsy-but-
    legitimate value (most importantly `0.0` — e.g. a symbol sitting exactly
    at a support level, or a 0% distance to a level) gets silently skipped in
    favor of the next candidate/default, corrupting the strongest possible SR
    signal into a "missing data" neutral value. See
    calibracion_entradas-a20ab8-v3.md (s106b).
    """
    for c in candidates:
        if c is not None:
            return c
    return None


def _symbol_category(symbol: str) -> str:
    if symbol in {
        "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
        "ADAUSDT", "DOGEUSDT", "TRXUSDT",
    }:
        return "LARGE_CAP"
    if symbol in {
        "FETUSDT", "TAOUSDT", "RENDERUSDT", "NEARUSDT", "WLDUSDT",
        "AGIXUSDT", "OCEANUSDT", "ARBUSDT", "MATICUSDT", "ATOMUSDT",
        "INJUSDT", "APTUSDT", "SUIUSDT", "SEIUSDT", "TIAUSDT",
        "PYTHUSDT", "JTOUSDT", "BRETTUSDT", "EIGENUSDT",
    }:
        return "NARRATIVE"
    return "MIDCAP_ALT"


def load_stats(path: Optional[str] = None) -> Dict[str, Any]:
    global _stats_cache
    if _stats_cache is not None:
        return _stats_cache
    p = Path(path) if path else _DEFAULT_STATS_PATH
    if not p.exists():
        logger.warning(f"peak_quality_stats not found at {p}; gate will use heuristic fallback")
        _stats_cache = {}
        return _stats_cache
    try:
        with open(p, "r", encoding="utf-8") as f:
            _stats_cache = json.load(f)
    except Exception as e:
        logger.error(f"Failed to load peak_quality_stats: {e}")
        _stats_cache = {}
    return _stats_cache or {}


def load_thresholds(path: Optional[str] = None) -> Dict[str, Any]:
    global _thresholds_cache
    if _thresholds_cache is not None:
        return _thresholds_cache
    p = Path(path) if path else _DEFAULT_THRESHOLDS_PATH
    if not p.exists():
        logger.warning(f"entry_profile_thresholds not found at {p}; using conservative heuristic defaults")
        _thresholds_cache = _default_thresholds()
        return _thresholds_cache
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # Support both legacy {thresholds: {regime: {...}}} and new universal flat layout.
        if isinstance(raw, dict) and "thresholds" in raw and isinstance(raw["thresholds"], dict):
            normalized = dict(raw["thresholds"])
            if isinstance(normalized, dict) and "meta" not in normalized:
                normalized["meta"] = raw.get("meta", {})
            _thresholds_cache = normalized
        elif isinstance(raw, dict):
            _thresholds_cache = raw
        else:
            _thresholds_cache = _default_thresholds()
    except Exception as e:
        logger.error(f"Failed to load entry_profile_thresholds: {e}")
        _thresholds_cache = _default_thresholds()
    return _thresholds_cache or _default_thresholds()


def _is_universal_thresholds(thresholds_data: Dict[str, Any]) -> bool:
    if not isinstance(thresholds_data, dict):
        return False
    meta_version = str(thresholds_data.get("meta", {}).get("version", "")).lower()
    if meta_version.startswith("universal"):
        return True
    return any("__" in k for k in thresholds_data.keys() if isinstance(k, str) and k != "meta")


def _load_universal_quality_model() -> Optional[Dict[str, Any]]:
    global _universal_model_cache, _universal_model_attempted
    if _universal_model_attempted:
        return _universal_model_cache
    _universal_model_attempted = True
    if not _UNIVERSAL_MODEL_PATH.exists():
        _universal_model_cache = None
        return None
    try:
        import joblib
        artifact = joblib.load(_UNIVERSAL_MODEL_PATH)
        if not isinstance(artifact, dict):
            artifact = None
        _universal_model_cache = artifact
        return artifact
    except Exception as e:
        logger.warning(f"Could not load universal quality model: {e}")
        _universal_model_cache = None
        return None


def _normalize_trend(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, str):
        value = value.strip().upper()
        if value in ("UP", "BULL", "BULLISH", "1"):
            return 1.0
        if value in ("DOWN", "BEAR", "BEARISH", "-1"):
            return -1.0
        return 0.0
    try:
        f = float(value)
        if f > 0:
            return 1.0
        if f < 0:
            return -1.0
        return 0.0
    except (TypeError, ValueError):
        return 0.0


def _extract_universal_features(signal_meta: Dict[str, Any]) -> List[float]:
    indicators = signal_meta.get("indicators") or signal_meta.get("indicators_at_entry") or {}
    if not isinstance(indicators, dict):
        indicators = {}
    price = _as_float(
        _first_present(
            signal_meta.get("current_price"),
            signal_meta.get("price"),
            indicators.get("price"),
        ),
        default=1.0,
    )
    if price <= 0:
        price = 1.0

    rsi_1h = _as_float(
        _first_present(
            signal_meta.get("rsi"),
            signal_meta.get("rsi_1h"),
            indicators.get("rsi"),
            indicators.get("rsi_1h"),
        ),
        default=50.0,
    )
    rsi_4h = _as_float(
        _first_present(signal_meta.get("rsi_4h"), indicators.get("rsi_4h")),
        default=rsi_1h,
    )

    bb_position = _as_float(signal_meta.get("bb_position"), default=None)
    if bb_position is None:
        upper = _as_float(indicators.get("upper_band"), default=None)
        lower = _as_float(indicators.get("lower_band"), default=None)
        if upper is not None and lower is not None and upper > lower:
            bb_position = (price - lower) / (upper - lower)
        else:
            bb_position = 0.5

    atr = _as_float(_first_present(signal_meta.get("atr"), indicators.get("atr")), default=0.0)
    atr_pct = _as_float(
        _first_present(signal_meta.get("atr_pct"), indicators.get("atr_pct")),
        default=(atr / price * 100 if price and atr else 0.0),
    )

    vol_spike = _as_float(
        _first_present(
            signal_meta.get("vol_spike"),
            signal_meta.get("volume_spike"),
            indicators.get("vol_spike"),
            indicators.get("volume_spike"),
        ),
        default=1.0,
    )

    range_position = _as_float(
        _first_present(
            signal_meta.get("range_position"),
            signal_meta.get("weekly_range"),
            indicators.get("range_position"),
            indicators.get("weekly_range"),
        ),
        default=bb_position,
    )

    trend_1h = _normalize_trend(
        _first_present(
            signal_meta.get("trend_1h"),
            indicators.get("trend_1h"),
            indicators.get("ema_cross"),
        )
    )
    if trend_1h == 0.0:
        trend_1h = 1.0 if str(signal_meta.get("signal", "")).upper() == "BUY" else -1.0

    trend_4h = _normalize_trend(
        _first_present(
            signal_meta.get("trend_4h"),
            indicators.get("trend_4h"),
            signal_meta.get("higher_tf_trend"),
        )
    )

    sr_zone_position = _as_float(
        _first_present(signal_meta.get("sr_zone_position"), indicators.get("sr_zone_position")),
        default=0.5,
    )
    sr_support_dist_pct = _as_float(
        _first_present(
            signal_meta.get("nearest_support_dist_pct"),
            signal_meta.get("sr_support_dist_pct"),
            indicators.get("nearest_support_dist_pct"),
            indicators.get("sr_support_dist_pct"),
        ),
        default=0.0,
    )
    sr_resistance_dist_pct = _as_float(
        _first_present(
            signal_meta.get("nearest_resistance_dist_pct"),
            signal_meta.get("sr_resistance_dist_pct"),
            indicators.get("nearest_resistance_dist_pct"),
            indicators.get("sr_resistance_dist_pct"),
        ),
        default=0.0,
    )

    return [
        rsi_1h,
        rsi_4h,
        bb_position,
        atr_pct,
        vol_spike,
        range_position,
        trend_1h,
        trend_4h,
        sr_zone_position,
        sr_support_dist_pct,
        sr_resistance_dist_pct,
    ]


def _universal_quality_score(
    signal_meta: Dict[str, Any], regime: str, symbol: str
) -> Tuple[Optional[float], List[str], Optional[str]]:
    artifact = _load_universal_quality_model()
    if artifact is None:
        return None, [], None
    cat = _symbol_category(symbol)
    key = f"{regime}__{cat}"
    entry = artifact.get(key) or artifact.get(regime) or artifact.get("__global__")
    if entry is None or not isinstance(entry, dict):
        return None, [], artifact.get("meta", {}).get("trained_at")
    model = entry.get("model")
    if model is None:
        return None, [], artifact.get("meta", {}).get("trained_at")
    try:
        import pandas as pd
        features = _extract_universal_features(signal_meta)
        df = pd.DataFrame([features], columns=_UNIVERSAL_FEATURES)
        score = float(model.predict_proba(df)[0, 1])
        return score, [], artifact.get("meta", {}).get("trained_at")
    except Exception as e:
        logger.debug(f"Universal quality score prediction failed: {e}")
        return None, [], artifact.get("meta", {}).get("trained_at")


def _default_thresholds() -> Dict[str, Any]:
    return {
        "SPOT_BULL": {"A": 1.0, "B": 0.35, "C": 0.05, "discard": float("-inf")},
        "RANGE_SCALP": {"A": 1.0, "B": 0.35, "C": 0.05, "discard": float("-inf")},
        "PRESERVATION": {"A": 1.0, "B": 0.35, "C": 0.05, "discard": float("-inf")},
        "FUTURES_SHORT": {"A": 1.0, "B": 0.35, "C": 0.05, "discard": float("-inf")},
    }


def _expected_return_pct(signal_meta: Dict[str, Any]) -> Optional[float]:
    """Return expected return in percentage points, normalizing USDT if needed.

    Priority:
      1. Explicit percentage fields (expected_pnl_percentage / expected_pnl_pct).
      2. expected_pnl / predictor_ev divided by position_size_usdt.
      3. expected_pnl / predictor_ev / expected_gain assumed already as percent.
    """
    for key in ("expected_pnl_percentage", "expected_pnl_pct", "expected_return_pct"):
        if key in signal_meta:
            return _as_float(signal_meta.get(key), default=float("nan"))

    expected_pnl = _as_float(signal_meta.get("expected_pnl"), default=float("nan"))
    predictor_ev = _as_float(signal_meta.get("predictor_ev"), default=float("nan"))
    position_size = _as_float(signal_meta.get("position_size_usdt"), default=float("nan"))

    if not math.isnan(expected_pnl) and not math.isnan(position_size) and position_size > 0:
        return expected_pnl / position_size * 100.0
    if not math.isnan(predictor_ev) and not math.isnan(position_size) and position_size > 0:
        return predictor_ev / position_size * 100.0
    if not math.isnan(expected_pnl):
        return expected_pnl
    if not math.isnan(predictor_ev):
        return predictor_ev
    expected_gain = _as_float(signal_meta.get("expected_gain"), default=float("nan"))
    if not math.isnan(expected_gain):
        return expected_gain
    return None


def _extract_float(signal_meta: Dict[str, Any], keys: List[str], default: float = 0.0) -> float:
    """Return the first float value found under any of the given keys."""
    for key in keys:
        v = signal_meta.get(key)
        if v is None:
            continue
        if isinstance(v, dict):
            v = v.get("value") if "value" in v else None
        if v is None:
            continue
        try:
            f = float(v)
            if not math.isnan(f):
                return f
        except Exception:
            pass
    return default


def _range_scalp_score(signal_meta: Dict[str, Any], symbol: str) -> float:
    """Heuristic score tailored for RANGE_SCALP long entries (v2).

    Rewards classic mean-reversion setups: price in the lower/middle part of the
    Bollinger bandwidth, a healthy volume spike, RSI in the neutral zone,
    and a non-down 4h trend.  It penalizes entries near the upper band or with
    low volume.  The predictor expected-return is treated as a contrarian
    signal: a negative short-term prediction often flags an oversold dip inside
    the range, while an optimistic prediction suggests limited mean-reversion
    upside.
    """
    score = 0.0

    # Bollinger position in [0, 1] (0 = lower band, 1 = upper band)
    price = _extract_float(signal_meta, ["current_price", "price", "entry_avg_price"], default=0.0)
    bb = signal_meta.get("bollinger_bands") or signal_meta.get("bb") or {}
    if not bb and isinstance(signal_meta.get("indicators"), dict):
        bb = signal_meta["indicators"].get("bollinger_bands") or {}
    bb_lower = _extract_float(bb, ["lower", "bb_lower"], default=0.0)
    bb_upper = _extract_float(bb, ["upper", "bb_upper"], default=0.0)
    bb_position = _extract_float(signal_meta, ["bb_position"], default=-1.0)
    if bb_position < 0.0 and price and bb_upper > bb_lower:
        bb_position = (price - bb_lower) / (bb_upper - bb_lower)
    if bb_position < 0.0:
        bb_position = 0.5

    # Optimal mean-reversion zone: 25-45% of BB width (lower half, not extreme)
    if 0.25 <= bb_position <= 0.45:
        score += 1.0
    elif 0.15 <= bb_position < 0.25 or 0.45 < bb_position <= 0.60:
        score += 0.3
    elif 0.60 < bb_position <= 0.80:
        score += 0.0
    else:
        score -= 1.0

    # Volume confirmation: strongest reward at 1.5-2.0x; avoid very low volume
    vol_spike = _extract_float(signal_meta, ["vol_spike", "vol_spike_at_entry"], default=1.0)
    if vol_spike >= 2.0:
        score += 0.3
    elif vol_spike >= 1.5:
        score += 0.8
    elif vol_spike >= 1.0:
        score += 0.2
    elif vol_spike >= 0.5:
        score += 0.0
    else:
        score -= 0.5

    # RSI: neutral-bullish 45-56 is ideal; avoid overbought > 65
    rsi = _extract_float(signal_meta, ["rsi", "rsi_at_entry", "rsi_4h"], default=50.0)
    if 45 <= rsi <= 56:
        score += 1.0
    elif 35 <= rsi < 45 or 56 < rsi <= 65:
        score += 0.3
    elif rsi > 65:
        score -= 1.0
    elif rsi < 30:
        score -= 0.5

    # 4h trend: up is supportive, down is penalized for longs
    trend = str(signal_meta.get("higher_tf_trend") or signal_meta.get("trend_4h") or "").upper()
    if trend == "UP":
        score += 0.3
    elif trend == "DOWN":
        score -= 0.3

    # Expected PnL as contrarian input: negative predictor = oversold bounce
    expected_pnl_pct = _expected_return_pct(signal_meta) or 0.0
    score -= 0.8 * max(-2.0, min(2.0, expected_pnl_pct))

    # Probability and strength add conviction but never dominate the setup
    prob = _extract_float(signal_meta, ["success_probability", "predictor_prob"], default=0.5)
    if prob >= 0.62:
        score += 0.3
    elif prob >= 0.55:
        score += 0.1
    else:
        score -= 0.2

    strength = _extract_float(signal_meta, ["strength", "signal_strength"], default=0.5)
    if 0.55 <= strength <= 0.75:
        score += 0.3
    elif strength > 0.75:
        score += 0.0
    else:
        score -= 0.2

    # Category bonus/penalty (large caps are stabler in ranges, midcaps more noisy)
    cat = _symbol_category(symbol)
    cat_bonus = _CATEGORIES.get(cat, {}).get("bonus", 0.0)
    score += cat_bonus

    # SR event bonus: confirmed bounce/breakout at support or breakdown for shorts
    sr_zone = _extract_float(signal_meta, ["sr_zone_position"], default=0.5)
    if signal_meta.get("support_bounce_buy") or signal_meta.get("sr_bounce"):
        score += 1.0
    if signal_meta.get("support_breakout_buy") or signal_meta.get("sr_breakout"):
        score += 0.8
    if sr_zone <= 0.25:
        score += 0.5
    elif sr_zone >= 0.75:
        score -= 1.0

    return score


def _fallback_score(
    signal_meta: Dict[str, Any],
    regime: str,
    symbol: str,
) -> float:
    """Heuristic score when no calibrated expected PnL is available."""
    if regime == "RANGE_SCALP":
        return _range_scalp_score(signal_meta, symbol)

    expected_pnl = _expected_return_pct(signal_meta) or 0.0
    prob = _as_float(
        signal_meta.get("success_probability")
        or signal_meta.get("predictor_prob")
    )
    strength = _as_float(signal_meta.get("strength"))
    vol_spike = _as_float(signal_meta.get("vol_spike"), 1.0)
    cat = _symbol_category(symbol)
    cat_bonus = _CATEGORIES.get(cat, {}).get("bonus", 0.0)
    regime_term = 0.0
    if regime == "SPOT_BULL":
        regime_term = -0.03 * _as_float(signal_meta.get("ticker_price_change_pct"))

    sr_zone = _extract_float(signal_meta, ["sr_zone_position"], default=0.5)
    sr_bonus = 0.0
    if regime == "FUTURES_SHORT":
        if signal_meta.get("support_breakdown_sell") or signal_meta.get("sr_breakdown"):
            sr_bonus += 0.8
        if sr_zone >= 0.75:
            sr_bonus += 0.5
        elif sr_zone <= 0.25:
            sr_bonus -= 0.5
    else:
        if signal_meta.get("support_bounce_buy") or signal_meta.get("sr_bounce"):
            sr_bonus += 0.6
        if signal_meta.get("support_breakout_buy") or signal_meta.get("sr_breakout"):
            sr_bonus += 0.4
        if sr_zone <= 0.25:
            sr_bonus += 0.4
        elif sr_zone >= 0.75:
            sr_bonus -= 0.6

    return (
        0.35 * vol_spike
        + 0.30 * expected_pnl
        + 0.25 * prob
        + 0.10 * strength
        + cat_bonus
        + regime_term
        + sr_bonus
    )


def compute_quality_score(
    signal_meta: Dict[str, Any],
    regime: str,
    direction: str = "LONG",
    symbol: str = "",
    model_score: Optional[float] = None,
    model_score_resolved: bool = False,
) -> float:
    """Return a calibrated quality score for a candidate signal.

    Higher is better.  The primary score is the expected return in percentage
    points (`expected_pnl_percentage` or `expected_pnl` normalized by
    `position_size_usdt`).  This makes the gate scale-invariant so Kelly sizing
    does not inflate/deflate the score.  When the predictor data is missing we
    fall back to a small heuristic that still requires a positive expected edge.

    For SPOT_BULL, a learned quality-score model is used when present and all
    required features are available, because the raw `expected_pnl_pct` is
    non-monotonically related to realized returns in the current windows.
    """
    regime = str(regime or "SPOT_BULL").strip().upper() or "SPOT_BULL"
    if regime == "SPOT_BULL":
        if not model_score_resolved:
            model_score, _, _ = _model_quality_score(signal_meta, regime)
        if model_score is not None:
            return model_score

    # For RANGE_SCALP the predictor's expected return is not reliably correlated with
    # realized PnL in the current windows, so we always use the feature-based fallback
    # score that blends vol_spike, success_probability, strength and price change.
    if regime == "RANGE_SCALP":
        return _fallback_score(signal_meta, regime, symbol)

    expected_pnl_pct = _expected_return_pct(signal_meta)
    if expected_pnl_pct is not None and not math.isnan(expected_pnl_pct):
        return expected_pnl_pct
    return _fallback_score(signal_meta, regime, symbol)


def classify(
    signal_meta: Dict[str, Any],
    regime: str,
    direction: str = "LONG",
    symbol: str = "",
    thresholds_path: Optional[str] = None,
    fear_greed_value: Optional[float] = None,
    range_scalp_model_mode: str = "shadow",
    range_scalp_model_path: Optional[str] = None,
) -> Tuple[Optional[str], Dict[str, Any]]:
    """Classify a candidate into A/B/C or None (discard).

    Returns (profile_label, info_dict).
    If `fear_greed_value` is provided, the C threshold is adjusted:
      - Extreme fear (<=20): slightly more permissive to capture value.
      - Extreme greed (>=75): slightly stricter to avoid buying euphoria.
    """
    # SR-pool signals already carry a calibrated profile and score.  Use it
    # directly so the legacy gate does not overwrite the pool decision.
    sr_pool_profile = str(signal_meta.get("sr_pool_profile") or "").strip().upper()
    if sr_pool_profile in ("A", "B", "C"):
        info = {
            "score": signal_meta.get("sr_pool_score"),
            "score_source": "sr_pool",
            "thresholds": {},
            "regime": str(regime or "SR_POOL").upper(),
            "category": _symbol_category(symbol),
            "direction": str(direction or "LONG").upper(),
            "fear_greed_value": fear_greed_value,
            "model_score": None,
            "baseline_score": None,
            "shadow_profile": None,
            "model_mode": None,
            "model_version": None,
            "missing_features": [],
        }
        return sr_pool_profile, info

    regime = str(regime or "SPOT_BULL").strip().upper() or "SPOT_BULL"
    category = _symbol_category(symbol)
    _thresholds_data = load_thresholds(thresholds_path)
    _use_universal = _is_universal_thresholds(_thresholds_data)

    # Select the most specific thresholds: regime+category, then regime,
    # then legacy nested layout (thresholds->regime), then default.
    specific_key = f"{regime}__{category}"
    legacy_thresholds = (_thresholds_data.get("thresholds") or {}) if isinstance(_thresholds_data, dict) else {}
    thresholds = dict(
        _thresholds_data.get(specific_key)
        or _thresholds_data.get(regime)
        or legacy_thresholds.get(regime)
        or _default_thresholds().get(regime, _default_thresholds()["SPOT_BULL"])
    )

    # Dynamic C-threshold adjustment by Fear & Greed.
    if fear_greed_value is not None and "C" in thresholds and "B" in thresholds:
        fng = float(fear_greed_value)
        adjustment = 0.0
        if fng <= 20:
            adjustment = -0.05
        elif fng >= 75:
            adjustment = +0.05
        if adjustment != 0.0:
            thresholds["C"] = min(thresholds["B"] - 1e-6, thresholds["C"] + adjustment)

    model_score: Optional[float] = None
    missing_features: List[str] = []
    model_version: Optional[str] = None
    baseline_score: Optional[float] = None
    score_source = "fallback"

    if _use_universal:
        model_score, missing_features, model_version = _universal_quality_score(
            signal_meta, regime, symbol
        )
        if model_score is not None:
            score = model_score
            score_source = "universal_model"
        else:
            score = compute_quality_score(signal_meta, regime, direction, symbol)
    else:
        model_score, missing_features, model_version = _model_quality_score(
            signal_meta, regime, range_scalp_model_path if regime == "RANGE_SCALP" else None,
        )
        baseline_score = _fallback_score(signal_meta, regime, symbol) if regime == "RANGE_SCALP" else None
        model_mode = str(range_scalp_model_mode or "shadow").lower()
        use_range_model = regime == "RANGE_SCALP" and model_mode == "enforced" and model_score is not None
        score = model_score if (regime == "SPOT_BULL" or use_range_model) and model_score is not None else compute_quality_score(
            signal_meta, regime, direction, symbol, model_score=model_score, model_score_resolved=True,
        )
        score_source = "model" if model_score is not None and (regime == "SPOT_BULL" or use_range_model) else "fallback"

    def profile_for(candidate_score: float) -> Optional[str]:
        for candidate_tier in ("A", "B", "C"):
            if candidate_score >= thresholds.get(candidate_tier, float("inf")):
                return candidate_tier
        return None

    profile = profile_for(score)
    shadow_profile = profile_for(model_score) if not _use_universal and regime == "RANGE_SCALP" and model_score is not None else None
    return profile, {
        "score": score,
        "score_source": score_source,
        "thresholds": {k: v for k, v in thresholds.items() if k in ("A", "B", "C")},
        "regime": regime,
        "category": category,
        "direction": direction,
        "fear_greed_value": fear_greed_value,
        "model_score": model_score,
        "baseline_score": baseline_score,
        "shadow_profile": shadow_profile,
        "model_mode": None if _use_universal else (str(range_scalp_model_mode or "shadow").lower() if regime == "RANGE_SCALP" else None),
        "model_version": model_version,
        "missing_features": missing_features,
    }


def _net_outcome(record: Dict[str, Any], horizon: str = "peak_4h", cost: float = 0.0025) -> float:
    """Return a cost-adjusted realized return proxy for a record."""
    raw = _as_float(record.get(horizon, 0.0))
    return raw - cost


def _eval_thresholds(
    records: List[Dict[str, Any]],
    a_thr: float,
    b_thr: float,
    c_thr: float,
    horizon: str,
    cost: float,
    outcome_col: Optional[str] = None,
    score_col: Optional[str] = None,
) -> Dict[str, Any]:
    tiers: Dict[str, List[Dict[str, Any]]] = {"A": [], "B": [], "C": [], "REJECT": []}
    for r in records:
        s = r.get("_score")
        if s is None:
            if score_col and score_col in r:
                s = float(r[score_col])
            else:
                s = compute_quality_score(r, r.get("regime", "SPOT_BULL"), "LONG", r.get("symbol", ""))
        if s >= a_thr:
            tiers["A"].append(r)
        elif s >= b_thr:
            tiers["B"].append(r)
        elif s >= c_thr:
            tiers["C"].append(r)
        else:
            tiers["REJECT"].append(r)

    def _outcome(r: Dict[str, Any]) -> float:
        if outcome_col and outcome_col in r:
            return float(r[outcome_col])
        return _net_outcome(r, horizon, cost)

    out = {}
    for tier, rows in tiers.items():
        if not rows:
            out[tier] = {"n": 0, "win_rate": 0.0, "mean_net": 0.0}
            continue
        nets = [_outcome(r) for r in rows]
        wins = sum(1 for n in nets if n > 0)
        out[tier] = {
            "n": len(rows),
            "win_rate": wins / len(rows),
            "mean_net": sum(nets) / len(nets),
        }
    return out


def calibrate(
    records: List[Dict[str, Any]],
    horizon: str = "peak_4h",
    cost: float = 0.0025,
    target_coverage_A: float = 0.10,
    target_coverage_B: float = 0.25,
    target_coverage_C: float = 0.45,
    min_win_rate_A: float = 0.85,
    min_win_rate_B: float = 0.75,
    min_win_rate_C: float = 0.65,
    min_mean_net_A: float = 0.010,
    min_mean_net_B: float = 0.005,
    min_mean_net_C: float = 0.002,
    outcome_col: Optional[str] = None,
    score_col: Optional[str] = None,
) -> Dict[str, Any]:
    """Find A/B/C thresholds per regime on a historical outcome dataset.

    The input records must contain the same features used by
    `compute_quality_score` plus a forward-looking outcome column.
    By default the outcome is `peak_4h` minus `cost`; if `outcome_col` is
    provided, that column is used directly (e.g. `realized_net_pnl_pct`).
    The routine tries quantile-based cutoffs first and then tightens them
    until the tier constraints are satisfied.
    """
    by_regime: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        regime = str(r.get("regime") or "SPOT_BULL").strip().upper()
        if regime not in by_regime:
            by_regime[regime] = []
        by_regime[regime].append(r)

    result: Dict[str, Any] = {"meta": {"horizon": horizon, "cost": cost}, "thresholds": {}}
    for regime, rows in by_regime.items():
        direction = "SHORT" if regime == "FUTURES_SHORT" else "LONG"
        for r in rows:
            if score_col and score_col in r:
                # For SHORT direction we often want the negative of a long-oriented
                # score column.  If the dataset already encodes expected net PnL for
                # the short side as positive, this is not needed.
                raw_score = float(r[score_col])
                r["_score"] = -raw_score if direction == "SHORT" and score_col not in ("expected_pnl_pct", "realized_net_pnl_pct") else raw_score
            else:
                r["_score"] = compute_quality_score(r, regime, direction, r.get("symbol", ""))
        rows_sorted = sorted(rows, key=lambda x: x["_score"], reverse=True)
        n = len(rows_sorted)
        if n < 100:
            logger.warning(f"Regime {regime} has only {n} records; using conservative defaults")
            result["thresholds"][regime] = _default_thresholds()["SPOT_BULL"]
            continue

        def _score_at(pct: float) -> float:
            idx = min(n - 1, max(0, int(n * pct)))
            return rows_sorted[idx]["_score"]

        # Start from target coverage quantiles.
        candidates = []
        for a_pct in [0.05, 0.075, 0.10, 0.125, 0.15, 0.20]:
            a_thr = _score_at(a_pct)
            for b_pct in [0.15, 0.20, 0.25, 0.30, 0.35]:
                if b_pct <= a_pct:
                    continue
                b_thr = _score_at(b_pct)
                for c_pct in [0.30, 0.35, 0.40, 0.45, 0.50]:
                    if c_pct <= b_pct:
                        continue
                    c_thr = _score_at(c_pct)
                    candidates.append((a_thr, b_thr, c_thr, a_pct, b_pct, c_pct))

        best = None
        best_score = -1.0
        for a_thr, b_thr, c_thr, a_pct, b_pct, c_pct in candidates:
            ev = _eval_thresholds(rows_sorted, a_thr, b_thr, c_thr, horizon, cost, outcome_col=outcome_col, score_col=score_col)
            if (
                ev["A"]["n"] >= 20
                and ev["A"]["win_rate"] >= min_win_rate_A
                and ev["A"]["mean_net"] >= min_mean_net_A
                and ev["B"]["n"] >= 30
                and ev["B"]["win_rate"] >= min_win_rate_B
                and ev["B"]["mean_net"] >= min_mean_net_B
                and ev["C"]["n"] >= 50
                and ev["C"]["win_rate"] >= min_win_rate_C
                and ev["C"]["mean_net"] >= min_mean_net_C
            ):
                # Score: prefer higher coverage while keeping A very selective.
                coverage_score = (
                    3.0 * ev["A"]["mean_net"]
                    + 2.0 * ev["B"]["mean_net"]
                    + 1.0 * ev["C"]["mean_net"]
                    + 0.1 * (ev["A"]["n"] + ev["B"]["n"] + ev["C"]["n"]) / n
                )
                if coverage_score > best_score:
                    best_score = coverage_score
                    best = (a_thr, b_thr, c_thr, ev, a_pct, b_pct, c_pct)

        if best is None:
            # Tighten to the top quantiles that still satisfy a relaxed C tier.
            for a_pct, b_pct, c_pct in [(0.10, 0.25, 0.45), (0.10, 0.30, 0.50), (0.15, 0.30, 0.50)]:
                a_thr, b_thr, c_thr = _score_at(a_pct), _score_at(b_pct), _score_at(c_pct)
                ev = _eval_thresholds(rows_sorted, a_thr, b_thr, c_thr, horizon, cost, outcome_col=outcome_col, score_col=score_col)
                if (
                    ev["A"]["n"] >= 20
                    and ev["C"]["win_rate"] >= 0.60
                    and ev["C"]["mean_net"] >= 0.001
                ):
                    best = (a_thr, b_thr, c_thr, ev, a_pct, b_pct, c_pct)
                    break

        if best is None:
            # A-only fallback: choose the largest top coverage that still yields
            # a positive mean net with a minimum number of observations.
            for a_pct in [0.10, 0.07, 0.05, 0.03, 0.02]:
                a_thr = _score_at(a_pct)
                b_thr = c_thr = a_thr
                ev = _eval_thresholds(rows_sorted, a_thr, b_thr, c_thr, horizon, cost, outcome_col=outcome_col, score_col=score_col)
                if ev["A"]["n"] >= 10 and ev["A"]["mean_net"] > 0.0:
                    best = (a_thr, b_thr, c_thr, ev, a_pct, a_pct, a_pct)
                    break

        if best is None:
            logger.warning(f"Could not calibrate {regime} to constraints; using defaults")
            result["thresholds"][regime] = _default_thresholds()["SPOT_BULL"]
            result.setdefault("validation", {})[regime] = {"note": "fallback to defaults"}
            continue

        a_thr, b_thr, c_thr, ev, a_pct, b_pct, c_pct = best
        result["thresholds"][regime] = {
            "A": float(a_thr),
            "B": float(b_thr),
            "C": float(c_thr),
            "discard": float("-inf"),
        }
        result.setdefault("validation", {})[regime] = {
            "coverage": {"A": a_pct, "B": b_pct, "C": c_pct},
            "metrics": ev,
            "n_total": n,
        }

    return result


def save_calibration(
    result: Dict[str, Any],
    path: Optional[str] = None,
) -> Path:
    p = Path(path) if path else _DEFAULT_THRESHOLDS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    logger.info(f"Saved entry profile thresholds to {p}")
    return p
