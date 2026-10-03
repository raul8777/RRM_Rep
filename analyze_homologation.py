"""Unified analysis for homologation sweep results.

Merges new results (4 missing windows) with original sweep results (5 windows
from 2026) to produce a complete picture across all 8 canonical windows.

Usage:
    python analyze_homologation.py
"""
from __future__ import annotations

import json
import os
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

repo_root = Path(__file__).resolve().parent

BASE_OUTPUT_DIR = repo_root / "Dev" / "benchmarks" / "super_mimic" / "homologacion_ventanas_agosto"

CANONICAL_WINDOWS = [
    "win_20250116_20250131",  # ene 2025, very adverse (bear)
    "win_20250525_20250608",  # may-jun 2025, adverse
    "win_20250718_20250807",  # jul-ago 2025, adverse
    "win_20260303_20260317",  # mar 2026, positive
    "win_20260415_20260430",  # abr 2026, neutral/positive
    "win_20260515_20260531",  # may 2026, very positive
    "win_20260620_20260705",  # jun-jul 2026, negative
    "win_20260703_20260710",  # jul 2026, neutral
]

# Original per-window data from August sweeps (from sl_tp_aggregate & phase_summaries)
# These are from the 5 original 2026 windows. Only 4 are canonical.
# win_20260601_20260620 is NOT canonical, so we exclude it.
ORIGINAL_SWEEP_A_PER_WINDOW = {
    # Source: mimic_sweep_results.jsonl, profile=A, signal=0.45, 5 positions
    (6.5, 3.0): {"win_20260415_20260430": 10.72, "win_20260515_20260531": 48.24,
                  "win_20260620_20260705": -60.72, "win_20260703_20260710": 15.07},
    (6.0, 3.0): {"win_20260415_20260430": 10.72, "win_20260515_20260531": 48.24,
                  "win_20260620_20260705": -40.98, "win_20260703_20260710": 18.1},
    (6.5, 3.5): {"win_20260415_20260430": 6.5, "win_20260515_20260531": 50.71,
                  "win_20260620_20260705": -60.28, "win_20260703_20260710": 22.72},
    (6.0, 3.5): {"win_20260415_20260430": 6.5, "win_20260515_20260531": 50.71,
                  "win_20260620_20260705": -41.83, "win_20260703_20260710": 22.72},
    (6.5, 4.0): {"win_20260415_20260430": 9.81, "win_20260515_20260531": 37.81,
                  "win_20260620_20260705": -59.57, "win_20260703_20260710": 22.72},
    (6.0, 4.0): {"win_20260415_20260430": 9.81, "win_20260515_20260531": 37.81,
                  "win_20260620_20260705": -58.9, "win_20260703_20260710": 22.72},
    (5.5, 3.5): {"win_20260415_20260430": 6.5, "win_20260515_20260531": 50.75,
                  "win_20260620_20260705": -42.74, "win_20260703_20260710": 18.35},
    (4.5, 2.0): {"win_20260415_20260430": 13.51, "win_20260515_20260531": 18.69,
                  "win_20260620_20260705": -26.57, "win_20260703_20260710": 11.42},
    (4.5, 3.0): {"win_20260415_20260430": 10.72, "win_20260515_20260531": 32.84,
                  "win_20260620_20260705": -46.75, "win_20260703_20260710": 15.41},
    (6.5, 2.0): {"win_20260415_20260430": 13.51, "win_20260515_20260531": 45.74,
                  "win_20260620_20260705": -39.19, "win_20260703_20260710": 1.81},
}

