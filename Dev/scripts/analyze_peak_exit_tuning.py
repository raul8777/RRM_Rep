"""analyze_peak_exit_tuning.py

Compare lifecycle tuning results against the peak-mimic oracle per window and
produce a human-readable report plus a `best_exit_policy.json` file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo_root)


def load_windows(windows_dir: str) -> List[Dict[str, Any]]:
    windows: List[Dict[str, Any]] = []
    manifest_path = os.path.join(windows_dir, "windows.json")
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = [{"window_id": k, **v} for k, v in data.items()]
        windows.extend(data)
    else:
        for fname in sorted(os.listdir(windows_dir)):
            if not fname.endswith(".json"):
                continue
            with open(os.path.join(windows_dir, fname), "r", encoding="utf-8") as f:
                w = json.load(f)
            windows.append(w)
    return windows


def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def oracle_pnl_by_window(peak_report_path: str, windows: List[Dict[str, Any]]) -> Dict[str, float]:
    with open(peak_report_path, "r", encoding="utf-8") as f:
        report = json.load(f)
    trades = report.get("trades", report if isinstance(report, list) else [])
    by_window: Dict[str, float] = {w["window_id"]: 0.0 for w in windows}
    bounds = [(w["window_id"], parse_dt(w["start"]), parse_dt(w["end"])) for w in windows]
    matched = 0
    for t in trades:
        start_field = t.get("start") or t.get("opened_at")
        if not start_field:
            continue
        dt = parse_dt(str(start_field))
        for wid, ws, we in bounds:
            if ws <= dt < we:
                by_window[wid] += float(t.get("pnl_usdt", t.get("pnl", 0.0)) or 0.0)
                matched += 1
                break
    print(f"[analyze] matched {matched} oracle trades to windows")
    return by_window


def aggregate_lifecycle(results_path: str) -> List[Dict[str, Any]]:
    by_fp: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"params": None, "window_results": []})
    with open(results_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if "error" in r:
                continue
            params = r.get("params", {})
            fp = json.dumps(params, sort_keys=True, default=str)
            entry = by_fp[fp]
            entry["params"] = params
            entry["window_results"].append(r)
    ranked = []
    for fp, data in by_fp.items():
        wres = data["window_results"]
        if not wres:
            continue
        pnl_per_day = [w.get("pnl_per_day_usdt", 0.0) for w in wres]
        pnl_pct = [w.get("pnl_pct", 0.0) for w in wres]
        max_dd = [w.get("max_drawdown_pct", 100.0) for w in wres]
        trades = sum(w.get("trades", 0) for w in wres)
        pf_vals = [w.get("profit_factor") for w in wres if w.get("profit_factor") is not None]
        avg_pf = sum(pf_vals) / len(pf_vals) if pf_vals else 0.0
        fails_gate = any(w.get("max_drawdown_pct", 100.0) >= 15.0 for w in wres) or any(w.get("trades", 0) < 3 for w in wres) or avg_pf < 1.0
        pnl_per_day_mean = sum(pnl_per_day) / len(pnl_per_day)
        objective = pnl_per_day_mean - (1000.0 if fails_gate else 0.0)
        ranked.append({
            "fingerprint": fp[:32],
            "params": data["params"],
            "objective": objective,
            "pnl_per_day_usdt_mean": pnl_per_day_mean,
            "pnl_pct_mean": sum(pnl_pct) / len(pnl_pct),
            "max_drawdown_pct_mean": sum(max_dd) / len(max_dd),
            "total_trades": trades,
            "avg_profit_factor": avg_pf,
            "windows_run": len(wres),
            "per_window": {w.get("window_id"): w for w in wres},
        })
    ranked.sort(key=lambda x: x["objective"], reverse=True)
    return ranked


def build_exit_policy_json(config: Dict[str, Any]) -> Dict[str, Any]:
    """Convert flat params into per-profile ExitPolicy structures."""
    profiles = {"A": {}, "B": {}, "C": {}}
    for key, val in config.items():
        if "_" not in key:
            continue
        prof, rest = key.split("_", 1)
        if prof in profiles:
            profiles[prof][rest] = val
    return profiles


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", default=os.path.join(repo_root, "Dev", "benchmarks", "peak_lifecycle_tuning", "tuning_results.jsonl"))
    parser.add_argument("--peak-report", default=os.path.join(repo_root, "Dev", "benchmarks", "peak_mimic_report.json"))
    parser.add_argument("--windows-dir", default=os.path.join(repo_root, "Dev", "benchmarks", "windows"))
    parser.add_argument("--output-dir", default=os.path.join(repo_root, "Dev", "benchmarks", "peak_lifecycle_tuning"))
    parser.add_argument("--top-n", type=int, default=5)
    args = parser.parse_args()

    windows = load_windows(args.windows_dir)
    window_ids = [w["window_id"] for w in windows]
    oracle = oracle_pnl_by_window(args.peak_report, windows)
    ranked = aggregate_lifecycle(args.results)

    os.makedirs(args.output_dir, exist_ok=True)

    best = ranked[0] if ranked else None
    if best:
        best_exit_policies = build_exit_policy_json(best["params"])
        with open(os.path.join(args.output_dir, "best_exit_policy.json"), "w", encoding="utf-8") as f:
            json.dump({
                "params": best["params"],
                "per_profile_exit_policy": best_exit_policies,
                "metrics": {k: v for k, v in best.items() if k not in ("params", "per_window", "fingerprint")},
            }, f, indent=2, ensure_ascii=False, default=str)

    report_lines: List[str] = []
    report_lines.append("# Peak Lifecycle Exit Policy Tuning Report\n")
    report_lines.append(f"- Generated: {datetime.utcnow().isoformat()}Z\n")
    report_lines.append(f"- Configs evaluated: {len(ranked)}\n")
    report_lines.append(f"- Windows: {len(windows)}\n\n")

    report_lines.append("## Oracle vs Lifecycle (top by pnl/day)\n\n")
    report_lines.append("| rank | fingerprint | pnl/day | pnl% | max DD% | PF | capture% |\n")
    report_lines.append("|------|-------------|---------|------|---------|----|----------|\n")

    def _metrics_for(cfg):
        lifecycle_total = sum(w.get("pnl_usdt", 0.0) for w in cfg["per_window"].values())
        oracle_total = sum(oracle.get(wid, 0.0) for wid in cfg["per_window"])
        capture_pct = (lifecycle_total / oracle_total * 100.0) if oracle_total and oracle_total > 0 else None
        return lifecycle_total, oracle_total, capture_pct

    for i, cfg in enumerate(ranked[: args.top_n]):
        lifecycle_total, oracle_total, capture_pct = _metrics_for(cfg)
        capture_str = f"{capture_pct:.2f}" if capture_pct is not None else "N/A"
        report_lines.append(
            f"| {i+1} | {cfg['fingerprint'][:12]} | {cfg['pnl_per_day_usdt_mean']:.2f} | "
            f"{cfg['pnl_pct_mean']:.2f} | {cfg['max_drawdown_pct_mean']:.2f} | "
            f"{cfg['avg_profit_factor']:.2f} | {capture_str} |\n"
        )

    report_lines.append("\n## Top by capture ratio (among configs that pass gates)\n\n")
    for cfg in ranked:
        lifecycle_total, oracle_total, capture_pct = _metrics_for(cfg)
        cfg["capture_pct"] = capture_pct
        cfg["lifecycle_total"] = lifecycle_total
        cfg["oracle_total"] = oracle_total
    capture_ranked = [c for c in ranked if c["capture_pct"] is not None and c["capture_pct"] > 0]
    capture_ranked.sort(key=lambda x: x["capture_pct"], reverse=True)
    report_lines.append("| rank | fingerprint | pnl/day | pnl% | max DD% | PF | capture% |\n")
    report_lines.append("|------|-------------|---------|------|---------|----|----------|\n")
    for i, cfg in enumerate(capture_ranked[: args.top_n]):
        capture_str = f"{cfg['capture_pct']:.2f}"
        report_lines.append(
            f"| {i+1} | {cfg['fingerprint'][:12]} | {cfg['pnl_per_day_usdt_mean']:.2f} | "
            f"{cfg['pnl_pct_mean']:.2f} | {cfg['max_drawdown_pct_mean']:.2f} | "
            f"{cfg['avg_profit_factor']:.2f} | {capture_str} |\n"
        )

    report_lines.append("\n## Best combined (pnl/day * capture%)\n\n")
    combined = []
    for c in ranked:
        if c["capture_pct"] is not None and c["capture_pct"] > 0 and c["avg_profit_factor"] >= 1.0 and c["max_drawdown_pct_mean"] < 15.0:
            combined.append((c, c["pnl_per_day_usdt_mean"] * c["capture_pct"]))
    combined.sort(key=lambda x: x[1], reverse=True)
    if combined:
        best_combined, score = combined[0]
        report_lines.append(
            f"Fingerprint `{best_combined['fingerprint'][:12]}` "
            f"score={score:.2f}, pnl/day={best_combined['pnl_per_day_usdt_mean']:.2f}, "
            f"capture%={best_combined['capture_pct']:.2f}, max DD%={best_combined['max_drawdown_pct_mean']:.2f}\n\n"
        )

    if best:
        report_lines.append("\n## Best config per-profile exit policy\n\n")
        for prof, pol in build_exit_policy_json(best["params"]).items():
            report_lines.append(f"### Profile {prof}\n\n")
            for k, v in pol.items():
                report_lines.append(f"- `{k}`: {v}\n")

    report_path = os.path.join(args.output_dir, "tuning_report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.writelines(report_lines)

    print(f"[analyze] wrote {report_path}")
    if best:
        print(f"[analyze] best pnl/day: {best['pnl_per_day_usdt_mean']:.2f}, max DD: {best['max_drawdown_pct_mean']:.2f}%")


if __name__ == "__main__":
    main()
