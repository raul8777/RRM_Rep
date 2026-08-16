"""FastAPI endpoints for Super MIMIC.

These routes are included into the main app under the /super-mimic prefix.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from super_mimic.core import DesignSpec, ExperimentRegistry, SuperMimicArtifact
from super_mimic.meta_learner import SuperMimicMetaLearner
from super_mimic.quantum_landscape import QuantumLandscapeAnalyzer
from super_mimic.runner import RoboticExperimentRunner

router = APIRouter(prefix="/super-mimic", tags=["super-mimic"])

# Shared in-memory runner instance (process-scoped)
_runner: Optional[RoboticExperimentRunner] = None


def _get_runner() -> RoboticExperimentRunner:
    global _runner
    if _runner is None:
        _runner = RoboticExperimentRunner()
    return _runner


class CreateExperimentRequest(BaseModel):
    mode: str = Field(default="RANGE_SCALP")
    profile: str = Field(default="A")
    windows: List[str] = Field(default_factory=list)
    engine: str = Field(default="mimic_sweep")
    strategy: str = Field(default="full")
    max_cycles: Optional[int] = Field(default=None)
    max_workers: int = Field(default=1)
    profiles: Optional[List[str]] = Field(default=None)
    param_grid: Optional[Dict[str, List[Any]]] = Field(default=None)
    param_grid_file: Optional[str] = Field(default=None)
    forced_regime: Optional[str] = Field(default=None)
    lifecycle: bool = Field(default=False)
    max_drawdown_stop_pct: Optional[float] = Field(default=15.0)
    output_dir: Optional[str] = Field(default=None)


class ExperimentResponse(BaseModel):
    ok: bool
    experiment_id: Optional[str] = None
    error: Optional[str] = None


class ExperimentStatusResponse(BaseModel):
    ok: bool
    experiment: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class ExperimentsListResponse(BaseModel):
    ok: bool
    experiments: List[Dict[str, Any]] = Field(default_factory=list)
    error: Optional[str] = None


@router.post("/experiment", response_model=ExperimentResponse)
async def create_experiment(request: Request, req: CreateExperimentRequest) -> Dict[str, Any]:
    """Create a new Super MIMIC experiment (design phase)."""
    try:
        design = DesignSpec(
            mode=req.mode,
            profile=req.profile,
            windows=req.windows,
            engine=req.engine,
            strategy=req.strategy,
            max_cycles=req.max_cycles,
            max_workers=req.max_workers,
            profiles=req.profiles,
            param_grid=req.param_grid,
            param_grid_file=req.param_grid_file,
            forced_regime=req.forced_regime,
            lifecycle=req.lifecycle,
            max_drawdown_stop_pct=req.max_drawdown_stop_pct,
            output_dir=req.output_dir,
        )
        experiment = _get_runner().create_experiment(design.to_dict())
        return {"ok": True, "experiment_id": experiment.experiment_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/experiment/{experiment_id}/start", response_model=ExperimentResponse)
async def start_experiment(experiment_id: str) -> Dict[str, Any]:
    """Start a previously created experiment in the background."""
    try:
        _get_runner().start_experiment(experiment_id)
        return {"ok": True, "experiment_id": experiment_id}
    except Exception as e:
        return {"ok": False, "experiment_id": experiment_id, "error": str(e)}


@router.get("/experiment/{experiment_id}", response_model=ExperimentStatusResponse)
async def get_experiment(experiment_id: str) -> Dict[str, Any]:
    """Get current status and artifact for an experiment."""
    try:
        status = _get_runner().get_status(experiment_id)
        if not status:
            raise HTTPException(status_code=404, detail="Experiment not found")
        return {"ok": True, "experiment": status}
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/experiments", response_model=ExperimentsListResponse)
async def list_experiments(limit: int = 50) -> Dict[str, Any]:
    """List recent experiments."""
    try:
        experiments = _get_runner().list_experiments(limit)
        return {"ok": True, "experiments": experiments}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/windows")
async def list_windows() -> Dict[str, Any]:
    """List available simulation windows."""
    try:
        windows_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "Dev", "benchmarks", "windows")
        windows = []
        if os.path.isdir(windows_dir):
            for fname in sorted(os.listdir(windows_dir)):
                if fname.endswith(".json"):
                    path = os.path.join(windows_dir, fname)
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            w = json.load(f)
                        windows.append({
                            "window_id": w.get("window_id", fname),
                            "start": w.get("start"),
                            "end": w.get("end"),
                            "symbols_count": len(w.get("symbols", []) or w.get("baseline_symbols", []) or []),
                        })
                    except Exception:
                        pass
        return {"ok": True, "windows": windows}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/experiment/{experiment_id}/cancel", response_model=ExperimentResponse)
async def cancel_experiment(experiment_id: str) -> Dict[str, Any]:
    """Cancel a running experiment."""
    try:
        cancelled = _get_runner().cancel(experiment_id)
        if cancelled:
            return {"ok": True, "experiment_id": experiment_id}
        return {"ok": False, "experiment_id": experiment_id, "error": "Experiment not running or already done"}
    except Exception as e:
        return {"ok": False, "experiment_id": experiment_id, "error": str(e)}


class DeployRequest(BaseModel):
    target: str = Field(default="kelly_config_override")
    operator: Optional[str] = Field(default=None)


@router.post("/experiment/{experiment_id}/deploy", response_model=ExperimentResponse)
async def deploy_experiment(experiment_id: str, req: DeployRequest) -> Dict[str, Any]:
    """Deploy a completed experiment's recommended config if the gate passes."""
    try:
        runner = _get_runner()
        exp = runner.get_status(experiment_id)
        if not exp:
            raise HTTPException(status_code=404, detail="Experiment not found")
        artifact = exp.get("artifact") or {}
        if artifact.get("status") != "completed":
            return {"ok": False, "experiment_id": experiment_id, "error": "Experiment not completed"}
        recommended = artifact.get("recommended_config") or {}
        if not recommended:
            return {"ok": False, "experiment_id": experiment_id, "error": "No recommended config"}

        # Enforce deploy gate
        from super_mimic.deploy_gate import DeployGate
        gate = DeployGate(runner.registry.db_client)
        gate_result = gate.evaluate(artifact)
        if not gate_result.passed:
            return {
                "ok": False,
                "experiment_id": experiment_id,
                "error": "Deploy gate blocked",
                "gate": gate_result.to_dict(),
            }

        # Apply config merge using existing governance helper (whitelist + runtime_state + audit)
        try:
            from app import _apply_kelly_config_patch
            kelly_result = _apply_kelly_config_patch(dict(recommended))
        except Exception as e:
            kelly_result = {"ok": False, "error": f"Config apply failed: {e}"}
        if not kelly_result.get("ok"):
            return {
                "ok": False,
                "experiment_id": experiment_id,
                "error": kelly_result.get("error"),
                "gate": gate_result.to_dict(),
            }

        # Record deployment audit
        apply_result = gate.apply_recommended_config(
            experiment_id=experiment_id,
            recommended_config=recommended,
            target=req.target,
            operator=req.operator,
            mode=exp.get("mode", ""),
            profile=exp.get("profile", ""),
        )

        return {
            "ok": True,
            "experiment_id": experiment_id,
            "gate": gate_result.to_dict(),
            "kelly": kelly_result,
            "recorded": apply_result,
        }
    except Exception as e:
        return {"ok": False, "experiment_id": experiment_id, "error": str(e)}


