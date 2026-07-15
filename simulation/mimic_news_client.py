"""MIMIC — Deterministic news/F&G client for simulations."""
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _load_fear_greed_series(data_dir: str = "historical_data") -> Dict[str, int]:
    """Load Fear & Greed historical parquet (if present) into a {YYYY-MM-DD: value} dict."""
    ext_dir = os.path.join(data_dir, "external")
    result: Dict[str, int] = {}
    if not os.path.isdir(ext_dir):
        return result
    for fname in os.listdir(ext_dir):
        if fname.startswith("fear_greed") and fname.endswith(".parquet"):
            try:
                import pandas as pd
                df = pd.read_parquet(os.path.join(ext_dir, fname))
                for _, row in df.iterrows():
                    date_str = str(row["date"])[:10]
                    result[date_str] = int(row["value"])
            except Exception:
                pass
    return result


class SimulationNewsClient:
    """Mock news client that returns a configurable F&G value and no trending symbols.

    If ``data_dir`` is provided and a fear_greed_*.parquet file exists under
    ``data_dir/external/``, F&G is served from the historical series keyed by
    date.  ``update_for_date()`` should be called each cycle to advance the
    current date.  Falls back to ``fear_greed_value`` when no historical data
    is available for the requested date.
    """

    def __init__(
        self,
        fear_greed_value: float = 50.0,
        trending: Optional[List[str]] = None,
        data_dir: Optional[str] = None,
    ):
        self.fear_greed_value = fear_greed_value
        self._default_fng = fear_greed_value
        self.trending = trending or []
        self._labels = [
            (0, 20, "Extreme Fear"),
            (20, 40, "Fear"),
            (40, 60, "Neutral"),
            (60, 80, "Greed"),
            (80, 100, "Extreme Greed"),
        ]
        self._fng_series: Dict[str, int] = {}
        if data_dir:
            self._fng_series = _load_fear_greed_series(data_dir)

    def update_for_date(self, dt: datetime) -> None:
        """Advance F&G value to match the historical date of the simulation clock."""
        if not self._fng_series:
            return
        date_str = dt.strftime("%Y-%m-%d")
        if date_str in self._fng_series:
            self.fear_greed_value = float(self._fng_series[date_str])
        else:
            self.fear_greed_value = self._default_fng

    def get_fear_greed(self) -> Dict[str, Any]:
        label = "Neutral"
        for lo, hi, lbl in self._labels:
            if lo <= self.fear_greed_value < hi:
                label = lbl
                break
        return {
            "value": self.fear_greed_value,
            "label": label,
            "updated_at": "",
            "source": "simulation",
        }

    def get_fear_greed_modifier(self) -> float:
        # Same mapping used in production (approximate)
        if self.fear_greed_value <= 15:
            return 0.15
        if self.fear_greed_value <= 25:
            return 0.08
        if self.fear_greed_value >= 75:
            return -0.06
        if self.fear_greed_value >= 85:
            return -0.12
        return 0.0

    def get_trending_symbols(self) -> List[str]:
        return self.trending

    def is_social_trending(self, symbol: str) -> bool:
        return symbol in self.trending

    def get_recent_news(self, symbols: List[str], since_hours: int = 12) -> List[Dict[str, Any]]:
        return []
