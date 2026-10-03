"""Audit finding #1 (2026-08-16): belief_last persistence must be throttled.

evaluate_belief() runs every 1s per open position; persisting it to DB does not
need to — a state transition should always persist immediately, but repeated
writes of the same unchanged state should be throttled, mirroring the existing
`_persist_snapshots` pattern.
"""
import time

import position_telemetry_watcher as ptw


def setup_function(_):
    ptw._belief_persist_state.clear()


def test_first_evaluation_always_persists():
    assert ptw._should_persist_belief("pos-1", "uncertain", throttle_seconds=10.0) is True


def test_unchanged_state_is_throttled_within_window():
    assert ptw._should_persist_belief("pos-1", "running_strong", throttle_seconds=10.0) is True
    assert ptw._should_persist_belief("pos-1", "running_strong", throttle_seconds=10.0) is False


def test_state_transition_always_persists_immediately_even_within_window():
    assert ptw._should_persist_belief("pos-1", "running_strong", throttle_seconds=10.0) is True
    assert ptw._should_persist_belief("pos-1", "exhausted", throttle_seconds=10.0) is True


def test_unchanged_state_persists_again_after_throttle_window_elapses():
    assert ptw._should_persist_belief("pos-1", "uncertain", throttle_seconds=0.05) is True
    assert ptw._should_persist_belief("pos-1", "uncertain", throttle_seconds=0.05) is False
    time.sleep(0.06)
    assert ptw._should_persist_belief("pos-1", "uncertain", throttle_seconds=0.05) is True


def test_different_positions_are_tracked_independently():
    assert ptw._should_persist_belief("pos-1", "running_strong", throttle_seconds=10.0) is True
    assert ptw._should_persist_belief("pos-2", "running_strong", throttle_seconds=10.0) is True
    assert ptw._should_persist_belief("pos-1", "running_strong", throttle_seconds=10.0) is False
