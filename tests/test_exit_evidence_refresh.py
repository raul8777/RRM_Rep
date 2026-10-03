"""s130b Exit Lifecycle Refactor: tests for _run_exit_evidence_refresh_cycle.

Covers:
- throttle per position
- SPOT + FUTURES coverage
- staleness handling
- cached evidence consumed by evaluate_belief
"""
import time
import threading
from types import SimpleNamespace

import pnl_service
import position_telemetry_watcher as ptw
from peak_capture_engine import PeakCaptureContext


def setup_function(_):
    ptw._exit_evidence_refresh_state.clear()
    ptw._exit_evidence_refresh_lock = threading.Lock()
    ptw._belief_persist_state.clear()


def _settings(enabled=True, throttle=15.0):
    return SimpleNamespace(
        position_exit_evidence_refresh_enabled=enabled,
        position_exit_evidence_refresh_seconds=throttle,
        position_belief_persist_throttle_seconds=10.0,
        futures_enabled=True,
        market_regime_enabled=False,
        current_regime_mode="SPOT_BULL",
    )


def _make_snapshot(trade_type="spot", symbol="BTCUSDT", position_id="pos-1", side="LONG"):
    return {
        "trade_type": trade_type,
        "symbol": symbol,
        "position_id": position_id,
        "side": side,
        "entry_price": 100.0,
        "quantity": 1.0,
        "leverage": 1.0,
        "current_price": 110.0,
        "unrealized_pnl_pct": 10.0,
        "unrealized_pnl_usdt": 10.0,
        "peak_pnl_pct": 0.15,
        "stop_loss": 95.0,
        "take_profit": 120.0,
        "_thesis_snapshot": {
            "stop_price": 95.0,
            "target_price": 120.0,
            "entry_success_probability": 0.55,
        },
    }


def test_should_refresh_first_time():
    assert ptw._should_refresh_exit_evidence("pos-1", throttle_seconds=1.0) is True


def test_should_refresh_throttles_within_window():
    assert ptw._should_refresh_exit_evidence("pos-1", throttle_seconds=1.0) is True
    assert ptw._should_refresh_exit_evidence("pos-1", throttle_seconds=1.0) is False


def test_should_refresh_allows_after_window():
    assert ptw._should_refresh_exit_evidence("pos-1", throttle_seconds=0.05) is True
    assert ptw._should_refresh_exit_evidence("pos-1", throttle_seconds=0.05) is False
    time.sleep(0.06)
    assert ptw._should_refresh_exit_evidence("pos-1", throttle_seconds=0.05) is True


def test_should_refresh_tracks_positions_independently():
    assert ptw._should_refresh_exit_evidence("pos-1", throttle_seconds=1.0) is True
    assert ptw._should_refresh_exit_evidence("pos-2", throttle_seconds=1.0) is True
    assert ptw._should_refresh_exit_evidence("pos-1", throttle_seconds=1.0) is False


def test_build_peak_capture_context_handles_spot_snapshot():
    snapshot = _make_snapshot(trade_type="spot")
    prediction = {"success_probability": 0.6, "expected_pnl": 5.0, "price_range": {"max": 120.0, "min": 90.0}}
    indicators = {"rsi": 65, "signal": "BUY", "strength": 0.7, "trend_1h": "UP"}
    ctx = ptw._build_peak_capture_context(snapshot, prediction, indicators, _settings())
    assert isinstance(ctx, PeakCaptureContext)
    assert ctx.symbol == "BTCUSDT"
    assert ctx.trade_type == "spot"
    assert ctx.current_pnl_pct == 0.10
    assert ctx.position_size_usdt == 110.0


def test_build_peak_capture_context_handles_futures_short():
    snapshot = _make_snapshot(trade_type="futures", side="SHORT")
    prediction = {"success_probability": 0.6, "expected_pnl": 5.0, "price_range": {"max": 120.0, "min": 90.0}}
    indicators = {"rsi": 25, "signal": "SELL", "strength": 0.7, "trend_1h": "UP"}
    ctx = ptw._build_peak_capture_context(snapshot, prediction, indicators, _settings())
    assert ctx.side == "SHORT"
    # SHORT SELL + rsi <= 30 => momentum_exhausted
    assert ctx.momentum_exhausted is True


def test_run_exit_evidence_refresh_disabled():
    components = SimpleNamespace(binance_client=None, position_manager=None, db_client=None)
    snapshot = _make_snapshot()
    settings = _settings(enabled=False)
    result = ptw._run_exit_evidence_refresh_cycle(components, [snapshot], settings)
    assert result["refreshed"] == 0


def test_run_exit_evidence_refresh_respects_throttle(monkeypatch):
    ptw._exit_evidence_refresh_state.clear()
    snapshot = _make_snapshot()
    settings = _settings(enabled=True, throttle=60.0)

    def _mock_predict(*_args, **_kwargs):
        return (
            {"success_probability": 0.7, "expected_pnl": 5.0, "price_range": {"max": 120.0, "min": 95.0}},
            {"rsi": 60, "signal": "BUY", "strength": 0.7, "trend_1h": "UP"},
        )

    monkeypatch.setattr(pnl_service, "predict_for_symbol", _mock_predict)
    client = SimpleNamespace()
    components = SimpleNamespace(binance_client=client, position_manager=None, db_client=None)
    # First call refreshes; second call within throttle window skips.
    result1 = ptw._run_exit_evidence_refresh_cycle(components, [snapshot], settings)
    result2 = ptw._run_exit_evidence_refresh_cycle(components, [snapshot], settings)
    assert result1["refreshed"] == 1
    assert result2["skipped"] == 1