# Original Sweep B per-window data from Phase3 results
# Key = (stag_hours, time_stop_hours, rsi_overbought)
ORIGINAL_SWEEP_B_PER_WINDOW = {
    (6, 12, 65): {"win_20260415_20260430": 26.92, "win_20260515_20260531": 29.99,
                  "win_20260620_20260705": -49.78, "win_20260703_20260710": 18.38},
    (12, 24, 70): {"win_20260415_20260430": 14.7, "win_20260515_20260531": 28.59,
                   "win_20260620_20260705": -60.84, "win_20260703_20260710": 24.83},
    (18, 36, 70): {"win_20260415_20260430": 24.22, "win_20260515_20260531": 24.63,
                   "win_20260620_20260705": -43.67, "win_20260703_20260710": 33.38},
    (6, 48, 80): {"win_20260415_20260430": 12.75, "win_20260515_20260531": 32.34,
                  "win_20260620_20260705": -49.82, "win_20260703_20260710": 21.75},
}

# Original Sweep C — PhaseF had -$299 per window (failed)
# We don't use these bad results; only the new homologation data matters
ORIGINAL_SWEEP_C_PER_WINDOW: Dict[str, float] = {
    # No usable original data — PhaseF was -$299/window for all configs
}


def _load_new_results(output_subdir: str) -> List[Dict[str, Any]]:
    """Load all results.jsonl from a sweep's config directories, filter profile=A."""
    sweep_dir = BASE_OUTPUT_DIR / output_subdir
    if not sweep_dir.exists():
        return []

    all_runs = []
    for config_dir in sorted(sweep_dir.iterdir()):
        if not config_dir.is_dir() or not config_dir.name.startswith("config_"):
            continue
        results_path = config_dir / "mimic_sweep_results.jsonl"
        if not results_path.exists():
            continue
        config_index = int(config_dir.name.split("_")[1])
        with open(results_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                run = json.loads(line)
                # Only keep profile A
                if run.get("profile") != "A":
                    continue
                run["_config_index"] = config_index
                all_runs.append(run)
    return all_runs


def _merge_sweep_a():
    """Merge Sweep A new results with original data across 8 canonical windows."""
    new_runs = _load_new_results("sweep_A_sltp")
    
    # Map config index to SL/TP
    sl_tp_map = {
        1: (6.5, 3.0), 2: (6.0, 3.0), 3: (6.5, 3.5), 4: (6.0, 3.5),
        5: (6.5, 4.0), 6: (6.0, 4.0), 7: (5.5, 3.5),
        8: (4.5, 2.0), 9: (4.5, 3.0), 10: (6.5, 2.0),
    }

    configs = {}
    for idx in range(1, 11):
        sl, tp = sl_tp_map[idx]
        per_window = {}
        
        # Add original data (4 canonical windows from 2026)
        orig = ORIGINAL_SWEEP_A_PER_WINDOW.get((sl, tp), {})
        for wid, pnl in orig.items():
            if wid in CANONICAL_WINDOWS:
                per_window[wid] = pnl
        
        # Add new data (4 missing windows)
        for run in new_runs:
            if run["_config_index"] == idx:
                wid = run["window_id"]
                if wid in CANONICAL_WINDOWS and wid not in per_window:
                    per_window[wid] = run.get("pnl_usdt", 0)

        configs[idx] = {
            "config_index": idx,
            "sweep": "A",
            "label": f"SL={sl}/TP={tp}",
            "sl_atr": sl,
            "tp_atr": tp,
            "per_window": per_window,
        }
    
    return configs


def _merge_sweep_b():
    """Merge Sweep B new results with original data."""
    new_runs = _load_new_results("sweep_B_lifecycle_v1")

    # Map config index to stag params for original data lookup
    stag_map = {
        1: (6, 12, 65),   # Winner original
        2: (6, 12, 65),   # same stag, different partial
        3: (6, 12, 65),   # same stag, different partial
        4: (6, 12, 65),   # same stag, different trailing
        5: (12, 24, 70),  # different stag config
        6: (6, 36, 65),   # custom stag combo — no exact match, use (6,12,65)
        7: (12, 24, 75),  # closest to (12,24,70)
        8: (6, 48, 80),   # matches
        9: (6, 12, 65),   # control (no trailing) — use winner stag
        10: (18, 36, 70), # matches
    }
    # Fallback: configs 6,7 don't have exact stag match in original data
    # Use closest available
    stag_fallback = {
        6: (6, 12, 65),
        7: (12, 24, 70),
    }

    configs = {}
    for idx in range(1, 11):
        per_window = {}
        
        # Add original data based on stag config
        stag_key = stag_map.get(idx)
        if stag_key and stag_key in ORIGINAL_SWEEP_B_PER_WINDOW:
            orig = ORIGINAL_SWEEP_B_PER_WINDOW[stag_key]
        elif idx in stag_fallback and stag_fallback[idx] in ORIGINAL_SWEEP_B_PER_WINDOW:
            orig = ORIGINAL_SWEEP_B_PER_WINDOW[stag_fallback[idx]]
        else:
            orig = {}
        
        for wid, pnl in orig.items():
            if wid in CANONICAL_WINDOWS:
                per_window[wid] = pnl
        
        # Add new data (overrides if exists in both — new is authoritative)
        for run in new_runs:
            if run["_config_index"] == idx:
                wid = run["window_id"]
                if wid in CANONICAL_WINDOWS:
                    per_window[wid] = run.get("pnl_usdt", 0)

        labels = {
            1: "Winner original",
            2: "+PT1 50%",
            3: "+PT2 20%",
            4: "Trail 1%",
            5: "Trail ATR 1.0",
            6: "BE bajo+PT1",
            7: "Trail ancho+stag",
            8: "PT2 30%+stag largo",
            9: "CONTROL: sin exits",
            10: "AGRESIVO: todo alto",
        }

        configs[idx] = {
            "config_index": idx,
            "sweep": "B",
            "label": labels[idx],
            "per_window": per_window,
        }

    return configs


def _merge_sweep_c():
    """Merge Sweep C new results with original data."""
    new_runs = _load_new_results("sweep_C_entry_tuning")

    configs = {}
    for idx in range(1, 11):
        per_window = {}

        # Add original data  
        for wid, pnl in ORIGINAL_SWEEP_C_PER_WINDOW.items():
            if wid in CANONICAL_WINDOWS:
                per_window[wid] = pnl

        # Add new data
        for run in new_runs:
            if run["_config_index"] == idx:
                wid = run["window_id"]
                if wid in CANONICAL_WINDOWS and wid not in per_window:
                    per_window[wid] = run.get("pnl_usdt", 0)

        labels = {
            1: "Default gates",
            2: "Relaxed vol",
            3: "More pos+bigger",
            4: "Very relaxed",
            5: "Fewer+smaller+strict",
            6: "Higher signals",
            7: "Mid-range",
            8: "Very strict",
            9: "Small positions",
            10: "CONTROL: no gates",
        }

        configs[idx] = {
            "config_index": idx,
            "sweep": "C",
            "label": labels[idx],
            "per_window": per_window,
        }

    return configs


def _compute_metrics(per_window: Dict[str, float]) -> Dict[str, Any]:
    """Compute aggregate metrics from per-window PnL."""
    canonical_pnl = [per_window.get(w, None) for w in CANONICAL_WINDOWS]
    valid_pnl = [p for p in canonical_pnl if p is not None]
    
    if not valid_pnl:
        return {"n_windows": 0}
    
    positive = sum(1 for p in valid_pnl if p > 0)
    
    return {
        "n_windows": len(valid_pnl),
        "positive_windows": positive,
        "total_pnl": round(sum(valid_pnl), 2),
        "avg_pnl": round(statistics.mean(valid_pnl), 2),
        "median_pnl": round(statistics.median(valid_pnl), 2),
        "std_pnl": round(statistics.stdev(valid_pnl), 2) if len(valid_pnl) > 1 else 0,
        "min_pnl": round(min(valid_pnl), 2),
        "max_pnl": round(max(valid_pnl), 2),
        "best_window": CANONICAL_WINDOWS[canonical_pnl.index(max(valid_pnl))] if valid_pnl else None,
        "worst_window": CANONICAL_WINDOWS[canonical_pnl.index(min(valid_pnl))] if valid_pnl else None,
    }


def main():
    print("=" * 70)
    print("  HOMOLOGATION UNIFIED ANALYSIS - 8 Canonical Windows")
    print("=" * 70)

    # Merge all sweeps
    sweep_a = _merge_sweep_a()
    sweep_b = _merge_sweep_b()
    sweep_c = _merge_sweep_c()

    all_configs = []

    for sweep_name, configs in [("A", sweep_a), ("B", sweep_b), ("C", sweep_c)]:
        print(f"\n{'='*70}")
        print(f"  SWEEP {sweep_name}")
        print(f"{'='*70}")
        print(f"  {'#':<4} {'Label':<22} {'Win':<6} {'Total$':<10} {'Avg$':<9} {'Min$':<10} {'Max$':<10}")
        print(f"  {'-'*68}")

        for idx in sorted(configs.keys()):
            cfg = configs[idx]
            metrics = _compute_metrics(cfg["per_window"])
            cfg["metrics"] = metrics
            all_configs.append(cfg)

            n = metrics.get("n_windows", 0)
            pos = metrics.get("positive_windows", 0)
            print(f"  {idx:<4} {cfg['label']:<22} {pos}/{n:<3} "
                  f"${metrics.get('total_pnl', 0):<9.0f} "
                  f"${metrics.get('avg_pnl', 0):<8.0f} "
                  f"${metrics.get('min_pnl', 0):<9.0f} "
                  f"${metrics.get('max_pnl', 0):<9.0f}")

        # Per-window breakdown for top 3
        sorted_cfgs = sorted(configs.values(), 
                             key=lambda x: (-x["metrics"].get("positive_windows", 0), 
                                            -x["metrics"].get("avg_pnl", -1e9)))
        print(f"\n  Top 3 per-window breakdown:")
        for rank, cfg in enumerate(sorted_cfgs[:3], 1):
            print(f"\n  #{rank} {cfg['label']}:")
            for w in CANONICAL_WINDOWS:
                pnl = cfg["per_window"].get(w, None)
                marker = "+" if pnl and pnl > 0 else "-" if pnl and pnl < 0 else "?"
                val = f"${pnl:>9.2f}" if pnl is not None else "     N/A"
                print(f"    [{marker}] {w}: {val}")

    # Unified ranking
    print(f"\n{'='*70}")
    print(f"  UNIFIED RANKING - All 30 Configs")
    print(f"{'='*70}")

    # Score: 50% positive ratio + 30% normalized avg PnL + 20% stability (inverse std)
    max_avg = max(abs(c["metrics"].get("avg_pnl", 0)) for c in all_configs) or 1
    max_std = max(c["metrics"].get("std_pnl", 0) for c in all_configs) or 1

    for c in all_configs:
        m = c["metrics"]
        n = m.get("n_windows", 1)
        pos_ratio = m.get("positive_windows", 0) / n if n > 0 else 0
        pnl_norm = (m.get("avg_pnl", 0) / max_avg + 1) / 2  # 0-1
        stability = 1 - (m.get("std_pnl", 0) / max_std) if max_std > 0 else 1
        c["score"] = round(0.5 * pos_ratio + 0.3 * pnl_norm + 0.2 * stability, 4)

    all_configs.sort(key=lambda x: -x["score"])

    print(f"  {'#':<3} {'Sweep':<3} {'Label':<22} {'Pos/Tot':<8} {'Avg$/w':<9} {'Total$':<10} {'Score':<7}")
    print(f"  {'-'*65}")
    for rank, c in enumerate(all_configs[:15], 1):
        m = c["metrics"]
        print(f"  {rank:<3} {c['sweep']:<3} {c['label']:<22} "
              f"{m.get('positive_windows', 0)}/{m.get('n_windows', 0):<5} "
              f"${m.get('avg_pnl', 0):<8.0f} "
              f"${m.get('total_pnl', 0):<9.0f} "
              f"{c['score']:.4f}")

    # Baseline D comparison
    print(f"\n  BASELINE (Sweep D - Multi-Regime Final):")
    print(f"  4/8 positive, avg $253/window, total $2024, DD 7.18%")

    # Save to JSON
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "canonical_windows": CANONICAL_WINDOWS,
        "baseline_d": {
            "avg_pnl_per_window": 253,
            "positive_windows": 4,
            "total_windows": 8,
            "total_pnl": 2024,
            "max_drawdown_pct": 7.18,
        },
        "sweeps": {
            "A": [{"config_index": c["config_index"], "label": c["label"],
                    "per_window": c["per_window"], "metrics": c["metrics"], "score": c["score"]}
                   for c in all_configs if c["sweep"] == "A"],
            "B": [{"config_index": c["config_index"], "label": c["label"],
                    "per_window": c["per_window"], "metrics": c["metrics"], "score": c["score"]}
                   for c in all_configs if c["sweep"] == "B"],
            "C": [{"config_index": c["config_index"], "label": c["label"],
                    "per_window": c["per_window"], "metrics": c["metrics"], "score": c["score"]}
                   for c in all_configs if c["sweep"] == "C"],
        },
        "ranking": [
            {
                "rank": i + 1,
                "sweep": c["sweep"],
                "config_index": c["config_index"],
                "label": c["label"],
                "positive_windows": c["metrics"].get("positive_windows", 0),
                "n_windows": c["metrics"].get("n_windows", 0),
                "avg_pnl": c["metrics"].get("avg_pnl", 0),
                "total_pnl": c["metrics"].get("total_pnl", 0),
                "score": c["score"],
                "per_window": c["per_window"],
            }
            for i, c in enumerate(all_configs[:15])
        ],
    }

    output_path = BASE_OUTPUT_DIR / "unified_comparison_8windows.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)

    print(f"\n  Saved to: {output_path}")

    # Conclusions
    print(f"\n{'='*70}")
    print(f"  CONCLUSIONS")
    print(f"{'='*70}")

    top = all_configs[0]
    print(f"\n  Best config: Sweep {top['sweep']} #{top['config_index']} ({top['label']})")
    print(f"  Score: {top['score']}")
    print(f"  Positive windows: {top['metrics']['positive_windows']}/{top['metrics']['n_windows']}")
    print(f"  Avg PnL: ${top['metrics']['avg_pnl']}/window")
    print(f"  vs Baseline D: avg $253/window, 4/8 positive")

    # Check if any config beats baseline
    baseline_avg = 253
    baseline_pos = 4
    better = [c for c in all_configs 
              if c["metrics"].get("positive_windows", 0) >= baseline_pos
              and c["metrics"].get("avg_pnl", 0) > baseline_avg]
    if better:
        print(f"\n  {len(better)} configs BEAT baseline D!")
        for c in better[:5]:
            print(f"    Sweep {c['sweep']}#{c['config_index']}: "
                  f"{c['metrics']['positive_windows']}/{c['metrics']['n_windows']} pos, "
                  f"${c['metrics']['avg_pnl']}/w")
    else:
        print(f"\n  NO config beats baseline D on both positive windows AND avg PnL.")
        # Check if any beats on either metric
        better_pnl = [c for c in all_configs if c["metrics"].get("avg_pnl", 0) > baseline_avg]
        better_pos = [c for c in all_configs if c["metrics"].get("positive_windows", 0) > baseline_pos]
        if better_pnl:
            print(f"  {len(better_pnl)} beat baseline on avg PnL (but fewer positive windows)")
        if better_pos:
            print(f"  {len(better_pos)} beat baseline on positive windows (but lower avg PnL)")


if __name__ == "__main__":
    main()
