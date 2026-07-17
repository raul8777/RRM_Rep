#!/usr/bin/env python3
"""Vectorized, memory-light builder for the peak calibration dataset.

Re-uses the regime/feature helpers from build_peak_calibration_dataset.py but
writes records as a flat Parquet (and optionally flat JSONL) stream per symbol,
avoiding per-record Python dict/JSON round-trips.

Output (default parquet):
    Dev/benchmarks/peaks_dataset/peaks_all.parquet
    Dev/benchmarks/peaks_dataset/peaks_summary.json
    Dev/benchmarks/peaks_dataset/peaks_by_regime.json
    Dev/benchmarks/peaks_dataset/README.md
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

# Re-use the already-fixed helpers in the sibling script.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import build_peak_calibration_dataset as base

PEAK_HORIZONS = base.PEAK_HORIZONS


def _cast_string_columns(df: pd.DataFrame, cols: List[str]) -> pd.DataFrame:
    """Ensure known categorical columns are pandas string dtype (PyArrow-safe)."""
    for c in cols:
        if c in df.columns:
            # Use .astype('string') which represents missing values as <NA>.
            df[c] = df[c].astype("string")
    return df


def make_forward_records(
    features: pd.DataFrame,
    close: pd.Series,
    symbol: str,
    horizons: List[int],
) -> pd.DataFrame:
    """Vectorized forward-looking peak records for a single symbol."""
    out_frames = []

    for horizon in horizons:
        lag_frames = {k: close.shift(-k) for k in range(1, horizon + 1)}
        lag_df = pd.concat(lag_frames, axis=1)
        max_val = lag_df.max(axis=1)
        min_val = lag_df.min(axis=1)
        valid = max_val.notna() & min_val.notna()

        argmax_k = lag_df.loc[valid].idxmax(axis=1)
        argmin_k = lag_df.loc[valid].idxmin(axis=1)

        up_return = (max_val / close - 1).loc[valid]
        down_return = (min_val / close - 1).loc[valid]

        for direction, ret, lag in [
            ("LONG", up_return, argmax_k),
            ("SHORT", down_return, argmin_k),
        ]:
            start_ts = ret.index.to_series()
            end_lag = lag.astype(int)
            end_ts = start_ts + pd.to_timedelta(end_lag, unit="h")

            # Only keep rows whose exact end timestamp exists in the hourly index.
            indexer = features.index.get_indexer(end_ts)
            mask = indexer != -1
            if not mask.any():
                continue

            start_ts = start_ts[mask]
            end_ts = end_ts[mask]
            ret = ret[mask]

            df_start = features.loc[start_ts.to_list()].add_prefix("s_").reset_index(drop=True)
            df_end = features.loc[end_ts.to_list()].add_prefix("e_").reset_index(drop=True)

            df = pd.concat([df_start, df_end], axis=1)
            n = len(df)
            df["symbol"] = pd.Series([symbol] * n, dtype="string")
            df["direction"] = pd.Series([direction] * n, dtype="string")
            df["peak_type"] = pd.Series([f"forward_{horizon}h"] * n, dtype="string")
            df["peak_start_ts"] = start_ts.to_numpy()
            df["peak_end_ts"] = end_ts.to_numpy()
            df["peak_return"] = ret.to_numpy()
            out_frames.append(df)

    if out_frames:
        return pd.concat(out_frames, ignore_index=True)
    return pd.DataFrame()


def make_local_records(
    features: pd.DataFrame,
    close: pd.Series,
    symbol: str,
) -> pd.DataFrame:
    """Vectorized local-extrema peak records for a single symbol."""
    up_pairs, down_pairs = base.find_local_peaks(close)
    out_frames = []

    for pairs, direction in [(up_pairs, "LONG"), (down_pairs, "SHORT")]:
        if not pairs:
            continue
        start_i, end_i = zip(*pairs)
        start_i = np.array(start_i)
        end_i = np.array(end_i)

        df_start = features.iloc[start_i].add_prefix("s_").reset_index(drop=True)
        df_end = features.iloc[end_i].add_prefix("e_").reset_index(drop=True)

        df = pd.concat([df_start, df_end], axis=1)
        n = len(df)
        df["symbol"] = pd.Series([symbol] * n, dtype="string")
        df["direction"] = pd.Series([direction] * n, dtype="string")
        df["peak_type"] = pd.Series(["local_extrema"] * n, dtype="string")
        df["peak_start_ts"] = close.index[start_i].to_numpy()
        df["peak_end_ts"] = close.index[end_i].to_numpy()
        df["peak_return"] = close.iloc[end_i].to_numpy() / close.iloc[start_i].to_numpy() - 1
        out_frames.append(df)

    if out_frames:
        return pd.concat(out_frames, ignore_index=True)
    return pd.DataFrame()


def process_symbol(args: Tuple[Any, ...]) -> pd.DataFrame:
    symbol, path, regime_df, ticker_df, funding_df, market_breadth_series, windows = args
    try:
        df = pd.read_parquet(path)
    except Exception as exc:
        print(f"[SKIP] {symbol}: {exc}")
        return pd.DataFrame()

    df = df.sort_values("open_time").set_index("open_time")
    df.index = base._strip_tz(pd.to_datetime(df.index))
    df = df[~df.index.duplicated(keep="last")]
    df = df[(df["close"] > 0) & df["close"].notna()]
    if len(df) < 50:
        return pd.DataFrame()

    btc_returns = (
        regime_df["btc_return_24h"]
        if "btc_return_24h" in regime_df.columns
        else pd.Series(index=regime_df.index, dtype=float)
    )
    asset_feats = base.compute_asset_features(df, btc_returns)
    merged = asset_feats.join(regime_df, how="left")
    merged["market_breadth"] = market_breadth_series.reindex(merged.index)
    merged["date"] = pd.DatetimeIndex(merged.index).normalize()

    # Ticker snapshot columns (24h), always create the destination columns first
    # so every symbol has the same column schema.
    ticker_dst = {
        "last_price": "ticker_last_price",
        "price_change_percent": "ticker_price_change_percent",
        "volume": "ticker_24h_volume",
        "quote_volume": "ticker_quote_volume",
        "weighted_avg_price": "ticker_weighted_avg_price",
    }
    for dst in ticker_dst.values():
        merged[dst] = np.nan

    if not ticker_df.empty:
        sym_ticker = ticker_df[ticker_df["symbol"] == symbol].copy()
        if not sym_ticker.empty:
            sym_ticker = sym_ticker.drop_duplicates("date").set_index("date")
            for src, dst in ticker_dst.items():
                if src in sym_ticker.columns:
                    merged[dst] = merged["date"].map(sym_ticker[src])

    # Funding rate via merge_asof on sorted hourly index.
    merged["funding_rate"] = np.nan
    if not funding_df.empty and symbol in funding_df.columns:
        funding_sub = funding_df[[symbol]].rename(columns={symbol: "funding_rate_merged"})
        tmp = merged.reset_index().rename(columns={"index": "open_time"})
        tmp["open_time"] = pd.to_datetime(tmp["open_time"])
        tmp = tmp.sort_values("open_time")
        tmp = pd.merge_asof(
            tmp,
            funding_sub,
            left_on="open_time",
            right_index=True,
            direction="backward",
            allow_exact_matches=True,
        ).set_index("open_time")
        tmp = tmp.sort_index()
        if "funding_rate_merged" in tmp.columns:
            merged["funding_rate"] = tmp["funding_rate_merged"]

    features = merged.drop(columns=["date"], errors="ignore")
    features = features[~features.index.duplicated(keep="last")]

    # Cast string columns so PyArrow sees a consistent string schema.
    str_cols = [
        "ema9_vs_ema21",
        "mode_raw",
        "mode_confirmed",
        "mode_futures",
        "regime_label",
        "reason",
        "fear_greed_classification",
    ]
    features = _cast_string_columns(features, str_cols)

    close = features["close"].copy()
    forward_df = make_forward_records(features, close, symbol, PEAK_HORIZONS)
    local_df = make_local_records(features, close, symbol)
    out = pd.concat([forward_df, local_df], ignore_index=True)

    if out.empty:
        return out

    # Assign windows per symbol (avoids loading the full dataset into memory later).
    out["peak_start_ts"] = pd.to_datetime(out["peak_start_ts"])
    out["window_id"] = pd.Series(dtype="string")
    out["window_regime"] = pd.Series(dtype="string")
    for w in windows:
        mask = (
            (out["peak_start_ts"] >= w["start"])
            & (out["peak_start_ts"] <= w["end"])
            & out["symbol"].isin(w["symbols"])
        )
        if mask.any():
            out.loc[mask, "window_id"] = w["window_id"]
            out.loc[mask, "window_regime"] = w["dominant_regime"]

    # Make sure metadata columns are string dtype.
    out = _cast_string_columns(out, ["symbol", "direction", "peak_type", "window_id", "window_regime"])
    return out


def _write_readme(out_dir: Path) -> None:
    readme = out_dir / "README.md"
    readme.write_text(
        """# Peak Calibration Dataset

