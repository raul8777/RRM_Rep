"""MIMIC — Execution window controller: schedules and gatekeeps simulation runs."""
from __future__ import annotations
import os
import json
from typing import Any, Dict, Optional
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict, field


DEFAULT_SCHEDULE = {
    "enabled": True,
    "mode": "auto",  # auto | manual | suggest-only
    "windows": [
        {"days": [0, 1, 2, 3, 4, 5, 6], "start": "00:00", "end": "07:00"},
    ],
    "timezone": "local",  # local | UTC
    "max_run_minutes": 90,
    "market_condition_check": True,
}


@dataclass
class ExecutionWindow:
    days: list  # 0=Monday ... 6=Sunday
    start: str  # HH:MM
    end: str  # HH:MM

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "ExecutionWindow":
        return ExecutionWindow(**data)


class MimicWindowController:
    """Determines whether MIMIC is allowed to run now and when the next window is."""

    def __init__(self, repo_root: Optional[str] = None):
        self.repo_root = repo_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.schedule_path = os.path.join(self.repo_root, "Dev", "benchmarks", "mimic_schedule.json")
        os.makedirs(os.path.dirname(self.schedule_path), exist_ok=True)
        self._schedule: Dict[str, Any] = DEFAULT_SCHEDULE.copy()
        self._suggested_override: Optional[Dict[str, Any]] = None
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.schedule_path):
            try:
                with open(self.schedule_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self._schedule = loaded
            except Exception:
                pass
        else:
            self._save()

    def _save(self) -> None:
        with open(self.schedule_path, "w", encoding="utf-8") as f:
            json.dump(self._schedule, f, indent=2, ensure_ascii=False)

    def get_schedule(self) -> Dict[str, Any]:
        return self._schedule

    def set_schedule(self, schedule: Dict[str, Any]) -> None:
        self._schedule = schedule
        self._save()

    def _parse_time(self, t: str) -> int:
        h, m = map(int, t.split(":"))
        return h * 60 + m

    def _now_local(self) -> datetime:
        return datetime.now()

    def is_within_window(self, now: Optional[datetime] = None) -> bool:
        if not self._schedule.get("enabled", True):
            return False
        if self._schedule.get("mode") == "manual":
            return False  # operator must trigger each run
        now = now or self._now_local()
        weekday = now.weekday()
        minutes = now.hour * 60 + now.minute
        for w in self._schedule.get("windows", []):
            raw_days = w.get("days", [])
            if isinstance(raw_days, str):
                try:
                    days = [int(d) for d in raw_days.split()]
                except ValueError:
                    days = []
            else:
                days = [int(d) for d in raw_days]
            if weekday in days:
                start = self._parse_time(w["start"])
                end = self._parse_time(w["end"])
                if end < start:  # crosses midnight
                    if minutes >= start or minutes <= end:
                        return True
                else:
                    if start <= minutes <= end:
                        return True
        return False

    def next_window_start(self, now: Optional[datetime] = None) -> Optional[datetime]:
        now = now or self._now_local()
        candidates = []
        for w in self._schedule.get("windows", []):
            for day_offset in range(0, 8):
                candidate = now + timedelta(days=day_offset)
                if candidate.weekday() not in w.get("days", []):
                    continue
                h, m = map(int, w["start"].split(":"))
                candidate = candidate.replace(hour=h, minute=m, second=0, microsecond=0)
                if day_offset == 0 and candidate <= now:
                    continue
                candidates.append(candidate)
        if not candidates:
            return None
        return min(candidates)

    def check_market_condition(self, condition: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Suggests whether to run outside the normal window based on market condition."""
        if not self._schedule.get("market_condition_check", True):
            return {"suggest_run": False, "reason": "market_condition_check disabled"}
        if condition is None:
            # Default: no strong suggestion; external caller should pass F&G, regime, signal quality
            return {"suggest_run": False, "reason": "no condition data"}
        suggest = False
        reasons = []
        if condition.get("regime_change", False):
            suggest = True
            reasons.append("regime_change")
        if condition.get("high_quality_signals", 0) >= 3:
            suggest = True
            reasons.append("high_quality_signals")
        return {"suggest_run": suggest, "reason": ", ".join(reasons) or "no strong condition"}

    def can_run(self, now: Optional[datetime] = None, market_condition: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._load()  # reload schedule from disk to honor dashboard updates
        now = now or self._now_local()
        in_window = self.is_within_window(now)
        market_suggestion = self.check_market_condition(market_condition)
        mode = self._schedule.get("mode", "auto")

        if mode == "manual":
            return {
                "can_run": False,
                "reason": "mode=manual: operator must trigger run",
                "next_window": self.next_window_start(now),
                "suggestion": market_suggestion,
            }

        if mode == "auto":
            if in_window:
                return {
                    "can_run": True,
                    "reason": "inside scheduled window",
                    "next_window": None,
                    "suggestion": None,
                }
            if market_suggestion.get("suggest_run"):
                return {
                    "can_run": False,  # requires operator approval outside window
                    "reason": "outside window but market condition suggests run",
                    "next_window": self.next_window_start(now),
                    "suggestion": market_suggestion,
                    "requires_approval": True,
                }
            return {
                "can_run": False,
                "reason": "outside scheduled window and no strong market condition",
                "next_window": self.next_window_start(now),
                "suggestion": market_suggestion,
            }

        # suggest-only: never auto-run, just report
        return {
            "can_run": False,
            "reason": "mode=suggest-only: operator must approve every run",
            "next_window": self.next_window_start(now),
            "suggestion": market_suggestion,
        }


def get_window_controller() -> MimicWindowController:
    return MimicWindowController()
