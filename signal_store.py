import json
from datetime import datetime
from threading import Lock
from typing import Any, Dict, List, Optional
import logging
import error_collector as ec

logger = logging.getLogger(__name__)

# In-memory fallback if database not available
_lock = Lock()
_signals: List[Dict[str, Any]] = []
_db_client = None


def set_db_client(db_client):
    """Set the database client for persistent storage."""
    global _db_client
    _db_client = db_client
    logger.info("Database client set for signal store")


def add_signal(signal: Dict[str, Any]) -> None:
    """Add a signal to storage (database or in-memory fallback)."""
    record = signal.copy()
    record["received_at"] = datetime.utcnow().isoformat() + "Z"
    record["processed"] = False  # Mark as not processed initially
    
    if _db_client:
        try:
            query = """
                INSERT INTO market_data (symbol, data_type, data, timestamp)
                VALUES (%s, 'signal', %s, %s)
            """
            _db_client.execute_update(
                query,
                (record.get("symbol", ""), json.dumps(record), record["received_at"])
            )
            logger.debug(f"Signal saved to database: {record.get('symbol')}")
        except Exception as e:
            logger.error(f"Failed to save signal to database, using in-memory fallback: {e}")
            ec.add_error(ec.SOURCE_DB, ec.LEVEL_WARNING, f"signal add_signal DB error: {e}")
            _add_signal_in_memory(record)
    else:
        _add_signal_in_memory(record)


def _add_signal_in_memory(signal: Dict[str, Any]) -> None:
    """Add signal to in-memory storage (fallback)."""
    with _lock:
        _signals.append(signal)


def get_signals(include_processed: bool = False) -> List[Dict[str, Any]]:
    """
    Get signals from storage (database or in-memory fallback).
    
    Args:
        include_processed: If True, include processed signals. If False, only return unprocessed signals.
    
    Returns:
        List of signal dictionaries
    """
    if _db_client:
        try:
            query = """
                SELECT data FROM market_data 
                WHERE data_type = 'signal' 
                ORDER BY timestamp DESC 
                LIMIT 100
            """
            results = _db_client.execute_query(query)
            signals = [row["data"] for row in results]
            
            # Filter out processed signals if requested
            if not include_processed:
                signals = [s for s in signals if not s.get("processed", False)]
            
            return signals
        except Exception as e:
            logger.error(f"Failed to get signals from database, using in-memory fallback: {e}")
            ec.add_error(ec.SOURCE_DB, ec.LEVEL_WARNING, f"signal get_signals DB error: {e}")
            return _get_signals_in_memory(include_processed)
    else:
        return _get_signals_in_memory(include_processed)


def _get_signals_in_memory(include_processed: bool = False) -> List[Dict[str, Any]]:
    """Get signals from in-memory storage (fallback)."""
    with _lock:
        signals = list(_signals)
        if not include_processed:
            signals = [s for s in signals if not s.get("processed", False)]
        return signals


def get_recent_signals(limit: int = 20) -> List[Dict[str, Any]]:
    """
    Get the most recent N signals (both BUY and SELL, processed or not).
    Used for the dashboard 'Recent Signals' view.
    """
    if _db_client:
        try:
            query = """
                SELECT data FROM market_data
                WHERE data_type = 'signal'
                ORDER BY timestamp DESC
                LIMIT %s
            """
            results = _db_client.execute_query(query, (limit,))
            return [row["data"] for row in results]
        except Exception as e:
            logger.error(f"Failed to get recent signals from DB: {e}")
            ec.add_error(ec.SOURCE_DB, ec.LEVEL_WARNING, f"signal get_recent DB error: {e}")
            return _get_recent_signals_in_memory(limit)
    return _get_recent_signals_in_memory(limit)


def _get_recent_signals_in_memory(limit: int = 20) -> List[Dict[str, Any]]:
    with _lock:
        return list(reversed(_signals[-limit:]))


def clear_signals() -> None:
    """Clear all signals from storage."""
    if _db_client:
        try:
            query = "DELETE FROM market_data WHERE data_type = 'signal'"
            _db_client.execute_update(query)
            logger.info("Signals cleared from database")
        except Exception as e:
            logger.error(f"Failed to clear signals from database: {e}")
            ec.add_error(ec.SOURCE_DB, ec.LEVEL_WARNING, f"signal clear_signals DB error: {e}")
    
    # Always clear in-memory as well
    with _lock:
        _signals.clear()


def mark_signal_as_processed(signal_id: str) -> None:
    """
    Mark a signal as processed to avoid reprocessing.
    
    Args:
        signal_id: Unique identifier for the signal (e.g., symbol + timestamp)
    """
    if _db_client:
        try:
            # PostgreSQL does not support ORDER BY / LIMIT in UPDATE — use ctid subquery
            query = """
                UPDATE market_data
                SET data = jsonb_set(data, '{processed}', 'true')
                WHERE ctid = (
                    SELECT ctid FROM market_data
                    WHERE data_type = 'signal'
                    AND data->>'symbol' = %s
                    ORDER BY timestamp DESC
                    LIMIT 1
                )
            """
            _db_client.execute_update(query, (signal_id,))
            logger.info(f"Signal marked as processed: {signal_id}")
        except Exception as e:
            logger.error(f"Failed to mark signal as processed in database: {e}")
            ec.add_error(ec.SOURCE_DB, ec.LEVEL_ERROR, f"mark_signal_as_processed failed ({signal_id}): {e}")
    
    # Also mark in memory
    with _lock:
        for signal in _signals:
            if signal.get("symbol") == signal_id:
                signal["processed"] = True
                break
