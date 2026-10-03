"""Tests for RANGE_SCALP bidirectional re-classification."""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest
from config import Settings
from config_helpers import get_param, get_staged_entry_regime_config
import staged_entry
import run_trades


def test_range_scalp_bidirectional_flags_exist():
    s = Settings()
    assert hasattr(s, "range_scalp_bidirectional_enabled")
    assert hasattr(s, "range_scalp_short_execution_enabled")
    assert hasattr(s, "range_scalp_short_resistance_threshold")
    assert hasattr(s, "range_scalp_short_usdt_per_trade")


def test_staging_canonical_entry_and_lifecycle_modes_are_active():
    settings = Settings()

    assert settings.range_scalp_quality_model_mode == "shadow"
    assert settings.range_reversion_predictor_mode == "enforced"
    assert settings.directional_policy_mode == "active"
    assert settings.direction_volume_gate_rollout == "respect_regime_config"
    assert settings.realtime_open_rerank_mode == "active"
    assert settings.anti_monopoly_decay_mode == "active"
    assert settings.opportunity_shadow_mode == "active"
    assert settings.ug_loss_policy_mode == "active"
    assert settings.sr_pool_enabled is True
    assert settings.sr_pool_trade_enabled is False
    assert settings.sr_pool_intra_cycle_enabled is True
    assert settings.sr_invalidation_shadow_mode is False


def test_range_scalp_uses_immediate_entry_without_temporal_confirmation():
    s = Settings()
    cfg = get_staged_entry_regime_config(s, "RANGE_SCALP")
    assert cfg["direction_confirmation_mode"] == "disabled"
    assert cfg["direction_required_confirmations"] == 0
    assert cfg["required_positive_checks"] == 1
    # s105: max_cycles decoupled from a short cycle-count ceiling; max_age_min
    # (8h) is now the single source of truth for how long a breakout candidate
    # may legitimately wait.
    assert cfg["max_cycles"] == 100
    assert cfg["max_age_min"] == 480


def test_non_range_scalp_also_removes_temporal_confirmations():
    s = Settings()
    cfg = get_staged_entry_regime_config(s, "SPOT_BULL")
    assert cfg["direction_confirmation_mode"] == "disabled"
    assert cfg["direction_required_confirmations"] == 0


@pytest.mark.parametrize(
    ("dry_run", "binance_use_testnet"),
    [(True, False), (False, True)],
)
def test_legacy_override_cannot_reenable_temporal_confirmation(dry_run, binance_use_testnet):
    settings = Settings(
        dry_run=dry_run,
        binance_use_testnet=binance_use_testnet,
        staged_entry_regime_config={
            "FUTURES_SHORT": {
                "enabled": True,
                "direction_confirmation_mode": "enforced",
            },
        },
    )

    cfg = get_staged_entry_regime_config(settings, "FUTURES_SHORT")

    assert cfg["direction_confirmation_mode"] == "disabled"
    assert cfg["direction_required_confirmations"] == 0


def test_circuit_breaker_clears_orphaned_failure_count_without_active_pause(monkeypatch):
    settings = Settings(circuit_breaker_enabled=True)
    monkeypatch.setattr(run_trades, "_db_client_ref", None)
    monkeypatch.setattr(run_trades, "_consecutive_failures", 611)
    monkeypatch.setattr(run_trades, "_circuit_breaker_until", None)

    assert run_trades._circuit_breaker_check(settings) is False
    assert run_trades._consecutive_failures == 0


def test_circuit_breaker_clears_count_when_pause_expires(monkeypatch):
    settings = Settings(circuit_breaker_enabled=True)
    monkeypatch.setattr(run_trades, "_db_client_ref", None)
    monkeypatch.setattr(run_trades, "_consecutive_failures", 7)
    monkeypatch.setattr(run_trades, "_circuit_breaker_until", datetime.now(timezone.utc) - timedelta(seconds=1))

    assert run_trades._circuit_breaker_check(settings) is False
    assert run_trades._consecutive_failures == 0
    assert run_trades._circuit_breaker_until is None


