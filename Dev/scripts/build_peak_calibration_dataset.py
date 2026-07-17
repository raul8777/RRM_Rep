#!/usr/bin/env python3
"""Build a self-contained historical peak calibration dataset.

For every spot 1h bar, compute 2h/4h forward-looking peaks (up and down) and
local extrema, capture a rich feature set at the start and end of each peak,
and assign the system mode (SPOT_BULL / RANGE_SCALP / PRESERVATION / FUTURES_SHORT)
using the same BTC-based logic as market_regime.py.

Output:
    Dev/benchmarks/peaks_dataset/peaks_all.jsonl
    Dev/benchmarks/peaks_dataset/peaks_summary.json
    Dev/benchmarks/peaks_dataset/peaks_by_regime.json
    Dev/benchmarks/peaks_dataset/README.md
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


def _strip_tz(idx):
    """Remove timezone from a DatetimeIndex, regardless of whether it is tz-aware."""
    if hasattr(idx, 'tz') and idx.tz is not None:
        return idx.tz_convert(None)
    return idx


# ──────────────────────────────────────────────────────────────────────────────
# Constants aligned with market_regime.py
# ──────────────────────────────────────────────────────────────────────────────
_BEAR_7D_THRESHOLD = -7.0
_BEAR_30D_THRESHOLD = -15.0
_SIDEWAYS_DAY_RANGE = 1.2
_SIDEWAYS_VOL_MAX = 0.7
_BULL_DAY_MIN = 0.5
_BB_SIDEWAYS_MAX = 0.06

PEAK_HORIZONS = [2, 4]
LOCAL_EXTREMA_ORDER = 6          # +/- 6h around a candidate extreme
LOCAL_EXTREMA_PROMINENCE = 0.0025  # 0.25% minimum reversal to count

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _clean_value(v: Any) -> Any:
    """Convert non-serializable values for JSON output."""
    if v is None:
        return None
    if isinstance(v, (dict,)):
        return _clean_dict(v)
    if isinstance(v, (list, tuple)):
        return [_clean_value(x) for x in v]
    if isinstance(v, float):
        if pd.isna(v) or np.isnan(v) or np.isinf(v):
            return None
        return v
    if isinstance(v, np.floating):
        return _clean_value(float(v))
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.isoformat()
    return v


def _clean_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    return {str(k): _clean_value(v) for k, v in d.items()}


def _safe_loc_to_dict(df: pd.DataFrame, ts: pd.Timestamp) -> Dict[str, Any]:
    """Return a dict for a single timestamp, collapsing duplicate index rows."""
    val = df.loc[ts]
    if isinstance(val, pd.DataFrame):
        val = val.iloc[-1]
    return val.to_dict()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def _bollinger(close: pd.Series, period: int = 20, mult: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series]:
    mid = close.rolling(window=period, min_periods=period).mean()
    std = close.rolling(window=period, min_periods=period).std()
    upper = mid + mult * std
    lower = mid - mult * std
    return upper, mid, lower


def _classified_mode(row: pd.Series) -> Tuple[str, str, str]:
    """Return (mode, regime_label, reason) for a row of BTC regime features."""
    btc_30d = row["btc_30d_pct"]
    btc_7d = row["btc_7d_pct"]
    btc_day = row["btc_day_pct"]
    vol_ratio = row["vol_ratio"]
    bb_bw = row["bb_bandwidth"]
    ema9_vs = row["ema9_vs_ema21"]

    is_structural_bear = btc_30d <= _BEAR_30D_THRESHOLD
    is_7d_bear = btc_7d <= _BEAR_7D_THRESHOLD
    is_sideways_vol = abs(btc_day) < _SIDEWAYS_DAY_RANGE and vol_ratio < _SIDEWAYS_VOL_MAX
    is_sideways_bb = bb_bw < _BB_SIDEWAYS_MAX
    is_ema_down = ema9_vs == "DOWN"

    if is_structural_bear or (is_7d_bear and is_ema_down):
        return "PRESERVATION", "BAJISTA", f"Bear estructural 30d={btc_30d:.1f}% 7d={btc_7d:.1f}%"
    if is_sideways_vol and is_sideways_bb:
        return "RANGE_SCALP", "LATERAL", f"Lateral dia={btc_day:.1f}% vol={vol_ratio:.1f}x bb_bw={bb_bw:.3f}"
    if is_7d_bear and not is_ema_down:
        return "PRESERVATION", "DEBIL BAJA", f"Tendencia bajista 7d={btc_7d:.1f}% EMA UP"
    if ema9_vs == "UP" and (btc_day > _BULL_DAY_MIN or bb_bw > _BB_SIDEWAYS_MAX):
        return "SPOT_BULL", "ALCISTA", f"Alcista EMA9>EMA21 dia={btc_day:.1f}% 7d={btc_7d:.1f}%"
    if ema9_vs == "UP":
        return "SPOT_BULL", "DEBIL ALZA", f"Alza debil EMA9>EMA21 dia={btc_day:.1f}%"
    return "RANGE_SCALP", "LATERAL", f"Sin tendencia clara EMA={ema9_vs} dia={btc_day:.1f}%"


def _confidence(raw: pd.Series) -> float:
    mode = raw["mode_raw"]
    score = 0
    if mode in ("PRESERVATION", "FUTURES_SHORT"):
        if raw["btc_30d_pct"] <= _BEAR_30D_THRESHOLD:
            score += 1
        if raw["btc_7d_pct"] <= _BEAR_7D_THRESHOLD:
            score += 1
        if raw["ema9_vs_ema21"] == "DOWN":
            score += 1
        if raw["vol_ratio"] < 0.8:
            score += 1
        if raw["btc_day_pct"] < -0.5:
            score += 1
    elif mode == "SPOT_BULL":
        if raw["btc_7d_pct"] > 0:
            score += 1
        if raw["btc_30d_pct"] > -5:
            score += 1
        if raw["ema9_vs_ema21"] == "UP":
            score += 1
        if raw["vol_ratio"] >= 1.0:
            score += 1
        if raw["btc_day_pct"] > _BULL_DAY_MIN:
            score += 1
    elif mode == "RANGE_SCALP":
        if raw["bb_bandwidth"] < _BB_SIDEWAYS_MAX:
            score += 1
        if abs(raw["btc_day_pct"]) < _SIDEWAYS_DAY_RANGE:
            score += 1
        if raw["vol_ratio"] < _SIDEWAYS_VOL_MAX:
            score += 1
        if raw["ema9_vs_ema21"] != "UP":
            score += 1
        if raw["btc_7d_pct"] > -5:
            score += 1
    return round(max(0.1, score / 5.0), 2)


def build_regime_df(btc_1h: pd.DataFrame, btc_1d: pd.DataFrame) -> pd.DataFrame:
    """Return hourly DataFrame with raw/confirmed/FUTURES_SHORT regime columns."""
    # Normalise indexes to timezone-naive DatetimeIndexes once.
    btc_1h = btc_1h.copy()
    btc_1h.index = _strip_tz(pd.to_datetime(btc_1h.index))
    btc_1d = btc_1d.copy()
    btc_1d.index = _strip_tz(pd.to_datetime(btc_1d.index, unit="ms", utc=True))

    # Deduplicate before any index-dependent operation.
    btc_1h = btc_1h[~btc_1h.index.duplicated(keep="last")]
    btc_1d = btc_1d[~btc_1d.index.duplicated(keep="last")]

    df = btc_1h[["close", "volume"]].copy()
    df["date"] = df.index.normalize()

    # Daily open/close mapped by date.
    btc_1d["date"] = btc_1d.index.normalize()
    daily = btc_1d[["date", "open", "close"]].rename(
        columns={"open": "day_open", "close": "day_close"}
    ).set_index("date")
    df["day_open"] = df["date"].map(daily["day_open"])
    df["day_close"] = df["date"].map(daily["day_close"])

    # 7d / 30d returns on daily data.
    btc_1d_sorted = btc_1d.sort_values("date")
    btc_1d_sorted["close_7d"] = btc_1d_sorted["close"].shift(7)
    btc_1d_sorted["close_30d"] = btc_1d_sorted["close"].shift(30)
    btc_1d_sorted["btc_7d_pct"] = (
        (btc_1d_sorted["close"] - btc_1d_sorted["close_7d"])
        / btc_1d_sorted["close_7d"]
        * 100
    )
    btc_1d_sorted["btc_30d_pct"] = (
        (btc_1d_sorted["close"] - btc_1d_sorted["close_30d"])
        / btc_1d_sorted["close_30d"]
        * 100
    )
    pct_map = btc_1d_sorted.set_index("date")[["btc_7d_pct", "btc_30d_pct"]]
    df["btc_7d_pct"] = df["date"].map(pct_map["btc_7d_pct"])
    df["btc_30d_pct"] = df["date"].map(pct_map["btc_30d_pct"])

    df["btc_day_pct"] = (df["close"] - df["day_open"]) / df["day_open"] * 100

    df["vol_ratio"] = df["volume"] / df["volume"].shift(1).rolling(24, min_periods=1).mean()

    # BTC 4h indicators, forward-filled to every hour.
    btc_4h = btc_1h.resample("4h", label="right", closed="right").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    btc_4h["ema9"] = _ema(btc_4h["close"], 9)
    btc_4h["ema21"] = _ema(btc_4h["close"], 21)
    btc_4h["ema9_vs_ema21"] = np.where(
        btc_4h["ema9"] > btc_4h["ema21"] * 1.001, "UP",
        np.where(btc_4h["ema9"] < btc_4h["ema21"] * 0.999, "DOWN", "FLAT")
    )
    upper, mid, lower = _bollinger(btc_4h["close"], 20, 2.0)
    btc_4h["bb_bandwidth"] = ((upper - lower) / mid).replace([np.inf, -np.inf], 0.05)

    btc_4h_features = btc_4h[["ema9_vs_ema21", "bb_bandwidth"]].reindex(df.index, method="ffill")
    df = df.join(btc_4h_features, how="left")

    classified = df.apply(_classified_mode, axis=1, result_type="expand")
    classified.columns = ["mode_raw", "regime_label", "reason"]
    df = pd.concat([df, classified], axis=1)

    df["confidence"] = df.apply(_confidence, axis=1)

    raw_modes = df["mode_raw"].tolist()
    confirmed = []
    last_confirmed = "SPOT_BULL"
    pending = None
    pending_count = 0
    for m in raw_modes:
        if m == pending:
            pending_count += 1
        else:
            pending = m
            pending_count = 1
        if pending_count >= 2:
            last_confirmed = pending
        confirmed.append(last_confirmed)
    df["mode_confirmed"] = confirmed

    df["mode_futures"] = df["mode_confirmed"].replace({"PRESERVATION": "FUTURES_SHORT"})

    df = df.rename(
        columns={
            "close": "btc_close",
            "volume": "btc_volume",
            "day_open": "btc_day_open",
            "day_close": "btc_day_close",
        }
    )

    keep = [
        "btc_close",
        "btc_volume",
        "btc_7d_pct",
        "btc_30d_pct",
        "btc_day_pct",
        "vol_ratio",
        "bb_bandwidth",
        "ema9_vs_ema21",
        "mode_raw",
        "mode_confirmed",
        "mode_futures",
        "regime_label",
        "reason",
        "confidence",
    ]
    df = df.drop(columns=["date"], errors="ignore")
    return df[keep]


# ──────────────────────────────────────────────────────────────────────────────
# Feature engineering per symbol
# ──────────────────────────────────────────────────────────────────────────────


def compute_asset_features(df: pd.DataFrame, btc_returns: pd.Series) -> pd.DataFrame:
    """Add technical features to a symbol's 1h DataFrame (already indexed by timestamp)."""
    feats = df[["open", "high", "low", "close", "volume"]].copy()

    feats["return_1h"] = feats["close"].pct_change(1)
    feats["return_4h"] = feats["close"].pct_change(4)
    feats["return_24h"] = feats["close"].pct_change(24)

    feats["momentum_3h"] = feats["close"].pct_change(3)
    feats["momentum_6h"] = feats["close"].pct_change(6)
    feats["momentum_12h"] = feats["close"].pct_change(12)

    feats["atr"] = _atr(feats["high"], feats["low"], feats["close"], 14)
    feats["atr_pct"] = feats["atr"] / feats["close"]

    feats["rsi_1h"] = _rsi(feats["close"], 14)

    rsi_4h = _rsi(feats["close"].resample("4h", label="right", closed="right").last(), 14)
    feats["rsi_4h"] = rsi_4h.reindex(feats.index, method="ffill")

    upper, mid, lower = _bollinger(feats["close"], 20, 2.0)
    feats["bb_position"] = (feats["close"] - lower) / (upper - lower)
    feats["bb_width"] = (upper - lower) / mid

    high_7d = feats["high"].rolling(168, min_periods=24).max()
    low_7d = feats["low"].rolling(168, min_periods=24).min()
    feats["range_position"] = (feats["close"] - low_7d) / (high_7d - low_7d)

    feats["vol_spike"] = feats["volume"] / feats["volume"].shift(1).rolling(24, min_periods=1).mean()

    ret = feats["return_1h"]
    feats["volatility_24h"] = ret.rolling(24, min_periods=12).std() * np.sqrt(24)
    feats["volatility_7d"] = ret.rolling(168, min_periods=48).std() * np.sqrt(168)
    feats["skew_24h"] = ret.rolling(24, min_periods=12).skew()

    rolling_max = feats["close"].rolling(24, min_periods=1).max()
    rolling_min = feats["close"].rolling(24, min_periods=1).min()
    feats["max_run_up_24h"] = feats["close"] / rolling_min - 1
    feats["max_drawdown_24h"] = 1 - feats["close"] / rolling_max

    btc_ret_aligned = btc_returns.reindex(feats.index)
    feats["btc_correlation_24h"] = ret.rolling(24, min_periods=12).corr(btc_ret_aligned)

    return feats


