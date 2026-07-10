"""MIMIC — Scheduler state machine with checkpoints and progress tracking."""
from __future__ import annotations
import os
import json
import time
from typing import Any, Dict, Optional
from datetime import datetime, timezone, timedelta
from enum import Enum

from simulation.mimic_universe_selector import MicroUniverseSelector, get_selector
from simulation.mimic_window_controller import MimicWindowController, get_window_controller
from simulation.mimic_runner import run_mimic_simulation
from config import get_settings


class Phase(str, Enum):
    IDLE = "IDLE"
    SELECT_UNIVERSE = "SELECT_UNIVERSE"
    AUDIT = "AUDIT"
    DOWNLOAD = "DOWNLOAD"
    VALIDATE = "VALIDATE"
    MAP_REGIMES = "MAP_REGIMES"
    CALIBRATE = "CALIBRATE"
    CONSOLIDATE = "CONSOLIDATE"
    CROSS_VALIDATE = "CROSS_VALIDATE"
    APPLY = "APPLY"
    MONITOR = "MONITOR"
    PAUSED = "PAUSED"
    FAILED = "FAILED"


DEFAULT_STATE: Dict[str, Any] = {
    "phase": Phase.IDLE.value,
    "status": "ready",
    "progress_pct": 0.0,
    "current_micro_universe_id": None,
    "last_result": {},
    "error": None,
    "started_at": None,
    "updated_at": None,
    "next_phase": Phase.SELECT_UNIVERSE.value,
    "completed_micro_universes": [],
    "metrics": {
        "total_days": 0,
        "regimes": {},
        "symbols": {},
    },
}