def test_only_technical_order_errors_increment_circuit_breaker():
    assert run_trades._is_technical_order_failure(TimeoutError("exchange timeout")) is True
    assert run_trades._is_technical_order_failure(RuntimeError("MAX_SHORTS reached (4/4)")) is False
    assert run_trades._is_technical_order_failure(RuntimeError("Account has insufficient balance")) is False


def test_maybe_open_range_scalp_shorts_function_exists():
    assert callable(run_trades._maybe_open_range_scalp_shorts)


# ═════════════════════════════════════════════════════════════════════════════
# Dual-direction execution tests
# ═════════════════════════════════════════════════════════════════════════════

class FakeDB:
    """In-memory DB that implements the queries used by _maybe_open_range_scalp_shorts."""

    def __init__(self, ready_rows=None):
        self.ready = ready_rows or []
        self.audit = []

    def execute_query(self, query, params=None):
        q = query.strip()
        if "FROM ready_candidates" in q and "status = 'discarded'" in q:
            rows = []
            for r in self.ready:
                if r["regime_mode"] == "RANGE_SCALP" and r["status"] == "discarded":
                    if (r.get("data") or {}).get("reclassified_as_short") is None:
                        rows.append(copy.deepcopy(r))
            return rows
        return []

    def execute_update(self, query, params=None):
        q = query.strip()
        if q.startswith("UPDATE ready_candidates") and "data = COALESCE" in q:
            delta_raw, ready_id = params[0], params[1]
            delta = json.loads(delta_raw) if isinstance(delta_raw, str) else delta_raw
            for r in self.ready:
                if r["id"] == ready_id:
                    r.setdefault("data", {}).update(delta)
                    return 1
            return 0
        if q.startswith("INSERT INTO trades_audit"):
            self.audit.append(params)
            return 1
        return 0


class FakeFuturesClient:
    def __init__(self, available_usdt=5000.0):
        self.dry_run = True
        self._open_positions = {}
        self._available_usdt = available_usdt

    def get_available_usdt(self):
        return self._available_usdt

    def get_price(self, symbol):
        return 100.0

    def open_short(self, symbol, usdt_amount, sl_pct, tp_pct, leverage, **kwargs):
        from futures_client import FuturesPosition

        price = 100.0
        qty = round(usdt_amount * leverage / price, 4)
        pos = FuturesPosition(
            symbol=symbol,
            side="SHORT",
            qty=qty,
            entry_price=price,
            sl_price=round(price * (1 + sl_pct / 100), 6),
            tp_price=round(price * (1 - tp_pct / 100), 6),
            order_id="DRY_1",
            sl_order_id=None,
            tp_order_id=None,
        )
        self._open_positions[f"{symbol}_SHORT"] = pos
        return pos


def _make_settings(**overrides):
    s = Settings()
    s.directional_policy_mode = overrides.get("directional_policy_mode", "disabled")
    for k, v in overrides.items():
        setattr(s, k, v)
    # Ensure defaults used by the opener are present.
    s.range_scalp_bidirectional_enabled = overrides.get("range_scalp_bidirectional_enabled", True)
    s.range_scalp_short_execution_enabled = overrides.get("range_scalp_short_execution_enabled", True)
    s.range_scalp_short_resistance_threshold = overrides.get("range_scalp_short_resistance_threshold", 0.65)
    s.range_scalp_short_max_per_cycle = overrides.get("range_scalp_short_max_per_cycle", 1)
    s.range_scalp_short_usdt_per_trade = overrides.get("range_scalp_short_usdt_per_trade", 1000.0)
    s.range_scalp_short_sl_pct = overrides.get("range_scalp_short_sl_pct", 2.0)
    s.range_scalp_short_tp_pct = overrides.get("range_scalp_short_tp_pct", 4.0)
    s.futures_enabled = overrides.get("futures_enabled", True)
    s.futures_leverage = overrides.get("futures_leverage", 10)
    s.sr_pool_trade_enabled = overrides.get("sr_pool_trade_enabled", False)
    return s


