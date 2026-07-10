"""MIMIC — In-memory database client for simulations.

Implements the small subset of DB operations used by run_trades during a cycle.
Production overrides can be injected as rows so the simulation sees the same
runtime configuration as production would.
"""
from typing import Any, Dict, List, Optional


class SimulationDatabaseClient:
    """Minimal in-memory DB for simulation state and overrides."""

    def __init__(self, runtime_state: Optional[Dict[str, Any]] = None):
        self._tables: Dict[str, List[Dict[str, Any]]] = {}
        self._runtime_state: Dict[str, Any] = dict(runtime_state or {})

    def execute_query(self, sql: str, params: Optional[tuple] = None) -> List[Dict[str, Any]]:
        # Minimal parsing for the queries used by run_trades
        sql_lower = sql.lower()
        if "trading_runtime_state" in sql_lower and "where key=%s" in sql_lower:
            key = params[0] if params else None
            if key in self._runtime_state:
                return [{"key": key, "value": self._runtime_state[key]}]
            return []
        if "trades_open" in sql_lower or "trades_closed" in sql_lower:
            return []
        return []

    def execute_transaction(self, sql: Any, params: Optional[tuple] = None) -> Any:
        # Production signature: list of (query, params) tuples
        if isinstance(sql, list):
            return [self.execute_update(query, params) for query, params in sql]
        # Simulation mode: treat transactions as simple updates
        return self.execute_update(sql, params)

    def execute_update(self, sql: str, params: Optional[tuple] = None) -> int:
        # Insert/Update trading_runtime_state
        sql_lower = sql.lower()
        if "trading_runtime_state" in sql_lower and "on conflict" in sql_lower:
            key = params[0] if params else None
            value = params[1] if params and len(params) > 1 else None
            if key:
                self._runtime_state[key] = value
            return 1
        if "insert into trades_audit" in sql_lower:
            return 1
        if "insert into" in sql_lower and "trading_runtime_state" in sql_lower:
            key = params[0] if params else None
            value = params[1] if params and len(params) > 1 else None
            if key:
                self._runtime_state[key] = value
            return 1
        return 0

    def set_runtime_state(self, key: str, value: Any) -> None:
        self._runtime_state[key] = value

    def get_runtime_state(self, key: str) -> Any:
        return self._runtime_state.get(key)
