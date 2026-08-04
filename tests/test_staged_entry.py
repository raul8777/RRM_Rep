"""Unit tests for staged_entry.py lifecycle, persistence and traceability."""

import copy
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

import staged_entry


class FakeDBClient:
    """In-memory DB client that understands the SQL used by staged_entry."""

    def __init__(self):
        self.ready: List[Dict[str, Any]] = []
        self.checks: List[Dict[str, Any]] = []
        self._ready_id = 0
        self._check_id = 0

    def _now(self):
        return datetime.now(timezone.utc)

    def execute_query(self, query: str, params=None):
        params = params or ()
        q = query.strip()

        # SELECT * FROM ready_candidates ...
        if q.startswith("SELECT * FROM ready_candidates"):
            rows = self._select_ready(q, params)
            # Return shallow copies so callers can mutate row['data'] without affecting stored rows.
            return [copy.deepcopy(r) for r in rows]

        # SELECT id, symbol FROM ready_candidates WHERE regime_mode=%s AND status IN ...
        if q.startswith("SELECT id, symbol FROM ready_candidates"):
            regime = params[0]
            return [
                {"id": r["id"], "symbol": r["symbol"]}
                for r in self.ready
                if r["regime_mode"] == regime and r["status"] in ("ready", "validated")
            ]

        # SELECT id, symbol, data FROM ready_candidates WHERE regime_mode=%s AND status IN ...
        if q.startswith("SELECT id, symbol, data FROM ready_candidates"):
            regime = params[0]
            return [
                {"id": r["id"], "symbol": r["symbol"], "data": copy.deepcopy(r.get("data") or {})}
                for r in self.ready
                if r["regime_mode"] == regime and r["status"] in ("ready", "validated")
            ]

        # SELECT id FROM ready_candidates WHERE symbol=... OR SELECT * FROM ...
        if q.startswith("SELECT id FROM ready_candidates") or q.startswith(
            "SELECT * FROM ready_candidates WHERE symbol=%s AND trade_type=%s AND regime_mode=%s"
        ):
            sym, tt, reg = params[:3]
            for r in self.ready:
                if r["symbol"] == sym and r["trade_type"] == tt and r["regime_mode"] == reg and r["status"] in ("ready", "validated"):
                    return [copy.deepcopy(r)]
            return []

        # candidate_revision pre-fetch used by _update_ready_status's OCC guard
        if "candidate_revision" in q and q.startswith("SELECT COALESCE"):
            ready_id = params[0]
            row = next((r for r in self.ready if r["id"] == ready_id), None)
            rev = int((row.get("data") or {}).get("candidate_revision", 0) or 0) if row else 0
            return [{"rev": rev}]

        # Check sequence
        if "COALESCE(MAX(check_sequence)" in q:
            ready_id = params[0]
            m = max(
                (c["check_sequence"] for c in self.checks if c["ready_candidate_id"] == ready_id),
                default=0,
            )
            return [{"seq": m + 1}]

        # Recent checks
        if "SELECT check_type, positive FROM ready_candidate_checks" in q:
            ready_id, check_type = params[:2]
            rows = [
                c
                for c in self.checks
                if c["ready_candidate_id"] == ready_id and c["check_type"] == check_type
            ]
            rows = sorted(rows, key=lambda c: c["id"], reverse=True)[:5]
            return [{"check_type": c["check_type"], "positive": c["positive"]} for c in rows]

        # INSERT RETURNING id
        if q.startswith("INSERT INTO ready_candidates") and "RETURNING id" in q:
            row = self._insert_ready(q, params)
            self._ready_id += 1
            row["id"] = self._ready_id
            self.ready.append(row)
            return [{"id": row["id"]}]

        raise NotImplementedError(f"execute_query not implemented for: {q[:120]}")

    def execute_update(self, query: str, params=None):
        params = params or ()
        q = query.strip()

        if q.startswith("INSERT INTO ready_candidate_checks"):
            self._check_id += 1
            row = self._insert_check(q, params)
            row["id"] = self._check_id
            self.checks.append(row)
            return 1

        if q.startswith("UPDATE ready_candidates SET"):
            return self._update_ready(q, params)

        if q.startswith("DELETE FROM ready_candidates"):
            days = params[0] if params else 7
            cutoff = self._now() - timedelta(days=int(days))
            before = len(self.ready)
            self.ready = [
                r
                for r in self.ready
                if not (r["status"] in ("discarded", "opened") and r.get("updated_at", self._now()) < cutoff)
            ]
            return before - len(self.ready)

        raise NotImplementedError(f"execute_update not implemented for: {q[:120]}")

    def _select_ready(self, query: str, params):
        # Determine allowed statuses.
        if "status IN %s" in query:
            statuses = list(params[0])
            param_idx = 1
        elif "status='validated'" in query:
            statuses = ["validated"]
            param_idx = 0
        elif "status IN ('ready','validated')" in query:
            statuses = ["ready", "validated"]
            param_idx = 0
        else:
            statuses = ["ready", "validated"]
            param_idx = 0

        rows = [r for r in self.ready if r["status"] in statuses]

        if "regime_mode = %s" in query or "regime_mode=%s" in query:
            if param_idx < len(params):
                regime = params[param_idx]
                param_idx += 1
                rows = [r for r in rows if r["regime_mode"] == regime]

        if "trade_type = %s" in query or "trade_type=%s" in query:
            if param_idx < len(params):
                tt = params[param_idx]
                param_idx += 1
                rows = [r for r in rows if r["trade_type"] == tt]

        if "symbol = %s" in query or "symbol=%s" in query:
            if param_idx < len(params):
                sym = params[param_idx]
                param_idx += 1
                rows = [r for r in rows if r["symbol"] == sym]

        if "symbol = ANY(%s)" in query or "symbol=ANY(%s)" in query:
            if param_idx < len(params):
                syms = list(params[param_idx])
                param_idx += 1
                rows = [r for r in rows if r["symbol"] in syms]

        if "ORDER BY positive_checks DESC" in query:
            rows = sorted(rows, key=lambda r: (-r["positive_checks"], r["ready_at"]))
        else:
            rows = sorted(rows, key=lambda r: r["ready_at"])
        return rows

    def _parse_insert(self, query: str, params, table: str):
        # Extract column list and values list between first and second parentheses.
        col_part = query[query.find("(") + 1 : query.find(")")]
        columns = [c.strip() for c in col_part.split(",")]
        after_values = query[query.find("VALUES") + 6 :]
        val_part = after_values[after_values.find("(") + 1 : after_values.rfind(")")]
        value_tokens = [t.strip() for t in val_part.split(",")]

        row: Dict[str, Any] = {}
        param_iter = iter(params)
        for col, tok in zip(columns, value_tokens):
            tok = tok.strip()
            if "%s" in tok:
                # JSON cast -> deserialize
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
                # integer literal
                try:
                    row[col] = int(tok)
                except ValueError:
                    row[col] = tok
        return row

    def _insert_ready(self, query: str, params):
        row = self._parse_insert(query, params, "ready_candidates")
        row.setdefault("updated_at", self._now())
        if isinstance(row.get("signal_meta"), str):
            row["signal_meta"] = json.loads(row["signal_meta"])
        if isinstance(row.get("data"), str):
            row["data"] = json.loads(row["data"])
        if isinstance(row.get("config_snapshot"), str):
            row["config_snapshot"] = json.loads(row["config_snapshot"])
        return row

    def _insert_check(self, query: str, params):
        row = self._parse_insert(query, params, "ready_candidate_checks")
        for k in ("input_features", "gate_thresholds"):
            if isinstance(row.get(k), str):
                try:
                    row[k] = json.loads(row[k])
                except Exception:
                    pass
        return row

    def _update_ready(self, query: str, params):
        # Extract SET assignments and WHERE conditions (id, optional status
        # guard, optional candidate_revision optimistic-concurrency guard).
        where_idx = query.upper().rfind(" WHERE ")
        set_part = query[query.upper().find("SET ") + 4 : where_idx]
        where_part = query[where_idx:].upper()
        assignments = [a.strip() for a in set_part.split(",")]

        has_status_guard = "AND STATUS = %S" in where_part
        has_revision_guard = "CANDIDATE_REVISION" in where_part

        n_where_params = 1 + int(has_status_guard) + int(has_revision_guard)
        where_params = params[-n_where_params:]
        row_id = where_params[0]
        guard_status = where_params[1] if has_status_guard else None
        expected_revision = where_params[-1] if has_revision_guard else None
        param_iter = iter(params[: len(params) - n_where_params])

        row = next((r for r in self.ready if r["id"] == row_id), None)
        if row is None:
            return 0
        if has_status_guard and row.get("status") != guard_status:
            return 0
        if has_revision_guard:
            current_rev = int((row.get("data") or {}).get("candidate_revision", 0) or 0)
            if current_rev != int(expected_revision):
                return 0
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
            # else: literal already handled above? None expected.
        row["updated_at"] = self._now()
        return 1


