import json
from datetime import datetime
from threading import Lock
from typing import Any, Dict, List, Optional
import uuid
import logging
import error_collector as ec

logger = logging.getLogger(__name__)

# In-memory fallback if database not available
_lock = Lock()
_signals: List[Dict[str, Any]] = []
_db_client = None


def _expire_unprocessed_signals_on_startup(max_age_minutes: int = 90) -> int:
    """Mark stale unprocessed signals as processed to avoid replay after restart."""
    if not _db_client:
        return 0
    try:
        query = """
            UPDATE market_data
            SET data = jsonb_set(data, '{processed}', 'true')
            WHERE data_type = 'signal'
              AND COALESCE(data->>'processed', 'false') <> 'true'
              AND timestamp < NOW() - make_interval(mins => %s)
        """
        updated = _db_client.execute_update(query, (max_age_minutes,))
        if updated:
            logger.info(f"Startup replay-guard: expired {updated} stale signals (> {max_age_minutes} min)")
        return int(updated or 0)
    except Exception as e:
        logger.error(f"Startup replay-guard failed: {e}")
        ec.add_error(ec.SOURCE_DB, ec.LEVEL_WARNING, f"signal startup replay-guard failed: {e}")
        return 0


def set_db_client(db_client):
    """Set the database client for persistent storage."""
    global _db_client
    _db_client = db_client
    logger.info("Database client set for signal store")
    _expire_unprocessed_signals_on_startup(max_age_minutes=90)


def add_signal(signal: Dict[str, Any], strict_persistence: bool = False) -> Dict[str, Any]:
    """
    Add a signal to storage.

    Args:
        signal: Signal payload.
        strict_persistence: If True, fail when DB write fails instead of silently
            falling back to memory. Use this for external webhooks where ACK must
            imply durable persistence.

    Returns:
        Dict with persistence status and signal metadata.
    """
    record = signal.copy()
    record.setdefault("signal_id", str(uuid.uuid4()))
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
            return {
                "ok": True,
                "persisted": "db",
                "signal_id": record.get("signal_id"),
                "record": record,
            }
        except Exception as e:
            logger.error(f"Failed to save signal to database, using in-memory fallback: {e}")
            ec.add_error(ec.SOURCE_DB, ec.LEVEL_WARNING, f"signal add_signal DB error: {e}")
            if strict_persistence:
                return {
                    "ok": False,
                    "persisted": "none",
                    "signal_id": record.get("signal_id"),
                    "record": record,
                    "error": str(e),
                }
            _add_signal_in_memory(record)
            return {
                "ok": True,
                "persisted": "memory",
                "signal_id": record.get("signal_id"),
                "record": record,
            }
    else:
        _add_signal_in_memory(record)
        return {
            "ok": True,
            "persisted": "memory",
            "signal_id": record.get("signal_id"),
            "record": record,
        }


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
            if include_processed:
                query = """
                    SELECT data FROM market_data
                    WHERE data_type = 'signal'
                    ORDER BY timestamp DESC
                    LIMIT 100
                """
                results = _db_client.execute_query(query)
            else:
                query = """
                    SELECT data FROM market_data
                    WHERE data_type = 'signal'
                      AND COALESCE(data->>'processed', 'false') <> 'true'
                    ORDER BY timestamp DESC
                    LIMIT 100
                """
                results = _db_client.execute_query(query)
            signals = [row["data"] for row in results]
            if not include_processed:
                # Defense-in-depth for legacy rows or malformed payloads
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


def update_signal_decision(symbol: str, decision: str, reason: str, signal_id: Optional[str] = None) -> None:
    """
    Persist the trade decision for the most recent signal of a symbol.

    Args:
        symbol:   Trading pair symbol (e.g. "SOLUSDT")
        decision: "executed" | "rejected" | "pending"
        reason:   Short keyword visible on dashboard
                  e.g. "PNL_LOW", "NO_SALDO", "MAX_POSIC", "EJECUTADO", "QTY_INVALIDA"
    """
    # Mark as processed for any final decision (executed or rejected); keep pending unprocessed
    is_final = decision in ("executed", "rejected")
    update = {"decision": decision, "decision_reason": reason,
              "decision_at": datetime.utcnow().isoformat() + "Z",
              "processed": is_final}
    if _db_client:
        try:
            if signal_id:
                query = """
                    UPDATE market_data
                    SET data = data || %s::jsonb
                    WHERE ctid = (
                        SELECT ctid FROM market_data
                        WHERE data_type = 'signal'
                        AND data->>'symbol' = %s
                        AND data->>'signal_id' = %s
                        ORDER BY timestamp DESC
                        LIMIT 1
                    )
                """
                rows = _db_client.execute_update(query, (json.dumps(update), symbol, signal_id))
                if not rows:
                    # Backward-compatible fallback for older signals without signal_id.
                    query_fb = """
                        UPDATE market_data
                        SET data = data || %s::jsonb
                        WHERE ctid = (
                            SELECT ctid FROM market_data
                            WHERE data_type = 'signal'
                            AND data->>'symbol' = %s
                            ORDER BY timestamp DESC
                            LIMIT 1
                        )
                    """
                    _db_client.execute_update(query_fb, (json.dumps(update), symbol))
            else:
                query = """
                    UPDATE market_data
                    SET data = data || %s::jsonb
                    WHERE ctid = (
                        SELECT ctid FROM market_data
                        WHERE data_type = 'signal'
                        AND data->>'symbol' = %s
                        ORDER BY timestamp DESC
                        LIMIT 1
                    )
                """
                _db_client.execute_update(query, (json.dumps(update), symbol))
        except Exception as e:
            logger.error(f"update_signal_decision DB failed ({symbol}): {e}")
            ec.add_error(ec.SOURCE_DB, ec.LEVEL_WARNING, f"update_signal_decision failed ({symbol}): {e}")
            _update_signal_decision_in_memory(symbol, update)
    else:
        _update_signal_decision_in_memory(symbol, update)


