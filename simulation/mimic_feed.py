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
        numeric_time = pd.to_numeric(df["open_time"], errors="coerce")
        finite_time = numeric_time.dropna()
        if not finite_time.empty and float(finite_time.abs().median()) < 100_000_000 and interval in {"4h", "1d"}:
            source_path = self.data_dir / f"{symbol}_1h.parquet"
            if source_path.exists():
                source = pd.read_parquet(source_path)
                if pd.api.types.is_datetime64_any_dtype(source["open_time"]):
                    source["_timestamp"] = pd.to_datetime(source["open_time"], utc=True, errors="coerce")
                else:
                    source_numeric = pd.to_numeric(source["open_time"], errors="coerce")
                    source_unit = "ms" if float(source_numeric.dropna().abs().median()) >= 100_000_000_000 else "s"
                    source["_timestamp"] = pd.to_datetime(source_numeric, unit=source_unit, utc=True, errors="coerce")
                source = source.dropna(subset=["_timestamp"]).set_index("_timestamp")
                rule = "4h" if interval == "4h" else "1D"
                aggregate = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
                for optional in ("quote_volume", "trades"):
                    if optional in source.columns:
                        aggregate[optional] = "sum"
                df = source.resample(rule).agg(aggregate).dropna().reset_index().rename(columns={"_timestamp": "open_time"})
        df = df.sort_values("open_time")
        records = df.to_dict("records")
        # Normalize timestamps
        for r in records:
            ot = r.get("open_time")
            if isinstance(ot, datetime):
                stamp = pd.Timestamp(ot)
                if stamp.tzinfo is None:
                    stamp = stamp.tz_localize("UTC")
                else:
                    stamp = stamp.tz_convert("UTC")
                r["open_time"] = stamp.timestamp()
            elif isinstance(ot, str):
                r["open_time"] = datetime.fromisoformat(ot).replace(tzinfo=timezone.utc).timestamp()
            else:
                numeric = float(ot)
                r["open_time"] = numeric / 1000.0 if abs(numeric) >= 100_000_000_000 else numeric
        return records

    def get_klines(self, symbol: str, interval: str = "1h") -> List[Dict[str, Any]]:
        key = f"{symbol}_{interval}"
        if key not in self._cache:
            try:
                self._cache[key] = self._load_parquet(symbol, interval)
            except FileNotFoundError:
                self._cache[key] = self._resample_from_1m(symbol, interval)
        return self._cache[key]

    def _resample_from_1m(self, symbol: str, interval: str) -> List[Dict[str, Any]]:
        rules = {"5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h", "1d": "1D"}
        if interval not in rules:
            raise FileNotFoundError(f"No historical data found for {symbol} {interval}: {self.data_dir}")
        if pd is None:
            raise RuntimeError("pandas is required to resample historical data")

        source = pd.DataFrame(self.get_klines(symbol, "1m"))
        if source.empty:
            return []
        source["_timestamp"] = pd.to_datetime(source["open_time"], unit="s", utc=True)
        source = source.set_index("_timestamp").sort_index()
        if source.index.has_duplicates or (source.index.second != 0).any():
            raise ValueError(f"1m source timestamps are duplicated or not minute-aligned for {symbol}")

        aggregation = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        for optional in ("quote_volume", "trades"):
            if optional in source.columns:
                aggregation[optional] = "sum"
        rule = rules[interval]
        expected_rows = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}[interval]
        grouped = source.resample(rule, origin="start_day", label="left", closed="left")
        result = grouped.agg(aggregation)
        result = result.loc[grouped["close"].count() == expected_rows].dropna(subset=["open", "high", "low", "close"])
        result.index.name = "open_time"
        records = result.reset_index().to_dict("records")
        for row in records:
            row["open_time"] = pd.Timestamp(row["open_time"]).timestamp()
        return records

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
