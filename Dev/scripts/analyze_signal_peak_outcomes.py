"""Analyze historical price peaks behind BUY signals for predictive A/B/C calibration.

For each BUY signal the lifecycle system would have emitted in historical 1h data,
compute the maximum forward return (peak) at 2h and 4h horizons. Record the
point-in-time features so we can derive thresholds that map A/B/C to actual
short-term gain potential.

Output:
  - Dev/benchmarks/super_mimic/predictive_sweep/signal_peak_outcomes.jsonl
  - Dev/benchmarks/super_mimic/predictive_sweep/peak_analysis_status.json
  - Dev/benchmarks/super_mimic/predictive_sweep/peak_analysis.log

This script is meant to run unattended (e.g. overnight). It writes progress to the
status file and log file periodically.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from simulation.mimic_lifecycle_adapter import _build_analyses, _compute_peak_quality

WINDOWS_DIR = Path("Dev/benchmarks/windows")
HIST_DIR = Path("historical_data")
EXT_DIR = HIST_DIR / "external"
OUT_DIR = Path("Dev/benchmarks/super_mimic/predictive_sweep")
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSONL = OUT_DIR / "signal_peak_outcomes.jsonl"
OUT_STATUS = OUT_DIR / "peak_analysis_status.json"
OUT_LOG = OUT_DIR / "peak_analysis.log"

NARRATIVE_TOKENS = {
    "FETUSDT", "TAOUSDT", "RENDERUSDT", "NEARUSDT", "WLDUSDT", "AGIXUSDT", "OCEANUSDT",
    "ARBUSDT", "MATICUSDT", "ATOMUSDT", "INJUSDT", "APTUSDT", "SUIUSDT", "SEIUSDT",
    "TIAUSDT", "PYTHUSDT", "JTOUSDT", "BRETTUSDT", "EIGENUSDT",
}
LARGE_CAPS = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "TRXUSDT"}

logging.basicConfig(
    filename=OUT_LOG,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    filemode="w",
)
logger = logging.getLogger("peak_analysis")
console = logging.StreamHandler(sys.stdout)
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(console)


class _Status:
    def __init__(self, total_tasks: int):
        self.lock = __import__("threading").Lock()
        self.total = total_tasks
        self.done = 0
        self.signals = 0
        self.errors: List[str] = []
        self.running = True

    def bump(self, signals: int, error: Optional[str] = None):
        with self.lock:
            self.done += 1
            self.signals += signals
            if error:
                self.errors.append(error)
            self._write()

    def mark_completed(self):
        with self.lock:
            self.running = False
            self._write()

    def _write(self):
        try:
            status = {
                "status": "running" if self.running else "completed",
                "progress_pct": round(self.done / self.total * 100, 2) if self.total else 100.0,
                "tasks_done": self.done,
                "tasks_total": self.total,
                "signals_analyzed": self.signals,
                "errors_count": len(self.errors),
                "last_errors": self.errors[-5:],
                "output_files": {
                    "jsonl": str(OUT_JSONL),
                    "log": str(OUT_LOG),
                },
            }
            with open(OUT_STATUS, "w", encoding="utf-8") as f:
                json.dump(status, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to write status: {e}")


def load_windows() -> List[Dict[str, Any]]:
    windows: List[Dict[str, Any]] = []
    if not WINDOWS_DIR.exists():
        return windows
    for p in sorted(WINDOWS_DIR.glob("win_*.json")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                windows.append(json.load(f))
        except Exception as e:
            logger.warning(f"Skipping {p}: {e}")
    return windows


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _prepare_bars(df: pd.DataFrame, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """Return list of bar dicts from parquet, filtered to window range."""
    if df.empty:
        return []
    ts_col = "open_time"
    if df[ts_col].dt.tz is None:
        df = df.copy()
        df[ts_col] = df[ts_col].dt.tz_localize("UTC")
    mask = (df[ts_col] >= start) & (df[ts_col] <= end)
    df = df.loc[mask].sort_values(ts_col).reset_index(drop=True)
    bars = []
    for _, row in df.iterrows():
        close = float(row["close"])
        volume = float(row["volume"])
        bars.append(
            {
                "open_time": row[ts_col].isoformat(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": close,
                "volume": volume,
                "quote_volume": volume * close,
            }
        )
    return bars


def load_symbol_bars(symbol: str, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    path = HIST_DIR / f"{symbol}_1h.parquet"
    if not path.exists():
        return []
    try:
        df = pd.read_parquet(path)
        return _prepare_bars(df, start, end)
    except Exception as e:
        logger.warning(f"Failed to load {symbol}: {e}")
        return []


def build_regime_lookup(timeline: List[Dict[str, Any]]) -> List[Tuple[datetime, str]]:
    """Return sorted list of (timestamp, mode) from window regime timeline."""
    entries = []
    for entry in timeline:
        try:
            ts = _parse_ts(entry["timestamp"])
            mode = entry.get("mode") or entry.get("regime") or "SPOT_BULL"
            entries.append((ts, mode))
        except Exception:
            continue
    entries.sort(key=lambda x: x[0])
    return entries


def regime_at(lookup: List[Tuple[datetime, str]], ts: datetime) -> str:
    current = "SPOT_BULL"
    for t, mode in lookup:
        if t <= ts:
            current = mode
        else:
            break
    return current


def analyze_symbol_window(window: Dict[str, Any], symbol: str) -> List[Dict[str, Any]]:
    start = _parse_ts(window["start"])
    end = _parse_ts(window["end"])
    regime_lookup = build_regime_lookup(window.get("regime_timeline", []))

    bars = load_symbol_bars(symbol, start, end)
    if len(bars) < 169:
        return []

    records: List[Dict[str, Any]] = []
    # Use a rolling feed: for each bar t, feed has bars[0:t+1].
    for t in range(168, len(bars)):
        feed = {symbol: bars[: t + 1]}
        ts = _parse_ts(bars[t]["open_time"])
        regime = regime_at(regime_lookup, ts)

        try:
            analyses = _build_analyses([symbol], feed, regime)
        except Exception as e:
            logger.debug(f"build_analyses failed {window['window_id']} {symbol} @ {t}: {e}")
            continue

        if not analyses:
            continue
        analysis = analyses[0]
        if analysis.get("signal") != "BUY":
            continue

        # Forward peaks using high prices.
        future_bars_12h = min(12, len(bars) - t - 1)
        future_bars_4h = min(4, len(bars) - t - 1)
        if future_bars_4h <= 0:
            continue

        future_highs = [bars[t + i]["high"] for i in range(1, future_bars_12h + 1)]
        future_lows = [bars[t + i]["low"] for i in range(1, future_bars_12h + 1)]
        entry_close = bars[t]["close"]
        peak_12h = max(future_highs) / entry_close - 1.0
        peak_4h = max(future_highs[:future_bars_4h]) / entry_close - 1.0
        peak_2h = max(future_highs[:2]) / entry_close - 1.0
        max_dd = min(future_lows[:future_bars_4h]) / entry_close - 1.0

        atr = float(analysis.get("atr", 0.0))
        atr_pct = atr / entry_close if entry_close > 0 else 0.0

        record = {
            "window_id": window["window_id"],
            "symbol": symbol,
            "timestamp": bars[t]["open_time"],
            "regime": regime,
            "entry_close": entry_close,
            "atr": atr,
            "atr_pct": atr_pct,
            "peak_2h": peak_2h,
            "peak_4h": peak_4h,
            "peak_12h": peak_12h,
            "max_drawdown_4h": max_dd,
        }
        for key, value in analysis.items():
            if key in record:
                continue
            if isinstance(value, (int, float, str, bool)):
                record[key] = value
            elif isinstance(value, dict):
                # flatten indicator dict with prefix to avoid nested JSONL
                for sub_key, sub_value in value.items():
                    if isinstance(sub_value, (int, float, str, bool)):
                        record[f"{key}_{sub_key}"] = sub_value
        records.append(record)

    return records


def symbol_category(symbol: str) -> str:
    if symbol in LARGE_CAPS:
        return "LARGE_CAP"
    if symbol in NARRATIVE_TOKENS:
        return "NARRATIVE"
    return "MIDCAP_ALT"


def _load_ticker_snapshot(date_str: str) -> Optional[pd.DataFrame]:
    path = EXT_DIR / f"ticker_24h_{date_str}.parquet"
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
        if "snapshot_time" in df.columns and df["snapshot_time"].dt.tz is None:
            df = df.copy()
            df["snapshot_time"] = df["snapshot_time"].dt.tz_localize("UTC")
        df = df.sort_values("snapshot_time").groupby("symbol").last().reset_index()
        return df
    except Exception as e:
        logger.warning(f"Failed to load ticker snapshot {path}: {e}")
        return None


def _pct(values: List[float], p: float) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    k = (len(s) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _enrich_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add category, ticker momentum, symbol quality, peak_quality and A/B/C label."""
    ticker_cache: Dict[str, Optional[pd.DataFrame]] = {}
    for r in records:
        r["category"] = symbol_category(r["symbol"])
        dt = datetime.fromisoformat(r["timestamp"])
        ds = dt.strftime("%Y%m%d")
        df = ticker_cache.get(ds)
        if df is None:
            df = _load_ticker_snapshot(ds)
            ticker_cache[ds] = df
        if df is not None and r["symbol"] in df["symbol"].values:
            row = df[df["symbol"] == r["symbol"]].iloc[0]
            r["ticker_price_change_pct"] = float(row["price_change_percent"])
        else:
            r["ticker_price_change_pct"] = 0.0

    # symbol quality: rolling mean peak_12h by symbol
    sym_peaks: Dict[str, List[float]] = defaultdict(list)
    for r in records:
        sym_peaks[r["symbol"]].append(r["peak_12h"])
    sym_mean = {s: sum(v) / len(v) for s, v in sym_peaks.items()}

    for r in records:
        sq = (sym_mean.get(r["symbol"], 0.02) - 0.02) * 2.0
        r["symbol_quality_score"] = sq
        features = {
            "atr_pct": r.get("atr_pct", 0.0),
            "expected_pnl": r.get("expected_pnl", 0.0),
            "short_term_score": r.get("short_term_score", 0.0),
            "pnl_score": r.get("pnl_score", 0.0),
            "confluence": r.get("confluence", 0.0),
            "vol_score": r.get("vol_score", 0.0),
            "range_score": r.get("range_score", 0.0),
            "range_position": r.get("range_position", 0.5),
        }
        r["peak_quality"] = _compute_peak_quality(
            r["symbol"], [], r.get("atr", 0.0),
            r.get("expected_pnl", 0.0),
            r.get("short_term_score", 0.0),
            r["regime"],
            features,
        )
        p = r["peak_12h"]
        if p >= 0.05:
            r["peak_label"] = "A"
        elif p >= 0.025:
            r["peak_label"] = "B"
        elif p >= 0.01:
            r["peak_label"] = "C"
        else:
            r["peak_label"] = "REJECT"

    return records