@router.post("/experiment/{experiment_id}/rollback", response_model=ExperimentResponse)
async def rollback_experiment(experiment_id: str, req: DeployRequest) -> Dict[str, Any]:
    """Rollback the last deployment of an experiment by restoring the saved payload."""
    try:
        runner = _get_runner()
        exp = runner.get_status(experiment_id)
        if not exp:
            raise HTTPException(status_code=404, detail="Experiment not found")

        reg = ExperimentRegistry(runner.registry.db_client)
        rows = runner.registry.db_client.execute_query(
            """
            SELECT changes, rollback_payload, target, mode, profile
            FROM super_mimic_deployments
            WHERE experiment_id = %s
            ORDER BY deployed_at DESC
            LIMIT 1
            """,
            (experiment_id,),
        )
        if not rows:
            return {"ok": False, "experiment_id": experiment_id, "error": "No deployment found"}

        last = rows[0]
        rollback_payload = last.get("rollback_payload") or {}
        target = last.get("target") or "kelly_config_override"

        # Restore runtime override if the rollback payload is a dict and non-empty
        kelly_result = {"ok": True, "applied": {}, "message": "no rollback payload"}
        if isinstance(rollback_payload, dict) and rollback_payload:
            try:
                from app import _apply_kelly_config_patch
                kelly_result = _apply_kelly_config_patch(dict(rollback_payload))
            except Exception as e:
                kelly_result = {"ok": False, "error": f"Rollback apply failed: {e}"}

        if not kelly_result.get("ok"):
            return {
                "ok": False,
                "experiment_id": experiment_id,
                "error": kelly_result.get("error"),
            }

        # Record rollback audit
        reg.record_deployment(
            experiment_id=experiment_id,
            target=target,
            mode=last.get("mode", ""),
            profile=last.get("profile", ""),
            changes={"rollback": True, "restored": rollback_payload},
            rollback_payload={},
            operator=req.operator,
        )

        return {
            "ok": True,
            "experiment_id": experiment_id,
            "kelly": kelly_result,
        }
    except Exception as e:
        return {"ok": False, "experiment_id": experiment_id, "error": str(e)}


