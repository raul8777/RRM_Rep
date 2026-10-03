"""Engine adapters for Super MIMIC.

Each engine accepts a DesignSpec and produces an EngineResults artifact.
Currently supports the MIMIC mode×profile sweep; backtest, Monte Carlo and
predictor engines are added in later phases.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional


def _effective_worker_count(requested: int, memory_per_worker_mb: int) -> int:
    requested = max(1, min(int(requested or 8), 10))
    available_mb = None
    try:
        import psutil
        available_mb = int(psutil.virtual_memory().available / (1024 * 1024))
    except Exception:
        pass
    cgroup_remaining_mb = None
    try:
        with open("/sys/fs/cgroup/memory.max", "r", encoding="utf-8") as handle:
            raw_max = handle.read().strip()
        with open("/sys/fs/cgroup/memory.current", "r", encoding="utf-8") as handle:
            raw_current = handle.read().strip()
        if raw_max != "max":
            cgroup_remaining_mb = max(0, (int(raw_max) - int(raw_current)) // (1024 * 1024))
    except Exception:
        pass
    if cgroup_remaining_mb is not None:
        available_mb = min(available_mb, cgroup_remaining_mb) if available_mb is not None else cgroup_remaining_mb
    if available_mb is None and os.name == "nt":
        try:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(MemoryStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            available_mb = int(status.available_physical / (1024 * 1024))
        except Exception:
            pass
    if available_mb is None:
        return requested
    reserve_mb = max(1024, int(available_mb * 0.20))
    memory_workers = max(1, (available_mb - reserve_mb) // max(256, memory_per_worker_mb))
    return max(1, min(requested, memory_workers))

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, repo_root)

from super_mimic.core import DesignSpec, EngineResults


def _best_recommendation(report: Dict[str, Any], profile: str) -> Optional[Dict[str, Any]]:
    """Pick the best config recommendation for the requested profile."""
    recommendations = report.get("recommendations", [])
    for rec in recommendations:
        if rec.get("profile") == profile:
            return rec
    return None


def _extract_recommended_params(rec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Flatten a recommendation into a config dict safe for regime_profiles."""
    if not rec:
        return {}
    params = rec.get("best_params") or {}
    overrides = rec.get("best_profile_overrides") or {}
    # Merge overrides under their profile key
    return {**params, **overrides}


