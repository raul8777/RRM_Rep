"""Thread-safe cache for technical analysis results."""
from typing import Dict, Optional
from threading import Lock
from datetime import datetime


class AnalysisCache:
    """Stores technical analysis results with timestamps."""

    def __init__(self):
        self._cache: Dict[str, Dict] = {}
        self._lock = Lock()

    def set(self, symbol: str, indicators: Dict, technical_score: float) -> None:
        """Store indicators and score for a symbol."""
        with self._lock:
            self._cache[symbol] = {
                'indicators': indicators,
                'technical_score': technical_score,
                'timestamp': datetime.utcnow().isoformat(),
            }

    def get(self, symbol: str) -> Optional[Dict]:
        """Retrieve cached analysis for a symbol."""
        with self._lock:
            return self._cache.get(symbol, None)

    def get_all(self) -> Dict[str, Dict]:
        """Retrieve all cached analyses."""
        with self._lock:
            return dict(self._cache)

    def clear(self) -> None:
        """Clear entire cache."""
        with self._lock:
            self._cache.clear()


# Global singleton instance
analysis_cache = AnalysisCache()