class FakeSettings:
    staged_entry_enabled = True
    staged_entry_regime_config = {
        "SPOT_BULL": {
            "enabled": True,
            "required_positive_checks": 2,
            "checks_per_cycle": 2,
            "max_cycles": 5,
            "max_age_min": 1000,
            "intra_mode": "predictor",
            "price_source": "avg",
            "direction_confirmation_mode": "shadow",
        },
        "FUTURES_SHORT": {
            "enabled": True,
            "required_positive_checks": 2,
            "checks_per_cycle": 1,
            "max_cycles": 4,
            "max_age_min": 1000,
            "intra_mode": "disabled",
            "price_source": "avg",
        },
    }


@pytest.fixture
def fake_db():
    return FakeDBClient()


@pytest.fixture
def settings():
    return FakeSettings()


@pytest.fixture(autouse=True)
def patch_staged_helpers(monkeypatch):
    """Stub heavy/IO helpers inside staged_entry so tests run offline."""
    monkeypatch.setattr(
        staged_entry,
        "_get_price",
        lambda client, symbol, source: 101.0,
    )
    monkeypatch.setattr(
        staged_entry,
        "_classify_with_gate",
        lambda meta, regime, symbol, settings, fng: ("A", 0.55, {"thresholds": {"A": 0.0, "B": 0.0, "C": 0.0}}),
    )
    monkeypatch.setattr(
        staged_entry,
        "_run_predictor_for_validation",
        lambda client, symbol, settings, meta, current_price: {
            "expected_pnl": 0.5,
            "expected_pnl_percentage": 0.5,
            "position_size_usdt": 100.0,
            "success_probability": 0.6,
        },
    )


def _make_signal(symbol: str, side: str = "LONG"):
    return {
        "symbol": symbol,
        "peak_quality_profile": "A",
        "peak_quality_score": 0.55,
        "expected_side": side,
        "current_price": 100.0,
        "position_size_usdt": 100.0,
        "expected_pnl": 0.5,
        "success_probability": 0.6,
        "signal_strength": 0.7,
    }


def _direction_cfg(**overrides):
    cfg = {
        "direction_confirmation_mode": "enforced",
        "direction_min_progress_bps": 1.0,
        "direction_max_adverse_bps": 500.0,
        "direction_required_confirmations": 2,
    }
    cfg.update(overrides)
    return cfg


def _direction_row(regime="SPOT_BULL", side="LONG", base=100.0):
    return {
        "regime_mode": regime,
        "signal_price": base,
        "data": {
            "direction": {
                "state": "awaiting_confirmation",
                "base_price": base,
                "last_observed_price": base,
                "side": side,
                "directional_confirmations": 0,
                "adverse_observations": 0,
            }
        },
    }


def test_geometry_anchor_requires_more_than_flat_bps_when_stop_is_wide():
    """s111: with direction_geometry_anchor_enabled, a tick that would have
    "confirmed" under a flat 1bps threshold must be held as noise when the
    trade's own stop distance implies a more meaningful bar (5% of a 500bps
    stop = 25bps here), while still confirming once a tick actually clears it."""
    cfg = _direction_cfg(
        direction_min_progress_bps=1.0,
        direction_required_confirmations=1,
        direction_geometry_anchor_enabled=True,
        direction_min_progress_stop_fraction=0.05,
    )
    row = _direction_row(base=100.0)  # target=105 (unused here), stop=95 -> 500bps distance
    policy = {"target_price": 105.0, "stop_price": 95.0}

    # 20bps move: would have confirmed under the flat 1bps default, but is below
    # the anchored 25bps (5% of 500bps stop distance) bar.
    positive, terminal, reason, snapshot = staged_entry._direction_transition(row, policy, 100.20, cfg)
    assert (positive, terminal, reason) == (False, False, "DIRECTION_NOISE_HOLD")

    # 30bps move clears the anchored bar and confirms (required_confirmations=1).
    positive, terminal, reason, snapshot = staged_entry._direction_transition(row, policy, 100.30, cfg)
    assert (positive, terminal, reason) == (True, False, "DIRECTION_CONFIRMED")


def test_geometry_anchor_disabled_by_default_preserves_flat_bps_behavior():
    """Backward compatibility: without direction_geometry_anchor_enabled, a 20bps
    move still confirms against a flat 1bps threshold as before s111."""
    cfg = _direction_cfg(direction_min_progress_bps=1.0, direction_required_confirmations=1)
    row = _direction_row(base=100.0)
    policy = {"target_price": 105.0, "stop_price": 95.0}
    positive, terminal, reason, snapshot = staged_entry._direction_transition(row, policy, 100.20, cfg)
    assert (positive, terminal, reason) == (True, False, "DIRECTION_CONFIRMED")


def test_drawdown_soft_decrements_instead_of_resetting_confirmations():
    """s111: a candidate with 3 confirmations earned that hits one adverse
    excursion (DIRECTION_DRAWDOWN_SOFT) keeps most of that progress (drops to 2)
    instead of losing all of it — the state still demotes away from "confirmed"
    either way, so the move still gets re-validated; only the earned-progress
    counter is preserved instead of discarded."""
    cfg = _direction_cfg(direction_max_adverse_bps=10.0, direction_required_confirmations=2)
    row = _direction_row(base=100.0)
    row["data"]["direction"]["directional_confirmations"] = 3
    row["data"]["direction"]["last_observed_price"] = 100.5
    policy = {"target_price": 105.0, "stop_price": 95.0}

    # Adverse move beyond max_adverse_bps (10bps) relative to base_price.
    positive, terminal, reason, snapshot = staged_entry._direction_transition(row, policy, 99.85, cfg)
    assert (positive, terminal, reason) == (False, False, "DIRECTION_DRAWDOWN_SOFT")
    assert snapshot["direction"]["directional_confirmations"] == 2
    assert snapshot["direction"]["state"] == "recovery_pending"


