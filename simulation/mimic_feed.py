"""MIMIC — Historical data feed for deterministic market replay."""
from __future__ import annotations
import os
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
from pathlib import Path

try:
    import pandas as pd
except Exception:
    pd = None


class HistoricalDataFeed:
    """Loads klines from parquet and serves them to simulation clients."""

    def __init__(self, data_dir: str = "historical_data"):
        self.data_dir = Path(data_dir)
        self._cache: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}

    def _load_parquet(self, symbol: str, interval: str) -> List[Dict[str, Any]]:
        path = self.data_dir / f"{symbol}_{interval}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"No historical data found for {symbol} {interval}: {path}")
        if pd is None:
            raise RuntimeError("pandas is required to load parquet historical data")
        df = pd.read_parquet(path)
        df = df.sort_values("open_time")
        records = df.to_dict("records")
        # Normalize timestamps
        for r in records:
            ot = r.get("open_time")
            if isinstance(ot, datetime):
                r["open_time"] = ot.replace(tzinfo=timezone.utc).timestamp()
            elif isinstance(ot, str):
                r["open_time"] = datetime.fromisoformat(ot).replace(tzinfo=timezone.utc).timestamp()
            else:
                r["open_time"] = float(ot) / 1000.0
        return records

    def get_klines(self, symbol: str, interval: str = "1h") -> List[Dict[str, Any]]:
        key = f"{symbol}_{interval}"
        if key not in self._cache:
            self._cache[key] = self._load_parquet(symbol, interval)
        return self._cache[key]

    def load_symbols(self, symbols: List[str], interval: str = "1h") -> None:
        for symbol in symbols:
            self.get_klines(symbol, interval)

    def get_price_at(self, symbol: str, interval: str, timestamp: datetime) -> float:
        bars = self.get_klines(symbol, interval)
        ts = timestamp.timestamp()
        bar = None
        for b in bars:
            if b.get("open_time", 0) <= ts:
                bar = b
        if bar is None:
            raise RuntimeError(f"No price for {symbol} at {timestamp.isoformat()}")
        return float(bar.get("close", 0))
