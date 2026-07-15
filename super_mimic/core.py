"""Core data model and persistence for Super MIMIC experiments."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import get_settings


@dataclass
class DesignSpec:
    """User-supplied design of a Super MIMIC experiment."""
    mode: str = "RANGE_SCALP"
    profile: str = "A"
    windows: List[str] = field(default_factory=list)
    engine: str = "mimic_sweep"
    strategy: str = "full"
    max_cycles: Optional[int] = None
    max_workers: int = 1
    param_grid: Optional[Dict[str, List[Any]]] = None
    output_dir: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DesignSpec":
        return cls(**d)


@dataclass
class MetricsWithCI:
    """Metric with confidence interval (quantum-inspired uncertainty)."""
    mean: float = 0.0
    ci_low: Optional[float] = None
    ci_high: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MetricsWithCI":
        return cls(**d)


@dataclass
class EngineResults:
    """Raw output from a simulation engine."""
    engine: str = ""
    summary: Dict[str, Any] = field(default_factory=dict)
    recommendations: List[Dict[str, Any]] = field(default_factory=list)
    results: List[Dict[str, Any]] = field(default_factory=list)
    report_path: Optional[str] = None
    results_path: Optional[str] = None
    status_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EngineResults":
        return cls(**d)


@dataclass
class SuperMimicArtifact:
    """Common artifact produced by every Super MIMIC experiment."""
    experiment_id: str
    phase: str
    status: str  # designing / running / completed / failed / cancelled
    design: DesignSpec
    engine_results: EngineResults
    metrics: Dict[str, MetricsWithCI] = field(default_factory=dict)
    fingerprint: Dict[str, Any] = field(default_factory=dict)
    landscape: Dict[str, Any] = field(default_factory=dict)
    recommended_config: Dict[str, Any] = field(default_factory=dict)
    deploy_gate: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: Optional[str] = None
    error: Optional[str] = None
    log_tail: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Convert dataclass children
        d["design"] = self.design.to_dict()
        d["engine_results"] = self.engine_results.to_dict()
        d["metrics"] = {k: v.to_dict() for k, v in self.metrics.items()}
        return d

    def metrics_to_serializable(self) -> Dict[str, Any]:
        return {k: v.to_dict() for k, v in self.metrics.items()}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SuperMimicArtifact":
        return cls(
            experiment_id=d["experiment_id"],
            phase=d["phase"],
            status=d["status"],
            design=DesignSpec.from_dict(d["design"]),
            engine_results=EngineResults.from_dict(d["engine_results"]),
            metrics={k: MetricsWithCI.from_dict(v) for k, v in d.get("metrics", {}).items()},
            fingerprint=d.get("fingerprint", {}),
            landscape=d.get("landscape", {}),
            recommended_config=d.get("recommended_config", {}),
            deploy_gate=d.get("deploy_gate", {}),
            created_at=d.get("created_at", datetime.now(timezone.utc).isoformat()),
            completed_at=d.get("completed_at"),
            error=d.get("error"),
            log_tail=d.get("log_tail", []),
        )

    def write(self, artifact_path: str) -> None:
        os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
        with open(artifact_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2, default=str)

    @classmethod
    def read(cls, artifact_path: str) -> "SuperMimicArtifact":
        with open(artifact_path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return cls.from_dict(d)


class ArtifactSchema:
    """JSON schema helpers for SuperMimicArtifact."""

    @staticmethod
    def empty(experiment_id: str, design: DesignSpec) -> SuperMimicArtifact:
        return SuperMimicArtifact(
            experiment_id=experiment_id,
            phase="design",
            status="designing",
            design=design,
            engine_results=EngineResults(),
        )


class Experiment:
    """Runtime representation of a Super MIMIC experiment."""

    def __init__(self, design: DesignSpec, experiment_id: Optional[str] = None):
        self.experiment_id = experiment_id or f"sm_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        self.design = design
        if design.output_dir is None:
            design.output_dir = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "Dev",
                "benchmarks",
                "super_mimic",
                self.experiment_id,
            )
        self.artifact = ArtifactSchema.empty(self.experiment_id, design)
        self.artifact_path = os.path.join(design.output_dir, "artifact.json")

    @property
    def output_dir(self) -> str:
        return self.design.output_dir or ""

    @property
    def status(self) -> str:
        return self.artifact.status

    def to_dict(self) -> Dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "design": self.design.to_dict(),
            "artifact": self.artifact.to_dict(),
            "artifact_path": self.artifact_path,
        }

    def save(self) -> None:
        self.artifact.write(self.artifact_path)

    def mark_running(self) -> None:
        self.artifact.phase = "execution"
        self.artifact.status = "running"
        self.save()

    def mark_completed(self, engine_results: EngineResults, recommended_config: Optional[Dict[str, Any]] = None) -> None:
        self.artifact.phase = "validation"
        self.artifact.status = "completed"
        self.artifact.engine_results = engine_results
        self.artifact.completed_at = datetime.now(timezone.utc).isoformat()
        if recommended_config:
            self.artifact.recommended_config = recommended_config
        self.save()

    def mark_failed(self, error: str) -> None:
        self.artifact.status = "failed"
        self.artifact.error = error
        self.artifact.completed_at = datetime.now(timezone.utc).isoformat()
        self.save()

    def update_log_tail(self, log_path: str, lines: int = 20) -> None:
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                self.artifact.log_tail = f.readlines()[-lines:]


class ExperimentRegistry:
    """DB-backed registry of Super MIMIC experiments."""

    def __init__(self, db_client=None):
        self.db_client = db_client

    def _get_db(self):
        if self.db_client is not None:
            return self.db_client
        from config import get_settings
        from db_client import get_db_client
        settings = get_settings()
        db_url = getattr(settings, "db_url", None)
        if not db_url:
            db_url = (
                f"postgresql://{settings.db_user}:{settings.db_password}"
                f"@{settings.db_host}:{settings.db_port}/{settings.db_name}"
            )
        return get_db_client(db_url)

    def create(self, experiment: Experiment) -> None:
        db = self._get_db()
        db.execute_update(
            """
            INSERT INTO super_mimic_experiments (
                id, created_at, updated_at, status, engine, mode, profile,
                windows, config, artifact_path, metrics, fingerprint,
                landscape, error, recommended_config
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(id) DO UPDATE SET
                updated_at=excluded.updated_at,
                status=excluded.status,
                artifact_path=excluded.artifact_path,
                metrics=excluded.metrics,
                fingerprint=excluded.fingerprint,
                landscape=excluded.landscape,
                error=excluded.error
            """,
            (
                experiment.experiment_id,
                experiment.artifact.created_at,
                experiment.artifact.created_at,
                experiment.artifact.status,
                experiment.artifact.design.engine,
                experiment.artifact.design.mode,
                experiment.artifact.design.profile,
                json.dumps(experiment.artifact.design.windows),
                json.dumps(experiment.artifact.design.to_dict()),
                experiment.artifact_path,
                json.dumps(experiment.artifact.metrics_to_serializable()),
                json.dumps(experiment.artifact.fingerprint),
                json.dumps(experiment.artifact.landscape),
                experiment.artifact.error,
                json.dumps(experiment.artifact.recommended_config),
            ),
        )

    def update(self, experiment: Experiment) -> None:
        db = self._get_db()
        db.execute_update(
            """
            UPDATE super_mimic_experiments
            SET updated_at = %s, status = %s, metrics = %s, fingerprint = %s,
                landscape = %s, error = %s, artifact_path = %s, recommended_config = %s
            WHERE id = %s
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                experiment.artifact.status,
                json.dumps(experiment.artifact.metrics_to_serializable()),
                json.dumps(experiment.artifact.fingerprint),
                json.dumps(experiment.artifact.landscape),
                experiment.artifact.error,
                experiment.artifact_path,
                json.dumps(experiment.artifact.recommended_config),
                experiment.experiment_id,
            ),
        )

    def list_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        db = self._get_db()
        return db.execute_query(
            "SELECT * FROM super_mimic_experiments ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )

    def get(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        db = self._get_db()
        rows = db.execute_query(
            "SELECT * FROM super_mimic_experiments WHERE id = %s",
            (experiment_id,),
        )
        return rows[0] if rows else None

    def record_deployment(
        self,
        experiment_id: str,
        target: str,
        mode: str,
        profile: str,
        changes: Dict[str, Any],
        rollback_payload: Dict[str, Any],
        operator: Optional[str] = None,
    ) -> str:
        from uuid import uuid4

        deployment_id = str(uuid4())
        db = self._get_db()
        db.execute_update(
            """
            INSERT INTO super_mimic_deployments (
                id, experiment_id, deployed_at, target, mode, profile,
                changes, rollback_payload, operator
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                deployment_id,
                experiment_id,
                datetime.now(timezone.utc).isoformat(),
                target,
                mode,
                profile,
                json.dumps(changes),
                json.dumps(rollback_payload),
                operator,
            ),
        )
        return deployment_id
