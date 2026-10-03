"""Durable progress and memory-aware scheduling primitives for offline runs."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable


GIB = 1024 ** 3


def memory_snapshot() -> Dict[str, Any]:
    """Return available physical memory, or explicitly report unknown telemetry."""
    if platform.system() == "Windows":
        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return {"available_bytes": None, "total_bytes": None, "memory_load_pct": None, "source": "windows_api_failed"}
        return {
            "available_bytes": int(status.ullAvailPhys),
            "total_bytes": int(status.ullTotalPhys),
            "memory_load_pct": int(status.dwMemoryLoad),
            "source": "GlobalMemoryStatusEx",
        }
    try:
        values = {}
        with Path("/proc/meminfo").open("r", encoding="ascii") as source:
            for line in source:
                key, _, rest = line.partition(":")
                if key in {"MemAvailable", "MemTotal"}:
                    values[key] = int(rest.strip().split()[0]) * 1024
        available = values.get("MemAvailable")
        total = values.get("MemTotal")
        if available is None or total is None:
            raise ValueError("memory fields missing")
        return {
            "available_bytes": available,
            "total_bytes": total,
            "memory_load_pct": round(100 * (1 - available / total)),
            "source": "/proc/meminfo",
        }
    except (OSError, ValueError, IndexError):
        return {"available_bytes": None, "total_bytes": None, "memory_load_pct": None, "source": "unavailable"}


def safe_worker_limit(
    requested_workers: int,
    *,
    available_bytes: int | None,
    reserve_bytes: int,
    estimated_worker_bytes: int,
) -> int:
    if requested_workers < 1 or reserve_bytes < 0 or estimated_worker_bytes < 1:
        raise ValueError("worker and memory limits must be positive")
    if available_bytes is None:
        return 0
    budget = available_bytes - reserve_bytes
    if budget < estimated_worker_bytes:
        return 0
    return min(requested_workers, budget // estimated_worker_bytes)


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ProgressLedger:
    """Single-writer run ledger suitable for polling and crash-safe resume."""

    def __init__(
        self,
        run_dir: Path,
        *,
        run_id: str,
        manifest: Dict[str, Any],
        resume: bool = False,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.status_path = self.run_dir / "status.json"
        self.events_path = self.run_dir / "events.jsonl"
        self.manifest_path = self.run_dir / "run_manifest.json"
        manifest_hash = _stable_hash(manifest)
        if self.manifest_path.exists():
            existing = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if existing.get("manifest_sha256") != manifest_hash:
                raise ValueError("resume manifest differs from the original run")
            if not resume:
                raise FileExistsError(f"run already exists: {self.run_dir}; use resume")
        else:
            self._atomic_json(self.manifest_path, {"run_id": run_id, "manifest": manifest, "manifest_sha256": manifest_hash})
        if resume and self.status_path.exists():
            self.state = json.loads(self.status_path.read_text(encoding="utf-8"))
            if self.state.get("run_id") != run_id:
                raise ValueError("resume run_id differs from the original run")
        else:
            self.state = {
                "run_id": run_id,
                "manifest_sha256": manifest_hash,
                "state": "queued",
                "phase": "queued",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "completed_jobs": {},
                "counts": {"total": 0, "completed": 0, "pending": 0, "failed": 0, "skipped": 0},
                "workers": {"requested": 8, "active": 0, "safe_limit": 0},
                "memory": memory_snapshot(),
                "last_checkpoint": None,
                "eta_seconds": None,
                "error": None,
            }
        self._save()

    def _atomic_json(self, path: Path, payload: Dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _save(self) -> None:
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._atomic_json(self.status_path, self.state)

    def event(self, event_type: str, **payload: Any) -> None:
        row = {"at": datetime.now(timezone.utc).isoformat(), "event": event_type, **payload}
        with self.events_path.open("a", encoding="utf-8", newline="\n") as target:
            target.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")

    def start(self, phase: str, total_jobs: int, requested_workers: int) -> None:
        completed = self.state.get("completed_jobs", {})
        done = sum(job.get("status") == "completed" for job in completed.values())
        self.state.update({
            "state": "running",
            "phase": phase,
            "counts": {"total": total_jobs, "completed": done, "pending": max(0, total_jobs - done), "failed": 0, "skipped": 0},
            "workers": {"requested": requested_workers, "active": 0, "safe_limit": 0},
            "memory": memory_snapshot(),
            "error": None,
        })
        self._save()
        self.event("phase_started", phase=phase, total_jobs=total_jobs, requested_workers=requested_workers)

    def update_runtime(self, *, active_workers: int, safe_worker_limit: int) -> None:
        self.state["workers"].update({"active": active_workers, "safe_limit": safe_worker_limit})
        self.state["memory"] = memory_snapshot()
        self._save()

    def complete_job(self, job_id: str, *, checkpoint: str, result_sha256: str) -> None:
        self.state.setdefault("completed_jobs", {})[job_id] = {
            "status": "completed", "checkpoint": checkpoint, "result_sha256": result_sha256,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        counts = self.state["counts"]
        counts["completed"] = sum(job.get("status") == "completed" for job in self.state["completed_jobs"].values())
        counts["failed"] = sum(job.get("status") == "failed" for job in self.state["completed_jobs"].values())
        counts["pending"] = max(0, counts["total"] - counts["completed"] - counts["failed"] - counts.get("skipped", 0))
        self.state["last_checkpoint"] = checkpoint
        self.state["memory"] = memory_snapshot()
        self._save()
        self.event("job_completed", job_id=job_id, checkpoint=checkpoint, result_sha256=result_sha256)

    def fail_job(self, job_id: str, reason: str) -> None:
        self.state.setdefault("completed_jobs", {})[job_id] = {"status": "failed", "reason": reason}
        self.state["counts"]["failed"] = sum(job.get("status") == "failed" for job in self.state["completed_jobs"].values())
        self.state["counts"]["completed"] = sum(job.get("status") == "completed" for job in self.state["completed_jobs"].values())
        self.state["counts"]["pending"] = max(0, self.state["counts"]["total"] - self.state["counts"]["completed"] - self.state["counts"]["failed"] - self.state["counts"].get("skipped", 0))
        self.state["error"] = {"job_id": job_id, "reason": reason}
        self._save()
        self.event("job_failed", job_id=job_id, reason=reason)

    def finish(self, *, state: str = "completed", error: str | None = None) -> None:
        self.state["state"] = state
        self.state["phase"] = "complete" if state == "completed" else self.state.get("phase")
        self.state["workers"]["active"] = 0
        if error:
            self.state["error"] = {"reason": error}
        self._save()
        self.event("run_finished", state=state, error=error)