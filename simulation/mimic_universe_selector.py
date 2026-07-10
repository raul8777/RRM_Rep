"""MIMIC — Micro universe selector and priority queue."""
from __future__ import annotations
import os
import json
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict


DEFAULT_UNIVERSE_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "ADAUSDT", "SOLUSDT",
    "XLMUSDT", "UNIUSDT", "BCHUSDT", "NEARUSDT",
]

DEFAULT_REGIMES = ["SPOT_BULL", "RANGE_SCALP", "PRESERVATION"]

# Score weights (adjustable via dashboard)
DEFAULT_WEIGHTS = {
    "opportunity": 0.40,
    "regime_missing": 0.35,
    "coverage": 0.25,
}


@dataclass
class MicroUniverse:
    id: str
    symbols: List[str]
    start: str  # ISO date
    end: str  # ISO date
    regime_target: str
    objective: str = "download_validate"
    status: str = "pending"  # pending, in_progress, completed, failed, skipped
    score: float = 0.0
    score_breakdown: Dict[str, float] = field(default_factory=dict)
    result: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "MicroUniverse":
        return MicroUniverse(**data)


class MicroUniverseSelector:
    """Manages the queue of micro universes and selects the next one to process."""

    def __init__(self, repo_root: Optional[str] = None, weights: Optional[Dict[str, float]] = None):
        self.repo_root = repo_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.weights = weights or DEFAULT_WEIGHTS.copy()
        self.queue_path = os.path.join(self.repo_root, "Dev", "benchmarks", "mimic_universe_queue.json")
        self.progress_path = os.path.join(self.repo_root, "Dev", "benchmarks", "mimic_dataset_progress.json")
        os.makedirs(os.path.dirname(self.queue_path), exist_ok=True)
        self._queue: List[MicroUniverse] = []
        self._forced_id: Optional[str] = None
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.queue_path):
            try:
                with open(self.queue_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._queue = [MicroUniverse.from_dict(u) for u in data.get("universes", [])]
                self.weights = data.get("weights", self.weights)
                self._forced_id = data.get("forced_id")
            except Exception:
                self._queue = []
        else:
            self._queue = []

    def _save(self) -> None:
        with open(self.queue_path, "w", encoding="utf-8") as f:
            json.dump({
                "weights": self.weights,
                "forced_id": self._forced_id,
                "universes": [u.to_dict() for u in self._queue],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, f, indent=2, ensure_ascii=False)

    def generate_initial_queue(
        self,
        symbols: Optional[List[str]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        window_days: int = 7,
        regimes: Optional[List[str]] = None,
    ) -> List[MicroUniverse]:
        """Generate micro universes covering symbol groups and date windows."""
        symbols = symbols or DEFAULT_UNIVERSE_SYMBOLS
        regimes = regimes or DEFAULT_REGIMES
        # Default window: last 180 days up to today, split in 7-day chunks
        end = datetime.strptime(end_date, "%Y-%m-%d") if end_date else datetime.now(timezone.utc)
        start = datetime.strptime(start_date, "%Y-%m-%d") if start_date else (end - timedelta(days=180))

        universes: List[MicroUniverse] = []
        chunk_start = start
        idx = 0
        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=window_days), end)
            for regime in regimes:
                universes.append(MicroUniverse(
                    id=f"mu_{idx:04d}_{regime.lower()}",
                    symbols=symbols,
                    start=chunk_start.strftime("%Y-%m-%d"),
                    end=chunk_end.strftime("%Y-%m-%d"),
                    regime_target=regime,
                    objective="download_validate_map",
                ))
                idx += 1
            chunk_start = chunk_end

        self._queue = universes
        self._save()
        return universes

    def _load_progress(self) -> Dict[str, Any]:
        """Load accumulated dataset progress.

        The progress file may be either the scheduler metrics dict or a list
        of completed micro-universe entries (newer format). Both are accepted
        and the aggregated coverage metrics are returned.
        """
        default = {"regimes": {}, "symbols": {}, "total_days": 0, "updated_at": None}
        if not os.path.exists(self.progress_path):
            return default
        try:
            with open(self.progress_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return default

        if isinstance(raw, dict):
            return {
                "regimes": raw.get("regimes") or {},
                "symbols": raw.get("symbols") or {},
                "total_days": raw.get("total_days") or 0,
                "updated_at": raw.get("updated_at"),
            }

        # List format: aggregate from completed entries
        regimes: Dict[str, int] = {}
        symbols: Dict[str, int] = {}
        total_days = 0
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            result = entry.get("result") or {}
            if result.get("skipped"):
                continue
            days = result.get("days", 0)
            regime = result.get("regime")
            if regime and days:
                regimes[regime] = regimes.get(regime, 0) + days
            for s in result.get("symbols", []):
                symbols[s] = symbols.get(s, 0) + days
            total_days += days
        return {
            "regimes": regimes,
            "symbols": symbols,
            "total_days": total_days,
            "updated_at": raw[-1].get("completed_at") if raw else None,
        }

    def compute_scores(self, opportunity_data: Optional[Dict[str, float]] = None) -> None:
        """Recompute priority scores for all pending micro universos."""
        progress = self._load_progress()
        regime_days = progress.get("regimes", {})
        symbol_days = progress.get("symbols", {})

        for u in self._queue:
            if u.status != "pending":
                continue

            # 1. Coverage score: lower if symbols/window already covered
            covered = sum(symbol_days.get(s, 0) for s in u.symbols)
            coverage_score = 1.0 / (1.0 + covered / max(len(u.symbols), 1))

            # 2. Regime missing score: higher if regime has fewer accumulated days
            regime_days_count = regime_days.get(u.regime_target, 0)
            regime_missing_score = 1.0 / (1.0 + regime_days_count / 30.0)

            # 3. Opportunity score: external signal from dashboard/market analysis
            opp_key = f"{u.regime_target}_{','.join(sorted(u.symbols))}"
            opportunity_score = (opportunity_data or {}).get(opp_key, 0.5)

            u.score_breakdown = {
                "opportunity": opportunity_score,
                "regime_missing": regime_missing_score,
                "coverage": coverage_score,
            }
            u.score = (
                opportunity_score * self.weights["opportunity"] +
                regime_missing_score * self.weights["regime_missing"] +
                coverage_score * self.weights["coverage"]
            )
            u.updated_at = datetime.now(timezone.utc).isoformat()
        self._save()

    def select_next(self) -> Optional[MicroUniverse]:
        """Select the next pending micro universe with highest score or forced id."""
        if self._forced_id:
            for u in self._queue:
                if u.id == self._forced_id:
                    self._forced_id = None
                    self._save()
                    return u

        pending = [u for u in self._queue if u.status == "pending"]
        if not pending:
            return None
        pending.sort(key=lambda x: x.score, reverse=True)
        return pending[0]

    def mark_status(self, universe_id: str, status: str, result: Optional[Dict[str, Any]] = None) -> None:
        for u in self._queue:
            if u.id == universe_id:
                u.status = status
                u.updated_at = datetime.now(timezone.utc).isoformat()
                if result:
                    u.result = result
                break
        self._save()

    def force_next(self, universe_id: str) -> bool:
        for u in self._queue:
            if u.id == universe_id:
                self._forced_id = universe_id
                self._save()
                return True
        return False

    def set_weights(self, weights: Dict[str, float]) -> None:
        self.weights = {k: float(v) for k, v in weights.items()}
        self._save()

    def get_queue(self) -> List[Dict[str, Any]]:
        return [u.to_dict() for u in self._queue]

    def get_summary(self) -> Dict[str, Any]:
        by_status: Dict[str, int] = {}
        for u in self._queue:
            by_status[u.status] = by_status.get(u.status, 0) + 1
        return {
            "total": len(self._queue),
            "pending": by_status.get("pending", 0),
            "completed": by_status.get("completed", 0),
            "failed": by_status.get("failed", 0),
            "skipped": by_status.get("skipped", 0),
            "weights": self.weights,
            "forced_id": self._forced_id,
        }


def get_selector() -> MicroUniverseSelector:
    return MicroUniverseSelector()
