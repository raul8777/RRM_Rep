"""MIMIC — Download historical klines for specific symbols and date windows."""
from __future__ import annotations
import os
import sys
import json
import argparse
import shutil
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo_root)

import pandas as pd
from bootstrap_history import _fetch_all_klines
from binance_client import BinanceClient


def _backup_existing(path: str) -> None:
    if not os.path.exists(path):
        return
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup = f"{path}.bak.{ts}"
    shutil.copy2(path, backup)
    print(f"Backed up {path} -> {backup}")


def _load_existing(path: str) -> pd.DataFrame:
    if os.path.exists(path):
        return pd.read_parquet(path)
    return pd.DataFrame(columns=[
        "open_time", "open", "high", "low", "close", "volume"
    ])


def _merge_and_save(df_old: pd.DataFrame, new_klines: List[Dict[str, Any]], path: str) -> int:
    if not new_klines:
        return len(df_old)
    df_new = pd.DataFrame(new_klines)
    # Normalize open_time to ms timestamp
    df_new["open_time"] = pd.to_datetime(df_new["open_time"], unit="ms", utc=True)
    df_new["open_time_ms"] = df_new["open_time"].astype("int64") // 1_000_000
    if "open_time_ms" not in df_old.columns:
        df_old["open_time_ms"] = df_old["open_time"].astype("int64") // 1_000_000
    df_combined = pd.concat([df_old, df_new], ignore_index=True)
    df_combined = df_combined.drop_duplicates(subset=["open_time_ms"], keep="last")
    df_combined = df_combined.sort_values("open_time_ms").reset_index(drop=True)
    # Keep canonical columns
    keep = ["open_time", "open", "high", "low", "close", "volume"]
    df_combined = df_combined[[c for c in keep if c in df_combined.columns]]
    df_combined.to_parquet(path, index=False)
    return len(df_combined)


def download_symbol(
    symbol: str,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    interval: str = "1h",
    days: Optional[int] = None,
    client: Optional[BinanceClient] = None,
    data_dir: Optional[str] = None,
) -> Dict[str, Any]:
    data_dir = data_dir or os.path.join(repo_root, "historical_data")
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, f"{symbol}_{interval}.parquet")

    result = {
        "symbol": symbol,
        "downloaded": 0,
        "total": 0,
        "error": None,
    }

    try:
        _backup_existing(path)
        df_old = _load_existing(path)

        if client is None:
            client = BinanceClient(api_key="", api_secret="", testnet=False, dry_run=False)

        if days is None:
            days = 180
            if start and end:
                days = max((end - start).days, 7)

        klines = _fetch_all_klines(client, symbol, interval, days=days)
        if start and end:
            # Trim by timestamp
            start_ms = int(start.timestamp() * 1000)
            end_ms = int(end.timestamp() * 1000)
            klines = [k for k in klines if start_ms <= k["open_time"] <= end_ms]

        total = _merge_and_save(df_old, klines, path)
        result["downloaded"] = len(klines)
        result["total"] = total
    except Exception as e:
        result["error"] = str(e)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Download historical klines for MIMIC")
    parser.add_argument("--symbols", required=True, help="Comma-separated symbols, e.g. BTCUSDT,ETHUSDT")
    parser.add_argument("--start", help="Start date ISO, e.g. 2026-01-01")
    parser.add_argument("--end", help="End date ISO, e.g. 2026-07-09")
    parser.add_argument("--interval", default="1h", help="Kline interval")
    parser.add_argument("--days", type=int, default=180, help="Days to download if start/end not provided")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc) if args.start else None
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) if args.end else datetime.now(timezone.utc)

    client = BinanceClient(api_key="", api_secret="", testnet=False, dry_run=False)
    data_dir = os.path.join(repo_root, "historical_data")
    results = []

    for symbol in symbols:
        print(f"Downloading {symbol} ...")
        res = download_symbol(symbol, start=start, end=end, interval=args.interval, client=client, data_dir=data_dir)
        print(f"  -> {res}")
        results.append(res)

    out_path = os.path.join(repo_root, "Dev", "benchmarks", "mimic_download_report.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "symbols": results,
        }, f, indent=2, ensure_ascii=False)
    print(f"Report saved to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