def test_directional_recovery_requires_reclaim_and_two_advances():
    cfg = _direction_cfg()
    row = _direction_row()
    policy = {"target_price": 105.0, "stop_price": 95.0}

    positive, terminal, reason, snapshot = staged_entry._direction_transition(row, policy, 99.5, cfg)
    assert (positive, terminal, reason) == (False, False, "DIRECTION_RECOVERY_PENDING")
    row["data"]["direction"] = snapshot["direction"]

    positive, terminal, reason, snapshot = staged_entry._direction_transition(row, policy, 100.2, cfg)
    assert (positive, terminal, reason) == (False, False, "DIRECTION_CONFIRMING")
    row["data"]["direction"] = snapshot["direction"]

    positive, terminal, reason, snapshot = staged_entry._direction_transition(row, policy, 100.5, cfg)
    assert (positive, terminal, reason) == (True, False, "DIRECTION_CONFIRMED")
    assert snapshot["directional_confirmations"] == 2


def test_direction_noise_tick_does_not_reset_confirmations():
    """s106: a tick smaller than direction_min_progress_bps (pure noise) must
    NOT be treated like a genuine adverse move — it must hold state and
    confirmations steady instead of resetting progress already earned.
    Regression guard for the RANGE_SCALP entry-delay fix documented in
    calibracion_entradas-a20ab8-v3.md (s106)."""
    cfg = _direction_cfg()  # direction_min_progress_bps=1.0, required_confirmations=2
    row = _direction_row()  # base=100.0, LONG, last_observed_price=100.0
    policy = {"target_price": 105.0, "stop_price": 95.0}

    # 1st tick: genuine favorable progress -> 1/2 confirmations.
    positive, terminal, reason, snapshot = staged_entry._direction_transition(row, policy, 100.2, cfg)
    assert (positive, terminal, reason) == (False, False, "DIRECTION_CONFIRMING")
    assert snapshot["direction"]["directional_confirmations"] == 1
    row["data"]["direction"] = snapshot["direction"]

    # 2nd tick: pure noise — 0.05bps move, well under the 1.0bps threshold —
    # must hold, not reset progress or count as an adverse observation.
    noisy_price = 100.2 * (1 + 0.05 / 10000)
    positive, terminal, reason, snapshot = staged_entry._direction_transition(row, policy, noisy_price, cfg)
    assert (positive, terminal, reason) == (False, False, "DIRECTION_NOISE_HOLD")
    assert snapshot["direction"]["directional_confirmations"] == 1
    assert snapshot["direction"]["adverse_observations"] == 0
    row["data"]["direction"] = snapshot["direction"]

    # 3rd tick: genuine favorable progress again -> reaches required=2 and
    # confirms, proving the noise tick did not cost the candidate its
    # earlier progress (legacy behavior would have reset it to 0 here).
    positive, terminal, reason, snapshot = staged_entry._direction_transition(row, policy, 100.5, cfg)
    assert (positive, terminal, reason) == (True, False, "DIRECTION_CONFIRMED")
    assert snapshot["directional_confirmations"] == 2


def test_directional_adverse_observations_keep_waiting_breakout():
    row = _direction_row()
    policy = {"target_price": 105.0, "stop_price": 90.0}
    # 1st adverse → recovery_pending
    _, _, _, snapshot = staged_entry._direction_transition(row, policy, 99.5, _direction_cfg())
    row["data"]["direction"] = snapshot["direction"]

    # 2nd adverse → still recovery_pending (default max_adverse_observations=3)
    positive, terminal, reason, snapshot2 = staged_entry._direction_transition(row, policy, 99.0, _direction_cfg())
    assert (positive, terminal, reason) == (False, False, "DIRECTION_RECOVERY_PENDING")
    row["data"]["direction"] = snapshot2["direction"]

    # 3rd adverse → still waiting breakout (no terminal discard by itself)
    positive, terminal, reason, _ = staged_entry._direction_transition(row, policy, 98.5, _direction_cfg())
    assert (positive, terminal, reason) == (False, False, "DIRECTION_WAITING_BREAKOUT")


def test_range_direction_requires_valid_target():
    positive, terminal, reason, _ = staged_entry._direction_transition(
        _direction_row(regime="RANGE_SCALP"), {}, 100.5, _direction_cfg()
    )
    assert (positive, terminal, reason) == (False, True, "DIRECTION_TARGET_MISSING")


def test_range_direction_soft_drawdown_persists_recovery_state():
    row = _direction_row(regime="RANGE_SCALP")
    policy = {"target_price": 105.0, "stop_price": 90.0}
    positive, terminal, reason, snapshot = staged_entry._direction_transition(
        row,
        policy,
        98.9,
        _direction_cfg(direction_max_adverse_bps=100.0, direction_max_adverse_observations=4),
    )
    assert (positive, terminal, reason) == (False, False, "DIRECTION_DRAWDOWN_SOFT")
    assert snapshot["direction"]["state"] == "recovery_pending"
    assert snapshot["direction"]["adverse_observations"] == 1
    assert snapshot["direction"]["last_observed_price"] == 98.9


def test_range_direction_target_surpassed_is_not_invalid():
    row = _direction_row(regime="RANGE_SCALP", side="LONG", base=100.0)
    policy = {"target_price": 101.0, "stop_price": 95.0}
    positive, terminal, reason, _ = staged_entry._direction_transition(
        row,
        policy,
        101.6,
        _direction_cfg(direction_required_confirmations=1),
    )
    assert terminal is False
    assert reason != "DIRECTION_TARGET_INVALID"
    assert (positive, reason) == (True, "DIRECTION_CONFIRMED")


def test_build_initial_direction_uses_arbiter_short_prediction():
    """When arbiter decides FUTURES_SHORT, direction target/stop come from the
    short_prediction price_range (target=min, stop=max), not the LONG policy."""
    meta = {
        "expected_side": "SHORT",
        "directional_arbitration": {
            "selected_mode": "FUTURES_SHORT",
            "short_prediction": {
                "price_range": {"min": 95.0, "max": 105.0},
            },
        },
    }
    policy_data = {"target_price": 110.0, "stop_price": 90.0}  # LONG values (wrong for SHORT)
    d = staged_entry._build_initial_direction(100.0, meta, policy_data, "RANGE_SCALP")
    assert d["side"] == "SHORT"
    assert d["target_price"] == 95.0   # price_range.min for SHORT
    assert d["stop_price"] == 105.0    # price_range.max for SHORT
    assert d["base_price"] == 100.0


def test_build_initial_direction_uses_arbiter_long_prediction():
    """When arbiter decides FUTURES_LONG, direction target/stop come from
    futures_long_prediction price_range (target=max, stop=min)."""
    meta = {
        "expected_side": "LONG",
        "directional_arbitration": {
            "selected_mode": "FUTURES_LONG",
            "futures_long_prediction": {
                "price_range": {"min": 95.0, "max": 108.0},
            },
        },
    }
    policy_data = {"target_price": 110.0, "stop_price": 90.0}
    d = staged_entry._build_initial_direction(100.0, meta, policy_data, "RANGE_SCALP")
    assert d["side"] == "LONG"
    assert d["target_price"] == 108.0  # price_range.max for LONG
    assert d["stop_price"] == 95.0     # price_range.min for LONG