def main() -> None:
    logger.info("Loading windows...")
    windows = load_windows()
    if not windows:
        logger.error("No windows found.")
        sys.exit(1)
    logger.info(f"Loaded {len(windows)} windows")

    tasks: List[Tuple[Dict[str, Any], str]] = []
    for window in windows:
        for symbol in window.get("symbols", []):
            tasks.append((window, symbol))

    logger.info(f"Total symbol-window tasks: {len(tasks)}")

    status = _Status(len(tasks))
    status._write()

    all_records: List[Dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(analyze_symbol_window, window, symbol): (window, symbol)
            for window, symbol in tasks
        }
        for future in as_completed(futures):
            window, symbol = futures[future]
            try:
                records = future.result()
                all_records.extend(records)
                status.bump(len(records))
                logger.info(
                    f"Done {window['window_id']}/{symbol}: {len(records)} BUY signals"
                )
            except Exception as e:
                msg = f"{window['window_id']}/{symbol}: {e}"
                logger.error(msg)
                status.bump(0, error=msg)

    if all_records:
        logger.info(f"Enriching {len(all_records)} records...")
        all_records = _enrich_records(all_records)

    # Remove previous output to avoid stale data.
    if OUT_JSONL.exists():
        OUT_JSONL.unlink()

    with open(OUT_JSONL, "w", encoding="utf-8") as out_f:
        for rec in all_records:
            out_f.write(json.dumps(rec, default=str) + "\n")

    status.mark_completed()
    logger.info(f"Finished. Signals recorded: {status.signals}")


if __name__ == "__main__":
    main()
