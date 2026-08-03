"""
Daily PnL Tracker (P1.1 — Master Plan 863057)

Provides thread-safe daily realized PnL totals for SSO-4 (Target Contribution).
Previously, today_realized_pnl was hardcoded to 0.0 for both SPOT and FUTURES,
causing SSO-4 to never fire correctly.

Design:
- Single Lock shared across both trade_type values (futures/spot)
- TTL=5min cache — max 12 BD queries/hour total
- Date-aware cache — auto-invalidates at midnight UTC
- Restart-safe: reads from trades_closed in BD → 100% trazabilidad
- Thread-safe: Lock protects all reads/writes to _cache dict
"""
import threading
import time
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Module-level thread-safe cache ────────────────────────────────────────────
_cache_lock = threading.Lock()
_cache: dict = {
    "futures": 0.0,
    "spot":    0.0,
    "ts":      0.0,        # monotonic timestamp of last refresh
    "date":    "",         # UTC date string "YYYY-MM-DD" — invalidates at midnight
}
_CACHE_TTL_SECONDS = 300   # 5 minutes — max 12 queries/hour per type


def get_daily_realized_pnl(db_client, trade_type: str = "futures") -> float:
    """
    Return today's realized PnL (UTC) for the given trade_type ("futures" or "spot").

    Thread-safe: uses module-level Lock.
    Restart-safe: queries trades_closed in BD (not RAM).
    Cache TTL=5min; auto-invalidates at UTC midnight.

    Args:
        db_client: DatabaseClient instance (must be set before calling)
        trade_type: "futures" or "spot"

    Returns:
        float: Sum of pnl from trades_closed WHERE exit_time >= CURRENT_DATE
               and metadata->>'trade_type' = trade_type
               Returns 0.0 on any error (fail-open — don't block SSO).
    """
    if db_client is None:
        return 0.0

    now_mono = time.monotonic()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with _cache_lock:
        # Cache hit: same day and within TTL
        if (_cache["date"] == today_str and
                now_mono - _cache["ts"] < _CACHE_TTL_SECONDS):
            val = _cache.get(trade_type, 0.0)
            logger.debug(f"[DailyPnL] cache hit {trade_type}=${val:.2f} (age={now_mono - _cache['ts']:.0f}s)")
            return val

        # Cache miss or day rollover — query BD for both types in one pass
        try:
            rows_futures = db_client.execute_query(
                "SELECT COALESCE(SUM(pnl), 0.0) AS total FROM trades_closed "
                "WHERE exit_time >= CURRENT_DATE "
                "AND metadata->>'trade_type' = 'futures' "
                "AND COALESCE(is_corrupted, FALSE) = FALSE",
                ()
            )
            rows_spot = db_client.execute_query(
                "SELECT COALESCE(SUM(pnl), 0.0) AS total FROM trades_closed "
                "WHERE exit_time >= CURRENT_DATE "
                "AND metadata->>'trade_type' = 'spot' "
                "AND COALESCE(is_corrupted, FALSE) = FALSE",
                ()
            )
            futures_val = float(rows_futures[0]["total"]) if rows_futures else 0.0
            spot_val    = float(rows_spot[0]["total"])    if rows_spot    else 0.0
            _cache["futures"] = futures_val
            _cache["spot"]    = spot_val
            _cache["ts"]      = now_mono
            _cache["date"]    = today_str
            logger.info(
                f"[DailyPnL] refreshed — futures=${futures_val:.2f} spot=${spot_val:.2f} "
                f"(date={today_str})"
            )
            return _cache.get(trade_type, 0.0)
        except Exception as e:
            logger.warning(f"[DailyPnL] BD query failed ({trade_type}): {e} — returning 0.0")
            return 0.0


def invalidate_cache() -> None:
    """Force cache refresh on next call (e.g. after a trade closes)."""
    with _cache_lock:
        _cache["ts"] = 0.0
        _cache["date"] = ""
    logger.debug("[DailyPnL] cache invalidated (force refresh)")
