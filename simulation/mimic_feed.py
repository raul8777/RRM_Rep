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
        try:
            df = pd.read_parquet(path)
        except Exception as exc:
            # Some historical files may be intermittently corrupt/truncated.
            # Log the exact file and return empty data so callers can degrade
            # gracefully instead of aborting the whole sweep/simulation.
            import logging
            logging.getLogger(__name__).warning(
                "Failed to read parquet for %s %s (%s): %s", symbol, interval, path, exc
            )
            return []
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

    def resample_from_1h(
        self,
        symbol: str,
        target_minutes: int = 5,
    ) -> List[Dict[str, Any]]:
        """D4: Synthesise sub-hourly bars from 1h klines for SL/TP confirmations.

        Each 1h bar is split into ``60 // target_minutes`` sub-bars.  Price
        moves linearly from open to close; volume is distributed evenly.  The
        result is deterministic and consistent with the 1h OHLCV data already
        used by the simulation.

        Supported ``target_minutes``: 5, 15 (anything that divides 60 evenly).
        """
        assert 60 % target_minutes == 0, f"target_minutes must divide 60 evenly; got {target_minutes}"
        cache_key = f"{symbol}_{target_minutes}m_resampled"
        if cache_key in self._cache:
            return self._cache[cache_key]

        hourly = self.get_klines(symbol, "1h")
        n_slots = 60 // target_minutes
        slot_seconds = target_minutes * 60
        result: List[Dict[str, Any]] = []

        for bar in hourly:
            ot = float(bar.get("open_time", 0))
            o = float(bar.get("open", 0))
            h = float(bar.get("high", 0))
            lo = float(bar.get("low", 0))
            c = float(bar.get("close", 0))
            vol = float(bar.get("volume", 0))
            qv = float(bar.get("quote_volume", 0))
            trades = int(bar.get("trades", 0))

            for i in range(n_slots):
                frac_start = i / n_slots
                frac_end = (i + 1) / n_slots
                sub_open = o + (c - o) * frac_start
                sub_close = o + (c - o) * frac_end
                # High/low: blend with bar extremes weighted by position
                sub_high = max(sub_open, sub_close, lo + (h - lo) * frac_end)
                sub_low = min(sub_open, sub_close, h - (h - lo) * (1 - frac_start))
                result.append({
                    "open_time": ot + i * slot_seconds,
                    "open": sub_open,
                    "high": sub_high,
                    "low": sub_low,
                    "close": sub_close,
                    "volume": vol / n_slots,
                    "quote_volume": qv / n_slots,
                    "trades": trades // n_slots,
                })

        self._cache[cache_key] = result
        return result

    def get_klines_resampled(self, symbol: str, interval: str) -> List[Dict[str, Any]]:
        """D4: Return klines for the given interval, resampling from 1h when needed.

        Falls back to loading parquet directly for intervals that have their
        own parquet files (e.g. '1h', '4h', '1d').  For '5m' and '15m',
        synthesises from 1h data when no parquet is available.
        """
        try:
            return self.get_klines(symbol, interval)
        except FileNotFoundError:
            if interval == "5m":
                return self.resample_from_1h(symbol, target_minutes=5)
            if interval == "15m":
                return self.resample_from_1h(symbol, target_minutes=15)
            raise
