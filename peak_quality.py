"""peak_quality.py

Predict forward peak return for a market-entry signal and assign a quality
profile A/B/C (or discard).

Models, thresholds and feature medians are loaded from:
    Dev/benchmarks/peaks_dataset/peak_quality_model/

Usage:
    import peak_quality as pq
    result = pq.predict_peak_quality(
        klines=klines_1h,
        signal_meta=signal_dict,
        btc_klines=btc_1h,
        market_context={'fear_greed_value': 55, 'market_breadth': 0.0},
        regime_mode='SPOT_BULL',
        direction='LONG',
    )
    # result keys: predicted_peak_return, expected_gain, peak_quality_profile,
    #              peak_quality_score, thresholds
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_DEFAULT_MODEL_DIR = Path(__file__).parent / "Dev" / "benchmarks" / "peaks_dataset" / "peak_quality_model"

# Each regime/direction maps to one trained model + thresholds
_REGIME_MODELS = {
    "SPOT_BULL": {"model": "SPOT_BULL_LONG", "direction": "LONG"},
    "RANGE_SCALP": {"model": "RANGE_SCALP_LONG", "direction": "LONG"},
    "PRESERVATION": {"model": "PRESERVATION_LONG", "direction": "LONG"},
    "FUTURES_SHORT": {"model": "FUTURES_SHORT_SHORT", "direction": "SHORT"},
}

# Sizing profile overrides used downstream (kept here as source of truth)
PROFILE_OVERRIDES = {
    "A": {"base_usdt": 500.0, "max_usdt": 2500.0, "cap_pct": 0.50},
    "B": {"base_usdt": 250.0, "max_usdt": 1200.0, "cap_pct": 0.35},
    "C": {"base_usdt": 100.0, "max_usdt": 600.0, "cap_pct": 0.25},
}


# ---------------------------------------------------------------------------
# Helper math
# ---------------------------------------------------------------------------
def _to_float(v, default: float = 0.0) -> float:
    try:
        if isinstance(v, dict):
            v = v.get("value", v.get("v"))
        return float(v)
    except Exception:
        return default


def _pct_return(series: np.ndarray, periods: int) -> Optional[float]:
    if len(series) < periods + 1:
        return None
    return float(series[-1] / series[-(periods + 1)] - 1.0)


def _std(arr: np.ndarray) -> float:
    if len(arr) < 2:
        return 0.0
    return float(np.std(arr, ddof=0))


def _skew(arr: np.ndarray) -> float:
    if len(arr) < 3:
        return 0.0
    arr = np.asarray(arr, dtype=float)
    m = arr.mean()
    s = arr.std(ddof=0)
    if s == 0:
        return 0.0
    return float(((arr - m) ** 3).mean() / (s ** 3))


def _rsi(values: np.ndarray, period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None
    deltas = np.diff(values)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(gains[:period].mean())
    avg_loss = float(losses[:period].mean())
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _bollinger_bands(closes: np.ndarray, period: int = 20, std_dev: float = 2.0):
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    sma = window.mean()
    std = window.std(ddof=0)
    upper = sma + std * std_dev
    lower = sma - std * std_dev
    return float(upper), float(sma), float(lower)


def _atr(closes: np.ndarray, highs: np.ndarray, lows: np.ndarray, period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    tr1 = highs[1:] - lows[1:]
    tr2 = np.abs(highs[1:] - closes[:-1])
    tr3 = np.abs(lows[1:] - closes[:-1])
    tr = np.maximum(np.maximum(tr1, tr2), tr3)
    if len(tr) >= period:
        return float(tr[-period:].mean())
    return float(tr.mean())


def _max_run_up(prices: np.ndarray, window: int) -> float:
    if len(prices) < window:
        return 0.0
    window_prices = prices[-window:]
    start = window_prices[0]
    if start == 0:
        return 0.0
    return float(window_prices.max() / start - 1.0)


def _max_drawdown(prices: np.ndarray, window: int) -> float:
    if len(prices) < window:
        return 0.0
    window_prices = prices[-window:]
    start = window_prices[0]
    if start == 0:
        return 0.0
    return float(abs(start - window_prices.min()) / start)


def _pearson_corr(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 3 or len(a) != len(b):
        return 0.0
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    sa = a.std(ddof=0)
    sb = b.std(ddof=0)
    if sa == 0 or sb == 0:
        return 0.0
    return float(((a - a.mean()) * (b - b.mean())).mean() / (sa * sb))


# ---------------------------------------------------------------------------
# Feature computation from klines / signal meta
# ---------------------------------------------------------------------------
def _array_from_klines(klines: List[Dict[str, Any]]) -> Dict[str, np.ndarray]:
    closes = np.array([k["close"] for k in klines], dtype=float)
    highs = np.array([k["high"] for k in klines], dtype=float)
    lows = np.array([k["low"] for k in klines], dtype=float)
    volumes = np.array([k["volume"] for k in klines], dtype=float)
    return {"close": closes, "high": highs, "low": lows, "volume": volumes, "n": len(closes)}


def _compute_symbol_features(klines: List[Dict[str, Any]]) -> Dict[str, float]:
    """Compute s_* features that are derivable from 1h symbol klines."""
    d = _array_from_klines(klines)
    n = d["n"]
    feats: Dict[str, float] = {}
    if n == 0:
        return feats
    close = d["close"]
    high = d["high"]
    low = d["low"]
    volume = d["volume"]
    last = float(close[-1])

    # returns
    feats["s_return_1h"] = _pct_return(close, 1) or 0.0
    feats["s_return_4h"] = _pct_return(close, 4) if n >= 5 else 0.0
    feats["s_return_24h"] = _pct_return(close, 24) if n >= 25 else 0.0

    # momentum (close-to-close over the window)
    def _momentum(periods: int) -> float:
        return _pct_return(close, periods) or 0.0

    feats["s_momentum_3h"] = _momentum(3)
    feats["s_momentum_6h"] = _momentum(6)
    feats["s_momentum_12h"] = _momentum(12)

    # ATR
    atr = _atr(close, high, low, period=14)
    feats["s_atr"] = atr or 0.0
    feats["s_atr_pct"] = (atr / last) if atr else 0.0

    # RSI
    rsi_val = _rsi(close, period=14)
    feats["s_rsi_1h"] = rsi_val if rsi_val is not None else 50.0

    # Bollinger Bands
    upper, mid, lower = _bollinger_bands(close, period=20, std_dev=2.0)
    if upper is not None and mid is not None and lower is not None:
        bb_range = upper - lower
        feats["s_bb_position"] = (last - lower) / bb_range if bb_range > 0 else 0.5
        feats["s_bb_width"] = (bb_range / mid) if mid > 0 else 0.05
        feats["s_bb_bandwidth"] = feats["s_bb_width"]
    else:
        feats["s_bb_position"] = 0.5
        feats["s_bb_width"] = 0.05
        feats["s_bb_bandwidth"] = 0.05

    # 24h range position
    if n >= 24:
        window_h = high[-24:]
        window_l = low[-24:]
        rr = window_h.max() - window_l.min()
        feats["s_range_position"] = (last - window_l.min()) / rr if rr > 0 else 0.5
    else:
        feats["s_range_position"] = 0.5

    # volume spike vs 20-period mean
    if n >= 21:
        avg20 = volume[-21:-1].mean()
        feats["s_vol_spike"] = float(volume[-1] / avg20) if avg20 > 0 else 1.0
    else:
        feats["s_vol_spike"] = 1.0

    # vol_ratio: current vs 24h mean
    if n >= 25:
        avg24 = volume[-24:-1].mean()
        feats["s_vol_ratio"] = float(volume[-1] / avg24) if avg24 > 0 else 1.0
    else:
        feats["s_vol_ratio"] = 1.0

    # volatility & skew on 1h log returns
    if n >= 25:
        window24 = close[-25:]
        rets = window24[1:] / window24[:-1] - 1.0
        feats["s_volatility_24h"] = _std(rets)
        feats["s_skew_24h"] = _skew(rets)
        feats["s_max_run_up_24h"] = _max_run_up(close, 24)
        feats["s_max_drawdown_24h"] = _max_drawdown(close, 24)
    else:
        feats["s_volatility_24h"] = 0.0
        feats["s_skew_24h"] = 0.0
        feats["s_max_run_up_24h"] = 0.0
        feats["s_max_drawdown_24h"] = 0.0

    # 7d volatility
    if n >= 169:
        window7d = close[-169:]
        rets7d = window7d[1:] / window7d[:-1] - 1.0
        feats["s_volatility_7d"] = _std(rets7d)
    else:
        feats["s_volatility_7d"] = feats["s_volatility_24h"]

    return feats


def _extract_from_signal_meta(signal_meta: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """Map any already-computed signal_meta fields to s_* feature names."""
    if not signal_meta:
        return {}
    sm = {}
    indicators = signal_meta.get("indicators") or {}
    if "rsi" in indicators:
        sm["s_rsi_1h"] = _to_float(indicators["rsi"], 50.0)
        # If no explicit 4h RSI, reuse the 1h value as a proxy
        sm["s_rsi_4h"] = _to_float(indicators.get("rsi_4h", indicators["rsi"]), 50.0)
    if "vol_spike" in signal_meta:
        sm["s_vol_spike"] = _to_float(signal_meta["vol_spike"], 1.0)
    if "bb_position" in signal_meta:
        sm["s_bb_position"] = _to_float(signal_meta["bb_position"], 0.5)
    if "bb_bandwidth" in signal_meta:
        sm["s_bb_width"] = _to_float(signal_meta["bb_bandwidth"], 0.05)
        sm["s_bb_bandwidth"] = sm["s_bb_width"]
    # confidence: prefer explicit confidence, then strength
    conf = signal_meta.get("confidence", signal_meta.get("strength", 0.5))
    sm["s_confidence"] = _to_float(conf, 0.5)
    if "price_change_24h" in signal_meta:
        sm["s_ticker_price_change_percent"] = _to_float(signal_meta["price_change_24h"], 0.0)
    elif "ticker_price_change_percent" in signal_meta:
        sm["s_ticker_price_change_percent"] = _to_float(signal_meta["ticker_price_change_percent"], 0.0)
    return sm


def _compute_btc_features(btc_klines: List[Dict[str, Any]]) -> Dict[str, float]:
    """Compute BTC market features from 1h BTC klines."""
    if not btc_klines:
        return {}
    d = _array_from_klines(btc_klines)
    n = d["n"]
    if n == 0:
        return {}
    close = d["close"]
    high = d["high"]
    low = d["low"]
    last = float(close[-1])
    bf = {
        "s_btc_return_1h": _pct_return(close, 1) or 0.0,
        "s_btc_return_4h": _pct_return(close, 4) if n >= 5 else 0.0,
        "s_btc_return_24h": _pct_return(close, 24) if n >= 25 else 0.0,
        "s_btc_day_pct": ((_pct_return(close, 24) or 0.0) * 100.0),
        "s_btc_7d_pct": ((_pct_return(close, 7 * 24) or 0.0) * 100.0),
        "s_btc_30d_pct": ((_pct_return(close, 30 * 24) or 0.0) * 100.0),
    }
    atr = _atr(close, high, low, period=14)
    bf["s_btc_atr_pct"] = (atr / last) if atr else 0.0
    return bf


def _btc_correlation(klines: List[Dict[str, Any]], btc_klines: List[Dict[str, Any]]) -> float:
    """Compute 24h 1h-return correlation between symbol and BTC (requires aligned klines)."""
    if not klines or not btc_klines:
        return 0.0
    # Use the most recent common length
    n = min(len(klines), len(btc_klines))
    if n < 25:
        return 0.0
    sym = np.array([k["close"] for k in klines[-n:]], dtype=float)
    btc = np.array([k["close"] for k in btc_klines[-n:]], dtype=float)
    sym_r = np.diff(sym) / sym[:-1]
    btc_r = np.diff(btc) / btc[:-1]
    if len(sym_r) >= 24:
        return _pearson_corr(sym_r[-24:], btc_r[-24:])
    return _pearson_corr(sym_r, btc_r)


# ---------------------------------------------------------------------------
# PeakQualityModel
# ---------------------------------------------------------------------------
class PeakQualityModel:
    _instance: Optional["PeakQualityModel"] = None

    def __init__(self, model_dir: Optional[str] = None):
        self.model_dir = Path(model_dir) if model_dir else _DEFAULT_MODEL_DIR
        self._models: Dict[str, Any] = {}
        self._thresholds: Dict[str, Dict[str, float]] = {}
        self._medians: Dict[str, Dict[str, float]] = {}
        self._metrics: Dict[str, Any] = {}
        self._feature_cols: Dict[str, List[str]] = {}
        self._load_metadata()

    def _load_metadata(self) -> None:
        json_path = self.model_dir / "metrics_and_thresholds.json"
        if not json_path.exists():
            logger.warning(f"peak_quality metadata not found at {json_path}")
            return
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                self._metrics = json.load(f)
            for key, data in self._metrics.items():
                self._thresholds[key] = {
                    "A": float(data["best_thresholds"]["A"]),
                    "B": float(data["best_thresholds"]["B"]),
                    "C": float(data["best_thresholds"]["C"]),
                }
                self._medians[key] = {k: float(v) for k, v in data["medians"].items()}
                self._feature_cols[key] = list(data["features"])
        except Exception as e:
            logger.error(f"Error loading peak_quality metadata: {e}")

    def _load_model(self, key: str) -> Any:
        if key in self._models:
            return self._models[key]
        path = self.model_dir / f"{key}.pkl"
        if not path.exists():
            raise FileNotFoundError(f"peak_quality model not found: {path}")
        try:
            import joblib

            payload = joblib.load(path)
        except Exception as e:
            raise RuntimeError(f"Could not load peak_quality model {key}: {e}")
        model = payload.get("model") or payload
        self._models[key] = model
        # Prefer per-pickle feature list/medians if available
        if isinstance(payload, dict):
            if "feature_cols" in payload and key not in self._feature_cols:
                self._feature_cols[key] = list(payload["feature_cols"])
            if "medians" in payload and key not in self._medians:
                self._medians[key] = {k: float(v) for k, v in payload["medians"].items()}
        return model

    def available(self) -> bool:
        return bool(self._metrics)

    def keys(self) -> List[str]:
        return list(self._metrics.keys())

    def resolve_model_key(self, regime_mode: str, direction: Optional[str] = None) -> str:
        regime = str(regime_mode or "SPOT_BULL").strip().upper()
        mapping = _REGIME_MODELS.get(regime)
        if mapping is None:
            # Fallback: if direction provided, try to build a key
            direction = str(direction or "LONG").strip().upper()
            return f"{regime}_{direction}"
        direction = str(direction or mapping["direction"]).strip().upper()
        return mapping["model"]

    def model_for(self, regime_mode: str, direction: Optional[str] = None) -> Any:
        return self._load_model(self.resolve_model_key(regime_mode, direction))

    def thresholds_for(self, key: str) -> Dict[str, float]:
        return self._thresholds.get(key, {"A": 0.0, "B": 0.0, "C": 0.0})

    def medians_for(self, key: str) -> Dict[str, float]:
        return self._medians.get(key, {})

    def features_for(self, key: str) -> List[str]:
        return self._feature_cols.get(key, [])

    @classmethod
    def get(cls, model_dir: Optional[str] = None) -> "PeakQualityModel":
        if cls._instance is None:
            cls._instance = cls(model_dir)
        return cls._instance


def get_peak_quality_model(model_dir: Optional[str] = None) -> PeakQualityModel:
    return PeakQualityModel.get(model_dir)


# ---------------------------------------------------------------------------
# Public feature / prediction API
# ---------------------------------------------------------------------------
def compute_peak_features(
    klines: Optional[List[Dict[str, Any]]] = None,
    signal_meta: Optional[Dict[str, Any]] = None,
    btc_klines: Optional[List[Dict[str, Any]]] = None,
    market_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, float]:
    """Build the s_* feature vector used by the peak_quality models.

    Order of precedence:
        1. Explicitly provided klines (compute from scratch)
        2. signal_meta / indicators (already computed values)
        3. market_context (BTC, fear/greed, market_breadth, ticker)
    Missing values are left as NaN here and imputed at prediction time.
    """
    features: Dict[str, float] = {}
    if klines:
        features.update(_compute_symbol_features(klines))
    features.update(_extract_from_signal_meta(signal_meta))
    if btc_klines:
        features.update(_compute_btc_features(btc_klines))
        if klines and btc_klines:
            features["s_btc_correlation_24h"] = _btc_correlation(klines, btc_klines)
    if market_context:
        if "fear_greed_value" in market_context:
            features["s_fear_greed_value"] = _to_float(market_context["fear_greed_value"], 50.0)
        if "market_breadth" in market_context:
            features["s_market_breadth"] = _to_float(market_context["market_breadth"], 0.0)
        if "btc_return_24h" in market_context:
            features["s_btc_return_24h"] = _to_float(market_context["btc_return_24h"], 0.0)
        if "ticker_price_change_percent" in market_context:
            features["s_ticker_price_change_percent"] = _to_float(market_context["ticker_price_change_percent"], 0.0)
    return features


def _align_features(features: Dict[str, float], model_key: str, model: PeakQualityModel) -> np.ndarray:
    """Align a feature dict to the model's expected columns and impute medians."""
    feature_cols = model.features_for(model_key)
    if not feature_cols:
        feature_cols = list(features.keys())
    medians = model.medians_for(model_key)
    row = []
    for col in feature_cols:
        v = features.get(col)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            v = medians.get(col, 0.0)
        row.append(float(v))
    return np.array(row, dtype=float).reshape(1, -1)