@router.get("/meta/fingerprint")
async def meta_fingerprint(request: Request) -> Dict[str, Any]:
    """Capture current regime fingerprint."""
    try:
        meta = SuperMimicMetaLearner()
        fp = meta.capture_fingerprint()
        return {"ok": True, "fingerprint": fp.to_dict()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/meta/warm-start")
async def meta_warm_start(
    mode: str, profile: str = "A", top_k: int = 5
) -> Dict[str, Any]:
    """Recommend warm-start configs from historically similar fingerprints."""
    try:
        meta = SuperMimicMetaLearner()
        fp = meta.capture_fingerprint(mode=mode)
        recs = meta.recommend_warm_start(
            fingerprint=fp, mode=mode, profile=profile, top_k=top_k
        )
        return {"ok": True, "fingerprint": fp.to_dict(), "recommendations": recs}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/meta/retrain")
async def meta_retrain() -> Dict[str, Any]:
    """Trigger predictor retrain (learning_engine.run)."""
    try:
        meta = SuperMimicMetaLearner()
        result = meta.retrain_predictor(min_trades=5, lookback_days=30)
        return {"ok": result.get("status") != "failed", "result": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/meta/summary")
async def meta_summary(lookback_days: int = 90) -> Dict[str, Any]:
    """Summarize recent experiment results per mode/profile."""
    try:
        meta = SuperMimicMetaLearner()
        summary = meta.learn_from_recent_experiments(lookback=lookback_days)
        return {"ok": True, "summary": summary}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/experiment/{experiment_id}/landscape")
async def experiment_landscape(experiment_id: str) -> Dict[str, Any]:
    """Return stored quantum landscape for a completed experiment."""
    try:
        exp = _get_runner().get_status(experiment_id)
        if not exp:
            raise HTTPException(status_code=404, detail="Experiment not found")
        artifact = exp.get("artifact") or {}
        landscape = artifact.get("landscape") or {}
        return {"ok": True, "experiment_id": experiment_id, "landscape": landscape}
    except Exception as e:
        return {"ok": False, "experiment_id": experiment_id, "error": str(e)}


@router.get("/experiment/{experiment_id}/attractors")
async def experiment_attractors(experiment_id: str) -> Dict[str, Any]:
    """Return detected attractors for a completed experiment."""
    try:
        exp = _get_runner().get_status(experiment_id)
        if not exp:
            raise HTTPException(status_code=404, detail="Experiment not found")
        artifact = exp.get("artifact") or {}
        landscape = artifact.get("landscape") or {}
        attractors = (landscape.get("attractors") or []) if isinstance(landscape, dict) else []
        return {"ok": True, "experiment_id": experiment_id, "attractors": attractors}
    except Exception as e:
        return {"ok": False, "experiment_id": experiment_id, "error": str(e)}


class AnalyzeLandscapeRequest(BaseModel):
    results: List[Dict[str, Any]]
    param_x: str = Field(default="min_signal_strength")
    param_y: str = Field(default="stop_loss_pct")
    metric: str = Field(default="sharpe")
    grid_size: int = Field(default=20)


@router.post("/analyze-landscape")
async def analyze_landscape(req: AnalyzeLandscapeRequest) -> Dict[str, Any]:
    """Analyze a raw results list and return landscape + attractors."""
    try:
        analyzer = QuantumLandscapeAnalyzer(req.results)
        landscape = analyzer.build_landscape(
            param_x=req.param_x,
            param_y=req.param_y,
            metric=req.metric,
            grid_size=req.grid_size,
        )
        attractors = analyzer.detect_attractors(metric=req.metric)
        return {
            "ok": True,
            "landscape": landscape.to_dict(),
            "attractors": [a.to_dict() for a in attractors],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
