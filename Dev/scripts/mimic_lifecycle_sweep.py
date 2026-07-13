"""mimic_lifecycle_sweep.py — optimized lifecycle kernel sweep wrapper.

Runs a focused MIMIC sweep using the new lifecycle kernel with sensible defaults:
  - 20 workers (uses all available logical cores)
  - lifecycle-quick grid (focused around best SPOT_BULL-A params)
  - representative windows (best + worst + recent)
  - clear previous results and outputs to isolated directory

Usage:
    python Dev/scripts/mimic_lifecycle_sweep.py
    python Dev/scripts/mimic_lifecycle_sweep.py --max-workers 16 --max-cycles 100
"""

from __future__ import annotations

import argparse
import os
import sys

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo_root)

from Dev.scripts.mimic_mode_profile_sweep import run_sweep


# Representative windows from Dev/sweep_analysis_SPOT_BULL_20260713.md
# Limited to 2 windows to keep the lifecycle baseline sweep fast but meaningful.
DEFAULT_WINDOWS = [
    "win_20260415_20260430",  # A best overall
    "win_20260515_20260531",  # A worst / B-C rescue
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Optimized lifecycle kernel MIMIC sweep")
    parser.add_argument("--max-workers", type=int, default=20, help="Parallel workers (default 20)")
    parser.add_argument("--max-cycles", type=int, default=None, help="Cap cycles per simulation")
    parser.add_argument("--windows", default=",".join(DEFAULT_WINDOWS), help="Comma-separated window IDs")
    parser.add_argument("--output-dir", default=os.path.join(repo_root, "Dev", "benchmarks", "lifecycle_sweep"), help="Output directory")
    parser.add_argument("--max-dd-stop", type=float, default=15.0, help="Early-stop drawdown threshold %%")
    args = parser.parse_args()

    window_ids = [w.strip() for w in args.windows.split(",") if w.strip()]
    output_dir = args.output_dir
    os.makedirs(output_dir, exist_ok=True)

    # Clear previous results for a clean run
    results_path = os.path.join(output_dir, "mimic_sweep_results.jsonl")
    if os.path.exists(results_path):
        os.remove(results_path)

    print(f"[lifecycle sweep] windows={window_ids}")
    print(f"[lifecycle sweep] workers={args.max_workers}")
    print(f"[lifecycle sweep] output_dir={output_dir}")

    report = run_sweep(
        window_ids=window_ids,
        strategy="lifecycle-quick",
        max_cycles_per_sim=args.max_cycles,
        max_workers=args.max_workers,
        max_drawdown_stop_pct=args.max_dd_stop,
        use_cache=False,
        output_dir=output_dir,
        lifecycle=True,
    )
    print("Summary:")
    import json
    print(json.dumps(report.get("summary", {}), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