def test_build_initial_direction_falls_back_to_policy():
    """Without arbiter decision, use policy target/stop as before."""
    meta = {"expected_side": "LONG"}
    policy_data = {"target_price": 110.0, "stop_price": 90.0}
    d = staged_entry._build_initial_direction(100.0, meta, policy_data, "SPOT_BULL")
    assert d["target_price"] == 110.0
    assert d["stop_price"] == 90.0


def test_side_for_meta_forces_selected_mode_without_expected_side():
    meta = {
        "trade_type": "spot",
        "directional_arbitration": {
            "selected_mode": "FUTURES_SHORT",
        },
    }
    side = staged_entry._side_for_meta(meta, "RANGE_SCALP", "spot")
    trade_type = staged_entry._trade_type_for_meta(meta, "RANGE_SCALP")
    assert side == "SHORT"
    assert trade_type == "futures"


def test_build_initial_direction_forces_selected_mode_and_aligns_target_stop():
    """If selected_mode is FUTURES_SHORT but policy target/stop are LONG-oriented,
    side must stay SHORT and target/stop must be aligned to SHORT geometry."""
    meta = {
        "expected_side": "LONG",  # conflicting hint should be ignored
        "trade_type": "spot",
        "directional_arbitration": {
            "selected_mode": "FUTURES_SHORT",
            # no short_prediction target/stop provided -> fallback to policy + alignment
            "short_prediction": {},
        },
    }
    policy_data = {"target_price": 110.0, "stop_price": 90.0}
    d = staged_entry._build_initial_direction(100.0, meta, policy_data, "RANGE_SCALP")
    assert d["side"] == "SHORT"
    assert d["target_price"] == 90.0
    assert d["stop_price"] == 110.0


def _direction_row_with_sr(regime="RANGE_SCALP", side="LONG", base=100.0, support_level=None, resistance_level=None):
    row = _direction_row(regime=regime, side=side, base=base)
    row["data"]["support_level"] = support_level
    row["data"]["resistance_level"] = resistance_level
    return row


def test_long_direction_discarded_when_support_proximity_lost():
    # LONG staged off a support bounce at 98.0; price falls well below support
    # (beyond direction_sr_invalidation_pct) without ever touching stop_price.
    row = _direction_row_with_sr(regime="RANGE_SCALP", side="LONG", base=100.0, support_level=98.0)
    policy = {"target_price": 105.0, "stop_price": 90.0}
    positive, terminal, reason, snapshot = staged_entry._direction_transition(
        row, policy, 96.0, _direction_cfg(direction_sr_invalidation_pct=1.5, direction_max_adverse_bps=5000.0),
    )
    assert (positive, terminal, reason) == (False, True, "DIRECTION_SR_PROXIMITY_LOST")
    assert snapshot["direction"]["sr_proximity_lost"]["support_level"] == 98.0


def test_short_direction_discarded_when_resistance_proximity_lost():
    # SHORT staged off a resistance rejection at 105.0; price rallies well above
    # resistance (beyond direction_sr_invalidation_pct) without touching stop_price.
    row = _direction_row_with_sr(regime="FUTURES_SHORT", side="SHORT", base=103.0, resistance_level=105.0)
    policy = {"target_price": 95.0, "stop_price": 115.0}
    positive, terminal, reason, snapshot = staged_entry._direction_transition(
        row, policy, 108.0, _direction_cfg(direction_sr_invalidation_pct=1.5, direction_max_adverse_bps=5000.0),
    )
    assert (positive, terminal, reason) == (False, True, "DIRECTION_SR_PROXIMITY_LOST")
    assert snapshot["direction"]["sr_proximity_lost"]["resistance_level"] == 105.0


def test_soft_pullback_within_sr_tolerance_does_not_discard():
    # Price dips slightly below support but stays within direction_sr_invalidation_pct
    # tolerance — should stay alive (soft drawdown / recovery), never terminal.
    row = _direction_row_with_sr(regime="RANGE_SCALP", side="LONG", base=100.0, support_level=98.0)
    policy = {"target_price": 105.0, "stop_price": 90.0}
    positive, terminal, reason, _ = staged_entry._direction_transition(
        row, policy, 97.9, _direction_cfg(direction_sr_invalidation_pct=1.5, direction_max_adverse_bps=5000.0),
    )
    assert terminal is False
    assert reason != "DIRECTION_SR_PROXIMITY_LOST"


def test_sr_invalidation_disabled_by_default_does_not_discard():
    # When direction_sr_invalidation_pct is not configured (0.0, legacy behavior),
    # the new check must be a no-op regardless of how far price strays from the SR level.
    row = _direction_row_with_sr(regime="RANGE_SCALP", side="LONG", base=100.0, support_level=98.0)
    policy = {"target_price": 105.0, "stop_price": 50.0}
    positive, terminal, reason, _ = staged_entry._direction_transition(
        row, policy, 80.0, _direction_cfg(direction_max_adverse_bps=5000.0),
    )
    assert reason != "DIRECTION_SR_PROXIMITY_LOST"


def test_candidate_not_expired_before_eight_hours(fake_db):
    # A validated RANGE_SCALP candidate must not be treated as expired before
    # the 8h (480 min) max_age_min ceiling, even if it has been waiting for a
    # while and consumed several cycles.
    row = {
        "id": 1,
        "status": "ready",
        "max_age_min": 480,
        "max_cycles": 100,
        "ready_at": staged_entry._now() - timedelta(hours=7, minutes=59),
        "data": {"cycles_active": 90, "candidate_revision": 0},
    }
    fake_db.ready.append(row)
    assert staged_entry._maybe_expire(fake_db, row) is False


def test_candidate_expires_past_eight_hours(fake_db):
    row = {
        "id": 1,
        "status": "ready",
        "max_age_min": 480,
        "max_cycles": 100,
        "ready_at": staged_entry._now() - timedelta(hours=8, minutes=1),
        "data": {"cycles_active": 5, "candidate_revision": 0},
    }
    fake_db.ready.append(row)
    assert staged_entry._maybe_expire(fake_db, row) is True
    assert row["discard_reason"] == "MAX_AGE_EXPIRED"


def test_direction_confirms_on_first_observation_using_base_progress():
    """s108/Opcion F: if the market already moved past the confirmation
    threshold between signal detection and the very first observation (i.e.
    `last_observed_price` still equals `base_price`, no prior tick recorded),
    confirm using cumulative progress since the signal (`base_progress_bps`)
    instead of discarding that head start and waiting for a fresh tick-to-tick
    move from wherever the first observed price happens to be."""
    cfg = _direction_cfg(direction_required_confirmations=1)
    row = _direction_row(regime="FUTURES_SHORT", side="SHORT", base=100.0)
    policy = {"target_price": 95.0, "stop_price": 105.0}

    # First observation ever for this candidate: price already fell 6bps
    # (0.06) below the signal price by the time anyone checked it.
    positive, terminal, reason, snapshot = staged_entry._direction_transition(row, policy, 99.94, cfg)
    assert (positive, terminal, reason) == (True, False, "DIRECTION_CONFIRMED")
    assert snapshot["base_progress_bps"] > snapshot["min_progress_bps"]


