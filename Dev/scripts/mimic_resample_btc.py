"""MIMIC — Resample BTC 1h parquet into 4h and 1d intervals for regime detection.

market_regime.get_regime() needs 1h, 4h and 1d BTC klines. Most data pipelines
only download 1h, so this script builds the other intervals locally.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo_root)


def _aggregate(bucket: List[Dict[str, Any]]) -> Dict[str, Any]:
    opens = [float(b["open"]) for b in bucket]
    highs = [float(b["high"]) for b in bucket]
    lows = [float(b["low"]) for b in bucket]
    closes = [float(b["close"]) for b in bucket]
    volumes = [float(b.get("volume", 0)) for b in bucket]
    quote_volumes = [float(b.get("quote_volume", 0)) for b in bucket]
    trades = [int(b.get("trades", 0)) for b in bucket]
    taker_buy_bases = [float(b.get("taker_buy_base", 0)) for b in bucket]
    taker_buy_quotes = [float(b.get("taker_buy_quote", 0)) for b in bucket]
    return {
        "open_time": bucket[0]["open_time"],
        "open": opens[0],
        "high": max(highs),
        "low": min(lows),
        "close": closes[-1],
        "volume": sum(volumes),
        "quote_volume": sum(quote_volumes),
        "trades": sum(trades),
        "taker_buy_base": sum(taker_buy_bases),
        "taker_buy_quote": sum(taker_buy_quotes),
    }


def resample(df: pd.DataFrame, interval_hours: int) -> pd.DataFrame:
    """Resample 1h dataframe to interval_hours."""
    df = df.sort_values("open_time").copy()
    df["bucket"] = (df["open_time"].astype(int) // (interval_hours * 3600)) * (interval_hours * 3600)
    buckets = []
    for _, group in df.groupby("bucket"):
        bucket = group.to_dict("records")
        buckets.append(_aggregate(bucket))
    out = pd.DataFrame(buckets)
    # Only keep complete buckets
    expected_rows = interval_hours
    out = out[out["open_time"].isin(df["bucket"].unique())]
    return out.sort_values("open_time").reset_index(drop=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Resample BTC 1h to 4h/1d")
    parser.add_argument("--data-dir", default="historical_data")
    args = parser.parse_args()

    path_1h = os.path.join(args.data_dir, "BTCUSDT_1h.parquet")
    if not os.path.exists(path_1h):
        print(f"[Resample] Missing {path_1h}")
        return 1

    df = pd.read_parquet(path_1h)
    if "open_time" not in df.columns:
        print("[Resample] BTCUSDT_1h.parquet missing open_time column")
        return 1

    # Normalize open_time to epoch seconds regardless of original dtype/unit
    if str(df["open_time"].dtype).startswith("datetime64"):
        unit = pd.DatetimeIndex(df["open_time"]).unit
        divisor = {"s": 1, "ms": 1_000, "us": 1_000_000, "ns": 1_000_000_000}.get(unit, 1_000_000_000)
        df["open_time"] = df["open_time"].astype("int64") // divisor
    else:
        # Already numeric; if it looks like milliseconds normalize to seconds
        if df["open_time"].dtype == "int64" and df["open_time"].max() > 10**12:
            df["open_time"] = df["open_time"] // 1000

    for interval, hours in (("4h", 4), ("1d", 24)):
        out = resample(df, hours)
        out_path = os.path.join(args.data_dir, f"BTCUSDT_{interval}.parquet")
        out.to_parquet(out_path, index=False)
        print(f"[Resample] {out_path}: {len(out)} rows")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
