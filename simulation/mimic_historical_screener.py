"""MIMIC — Historical pair screener.

Replaces the live `pair_screener.py` API calls with historical 24h ticker
snapshots downloaded by `mimic_download_external.py`.  Produces the same
regime-aware ranked symbol lists used in production.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any, Dict, List, Optional

import pandas as pd


# Mirrors pair_screener.py defaults
_VOLUME_TIER_A_THRESHOLD = 100_000_000.0  # $100M+
_VOLUME_TIER_B_THRESHOLD = 20_000_000.0   # $20M+
_MAX_SCREENED = 50


class HistoricalScreener:
    """Screen symbols using historical 24h ticker snapshots."""

    def __init__(self, ticker_dir: str = "historical_data/external"):
        self.ticker_dir = ticker_dir
        self._cache: Dict[str, pd.DataFrame] = {}

    def _load_day(self, date: datetime) -> pd.DataFrame:
        key = date.strftime("%Y%m%d")
        if key not in self._cache:
            historical_path = f"{self.ticker_dir}/ticker_24h_historical_{key}.parquet"
            path = historical_path if os.path.exists(historical_path) else f"{self.ticker_dir}/ticker_24h_{key}.parquet"
            try:
                self._cache[key] = pd.read_parquet(path)
            except Exception:
                self._cache[key] = pd.DataFrame()
        return self._cache[key]

    def screen(
        self,
        clock: datetime,
        regime: str,
        settings: Any,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return ranked symbols for the given regime at the simulated clock."""
        df = self._load_day(clock)
        if df.empty:
            return []

        # Pick snapshot nearest to clock (but not after)
        df["snapshot_time"] = pd.to_datetime(df["snapshot_time"], utc=True)
        past = df[df["snapshot_time"] <= clock]
        if past.empty:
            past = df
        latest = past.sort_values("snapshot_time").iloc[-1]["snapshot_time"]
        snapshot = past[past["snapshot_time"] == latest]

        # Exclude stablecoins and leveraged tokens (same filters as production)
        excluded_suffixes = ("UP", "DOWN", "BEAR", "BULL")
        excluded_stables = {"USDT", "BUSD", "USDC", "DAI", "TUSD"}
        rows = []
        for _, r in snapshot.iterrows():
            sym = str(r["symbol"])
            if not sym.endswith("USDT"):
                continue
            if sym.endswith(excluded_suffixes):
                continue
            asset = sym.replace("USDT", "")
            if asset in excluded_stables:
                continue
            qv = float(r.get("quote_volume", 0))
            rows.append({
                "symbol": sym,
                "quote_volume": qv,
                "price_change_percent": float(r.get("price_change_percent", 0)),
            })

        if not rows:
            return []

        # Apply volume tiers
        for row in rows:
            if row["quote_volume"] >= _VOLUME_TIER_A_THRESHOLD:
                row["tier"] = "A"
                row["tier_score"] = 3
            elif row["quote_volume"] >= _VOLUME_TIER_B_THRESHOLD:
                row["tier"] = "B"
                row["tier_score"] = 2
            else:
                row["tier"] = "C"
                row["tier_score"] = 1

        # Regime-specific scoring mirrors pair_screener.py
        regime = str(regime or "SPOT_BULL").upper()
        for row in rows:
            score = row["tier_score"] * 10
            chg = row["price_change_percent"]
            if regime == "SPOT_BULL":
                score += max(-5, min(5, chg * 0.3))
            elif regime == "RANGE_SCALP":
                score += 2 if abs(chg) < 3 else -1
            elif regime in ("PRESERVATION", "FUTURES_SHORT"):
                score -= max(-5, min(5, chg * 0.2))
            row["score"] = score

        rows.sort(key=lambda x: x["score"], reverse=True)
        cap = limit or _MAX_SCREENED
        return rows[:cap]


def load_historical_universe(
    start: datetime,
    end: datetime,
    regime: str,
    settings: Any,
    ticker_dir: str = "historical_data/external",
    top_n: int = 40,
) -> List[str]:
    """Build a static-ish universe from symbols that appear most often in the top N."""
    screener = HistoricalScreener(ticker_dir=ticker_dir)
    counts: Dict[str, int] = {}
    current = start
    while current <= end:
        for row in screener.screen(current, regime, settings, limit=top_n):
            sym = row["symbol"]
            counts[sym] = counts.get(sym, 0) + 1
        current += __import__("datetime").timedelta(days=1)
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [sym for sym, _ in ranked[:top_n]]
