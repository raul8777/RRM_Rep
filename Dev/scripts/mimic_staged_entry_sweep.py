"""mimic_staged_entry_sweep.py — MIMIC sweep for staged-entry parameters.

Calibrates `staged_entry_regime_config` per regime by forcing the simulation
regime so staged entries are actually exercised.

Usage:
    python Dev/scripts/mimic_staged_entry_sweep.py --regimes SPOT_BULL,SPOT_BULL,SPOT_BULL --max-workers 10 --max-cycles 30
    python Dev/scripts/mimic_staged_entry_sweep.py --regimes SPOT_BULL --max-workers 10 --max-cycles 30
"""

from __future__ import annotations

import argparse
import json
import os
import sys

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo_root)

from Dev.scripts.mimic_mode_profile_sweep import run_sweep


WINDOWS_BY_REGIME = {
    "SPOT_BULL": [
        "win_20260415_20260430",
        "win_20260303_20260317",
        "win_20260515_20260531",
        "win_20260620_20260705",
    ],
    "RANGE_SCALP": [
        "win_20260525_20250608",
        "win_20260513_20260527",
    ],
    "FUTURES_SHORT": [
        "win_20260525_20250608",
        "win_20260513_20260527",
    ],
}


def _default_config_for(regime: str, required_positive: int, intra_mode: str, enabled: bool) -> dict:
    """Return a staged_entry_regime_config tuned to one forced regime."""
    base = {
        "enabled": False,
        "required_positive_checks": 2,
        "checks_per_cycle": 1,
        "max_cycles": 2,
        "max_age_min": 100,
        "intra_mode": "disabled",
        "price_source": "avg",
        "use_fear_greed": True,
        "cooldown_min": 0,
        "require_inter_positive": False,
    }

    regimes = ["SPOT_BULL", "RANGE_SCALP", "FUTURES_SHORT", "PRESERVATION"]
    cfg: dict = {r: dict(base) for r in regimes}
    for r in cfg:
        if r in ("FUTURES_SHORT", "PRESERVATION"):
            cfg[r]["enabled"] = False
            cfg[r]["intra_mode"] = "disabled"

    if regime == "SPOT_BULL":
        cfg["SPOT_BULL"] = {
            "enabled": enabled,
            "required_positive_checks": required_positive,
            "checks_per_cycle": 3 if intra_mode == "predictor" else 1,
            "max_cycles": max(3, required_positive + 1),
            "max_age_min": 60,
            "intra_mode": intra_mode,
            "price_source": "avg",
            "use_fear_greed": True,
            "cooldown_min": 0,
            "require_inter_positive": False,
        }
    elif regime == "RANGE_SCALP":
        cfg["RANGE_SCALP"] = {
            "enabled": enabled,
            "required_positive_checks": required_positive,
            "checks_per_cycle": 2 if intra_mode == "predictor" else 1,
            "max_cycles": max(3, required_positive + 1),
            "max_age_min": 20,
            "intra_mode": intra_mode,
            "price_source": "avg",
            "use_fear_greed": True,
            "cooldown_min": 0,
            "require_inter_positive": False,
        }
    elif regime == "FUTURES_SHORT":
        cfg["FUTURES_SHORT"] = {
            "enabled": enabled,
            "required_positive_checks": required_positive,
            "checks_per_cycle": 2 if intra_mode == "predictor" else 1,
            "max_cycles": max(3, required_positive + 1),
            "max_age_min": 20,
            "intra_mode": intra_mode,
            "price_source": "ticker",
            "use_fear_greed": False,
            "cooldown_min": 0,
            "require_inter_positive": True,
        }
    return cfg


def _build_grid(regime: str) -> list:
    """Build a focused grid for the forced regime:
    - baseline (staged_entry_enabled=False)
    - staged enabled with 2/3 required positive checks and predictor intra-mode.
    """
    configs = []
    for enabled, req in [(False, 2), (True, 2), (True, 3)]:
        configs.append(_default_config_for(regime, req, "predictor", enabled))
    return configs


def _run_for_regime(regime: str, max_workers: int, max_cycles: int, max_dd_stop: float) -> dict:
    window_ids = WINDOWS_BY_REGIME.get(regime, [])
    if not window_ids:
        raise ValueError(f"No windows configured for regime {regime}")

    output_dir = os.path.join(repo_root, "Dev", "benchmarks", f"staged_entry_sweep_{regime.lower()}")
    os.makedirs(output_dir, exist_ok=True)

    staged_configs = _build_grid(regime)
    grid = {
        "staged_entry_enabled": [False, True],
        "staged_entry_regime_config": staged_configs,
    }

    grid_path = os.path.join(output_dir, "param_grid.json")
    with open(grid_path, "w", encoding="utf-8") as f:
        json.dump(grid, f, indent=2, ensure_ascii=False)

    for fname in ("mimic_sweep_results.jsonl", "mimic_sweep_status.json", "mimic_sweep_report.json"):
        path = os.path.join(output_dir, fname)
        if os.path.exists(path):
            os.remove(path)

    print(f"\n[staged entry sweep] regime={regime} windows={window_ids}")
    print(f"[staged entry sweep] workers={max_workers} max_cycles={max_cycles}")
    print(f"[staged entry sweep] grid_size={len(staged_configs)} × {len(window_ids)} windows")
    print(f"[staged entry sweep] output_dir={output_dir}")

    report = run_sweep(
        window_ids=window_ids,
        strategy="custom_grid",
        param_grid_file=grid_path,
        max_cycles_per_sim=max_cycles,
        max_workers=max_workers,
        max_drawdown_stop_pct=max_dd_stop,
        use_cache=False,
        output_dir=output_dir,
        profiles=["A"],
        forced_regime=regime,
    )

    print(f"\n[{regime}] Summary:")
    print(json.dumps(report.get("summary", {}), indent=2, ensure_ascii=False))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="MIMIC sweep for staged-entry calibration")
    parser.add_argument("--regimes", default="SPOT_BULL,RANGE_SCALP,FUTURES_SHORT", help="Comma-separated regimes to force")
    parser.add_argument("--max-workers", type=int, default=10, help="Parallel workers")
    parser.add_argument("--max-cycles", type=int, default=40, help="Max cycles per simulation")
    parser.add_argument("--max-dd-stop", type=float, default=15.0, help="Early-stop drawdown threshold %%")
    args = parser.parse_args()

    regimes = [r.strip().upper() for r in args.regimes.split(",") if r.strip()]
    all_reports = {}
    for regime in regimes:
        try:
            all_reports[regime] = _run_for_regime(regime, args.max_workers, args.max_cycles, args.max_dd_stop)
        except Exception as e:
            print(f"[{regime}] Sweep failed: {e}")
            import traceback
            traceback.print_exc()

    print("\n=== All regimes completed ===")
    for regime, report in all_reports.items():
        summary = report.get("summary", {})
        print(f"{regime}: best_pnl_pct={summary.get('best_pnl_pct'):.4f} avg_pnl_pct={summary.get('avg_pnl_pct'):.4f} best_sharpe={summary.get('best_sharpe')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
