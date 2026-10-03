"""
Orchestrator/Scheduler Module

Coordinates all agents and manages system state including manual control (pause/stop/start).
"""

import logging
import threading
import time
import json
from typing import Dict, Optional, Callable
from datetime import datetime
from enum import Enum
from binance_truth_guard import is_truth_critical
import staged_entry
import position_telemetry_watcher

logger = logging.getLogger(__name__)


def _request_smart_recheck(db_client, reason: str, context: Optional[Dict] = None) -> None:
    """Emit an event-driven SMART health recheck request through runtime state."""
    if not db_client:
        return
    try:
        payload = json.dumps({
            "requested_at": datetime.utcnow().isoformat() + "Z",
            "source": "orchestrator",
            "reason": str(reason or "orchestrator_event"),
            "context": context or {},
        })
        db_client.execute_update(
            "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
            ("health_results_recheck_request", payload, payload),
        )
    except Exception as e:
        logger.debug(f"[SMART-HEALTH] orchestrator trigger failed: {e}")


class SystemState(Enum):
    """System states for manual control."""
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


class Orchestrator:
    """Orchestrates all trading agents and manages system state."""
    
    def __init__(self, db_client=None):
        """
        Initialize Orchestrator.
        
        Args:
            db_client: Database client for state persistence
        """
        self.db_client = db_client
        self.state = SystemState.STOPPED
        self.state_changed_at = datetime.utcnow()
        self.state_changed_by = "system"
        self.state_reason = "Initial state"
        
        # Agent references
        self.portfolio_manager = None
        self.position_manager = None
        self.backtester = None
        
        # Background threads
        self.trade_cycle_thread = None
        self.analysis_cycle_thread = None
        self.learning_cycle_thread = None
        self.staged_entry_intra_thread = None
        self.staged_entry_intra_stop: Optional[threading.Event] = None
        self.hard_risk_monitor_thread = None
        self.hard_risk_monitor_stop: Optional[threading.Event] = None
        self.hard_risk_monitor_last_at: Optional[datetime] = None
        self.hard_risk_monitor_last_error: Optional[str] = None
        # s116: high-frequency position telemetry watcher
        self.position_telemetry_thread = None
        self.position_telemetry_stop: Optional[threading.Event] = None
        self.position_telemetry_last_at: Optional[datetime] = None
        self.position_telemetry_last_error: Optional[str] = None
        # S105g: real-time Binance User Data Stream (futures + spot)
        self.event_stream = None
        self.trade_cycle_wake_event = threading.Event()
        self._trade_cycle_lock = threading.RLock()
        self._trade_cycle_func: Optional[Callable] = None
        self._trade_cycle_interval_minutes: int = 5
        self.trade_cycle_in_progress = False
        self.trade_cycle_started_at: Optional[datetime] = None
        self.trade_cycle_completed_at: Optional[datetime] = None
        self.trade_cycle_last_duration_seconds: Optional[float] = None
        self.trade_cycle_phase = "idle"
        self.trade_cycle_recovery_attempts = 0
        self.trade_cycle_last_recovery_action: Optional[str] = None
        self.trade_cycle_last_recovery_at: Optional[datetime] = None
        self.should_run = False
        self.last_trade_cycle_at: Optional[datetime] = None
        self.current_cycle_minutes: int = 5  # updated dynamically per regime
        self.current_regime_mode: str = 'SPOT_BULL'  # updated dynamically per cycle
        self.consecutive_cycle_failures: int = 0
        self.last_cycle_error: Optional[str] = None
        self.last_cycle_error_at: Optional[datetime] = None
        self.binance_truth_drift_cycles: int = 0
        # s110: counts consecutive clean truth checks while PAUSED for
        # binance_truth_drift_critical, so we can auto-resume once resolved instead of
        # staying paused indefinitely until someone restarts the container (observed
        # 2026-08-02: 13h46min paused with a real open position unmanaged).
        self.binance_truth_recovery_cycles: int = 0
        
        # Callbacks
        self.on_state_change: Optional[Callable] = None
        
        # Load state from database if available
        self._load_state_from_db()
        
        logger.info(f"Orchestrator initialized with state: {self.state.value}")
    
    def _load_state_from_db(self) -> None:
        """Load system state from database."""
        if not self.db_client:
            return
        
        try:
            query = "SELECT state, changed_at, changed_by, reason FROM system_state ORDER BY changed_at DESC LIMIT 1"
            results = self.db_client.execute_query(query)
            
            if results:
                row = results[0]
                self.state = SystemState(row['state'])
                self.state_changed_at = row['changed_at']
                self.state_changed_by = row['changed_by']
                self.state_reason = row['reason']
                logger.info(f"Loaded state from database: {self.state.value}")
        except Exception as e:
            logger.error(f"Failed to load state from database: {e}")
    
    def _save_state_to_db(self) -> None:
        """Save system state to database."""
        if not self.db_client:
            return
        
        try:
            query = """
                INSERT INTO system_state (state, changed_at, changed_by, reason)
                VALUES (%s, %s, %s, %s)
            """
            self.db_client.execute_update(
                query,
                (self.state.value, self.state_changed_at, self.state_changed_by, self.state_reason)
            )
            logger.info(f"State saved to database: {self.state.value}")
        except Exception as e:
            logger.error(f"Failed to save state to database: {e}")
    
    def set_portfolio_manager(self, portfolio_manager) -> None:
        """Set Portfolio Manager instance."""
        self.portfolio_manager = portfolio_manager
    
    def set_position_manager(self, position_manager) -> None:
        """Set Position Manager instance."""
        self.position_manager = position_manager
    
    def set_backtester(self, backtester) -> None:
        """Set Backtester instance."""
        self.backtester = backtester
    
    def get_state(self) -> Dict:
        """
        Get current system state.
        
        Returns:
            Dictionary with state information
        """
        return {
            "state": self.state.value,
            "changed_at": self.state_changed_at.isoformat(),
            "changed_by": self.state_changed_by,
            "reason": self.state_reason,
            "portfolio_status": self._get_portfolio_status() if self.portfolio_manager else None,
            "position_status": self._get_position_status() if self.position_manager else None
        }
    
    def _get_portfolio_status(self) -> Dict:
        """Get Portfolio Manager status."""
        try:
            return self.portfolio_manager.get_portfolio_summary()
        except Exception as e:
            logger.error(f"Failed to get portfolio status: {e}")
            return {}
    
    def _get_position_status(self) -> Dict:
        """Get Position Manager status."""
        try:
            return self.position_manager.get_position_summary()
        except Exception as e:
            logger.error(f"Failed to get position status: {e}")
            return {}
    
    def _evaluate_binance_truth_drift(self) -> None:
        """s110: pause on sustained critical Binance/runtime drift, and auto-resume once
        it clears — both gated by 2 consecutive cycles to avoid flapping on a single
        transient read.

        Before this method existed, only the pause half was implemented: once
        `binance_truth_drift_critical` fired, the system stayed PAUSED indefinitely
        until a human or an unrelated restart brought it back to RUNNING. On
        2026-08-02 that left the system paused for 13h46min with a real open position
        receiving no active exit management. Positions still get monitor-only SL/TP
        protection while PAUSED (see `can_monitor_positions()` in `_run_cycle`), so
        auto-resuming once the drift check comes back clean does not leave an
        unmanaged position exposed in the meantime — it just stops the system from
        staying frozen for hours after Binance and the bot's own state have already
        reconciled.
        """
        try:
            from init_components import get_components
            settings = getattr(get_components(), "settings", None)
            if settings is not None and bool(getattr(settings, "dry_run", False)):
                self.binance_truth_drift_cycles = 0
                self.binance_truth_recovery_cycles = 0
                return
        except Exception:
            pass

        if is_truth_critical(self.db_client, max_age_seconds=900):
            self.binance_truth_drift_cycles += 1
            self.binance_truth_recovery_cycles = 0
            logger.warning(
                "[BinanceTruth] critical drift cycle %s/2",
                self.binance_truth_drift_cycles,
            )
            if self.binance_truth_drift_cycles >= 2:
                self.pause(changed_by="system", reason="binance_truth_drift_critical")
            return
        self.binance_truth_drift_cycles = 0
        # Only auto-resume pauses this exact mechanism caused — never a manual/operator
        # pause or any other reason string.
        if self.state != SystemState.PAUSED or self.state_reason != "binance_truth_drift_critical":
            self.binance_truth_recovery_cycles = 0
            return
        self.binance_truth_recovery_cycles += 1
        logger.info(
            "[BinanceTruth] clean check %s/2 while paused for drift",
            self.binance_truth_recovery_cycles,
        )
        if self.binance_truth_recovery_cycles >= 2:
            logger.warning(
                "[BinanceTruth] drift resolved for %s consecutive cycles — auto-resuming",
                self.binance_truth_recovery_cycles,
            )
            self.start(changed_by="system", reason="binance_truth_resolved")
            self.binance_truth_recovery_cycles = 0

    def pause(self, changed_by: str = "user", reason: str = "Manual pause") -> bool:
        """
        Pause the system (stop new orders, monitor open positions).
        
        Args:
            changed_by: Who initiated the state change
            reason: Reason for the state change
            
        Returns:
            True if successful, False otherwise
        """
        if self.state == SystemState.PAUSED:
            logger.warning("System already paused")
            return False
        
        old_state = self.state
        self.state = SystemState.PAUSED
        self.state_changed_at = datetime.utcnow()
        self.state_changed_by = changed_by
        self.state_reason = reason
        
        self._save_state_to_db()
        
        if self.on_state_change:
            self.on_state_change(old_state, self.state)
        
        logger.info(f"System paused by {changed_by}: {reason}")
        _request_smart_recheck(self.db_client, "orchestrator_pause", {"changed_by": changed_by, "reason": reason})
        return True
    
    def stop(self, changed_by: str = "user", reason: str = "Manual stop") -> bool:
        """
        Stop the system (stop all, keep open positions).
        
        Args:
            changed_by: Who initiated the state change
            reason: Reason for the state change
            
        Returns:
            True if successful, False otherwise
        """
        if self.state == SystemState.STOPPED:
            logger.warning("System already stopped")
            return False
        
        old_state = self.state
        self.state = SystemState.STOPPED
        self.state_changed_at = datetime.utcnow()
        self.state_changed_by = changed_by
        self.state_reason = reason
        
        self.should_run = False
        
        self._save_state_to_db()
        
        if self.on_state_change:
            self.on_state_change(old_state, self.state)
        
        logger.info(f"System stopped by {changed_by}: {reason}")
        _request_smart_recheck(self.db_client, "orchestrator_stop", {"changed_by": changed_by, "reason": reason})
        return True
    
    def start(self, changed_by: str = "user", reason: str = "Manual start") -> bool:
        """
        Start the system (recalculate everything).
        
        Args:
            changed_by: Who initiated the state change
            reason: Reason for the state change
            
        Returns:
            True if successful, False otherwise
        """
        if self.state == SystemState.RUNNING:
            logger.warning("System already running")
            return False
        
        old_state = self.state
        self.state = SystemState.RUNNING
        self.state_changed_at = datetime.utcnow()
        self.state_changed_by = changed_by
        self.state_reason = reason
        
        self.should_run = True
        
        self._save_state_to_db()
        
        if self.on_state_change:
            self.on_state_change(old_state, self.state)
        
        logger.info(f"System started by {changed_by}: {reason}")
        _request_smart_recheck(self.db_client, "orchestrator_start", {"changed_by": changed_by, "reason": reason})
        return True
    
    def is_running(self) -> bool:
        """Check if system is in RUNNING state."""
        return self.state == SystemState.RUNNING
    
    def is_paused(self) -> bool:
        """Check if system is in PAUSED state."""
        return self.state == SystemState.PAUSED
    
    def is_stopped(self) -> bool:
        """Check if system is in STOPPED state."""
        return self.state == SystemState.STOPPED
    
    def can_execute_trades(self) -> bool:
        """
        Check if trades can be executed based on current state.
        
        Returns:
            True if trades can be executed, False otherwise
        """
        return self.state == SystemState.RUNNING
    
    def can_monitor_positions(self) -> bool:
        """
        Check if positions can be monitored based on current state.
        
        Returns:
            True if positions can be monitored, False otherwise
        """
        return self.state in [SystemState.RUNNING, SystemState.PAUSED]
    
    def start_trade_cycle(self, cycle_func: Callable, interval_minutes: int = 5) -> None:
        """
        Start the trade cycle in a background thread.
        
        Args:
            cycle_func: Function to run in the cycle
            interval_minutes: Interval between cycles in minutes
        """
        with self._trade_cycle_lock:
            if self.trade_cycle_thread and self.trade_cycle_thread.is_alive():
                logger.warning("Trade cycle already running")
                return
            self.should_run = True
            self.trade_cycle_wake_event.clear()
            self._trade_cycle_func = cycle_func
            self._trade_cycle_interval_minutes = interval_minutes
            self.trade_cycle_phase = "starting"
            self.trade_cycle_thread = threading.Thread(
                target=self._run_cycle,
                args=(cycle_func, interval_minutes),
                daemon=True
            )
            self.trade_cycle_thread.start()
        logger.info(f"Trade cycle started with {interval_minutes} minute interval")
        self._start_staged_entry_intra_cycle()
        self._start_hard_risk_monitor()
        self._start_position_telemetry()
        # S105g: defer the event stream a few seconds so its websocket handshake
        # doesn't compete for the GIL with the heavy synchronous startup burst
        # (futures position sync, regime detection, etc. on trade_cycle_thread)
        # right after boot — python-binance's socket manager has a fixed internal
        # init timeout and was observed failing under that startup contention in
        # production, even though it connects cleanly once things settle.
        threading.Thread(target=self._delayed_start_event_stream, daemon=True).start()
    
    def start_analysis_cycle(self, cycle_func: Callable, interval_minutes: int = 15) -> None:
        """
        Start the analysis cycle in a background thread.
        
        Args:
            cycle_func: Function to run in the cycle
            interval_minutes: Interval between cycles in minutes
        """
        if self.analysis_cycle_thread and self.analysis_cycle_thread.is_alive():
            logger.warning("Analysis cycle already running")
            return

        self.should_run = True
        self.analysis_cycle_thread = threading.Thread(
            target=self._run_cycle,
            args=(cycle_func, interval_minutes),
            daemon=True
        )
        self.analysis_cycle_thread.start()
        logger.info(f"Analysis cycle started with {interval_minutes} minute interval")

    def start_learning_cycle(self, interval_minutes: int = 90) -> None:
        """
        Start the self-learning cycle in a background thread.

        Args:
            interval_minutes: Interval between learning runs (default 90 min)
        """
        if self.learning_cycle_thread and self.learning_cycle_thread.is_alive():
            logger.warning("Learning cycle already running")
            return

        self.should_run = True
        self.learning_cycle_thread = threading.Thread(
            target=self._run_cycle,
            args=(self._learning_execute_once, interval_minutes),
            daemon=True
        )
        self.learning_cycle_thread.start()
        logger.info(f"Learning cycle started with {interval_minutes} minute interval")

    @staticmethod
    def _learning_execute_once() -> None:
        """Run one LearningEngine cycle and invalidate the predictor cache."""
        try:
            from init_components import get_components as _gc_lrn
            _db = _gc_lrn().db_client if _gc_lrn() else None
            if is_truth_critical(_db, max_age_seconds=1800):
                logger.warning("[LearningCycle] skipped: binance truth status is critical")
                return
            from learning_engine import get_learning_engine
            from pnl_service import get_predictor
            result = get_learning_engine().run(min_trades=5, lookback_days=30)
            if result.get("status") == "completed":
                logger.info(
                    f"LearningEngine: {result['trades_analyzed']} trades, "
                    f"accuracy={result.get('accuracy', {}).get('prediction_accuracy', 0):.2%}, "
                    f"updated={result.get('symbols_updated', [])}"
                )
                get_predictor().invalidate_cache()
                # P2.2: Persist last_learning_run to trading_runtime_state for dashboard visibility
                try:
                    import json as _json
                    from init_components import get_components as _gc
                    _db = _gc().db_client
                    if _db:
                        _payload = _json.dumps({
                            'ts': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
                            'trigger': 'scheduled',
                            'trades_analyzed': result.get('trades_analyzed', 0),
                            'symbols': result.get('symbols_updated', []),
                        })
                        _db.execute_update(
                            "INSERT INTO trading_runtime_state (key, value, updated_at) "
                            "VALUES ('last_learning_run', %s, NOW()) "
                            "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                            (_payload, _payload)
                        )
                except Exception as _rs_err:
                    logger.debug(f"[LearningCycle] runtime_state persist failed (non-fatal): {_rs_err}")
        except Exception as e:
            import error_collector as ec
            logger.warning(f"LearningEngine cycle failed (non-critical): {e}")
            ec.add_error(ec.SOURCE_SYSTEM, ec.LEVEL_WARNING, f"LearningEngine cycle failed: {e}")
    
    def get_trade_cycle_diagnostics(self, now: Optional[datetime] = None) -> Dict:
        now = now or datetime.utcnow()
        with self._trade_cycle_lock:
            thread_alive = bool(self.trade_cycle_thread and self.trade_cycle_thread.is_alive())
            last_cycle_at = self.last_trade_cycle_at
            started_at = self.trade_cycle_started_at
            threshold_minutes = max(1.0, float(self.current_cycle_minutes) * 1.5)
            age_minutes = None
            if last_cycle_at:
                age_minutes = max(0.0, (now - last_cycle_at).total_seconds() / 60.0)
            running_minutes = None
            if self.trade_cycle_in_progress and started_at:
                running_minutes = max(0.0, (now - started_at).total_seconds() / 60.0)
            stale = age_minutes is not None and age_minutes > threshold_minutes
            if not thread_alive:
                worker_state = "dead"
            elif self.trade_cycle_in_progress:
                worker_state = "stale_running" if stale else "running"
            else:
                worker_state = "idle"
            return {
                "worker_state": worker_state,
                "thread_alive": thread_alive,
                "in_progress": self.trade_cycle_in_progress,
                "phase": self.trade_cycle_phase,
                "last_cycle_at": last_cycle_at,
                "started_at": started_at,
                "completed_at": self.trade_cycle_completed_at,
                "age_minutes": age_minutes,
                "running_minutes": running_minutes,
                "stale_threshold_minutes": threshold_minutes,
                "stale": stale,
                "last_duration_seconds": self.trade_cycle_last_duration_seconds,
                "last_error": self.last_cycle_error,
                "last_error_at": self.last_cycle_error_at,
                "recovery_attempts": self.trade_cycle_recovery_attempts,
                "last_recovery_action": self.trade_cycle_last_recovery_action,
                "last_recovery_at": self.trade_cycle_last_recovery_at,
            }

    def recover_stale_trade_cycle(self) -> Dict:
        with self._trade_cycle_lock:
            diagnostics = self.get_trade_cycle_diagnostics()
            self.trade_cycle_last_recovery_at = datetime.utcnow()
            if diagnostics["worker_state"] == "idle":
                if not self.can_monitor_positions():
                    action = "wake_skipped_system_stopped"
                else:
                    self.trade_cycle_wake_event.set()
                    action = "wake_requested"
            elif diagnostics["worker_state"] in ("running", "stale_running"):
                action = "recovery_skipped_cycle_running"
            elif self.state != SystemState.RUNNING:
                action = "restart_skipped_system_not_running"
            elif not self._trade_cycle_func:
                action = "restart_skipped_missing_callback"
            elif self.trade_cycle_recovery_attempts >= 1:
                action = "restart_skipped_retry_limit"
            else:
                self.trade_cycle_recovery_attempts += 1
                self.start_trade_cycle(self._trade_cycle_func, self._trade_cycle_interval_minutes)
                action = "worker_restarted"
            self.trade_cycle_last_recovery_action = action
            return {"action": action, **self.get_trade_cycle_diagnostics()}

    def _run_cycle(self, cycle_func: Callable, interval_minutes: int) -> None:
        """
        Run a cycle function periodically.
        interval_minutes is used as default; trade cycle may override it dynamically
        from the returned result's 'next_cycle_minutes' key.

        When PAUSED: trade cycle still runs in monitor-only mode (SL/TP protection)
        by passing monitor_only=True. New trade entries are blocked inside execute_once.
        """
        first_run = True
        _next_interval = interval_minutes
        _is_trade_cycle = False  # determined on first run
        while self.should_run:
            if not first_run:
                if cycle_func == getattr(self, '_trade_cycle_func', None):
                    self.trade_cycle_wake_event.wait(max(1, _next_interval) * 60)
                    self.trade_cycle_wake_event.clear()
                else:
                    time.sleep(max(1, _next_interval) * 60)
            first_run = False
            _cycle_ok = False
            _cycle_executed = False
            _cycle_error = None
            _is_trade_worker = cycle_func == getattr(self, '_trade_cycle_func', None)
            if _is_trade_worker:
                with self._trade_cycle_lock:
                    self.trade_cycle_in_progress = True
                    self.trade_cycle_started_at = datetime.utcnow()
                    self.trade_cycle_phase = "executing"
            try:
                if self.can_execute_trades():
                    _cycle_executed = True
                    result = cycle_func()
                    # Dynamic cycle: trade cycle may return next_cycle_minutes
                    if isinstance(result, dict) and 'next_cycle_minutes' in result:
                        _is_trade_cycle = True
                        _next_interval = int(result['next_cycle_minutes'])
                        if cycle_func == getattr(self, '_trade_cycle_func', None):
                            self.current_cycle_minutes = _next_interval
                            if 'regime_mode' in result:
                                self.current_regime_mode = result['regime_mode']
                    else:
                        _next_interval = interval_minutes
                    _cycle_ok = True
                elif self.can_monitor_positions():
                    # PAUSED: run trade cycle in monitor-only mode to protect open positions
                    _is_trade_cycle = cycle_func == getattr(self, '_trade_cycle_func', None)
                    if _is_trade_cycle:
                        try:
                            result = cycle_func(monitor_only=True)
                            logger.debug("System paused — monitor-only SL/TP cycle executed")
                            _cycle_ok = True
                        except TypeError:
                            # cycle_func does not accept monitor_only kwarg (e.g. analysis cycle)
                            logger.debug("System paused, skipping non-trade cycle")
                            _cycle_ok = True
                    else:
                        logger.debug("System paused, skipping non-trade cycle")
                        _cycle_ok = True
                else:
                    logger.debug("System stopped, skipping cycle")
                    _cycle_ok = True
            except Exception as e:
                import traceback
                logger.error(f"Error in cycle: {e}\n{traceback.format_exc()}")
                _cycle_error = str(e)
            else:
                if _cycle_ok and cycle_func == getattr(self, '_trade_cycle_func', None):
                    self.last_trade_cycle_at = datetime.utcnow()
                    _request_smart_recheck(
                        self.db_client,
                        "trade_cycle_completed",
                        {
                            "last_trade_cycle_at": self.last_trade_cycle_at.isoformat(),
                            "cycle_minutes": self.current_cycle_minutes,
                            "regime_mode": self.current_regime_mode,
                        },
                    )
                    if _cycle_executed:
                        self._reconcile_proactive_liquidity("trade_cycle_completed")
            finally:
                if _is_trade_worker:
                    with self._trade_cycle_lock:
                        completed_at = datetime.utcnow()
                        self.trade_cycle_completed_at = completed_at
                        if self.trade_cycle_started_at:
                            self.trade_cycle_last_duration_seconds = round(
                                (completed_at - self.trade_cycle_started_at).total_seconds(), 3
                            )
                        self.trade_cycle_in_progress = False
                        self.trade_cycle_phase = "completed" if _cycle_ok else "failed"
                        if _cycle_ok:
                            self.trade_cycle_recovery_attempts = 0
                if _cycle_ok:
                    self.consecutive_cycle_failures = 0
                    self.last_cycle_error = None
                    self.last_cycle_error_at = None
                    if cycle_func == getattr(self, '_trade_cycle_func', None):
                        self._evaluate_binance_truth_drift()
                elif _cycle_error is not None:
                    self.consecutive_cycle_failures += 1
                    self.last_cycle_error = _cycle_error
                    self.last_cycle_error_at = datetime.utcnow()
                    if self.consecutive_cycle_failures >= 3:
                        logger.error(
                            "Pausing orchestrator after %s consecutive cycle failures",
                            self.consecutive_cycle_failures,
                        )
                        self.pause(
                            changed_by="system",
                            reason=f"consecutive_cycle_failures:{self.consecutive_cycle_failures}",
                        )

    def _reconcile_proactive_liquidity(self, trigger: str) -> None:
        portfolio_manager = self.portfolio_manager
        settings = getattr(portfolio_manager, "settings", None) if portfolio_manager else None
        if (
            portfolio_manager is None
            or settings is None
            or not bool(getattr(settings, "portfolio_proactive_liquidity_enabled", False))
        ):
            return
        try:
            futures_client = None
            if bool(getattr(settings, "futures_enabled", True)):
                from run_trades import _get_futures_client
                futures_client = _get_futures_client(settings, db_client=self.db_client)
            result = portfolio_manager.reconcile_proactive_liquidity(
                futures_client=futures_client,
                db_client=self.db_client,
                trigger=trigger,
            )
            logger.info("Proactive portfolio liquidity: %s", result)
        except Exception as exc:
            logger.error("Proactive portfolio liquidity failed: %s", exc)
    
    def _start_hard_risk_monitor(self) -> None:
        if self.hard_risk_monitor_thread and self.hard_risk_monitor_thread.is_alive():
            return
        self.hard_risk_monitor_stop = threading.Event()
        self.hard_risk_monitor_thread = threading.Thread(
            target=self._hard_risk_monitor_loop,
            daemon=True,
        )
        self.hard_risk_monitor_thread.start()
        logger.info("Hard-risk monitor started")

    def _hard_risk_monitor_loop(self) -> None:
        while self.should_run and self.hard_risk_monitor_stop is not None and not self.hard_risk_monitor_stop.is_set():
            interval = 2.0
            try:
                from init_components import get_components as _get_components
                from hard_risk_monitor import monitor_spot_hard_stops_once, monitor_futures_hard_stops_once
                comp = _get_components()
                settings = getattr(comp, "settings", None) if comp else None
                if settings is not None:
                    interval = max(0.5, float(getattr(settings, "hard_risk_monitor_interval_seconds", 2.0) or 2.0))
                    if bool(getattr(settings, "hard_risk_monitor_enabled", True)) and self.can_monitor_positions():
                        monitor_spot_hard_stops_once(comp)
                        # s107: futures had no fast (2s) SL/TP safety net before this —
                        # only the 3-45min trade cycle checked is_sl_hit()/is_tp_hit(),
                        # while Binance's own algo order was watched in real time.
                        monitor_futures_hard_stops_once(comp)
                self.hard_risk_monitor_last_at = datetime.utcnow()
                self.hard_risk_monitor_last_error = None
            except Exception as exc:
                self.hard_risk_monitor_last_error = str(exc)
                logger.error("Hard-risk monitor error: %s", exc)
            if self.hard_risk_monitor_stop.wait(interval):
                break
        logger.info("Hard-risk monitor stopped")

    def _start_position_telemetry(self) -> None:
        if self.position_telemetry_thread and self.position_telemetry_thread.is_alive():
            return
        self.position_telemetry_stop = threading.Event()
        self.position_telemetry_thread = threading.Thread(
            target=self._position_telemetry_loop,
            daemon=True,
        )
        self.position_telemetry_thread.start()
        logger.info("Position telemetry watcher started")

    def _position_telemetry_loop(self) -> None:
        while self.should_run and self.position_telemetry_stop is not None and not self.position_telemetry_stop.is_set():
            interval = 1.0
            try:
                from init_components import get_components as _get_components
                comp = _get_components()
                settings = getattr(comp, "settings", None) if comp else None
                if settings is not None:
                    interval = max(0.5, float(getattr(settings, "position_telemetry_watcher_interval_seconds", 1.0) or 1.0))
                    if bool(getattr(settings, "position_telemetry_watcher_enabled", True)):
                        position_telemetry_watcher.run_telemetry_cycle(comp)
                self.position_telemetry_last_at = datetime.utcnow()
                self.position_telemetry_last_error = None
            except Exception as exc:
                self.position_telemetry_last_error = str(exc)
                logger.error("Position telemetry watcher error: %s", exc)
            if self.position_telemetry_stop.wait(interval):
                break
        logger.info("Position telemetry watcher stopped")

    def _delayed_start_event_stream(self, delay_seconds: float = 15.0) -> None:
        """S105g: wait out the initial startup burst before starting the
        websocket, then start it (see start_trade_cycle for why)."""
        try:
            time.sleep(delay_seconds)
            self._start_event_stream()
        except Exception as exc:
            print(f"[EventStream] delayed start failed: {exc}")

    def _start_event_stream(self) -> None:
        """S105g: start the real-time Binance User Data Stream (futures + spot).

        This is additive, not a replacement: if it fails to connect for any
        reason (disabled, dry_run, no credentials, network issue), the system
        keeps working exactly as before via polling/reconciliation. Errors here
        must never block or fail trade-cycle startup.
        """
        try:
            if self.event_stream is not None and getattr(self.event_stream, "running", False):
                return
            from init_components import get_components as _get_components
            comp = _get_components()
            settings = getattr(comp, "settings", None) if comp else None
            if settings is None or not bool(getattr(settings, "binance_event_stream_enabled", True)):
                print("[EventStream] disabled or settings unavailable — skipping start")
                return
            from binance_event_stream import BinanceEventStream

            def _futures_client_getter():
                from run_trades import _get_futures_client
                _c = _get_components()
                return _get_futures_client(
                    getattr(_c, "settings", None),
                    db_client=getattr(_c, "db_client", None),
                    injected_client=getattr(_c, "futures_client", None),
                )

            self.event_stream = BinanceEventStream(
                settings=settings,
                futures_client_getter=_futures_client_getter,
                db_client=getattr(comp, "db_client", None),
            )
            started = self.event_stream.start()
            print(f"[EventStream] start attempted: connected={started}")
        except Exception as exc:
            print(f"[EventStream] failed to start (falling back to polling only): {exc}")

    def _stop_event_stream(self) -> None:
        try:
            if self.event_stream is not None:
                self.event_stream.stop()
        except Exception as exc:
            print(f"[EventStream] stop error: {exc}")

    def _start_staged_entry_intra_cycle(self) -> None:
        """Start a background thread that runs intra-cycle staged-entry checks.

        It sleeps dynamically between checks based on the current regime's
        staged_entry_regime_config.  If staged entry is disabled, the thread
        exits cleanly on the next stop event.
        """
        if self.staged_entry_intra_thread and self.staged_entry_intra_thread.is_alive():
            return
        self.staged_entry_intra_stop = threading.Event()
        self.staged_entry_intra_thread = threading.Thread(
            target=self._staged_entry_intra_loop,
            daemon=True,
        )
        self.staged_entry_intra_thread.start()
        logger.info("Staged-entry intra-cycle validation started")

    def _staged_entry_intra_loop(self) -> None:
        while True:
            try:
                if not self.should_run or self.staged_entry_intra_stop is None or self.staged_entry_intra_stop.is_set():
                    break
                if not self.can_execute_trades():
                    # Wait briefly until RUNNING again, but still respect stop.
                    if self.staged_entry_intra_stop.wait(5):
                        break
                    continue

                from init_components import get_components as _get_components
                comp = _get_components()
                if comp is None or not comp.db_client or not comp.settings:
                    if self.staged_entry_intra_stop.wait(5):
                        break
                    continue

                settings = comp.settings
                db_client = comp.db_client
                client = getattr(comp, 'binance_client', None)
                news_client = getattr(comp, 'news_client', None)

                # The persisted operator pause is authoritative for every
                # entry route, including the intra-cycle FastPath.
                try:
                    from run_trades import _is_new_entries_paused
                    if _is_new_entries_paused(db_client):
                        logger.info("[FastPath] PAUSED_DB — skipping intra-cycle entry execution")
                        if self.staged_entry_intra_stop.wait(5):
                            break
                        continue
                except Exception as _pause_exc:
                    logger.warning("[FastPath] pause-state check failed closed: %s", _pause_exc)
                    if self.staged_entry_intra_stop.wait(5):
                        break
                    continue

                # Determine current regime; prefer orchestrator state, then settings.
                regime_mode = self.current_regime_mode or getattr(settings, 'current_regime_mode', 'SPOT_BULL')
                cfg = staged_entry.get_staged_entry_regime_config(settings, regime_mode)
                if not cfg.get('enabled') or cfg.get('intra_mode') == 'disabled':
                    # Feature off for this regime; back off for a full cycle.
                    if self.staged_entry_intra_stop.wait(max(1, self.current_cycle_minutes) * 60):
                        break
                    continue

                # Watcher mode: tight, fixed interval; legacy mode: cycle-based.
                if cfg.get('intra_mode') == 'watcher':
                    sleep_seconds = max(5, int(cfg.get('watcher_interval_seconds', 10)))
                else:
                    cycle_min = self.current_cycle_minutes or 5
                    checks = max(1, int(cfg.get('checks_per_cycle', 1)))
                    sleep_seconds = max(30, int(cycle_min * 60 / checks))

                validated = staged_entry.perform_intra_cycle_check(
                    db_client, client, settings, regime_mode, news_client=news_client
                )
                if validated:
                    # Fast-path: execute immediately from this thread if enabled
                    _fp_enabled = getattr(settings, 'staged_entry_fast_path_enabled', True)
                    _fp_executed = False
                    if _fp_enabled and client:
                        try:
                            qubo_authority = staged_entry.get_qubo_authority_snapshot(
                                db_client, regime_mode,
                            )
                            if not qubo_authority:
                                logger.info("[FastPath] NO_QUBO_AUTHORITY — skipping intra-cycle entry execution")
                                if self.staged_entry_intra_stop.wait(5):
                                    break
                                continue
                            openable = staged_entry.get_openable_candidates(
                                db_client, regime_mode, qubo_authority["symbols"], settings=settings,
                                qubo_cycle_id=qubo_authority["cycle_id"],
                                qubo_symbols_hash=qubo_authority["symbols_hash"],
                                qubo_authority_symbols=qubo_authority["symbols"],
                            )
                            if openable:
                                from run_trades import _execute_validated_fast_path
                                portfolio_mgr = getattr(comp, 'portfolio_manager', None)
                                position_mgr = getattr(comp, 'position_manager', None)
                                futures_client = getattr(comp, 'futures_client', None)
                                regime_obj = None
                                try:
                                    from market_regime import get_regime
                                    regime_obj = get_regime(client, settings=settings)
                                except Exception:
                                    pass
                                # Watcher mode must not block behind a long-running main cycle.
                                # Try to acquire the lock briefly; retry next tick if busy.
                                _lock_ok = self._trade_cycle_lock.acquire(timeout=5)
                                if not _lock_ok:
                                    logger.info("[FastPath] trade cycle lock busy, retry next tick for %s", validated)
                                    continue
                                try:
                                    orders = _execute_validated_fast_path(
                                        client, settings, regime_mode, regime_obj,
                                        db_client, portfolio_mgr, position_mgr,
                                        openable, futures_client=futures_client,
                                    )
                                    if orders:
                                        _fp_executed = True
                                        logger.info(
                                            "[FastPath] Executed %d orders from intra-cycle: %s",
                                            len(orders),
                                            [o.get("symbol") for o in orders if isinstance(o, dict)],
                                        )
                                finally:
                                    self._trade_cycle_lock.release()
                        except Exception as _fp_err:
                            logger.warning("[FastPath] intra-cycle execution error: %s", _fp_err)
                    if not _fp_executed and cfg.get('intra_mode') != 'watcher':
                        self.trade_cycle_wake_event.set()
                        logger.info("Staged-entry validation woke trade cycle: %s", validated)
                if self.staged_entry_intra_stop.wait(sleep_seconds):
                    break
            except Exception as e:
                logger.warning(f"[StagedEntry/IntraCycle] error: {e}")
                try:
                    if self.staged_entry_intra_stop and self.staged_entry_intra_stop.wait(30):
                        break
                except Exception:
                    break
        logger.info("Staged-entry intra-cycle validation stopped")

    def stop_all_cycles(self) -> None:
        """Stop all background cycles."""
        self.should_run = False
        self.trade_cycle_wake_event.set()

        if self.staged_entry_intra_stop:
            self.staged_entry_intra_stop.set()
        if self.staged_entry_intra_thread:
            self.staged_entry_intra_thread.join(timeout=2)

        if self.hard_risk_monitor_stop:
            self.hard_risk_monitor_stop.set()
        if self.hard_risk_monitor_thread:
            self.hard_risk_monitor_thread.join(timeout=2)

        if self.position_telemetry_stop:
            self.position_telemetry_stop.set()
        if self.position_telemetry_thread:
            self.position_telemetry_thread.join(timeout=2)

        self._stop_event_stream()

        if self.trade_cycle_thread:
            self.trade_cycle_thread.join(timeout=5)
            logger.info("Trade cycle stopped")

        if self.analysis_cycle_thread:
            self.analysis_cycle_thread.join(timeout=5)
            logger.info("Analysis cycle stopped")

        if self.learning_cycle_thread:
            self.learning_cycle_thread.join(timeout=5)
            logger.info("Learning cycle stopped")
    
    def get_health_status(self) -> Dict:
        """
        Get overall health status of the system.
        
        Returns:
            Dictionary with health information
        """
        return {
            "system_state": self.state.value,
            "trade_cycle_running": self.trade_cycle_thread.is_alive() if self.trade_cycle_thread else False,
            "analysis_cycle_running": self.analysis_cycle_thread.is_alive() if self.analysis_cycle_thread else False,
            "learning_cycle_running": self.learning_cycle_thread.is_alive() if self.learning_cycle_thread else False,
            "staged_entry_intra_cycle_running": self.staged_entry_intra_thread.is_alive() if self.staged_entry_intra_thread else False,
            "hard_risk_monitor_running": self.hard_risk_monitor_thread.is_alive() if self.hard_risk_monitor_thread else False,
            "hard_risk_monitor_last_at": self.hard_risk_monitor_last_at.isoformat() if self.hard_risk_monitor_last_at else None,
            "hard_risk_monitor_last_error": self.hard_risk_monitor_last_error,
            "position_telemetry_running": self.position_telemetry_thread.is_alive() if self.position_telemetry_thread else False,
            "position_telemetry_last_at": self.position_telemetry_last_at.isoformat() if self.position_telemetry_last_at else None,
            "position_telemetry_last_error": self.position_telemetry_last_error,
            **(self.event_stream.health() if self.event_stream is not None else {
                "event_stream_running": False,
                "event_stream_connected": False,
                "event_stream_last_event_at": None,
                "event_stream_last_futures_event_at": None,
                "event_stream_last_spot_event_at": None,
                "event_stream_last_error": None,
            }),
            "portfolio_manager_active": self.portfolio_manager is not None,
            "position_manager_active": self.position_manager is not None,
            "backtester_active": self.backtester is not None,
            "last_trade_cycle_at": self.last_trade_cycle_at.isoformat() if self.last_trade_cycle_at else None,
            "trade_cycle_diagnostics": self.get_trade_cycle_diagnostics(),
            "consecutive_cycle_failures": self.consecutive_cycle_failures,
            "last_cycle_error": self.last_cycle_error,
            "last_cycle_error_at": self.last_cycle_error_at.isoformat() if self.last_cycle_error_at else None,
            "timestamp": datetime.utcnow().isoformat()
        }