def run_sweep(design: DesignSpec) -> Dict[str, Any]:
    """Execute a MIMIC mode×profile sweep as an isolated subprocess.

    Spawning a separate process for the sweep avoids nesting a
    ProcessPoolExecutor inside the runner's own process pool, which would
    deadlock when ``design.max_workers`` is 10 or more.

    Returns the parsed sweep report dict.
    """
    output_dir = design.output_dir or os.path.join(
        repo_root, "Dev", "benchmarks", "super_mimic", "unknown"
    )
    os.makedirs(output_dir, exist_ok=True)

    script_path = os.path.join(repo_root, "Dev", "scripts", "mimic_mode_profile_sweep.py")
    effective_workers = _effective_worker_count(design.max_workers, design.memory_per_worker_mb)
    cmd = [
        sys.executable,
        script_path,
        "--strategy", design.strategy,
        "--max-workers", str(effective_workers),
        "--max-jobs", str(design.max_jobs),
        "--seed", str(design.seed),
        "--max-dd-stop", str(design.max_drawdown_stop_pct if design.max_drawdown_stop_pct is not None else 15.0),
        "--output-dir", output_dir,
    ]

    if design.stratified_jobs:
        cmd += ["--stratified-jobs"]
    if design.max_symbols is not None:
        cmd += ["--max-symbols", str(design.max_symbols)]
    if design.certification_mode:
        cmd += ["--no-cache"]
    if design.window_manifest_dir:
        cmd += ["--windows-dir", design.window_manifest_dir]
    if design.windows:
        cmd += ["--windows", ",".join(design.windows)]
    if design.max_cycles is not None:
        cmd += ["--max-cycles", str(design.max_cycles)]
    if design.lifecycle:
        cmd += ["--lifecycle"]
    if design.e2e_lifecycle_enabled:
        cmd += ["--e2e-lifecycle"]
    if design.forced_regime:
        cmd += ["--forced-regime", str(design.forced_regime)]
    if design.profiles and len(design.profiles) == 1:
        cmd += ["--profile", str(design.profiles[0])]

    grid_file: Optional[str] = None
    if design.strategy == "custom_grid":
        if design.param_grid_file and os.path.exists(design.param_grid_file):
            grid_file = design.param_grid_file
        elif design.param_grid:
            grid_file = os.path.join(output_dir, "custom_param_grid.json")
            with open(grid_file, "w", encoding="utf-8") as f:
                json.dump(design.param_grid, f, ensure_ascii=False, indent=2)
        if grid_file:
            cmd += ["--param-grid-file", grid_file]
        else:
            raise ValueError("custom_grid strategy requires param_grid or param_grid_file")

    log_path = os.path.join(output_dir, "mimic_sweep.log")
    with open(log_path, "w", encoding="utf-8") as log_fh:
        completed = subprocess.run(
            cmd,
            cwd=repo_root,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )

    paths = {
        "status_path": os.path.join(output_dir, "mimic_sweep_status.json"),
        "results_path": os.path.join(output_dir, "mimic_sweep_results.jsonl"),
        "report_path": os.path.join(output_dir, "mimic_sweep_report.json"),
    }

    if completed.returncode != 0:
        raise RuntimeError(
            f"mimic_mode_profile_sweep subprocess failed (exit {completed.returncode}); "
            f"see log: {log_path}"
        )

    if not os.path.exists(paths["report_path"]):
        raise RuntimeError(
            f"Sweep subprocess produced no report. See log: {log_path}"
        )

    with open(paths["report_path"], "r", encoding="utf-8") as f:
        report = json.load(f)

    report.setdefault("summary", {})["requested_workers"] = design.max_workers
    report["summary"]["effective_workers"] = effective_workers
    report["summary"]["memory_per_worker_mb"] = design.memory_per_worker_mb
    report["summary"]["certification_mode"] = design.certification_mode
    report["summary"]["experiment_stage"] = design.experiment_stage
    report["_paths"] = paths
    return report


def run_mimic_sweep_experiment(design: DesignSpec) -> tuple[EngineResults, Dict[str, Any]]:
    """Run a MIMIC mode×profile sweep and return EngineResults plus recommended config."""
    report = run_sweep(design)
    paths = report.pop("_paths", None)
    if paths is None:
        output_dir = design.output_dir or os.path.join(
            repo_root, "Dev", "benchmarks", "super_mimic", "unknown"
        )
        paths = {
            "status_path": os.path.join(output_dir, "mimic_sweep_status.json"),
            "results_path": os.path.join(output_dir, "mimic_sweep_results.jsonl"),
            "report_path": os.path.join(output_dir, "mimic_sweep_report.json"),
        }

    rec = _best_recommendation(report, design.profile)
    recommended_config = _extract_recommended_params(rec)
    from super_mimic.certification import build_differentiation_report, build_invariant_report, build_reproducibility_report
    report.setdefault("summary", {})["differentiation"] = build_differentiation_report(report.get("results", []))
    report["summary"]["reproducibility"] = build_reproducibility_report(report.get("results", []))
    report["summary"]["invariants"] = build_invariant_report(report.get("results", []), design.experiment_stage)
    if design.certification_mode:
        certification_path = os.path.join(design.output_dir or repo_root, "certification_report.json")
        with open(certification_path, "w", encoding="utf-8") as handle:
            json.dump({
                "experiment_stage": design.experiment_stage,
                "certification_mode": True,
                "apply_allowed": False,
                "differentiation": report["summary"]["differentiation"],
                "reproducibility": report["summary"]["reproducibility"],
                "invariants": report["summary"]["invariants"],
            }, handle, ensure_ascii=False, indent=2)
        report["summary"]["certification_report_path"] = certification_path

    return EngineResults(
        engine="mimic_sweep",
        summary=report.get("summary", {}),
        recommendations=report.get("recommendations", []),
        results=report.get("results", []),
        report_path=paths["report_path"],
        results_path=paths["results_path"],
        status_path=paths["status_path"],
    ), recommended_config


