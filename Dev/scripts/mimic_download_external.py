"""MIMIC — Download external market data needed for realistic simulations.

Fetches historical funding rates, 24h tickers, BTC dominance and Fear & Greed
from public APIs and stores them as parquet files under historical_data/external/.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
import urllib.request

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo_root)


EXTERNAL_DIR = os.path.join(repo_root, "historical_data", "external")
os.makedirs(EXTERNAL_DIR, exist_ok=True)


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _ts_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_json(url: str, retries: int = 3, timeout: int = 20) -> Any:
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "MIMIC-data-fetcher/1.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def download_funding_rate(
    symbols: List[str],
    start: datetime,
    end: datetime,
    output_dir: str = EXTERNAL_DIR,
) -> Dict[str, Any]:
    """Download Binance USDT-M funding rate history for symbols."""
    all_rows: List[Dict[str, Any]] = []
    start_ms = _ts_to_ms(start)
    end_ms = _ts_to_ms(end)

    for symbol in symbols:
        url = (
            f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}"
            f"&startTime={start_ms}&endTime={end_ms}&limit=1000"
        )
        try:
            data = _fetch_json(url)
            for row in data:
                all_rows.append({
                    "symbol": symbol,
                    "funding_time": datetime.fromtimestamp(int(row["fundingTime"]) / 1000, tz=timezone.utc),
                    "funding_rate": float(row["fundingRate"]),
                    "source_url": url,
                })
        except Exception as e:
            print(f"[FundingRate] {symbol}: {e}")

    path = os.path.join(output_dir, f"funding_rate_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.parquet")
    df = pd.DataFrame(all_rows)
    if not df.empty:
        df.to_parquet(path, index=False)
    return {"path": path, "rows": len(df), "error": None if not df.empty else "no_data"}


def download_ticker_24h(
    dates: List[datetime],
    output_dir: str = EXTERNAL_DIR,
) -> Dict[str, Any]:
    """Download daily 24hr ticker snapshots from Binance."""
    paths: List[str] = []
    for date in dates:
        # Use a time near the end of the day UTC for consistency
        snapshot_time = date.replace(hour=23, minute=59)
        url = "https://api.binance.com/api/v3/ticker/24hr"
        try:
            data = _fetch_json(url)
            rows = []
            for row in data:
                if not row.get("symbol", "").endswith("USDT"):
                    continue
                rows.append({
                    "symbol": row["symbol"],
                    "snapshot_time": snapshot_time,
                    "last_price": float(row.get("lastPrice", 0)),
                    "price_change_percent": float(row.get("priceChangePercent", 0)),
                    "volume": float(row.get("volume", 0)),
                    "quote_volume": float(row.get("quoteVolume", 0)),
                    "weighted_avg_price": float(row.get("weightedAvgPrice", 0)),
                    "source_url": url,
                })
            path = os.path.join(output_dir, f"ticker_24h_{date.strftime('%Y%m%d')}.parquet")
            df = pd.DataFrame(rows)
            df.to_parquet(path, index=False)
            paths.append(path)
            time.sleep(0.5)  # be polite to public API
        except Exception as e:
            print(f"[Ticker24h] {date.date()}: {e}")
    return {"paths": paths, "total_files": len(paths)}


def download_btc_dominance(
    start: datetime,
    end: datetime,
    output_dir: str = EXTERNAL_DIR,
) -> Dict[str, Any]:
    """Download CoinGecko BTC dominance (global data) daily.

    Note: CoinGecko /global returns current data only on free tier.
    We record the fetch and flag it as snapshot; historical daily data may
    require paid tier or interpolation.
    """
    url = "https://api.coingecko.com/api/v3/global"
    rows = []
    date = start
    while date <= end:
        try:
            data = _fetch_json(url)
            btc_dom = data.get("data", {}).get("market_cap_percentage", {}).get("btc", 0.0)
            rows.append({
                "date": date.date(),
                "btc_dominance": float(btc_dom),
                "snapshot_time": datetime.now(timezone.utc),
                "source_url": url,
                "interpolated": False,
            })
            time.sleep(6)  # CoinGecko free rate limit
        except Exception as e:
            print(f"[BTC Dominance] {date.date()}: {e}")
        date += timedelta(days=1)

    path = os.path.join(output_dir, f"btc_dominance_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.parquet")
    df = pd.DataFrame(rows)
    if not df.empty:
        df.to_parquet(path, index=False)
    return {"path": path, "rows": len(df)}


def download_fear_greed(
    start: datetime,
    end: datetime,
    output_dir: str = EXTERNAL_DIR,
) -> Dict[str, Any]:
    """Download Alternative.me Fear & Greed historical index."""
    url = "https://api.alternative.me/fng/?limit=0&format=json"
    data = _fetch_json(url)
    rows = []
    for item in data.get("data", []):
        try:
            ts = datetime.fromtimestamp(int(item["timestamp"]), tz=timezone.utc)
            if start <= ts <= end:
                rows.append({
                    "date": ts.date(),
                    "timestamp": ts,
                    "value": int(item["value"]),
                    "classification": item.get("value_classification", ""),
                    "source_url": url,
                })
        except Exception:
            continue

    path = os.path.join(output_dir, f"fear_greed_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.parquet")
    df = pd.DataFrame(rows)
    if not df.empty:
        df.to_parquet(path, index=False)
    return {"path": path, "rows": len(df)}


def build_manifest(output_dir: str = EXTERNAL_DIR) -> str:
    """Generate manifest with SHA256 hashes for all external parquet files."""
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": [],
    }
    for fname in sorted(os.listdir(output_dir)):
        if not fname.endswith(".parquet"):
            continue
        path = os.path.join(output_dir, fname)
        manifest["files"].append({
            "file": fname,
            "path": path,
            "sha256": _hash_file(path),
            "size_bytes": os.path.getsize(path),
        })
    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Download external data for MIMIC simulations")
    parser.add_argument("--start", required=True, help="Start date ISO, e.g. 2026-01-01")
    parser.add_argument("--end", required=True, help="End date ISO, e.g. 2026-01-30")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT",
                        help="Comma-separated futures symbols for funding rate")
    parser.add_argument("--skip-funding", action="store_true")
    parser.add_argument("--skip-ticker", action="store_true")
    parser.add_argument("--skip-btc-dominance", action="store_true")
    parser.add_argument("--skip-fear-greed", action="store_true")
    args = parser.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    results: Dict[str, Any] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "symbols": symbols,
    }

    if not args.skip_funding:
        print("Downloading funding rates...")
        results["funding_rate"] = download_funding_rate(symbols, start, end)

    if not args.skip_ticker:
        print("Downloading ticker 24h snapshots...")
        results["ticker_24h"] = download_ticker_24h(dates)

    if not args.skip_btc_dominance:
        print("Downloading BTC dominance snapshots...")
        results["btc_dominance"] = download_btc_dominance(start, end)

    if not args.skip_fear_greed:
        print("Downloading Fear & Greed...")
        results["fear_greed"] = download_fear_greed(start, end)

    manifest_path = build_manifest()
    results["manifest_path"] = manifest_path

    report_path = os.path.join(repo_root, "Dev", "benchmarks", "mimic_external_download_report.json")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": _now_str(), "results": results}, f, indent=2, ensure_ascii=False)
    print(f"Report saved to {report_path}")
    print(f"Manifest saved to {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
