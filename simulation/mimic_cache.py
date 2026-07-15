"""MIMIC — Warm-start result cache backed by SQLite.

Stores (key → result) for completed sweep jobs so that re-runs with the same
window/profile/params combo skip the simulation entirely.  Key is a SHA-256
hash of the canonical JSON of (window_id, profile, params).

Usage::

    cache = MimicResultCache()
    key = cache.make_key(window_id, profile, params)
    cached = cache.get(key)
    if cached is None:
        result = _run_single_config(...)
        cache.put(key, result)
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Optional


_DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Dev", "benchmarks", "mimic_cache.db",
)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS sweep_cache (
    key       TEXT PRIMARY KEY,
    result    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

_PRUNE_OLD = """
DELETE FROM sweep_cache WHERE created_at < ?;
"""


class MimicResultCache:
    """SQLite-backed warm-start cache for sweep job results."""

    def __init__(self, db_path: Optional[str] = None, ttl_days: int = 30):
        self.db_path = db_path or _DEFAULT_DB_PATH
        self.ttl_days = ttl_days
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def _init_db(self) -> None:
        conn = self._connect()
        conn.execute(_CREATE_TABLE)
        conn.commit()

    @staticmethod
    def make_key(window_id: str, profile: str, params: Dict[str, Any]) -> str:
        """Return a stable SHA-256 hash key for the given job parameters."""
        payload = json.dumps(
            {"window_id": window_id, "profile": profile, "params": params},
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Return cached result for ``key``, or None if not found."""
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT result FROM sweep_cache WHERE key = ?", (key,)
            ).fetchone()
            if row:
                return json.loads(row[0])
        except Exception:
            pass
        return None

    def put(self, key: str, result: Dict[str, Any]) -> None:
        """Persist ``result`` under ``key``."""
        try:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO sweep_cache (key, result, created_at) VALUES (?, ?, ?)",
                (key, json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        except Exception:
            pass

    def prune(self) -> int:
        """Remove entries older than ``ttl_days``. Returns number of rows deleted."""
        try:
            from datetime import timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(days=self.ttl_days)).isoformat()
            conn = self._connect()
            cursor = conn.execute(_PRUNE_OLD, (cutoff,))
            conn.commit()
            return cursor.rowcount
        except Exception:
            return 0

    def invalidate(self, key: str) -> None:
        """Remove a single cache entry."""
        try:
            conn = self._connect()
            conn.execute("DELETE FROM sweep_cache WHERE key = ?", (key,))
            conn.commit()
        except Exception:
            pass

    def clear(self) -> None:
        """Wipe the entire cache."""
        try:
            conn = self._connect()
            conn.execute("DELETE FROM sweep_cache")
            conn.commit()
        except Exception:
            pass

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __del__(self) -> None:
        self.close()


_default_cache: Optional[MimicResultCache] = None


def get_cache(db_path: Optional[str] = None) -> MimicResultCache:
    """Return (or create) the process-global cache instance."""
    global _default_cache
    if _default_cache is None or (db_path and db_path != _default_cache.db_path):
        _default_cache = MimicResultCache(db_path=db_path)
    return _default_cache
