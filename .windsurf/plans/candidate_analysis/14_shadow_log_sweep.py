#!/usr/bin/env python3
"""Forced-regime shadow-log sweep.

Runs every window under every regime with the evidence gate "open"
(threshold 0.0) while logging each entry's posterior and its eventual
closed PnL.  The resulting `evidence_gate_shadow_log.jsonl` is used to
calibrate the posterior formula and choose per-regime thresholds.
"""
from __future__ import annotations
import argparse
import faulthandler
import gc
import itertools
import json
import os
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List

faulthandler.enable()
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, "reconfigure") else None

repo_root = "/app"
sys.path.insert(0, repo_root)

import Dev.scripts.mimic_mode_profile_sweep as sweep

WINDOWS_DIR = os.path.join(repo_root, "Dev", "benchmarks", "windows")
sweep.WINDOWS_DIR = WINDOWS_DIR

OUTPUT_DIR = os.path.join(repo_root, "Dev", "benchmarks", "forced_regime_sweep", "shadow")
STATUS_PATH = os.path.join(OUTPUT_DIR, "mimic_sweep_status.json")
RESULTS_PATH = os.path.join(OUTPUT_DIR, "mimic_sweep_results.jsonl")
LOG_PATH = os.path.join(OUTPUT_DIR, "mimic_sweep.log")
SHADOW_LOG_PATH = os.path.join(repo_root, "Dev", "benchmarks", "forced_regime_sweep", "evidence_gate_shadow_log.jsonl")

MAX_WORKERS = 10
MAX_CYCLES = 200
DRAWDOWN_STOP_PCT = 15.0

REGIMES = ["SPOT_BULL", "RANGE_SCALP", "PRESERVATION", "FUTURES_SHORT"]
PROFILES = ["A", "C"]  # enough diversity without exploding runtime
THRESHOLDS = [0.0]     # gate passes all candidates; posterior still computed

BASE_PARAMS = {
    "SPOT_BULL": {
        "min_signal_strength": 0.45,
        "min_success_probability": 0.45,
        "max_positions_spot_bull": 5,
        "stop_loss_pct": 3.0,
        "category_max_positions_enabled": False,
        "entry_quality_vol_spike_min_range_scalp": 0.35,
    },
    "RANGE_SCALP": {
        "min_signal_strength": 0.6,
        "min_success_probability": 0.45,
        "max_positions_spot_bull": 5,
        "max_positions_range_scalp": 5,
        "stop_loss_pct": 3.0,
        "category_max_positions_enabled": False,
        "entry_quality_vol_spike_min_range_scalp": 0.20,
    },
    "FUTURES_SHORT": {
        "min_signal_strength": 0.45,
        "min_success_probability": 0.6,
        "max_positions_spot_bull": 5,
        "max_positions_futures_short": 5,
        "stop_loss_pct": 3.0,
        "category_max_positions_enabled": False,
        "entry_quality_vol_spike_min_range_scalp": 0.35,
    },
    "PRESERVATION": {
        "min_signal_strength": 0.6,
        "min_success_probability": 0.6,
        "max_positions_spot_bull": 5,
        "max_positions_preservation": 1,
        "stop_loss_pct": 3.0,
        "category_max_positions_enabled": False,
        "entry_quality_vol_spike_min_range_scalp": 0.35,
    },
}


def _make_job(window, regime, profile, threshold):
    params = dict(BASE_PARAMS[regime])
    params["evidence_gate_enabled"] = True
    params["evidence_gate_prior"] = 0.50
    params["evidence_gate_min_posterior_per_regime"] = {r: threshold for r in REGIMES}
    return {
        "window": window,
        "regime": regime,
        "profile": profile,
        "threshold": threshold,
        "params": params,
        "max_cycles": MAX_CYCLES,
        "windows_dir": WINDOWS_DIR,
        "status_path": STATUS_PATH,
        "results_path": RESULTS_PATH,
        "log_path": LOG_PATH,
        "shadow_log_path": SHADOW_LOG_PATH,
    }


def _load_windows():
    return sweep._load_windows()


