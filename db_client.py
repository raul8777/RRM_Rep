"""
Database Client Module

Handles PostgreSQL database connections and operations with connection pooling.
"""

import logging
from typing import Dict, List, Optional, Any
from contextlib import contextmanager
import psycopg2
from psycopg2 import pool, OperationalError
from psycopg2.extras import RealDictCursor, execute_batch as pg_execute_batch

logger = logging.getLogger(__name__)


class DatabaseClient:
    """PostgreSQL database client with threaded connection pooling."""
    
    def __init__(self, db_url: str, min_connections: int = 2, max_connections: int = 10,
                 statement_timeout_ms: int = 5000):
        """
        Initialize database client with connection pool.
        
        Args:
            db_url: PostgreSQL connection URL
            min_connections: Minimum connections to keep open
            max_connections: Maximum simultaneous connections
            statement_timeout_ms: Per-query timeout in milliseconds
        """
        self.db_url = db_url
        self.min_connections = min_connections
        self.max_connections = max_connections
        self.statement_timeout_ms = statement_timeout_ms
        self.connection_pool = None
        
        self._initialize_pool()
    
    def _initialize_pool(self):
        """Initialize the threaded connection pool."""
        try:
            self.connection_pool = pool.ThreadedConnectionPool(
                self.min_connections,
                self.max_connections,
                self.db_url,
                options=f"-c statement_timeout={self.statement_timeout_ms}",
            )
            logger.info(
                f"DB ThreadedConnectionPool initialized "
                f"(min={self.min_connections}, max={self.max_connections}, "
                f"timeout={self.statement_timeout_ms}ms)"
            )
        except Exception as e:
            logger.error(f"Failed to initialize database connection pool: {e}")
            raise
    
    @contextmanager
    def get_connection(self):
        """
        Context manager for getting a connection from the pool.
        
        Yields:
            psycopg2 connection object
        """
        if not self.connection_pool:
            raise RuntimeError("Connection pool not initialized")
        
        conn = None
        try:
            conn = self.connection_pool.getconn()
            # Reconnect if the connection was dropped
            if conn.closed:
                self.connection_pool.putconn(conn, close=True)
                conn = self.connection_pool.getconn()
            yield conn
        except (OperationalError, psycopg2.InterfaceError) as e:
            logger.warning(f"DB connection error, will retry on next call: {e}")
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
                self.connection_pool.putconn(conn, close=True)
                conn = None
            raise
        except Exception as e:
            logger.error(f"Error getting connection from pool: {e}")
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                self.connection_pool.putconn(conn)
    
    def execute_query(self, query: str, params: Optional[tuple] = None) -> List[Dict[str, Any]]:
        """
        Execute a SELECT query and return results.
        
        Args:
            query: SQL query string
            params: Query parameters
            
        Returns:
            List of dictionaries representing rows
        """
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(query, params or ())
                results = cursor.fetchall()
                return [dict(row) for row in results]
    
    def execute_update(self, query: str, params: Optional[tuple] = None) -> int:
        """
        Execute an INSERT, UPDATE, or DELETE query.
        
        Args:
            query: SQL query string
            params: Query parameters
            
        Returns:
            Number of affected rows
        """
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(query, params or ())
                affected = cursor.rowcount
                conn.commit()
                return affected
    
    def execute_batch(self, query: str, params_list: List[tuple], page_size: int = 100) -> int:
        """
        Execute a batch of INSERT/UPDATE/DELETE operations using psycopg2.extras.execute_batch.
        
        Args:
            query: SQL query string
            params_list: List of parameter tuples
            page_size: Number of rows per round-trip
            
        Returns:
            Total number of affected rows
        """
        if not params_list:
            return 0
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                pg_execute_batch(cursor, query, params_list, page_size=page_size)
                total_affected = len(params_list)
                conn.commit()
                return total_affected
    
    def test_connection(self) -> bool:
        """
        Test database connection.
        
        Returns:
            True if connection successful, False otherwise
        """
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    result = cursor.fetchone()
                    return result[0] == 1
        except Exception as e:
            logger.error(f"Database connection test failed: {e}")
            return False
    
    def close_all(self):
        """Close all connections in the pool."""
        if self.connection_pool:
            self.connection_pool.closeall()
            logger.info("Database connection pool closed")


def get_db_client(db_url: str) -> DatabaseClient:
    """
    Factory function to get a database client instance.
    
    Args:
        db_url: PostgreSQL connection URL
        
    Returns:
        DatabaseClient instance
    """
    return DatabaseClient(db_url)