def find_local_peaks(
    close: pd.Series,
    order: int = LOCAL_EXTREMA_ORDER,
    prominence: float = LOCAL_EXTREMA_PROMINENCE,
) -> Tuple[List[Tuple[int, int]], List[Tuple[int, int]]]:
    """Return lists of (start_idx, end_idx) for up-peaks and down-peaks."""
    n = len(close)
    if n < 2 * order + 1:
        return [], []

    max_idx = []
    min_idx = []
    vals = close.values

    for i in range(order, n - order):
        val = vals[i]
        left_max = vals[i - order:i].max()
        right_max = vals[i + 1:i + order + 1].max()
        if val > left_max and val >= right_max:
            future_min = vals[i + 1:i + order + 1].min()
            if (val - future_min) / val >= prominence:
                max_idx.append(i)

        left_min = vals[i - order:i].min()
        right_min = vals[i + 1:i + order + 1].min()
        if val < left_min and val <= right_min:
            future_max = vals[i + 1:i + order + 1].max()
            if (future_max - val) / future_max >= prominence:
                min_idx.append(i)

    up_peaks = []
    last_min = None
    j = 0
    for mi in max_idx:
        while j < len(min_idx) and min_idx[j] < mi:
            last_min = min_idx[j]
            j += 1
        if last_min is not None:
            up_peaks.append((last_min, mi))

    down_peaks = []
    last_max = None
    k = 0
    for mi in min_idx:
        while k < len(max_idx) and max_idx[k] < mi:
            last_max = max_idx[k]
            k += 1
        if last_max is not None:
            down_peaks.append((last_max, mi))

    return up_peaks, down_peaks