def _existing_keys():
    if not os.path.exists(RESULTS_PATH):
        return set()
    keys = set()
    with open(RESULTS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                keys.add((r.get("window_id"), r.get("profile"), r.get("forced_regime"), r.get("threshold")))
            except Exception:
                pass
    return keys


def _run_job(job):
    sweep._DEFAULT_LOG_PATH = job["log_path"]
    sweep.STATUS_PATH = job["status_path"]
    sweep.RESULTS_PATH = job["results_path"]
    sweep.WINDOWS_DIR = job["windows_dir"]
    sweep.SHADOW_LOG_PATH = job["shadow_log_path"]

    params = dict(job["params"])
    profile = job["profile"]
    regime = job["regime"]
    window = job["window"]

    profile_overrides = sweep._build_profile_overrides(profile)

    try:
        res = sweep._run_single_config(
            window=window,
            profile=profile,
            params=params,
            profile_overrides=profile_overrides,
            max_cycles=job["max_cycles"],
            max_drawdown_stop_pct=DRAWDOWN_STOP_PCT,
            forced_regime=regime,
            shadow_log=True,
        )
        res["forced_regime"] = regime
        res["threshold"] = job["threshold"]
        return res
    except Exception as e:
        return {
            "error": str(e),
            "traceback": traceback.format_exc(),
            "window_id": window.get("window_id"),
            "profile": profile,
            "forced_regime": regime,
            "threshold": job["threshold"],
            "params": params,
        }


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    logger = sweep._configure_logging(LOG_PATH)
    logger.info("Starting shadow-log sweep: %s workers, %s cycles", MAX_WORKERS, MAX_CYCLES)

    windows = _load_windows()
    if not windows:
        raise RuntimeError(f"No windows found in {WINDOWS_DIR}")
    logger.info("Loaded %d windows", len(windows))

    jobs = []
    for window, regime, profile, threshold in itertools.product(windows, REGIMES, PROFILES, THRESHOLDS):
        jobs.append(_make_job(window, regime, profile, threshold))

    done = _existing_keys()
    pending = [j for j in jobs if (j["window"].get("window_id"), j["profile"], j["regime"], j["threshold"]) not in done]

    total = len(jobs)
    completed = len(done)
    failed = 0

    status = {
        "state": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "total_jobs": total,
        "completed_jobs": completed,
        "failed_jobs": 0,
        "pending_jobs": len(pending),
        "errors": [],
        "eta_seconds": None,
        "current_job": None,
        "regimes": REGIMES,
        "profiles": PROFILES,
        "thresholds": THRESHOLDS,
        "shadow_log_path": SHADOW_LOG_PATH,
    }
    sweep._save_status(status, STATUS_PATH)

    started = datetime.now(timezone.utc)
    processed = 0

    with ProcessPoolExecutor(max_workers=MAX_WORKERS, max_tasks_per_child=1) as executor:
        future_to_job = {executor.submit(_run_job, job): job for job in pending}
        for future in as_completed(future_to_job):
            job = future_to_job[future]
            processed += 1
            completed += 1
            try:
                res = future.result()
                if res.get("error"):
                    failed += 1
                    status["errors"].append({
                        "window_id": res.get("window_id"),
                        "profile": res.get("profile"),
                        "regime": res.get("forced_regime"),
                        "threshold": res.get("threshold"),
                        "error": res.get("error"),
                    })
                    logger.error("Job failed: %s %s %s t=%s -> %s", res.get("window_id"), res.get("profile"), res.get("forced_regime"), res.get("threshold"), res.get("error"))
                else:
                    sweep._append_result(res, RESULTS_PATH)
            except Exception as e:
                failed += 1
                status["errors"].append({
                    "window_id": job["window"].get("window_id"),
                    "profile": job["profile"],
                    "regime": job["regime"],
                    "threshold": job["threshold"],
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                })
                logger.error("Future exception: %s", e)

            status["completed_jobs"] = completed
            status["failed_jobs"] = failed
            status["pending_jobs"] = total - completed
            status["current_job"] = {
                "index": processed,
                "window_id": job["window"].get("window_id"),
                "profile": job["profile"],
                "regime": job["regime"],
                "threshold": job["threshold"],
            }

            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            avg = elapsed / max(1, processed)
            remaining = int(avg * (total - completed))
            status["eta_seconds"] = remaining

            sweep._save_status(status, STATUS_PATH)

            if processed % 50 == 0:
                gc.collect()

    status["state"] = "completed"
    status["completed_at"] = datetime.now(timezone.utc).isoformat()
    status["current_job"] = None
    sweep._save_status(status, STATUS_PATH)
    logger.info("Shadow-log sweep completed. total=%d completed=%d failed=%d", total, completed, failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-workers", type=int, default=MAX_WORKERS)
    parser.add_argument("--max-cycles", type=int, default=MAX_CYCLES)
    args = parser.parse_args()
    MAX_WORKERS = args.max_workers
    MAX_CYCLES = args.max_cycles
    main()