def test_direction_noise_hold_unaffected_after_first_observation():
    """Option F's first-observation shortcut must not resurrect the tick that
    the s106 noise-hold fix was designed to ignore, once a real tick-to-tick
    history exists (last_observed_price != base_price)."""
    cfg = _direction_cfg()  # direction_min_progress_bps=1.0, required_confirmations=2
    row = _direction_row()  # base=100.0, LONG, last_observed_price=100.0
    policy = {"target_price": 105.0, "stop_price": 95.0}

    positive, terminal, reason, snapshot = staged_entry._direction_transition(row, policy, 100.2, cfg)
    assert (positive, terminal, reason) == (False, False, "DIRECTION_CONFIRMING")
    row["data"]["direction"] = snapshot["direction"]

    # Second observation: last_observed_price (100.2) != base_price (100.0),
    # so the first-observation shortcut no longer applies even though
    # base_progress_bps is still well above the threshold.
    noisy_price = 100.2 * (1 + 0.05 / 10000)
    positive, terminal, reason, snapshot = staged_entry._direction_transition(row, policy, noisy_price, cfg)
    assert (positive, terminal, reason) == (False, False, "DIRECTION_NOISE_HOLD")


def test_short_direction_confirms_downward_progress():
    row = _direction_row(regime="FUTURES_SHORT", side="SHORT")
    policy = {"target_price": 95.0, "stop_price": 105.0}
    positive, terminal, reason, snapshot = staged_entry._direction_transition(row, policy, 99.5, _direction_cfg())
    assert (positive, terminal, reason) == (False, False, "DIRECTION_CONFIRMING")
    row["data"]["direction"] = snapshot["direction"]

    positive, terminal, reason, _ = staged_entry._direction_transition(row, policy, 99.0, _direction_cfg())
    assert (positive, terminal, reason) == (True, False, "DIRECTION_CONFIRMED")


def test_direction_stamps_first_confirmed_price_once(monkeypatch):
    """s108/Opcion E: `first_confirmed_price`/`first_confirmed_at` must be set
    exactly once, the first time the setup reaches "confirmed", and must NOT
    be overwritten by later favorable ticks (unlike `last_confirmed_price`,
    which is meant to track the most recent one) — this is what lets the
    system measure the wait cost (first confirmation vs. actual fill price)."""
    row = _direction_row(regime="FUTURES_SHORT", side="SHORT")
    policy = {"target_price": 95.0, "stop_price": 105.0}
    cfg = _direction_cfg(direction_required_confirmations=1)

    positive, terminal, reason, snapshot = staged_entry._direction_transition(row, policy, 99.5, cfg)
    assert (positive, terminal, reason) == (True, False, "DIRECTION_CONFIRMED")
    assert snapshot["direction"]["first_confirmed_price"] == 99.5
    first_confirmed_at = snapshot["direction"]["first_confirmed_at"]
    row["data"]["direction"] = snapshot["direction"]

    # A later, further favorable tick must update last_confirmed_price but
    # must NOT touch first_confirmed_price/first_confirmed_at.
    positive, terminal, reason, snapshot2 = staged_entry._direction_transition(row, policy, 99.0, cfg)
    assert (positive, terminal, reason) == (True, False, "DIRECTION_CONFIRMED")
    assert snapshot2["direction"]["last_confirmed_price"] == 99.0
    assert snapshot2["direction"]["first_confirmed_price"] == 99.5
    assert snapshot2["direction"]["first_confirmed_at"] == first_confirmed_at


def test_watcher_discards_ready_candidate_on_adverse_break(fake_db, settings):
    """s108/Opcion H: direction analysis now runs during `ready` (not only after
    `validated`), so a candidate that already broke decisively against the
    setup can be discarded immediately instead of waiting for the quality/EV
    gate to keep re-checking it every cycle until it eventually reaches
    `validated` (or times out)."""
    meta = {"BTCUSDT": _make_signal("BTCUSDT", side="SHORT")}
    top = [meta["BTCUSDT"]]
    staged_entry.stage_ready_candidates(top, {}, meta, "FUTURES_SHORT", fake_db, settings=settings)
    row = fake_db.ready[0]
    assert row["status"] == "ready"
    row["data"]["direction"] = {
        "side": "SHORT", "base_price": 100.0, "target_price": 95.0, "stop_price": 105.0,
    }
    cfg = staged_entry.get_staged_entry_regime_config(settings, "FUTURES_SHORT")
    staged_entry._watcher_process_row(fake_db, row, 108.0, cfg, [])
    updated = fake_db.ready[0]
    assert updated["status"] == "discarded"
    assert updated["discard_reason"] == "DIRECTION_STOP_BREACHED"


def test_watcher_confirms_ready_candidate_direction_without_promoting(fake_db, settings):
    """A favorable direction confirmation detected during `ready` must be
    persisted (so the row reaches `validated` already confirmed and the
    fast-path does not re-earn it from scratch) but must NOT by itself promote
    the row to `validated` or add it to the validated list — that stays
    governed exclusively by the quality/EV gate in `evaluate_inter_cycle`."""
    meta = {"BTCUSDT": _make_signal("BTCUSDT", side="SHORT")}
    top = [meta["BTCUSDT"]]
    staged_entry.stage_ready_candidates(top, {}, meta, "FUTURES_SHORT", fake_db, settings=settings)
    row = fake_db.ready[0]
    row["data"]["direction"] = {
        "side": "SHORT", "base_price": 100.0, "target_price": 95.0, "stop_price": 105.0,
    }
    cfg = dict(staged_entry.get_staged_entry_regime_config(settings, "FUTURES_SHORT"))
    cfg["direction_required_confirmations"] = 1
    cfg["direction_min_progress_bps"] = 1.0
    validated_list: List[str] = []
    staged_entry._watcher_process_row(fake_db, row, 99.5, cfg, validated_list)
    updated = fake_db.ready[0]
    assert updated["status"] == "ready"
    assert validated_list == []
    assert updated["data"]["direction"]["state"] == "confirmed"


def test_stage_ready_candidates_creates_record(fake_db, settings):
    top = [_make_signal("BTCUSDT")]
    staged = staged_entry.stage_ready_candidates(
        top, {}, {"BTCUSDT": _make_signal("BTCUSDT")}, "SPOT_BULL",
        fake_db, cycle_id="c1", settings=settings, client=None,
    )
    assert len(staged) == 1
    assert staged[0]["symbol"] == "BTCUSDT"
    assert staged[0]["new"] is True

    rows = fake_db.ready
    assert len(rows) == 1
    assert rows[0]["status"] == "ready"
    # direction_confirmation_mode='shadow' → initial check counts as positive
    assert rows[0]["positive_checks"] == 1
    assert rows[0]["total_checks"] == 1
    assert rows[0]["data"]["cycles_active"] == 1

    checks = fake_db.checks
    assert len(checks) == 1
    assert checks[0]["check_type"] == "inter"
    assert checks[0]["positive"] is True
    assert checks[0]["decision"] == "setup_detected"


def test_stage_existing_is_skipped(fake_db, settings):
    meta = {"BTCUSDT": _make_signal("BTCUSDT")}
    top = [_make_signal("BTCUSDT")]
    staged_entry.stage_ready_candidates(top, {}, meta, "SPOT_BULL", fake_db, settings=settings)
    staged2 = staged_entry.stage_ready_candidates(top, {}, meta, "SPOT_BULL", fake_db, settings=settings)
    assert staged2 == []
    assert len(fake_db.ready) == 1
    assert len(fake_db.checks) == 1


