"""MIMIC — Deterministic news/F&G client for simulations."""
from typing import Any, Dict, List, Optional


class SimulationNewsClient:
    """Mock news client that returns a configurable F&G value and no trending symbols."""

    def __init__(self, fear_greed_value: float = 50.0, trending: Optional[List[str]] = None):
        self.fear_greed_value = fear_greed_value
        self.trending = trending or []
        self._labels = [
            (0, 20, "Extreme Fear"),
            (20, 40, "Fear"),
            (40, 60, "Neutral"),
            (60, 80, "Greed"),
            (80, 100, "Extreme Greed"),
        ]

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
