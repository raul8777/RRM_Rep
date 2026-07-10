"""MIMIC — Audit historical data and generate prioritized micro-universe queue."""
from __future__ import annotations
import os
import sys
import json
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Tuple

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo_root)

import pandas as pd
from simulation.mimic_universe_selector import MicroUniverseSelector, DEFAULT_UNIVERSE_SYMBOLS, DEFAULT_REGIMES


def _parse_manifests(data_dir: str) -> Dict[str, Any]:
    manifests: Dict[str, Any] = {}
    import glob
    for path in glob.glob(os.path.join(data_dir, "manifest_*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            manifests[os.path.basename(path)] = data
        except Exception:
            pass
    return manifests


def _audit_symbol(symbol: str, data_dir: str, interval: str = "1h") -> Dict[str, Any]:
    path = os.path.join(data_dir, f"{symbol}_{interval}.parquet")
    info = {
        "symbol": symbol,
        "file_exists": os.path.exists(path),
        "start": None,
        "end": None,
        "total_bars": 0,
        "gaps": 0,
        "nan_count": 0,
    }
    if not info["file_exists"]:
        return info
    try:
        df = pd.read_parquet(path)
        if "open_time" not in df.columns:
            return info
        df = df.sort_values("open_time").reset_index(drop=True)
        info["total_bars"] = len(df)
        # timestamps
        first = df["open_time"].iloc[0]
        last = df["open_time"].iloc[-1]
        if isinstance(first, str):
            first = datetime.fromisoformat(first)
            last = datetime.fromisoformat(last)
        elif isinstance(first, (int, float)):
            first = datetime.fromtimestamp(first / 1000.0, tz=timezone.utc)
            last = datetime.fromtimestamp(last / 1000.0, tz=timezone.utc)
        info["start"] = first.isoformat() if hasattr(first, "isoformat") else str(first)
        info["end"] = last.isoformat() if hasattr(last, "isoformat") else str(last)
        # gaps: expected 1h interval
        df["ts"] = pd.to_datetime(df["open_time"], utc=True, unit="ms" if pd.api.types.is_integer_dtype(df["open_time"]) else None)
        df = df.sort_values("ts")
        deltas = df["ts"].diff().dropna()
        info["gaps"] = int((deltas > pd.Timedelta(hours=1, minutes=5)).sum())
        info["nan_count"] = int(df.isna().sum().sum())
    except Exception as e:
        info["error"] = str(e)
    return info


def _detect_regime_months(btc_path: str) -> Dict[str, str]:
    """Very rough regime tagging per month based on BTC close trend."""
    regimes: Dict[str, str] = {}
    if not os.path.exists(btc_path):
        return regimes
    try:
        df = pd.read_parquet(btc_path)
        df["ts"] = pd.to_datetime(df["open_time"], utc=True, unit="ms" if pd.api.types.is_integer_dtype(df["open_time"]) else None)
        df = df.sort_values("ts").set_index("ts")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        monthly = df["close"].resample("ME").agg(["first", "last", "min", "max"])
        for month, row in monthly.iterrows():
            if pd.isna(row["first"]) or pd.isna(row["last"]):
                continue
            change = (row["last"] - row["first"]) / row["first"]
            if change > 0.05:
                label = "SPOT_BULL"
            elif change < -0.05:
                label = "PRESERVATION"
            else:
                label = "RANGE_SCALP"
            regimes[month.strftime("%Y-%m")] = label
    except Exception:
        pass
    return regimes


def main() -> int:
    data_dir = os.path.join(repo_root, "historical_data")
    os.makedirs(data_dir, exist_ok=True)
    benchmarks_dir = os.path.join(repo_root, "Dev", "benchmarks")
    os.makedirs(benchmarks_dir, exist_ok=True)

    symbols = DEFAULT_UNIVERSE_SYMBOLS
    audit: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": data_dir,
        "symbols": {},
        "manifests": _parse_manifests(data_dir),
        "regime_months": _detect_regime_months(os.path.join(data_dir, "BTCUSDT_1h.parquet")),
    }

    for symbol in symbols:
        audit["symbols"][symbol] = _audit_symbol(symbol, data_dir)

    # Coverage summary
    total_days = 0
    covered_symbols = 0
    for s, info in audit["symbols"].items():
        if info["file_exists"] and info["total_bars"] > 168:
            covered_symbols += 1
            if info.get("start") and info.get("end"):
                try:
                    start = datetime.fromisoformat(info["start"])
                    end = datetime.fromisoformat(info["end"])
                    total_days = max(total_days, (end - start).days)
                except Exception:
                    pass

    audit["summary"] = {
        "covered_symbols": covered_symbols,
        "total_symbols": len(symbols),
        "max_span_days": total_days,
    }

    out_path = os.path.join(benchmarks_dir, "mimic_data_audit.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(audit, f, indent=2, ensure_ascii=False)
    print(f"Audit saved to {out_path}")

    # Generate initial micro universe queue
    selector = MicroUniverseSelector(repo_root=repo_root)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=180)
    selector.generate_initial_queue(
        symbols=symbols,
        start_date=start.strftime("%Y-%m-%d"),
        end_date=end.strftime("%Y-%m-%d"),
        window_days=7,
        regimes=DEFAULT_REGIMES,
    )
    selector.compute_scores()
    print(f"Generated {len(selector.get_queue())} micro universes in queue.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
