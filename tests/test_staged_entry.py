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

        # SELECT id FROM ready_candidates WHERE symbol=... OR SELECT * FROM ...
        if q.startswith("SELECT id FROM ready_candidates") or q.startswith(
            "SELECT * FROM ready_candidates WHERE symbol=%s AND trade_type=%s AND regime_mode=%s"
        ):
            sym, tt, reg = params[:3]
            for r in self.ready:
                if r["symbol"] == sym and r["trade_type"] == tt and r["regime_mode"] == reg and r["status"] in ("ready", "validated"):
                    return [copy.deepcopy(r)]
            return []

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
        # Extract SET assignments and WHERE id param.
        set_part = query[query.upper().find("SET ") + 4 : query.upper().rfind(" WHERE ")]
        assignments = [a.strip() for a in set_part.split(",")]
        row_id = params[-1]

        row = next((r for r in self.ready if r["id"] == row_id), None)
        if row is None:
            return 0

        param_iter = iter(params[:-1])
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
    assert rows[0]["positive_checks"] == 1
    assert rows[0]["total_checks"] == 1
    assert rows[0]["data"]["cycles_active"] == 1

    checks = fake_db.checks
    assert len(checks) == 1
    assert checks[0]["check_type"] == "inter"
    assert checks[0]["positive"] is True
    assert checks[0]["decision"] == "positive"


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

    # First inter-cycle positive check should move to validated (required=2).
    result = staged_entry.evaluate_inter_cycle(
        fake_db, None, settings, "SPOT_BULL", top, {}, meta, cycle_id="c2", news_client=None,
    )
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
    btc_row["data"]["policy"].update({"expected_pnl": 1.0, "quality_score": 0.5, "success_probability": 0.6})
    eth_row["data"]["policy"].update({"expected_pnl": 2.0, "quality_score": 0.4, "success_probability": 0.7})

    openable = staged_entry.get_openable_candidates(fake_db, "SPOT_BULL", ["BTCUSDT"], settings=settings)
    assert len(openable) == 1
    assert openable[0]["symbol"] == "ETHUSDT"
    assert eth_row["data"]["global_validated_rank"] == 1
    assert eth_row["data"]["global_validated_universe_size"] == 2


def test_perform_intra_cycle_check_validates(fake_db, settings):
    meta = {"BTCUSDT": _make_signal("BTCUSDT")}
    top = [_make_signal("BTCUSDT")]
    staged_entry.stage_ready_candidates(top, {}, meta, "SPOT_BULL", fake_db, settings=settings)

    validated = staged_entry.perform_intra_cycle_check(fake_db, None, settings, "SPOT_BULL", news_client=None)
    assert "BTCUSDT" in validated
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
