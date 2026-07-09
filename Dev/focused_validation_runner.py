#!/usr/bin/env python3
"""Run focused pytest suites and persist deterministic artifacts.

This avoids shell encoding and streaming issues by using subprocess capture.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[1]
DEV_DIR = ROOT / "Dev"

SUITES = [
    "tests/test_system_health.py",
    "tests/test_futures_smart_exits.py",
    "tests/test_portfolio_manager.py",
]

SUITE_ARGS = {
    "tests/test_system_health.py": ["-k", "not TestCat7_APIEndpoints"],
}

# Validation baseline values expected by focused suites.
# This isolates test assertions from local runtime drift in .env.
BASELINE_ENV = {
    "KELLY_MAX_PCT_LIQUID": "0.85",
    "FUTURES_LEVERAGE": "10",
    "FUTURES_TRAILING_ACTIVATION_PCT": "2.0",
    "FUTURES_BREAKEVEN_ACTIVATION_PCT": "1.0",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_suite(suite: str) -> Dict[str, Any]:
    name = Path(suite).stem.replace("test_", "")
    out_path = DEV_DIR / f"validation_{name}_utf8.txt"

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        suite,
        "--color=no",
    ]
    cmd.extend(SUITE_ARGS.get(suite, []))

    started_at = utc_now()
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        env={**os.environ, **BASELINE_ENV},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    ended_at = utc_now()

    combined = (proc.stdout or "") + ("\n" if proc.stdout and proc.stderr else "") + (proc.stderr or "")
    out_path.write_text(combined, encoding="utf-8")

    return {
        "suite": suite,
        "exit_code": int(proc.returncode),
        "status": "PASS" if proc.returncode == 0 else "FAIL",
        "started_at": started_at,
        "ended_at": ended_at,
        "output_file": str(out_path.relative_to(ROOT)).replace("\\", "/"),
        "tail": combined.splitlines()[-20:],
    }


def main() -> int:
    DEV_DIR.mkdir(parents=True, exist_ok=True)
    results: List[Dict[str, Any]] = [run_suite(s) for s in SUITES]
    overall = "PASS" if all(r["exit_code"] == 0 for r in results) else "FAIL"

    summary = {
        "generated_at": utc_now(),
        "python_executable": sys.executable,
        "overall": overall,
        "results": results,
    }

    summary_path = DEV_DIR / "validation_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=True), encoding="utf-8")

    print(json.dumps({"overall": overall, "summary_file": str(summary_path.relative_to(ROOT)).replace("\\", "/")}, ensure_ascii=True))
    return 0 if overall == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
