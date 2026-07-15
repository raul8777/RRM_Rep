"""MIMIC — In-memory database client for simulations.

Implements the small subset of DB operations used by run_trades during a cycle.
Production overrides can be injected as rows so the simulation sees the same
runtime configuration as production would.
"""
import json
import os
from typing import Any, Dict, List, Optional


MODES = ["SPOT_BULL", "RANGE_SCALP", "PRESERVATION", "FUTURES_SHORT"]
PROFILES = ["A", "B", "C"]


def _default_selector_config() -> Dict[str, Any]:
    return {
        "smart_total_enabled": True,
        "mode_selector_enabled": True,
        "mode_require_confirmed": True,
        "mode_min_hold_cycles": 2,
        "mode_min_confidence_delta": 0.03,
        "mode_audit_every_cycle": True,
        "profile_selector_enabled": True,
        "audit_every_cycle": True,
        "min_hold_cycles": 3,
        "min_score_delta": 0.05,
        "profile_min_hold_cycles": 3,
        "profile_min_score_delta": 0.05,
        "profile_rank": {"A": 0.0, "B": 0.0, "C": 0.0},
    }


def _load_indicator_weights(repo_root: Optional[str] = None) -> Dict[str, Any]:
    """D19: Load indicator_weights from the last learning engine snapshot.

    Looks for ``indicator_weights_snapshot.json`` in Dev/benchmarks/.  Falls
    back to empty dict so the simulation degrades gracefully when no snapshot
    is present.
    """
    root = repo_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(root, "Dev", "benchmarks", "indicator_weights_snapshot.json"),
        os.path.join(root, "Dev", "benchmarks", "mimic_indicator_weights.json"),
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return {}


def _profile_rank_from_weights(weights: Dict[str, Any]) -> Dict[str, float]:
    """D9: Derive profile A/B/C scores from indicator_weights global entry.

    Profile A (aggressive) benefits from high vol_spike and trend_4h weights.
    Profile C (conservative) benefits from high rsi and macd weights.
    Profile B is the balanced baseline.
    Returns a dict with relative scores used by the production profile selector.
    """
    global_w = weights.get("__global__", {})
    if not global_w:
        return {"A": 0.0, "B": 0.0, "C": 0.0}
    vol_spike = float(global_w.get("vol_spike", 0.15))
    trend_4h = float(global_w.get("trend_4h", 0.15))
    rsi = float(global_w.get("rsi", 0.12))
    macd = float(global_w.get("macd", 0.10))
    # Profile A benefits when momentum indicators dominate
    score_a = (vol_spike + trend_4h) * 0.5
    # Profile C benefits when mean-reversion indicators dominate
    score_c = (rsi + macd) * 0.5
    # Profile B is always slightly below whichever leads
    score_b = (score_a + score_c) * 0.5
    # Normalise to [-0.1, +0.1] range expected by production selector
    base = max(score_a, score_b, score_c, 0.001)
    return {
        "A": round((score_a - score_b) / base * 0.1, 4),
        "B": 0.0,
        "C": round((score_c - score_b) / base * 0.1, 4),
    }


class SimulationDatabaseClient:
    """Minimal in-memory DB for simulation state and overrides."""

    def __init__(
        self,
        runtime_state: Optional[Dict[str, Any]] = None,
        forced_profile: Optional[str] = None,
        profile_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
        use_indicator_weights: bool = True,
    ):
        self._tables: Dict[str, List[Dict[str, Any]]] = {}
        self._runtime_state: Dict[str, Any] = dict(runtime_state or {})
        # D19: Load historical indicator_weights for meta-learning
        self._indicator_weights: Dict[str, Any] = {}
        if use_indicator_weights:
            self._indicator_weights = _load_indicator_weights()
        self._init_regime_state(forced_profile, profile_overrides)

    def _init_regime_state(
        self,
        forced_profile: Optional[str] = None,
        profile_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        """Initialize trading_runtime_state keys used by production regime logic."""
        # Regime selector config
        if "regime_profile_selector_config" not in self._runtime_state:
            cfg = _default_selector_config()
            # D9: Seed profile_rank from historical indicator_weights if available
            if self._indicator_weights:
                cfg["profile_rank"] = _profile_rank_from_weights(self._indicator_weights)
            self._runtime_state["regime_profile_selector_config"] = cfg

        # Active mode / profile per regime
        for mode in MODES:
            active_key = f"regime_profile_active_{mode}"
            if active_key not in self._runtime_state:
                profile = forced_profile or "B"
                self._runtime_state[active_key] = {
                    "profile": profile,
                    "label": profile,
                    "updated_at": None,
                }

            # Optional per-mode/per-profile parameter overrides for sweep experiments
            for profile in PROFILES:
                override_key = f"regime_profile_{mode}_{profile}"
                if override_key not in self._runtime_state:
                    overrides = (profile_overrides or {}).get(f"{mode}_{profile}", {})
                    if overrides:
                        self._runtime_state[override_key] = {"params": overrides}

        # Smart Total selector persisted state
        if "smart_total_active_mode" not in self._runtime_state:
            self._runtime_state["smart_total_active_mode"] = {
                "mode": "SPOT_BULL",
                "reason": "simulation_initialization",
            }
        if "smart_total_mode_selector_state" not in self._runtime_state:
            self._runtime_state["smart_total_mode_selector_state"] = {
                "last_selected": "SPOT_BULL",
                "last_selected_score": 0.0,
                "cycles_since_switch": 999,
            }

    def inject_profile_override(
        self,
        mode: str,
        profile: str,
        params: Dict[str, Any],
    ) -> None:
        """Inject hot-config-like overrides for a specific mode/profile pair."""
        key = f"regime_profile_{mode.upper()}_{profile.upper()}"
        self._runtime_state[key] = {"params": dict(params)}

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

    def get_indicator_weights(self, symbol: str = "__global__") -> Dict[str, Any]:
        """D19: Return indicator weights for ``symbol`` (falls back to __global__)."""
        return self._indicator_weights.get(symbol) or self._indicator_weights.get("__global__") or {}