def _make_discarded_row(
    symbol="BTCUSDT",
    sr_zone=0.9,
    support_breakdown=False,
    support_bounce=False,
    discard_reason="DIRECTION_NOT_CONFIRMED",
    ready_id=1,
):
    data = {"sr_zone_position": sr_zone}
    if support_breakdown:
        data["support_breakdown_sell"] = True
    if support_bounce:
        data["support_bounce_buy"] = True
    return {
        "id": ready_id,
        "symbol": symbol,
        "regime_mode": "RANGE_SCALP",
        "status": "discarded",
        "discarded_at": datetime.now(timezone.utc),
        "discard_reason": discard_reason,
        "signal_meta": {"indicators": {}},
        "data": data,
    }


@pytest.fixture(autouse=True)
def patch_predictor(monkeypatch):
    """Return a healthy SHORT prediction for every call."""

    def _fake_predict(symbol, client, settings, position_size_usdt, **kwargs):
        return {
            "expected_pnl": 5.0,
            "success_probability": 0.65,
            "target_price": 96.0,
            "stop_price": 102.0,
        }, {"rsi": 50.0}

    monkeypatch.setattr(run_trades.pnl_service, "predict_for_symbol", _fake_predict)


def test_opens_short_for_discarded_at_resistance():
    db = FakeDB([_make_discarded_row()])
    settings = _make_settings(futures_symbols=["BTCUSDT"])
    orders = run_trades._maybe_open_range_scalp_shorts(
        db, None, settings, FakeFuturesClient(), None, None, "RANGE_SCALP", "c1"
    )
    assert len(orders) == 1
    order = orders[0]
    assert order["symbol"] == "BTCUSDT"
    assert order["side"] == "SHORT_FUTURES"
    assert order["range_scalp_reclassified_from_long"] is True
    assert order["pnl_prediction"]["expected_pnl"] == 5.0
    # Audit record written.
    assert len(db.audit) == 1
    detail = json.loads(db.audit[0][3])
    assert detail["ready_id"] == 1
    # ready_candidates row marked as reclassified.
    assert db.ready[0]["data"]["reclassified_as_short"]["execution_enabled"] is True


def test_opens_short_at_calibrated_resistance_threshold():
    db = FakeDB([_make_discarded_row(sr_zone=0.65)])
    settings = _make_settings(futures_symbols=["BTCUSDT"])
    orders = run_trades._maybe_open_range_scalp_shorts(
        db, None, settings, FakeFuturesClient(), None, None, "RANGE_SCALP", "c1"
    )
    assert len(orders) == 1


def test_opens_short_below_resistance_without_long_structure():
    db = FakeDB([_make_discarded_row(sr_zone=0.649)])
    settings = _make_settings(futures_symbols=["BTCUSDT"])
    orders = run_trades._maybe_open_range_scalp_shorts(
        db, None, settings, FakeFuturesClient(), None, None, "RANGE_SCALP", "c1"
    )
    assert len(orders) == 1


def test_rejects_reclassification_for_confirmed_long_structure():
    db = FakeDB([_make_discarded_row(sr_zone=0.9, support_bounce=True)])
    settings = _make_settings(futures_symbols=["BTCUSDT"])
    orders = run_trades._maybe_open_range_scalp_shorts(
        db, None, settings, FakeFuturesClient(), None, None, "RANGE_SCALP", "c1"
    )
    assert orders == []
    assert json.loads(db.audit[0][3])["reason"] == "LONG_STRUCTURE_CONFIRMED"


def test_opens_short_on_support_breakdown_below_threshold():
    db = FakeDB([_make_discarded_row(sr_zone=0.5, support_breakdown=True)])
    settings = _make_settings(futures_symbols=["BTCUSDT"])
    orders = run_trades._maybe_open_range_scalp_shorts(
        db, None, settings, FakeFuturesClient(), None, None, "RANGE_SCALP", "c1"
    )
    assert len(orders) == 1