def test_evaluate_inter_cycle_validates_after_required(fake_db, settings):
    meta = {"BTCUSDT": _make_signal("BTCUSDT")}
    top = [_make_signal("BTCUSDT")]
    staged_entry.stage_ready_candidates(top, {}, meta, "SPOT_BULL", fake_db, settings=settings)

    result = staged_entry.evaluate_inter_cycle(
        fake_db, None, settings, "SPOT_BULL", top, {}, meta, cycle_id="c2", news_client=None,
    )
    # direction_confirmation_mode='shadow' → staging gives 1 positive, inter-cycle gives 2nd → validated
    assert "BTCUSDT" in result["validated"]
    row = fake_db.ready[0]
    assert row["status"] == "validated"
    assert row["positive_checks"] == 2
    assert row["total_checks"] == 2
    assert row["data"]["cycles_active"] == 2


def test_evaluate_inter_cycle_discards_on_negative(fake_db, settings, monkeypatch):
    meta = {"BTCUSDT": _make_signal("BTCUSDT")}
    top = [_make_signal("BTCUSDT")]
    staged_entry.stage_ready_candidates(top, {}, meta, "SPOT_BULL", fake_db, settings=settings)

    monkeypatch.setattr(
        staged_entry,
        "_classify_with_gate",
        lambda *args, **kwargs: (None, 0.0, {}),
    )
    result = staged_entry.evaluate_inter_cycle(
        fake_db, None, settings, "SPOT_BULL", top, {}, meta, cycle_id="c2", news_client=None,
    )
    assert "BTCUSDT" in result["discarded"]
    assert fake_db.ready[0]["status"] == "discarded"
    assert fake_db.ready[0]["discard_reason"] == "QUALITY_BELOW_C"


def test_get_openable_candidates_returns_best_validated_ev_globally(fake_db, settings):
    meta = {
        "BTCUSDT": _make_signal("BTCUSDT"),
        "ETHUSDT": _make_signal("ETHUSDT"),
    }
    top = [meta["BTCUSDT"], meta["ETHUSDT"]]
    staged_entry.stage_ready_candidates(top, {}, meta, "SPOT_BULL", fake_db, settings=settings)
    staged_entry.evaluate_inter_cycle(
        fake_db, None, settings, "SPOT_BULL", top, {}, meta, cycle_id="c2", news_client=None,
    )
    btc_row, eth_row = fake_db.ready
    for row, expected_pnl, quality_score, probability in ((btc_row, 1.0, 0.5, 0.6), (eth_row, 2.0, 0.4, 0.7)):
        row["status"] = "validated"
        row["data"]["policy"].update({"expected_pnl": expected_pnl, "quality_score": quality_score, "success_probability": probability})
        row["data"]["direction"].update({"state": "confirmed", "directional_confirmations": 2})

    openable = staged_entry.get_openable_candidates(fake_db, "SPOT_BULL", ["BTCUSDT"], settings=settings)
    assert len(openable) == 2
    assert openable[0]["symbol"] == "ETHUSDT"  # higher expected_pnl first
    assert openable[1]["symbol"] == "BTCUSDT"
    assert eth_row["data"]["global_validated_rank"] == 1
    assert btc_row["data"]["global_validated_rank"] == 2
    assert eth_row["data"]["global_validated_universe_size"] == 2


def test_get_openable_candidates_does_not_clobber_concurrent_discard(fake_db, settings, monkeypatch):
    """Regression: a candidate discarded by a concurrent watcher thread between
    get_openable_candidates' SELECT and its risk-queue stage-stamping UPDATE must
    keep its real discard stage_kind/stage_reason, not the stale RISK_QUEUE_SELECTED
    payload written by the (now-stale) risk-queue selection.
    """
    meta = {"BTCUSDT": _make_signal("BTCUSDT")}
    top = [meta["BTCUSDT"]]
    staged_entry.stage_ready_candidates(top, {}, meta, "SPOT_BULL", fake_db, settings=settings)
    staged_entry.evaluate_inter_cycle(
        fake_db, None, settings, "SPOT_BULL", top, {}, meta, cycle_id="c2", news_client=None,
    )
    row = fake_db.ready[0]
    row["status"] = "validated"
    row["data"]["direction"].update({"state": "confirmed", "directional_confirmations": 2})

    # Simulate the SELECT already having captured status='validated' by monkeypatching
    # execute_query to discard the row (as a concurrent watcher would) right after the
    # SELECT executes, before get_openable_candidates' UPDATE runs.
    original_execute_query = fake_db.execute_query

    def _execute_query_with_race(query, params=None):
        result = original_execute_query(query, params)
        if query.strip().startswith("SELECT * FROM ready_candidates"):
            staged_entry._discard_ready(fake_db, row["id"], "DIRECTION_STOP_BREACHED")
        return result

    monkeypatch.setattr(fake_db, "execute_query", _execute_query_with_race)

    staged_entry.get_openable_candidates(fake_db, "SPOT_BULL", settings=settings)
    assert row["status"] == "discarded"
    assert row["discard_reason"] == "DIRECTION_STOP_BREACHED"
    assert row["data"]["stage_kind"] == "discarded"
    assert row["data"]["stage_reason"] != "RISK_QUEUE_SELECTED"


def test_evaluate_inter_cycle_skips_stale_write_on_concurrent_conflict(fake_db, settings, monkeypatch):
    """Regression: if a concurrent watcher discards the row between
    evaluate_inter_cycle's load and its own decision write, the stale
    evaluate_inter_cycle write must be skipped (revision mismatch) instead of
    silently clobbering the concurrent discard.
    """
    meta = {"BTCUSDT": _make_signal("BTCUSDT")}
    top = [meta["BTCUSDT"]]
    staged_entry.stage_ready_candidates(top, {}, meta, "SPOT_BULL", fake_db, settings=settings)
    row = fake_db.ready[0]

    original_execute_query = fake_db.execute_query

    def _execute_query_with_race(query, params=None):
        result = original_execute_query(query, params)
        if query.strip().startswith("SELECT * FROM ready_candidates"):
            staged_entry._discard_ready(fake_db, row["id"], "DIRECTION_STOP_BREACHED")
        return result

    monkeypatch.setattr(fake_db, "execute_query", _execute_query_with_race)

    staged_entry.evaluate_inter_cycle(
        fake_db, None, settings, "SPOT_BULL", top, {}, meta, cycle_id="c2", news_client=None,
    )

    assert row["status"] == "discarded"
    assert row["discard_reason"] == "DIRECTION_STOP_BREACHED"
    checks = [c for c in fake_db.checks if c["ready_candidate_id"] == row["id"]]
    assert any(c["decision"] == "concurrent_conflict" for c in checks)