Self-contained historical peak dataset for A/B/C profile calibration.

## Files

- `peaks_all.parquet` — flat DataFrame with one row per detected peak.
- `peaks_all.jsonl` (optional) — JSON Lines version, generated with `--format jsonl`.
- `peaks_summary.json` — overall metadata.
- `peaks_by_regime.json` — return distribution by `mode` and `direction`.

## Columns

Metadata: `symbol`, `direction`, `peak_type`, `peak_start_ts`, `peak_end_ts`,
`peak_return`, `window_id`, `window_regime`.

Asset start features are prefixed with `s_` and end features with `e_`.
Examples: `s_return_1h`, `e_rsi_1h`, `s_vol_spike`, `e_btc_close`.

Market/regime features (prefixed `s_`/`e_` as well):
`btc_close`, `btc_return_1h/4h/24h`, `btc_atr_pct`, `btc_7d_pct`, `btc_30d_pct`,
`btc_day_pct`, `vol_ratio`, `bb_bandwidth`, `ema9_vs_ema21`, `mode_raw`,
`mode_confirmed`, `mode_futures`, `regime_label`, `confidence`, `fear_greed_value`,
`fear_greed_classification`, `btc_dominance`, `market_breadth`, `funding_rate`,
`ticker_last_price`, `ticker_price_change_percent`, `ticker_24h_volume`,
`ticker_quote_volume`, `ticker_weighted_avg_price`.
""",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build peak calibration dataset (fast/vectorized)")
    parser.add_argument("--output-dir", default="Dev/benchmarks/peaks_dataset")
    parser.add_argument("--max-workers", type=int, default=10)
    parser.add_argument("--format", choices=["parquet", "jsonl", "both"], default="parquet")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[2]
    out_dir = repo / args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[{datetime.now(timezone.utc).isoformat()}] Loading BTC data...")
    btc_1h = pd.read_parquet(repo / "historical_data" / "BTCUSDT_1h.parquet")
    btc_1h = btc_1h.sort_values("open_time").set_index("open_time")
    btc_1h.index = base._strip_tz(pd.to_datetime(btc_1h.index))
    btc_1h = btc_1h[~btc_1h.index.duplicated(keep="last")]

    btc_1d = pd.read_parquet(repo / "historical_data" / "BTCUSDT_1d.parquet")
    btc_1d = btc_1d.sort_values("open_time").set_index("open_time")
    btc_1d.index = base._strip_tz(pd.to_datetime(btc_1d.index, unit="ms", utc=True))
    btc_1d = btc_1d[~btc_1d.index.duplicated(keep="last")]

    print("Building regime time series...")
    regime_df = base.build_regime_df(btc_1h, btc_1d)
    regime_df["btc_return_1h"] = btc_1h["close"].pct_change(1).reindex(regime_df.index)
    regime_df["btc_return_4h"] = btc_1h["close"].pct_change(4).reindex(regime_df.index)
    regime_df["btc_return_24h"] = btc_1h["close"].pct_change(24).reindex(regime_df.index)
    regime_df["btc_atr_pct"] = (
        base._atr(btc_1h["high"], btc_1h["low"], btc_1h["close"], 14) / btc_1h["close"]
    ).reindex(regime_df.index)

    print("Loading external data...")
    fg = base.load_fear_greed(repo)
    dom = base.load_btc_dominance(repo)
    ticker = base.load_ticker_snapshots(repo)
    funding = base.load_funding(repo)

    # Broadcast daily external data to every hour.
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
            df.index = base._strip_tz(pd.to_datetime(df.index))
            all_returns[sym] = df["close"].pct_change(24)
        except Exception:
            pass
    if all_returns:
        market_breadth = pd.concat(all_returns, axis=1).median(axis=1)
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
        (f.stem.replace("_1h", ""), f, regime_df, ticker, funding, market_breadth, windows)
        for f in spot_files
    ]

    parquet_path = out_dir / "peaks_all.parquet"
    schema = None
    writer = None
    total = 0
    symbols = set()
    peak_types = set()
    directions = set()
    # Accumulate peak_return values by regime/direction for the summary JSON.
    stats: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: {"LONG": [], "SHORT": []})

    start_time = time.time()
    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        futures = {executor.submit(process_symbol, wa): wa[0] for wa in worker_args}
        for future in as_completed(futures):
            sym = futures[future]
            df = future.result()
            if df.empty:
                print(f"  {sym}: 0 records")
                continue

            # Update aggregate stats from the in-memory symbol DataFrame.
            symbols.update(df["symbol"].dropna().astype(str).unique())
            peak_types.update(df["peak_type"].dropna().astype(str).unique())
            directions.update(df["direction"].dropna().astype(str).unique())

            mode_col = "s_mode_confirmed"
            futures_col = "s_mode_futures"
            for _, row in df.iterrows():
                direction = str(row["direction"])
                pr = float(row["peak_return"])
                m = str(row[mode_col]) if pd.notna(row[mode_col]) else "UNKNOWN"
                stats[m][direction].append(pr)
                if pd.notna(row[futures_col]) and str(row[futures_col]) == "FUTURES_SHORT":
                    stats["FUTURES_SHORT"][direction].append(pr)

            # Initialize PyArrow writer/schema on the first non-empty batch.
            table = pa.Table.from_pandas(df)
            if writer is None:
                schema = table.schema
                writer = pq.ParquetWriter(parquet_path, schema)
            else:
                table = pa.Table.from_pandas(df, schema=schema)
            writer.write_table(table)

            total += len(df)
            elapsed = time.time() - start_time
            print(f"  {sym}: {len(df):,} records (running total {total:,} in {elapsed/60:.1f}m)")

    if writer is not None:
        writer.close()
    print(f"Total records: {total:,}")

    if total == 0:
        print("No records generated.")
        return

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_records": total,
        "symbols": sorted(symbols),
        "peak_types": sorted(peak_types),
        "directions": sorted(directions),
    }
    (out_dir / "peaks_summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )

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
    (out_dir / "peaks_by_regime.json").write_text(
        json.dumps(stats_out, indent=2, default=str), encoding="utf-8"
    )

    # Optional JSONL output.
    if args.format in ("jsonl", "both"):
        jsonl_path = out_dir / "peaks_all.jsonl"
        all_df = pd.read_parquet(parquet_path)
        all_df.to_json(jsonl_path, orient="records", lines=True, date_format="iso")
        print(f"Saved JSONL: {jsonl_path}")

    _write_readme(out_dir)
    print(f"Done. Output written to {out_dir}")


if __name__ == "__main__":
    main()