def make_forward_records(
    features: pd.DataFrame,
    close: pd.Series,
    symbol: str,
    horizons: List[int],
) -> List[Dict[str, Any]]:
    """Create forward-looking peak records for every valid starting bar."""
    records: List[Dict[str, Any]] = []

    for horizon in horizons:
        lag_frames = {k: close.shift(-k) for k in range(1, horizon + 1)}
        lag_df = pd.concat(lag_frames, axis=1)
        max_val = lag_df.max(axis=1)
        min_val = lag_df.min(axis=1)

        valid = max_val.notna() & min_val.notna()
        valid_lag = lag_df.loc[valid]
        argmax_k = valid_lag.idxmax(axis=1)
        argmin_k = valid_lag.idxmin(axis=1)

        up_return = max_val / close - 1
        down_return = min_val / close - 1

        valid_index = features[valid].index

        for direction, ret, lag in [("LONG", up_return, argmax_k), ("SHORT", down_return, argmin_k)]:
            peak_return = ret[valid]
            peak_end_lag = lag.astype(int)
            for ts in valid_index:
                lag_h = int(peak_end_lag.loc[ts])
                end_ts = ts + pd.Timedelta(hours=lag_h)
                if end_ts not in features.index:
                    continue
                records.append({
                    "symbol": symbol,
                    "direction": direction,
                    "peak_type": f"forward_{horizon}h",
                    "peak_start_ts": ts.isoformat(),
                    "peak_end_ts": end_ts.isoformat(),
                    "peak_return": float(peak_return.loc[ts]),
                    "features_start": _clean_dict(_safe_loc_to_dict(features, ts)),
                    "features_end": _clean_dict(_safe_loc_to_dict(features, end_ts)),
                })
    return records


