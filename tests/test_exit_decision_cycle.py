"""s130b Exit Lifecycle Refactor: tests for _run_exit_decision_cycle.

Covers:
- enabled by default in staging
- explicitly disabled fallback
- lock acquisition
- integration call path (without executing real trades)
"""
from types import SimpleNamespace

from config import Settings
import position_telemetry_watcher as ptw


def _settings(exit_decision_enabled=False, futures_enabled=True, market_regime_enabled=False):
    return SimpleNamespace(
        position_exit_decision_loop_enabled=exit_decision_enabled,
        position_exit_evidence_refresh_enabled=False,
        futures_enabled=futures_enabled,
        market_regime_enabled=market_regime_enabled,
        current_regime_mode="SPOT_BULL",
        dry_run=True,
        allow_real_trading=False,
    )


def _make_snapshot(symbol="BTCUSDT", position_id="pos-1", trade_type="spot"):
    return {
        "symbol": symbol,
        "position_id": position_id,
        "trade_type": trade_type,
        "side": "LONG",
        "current_price": 110.0,
    }


def test_exit_decision_enabled_by_default_staging():
    settings = Settings()
    assert settings.position_exit_decision_loop_enabled is True
    assert settings.position_exit_evidence_refresh_enabled is True
    assert settings.position_exit_evidence_refresh_seconds == 15.0


def test_exit_decision_explicitly_disabled():
    components = SimpleNamespace(binance_client=None, position_manager=None)
    summary = ptw._run_exit_decision_cycle(components, [_make_snapshot()], _settings())
    assert summary["closed_spot"] == []
    assert summary["closed_futures"] == []
    assert summary["error"] is None


def test_exit_decision_requires_client_and_position_manager():
    components = SimpleNamespace(binance_client=None, position_manager=None)
    settings = _settings(exit_decision_enabled=True)
    summary = ptw._run_exit_decision_cycle(components, [_make_snapshot()], settings)
    assert summary["error"] == "missing client or position_manager"


def test_exit_decision_accepts_empty_snapshots():
    components = SimpleNamespace(binance_client=object(), position_manager=object())
    settings = _settings(exit_decision_enabled=True)
    summary = ptw._run_exit_decision_cycle(components, [], settings)
    assert summary["closed_spot"] == []
    assert summary["closed_futures"] == []


def test_get_exit_decision_metrics_exposes_counters():
    metrics = ptw.get_exit_decision_metrics()
    assert isinstance(metrics, dict)
    assert metrics.get("name") == "position_exit_decision"
    assert "evaluated" in metrics
    assert "closed_spot" in metrics
    assert "closed_futures" in metrics
    assert "errors" in metrics
    assert "last_at" in metrics


def test_exit_decision_records_stop_loss_close(monkeypatch):
    """s130b: when _check_sl_tp_positions signals a stop-loss, the cycle
    must return it and update the in-memory metrics counters."""
    closed = [
        {"symbol": "BTCUSDT", "close_reason": "stop_loss (-5.00%, sl=100.000000)"}
    ]

    def _mock_check(*args, **kwargs):
        return closed

    import run_trades

    monkeypatch.setattr(run_trades, "_check_sl_tp_positions", _mock_check)

    class _FakeClient:
        def get_symbol_price(self, *args, **kwargs):
            return 95.0

    class _FakePM:
        def get_all_positions(self):
            return {"BTCUSDT": [{"quantity": 0.1, "entry_price": 110.0, "stop_loss": 100.0}]}

    components = SimpleNamespace(
        binance_client=_FakeClient(),
        position_manager=_FakePM(),
        news_client=None,
        db_client=None,
    )
    settings = _settings(exit_decision_enabled=True)

    # Reset metrics for a clean assertion.
    with ptw._exit_decision_metrics_lock:
        ptw._exit_decision_metrics["evaluated"] = 0
        ptw._exit_decision_metrics["closed_spot"] = 0
        ptw._exit_decision_metrics["closed_futures"] = 0
        ptw._exit_decision_metrics["errors"] = 0
        ptw._exit_decision_metrics["last_at"] = None

    summary = ptw._run_exit_decision_cycle(components, [_make_snapshot()], settings)

    assert summary["closed_spot"] == closed
    assert summary["closed_futures"] == []
    assert summary["error"] is None

    metrics = ptw.get_exit_decision_metrics()
    assert metrics["evaluated"] >= 1
    assert metrics["closed_spot"] == 1
    assert metrics["closed_futures"] == 0
    assert metrics["errors"] == 0
    assert metrics["last_at"] is not None
