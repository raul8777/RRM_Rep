"""Thread-safe cache for technical analysis results."""
from typing import Dict, Optional
from threading import Lock
from datetime import datetime
import logging
import json

logger = logging.getLogger(__name__)


class AnalysisCache:
    """Stores technical analysis results with timestamps (database or in-memory fallback)."""

    def __init__(self):
        self._cache: Dict[str, Dict] = {}
        self._lock = Lock()
        self._db_client = None

    def set_db_client(self, db_client):
        """Set the database client for persistent storage."""
        self._db_client = db_client
        logger.info("Database client set for analysis cache")

    def set(self, symbol: str, indicators: Dict, technical_score: float) -> None:
        """Store indicators and score for a symbol."""
        data = {
            'indicators': indicators,
            'technical_score': technical_score,
            'timestamp': datetime.utcnow().isoformat(),
        }
        
        if self._db_client:
            try:
                data_json = json.dumps(data)
                updated = self._db_client.execute_update(
                    """UPDATE market_data SET data = %s, timestamp = %s
                       WHERE symbol = %s AND data_type = 'technical'""",
                    (data_json, data['timestamp'], symbol),
                )
                if not updated:
                    self._db_client.execute_update(
                        """INSERT INTO market_data (symbol, data_type, data, timestamp)
                           VALUES (%s, 'technical', %s, %s)""",
                        (symbol, data_json, data['timestamp']),
                    )
                logger.debug(f"Technical analysis saved to database: {symbol}")
            except Exception as e:
                logger.error(f"Failed to save analysis to database, using in-memory fallback: {e}")
                self._set_in_memory(symbol, data)
        else:
            self._set_in_memory(symbol, data)

    def _set_in_memory(self, symbol: str, data: Dict) -> None:
        """Store analysis in in-memory cache (fallback)."""
        with self._lock:
            self._cache[symbol] = data

    def get(self, symbol: str) -> Optional[Dict]:
        """Retrieve cached analysis for a symbol."""
        if self._db_client:
            try:
                query = """
                    SELECT data FROM market_data 
                    WHERE symbol = %s AND data_type = 'technical'
                    ORDER BY timestamp DESC 
                    LIMIT 1
                """
                results = self._db_client.execute_query(query, (symbol,))
                if results:
                    data = results[0]['data']
                    if isinstance(data, str):
                        return json.loads(data)
                    return data
            except Exception as e:
                logger.error(f"Failed to get analysis from database, using in-memory fallback: {e}")
        
        return self._get_in_memory(symbol)

    def _get_in_memory(self, symbol: str) -> Optional[Dict]:
        """Retrieve analysis from in-memory cache (fallback)."""
        with self._lock:
            return self._cache.get(symbol, None)

    def get_all(self) -> Dict[str, Dict]:
        """Retrieve all cached analyses."""
        if self._db_client:
            try:
                query = """
                    SELECT symbol, data FROM market_data 
                    WHERE data_type = 'technical'
                    ORDER BY timestamp DESC
                """
                results = self._db_client.execute_query(query)
                result_dict = {}
                for row in results:
                    data = row['data']
                    if isinstance(data, str):
                        data = json.loads(data)
                    result_dict[row['symbol']] = data
                return result_dict
            except Exception as e:
                logger.error(f"Failed to get all analyses from database, using in-memory fallback: {e}")
        
        return self._get_all_in_memory()

    def _get_all_in_memory(self) -> Dict[str, Dict]:
        """Retrieve all analyses from in-memory cache (fallback)."""
        with self._lock:
            return dict(self._cache)

    def clear(self) -> None:
        """Clear entire cache."""
        if self._db_client:
            try:
                query = "DELETE FROM market_data WHERE data_type = 'technical'"
                self._db_client.execute_update(query)
                logger.info("Technical analyses cleared from database")
            except Exception as e:
                logger.error(f"Failed to clear analyses from database: {e}")
        
        with self._lock:
            self._cache.clear()


# Global singleton instance
analysis_cache = AnalysisCache()