def make_local_records(
    features: pd.DataFrame,
    close: pd.Series,
    symbol: str,
) -> List[Dict[str, Any]]:
    """Create local-extrema based peak records."""
    records: List[Dict[str, Any]] = []
    up_pairs, down_pairs = find_local_peaks(close)

    for start_i, end_i in up_pairs:
        start_ts = close.index[start_i]
        end_ts = close.index[end_i]
        ret = close.iloc[end_i] / close.iloc[start_i] - 1
        records.append({
            "symbol": symbol,
            "direction": "LONG",
            "peak_type": "local_extrema",
            "peak_start_ts": start_ts.isoformat(),
            "peak_end_ts": end_ts.isoformat(),
            "peak_return": float(ret),
            "features_start": _clean_dict(_safe_loc_to_dict(features, start_ts)),
            "features_end": _clean_dict(_safe_loc_to_dict(features, end_ts)),
        })

    for start_i, end_i in down_pairs:
        start_ts = close.index[start_i]
        end_ts = close.index[end_i]
        ret = close.iloc[end_i] / close.iloc[start_i] - 1
        records.append({
            "symbol": symbol,
            "direction": "SHORT",
            "peak_type": "local_extrema",
            "peak_start_ts": start_ts.isoformat(),
            "peak_end_ts": end_ts.isoformat(),
            "peak_return": float(ret),
            "features_start": _clean_dict(_safe_loc_to_dict(features, start_ts)),
            "features_end": _clean_dict(_safe_loc_to_dict(features, end_ts)),
        })

    return records


