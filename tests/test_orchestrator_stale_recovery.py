from datetime import datetime, timedelta

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
