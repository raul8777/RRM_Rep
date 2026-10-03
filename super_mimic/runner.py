"""Robotic execution runner for Super MIMIC experiments."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
import uuid
from concurrent.futures import ProcessPoolExecutor, Future
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from super_mimic.core import DesignSpec, Experiment, ExperimentRegistry, SuperMimicArtifact
from super_mimic.engines import (
    run_aic_certification_experiment,
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
        self._suite_executor = ProcessPoolExecutor(max_workers=1)
        self._suites: Dict[str, Dict[str, Any]] = {}
        self._suites_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "Dev", "benchmarks", "super_mimic", "suites"
        )
        os.makedirs(self._suites_dir, exist_ok=True)

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
        existing_future = self._futures.get(experiment_id)
        if existing_future and not existing_future.done():
            raise ValueError(f"Experiment {experiment_id} is already running")
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

    def get_status(self, experiment_id: str, include_results: bool = True) -> Optional[Dict[str, Any]]:
        """Return current status and artifact for an experiment."""
        row = self.registry.get(experiment_id)
        if not row:
            return None
        if os.path.exists(row["artifact_path"]):
            with open(row["artifact_path"], "r", encoding="utf-8") as f:
                artifact = json.load(f)
            progress = None
            status_path = (
                artifact.get("engine_results", {}).get("status_path")
                or os.path.join(os.path.dirname(row["artifact_path"]), "mimic_sweep_status.json")
            )
            if status_path and os.path.exists(status_path):
                try:
                    with open(status_path, "r", encoding="utf-8") as f:
                        progress = json.load(f)
                except Exception:
                    progress = None
            future = self._futures.get(experiment_id)
            worker_state = "running" if future and not future.done() else artifact.get("status")
            if not include_results:
                artifact = dict(artifact)
                engine_results = dict(artifact.get("engine_results") or {})
                engine_results["results_count"] = len(engine_results.get("results") or [])
                engine_results["results"] = []
                artifact["engine_results"] = engine_results
            return {**row, "artifact": artifact, "progress": progress, "worker_state": worker_state}
        return row

    def list_experiments(self, limit: int = 50) -> list[Dict[str, Any]]:
        return self.registry.list_recent(limit)

    def cancel(self, experiment_id: str) -> bool:
        future = self._futures.get(experiment_id)
        if future and not future.done():
            future.cancel()
            return True
        return False

    def create_certification_suite(self, design: Dict[str, Any]) -> Dict[str, Any]:
        suite_id = f"smc_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        base = dict(design)
        stage_overrides = base.pop("stage_overrides", {}) or {}
        base.update({"certification_mode": True, "apply_allowed": False, "auto_start": False})
        experiments = []
        for stage in ("entry", "kelly", "lifecycle", "aic", "e2e"):
            stage_design = {
                **base,
                **stage_overrides.get(stage, {}),
                "experiment_stage": stage,
                "lifecycle": stage == "lifecycle",
                "e2e_lifecycle_enabled": bool(
                    stage_overrides.get(stage, {}).get("e2e_lifecycle_enabled", base.get("e2e_lifecycle_enabled"))
                ) and stage == "e2e",
            }
            experiment = self.create_experiment(stage_design)
            experiments.append(experiment.experiment_id)
        state = {
            "suite_id": suite_id,
            "status": "queued",
            "stages": ["entry", "kelly", "lifecycle", "aic", "e2e"],
            "experiments": experiments,
            "current_stage": None,
            "completed_stages": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._suites[suite_id] = state
        with open(os.path.join(self._suites_dir, f"{suite_id}.json"), "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
        future = self._suite_executor.submit(_run_certification_suite_worker, suite_id, experiments)
        state["future"] = future
        return {key: value for key, value in state.items() if key != "future"}

    def get_certification_suite_status(self, suite_id: str) -> Optional[Dict[str, Any]]:
        state = self._suites.get(suite_id)
        if not state:
            path = os.path.join(self._suites_dir, f"{suite_id}.json")
            if not os.path.exists(path):
                return None
            with open(path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
            self._suites[suite_id] = state
        statuses = [self.get_status(experiment_id, include_results=False) for experiment_id in state["experiments"]]
        completed = sum(1 for item in statuses if item and (item.get("artifact") or {}).get("status") == "completed")
        failed = sum(1 for item in statuses if item and (item.get("artifact") or {}).get("status") == "failed")
        future = state.get("future")
        if completed < len(statuses) and not failed and future is None:
            remaining = [
                experiment_id for experiment_id, item in zip(state["experiments"], statuses)
                if not item or (item.get("artifact") or {}).get("status") != "completed"
            ]
            future = self._suite_executor.submit(_run_certification_suite_worker, suite_id, remaining)
            state["future"] = future
        status = "failed" if failed else "completed" if completed == len(statuses) else "running" if future else state["status"]
        return {
            **{key: value for key, value in state.items() if key != "future"},
            "status": status,
            "completed_stages": completed,
            "failed_stages": failed,
            "experiment_statuses": statuses,
        }


def _find_entry_cohort_path(suite_id: str, registry: ExperimentRegistry) -> Optional[str]:
    suite_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "Dev",
        "benchmarks",
        "super_mimic",
        "suites",
        f"{suite_id}.json",
    )
    if not os.path.exists(suite_path):
        return None
    with open(suite_path, "r", encoding="utf-8") as handle:
        suite_state = json.load(handle)
    for experiment_id in suite_state.get("experiments", []):
        row = registry.get(experiment_id)
        if not row:
            continue
        config = row["config"] if isinstance(row["config"], dict) else json.loads(row["config"])
        if config.get("experiment_stage") == "entry" and row.get("artifact_path"):
            artifact = SuperMimicArtifact.read(row["artifact_path"])
            if artifact.status == "completed" and artifact.engine_results and artifact.engine_results.results_path:
                return artifact.engine_results.results_path
    return None


def _run_certification_suite_worker(suite_id: str, experiment_ids: list[str]) -> Dict[str, Any]:
    registry = ExperimentRegistry()
    results = []
    entry_cohort_path = _find_entry_cohort_path(suite_id, registry)
    for experiment_id in experiment_ids:
        row = registry.get(experiment_id)
        if not row:
            return {"ok": False, "suite_id": suite_id, "error": f"Missing experiment {experiment_id}"}
        config = row["config"] if isinstance(row["config"], dict) else json.loads(row["config"])
        if config.get("experiment_stage") in {"lifecycle", "aic"} and entry_cohort_path:
            config["entry_cohort_path"] = entry_cohort_path
        experiment = Experiment(DesignSpec.from_dict(config))
        experiment.experiment_id = experiment_id
        experiment.artifact = SuperMimicArtifact.read(row["artifact_path"])
        experiment.mark_running()
        registry.update(experiment)
        result = _run_experiment_worker(experiment.to_dict())
        results.append(result)
        if not result.get("ok"):
            return {"ok": False, "suite_id": suite_id, "results": results}
        updated = registry.get(experiment_id)
        if config.get("experiment_stage") == "entry" and updated and os.path.exists(updated["artifact_path"]):
            completed_artifact = SuperMimicArtifact.read(updated["artifact_path"])
            entry_cohort_path = completed_artifact.engine_results.results_path
    return {"ok": True, "suite_id": suite_id, "results": results}


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
    data_certificate_summary = None

    if experiment.design.certification_mode:
        try:
            windows_dir = experiment.design.window_manifest_dir or os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "Dev", "benchmarks", "windows"
            )
            selected = set(experiment.design.windows or [])
            manifests = []
            for name in sorted(os.listdir(windows_dir)):
                if not name.endswith(".json"):
                    continue
                with open(os.path.join(windows_dir, name), "r", encoding="utf-8") as handle:
                    manifest = json.load(handle)
                if not isinstance(manifest, dict) or not all(manifest.get(key) for key in ("window_id", "start", "end")):
                    continue
                if selected and manifest.get("window_id") not in selected and name[:-5] not in selected:
                    continue
                manifests.append(manifest)
            from super_mimic.window_certification import certify_manifests
            certificate_path = os.path.join(experiment.output_dir, "data_certificate.json")
            data_certificate = certify_manifests(
                manifests,
                os.path.join(os.path.dirname(os.path.dirname(__file__)), "historical_data"),
                experiment.design.experiment_stage,
                certificate_path,
            )
            if not selected:
                certified_windows = [
                    window["window_id"] for window in data_certificate["windows"] if window["certified"]
                ]
                experiment.design.windows = certified_windows[:5]
                data_certificate["auto_selected_windows"] = experiment.design.windows
                with open(certificate_path, "w", encoding="utf-8") as handle:
                    json.dump(data_certificate, handle, ensure_ascii=False, indent=2)
            data_certificate_summary = {
                "path": certificate_path,
                "total": data_certificate["total"],
                "certified": data_certificate["certified"],
                "rejected": data_certificate["rejected"],
            }
            experiment.artifact.engine_results.summary["data_certificate"] = data_certificate_summary
            experiment.save()
            if not manifests or data_certificate["certified"] == 0 or (selected and data_certificate["rejected"]):
                raise RuntimeError(
                    f"Window certification failed: {data_certificate['certified']}/{data_certificate['total']} certified"
                )
        except Exception as e:
            error = f"data_certification_failed: {e}\n{traceback.format_exc()}"
            experiment.mark_failed(error)
            ExperimentRegistry().update(experiment)
            return {"ok": False, "error": error, "experiment_id": experiment.experiment_id}

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
    if experiment.design.strategy == "full" and not experiment.design.certification_mode:
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
        if experiment.design.experiment_stage == "aic":
            engine_results, recommended_config = run_aic_certification_experiment(experiment.design)
        elif experiment.design.experiment_stage == "kelly" and experiment.design.certification_mode:
            from super_mimic.kelly_certification import run_kelly_certification
            engine_results, recommended_config = run_kelly_certification(
                experiment.design, os.path.dirname(os.path.dirname(__file__))
            )
        elif experiment.design.experiment_stage == "lifecycle" and experiment.design.certification_mode:
            from super_mimic.lifecycle_certification import run_lifecycle_certification
            engine_results, recommended_config = run_lifecycle_certification(
                experiment.design, os.path.dirname(os.path.dirname(__file__))
            )
        elif engine == "mimic_sweep":
            engine_results, recommended_config = run_mimic_sweep_experiment(experiment.design)
        elif engine == "backtest":
            engine_results, recommended_config = run_backtest_experiment(experiment.design)
        elif engine == "monte_carlo":
            engine_results, recommended_config = run_monte_carlo_experiment(experiment.design)
        else:
            raise NotImplementedError(f"Engine {engine} not yet implemented")

        if data_certificate_summary is not None:
            engine_results.summary["data_certificate"] = data_certificate_summary

        if experiment.design.certification_mode:
            checks = {
                "job_count": len(engine_results.results) == experiment.design.max_jobs,
                "differentiation": bool((engine_results.summary.get("differentiation") or {}).get("passed")),
                "reproducibility": bool((engine_results.summary.get("reproducibility") or {}).get("passed")),
                "invariants": bool((engine_results.summary.get("invariants") or {}).get("passed")),
            }
            if experiment.design.experiment_stage == "lifecycle":
                checks["real_entry_cohort"] = engine_results.summary.get("entry_cohort_source") == "entry_results"
                checks["entry_cohort_nonempty"] = int(engine_results.summary.get("entry_cohort_records") or 0) > 0
            if experiment.design.experiment_stage == "aic":
                checks["real_aic_cohort"] = engine_results.summary.get("cohort_source") == "entry_results"
                checks["aic_cohort_nonempty"] = int(engine_results.summary.get("cohort_records") or 0) > 0
            engine_results.summary["certification_checks"] = checks
            if not all(checks.values()):
                raise RuntimeError(f"Certification checks failed: {checks}")

        # Persist fingerprint + comparable metrics for meta-learning
        if fingerprint is not None and not experiment.design.certification_mode:
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