def process_symbol(
    args: Tuple[str, Path, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.Series]
) -> List[Dict[str, Any]]:
    symbol, path, regime_df, ticker_df, funding_df, market_breadth_series = args
    try:
        df = pd.read_parquet(path)
    except Exception as exc:
        print(f"[SKIP] {symbol}: {exc}")
        return []

    df = df.sort_values("open_time")
    df = df.set_index("open_time")
    df.index = _strip_tz(pd.to_datetime(df.index))
    df = df[~df.index.duplicated(keep="last")]

    df = df[(df["close"] > 0) & df["close"].notna()]
    if len(df) < 50:
        return []

    btc_returns = regime_df["btc_return_24h"] if "btc_return_24h" in regime_df.columns else pd.Series(index=regime_df.index, dtype=float)
    asset_feats = compute_asset_features(df, btc_returns)

    merged = asset_feats.join(regime_df, how="left")
    merged["market_breadth"] = market_breadth_series.reindex(merged.index)

    merged["date"] = pd.DatetimeIndex(merged.index).normalize()

    if not ticker_df.empty:
        sym_ticker = ticker_df[ticker_df["symbol"] == symbol].copy()
        if not sym_ticker.empty:
            sym_ticker = sym_ticker.drop_duplicates("date").set_index("date")
            ticker_cols = {
                "last_price": "ticker_last_price",
                "price_change_percent": "ticker_price_change_percent",
                "volume": "ticker_24h_volume",
                "quote_volume": "ticker_quote_volume",
                "weighted_avg_price": "ticker_weighted_avg_price",
            }
            for src, dst in ticker_cols.items():
                if src in sym_ticker.columns:
                    merged[dst] = merged["date"].map(sym_ticker[src])

    if not funding_df.empty and symbol in funding_df.columns:
        funding_sub = funding_df[[symbol]].rename(columns={symbol: "funding_rate"})
        merged = merged.reset_index().rename(columns={"index": "open_time"})
        merged["open_time"] = pd.to_datetime(merged["open_time"])
        merged = merged.sort_values("open_time")
        merged = pd.merge_asof(
            merged,
            funding_sub,
            left_on="open_time",
            right_index=True,
            direction="backward",
            allow_exact_matches=True,
        ).set_index("open_time")
        merged = merged.sort_index()

    for col in ["funding_rate"]:
        if col not in merged.columns:
            merged[col] = np.nan

    # Build the clean feature matrix used by peak detectors.
    features = merged.drop(columns=["date"], errors="ignore")
    features = features[~features.index.duplicated(keep="last")]

    close = features["close"].copy()
    records = []
    records.extend(make_forward_records(features, close, symbol, PEAK_HORIZONS))
    records.extend(make_local_records(features, close, symbol))

    print(f"[OK] {symbol}: {len(records)} records")
    return records


