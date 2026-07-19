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

_CATEGORIES = {
    "LARGE_CAP": {"bonus": -0.02},
    "NARRATIVE": {"bonus": 0.02},
    "MIDCAP_ALT": {"bonus": 0.0},
}

_stats_cache: Optional[Dict[str, Any]] = None
_thresholds_cache: Optional[Dict[str, Any]] = None
_model_cache: Optional[Any] = None


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


def _load_quality_score_model() -> Optional[Any]:
    """Lazy-load the SPOT_BULL quality score model artifact."""
    global _model_cache
    if _model_cache is not None:
        return _model_cache
    if not _QUALITY_MODEL_PATH.exists():
        return None
    try:
        import joblib
        artifact = joblib.load(_QUALITY_MODEL_PATH)
        _model_cache = artifact["model"] if isinstance(artifact, dict) else artifact
        return _model_cache
    except Exception as e:
        logger.warning(f"Could not load quality score model: {e}")
        return None


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


def _model_quality_score(signal_meta: Dict[str, Any]) -> Optional[float]:
    """Return a model-predicted realized return for SPOT_BULL if the model exists and features are present."""
    model = _load_quality_score_model()
    if model is None:
        return None
    if not _has_quality_signal_features(signal_meta):
        return None
    try:
        import numpy as np
    except Exception:
        return None
    values = []
    for k in _QUALITY_SCORE_FEATURES:
        if k == "expected_pnl_pct":
            v = _expected_return_pct(signal_meta)
            if v is None or math.isnan(v):
                v = 0.0
        else:
            v = signal_meta.get(k, 0.0)
        try:
            values.append(float(v) if v is not None else 0.0)
        except Exception:
            values.append(0.0)
    try:
        score = float(model.predict(np.array([values]))[0])
    except Exception as e:
        logger.debug(f"Model prediction failed: {e}")
        return None
    return score


def _as_float(v: Any, default: float = 0.0) -> float:
    try:
        if isinstance(v, dict):
            v = v.get("value", v.get("v"))
        return float(v)
    except Exception:
        return default


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
            _thresholds_cache = json.load(f)
    except Exception as e:
        logger.error(f"Failed to load entry_profile_thresholds: {e}")
        _thresholds_cache = _default_thresholds()
    return _thresholds_cache or _default_thresholds()


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


def _fallback_score(
    signal_meta: Dict[str, Any],
    regime: str,
    symbol: str,
) -> float:
    """Heuristic score when no calibrated expected PnL is available."""
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
    elif regime == "RANGE_SCALP":
        regime_term = 0.03 * _as_float(signal_meta.get("ticker_price_change_pct"))
    return (
        0.35 * vol_spike
        + 0.30 * expected_pnl
        + 0.25 * prob
        + 0.10 * strength
        + cat_bonus
        + regime_term
    )


def compute_quality_score(
    signal_meta: Dict[str, Any],
    regime: str,
    direction: str = "LONG",
    symbol: str = "",
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
        model_score = _model_quality_score(signal_meta)
        if model_score is not None:
            return model_score

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
) -> Tuple[Optional[str], Dict[str, Any]]:
    """Classify a candidate into A/B/C or None (discard).

    Returns (profile_label, info_dict).
    If `fear_greed_value` is provided, the C threshold is adjusted:
      - Extreme fear (<=20): slightly more permissive to capture value.
      - Extreme greed (>=75): slightly stricter to avoid buying euphoria.
    """
    regime = str(regime or "SPOT_BULL").strip().upper() or "SPOT_BULL"
    _thresholds_data = load_thresholds(thresholds_path)
    thresholds = dict(
        _thresholds_data.get("thresholds", {}).get(regime)
        or _thresholds_data.get(regime)
        or _default_thresholds()["SPOT_BULL"]
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

    score = compute_quality_score(signal_meta, regime, direction, symbol)

    profile = None
    for tier in ("A", "B", "C"):
        if score >= thresholds.get(tier, float("inf")):
            profile = tier
            break

    return profile, {
        "score": score,
        "thresholds": {k: v for k, v in thresholds.items() if k in ("A", "B", "C")},
        "regime": regime,
        "direction": direction,
        "fear_greed_value": fear_greed_value,
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
