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
        self.should_run = False
        self.last_trade_cycle_at: Optional[datetime] = None
        self.current_cycle_minutes: int = 5  # updated dynamically per regime
        self.current_regime_mode: str = 'SPOT_BULL'  # updated dynamically per cycle
        
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
        if self.trade_cycle_thread and self.trade_cycle_thread.is_alive():
            logger.warning("Trade cycle already running")
            return
        
        self.should_run = True
        self._trade_cycle_func = cycle_func
        self.trade_cycle_thread = threading.Thread(
            target=self._run_cycle,
            args=(cycle_func, interval_minutes),
            daemon=True
        )
        self.trade_cycle_thread.start()
        logger.info(f"Trade cycle started with {interval_minutes} minute interval")
    
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
                time.sleep(max(1, _next_interval) * 60)
            first_run = False
            try:
                if self.can_execute_trades():
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
                elif self.can_monitor_positions():
                    # PAUSED: run trade cycle in monitor-only mode to protect open positions
                    _is_trade_cycle = cycle_func == getattr(self, '_trade_cycle_func', None)
                    if _is_trade_cycle:
                        try:
                            result = cycle_func(monitor_only=True)
                            logger.debug("System paused — monitor-only SL/TP cycle executed")
                        except TypeError:
                            # cycle_func does not accept monitor_only kwarg (e.g. analysis cycle)
                            logger.debug("System paused, skipping non-trade cycle")
                    else:
                        logger.debug("System paused, skipping non-trade cycle")
                else:
                    logger.debug("System stopped, skipping cycle")
            except Exception as e:
                import traceback
                logger.error(f"Error in cycle: {e}\n{traceback.format_exc()}")
            else:
                if cycle_func == getattr(self, '_trade_cycle_func', None):
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
    
    def stop_all_cycles(self) -> None:
        """Stop all background cycles."""
        self.should_run = False

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
            "portfolio_manager_active": self.portfolio_manager is not None,
            "position_manager_active": self.position_manager is not None,
            "backtester_active": self.backtester is not None,
            "last_trade_cycle_at": self.last_trade_cycle_at.isoformat() if self.last_trade_cycle_at else None,
            "timestamp": datetime.utcnow().isoformat()
        }
