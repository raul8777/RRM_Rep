"""Robotic execution runner for Super MIMIC experiments."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from concurrent.futures import ProcessPoolExecutor, Future
from typing import Any, Callable, Dict, Optional

from super_mimic.core import DesignSpec, Experiment, ExperimentRegistry, SuperMimicArtifact
from super_mimic.engines import (
    run_backtest_experiment,
    run_mimic_sweep_experiment,
    run_monte_carlo_experiment,
)
from super_mimic.meta_learner import SuperMimicMetaLearner
from super_mimic.quantum_landscape import QuantumLandscapeAnalyzer


class RoboticExperimentRunner:
    """Execute Super MIMIC experiments and manage their lifecycle."""

    def __init__(self, registry: Optional[ExperimentRegistry] = None):
        self.registry = registry or ExperimentRegistry()
        self._futures: Dict[str, Future] = {}
        self._executor = ProcessPoolExecutor(max_workers=2)

    def create_experiment(self, design: Dict[str, Any]) -> Experiment:
        """Create an experiment from a design dict and persist it."""
        from super_mimic.core import DesignSpec
        spec = DesignSpec.from_dict(design)
        experiment = Experiment(design=spec)
        experiment.save()
        self.registry.create(experiment)
        return experiment

    def start_experiment(self, experiment_id: str) -> Experiment:
        """Launch an experiment in a background worker."""
        row = self.registry.get(experiment_id)
        if not row:
            raise ValueError(f"Experiment {experiment_id} not found in registry")
        # Load from disk
        _config = row["config"]
        if not isinstance(_config, dict):
            _config = json.loads(_config)
        exp = Experiment(DesignSpec.from_dict(_config))
        exp.experiment_id = experiment_id
        exp.artifact = SuperMimicArtifact.read(row["artifact_path"])

        exp.mark_running()
        self.registry.update(exp)

        future = self._executor.submit(_run_experiment_worker, exp.to_dict())
        self._futures[experiment_id] = future
        return exp

    def get_status(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """Return current status and artifact for an experiment."""
        row = self.registry.get(experiment_id)
        if not row:
            return None
        if os.path.exists(row["artifact_path"]):
            with open(row["artifact_path"], "r", encoding="utf-8") as f:
                artifact = json.load(f)
            return {**row, "artifact": artifact}
        return row

    def list_experiments(self, limit: int = 50) -> list[Dict[str, Any]]:
        return self.registry.list_recent(limit)

    def cancel(self, experiment_id: str) -> bool:
        future = self._futures.get(experiment_id)
        if future and not future.done():
            future.cancel()
            return True
        return False


def _run_experiment_worker(experiment_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Worker function that runs in a separate process."""
    from super_mimic.core import (
        DesignSpec,
        EngineResults,
        Experiment,
        ExperimentRegistry,
        SuperMimicArtifact,
    )
    from super_mimic.meta_learner import SuperMimicMetaLearner

    experiment = Experiment(DesignSpec.from_dict(experiment_dict["design"]))
    experiment.experiment_id = experiment_dict["experiment_id"]
    if os.path.exists(experiment.artifact_path):
        experiment.artifact = SuperMimicArtifact.read(experiment.artifact_path)
    else:
        experiment.artifact = SuperMimicArtifact.from_dict(experiment_dict["artifact"])

    meta = SuperMimicMetaLearner()

    # ── Phase 1: Regime fingerprint and optional predictor retrain ───────────
    try:
        fingerprint = meta.capture_fingerprint(mode=experiment.design.mode)
        experiment.artifact.fingerprint = fingerprint.to_dict()
        experiment.save()
    except Exception as e:
        fingerprint = None
        experiment.artifact.error = f"fingerprint_failed: {e}"
        experiment.save()

    # For full strategy, refresh predictor weights before simulating
    if experiment.design.strategy == "full":
        try:
            retrain_result = meta.retrain_predictor(min_trades=5, lookback_days=30)
            experiment.artifact.engine_results.summary["predictor_retrain"] = retrain_result
            experiment.save()
        except Exception as e:
            experiment.artifact.engine_results.summary["predictor_retrain"] = {
                "status": "failed",
                "error": str(e),
            }
            experiment.save()

    # ── Phase 2: Run selected engine ──────────────────────────────────────────
    try:
        engine = experiment.design.engine
        if engine == "mimic_sweep":
            engine_results, recommended_config = run_mimic_sweep_experiment(experiment.design)
        elif engine == "backtest":
            engine_results, recommended_config = run_backtest_experiment(experiment.design)
        elif engine == "monte_carlo":
            engine_results, recommended_config = run_monte_carlo_experiment(experiment.design)
        else:
            raise NotImplementedError(f"Engine {engine} not yet implemented")

        # Persist fingerprint + comparable metrics for meta-learning
        if fingerprint is not None:
            try:
                metrics = _extract_meta_metrics(engine_results, recommended_config)
                meta.record_experiment_result(
                    experiment.experiment_id,
                    fingerprint,
                    metrics,
                )
            except Exception:
                pass

        # Quantum landscape / attractor analysis
        try:
            analyzer = QuantumLandscapeAnalyzer(engine_results.results)
            landscape = analyzer.build_landscape(
                param_x="min_signal_strength",
                param_y="stop_loss_pct",
                metric="sharpe",
                grid_size=20,
            )
            attractors = analyzer.detect_attractors(
                metric="sharpe",
                min_metric=0.0,
                max_drawdown=20.0,
                neighbor_std_threshold=0.15,
                radius=0.05,
            )
            engine_results.summary["quantum_landscape"] = {
                "landscape": landscape.to_dict(),
                "attractors": [a.to_dict() for a in attractors],
            }
            experiment.artifact.landscape = engine_results.summary["quantum_landscape"]
            experiment.artifact.deploy_gate["attractors_count"] = len(attractors)
        except Exception:
            pass

        # Deploy gate: hard rules + ML advisor
        try:
            from super_mimic.deploy_gate import DeployGate
            gate = DeployGate()
            experiment.artifact.deploy_gate = {
                **experiment.artifact.deploy_gate,
                **gate.evaluate(experiment.artifact.to_dict()).to_dict(),
            }
        except Exception:
            pass

        experiment.mark_completed(engine_results, recommended_config)
        ExperimentRegistry().update(experiment)
        return {"ok": True, "experiment_id": experiment.experiment_id}
    except Exception as e:
        error = f"{e}\n{traceback.format_exc()}"
        experiment.mark_failed(error)
        ExperimentRegistry().update(experiment)
        return {"ok": False, "error": error, "experiment_id": experiment.experiment_id}


def _extract_meta_metrics(
    engine_results: EngineResults,
    recommended_config: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Flatten best recommendation metrics for meta-learner storage."""
    recs = engine_results.recommendations or []
    best = recs[0] if recs else (recommended_config or {})
    return {
        "pnl_per_day_usdt": best.get("best_pnl_per_day_usdt", 0.0),
        "win_rate": best.get("best_win_rate", 0.0),
        "sharpe": best.get("best_sharpe", 0.0),
        "trades": best.get("best_trades", 0),
        "drawdown_pct": best.get("best_drawdown_pct", 0.0),
    }
