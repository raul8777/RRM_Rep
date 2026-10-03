"""MIMIC — In-memory database client for simulations.

Implements the small subset of DB operations used by run_trades during a cycle.
Production overrides can be injected as rows so the simulation sees the same
runtime configuration as production would.
"""
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional


class _StagedTables:
    """In-memory implementation of ready_candidates and ready_candidate_checks for simulations."""

    def __init__(self) -> None:
        self.ready: List[Dict[str, Any]] = []
        self.checks: List[Dict[str, Any]] = []
        self._ready_id = 0
        self._check_id = 0

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _json(self, value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:
                return value
        return value

    def query(self, sql: str, params: Optional[tuple] = None) -> List[Dict[str, Any]]:
        params = params or ()
        s = sql.strip()
        if "COALESCE(MAX(check_sequence)" in s:
            ready_id = params[0]
            m = max(
                (c["check_sequence"] for c in self.checks if c["ready_candidate_id"] == ready_id),
                default=0,
            )
            return [{"seq": m + 1}]

        if "candidate_revision" in s and s.startswith("SELECT COALESCE"):
            ready_id = params[0]
            row = next((r for r in self.ready if r["id"] == ready_id), None)
            rev = int((row.get("data") or {}).get("candidate_revision", 0) or 0) if row else 0
            return [{"rev": rev}]

        if "SELECT check_type, positive FROM ready_candidate_checks" in s:
            ready_id, check_type = params[:2]
            rows = [c for c in self.checks if c["ready_candidate_id"] == ready_id and c["check_type"] == check_type]
            rows = sorted(rows, key=lambda c: c["id"], reverse=True)[:5]
            return [{"check_type": c["check_type"], "positive": c["positive"]} for c in rows]

        if "SELECT id, symbol FROM ready_candidates" in s:
            regime = params[0]
            return [
                {"id": r["id"], "symbol": r["symbol"]}
                for r in self.ready
                if r["regime_mode"] == regime and r["status"] in ("ready", "validated")
            ]

        if "SELECT id FROM ready_candidates" in s or ("SELECT * FROM ready_candidates" in s and "symbol=%s AND trade_type=%s AND regime_mode=%s" in s):
            sym, tt, reg = params[:3]
            for r in self.ready:
                if r["symbol"] == sym and r["trade_type"] == tt and r["regime_mode"] == reg and r["status"] in ("ready", "validated"):
                    return [dict(r)]
            return []

        if "SELECT * FROM ready_candidates" in s:
            return self._select_ready(s, params)

        if s.startswith("INSERT INTO ready_candidates") and "RETURNING id" in s:
            row = self._insert_ready(s, params)
            self._ready_id += 1
            row["id"] = self._ready_id
            self.ready.append(row)
            return [{"id": row["id"]}]

        return []

    def _select_ready(self, sql: str, params: tuple) -> List[Dict[str, Any]]:
        if "status IN %s" in sql:
            statuses = list(params[0])
            idx = 1
        elif "status='validated'" in sql:
            statuses = ["validated"]
            idx = 0
        elif "status IN ('ready','validated')" in sql:
            statuses = ["ready", "validated"]
            idx = 0
        else:
            statuses = ["ready", "validated"]
            idx = 0

        rows = [r for r in self.ready if r["status"] in statuses]

        if "regime_mode = %s" in sql or "regime_mode=%s" in sql:
            if idx < len(params):
                regime = params[idx]
                idx += 1
                rows = [r for r in rows if r["regime_mode"] == regime]

        if "trade_type = %s" in sql or "trade_type=%s" in sql:
            if idx < len(params):
                tt = params[idx]
                idx += 1
                rows = [r for r in rows if r["trade_type"] == tt]

        if "symbol = %s" in sql or "symbol=%s" in sql:
            if idx < len(params):
                sym = params[idx]
                idx += 1
                rows = [r for r in rows if r["symbol"] == sym]

        if "symbol = ANY(%s)" in sql or "symbol=ANY(%s)" in sql:
            if idx < len(params):
                syms = list(params[idx])
                idx += 1
                rows = [r for r in rows if r["symbol"] in syms]

        if "ORDER BY positive_checks DESC" in sql:
            rows = sorted(rows, key=lambda r: (-r.get("positive_checks", 0), r.get("ready_at") or self._now()))
        else:
            rows = sorted(rows, key=lambda r: r.get("ready_at") or self._now())
        return [dict(r) for r in rows]

    def _parse_insert(self, sql: str, params: tuple) -> Dict[str, Any]:
        col_part = sql[sql.find("(") + 1 : sql.find(")")]
        columns = [c.strip() for c in col_part.split(",")]
        after_values = sql[sql.find("VALUES") + 6 :]
        val_part = after_values[after_values.find("(") + 1 : after_values.rfind(")")]
        value_tokens = [t.strip() for t in val_part.split(",")]

        row: Dict[str, Any] = {}
        param_iter = iter(params)
        for col, tok in zip(columns, value_tokens):
            tok = tok.strip()
            if "%s" in tok:
                val = next(param_iter)
                if "::jsonb" in tok and isinstance(val, str):
                    try:
                        val = json.loads(val)
                    except Exception:
                        pass
                row[col] = val
            elif tok == "NOW()":
                row[col] = self._now()
            elif tok.startswith("'") and tok.endswith("'"):
                row[col] = tok[1:-1]
            else:
                try:
                    row[col] = int(tok)
                except ValueError:
                    row[col] = tok
        return row

    def _insert_ready(self, sql: str, params: tuple) -> Dict[str, Any]:
        row = self._parse_insert(sql, params)
        row.setdefault("updated_at", self._now())
        for k in ("signal_meta", "config_snapshot", "data"):
            if isinstance(row.get(k), str):
                try:
                    row[k] = json.loads(row[k])
                except Exception:
                    pass
        return row

    def update(self, sql: str, params: Optional[tuple] = None) -> int:
        params = params or ()
        s = sql.strip()
        if s.startswith("INSERT INTO ready_candidate_checks"):
            self._check_id += 1
            row = self._parse_insert(s, params)
            for k in ("input_features", "gate_thresholds"):
                if isinstance(row.get(k), str):
                    try:
                        row[k] = json.loads(row[k])
                    except Exception:
                        pass
            row["id"] = self._check_id
            self.checks.append(row)
            return 1

        if s.startswith("UPDATE ready_candidates SET"):
            return self._update_ready(s, params)

        if s.startswith("DELETE FROM ready_candidates"):
            days = params[0] if params else 7
            cutoff = self._now() - timedelta(days=int(days))
            before = len(self.ready)
            self.ready = [
                r
                for r in self.ready
                if not (r["status"] in ("discarded", "opened") and r.get("updated_at", self._now()) < cutoff)
            ]
            return before - len(self.ready)

        return 0

    def _update_ready(self, sql: str, params: tuple) -> int:
        upper = sql.upper()
        set_start = upper.find("SET ") + 4
        where_pos = upper.rfind(" WHERE ")
        set_part = sql[set_start:where_pos]
        where_part = upper[where_pos:]
        assignments = [a.strip() for a in set_part.split(",")]

        has_status_guard = "AND STATUS = %S" in where_part
        has_revision_guard = "CANDIDATE_REVISION" in where_part
        n_where_params = 1 + int(has_status_guard) + int(has_revision_guard)
        where_params = params[-n_where_params:]
        row_id = where_params[0]
        guard_status = where_params[1] if has_status_guard else None
        expected_revision = where_params[-1] if has_revision_guard else None

        row = next((r for r in self.ready if r["id"] == row_id), None)
        if row is None:
            return 0
        if has_status_guard and row.get("status") != guard_status:
            return 0
        if has_revision_guard:
            current_rev = int((row.get("data") or {}).get("candidate_revision", 0) or 0)
            if current_rev != int(expected_revision):
                return 0

        param_iter = iter(params[: len(params) - n_where_params])
        for assign in assignments:
            if "=" not in assign:
                continue
            field, value_expr = assign.split("=", 1)
            field = field.strip()
            value_expr = value_expr.strip()
            if value_expr == "NOW()":
                row[field] = self._now()
            elif "|| %s::jsonb" in value_expr and field == "data":
                delta_raw = next(param_iter)
                if isinstance(delta_raw, str):
                    try:
                        delta_raw = json.loads(delta_raw)
                    except Exception:
                        pass
                if isinstance(delta_raw, dict):
                    if not isinstance(row.get("data"), dict):
                        row["data"] = {}
                    row["data"].update(delta_raw)
            elif "%s" in value_expr:
                val = next(param_iter)
                if value_expr.endswith("::jsonb") and isinstance(val, str):
                    try:
                        val = json.loads(val)
                    except Exception:
                        pass
                row[field] = val
        row["updated_at"] = self._now()
        return 1


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
        self.audit_events: List[Dict[str, Any]] = []
        self._runtime_state: Dict[str, Any] = dict(runtime_state or {})
        # D19: Load historical indicator_weights for meta-learning
        self._indicator_weights: Dict[str, Any] = {}
        if use_indicator_weights:
            self._indicator_weights = _load_indicator_weights()
        # In-memory staged-entry tables so the lifecycle kernel works under MIMIC.
        self._staged = _StagedTables()
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
        # Route staged-entry tables to the in-memory store.
        if "ready_candidates" in sql_lower or "ready_candidate_checks" in sql_lower:
            return self._staged.query(sql, params)
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
        sql_lower = sql.lower()
        # Route staged-entry tables to the in-memory store.
        if "ready_candidates" in sql_lower or "ready_candidate_checks" in sql_lower:
            return self._staged.update(sql, params)
        # Insert/Update trading_runtime_state
        if "trading_runtime_state" in sql_lower and "on conflict" in sql_lower:
            key = params[0] if params else None
            value = params[1] if params and len(params) > 1 else None
            if key:
                self._runtime_state[key] = value
            return 1
        if "insert into trades_audit" in sql_lower:
            values = tuple(params or ())
            event = None
            detail = None
            symbol = None
            trade_type = None
            if len(values) >= 4:
                symbol, trade_type, event, detail = values[:4]
            elif len(values) == 1:
                detail = values[0]
                marker = "'"
                parts = sql.split(marker)
                if len(parts) >= 8:
                    symbol, trade_type, event = parts[1], parts[3], parts[5]
            try:
                parsed_detail = json.loads(detail) if isinstance(detail, str) else detail
            except Exception:
                parsed_detail = detail
            self.audit_events.append({
                "symbol": symbol,
                "trade_type": trade_type,
                "event": event,
                "detail": parsed_detail,
            })
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