# ──────────────────────────────────────────────────────────────────────────────
# External data loaders
# ──────────────────────────────────────────────────────────────────────────────


def load_ticker_snapshots(repo: Path) -> pd.DataFrame:
    files = sorted((repo / "historical_data" / "external").glob("ticker_24h_*.parquet"))
    frames = []
    for f in files:
        try:
            df = pd.read_parquet(f)
            df["date"] = pd.to_datetime(df["snapshot_time"]).dt.tz_localize(None).dt.normalize()
            frames.append(df[["symbol", "date", "last_price", "price_change_percent", "volume", "quote_volume", "weighted_avg_price"]])
        except Exception as exc:
            print(f"[WARN] ticker {f}: {exc}")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def load_funding(repo: Path) -> pd.DataFrame:
    files = sorted((repo / "historical_data" / "external").glob("funding_rate_*.parquet"))
    frames = []
    for f in files:
        try:
            df = pd.read_parquet(f)
            frames.append(df[["symbol", "funding_time", "funding_rate"]])
        except Exception as exc:
            print(f"[WARN] funding {f}: {exc}")
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["funding_time"] = pd.to_datetime(df["funding_time"])
    df = df.groupby(["symbol", "funding_time"], as_index=False)["funding_rate"].median()
    symbols = df["symbol"].unique()
    idx = pd.date_range(df["funding_time"].min(), df["funding_time"].max(), freq="h", tz="UTC").tz_localize(None)
    wide = pd.DataFrame(index=idx)
    for sym in symbols:
        sub = df[df["symbol"] == sym].set_index("funding_time").sort_index()["funding_rate"]
        sub.index = sub.index.tz_localize(None)
        wide[sym] = sub.reindex(wide.index, method="ffill")
    return wide


def load_fear_greed(repo: Path) -> pd.DataFrame:
    files = sorted((repo / "historical_data" / "external").glob("fear_greed_*.parquet"))
    frames = []
    for f in files:
        try:
            df = pd.read_parquet(f)
            df["date"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None).dt.normalize()
            frames.append(df[["date", "value", "classification"]])
        except Exception as exc:
            print(f"[WARN] fng {f}: {exc}")
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True).drop_duplicates("date")
    return df.set_index("date")