def _update_signal_decision_in_memory(symbol: str, update: Dict) -> None:
    with _lock:
        for sig in reversed(_signals):
            if sig.get("symbol") == symbol:
                sig.update(update)
                break


def expire_stale_signals(max_age_minutes: int = 30) -> int:
    """
    Mark autonomous signals older than max_age_minutes with no final decision
    as processed=True. Returns count of expired signals.
    """
    expired = 0
    if _db_client:
        try:
            query = """
                UPDATE market_data
                SET data = jsonb_set(data, '{processed}', 'true')
                WHERE data_type = 'signal'
                AND (data->>'source' = 'autonomous' OR data->>'source' IS NULL)
                AND (data->>'decision' IS NULL OR data->>'decision' = 'pending')
                AND COALESCE((data->>'processed')::boolean, false) = false
                AND timestamp < NOW() - make_interval(mins => %s)
            """
            expired = _db_client.execute_update(query, (max_age_minutes,))
            if expired:
                logger.info(f"Expired {expired} stale signals older than {max_age_minutes}min")
        except Exception as e:
            logger.error(f"expire_stale_signals failed: {e}")
    else:
        cutoff = datetime.utcnow().isoformat()
        with _lock:
            from datetime import timedelta
            cutoff_dt = datetime.utcnow() - timedelta(minutes=max_age_minutes)
            for sig in _signals:
                if (sig.get('source') in ('autonomous', None)
                        and sig.get('decision') in (None, 'pending')
                        and not sig.get('processed')):
                    try:
                        ra = sig.get('received_at', '')
                        if ra and datetime.fromisoformat(ra.replace('Z', '')) < cutoff_dt:
                            sig['processed'] = True
                            expired += 1
                    except Exception:
                        pass
    return expired


def mark_signal_as_processed(symbol: str, signal_id: Optional[str] = None) -> None:
    """
    Mark a signal as processed to avoid reprocessing.
    
    Args:
        symbol: Trading symbol.
        signal_id: Optional unique identifier for deterministic selection.
    """
    if _db_client:
        try:
            if signal_id:
                query = """
                    UPDATE market_data
                    SET data = jsonb_set(data, '{processed}', 'true')
                    WHERE ctid = (
                        SELECT ctid FROM market_data
                        WHERE data_type = 'signal'
                        AND data->>'symbol' = %s
                        AND data->>'signal_id' = %s
                        ORDER BY timestamp DESC
                        LIMIT 1
                    )
                """
                rows = _db_client.execute_update(query, (symbol, signal_id))
                if not rows:
                    signal_id = None  # fallback to symbol-only path

            if not signal_id:
                # Prefer marking the most recent external/non-autonomous signal for this symbol.
                query = """
                    UPDATE market_data
                    SET data = jsonb_set(data, '{processed}', 'true')
                    WHERE ctid = (
                        SELECT ctid FROM market_data
                        WHERE data_type = 'signal'
                        AND data->>'symbol' = %s
                        AND (data->>'source' IS NULL OR data->>'source' != 'autonomous')
                        ORDER BY timestamp DESC
                        LIMIT 1
                    )
                """
                rows = _db_client.execute_update(query, (symbol,))
                if not rows:
                    # fallback: mark most recent signal regardless of source
                    query_fb = """
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
                    _db_client.execute_update(query_fb, (symbol,))
            logger.info(f"Signal marked as processed: {symbol} ({signal_id or 'symbol-fallback'})")
        except Exception as e:
            logger.error(f"Failed to mark signal as processed in database: {e}")
            ec.add_error(ec.SOURCE_DB, ec.LEVEL_ERROR, f"mark_signal_as_processed failed ({symbol}): {e}")
    
    # Also mark in memory
    with _lock:
        for signal in _signals:
            if signal.get("symbol") == symbol:
                signal["processed"] = True
                break