def test_get_openable_candidates_records_lifecycle_check(fake_db, settings):
    """RISK_QUEUE_SELECTED must produce an auditable lifecycle check, not a
    silent write."""
    meta = {"BTCUSDT": _make_signal("BTCUSDT")}
    top = [meta["BTCUSDT"]]
    staged_entry.stage_ready_candidates(top, {}, meta, "SPOT_BULL", fake_db, settings=settings)
    staged_entry.evaluate_inter_cycle(
        fake_db, None, settings, "SPOT_BULL", top, {}, meta, cycle_id="c2", news_client=None,
    )
    row = fake_db.ready[0]
    row["status"] = "validated"
    row["data"]["direction"].update({"state": "confirmed", "directional_confirmations": 2})

    staged_entry.get_openable_candidates(fake_db, "SPOT_BULL", settings=settings)

    lifecycle_checks = [
        c for c in fake_db.checks
        if c["ready_candidate_id"] == row["id"] and c["check_type"] == "lifecycle"
    ]
    assert any(c["reason"] == "RISK_QUEUE_SELECTED" and c["writer"] == "risk_selector" for c in lifecycle_checks)


def test_get_openable_candidates_deprioritizes_recent_execution_failure(fake_db, settings):
    """s111: a candidate whose last real execution attempt failed
    (`last_execution_gate.passed=False`) must rank BEHIND a candidate that hasn't
    failed, even with a lower expected_pnl/quality_score. Regression guard for the
    HEIUSDT/RIFUSDT queue-starvation incident (2026-08-02): both held the only 2
    `max_open_per_cycle` slots almost continuously for 46 minutes despite
    repeatedly failing to execute, starving 6 other genuinely executable
    candidates (93% of all risk-queue selections in 48h went to candidates that
    never opened — see calibracion_entradas-a20ab8-v3.md s111)."""
    meta = {
        "HIGHEV_BUT_FAILED": _make_signal("HIGHEV_BUT_FAILED"),
        "LOWEV_BUT_VIABLE": _make_signal("LOWEV_BUT_VIABLE"),
    }
    top = [meta["HIGHEV_BUT_FAILED"], meta["LOWEV_BUT_VIABLE"]]
    staged_entry.stage_ready_candidates(top, {}, meta, "SPOT_BULL", fake_db, settings=settings)
    staged_entry.evaluate_inter_cycle(
        fake_db, None, settings, "SPOT_BULL", top, {}, meta, cycle_id="c2", news_client=None,
    )
    for row in fake_db.ready:
        row["status"] = "validated"
        row["data"]["direction"].update({"state": "confirmed", "directional_confirmations": 2})
        row["data"].setdefault("policy", {})
        if row["symbol"] == "HIGHEV_BUT_FAILED":
            row["data"]["policy"].update({"expected_pnl": 100.0, "quality_score": 0.9, "success_probability": 0.9})
            row["data"]["last_execution_gate"] = {"passed": False, "reason": "STALE_ENTRY_REWARD_RISK_BELOW_MIN"}
        else:
            row["data"]["policy"].update({"expected_pnl": 1.0, "quality_score": 0.5, "success_probability": 0.5})

    settings.staged_entry_regime_config["SPOT_BULL"]["max_open_per_cycle"] = 1
    selected = staged_entry.get_openable_candidates(fake_db, "SPOT_BULL", settings=settings)

    assert len(selected) == 1
    assert selected[0]["symbol"] == "LOWEV_BUT_VIABLE"


def test_mark_opened_records_lifecycle_check(fake_db, settings):
    meta = {"BTCUSDT": _make_signal("BTCUSDT")}
    top = [meta["BTCUSDT"]]
    staged_entry.stage_ready_candidates(top, {}, meta, "SPOT_BULL", fake_db, settings=settings)
    row = fake_db.ready[0]

    staged_entry.mark_opened(fake_db, row["id"], trade_id=123)

    lifecycle_checks = [
        c for c in fake_db.checks
        if c["ready_candidate_id"] == row["id"] and c["check_type"] == "lifecycle"
    ]
    assert any(c["reason"] == "EXECUTION_CONFIRMED" and c["writer"] == "main_cycle" for c in lifecycle_checks)


def test_discard_ready_by_regime_change_records_lifecycle_check(fake_db, settings):
    meta = {"BTCUSDT": _make_signal("BTCUSDT")}
    top = [meta["BTCUSDT"]]
    staged_entry.stage_ready_candidates(top, {}, meta, "SPOT_BULL", fake_db, settings=settings)
    row = fake_db.ready[0]

    discarded = staged_entry.discard_ready_by_regime_change(fake_db, "SPOT_BULL", "RANGE_SCALP")

    assert discarded == ["BTCUSDT"]
    lifecycle_checks = [
        c for c in fake_db.checks
        if c["ready_candidate_id"] == row["id"] and c["check_type"] == "lifecycle" and c["writer"] == "regime_guard"
    ]
    assert lifecycle_checks


def test_perform_intra_cycle_check_validates(fake_db, settings):
    meta = {"BTCUSDT": _make_signal("BTCUSDT")}
    top = [_make_signal("BTCUSDT")]
    staged_entry.stage_ready_candidates(top, {}, meta, "SPOT_BULL", fake_db, settings=settings)

    validated = staged_entry.perform_intra_cycle_check(fake_db, None, settings, "SPOT_BULL", news_client=None)
    # direction_confirmation_mode='shadow' → staging=1 positive + intra=2nd → validated
    assert validated == ["BTCUSDT"]
    row = fake_db.ready[0]
    assert row["status"] == "validated"
    assert row["positive_checks"] == 2
    assert any(c["check_type"] == "intra" for c in fake_db.checks)


def test_discard_ready_by_regime_change(fake_db, settings):
    meta = {"BTCUSDT": _make_signal("BTCUSDT")}
    top = [_make_signal("BTCUSDT")]
    staged_entry.stage_ready_candidates(top, {}, meta, "SPOT_BULL", fake_db, settings=settings)

    discarded = staged_entry.discard_ready_by_regime_change(fake_db, "SPOT_BULL", "RANGE_SCALP")
    assert "BTCUSDT" in discarded
    assert fake_db.ready[0]["status"] == "discarded"


def test_mark_opened(fake_db, settings):
    meta = {"BTCUSDT": _make_signal("BTCUSDT")}
    top = [_make_signal("BTCUSDT")]
    staged_entry.stage_ready_candidates(top, {}, meta, "SPOT_BULL", fake_db, settings=settings)
    staged_entry.evaluate_inter_cycle(
        fake_db, None, settings, "SPOT_BULL", top, {}, meta, cycle_id="c2", news_client=None,
    )
    staged_entry.mark_opened(fake_db, fake_db.ready[0]["id"], 12345)
    row = fake_db.ready[0]
    assert row["status"] == "opened"
    assert row["opened_trade_id"] == 12345
    assert row["opened_at"] is not None
    assert row["data"]["stage_kind"] == "opened"
    assert row["data"]["stage_reason"] == "EXECUTION_CONFIRMED"


def test_get_openable_candidates_discards_stale_hold_without_block_reason(fake_db, settings):
    meta = {"BTCUSDT": _make_signal("BTCUSDT")}
    top = [_make_signal("BTCUSDT")]
    staged_entry.stage_ready_candidates(top, {}, meta, "SPOT_BULL", fake_db, settings=settings)
    row = fake_db.ready[0]
    row["status"] = "validated"
    row["validated_at"] = datetime.now(timezone.utc) - timedelta(minutes=75)
    row["data"] = dict(row.get("data") or {})
    row["data"]["candidate_revision"] = int(row["data"].get("candidate_revision", 0) or 0)
    row["data"]["last_execution_gate"] = {
        "passed": False,
        "reason": "QUALITY_RECHECK_PENDING",
    }

    selected = staged_entry.get_openable_candidates(fake_db, "SPOT_BULL", settings=settings)

    assert selected == []
    assert fake_db.ready[0]["status"] == "discarded"
    assert fake_db.ready[0]["discard_reason"] == "STALE_HOLD_NO_SLOT_OR_CAPITAL_BLOCK"
    assert any(
        c["check_type"] == "lifecycle" and c["reason"] == "STALE_HOLD_NO_SLOT_OR_CAPITAL_BLOCK"
        for c in fake_db.checks
    )