def test_sr_pool_short_skips_symbol_with_spot_position():
    class OccupiedPositionManager:
        def get_positions_count(self, symbol):
            return 1

    settings = _make_settings(
        futures_symbols=["BTCUSDT"],
        sr_pool_trade_enabled=True,
    )
    orders = run_trades._maybe_open_sr_pool_shorts(
        settings,
        FakeFuturesClient(),
        [{"symbol": "BTCUSDT", "sr_pool_profile": "A", "sr_pool_score": 0.9}],
        OccupiedPositionManager(),
    )
    assert orders == []


def test_sr_pool_shadow_accepts_missing_score(capsys):
    settings = _make_settings(sr_pool_trade_enabled=False)

    orders = run_trades._maybe_open_sr_pool_shorts(
        settings,
        None,
        [{"symbol": "BTCUSDT", "sr_pool_profile": "B", "sr_pool_score": None}],
    )

    assert orders == []
    assert "score=0.0000" in capsys.readouterr().out


def test_selection_candidate_assigns_side_aware_decision_id(monkeypatch):
    persisted = []

    class SelectionDB:
        def execute_update(self, query, params):
            persisted.append(params)
            return 1

    monkeypatch.setattr(run_trades, "_selection_context", {
        "cycle_id": "cycle-1",
        "db_client": SelectionDB(),
        "regime_mode": "RANGE_SCALP",
        "candidate_count": 0,
    })
    signal = {"symbol": "BTCUSDT", "trade_type": "futures", "expected_side": "SHORT"}
    run_trades._selection_capture_candidates([signal], {"BTCUSDT": {}}, "selected")
    assert signal["decision_id"] == "cycle-1:BTCUSDT:futures:SHORT"
    payload = json.loads(persisted[0][5])
    assert payload["decision_id"] == signal["decision_id"]
    assert payload["expected_side"] == "SHORT"


def test_skips_when_bidirectional_disabled():
    db = FakeDB([_make_discarded_row()])
    settings = _make_settings(range_scalp_bidirectional_enabled=False, futures_symbols=["BTCUSDT"])
    orders = run_trades._maybe_open_range_scalp_shorts(
        db, None, settings, FakeFuturesClient(), None, None, "RANGE_SCALP", "c1"
    )
    assert orders == []


def test_skips_when_execution_disabled():
    db = FakeDB([_make_discarded_row()])
    settings = _make_settings(
        range_scalp_short_execution_enabled=False,
        futures_symbols=["BTCUSDT"],
    )
    orders = run_trades._maybe_open_range_scalp_shorts(
        db, None, settings, FakeFuturesClient(), None, None, "RANGE_SCALP", "c1"
    )
    assert orders == []
    # Even in shadow, row should be marked reclassified and predictor data stored.
    assert db.ready[0]["data"]["reclassified_as_short"]["execution_enabled"] is False


def test_skips_when_not_in_futures_symbols():
    db = FakeDB([_make_discarded_row(symbol="ETHUSDT")])
    settings = _make_settings(futures_symbols=["BTCUSDT"])
    orders = run_trades._maybe_open_range_scalp_shorts(
        db, None, settings, FakeFuturesClient(), None, None, "RANGE_SCALP", "c1"
    )
    assert orders == []


def test_skips_when_wrong_regime():
    db = FakeDB([_make_discarded_row()])
    settings = _make_settings(futures_symbols=["BTCUSDT"])
    orders = run_trades._maybe_open_range_scalp_shorts(
        db, None, settings, FakeFuturesClient(), None, None, "SPOT_BULL", "c1"
    )
    assert orders == []


