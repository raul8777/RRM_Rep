import threading
import time

from orchestrator import Orchestrator, SystemState


def test_trade_cycle_wake_event_runs_next_cycle_without_waiting_full_interval():
    orchestrator = Orchestrator()
    started = threading.Event()
    completed = threading.Event()
    calls = []

    def cycle():
        calls.append(time.monotonic())
        if len(calls) == 1:
            started.set()
        elif len(calls) == 2:
            completed.set()
        return {"next_cycle_minutes": 60, "regime_mode": "SPOT_BULL"}

    orchestrator.should_run = True
    orchestrator.state = SystemState.RUNNING
    orchestrator._trade_cycle_func = cycle
    worker = threading.Thread(target=orchestrator._run_cycle, args=(cycle, 60), daemon=True)
    worker.start()

    assert started.wait(1)
    before_wake = time.monotonic()
    orchestrator.trade_cycle_wake_event.set()
    assert completed.wait(1)
    assert calls[1] - before_wake < 1

    orchestrator.stop_all_cycles()
    worker.join(timeout=1)
