"""
Error Collector Module

In-memory singleton that captures errors and warnings from all system layers
(DB, Binance, Trading Cycle, Position Manager, Analysis) for display in the dashboard.
"""

import threading
from collections import deque
from datetime import datetime, timedelta
from typing import Dict, List, Optional

# ── Source constants ────────────────────────────────────────────────────────
SOURCE_DB              = "DB"
SOURCE_BINANCE         = "BINANCE"
SOURCE_TRADING_CYCLE   = "TRADING_CYCLE"
SOURCE_POSITION_MANAGER = "POSITION_MANAGER"
SOURCE_ANALYSIS        = "ANALYSIS"
SOURCE_SYSTEM          = "SYSTEM"

# ── Level constants ─────────────────────────────────────────────────────────
LEVEL_ERROR   = "ERROR"
LEVEL_WARNING = "WARNING"
LEVEL_INFO    = "INFO"

_MAX_RECORDS = 200


class ErrorCollector:
    """Thread-safe in-memory store for system errors and warnings."""

    def __init__(self, maxlen: int = _MAX_RECORDS):
        self._records: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(
        self,
        source: str,
        level: str,
        message: str,
        context: Optional[Dict] = None
    ) -> None:
        record = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "source": source,
            "level": level,
            "message": str(message)[:500],
            "context": context or {},
        }
        with self._lock:
            self._records.append(record)

    def get_recent(self, limit: int = 50) -> List[Dict]:
        with self._lock:
            records = list(self._records)
        records.reverse()
        return records[:limit]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def get_summary(self, minutes: int = 5) -> Dict:
        """Count errors/warnings in the last N minutes."""
        cutoff = datetime.utcnow().replace(tzinfo=None) - timedelta(minutes=minutes)
        errors = 0
        warnings = 0
        with self._lock:
            for r in self._records:
                try:
                    ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    continue
                if ts >= cutoff:
                    if r["level"] == LEVEL_ERROR:
                        errors += 1
                    elif r["level"] == LEVEL_WARNING:
                        warnings += 1
        return {
            "errors_last_5min": errors,
            "warnings_last_5min": warnings,
            "total_records": len(self._records),
            "health": "error" if errors > 0 else ("warning" if warnings > 0 else "ok"),
        }


# ── Module-level singleton ──────────────────────────────────────────────────
_collector = ErrorCollector()


def add_error(source: str, level: str, message: str, context: Optional[Dict] = None) -> None:
    _collector.add(source, level, message, context)


def get_recent(limit: int = 50) -> List[Dict]:
    return _collector.get_recent(limit)


def clear_errors() -> None:
    _collector.clear()


def get_summary(minutes: int = 5) -> Dict:
    return _collector.get_summary(minutes)