def test_get_openable_candidates_keeps_stale_hold_when_capital_blocked(fake_db, settings):
    meta = {"BTCUSDT": _make_signal("BTCUSDT")}
    top = [_make_signal("BTCUSDT")]
    staged_entry.stage_ready_candidates(top, {}, meta, "SPOT_BULL", fake_db, settings=settings)
    row = fake_db.ready[0]
    row["status"] = "validated"
    row["validated_at"] = datetime.now(timezone.utc) - timedelta(minutes=75)
    row["data"] = dict(row.get("data") or {})
    row["data"]["candidate_revision"] = int(row["data"].get("candidate_revision", 0) or 0)
    row["data"]["last_execution_gate"] = {
        "passed": False,
        "reason": "CAPITAL_ROUTING_COOLDOWN_ACTIVE",
    }

    selected = staged_entry.get_openable_candidates(fake_db, "SPOT_BULL", settings=settings)

    assert len(selected) == 1
    assert selected[0]["symbol"] == "BTCUSDT"
    assert fake_db.ready[0]["status"] == "validated"


def test_stage_kind_progresses_setup_trigger_risk(fake_db, settings):
    meta = {"BTCUSDT": _make_signal("BTCUSDT")}
    top = [_make_signal("BTCUSDT")]
    staged_entry.stage_ready_candidates(top, {}, meta, "SPOT_BULL", fake_db, settings=settings)

    row = fake_db.ready[0]
    assert row["data"]["stage_kind"] == "setup_detected"
    assert row["data"]["stage_reason"] == "SETUP_CAPTURED"

    staged_entry.evaluate_inter_cycle(
        fake_db, None, settings, "SPOT_BULL", top, {}, meta, cycle_id="c2", news_client=None,
    )
    row = fake_db.ready[0]
    assert row["status"] == "validated"
    assert row["data"]["stage_kind"] == "risk_authorized"
    assert row["data"]["stage_reason"] == "TRIGGER_FIRED_VALIDATED"


def test_cleanup_old(fake_db, settings):
    old = {
        "symbol": "OLDUSDT",
        "trade_type": "spot",
        "regime_mode": "SPOT_BULL",
        "status": "discarded",
        "updated_at": datetime.now(timezone.utc) - timedelta(days=10),
    }
    fake_db.ready.append(old)
    staged_entry.cleanup_old(fake_db, retention_days=7)
    assert len(fake_db.ready) == 0


def test_max_cycles_expires(fake_db, settings, monkeypatch):
    # Required > max_cycles so validation cannot happen before expiry.
    settings.staged_entry_regime_config["SPOT_BULL"]["required_positive_checks"] = 10
    settings.staged_entry_regime_config["SPOT_BULL"]["max_cycles"] = 2
    meta = {"BTCUSDT": _make_signal("BTCUSDT")}
    top = [_make_signal("BTCUSDT")]
    staged_entry.stage_ready_candidates(top, {}, meta, "SPOT_BULL", fake_db, settings=settings)

    # First inter check: cycles_active becomes 2, max_cycles reached -> discarded.
    result = staged_entry.evaluate_inter_cycle(
        fake_db, None, settings, "SPOT_BULL", top, {}, meta, cycle_id="c2", news_client=None,
    )
    assert "BTCUSDT" in result["discarded"]
    assert fake_db.ready[0]["status"] == "discarded"
    assert fake_db.ready[0]["discard_reason"] == "MAX_CYCLES_REACHED"


def test_status_disabled_returns_empty(fake_db):
    disabled = types.SimpleNamespace(
        staged_entry_enabled=False,
        staged_entry_regime_config={},
    )
    result = staged_entry.stage_ready_candidates(
        [_make_signal("BTCUSDT")], {}, {}, "SPOT_BULL", fake_db, cycle_id="c1", settings=disabled,
    )
    assert result == []
    result2 = staged_entry.evaluate_inter_cycle(
        fake_db, None, disabled, "SPOT_BULL", [], {}, {}, cycle_id="c1", news_client=None,
    )
    assert result2 == {"validated": [], "discarded": [], "kept": [], "opened": []}


# ═════════════════════════════════════════════════════════════════════════════
# SR feature propagation tests
# ═════════════════════════════════════════════════════════════════════════════

def _make_sr_signal(symbol="BTCUSDT", side="LONG", sr_zone_position=0.3):
    sig = _make_signal(symbol, side)
    sig.update({
        "sr_zone_position": sr_zone_position,
        "nearest_support_dist_pct": -0.02,
        "nearest_resistance_dist_pct": 0.04,
        "support_level": 98.0,
        "resistance_level": 105.0,
        "support_bounce_buy": True,
    })
    return sig


def test_stage_ready_candidates_persist_sr_features(fake_db, settings):
    settings.staged_entry_regime_config["RANGE_SCALP"] = {
        "enabled": True,
        "required_positive_checks": 1,
        "checks_per_cycle": 2,
        "max_cycles": 4,
        "max_age_min": 1000,
        "intra_mode": "predictor",
        "price_source": "avg",
        "direction_confirmation_mode": "enforced",
        "direction_min_progress_bps": 1.0,
        "direction_max_adverse_bps": 500.0,
        "direction_required_confirmations": 1,
    }
    meta = {"BTCUSDT": _make_sr_signal()}
    top = [_make_sr_signal()]
    staged = staged_entry.stage_ready_candidates(
        top, {}, meta, "RANGE_SCALP", fake_db, cycle_id="c1", settings=settings, client=None,
    )
    assert len(staged) == 1
    row = fake_db.ready[0]
    data = row["data"]
    assert data["sr_zone_position"] == 0.3
    assert data["nearest_support_dist_pct"] == -0.02
    assert data["nearest_resistance_dist_pct"] == 0.04
    assert data["support_level"] == 98.0
    assert data["resistance_level"] == 105.0
    assert data["support_bounce_buy"] is True


def test_adapt_cfg_for_sr_signal_relaxes_gates():
    cfg = {
        "required_positive_checks": 2,
        "direction_required_confirmations": 2,
        "direction_min_progress_bps": 5.0,
        "max_cycles": 2,
    }
    meta = {"support_bounce_buy": True}
    adapted = staged_entry._adapt_cfg_for_sr_signal(cfg, meta)
    assert adapted["required_positive_checks"] == 1
    assert adapted["direction_required_confirmations"] == 1
    assert adapted["direction_min_progress_bps"] == 1.0
    assert adapted["max_cycles"] == 4


def test_adapt_cfg_for_sr_signal_unchanged_without_sr():
    cfg = {
        "required_positive_checks": 2,
        "direction_required_confirmations": 2,
        "direction_min_progress_bps": 5.0,
        "max_cycles": 2,
    }
    adapted = staged_entry._adapt_cfg_for_sr_signal(cfg, {})
    assert adapted == cfg
