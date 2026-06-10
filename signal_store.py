from datetime import datetime
from threading import Lock
from typing import Any, Dict, List, Optional
import logging

logger = logging.getLogger(__name__)

# In-memory fallback if database not available
_lock = Lock()
_signals: List[Dict[str, Any]] = _db_client = None


def set_db_client(db_client):
    """Set the database client for persistent storage."""
    global _db_client
    _db_client = db_client
    logger.info("Database client set for signal store")


def add_signal(signal: Dict[str, Any]) -> None:
    """Add a signal to storage (database or in-memory fallback)."""
    record = signal.copy()
    record["received_at"] = datetime.utcnow().isoformat() + "Z"
    
    if _db_client:
        try:
            query = """
                INSERT INTO market_data (symbol, data_type, data, timestamp)
                VALUES (%s, 'signal', %s, %s)
            """
            _db_client.execute_update(
                query,
                (record.get("symbol", ""), record, record["received_at"])
            )
            logger.debug(f"Signal saved to database: {record.get('symbol')}")
        except Exception as e:
            logger.error(f"Failed to save signal to database, using in-memory fallback: {e}")
            _add_signal_in_memory(record)
    else:
        _add_signal_in_memory(record)


def _add_signal_in_memory(signal: Dict[str, Any]) -> None:
    """Add signal to in-memory storage (fallback)."""
    with _lock:
        _signals.append(signal)


def get_signals() -> List[Dict[str, Any]]:
    """Get all signals from storage (database or in-memory fallback)."""
    if _db_client:
        try:
            query = """
                SELECT data FROM market_data 
                WHERE data_type = 'signal' 
                ORDER BY timestamp DESC 
                LIMIT 100
            """
            results = _db_client.execute_query(query)
            return [row["data"] for row in results]
        except Exception as e:
            logger.error(f"Failed to get signals from database, using in-memory fallback: {e}")
            return _get_signals_in_memory()
    else:
        return _get_signals_in_memory()


def _get_signals_in_memory() -> List[Dict[str, Any]]:
    """Get signals from in-memory storage (fallback)."""
    with _lock:
        return list(_signals)


def clear_signals() -> None:
    """Clear all signals from storage."""
    if _db_client:
        try:
            query = "DELETE FROM market_data WHERE data_type = 'signal'"
            _db_client.execute_update(query)
            logger.info("Signals cleared from database")
        except Exception as e:
            logger.error(f"Failed to clear signals from database: {e}")
    
    # Always clear in-memory as well
    with _lock:
        _signals.clear()