def run_backtest_experiment(design: DesignSpec) -> tuple[EngineResults, Dict[str, Any]]:
    if len(design.windows) < 2:
        raise ValueError("Independent walk-forward backtest requires at least two ordered windows")
    split = max(1, int(len(design.windows) * 0.70))
    if split >= len(design.windows):
        split = len(design.windows) - 1
    development = DesignSpec.from_dict({
        **design.to_dict(),
        "windows": design.windows[:split],
        "engine": "mimic_sweep",
        "output_dir": os.path.join(design.output_dir or repo_root, "development"),
    })
    development_results, recommended_config = run_mimic_sweep_experiment(development)
    if not recommended_config:
        raise RuntimeError("Development sweep produced no recommended configuration")
    validation = DesignSpec.from_dict({
        **design.to_dict(),
        "windows": design.windows[split:],
        "engine": "mimic_sweep",
        "strategy": "custom_grid",
        "param_grid": {key: [value] for key, value in recommended_config.items() if not isinstance(value, dict)},
        "param_grid_file": None,
        "output_dir": os.path.join(design.output_dir or repo_root, "validation"),
        "max_jobs": min(design.max_jobs, max(1, len(design.windows[split:]) * len(design.profiles or [design.profile]))),
    })
    validation_results, _ = run_mimic_sweep_experiment(validation)
    summary = {
        "engine_note": "purged chronological development/validation windows",
        "development_windows": development.windows,
        "validation_windows": validation.windows,
        "development": development_results.summary,
        "validation": validation_results.summary,
        "validation_runs": len(validation_results.results),
    }
    output_dir = design.output_dir or os.path.join(repo_root, "Dev", "benchmarks", "super_mimic", "backtest")
    os.makedirs(output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, "walk_forward_report.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    return EngineResults(
        engine="walk_forward_backtest",
        summary=summary,
        recommendations=development_results.recommendations,
        results=validation_results.results,
        report_path=report_path,
        results_path=validation_results.results_path,
        status_path=validation_results.status_path,
    ), recommended_config