def test_skips_degenerate_prediction(monkeypatch):
    db = FakeDB([_make_discarded_row()])
    settings = _make_settings(futures_symbols=["BTCUSDT"])

    def _degenerate(*args, **kwargs):
        return {"expected_pnl": 0.0, "success_probability": 0.5, "degenerate_output": True}, {}

    monkeypatch.setattr(run_trades.pnl_service, "predict_for_symbol", _degenerate)
    orders = run_trades._maybe_open_range_scalp_shorts(
        db, None, settings, FakeFuturesClient(), None, None, "RANGE_SCALP", "c1"
    )
    assert orders == []
    assert json.loads(db.audit[0][3])["reason"] == "PREDICTOR_INVALID"


def test_rejects_non_positive_short_prediction(monkeypatch):
    db = FakeDB([_make_discarded_row()])
    settings = _make_settings(futures_symbols=["BTCUSDT"])

    def _non_positive(*args, **kwargs):
        return {
            "expected_pnl": 0.0,
            "success_probability": 0.65,
            "target_price": 96.0,
            "stop_price": 102.0,
        }, {"rsi": 50.0}

    monkeypatch.setattr(run_trades.pnl_service, "predict_for_symbol", _non_positive)
    orders = run_trades._maybe_open_range_scalp_shorts(
        db, None, settings, FakeFuturesClient(), None, None, "RANGE_SCALP", "c1"
    )
    assert orders == []
    assert json.loads(db.audit[0][3])["reason"] == "EV_NON_POSITIVE"


def test_respects_max_per_cycle():
    rows = [
        _make_discarded_row(symbol="BTCUSDT", ready_id=1),
        _make_discarded_row(symbol="ETHUSDT", ready_id=2),
    ]
    db = FakeDB(rows)
    settings = _make_settings(futures_symbols=["BTCUSDT", "ETHUSDT"], range_scalp_short_max_per_cycle=1)
    orders = run_trades._maybe_open_range_scalp_shorts(
        db, None, settings, FakeFuturesClient(), None, None, "RANGE_SCALP", "c1"
    )
    assert len(orders) == 1


def test_passes_prediction_and_indicators_to_open_short(monkeypatch):
    captured = {}

    class CapturingFuturesClient(FakeFuturesClient):
        def open_short(self, symbol, usdt_amount, sl_pct, tp_pct, leverage, **kwargs):
            captured.update(kwargs)
            return super().open_short(symbol, usdt_amount, sl_pct, tp_pct, leverage, **kwargs)

    db = FakeDB([_make_discarded_row()])
    settings = _make_settings(futures_symbols=["BTCUSDT"])
    orders = run_trades._maybe_open_range_scalp_shorts(
        db, None, settings, CapturingFuturesClient(), None, None, "RANGE_SCALP", "c1"
    )
    assert len(orders) == 1
    assert captured.get("pnl_prediction") is not None
    assert captured.get("indicators_at_entry") is not None


def test_opens_short_on_drawdown_limit_without_sr_features():
    # When SR features are missing, DIRECTION_DRAWDOWN_LIMIT acts as a breakdown proxy.
    db = FakeDB([_make_discarded_row(discard_reason="DIRECTION_DRAWDOWN_LIMIT", sr_zone=None)])
    settings = _make_settings(futures_symbols=["BTCUSDT"])
    orders = run_trades._maybe_open_range_scalp_shorts(
        db, None, settings, FakeFuturesClient(), None, None, "RANGE_SCALP", "c1"
    )
    assert len(orders) == 1


def test_skips_direction_not_confirmed_without_sr_features():
    # DIRECTION_NOT_CONFIRMED alone is not a valid breakdown proxy; it needs SR resistance
    # or DIRECTION_DRAWDOWN_LIMIT to avoid random short entries.
    db = FakeDB([_make_discarded_row(discard_reason="DIRECTION_NOT_CONFIRMED", sr_zone=None)])
    settings = _make_settings(futures_symbols=["BTCUSDT"])
    orders = run_trades._maybe_open_range_scalp_shorts(
        db, None, settings, FakeFuturesClient(), None, None, "RANGE_SCALP", "c1"
    )
    assert len(orders) == 1
