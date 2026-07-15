"""MIMIC — Build a coherent 30-day simulation window.

This script:
1. Validates that OHLCV data exists for the requested window.
2. Downloads / validates external data (funding rate, ticker 24h, BTC dominance, F&G).
3. Detects historical regimes across the window using BTC data.
4. Runs a historical screener to pick the symbol universe.
5. Writes a window manifest with full traceability (hashes, commit, settings snapshot).

Usage:
    python Dev/scripts/mimic_build_window.py --start 2026-01-01 --end 2026-01-30 \
        --symbols BTCUSDT,ETHUSDT,BNBUSDT,... --output Dev/benchmarks/mimic_window_2026_01.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

import pandas as pd

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo_root)

from config import get_settings
from simulation.mimic_historical_screener import HistoricalScreener
from simulation.mimic_regime_detector import detect_regime_at, compute_confidence


WINDOWS_DIR = os.path.join(repo_root, "Dev", "benchmarks", "windows")
os.makedirs(WINDOWS_DIR, exist_ok=True)


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            text=True,
        ).strip()
    except Exception:
        return "unknown"


def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_parquet_symbol(symbol: str, data_dir: str) -> Optional[str]:
    path = os.path.join(data_dir, f"{symbol}_1h.parquet")
    if not os.path.exists(path):
        return None
    return _hash_file(path)


def _load_btc_feeds(data_dir: str) -> Dict[str, List[Dict[str, Any]]]:
    from simulation.mimic_feed import HistoricalDataFeed
    feed = HistoricalDataFeed(data_dir=data_dir)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for iv in ("1h", "4h", "1d"):
        try:
            feed.load_symbols(["BTCUSDT"], iv)
            out[iv] = feed.get_klines("BTCUSDT", iv)
        except Exception as e:
            print(f"[Window] Could not load BTC {iv}: {e}")
            out[iv] = []
    return out


def _coverage_ok(symbols: List[str], start: datetime, end: datetime, data_dir: str) -> bool:
    ok = True
    for symbol in symbols:
        path = os.path.join(data_dir, f"{symbol}_1h.parquet")
        if not os.path.exists(path):
            print(f"[Window] Missing parquet for {symbol}")
            ok = False
            continue
        df = pd.read_parquet(path)
        if df.empty:
            print(f"[Window] Empty parquet for {symbol}")
            ok = False
            continue
        ts_col = "open_time" if "open_time" in df.columns else df.columns[0]
        df[ts_col] = pd.to_datetime(df[ts_col], unit="s", utc=True)
        _tol = timedelta(days=1)
        if df[ts_col].min() > start + _tol or df[ts_col].max() < end - _tol:
            print(f"[Window] {symbol} coverage {df[ts_col].min()} - {df[ts_col].max()} does not contain {start.date()} - {end.date()}")
            ok = False
    return ok


def _detect_regimes_for_window(
    btc_feeds: Dict[str, List[Dict[str, Any]]],
    start: datetime,
    end: datetime,
    step_hours: int = 6,
) -> List[Dict[str, Any]]:
    """Sample regime detection every N hours to produce a regime timeline."""
    timeline = []
    current = start
    while current <= end:
        raw = detect_regime_at(btc_feeds, current)
        raw["confidence"] = compute_confidence(raw)
        timeline.append({
            "timestamp": current.isoformat(),
            "mode": raw.get("mode"),
            "regime": raw.get("regime"),
            "reason": raw.get("reason"),
            "confidence": raw.get("confidence"),
            "btc_7d_pct": raw.get("btc_7d_pct"),
            "btc_30d_pct": raw.get("btc_30d_pct"),
            "btc_day_pct": raw.get("btc_day_pct"),
            "vol_ratio": raw.get("vol_ratio"),
            "bb_bandwidth": raw.get("bb_bandwidth"),
            "ema9_vs_ema21": raw.get("ema9_vs_ema21"),
        })
        current += timedelta(hours=step_hours)
    return timeline


def _dominant_regime(timeline: List[Dict[str, Any]]) -> str:
    counts: Dict[str, int] = {}
    for t in timeline:
        counts[t.get("mode", "SPOT_BULL")] = counts.get(t.get("mode", "SPOT_BULL"), 0) + 1
    return max(counts, key=counts.get) if counts else "SPOT_BULL"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a MIMIC 30-day simulation window")
    parser.add_argument("--start", required=True, help="Window start ISO date")
    parser.add_argument("--end", required=True, help="Window end ISO date")
    parser.add_argument("--symbols", default=None, help="Comma-separated baseline symbols")
    parser.add_argument("--data-dir", default="historical_data")
    parser.add_argument("--external-dir", default="historical_data/external")
    parser.add_argument("--top-n", type=int, default=40, help="Max symbols after screener")
    parser.add_argument("--output", default=None, help="Output JSON path")
    parser.add_argument("--skip-external-check", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    baseline_symbols = []
    if args.symbols:
        baseline_symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        # Default broad universe
        baseline_symbols = [
            "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT",
            "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "TRXUSDT", "TONUSDT",
            "NEARUSDT", "MATICUSDT", "FETUSDT", "LTCUSDT", "BCHUSDT", "UNIUSDT",
            "APTUSDT", "ATOMUSDT", "ICPUSDT", "FILUSDT", "XLMUSDT", "ALGOUSDT",
            "INJUSDT", "SUIUSDT", "OPUSDT", "ARBUSDT", "RENDERUSDT", "WLDUSDT",
            "ONDOUSDT", "PEPEUSDT", "SHIBUSDT", "WIFUSDT", "JTOUSDT", "TIAUSDT",
            "SEIUSDT", "STRKUSDT", "PYTHUSDT", "AXSUSDT", "SANDUSDT", "MANAUSDT",
        ]

    print(f"[Window] Building window {start.date()} to {end.date()}")

    # 1. Coverage check
    if not _coverage_ok(baseline_symbols, start, end, args.data_dir):
        print("[Window] Coverage check failed. Download missing symbols first.")
        return 1

    # 2. External data check
    external_files = {}
    if not args.skip_external_check:
        for prefix in ["funding_rate", "ticker_24h", "btc_dominance", "fear_greed"]:
            matches = [
                f for f in os.listdir(args.external_dir)
                if f.startswith(prefix) and f.endswith(".parquet")
            ]
            external_files[prefix] = [os.path.join(args.external_dir, m) for m in matches]
            if not matches:
                print(f"[Window] Warning: no external data files for {prefix}")

    # 3. Regime timeline
    btc_feeds = _load_btc_feeds(args.data_dir)
    timeline = _detect_regimes_for_window(btc_feeds, start, end, step_hours=6)
    dominant = _dominant_regime(timeline)
    print(f"[Window] Dominant regime: {dominant} ({len(timeline)} samples)")

    # 4. Historical screener universe
    screener = HistoricalScreener(ticker_dir=args.external_dir)
    universe_symbols = set(baseline_symbols)
    current = start
    while current <= end:
        for row in screener.screen(current, dominant, settings, limit=args.top_n):
            universe_symbols.add(row["symbol"])
        current += timedelta(days=1)
    final_symbols = sorted(universe_symbols)
    print(f"[Window] Universe size: {len(final_symbols)}")

    # 5. Build manifest with hashes
    data_manifest = {}
    for symbol in final_symbols:
        h = _hash_parquet_symbol(symbol, args.data_dir)
        data_manifest[symbol] = h

    window_id = f"win_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}"
    payload = {
        "window_id": window_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "symbols": final_symbols,
        "baseline_symbols": baseline_symbols,
        "dominant_regime": dominant,
        "regime_timeline": timeline,
        "data_manifest_hash": hashlib.sha256(
            json.dumps(data_manifest, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "data_manifest": data_manifest,
        "external_files": external_files,
        "settings_snapshot": {k: str(v) for k, v in settings.model_dump().items()},
        "commit_git": _git_commit(),
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }

    output_path = args.output or os.path.join(WINDOWS_DIR, f"{window_id}.json")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(f"[Window] Saved manifest to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