def load_btc_dominance(repo: Path) -> pd.DataFrame:
    files = sorted((repo / "historical_data" / "external").glob("btc_dominance_*.parquet"))
    frames = []
    for f in files:
        try:
            df = pd.read_parquet(f)
            df["date"] = pd.to_datetime(df["snapshot_time"]).dt.tz_localize(None).dt.normalize()
            frames.append(df[["date", "btc_dominance"]])
        except Exception as exc:
            print(f"[WARN] dom {f}: {exc}")
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True).drop_duplicates("date")
    return df.set_index("date")


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Build peak calibration dataset")
    parser.add_argument("--output-dir", default="Dev/benchmarks/peaks_dataset")
    parser.add_argument("--max-workers", type=int, default=10)
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[2]
    out_dir = repo / args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[{datetime.now(timezone.utc).isoformat()}] Loading BTC data...")
    btc_1h = pd.read_parquet(repo / "historical_data" / "BTCUSDT_1h.parquet")
    btc_1h = btc_1h.sort_values("open_time").set_index("open_time")
    btc_1h.index = _strip_tz(pd.to_datetime(btc_1h.index))
    btc_1h = btc_1h[~btc_1h.index.duplicated(keep="last")]

    btc_1d = pd.read_parquet(repo / "historical_data" / "BTCUSDT_1d.parquet")
    btc_1d = btc_1d.sort_values("open_time").set_index("open_time")
    btc_1d.index = _strip_tz(pd.to_datetime(btc_1d.index, unit="ms", utc=True))
    btc_1d = btc_1d[~btc_1d.index.duplicated(keep="last")]

    print("Building regime time series...")
    regime_df = build_regime_df(btc_1h, btc_1d)
    regime_df["btc_return_1h"] = btc_1h["close"].pct_change(1).reindex(regime_df.index)
    regime_df["btc_return_4h"] = btc_1h["close"].pct_change(4).reindex(regime_df.index)
    regime_df["btc_return_24h"] = btc_1h["close"].pct_change(24).reindex(regime_df.index)
    regime_df["btc_atr_pct"] = (
        _atr(btc_1h["high"], btc_1h["low"], btc_1h["close"], 14) / btc_1h["close"]
    ).reindex(regime_df.index)

    print("Loading external data...")
    fg = load_fear_greed(repo)
    dom = load_btc_dominance(repo)
    ticker = load_ticker_snapshots(repo)
    funding = load_funding(repo)

    # Daily external data is broadcast to every hour of the same date.
    regime_df["date"] = regime_df.index.normalize()
    if not fg.empty:
        regime_df["fear_greed_value"] = regime_df["date"].map(fg["value"])
        regime_df["fear_greed_classification"] = regime_df["date"].map(fg["classification"])
    if not dom.empty:
        regime_df["btc_dominance"] = regime_df["date"].map(dom["btc_dominance"])
    regime_df = regime_df.drop(columns=["date"], errors="ignore")

    print("Computing market breadth...")
    spot_files = [
        f for f in (repo / "historical_data").glob("*_1h.parquet")
        if ".bak." not in f.name
    ]
    all_returns = {}
    for f in spot_files:
        sym = f.stem.replace("_1h", "")
        try:
            df = pd.read_parquet(f, columns=["open_time", "close"])
            df = df.drop_duplicates("open_time").set_index("open_time").sort_index()
            df.index = _strip_tz(pd.to_datetime(df.index))
            all_returns[sym] = df["close"].pct_change(24)
        except Exception:
            pass
    if all_returns:
        ret_matrix = pd.concat(all_returns, axis=1)
        market_breadth = ret_matrix.median(axis=1)
    else:
        market_breadth = pd.Series(dtype=float)

    windows = []
    for wf in (repo / "Dev" / "benchmarks" / "windows").glob("*.json"):
        try:
            data = json.loads(wf.read_text(encoding="utf-8"))
            windows.append({
                "window_id": data["window_id"],
                "start": pd.to_datetime(data["start"]).tz_localize(None),
                "end": pd.to_datetime(data["end"]).tz_localize(None),
                "dominant_regime": data.get("dominant_regime"),
                "symbols": set(data.get("symbols", [])),
            })
        except Exception:
            pass

    print(f"Processing {len(spot_files)} symbols with {args.max_workers} workers...")
    worker_args = [
        (f.stem.replace("_1h", ""), f, regime_df, ticker, funding, market_breadth)
        for f in spot_files
    ]

    jsonl_path = out_dir / "peaks_all.jsonl"
    total = 0
    symbols = set()
    peak_types = set()
    directions = set()
    stats = {}

    # Stream-write results as workers finish; this keeps the full record list out of memory.
    with open(jsonl_path, "w", encoding="utf-8") as f, ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        futures = {executor.submit(process_symbol, wa): wa[0] for wa in worker_args}
        for future in as_completed(futures):
            symbol_name = futures[future]
            recs = future.result()
            for r in recs:
                ts = pd.to_datetime(r["peak_start_ts"]).tz_localize(None)
                sym = r["symbol"]
                r["window_id"] = None
                r["window_regime"] = None
                for w in windows:
                    if w["start"] <= ts <= w["end"] and sym in w["symbols"]:
                        r["window_id"] = w["window_id"]
                        r["window_regime"] = w["dominant_regime"]
                        break
                f.write(json.dumps(_clean_dict(r), default=str) + "\n")
                total += 1
                symbols.add(sym)
                peak_types.add(r["peak_type"])
                directions.add(r["direction"])

                fs = r.get("features_start", {})
                mode = fs.get("mode_confirmed", "UNKNOWN")
                if mode not in stats:
                    stats[mode] = {"LONG": [], "SHORT": []}
                stats[mode][r["direction"]].append(float(r["peak_return"]))
            print(f"  {symbol_name}: {len(recs):,} records (running total {total:,})")

    print(f"Total records: {total}")

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_records": total,
        "symbols": sorted(symbols),
        "peak_types": sorted(peak_types),
        "directions": sorted(directions),
    }
    (out_dir / "peaks_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    stats_out = {}
    for mode, dirs in stats.items():
        stats_out[mode] = {}
        for direction, vals in dirs.items():
            if not vals:
                continue
            arr = np.array(vals)
            stats_out[mode][direction] = {
                "count": int(len(arr)),
                "mean_peak_return": float(arr.mean()),
                "p25": float(np.percentile(arr, 25)),
                "p50": float(np.percentile(arr, 50)),
                "p75": float(np.percentile(arr, 75)),
                "p90": float(np.percentile(arr, 90)),
                "p10": float(np.percentile(arr, 10)),
            }
    (out_dir / "peaks_by_regime.json").write_text(json.dumps(stats_out, indent=2, default=str), encoding="utf-8")

    readme = out_dir / "README.md"
    readme.write_text(
        """# Peak Calibration Dataset

Self-contained historical peak dataset for A/B/C profile calibration.

## Files

- `peaks_all.jsonl` — one record per detected peak, with `features_start` and `features_end`.
- `peaks_summary.json` — overall metadata.
- `peaks_by_regime.json` — return distribution by `mode` and `direction`.

## Schema (per record)

| field | description |
|-------|-------------|
| `symbol` | Spot symbol |
| `direction` | `LONG` / `SHORT` |
| `peak_type` | `forward_2h`, `forward_4h`, `local_extrema` |
| `peak_start_ts` | Timestamp at the beginning of the peak |
| `peak_end_ts` | Timestamp at the peak/trough |
| `peak_return` | `close_end / close_start - 1` |
| `features_start` | Full feature snapshot at `peak_start_ts` |
| `features_end` | Full feature snapshot at `peak_end_ts` |
| `window_id` | Window from `Dev/benchmarks/windows` if applicable |
| `window_regime` | Dominant regime of that window |

## Feature keys in `features_start/end`

Asset features: `open`, `high`, `low`, `close`, `volume`, `return_1h`, `return_4h`, `return_24h`,
`momentum_3h`, `momentum_6h`, `momentum_12h`, `atr`, `atr_pct`, `rsi_1h`, `rsi_4h`,
`bb_position`, `bb_width`, `range_position`, `vol_spike`, `volatility_24h`, `volatility_7d`,
`skew_24h`, `max_run_up_24h`, `max_drawdown_24h`, `btc_correlation_24h`.

Market/regime features: `btc_close`, `btc_return_1h/4h/24h`, `btc_atr_pct`,
`btc_7d_pct`, `btc_30d_pct`, `btc_day_pct`, `vol_ratio`, `bb_bandwidth`, `ema9_vs_ema21`,
`mode_raw`, `mode_confirmed`, `mode_futures`, `regime_label`, `confidence`,
`fear_greed_value`, `fear_greed_classification`, `btc_dominance`, `market_breadth`,
`funding_rate`, `ticker_last_price`, `ticker_price_change_percent`, `ticker_24h_volume`,
`ticker_quote_volume`, `ticker_weighted_avg_price`.
""",
        encoding="utf-8",
    )

    print(f"Done. Output written to {out_dir}")


if __name__ == "__main__":
    main()
