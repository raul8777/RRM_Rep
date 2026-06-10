"""
Database Client Module

Handles PostgreSQL database connections and operations with connection pooling.
"""

import logging
from typing import Dict, List, Optional, Any
from contextlib import contextmanager
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)


class DatabaseClient:
    """PostgreSQL database client with connection pooling."""
    
    def __init__(self, db_url: str, min_connections: int = 1, max_connections: int = 10):
        """
        Initialize database client with connection pool.
        
        Args:
            db_url: PostgreSQL connection URL
            min_connections: Minimum number of connections in pool
            max_connections: Maximum number of connections in pool
        """
        self.db_url = db_url
        self.min_connections = min_connections
        self.max_connections = max_connections
        self.connection_pool = None
        
        self._initialize_pool()
    
    def _initialize_pool(self):
        """Initialize the connection pool."""
        try:
            self.connection_pool = pool.SimpleConnectionPool(
                self.min_connections,
                self.max_connections,
                self.db_url
            )
            logger.info(f"Database connection pool initialized (min={self.min_connections}, max={self.max_connections})")
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
            yield conn
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
    
    def execute_batch(self, query: str, params_list: List[tuple]) -> int:
        """
        Execute a batch of INSERT/UPDATE/DELETE operations.
        
        Args:
            query: SQL query string
            params_list: List of parameter tuples
            
        Returns:
            Total number of affected rows
        """
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.executemany(query, params_list)
                total_affected = cursor.rowcount
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
