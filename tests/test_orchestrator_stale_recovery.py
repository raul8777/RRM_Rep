from datetime import datetime, timedelta
from types import SimpleNamespace

from orchestrator import Orchestrator, SystemState


class _ThreadState:
    def __init__(self, alive):
        self.alive = alive

    def is_alive(self):
        return self.alive


def _stale_orchestrator(*, alive=True, in_progress=False):
    orchestrator = Orchestrator()
    orchestrator.state = SystemState.RUNNING
    orchestrator.trade_cycle_thread = _ThreadState(alive)
    orchestrator.last_trade_cycle_at = datetime.utcnow() - timedelta(minutes=20)
    orchestrator.current_cycle_minutes = 5
    orchestrator.trade_cycle_in_progress = in_progress
    orchestrator.trade_cycle_started_at = datetime.utcnow() - timedelta(minutes=20) if in_progress else None
    return orchestrator


def test_stale_idle_cycle_requests_wake_without_restart():
    orchestrator = _stale_orchestrator(alive=True, in_progress=False)

    result = orchestrator.recover_stale_trade_cycle()

    assert result['action'] == 'wake_requested'
    assert orchestrator.trade_cycle_wake_event.is_set()


def test_stale_running_cycle_never_starts_another_worker():
    orchestrator = _stale_orchestrator(alive=True, in_progress=True)
    starts = []
    orchestrator.start_trade_cycle = lambda *args: starts.append(args)

    result = orchestrator.recover_stale_trade_cycle()

    assert result['action'] == 'recovery_skipped_cycle_running'
    assert starts == []


def test_dead_worker_restarts_once():
    orchestrator = _stale_orchestrator(alive=False)
    callback = lambda: None
    starts = []
    orchestrator._trade_cycle_func = callback
    orchestrator._trade_cycle_interval_minutes = 10
    orchestrator.start_trade_cycle = lambda func, interval: starts.append((func, interval))

    first = orchestrator.recover_stale_trade_cycle()
    second = orchestrator.recover_stale_trade_cycle()

    assert first['action'] == 'worker_restarted'
    assert second['action'] == 'restart_skipped_retry_limit'
    assert starts == [(callback, 10)]


def test_cycle_diagnostics_reports_stale_running_state():
    orchestrator = _stale_orchestrator(alive=True, in_progress=True)

    diagnostics = orchestrator.get_trade_cycle_diagnostics()

    assert diagnostics['worker_state'] == 'stale_running'
    assert diagnostics['stale'] is True
    assert diagnostics['running_minutes'] is not None


def test_proactive_liquidity_hook_runs_with_cycle_context_when_enabled():
    calls = []

    class _PortfolioManager:
        settings = SimpleNamespace(
            portfolio_proactive_liquidity_enabled=True,
            futures_enabled=False,
        )

        def reconcile_proactive_liquidity(self, **kwargs):
            calls.append(kwargs)
            return {"status": "targets_met"}

    orchestrator = Orchestrator()
    orchestrator.portfolio_manager = _PortfolioManager()

    orchestrator._reconcile_proactive_liquidity("trade_cycle_completed")

    assert calls == [{"futures_client": None, "db_client": None, "trigger": "trade_cycle_completed"}]


def test_watcher_pause_activated_after_candidate_read_blocks_fast_path(monkeypatch):
    import run_trades
    import staged_entry

    class _StopAfterOnePass:
        def is_set(self):
            return False

        def wait(self, _seconds):
            return True

    class _Db:
        paused = False

    class _Client:
        def create_market_order(self, **_kwargs):
            raise AssertionError("paused watcher must never submit an order")

    settings = SimpleNamespace(
        staged_entry_fast_path_enabled=True,
        staged_entry_regime_config={
            "RANGE_SCALP": {"enabled": True, "intra_mode": "watcher", "watcher_interval_seconds": 5},
        },
        directional_policy_mode="active",
    )
    db_client = _Db()
    client = _Client()
    comp = SimpleNamespace(
        db_client=db_client,
        settings=settings,
        binance_client=client,
        news_client=None,
        portfolio_manager=None,
        position_manager=None,
        futures_client=None,
    )
    gate_events = []
    authority = {
        "cycle_id": "qubo-cycle-1",
        "symbols_hash": "a" * 64,
        "symbols": ["BTCUSDT"],
    }

    monkeypatch.setattr("init_components.get_components", lambda: comp)
    monkeypatch.setattr(
        staged_entry,
        "get_staged_entry_regime_config",
        lambda *_args, **_kwargs: settings.staged_entry_regime_config["RANGE_SCALP"],
    )
    monkeypatch.setattr(staged_entry, "perform_intra_cycle_check", lambda *_args, **_kwargs: ["BTCUSDT"])
    monkeypatch.setattr(staged_entry, "get_qubo_authority_snapshot", lambda *_args, **_kwargs: authority)

    def _read_candidates(*_args, **_kwargs):
        db_client.paused = True
        return [{
            "id": 17,
            "symbol": "BTCUSDT",
            "qubo_cycle_id": authority["cycle_id"],
            "qubo_symbols_hash": authority["symbols_hash"],
        }]

    monkeypatch.setattr(staged_entry, "get_openable_candidates", _read_candidates)
    monkeypatch.setattr(run_trades, "_is_new_entries_paused", lambda db: db.paused)
    monkeypatch.setattr(
        run_trades,
        "_update_fast_path_gate",
        lambda _db, ready_id, passed, reason, _balance: gate_events.append((ready_id, passed, reason)),
    )
    monkeypatch.setattr("market_regime.get_regime", lambda *_args, **_kwargs: None)

    orchestrator = Orchestrator(db_client=db_client)
    orchestrator.state = SystemState.RUNNING
    orchestrator.should_run = True
    orchestrator.current_regime_mode = "RANGE_SCALP"
    orchestrator.staged_entry_intra_stop = _StopAfterOnePass()
    orchestrator.can_execute_trades = lambda: True

    orchestrator._staged_entry_intra_loop()

    assert db_client.paused is True
    assert gate_events == [(17, False, "PAUSED_DB")]