def _assign_profile(expected_gain: float, thresholds: Dict[str, float]) -> str:
    if expected_gain >= thresholds.get("A", 0.0):
        return "A"
    if expected_gain >= thresholds.get("B", 0.0):
        return "B"
    if expected_gain >= thresholds.get("C", 0.0):
        return "C"
    return "discarded"


def predict_peak_quality(
    klines: Optional[List[Dict[str, Any]]] = None,
    signal_meta: Optional[Dict[str, Any]] = None,
    btc_klines: Optional[List[Dict[str, Any]]] = None,
    market_context: Optional[Dict[str, Any]] = None,
    regime_mode: str = "SPOT_BULL",
    direction: Optional[str] = None,
    features: Optional[Dict[str, float]] = None,
    model_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Predict the forward peak return and assign an A/B/C profile.

    If ``features`` is provided it is used directly (avoids recomputing).
    Otherwise klines/signal_meta/btc_klines/market_context are used.
    """
    model = get_peak_quality_model(model_dir)
    if not model.available():
        return {
            "predicted_peak_return": 0.0,
            "expected_gain": 0.0,
            "peak_quality_profile": "C",
            "peak_quality_score": 0.0,
            "thresholds": {"A": 0.0, "B": 0.0, "C": 0.0},
            "model_key": None,
            "error": "peak_quality model not available",
        }

    model_key = model.resolve_model_key(regime_mode, direction)
    regressor = model._load_model(model_key)

    if features is None:
        features = compute_peak_features(klines, signal_meta, btc_klines, market_context)

    X = _align_features(features, model_key, model)
    pred = float(regressor.predict(X)[0])

    expected_direction = _REGIME_MODELS.get(str(regime_mode).strip().upper(), {}).get(
        "direction", str(direction or "LONG").strip().upper() or "LONG"
    )
    expected_gain = pred if expected_direction == "LONG" else -pred

    thresholds = model.thresholds_for(model_key)
    profile = _assign_profile(expected_gain, thresholds)

    return {
        "predicted_peak_return": pred,
        "expected_gain": expected_gain,
        "peak_quality_profile": profile,
        "peak_quality_score": expected_gain,
        "thresholds": thresholds,
        "model_key": model_key,
        "features_used": list(model.features_for(model_key)),
        "features": {k: (float(v) if v is not None else math.nan) for k, v in features.items()},
    }


def assign_profile(
    expected_gain: float,
    regime_mode: str = "SPOT_BULL",
    direction: Optional[str] = None,
    model_dir: Optional[str] = None,
) -> str:
    """Standalone threshold-only profile assignment (no prediction)."""
    model = get_peak_quality_model(model_dir)
    model_key = model.resolve_model_key(regime_mode, direction)
    thresholds = model.thresholds_for(model_key)
    return _assign_profile(expected_gain, thresholds)