def run_aic_certification_experiment(design: DesignSpec) -> tuple[EngineResults, Dict[str, Any]]:
    scenarios = [
        ("expected_fill", 0.0, 0.0004, 0.0),
        ("moderate_slippage", 0.0005, 0.0004, 0.0),
        ("severe_slippage", 0.0020, 0.0004, 0.0),
        ("higher_fee", 0.0, 0.0010, 0.0),
        ("positive_funding", 0.0, 0.0004, 0.50),
        ("negative_funding", 0.0, 0.0004, -0.50),
        ("trigger_fill_gap", 0.0010, 0.0004, 0.0),
        ("multi_fill_group", 0.0007, 0.0004, 0.0),
        ("market_fallback", 0.0015, 0.0004, 0.0),
        ("estimated_fallback", 0.0012, 0.0004, 0.0),
    ]
    source_cohort = []
    if design.entry_cohort_path and os.path.exists(design.entry_cohort_path):
        with open(design.entry_cohort_path, "r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                for item in ((row.get("e2e_evidence") or {}).get("lineage") or []):
                    if item.get("entry_price") and item.get("quantity"):
                        source_cohort.append(item)
    cohorts = []
    if source_cohort:
        for index, item in enumerate(source_cohort):
            raw_side = str(item.get("selected_side") or item.get("executed_side") or "LONG").upper()
            cohorts.append({
                "id": index,
                "entry": float(item["entry_price"]),
                "qty": float(item["quantity"]),
                "side": "SHORT" if "SHORT" in raw_side else "LONG",
                "expected_move": 0.01,
            })
    else:
        for cohort in range(5):
            for side in ("LONG", "SHORT"):
                cohorts.append({
                    "id": cohort,
                    "entry": 100.0 + cohort * 10.0,
                    "qty": 10.0,
                    "side": side,
                    "expected_move": 0.01 + cohort * 0.001,
                })
    results = []
    for cohort in cohorts:
        entry = cohort["entry"]
        qty = cohort["qty"]
        side = cohort["side"]
        expected_move = cohort["expected_move"]
        direction = 1.0 if side == "LONG" else -1.0
        expected_exit = entry * (1.0 + direction * expected_move)
        for scenario, slippage, fee_rate, funding in scenarios:
            real_exit = expected_exit * (1.0 - direction * slippage)
            gross = direction * (real_exit - entry) * qty
            fees = (entry + real_exit) * qty * fee_rate
            net = gross - fees + funding
            expected_gross = direction * (expected_exit - entry) * qty
            results.append({
                "window_id": f"aic_cohort_{cohort['id']}",
                "profile": design.profile,
                "params": {"scenario": scenario, "side": side},
                "scenario": scenario,
                "side": side,
                "entry_price": entry,
                "expected_exit_price": expected_exit,
                "real_exit_price": real_exit,
                "expected_gross_pnl": expected_gross,
                "realized_gross_pnl": gross,
                "realized_net_pnl": net,
                "realized_slippage_usdt": expected_gross - gross,
                "fees_usdt": fees,
                "funding_usdt": funding,
                "accounting_mode": "price_estimated_fallback" if scenario == "estimated_fallback" else "simulated_fill_verified",
                "close_group_count": 1,
                "trades": 1,
                "pnl_usdt": net,
                "pnl_pct": net / (entry * qty) * 100.0,
            })
    results = results[:design.max_jobs]
    from super_mimic.certification import build_differentiation_report, build_invariant_report, build_reproducibility_report
    summary = {
        "total_runs": len(results),
        "experiment_stage": "aic",
        "cohort_source": "entry_results" if source_cohort else "synthetic_fallback",
        "cohort_records": len(source_cohort),
        "differentiation": build_differentiation_report(results),
        "reproducibility": build_reproducibility_report(results),
        "invariants": build_invariant_report(results, "aic"),
    }
    output_dir = design.output_dir or os.path.join(repo_root, "Dev", "benchmarks", "super_mimic", "aic")
    os.makedirs(output_dir, exist_ok=True)
    results_path = os.path.join(output_dir, "aic_results.jsonl")
    with open(results_path, "w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
    report_path = os.path.join(output_dir, "aic_report.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    return EngineResults(
        engine="aic_certification",
        summary=summary,
        results=results,
        report_path=report_path,
        results_path=results_path,
    ), {}


def run_monte_carlo_experiment(design: DesignSpec) -> tuple[EngineResults, Dict[str, Any]]:
    """Run MIMIC sweep and add Monte Carlo bootstrap confidence intervals.

    Bootstraps the raw results to estimate stability of the recommended config.
    """
    import numpy as np

    results, recommended_config = run_mimic_sweep_experiment(design)
    results.engine = "monte_carlo"

    raw = results.results
    metric_keys = ["sharpe", "pnl_per_day_usdt", "drawdown_pct", "win_rate"]
    mc_summary = {}
    rng = np.random.default_rng(design.seed)
    for key in metric_keys:
        vals = [float(r[key]) for r in raw if key in r and r[key] is not None]
        if len(vals) < 5:
            continue
        arr = np.array(vals)
        n = len(arr)
        block_size = max(2, min(10, int(np.sqrt(n))))
        boots = []
        for _ in range(1000):
            blocks = []
            while sum(len(block) for block in blocks) < n:
                start = int(rng.integers(0, n))
                indices = [(start + offset) % n for offset in range(block_size)]
                blocks.append(arr[indices])
            sample = np.concatenate(blocks)[:n]
            boots.append(float(np.mean(sample)))
        boots = np.array(boots)
        mc_summary[key] = {
            "mean": float(np.mean(arr)),
            "std": float(np.std(arr)),
            "ci_low": float(np.percentile(boots, 5)),
            "ci_high": float(np.percentile(boots, 95)),
            "block_size": block_size,
        }

    pnl_sequence = np.array([float(r.get("pnl_usdt", 0.0)) for r in raw], dtype=float)
    if len(pnl_sequence) >= 5:
        equity = np.cumsum(pnl_sequence)
        peaks = np.maximum.accumulate(np.concatenate(([0.0], equity)))[1:]
        drawdowns = peaks - equity
        mc_summary["sequence_risk"] = {
            "max_drawdown_usdt": float(drawdowns.max()) if len(drawdowns) else 0.0,
            "loss_run_probability": float(np.mean(pnl_sequence < 0)),
            "ending_pnl_usdt": float(equity[-1]),
        }

    results.summary["monte_carlo"] = mc_summary
    results.summary["engine_note"] = "Deterministic moving-block bootstrap over ordered MIMIC results"
    return results, recommended_config