class MimicScheduler:
    """State machine for autonomous MIMIC execution."""

    STATE_KEY = "mimic_state"

    def __init__(
        self,
        repo_root: Optional[str] = None,
        selector: Optional[MicroUniverseSelector] = None,
        window_controller: Optional[MimicWindowController] = None,
        db_client=None,
    ):
        self.repo_root = repo_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.state_path = os.path.join(self.repo_root, "Dev", "benchmarks", "mimic_state.json")
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        self.selector = selector or get_selector()
        self.window_controller = window_controller or get_window_controller()
        self.db_client = db_client
        self._state: Dict[str, Any] = DEFAULT_STATE.copy()
        self._load_state()

    def _load_state(self) -> None:
        # Try DB first
        if self.db_client:
            try:
                rows = self.db_client.execute_query(
                    "SELECT value FROM trading_runtime_state WHERE key=%s",
                    (self.STATE_KEY,)
                )
                if rows:
                    value = rows[0].get("value")
                    if isinstance(value, str):
                        self._state = json.loads(value)
                    else:
                        self._state = value
                    return
            except Exception:
                pass
        # Fallback to local file
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    self._state = json.load(f)
            except Exception:
                self._state = DEFAULT_STATE.copy()
        else:
            self._state = DEFAULT_STATE.copy()

    def _save_state(self) -> None:
        self._state["updated_at"] = datetime.now(timezone.utc).isoformat()
        # Save to local file
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self._state, f, indent=2, ensure_ascii=False)
        # Save to DB if available
        if self.db_client:
            try:
                self.db_client.execute_update(
                    "INSERT INTO trading_runtime_state (key, value, updated_at) "
                    "VALUES (%s, %s, NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                    (self.STATE_KEY, json.dumps(self._state), json.dumps(self._state))
                )
            except Exception as e:
                print(f"[MimicScheduler] DB save failed: {e}")

    def get_state(self) -> Dict[str, Any]:
        self._load_state()
        return self._state

    def set_phase(self, phase: Phase, status: str = "running", progress_pct: Optional[float] = None) -> None:
        self._state["phase"] = phase.value
        self._state["status"] = status
        if progress_pct is not None:
            self._state["progress_pct"] = progress_pct
        self._save_state()

    def set_error(self, error: str) -> None:
        self._state["status"] = "failed"
        self._state["error"] = error
        self._state["phase"] = Phase.FAILED.value
        self._save_state()

    def set_result(self, result: Dict[str, Any], next_phase: Phase) -> None:
        self._state["last_result"] = result
        self._state["error"] = None
        self._state["next_phase"] = next_phase.value
        self._state["phase"] = next_phase.value
        self._state["status"] = "completed"
        self._save_state()

    def pause(self) -> None:
        self._state["phase"] = Phase.PAUSED.value
        self._state["status"] = "paused"
        self._save_state()

    def resume(self) -> None:
        self._state["phase"] = self._state.get("next_phase", Phase.SELECT_UNIVERSE.value)
        self._state["status"] = "ready"
        self._save_state()

    def reset(self) -> None:
        self._state = DEFAULT_STATE.copy()
        self._state["started_at"] = datetime.now(timezone.utc).isoformat()
        self._save_state()

    def start(self) -> None:
        if not self._state.get("started_at"):
            self._state["started_at"] = datetime.now(timezone.utc).isoformat()
        if self._state.get("phase") == Phase.IDLE.value:
            self._state["phase"] = Phase.SELECT_UNIVERSE.value
            self._state["status"] = "ready"
        self._save_state()

    def can_run_now(self, market_condition: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.window_controller.can_run(market_condition=market_condition)

    def select_next_micro_universe(self) -> Optional[Dict[str, Any]]:
        self.selector.compute_scores()
        universe = self.selector.select_next()
        if universe:
            self._state["current_micro_universe_id"] = universe.id
            self.selector.mark_status(universe.id, "in_progress")
            self._save_state()
            return universe.to_dict()
        return None

    def complete_micro_universe(self, universe_id: str, result: Dict[str, Any]) -> None:
        self.selector.mark_status(universe_id, "completed", result)
        completed = self._state.setdefault("completed_micro_universes", [])
        if universe_id not in completed:
            completed.append(universe_id)
        self._update_metrics(result)
        self._state["current_micro_universe_id"] = None
        self._save_state()

    def fail_micro_universe(self, universe_id: str, error: str) -> None:
        self.selector.mark_status(universe_id, "failed", {"error": error})
        self._state["current_micro_universe_id"] = None
        self._save_state()

    def _update_metrics(self, result: Dict[str, Any]) -> None:
        regimes = self._state.setdefault("metrics", {}).setdefault("regimes", {})
        symbols = self._state.setdefault("metrics", {}).setdefault("symbols", {})
        total_days = self._state.setdefault("metrics", {}).setdefault("total_days", 0)

        regime = result.get("regime")
        days = result.get("days", 0)
        syms = result.get("symbols", [])

        if regime:
            regimes[regime] = regimes.get(regime, 0) + days
        for s in syms:
            symbols[s] = symbols.get(s, 0) + days
        self._state["metrics"]["total_days"] = total_days + days

    def is_ready_for_cross_validation(self) -> bool:
        metrics = self._state.get("metrics", {})
        regimes = metrics.get("regimes", {})
        total_days = metrics.get("total_days", 0)
        covered_regimes = sum(1 for v in regimes.values() if v >= 10)
        return total_days >= 90 and covered_regimes >= 3

    def run_phase(self) -> Dict[str, Any]:
        """Execute one scheduler phase; return result and next phase."""
        phase = self._state.get("phase", Phase.IDLE.value)
        if phase == Phase.IDLE.value or phase == Phase.PAUSED.value:
            return {"ok": True, "phase": phase, "result": {}}
        if phase == Phase.SELECT_UNIVERSE.value:
            return self._phase_select_universe()
        if phase == Phase.AUDIT.value:
            return self._phase_audit()
        if phase == Phase.DOWNLOAD.value:
            return self._phase_download()
        if phase == Phase.VALIDATE.value:
            return self._phase_validate()
        if phase == Phase.MAP_REGIMES.value:
            return self._phase_map_regimes()
        if phase == Phase.CALIBRATE.value:
            return self._phase_calibrate()
        if phase == Phase.CONSOLIDATE.value:
            return self._phase_consolidate()
        if phase == Phase.CROSS_VALIDATE.value:
            return self._phase_cross_validate()
        if phase == Phase.APPLY.value:
            return self._phase_apply()
        if phase == Phase.MONITOR.value:
            return self._phase_monitor()
        return {"ok": False, "error": f"unknown phase {phase}"}

    def run_once(self) -> Dict[str, Any]:
        """Run one full micro-universe cycle from selection to apply."""
        start_phase = self._state.get("phase", Phase.IDLE.value)
        if start_phase == Phase.IDLE.value:
            self.start()
        target_phases = [Phase.SELECT_UNIVERSE, Phase.AUDIT, Phase.DOWNLOAD, Phase.VALIDATE,
                         Phase.MAP_REGIMES, Phase.CALIBRATE, Phase.CONSOLIDATE,
                         Phase.CROSS_VALIDATE, Phase.APPLY, Phase.MONITOR]
        for phase in target_phases:
            if self._state.get("status") == "paused":
                break
            self._state["phase"] = phase.value
            self._state["status"] = "running"
            self._save_state()
            res = self.run_phase()
            if not res.get("ok"):
                self.set_error(res.get("error", "phase failed"))
                return {"ok": False, "error": res.get("error"), "phase": phase.value}
        self._state["phase"] = Phase.IDLE.value
        self._state["status"] = "ready"
        self._state["next_phase"] = Phase.SELECT_UNIVERSE.value
        self._save_state()
        return {"ok": True, "state": self._state}

    def _phase_select_universe(self) -> Dict[str, Any]:
        universe = self.select_next_micro_universe()
        if not universe:
            self.set_result({}, Phase.IDLE)
            self._state["status"] = "completed"
            self._save_state()
            return {"ok": True, "result": {"empty": True}, "next_phase": Phase.IDLE.value}
        self.set_result({"micro_universe": universe}, Phase.AUDIT)
        return {"ok": True, "result": {"micro_universe": universe}, "next_phase": Phase.AUDIT.value}

    def _phase_audit(self) -> Dict[str, Any]:
        universe = self._current_universe_dict()
        if not universe:
            return {"ok": False, "error": "no current universe"}
        # Quick audit: check which symbols have data for this window
        data_dir = os.path.join(self.repo_root, "historical_data")
        missing = []
        for s in universe.get("symbols", []):
            path = os.path.join(data_dir, f"{s}_1h.parquet")
            if not os.path.exists(path):
                missing.append(s)
        result = {"missing_symbols": missing, "data_dir": data_dir}
        self.set_result(result, Phase.DOWNLOAD)
        return {"ok": True, "result": result, "next_phase": Phase.DOWNLOAD.value}

    def _phase_download(self) -> Dict[str, Any]:
        universe = self._current_universe_dict()
        if not universe:
            return {"ok": False, "error": "no current universe"}
        start = self._parse_date(universe.get("start"))
        end = self._parse_date(universe.get("end"))
        symbols = universe.get("symbols", [])
        try:
            from Dev.scripts.download_historical_data import download_symbol
            from binance_client import BinanceClient
            client = BinanceClient(api_key="", api_secret="", testnet=False, dry_run=False)
            results = []
            # Download enough history to cover the requested window
            now = datetime.now(timezone.utc)
            window_days = (end - start).days if start and end else 7
            days_to_fetch = max((now - start).days + window_days + 2, 7) if start else 7
            for s in symbols:
                res = download_symbol(s, start=start, end=end, interval="1h", days=days_to_fetch, client=client, data_dir=os.path.join(self.repo_root, "historical_data"))
                results.append(res)
            self.set_result({"downloads": results}, Phase.VALIDATE)
            return {"ok": True, "result": {"downloads": results}, "next_phase": Phase.VALIDATE.value}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _phase_validate(self) -> Dict[str, Any]:
        universe = self._current_universe_dict()
        if not universe:
            return {"ok": False, "error": "no current universe"}
        # Validate data exists and has rows
        data_dir = os.path.join(self.repo_root, "historical_data")
        errors = []
        for s in universe.get("symbols", []):
            path = os.path.join(data_dir, f"{s}_1h.parquet")
            if not os.path.exists(path):
                errors.append(f"missing {s}")
                continue
            try:
                import pandas as pd
                df = pd.read_parquet(path)
                if len(df) < 24:
                    errors.append(f"{s} has only {len(df)} rows")
            except Exception as e:
                errors.append(f"{s} read error: {e}")
        ok = not errors
        self.set_result({"errors": errors, "valid": ok}, Phase.MAP_REGIMES if ok else Phase.DOWNLOAD)
        return {"ok": ok, "result": {"errors": errors, "valid": ok}, "next_phase": (Phase.MAP_REGIMES if ok else Phase.DOWNLOAD).value}

    def _phase_map_regimes(self) -> Dict[str, Any]:
        universe = self._current_universe_dict()
        if not universe:
            return {"ok": False, "error": "no current universe"}
        # Use provided regime target; optionally verify with market_regime
        regime = universe.get("regime_target", "SPOT_BULL")
        result = {"regime": regime}
        self.set_result(result, Phase.CALIBRATE)
        return {"ok": True, "result": result, "next_phase": Phase.CALIBRATE.value}

    def _phase_calibrate(self) -> Dict[str, Any]:
        universe = self._current_universe_dict()
        if not universe:
            return {"ok": False, "error": "no current universe"}
        start = self._parse_date(universe.get("start"))
        end = self._parse_date(universe.get("end"))
        symbols = universe.get("symbols", [])
        regime = universe.get("regime_target", "SPOT_BULL")
        try:
            sim_result = run_mimic_simulation(
                settings=get_settings(),
                symbols=symbols,
                interval="1h",
                start=start,
                end=end,
                initial_usdt=2910.0,
                fear_greed_value=50.0,
                data_dir=os.path.join(self.repo_root, "historical_data"),
            )
            # Extract summary metrics
            final = sim_result.get("final_balance", {})
            pnl = sim_result.get("pnl_stats", {}).get("total_pnl", 0)
            result = {
                "regime": regime,
                "days": (end - start).days if start and end else 0,
                "symbols": symbols,
                "cycles": sim_result.get("cycles", 0),
                "final_balance": final,
                "pnl": pnl,
            }
            self.set_result(result, Phase.CONSOLIDATE)
            return {"ok": True, "result": result, "next_phase": Phase.CONSOLIDATE.value}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _phase_consolidate(self) -> Dict[str, Any]:
        result = self._state.get("last_result", {})
        universe_id = self._state.get("current_micro_universe_id")
        if universe_id:
            self.complete_micro_universe(universe_id, result)
        self.set_result(result, Phase.CROSS_VALIDATE)
        return {"ok": True, "result": result, "next_phase": Phase.CROSS_VALIDATE.value}

    def _phase_cross_validate(self) -> Dict[str, Any]:
        # If enough data accumulated, compare against baseline; otherwise pass
        if not self.is_ready_for_cross_validation():
            self.set_result({"skipped": True}, Phase.APPLY)
            return {"ok": True, "result": {"skipped": True}, "next_phase": Phase.APPLY.value}
        try:
            import subprocess
            proc = subprocess.run(
                ["python", "-m", "pytest", "tests/test_mimic_regression.py", "-q"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
            )
            passed = proc.returncode == 0
            result = {"passed": passed, "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-1000:]}
            self.set_result(result, Phase.APPLY if passed else Phase.IDLE)
            return {"ok": passed, "result": result, "next_phase": (Phase.APPLY if passed else Phase.IDLE).value}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _phase_apply(self) -> Dict[str, Any]:
        result = self._state.get("last_result", {})
        # Persist result to dataset progress file
        progress_path = os.path.join(self.repo_root, "Dev", "benchmarks", "mimic_dataset_progress.json")
        progress = []
        if os.path.exists(progress_path):
            try:
                with open(progress_path, "r", encoding="utf-8") as f:
                    progress = json.load(f)
            except Exception:
                progress = []
        progress.append({
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "result": result,
            "micro_universe_id": self._state.get("current_micro_universe_id"),
        })
        with open(progress_path, "w", encoding="utf-8") as f:
            json.dump(progress, f, indent=2, ensure_ascii=False)
        self.set_result({"applied": True, "progress_entries": len(progress)}, Phase.MONITOR)
        return {"ok": True, "result": {"applied": True, "progress_entries": len(progress)}, "next_phase": Phase.MONITOR.value}

    def _phase_monitor(self) -> Dict[str, Any]:
        # Record audit entry for completed micro universe
        if self.db_client:
            try:
                self.db_client.execute_update(
                    "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
                    "VALUES ('__mimic__', 'system', 'micro_universe_completed', %s, false)",
                    (json.dumps({
                        "micro_universe_id": self._state.get("current_micro_universe_id"),
                        "result": self._state.get("last_result", {}),
                    }),)
                )
            except Exception as e:
                print(f"[MimicScheduler] monitor audit failed: {e}")
        self._state["current_micro_universe_id"] = None
        self.set_result({"monitored": True}, Phase.IDLE)
        return {"ok": True, "result": {"monitored": True}, "next_phase": Phase.IDLE.value}

    def _current_universe_dict(self) -> Optional[Dict[str, Any]]:
        uid = self._state.get("current_micro_universe_id")
        if not uid:
            return None
        for u in self.selector.get_queue():
            if u.get("id") == uid:
                return u
        return None

    def _parse_date(self, value) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            dt = value
        else:
            try:
                dt = datetime.fromisoformat(str(value))
            except Exception:
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def get_summary(self) -> Dict[str, Any]:
        return {
            "state": self._state,
            "queue_summary": self.selector.get_summary(),
            "schedule": self.window_controller.get_schedule(),
            "can_run_now": self.can_run_now(),
        }


def get_scheduler(db_client=None) -> MimicScheduler:
    return MimicScheduler(db_client=db_client)
