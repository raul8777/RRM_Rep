"""Post-sweep analysis for SPOT_BULL-A/B/C sweep completed 2026-07-13.

Reads Dev/benchmarks/mimic_sweep_results.jsonl and produces a markdown report
in Dev/sweep_analysis_SPOT_BULL_20260713.md following the post_sweep_analysis
workflow.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from statistics import median
from typing import Any, Dict, List, Optional

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo_root)

RESULTS_PATH = os.path.join(repo_root, "Dev", "benchmarks", "mimic_sweep_results.jsonl")
STATUS_PATH = os.path.join(repo_root, "Dev", "benchmarks", "mimic_sweep_status.json")
REPORT_PATH = os.path.join(repo_root, "Dev", "sweep_analysis_SPOT_BULL_20260713.md")
SYSTEM_REF_PATH = os.path.join(repo_root, "SYSTEM_REFERENCE.md")

PROFILES = ["A", "B", "C"]
PARAMS = [
    "min_signal_strength",
    "min_success_probability",
    "stop_loss_pct",
    "kelly_max_usdt",
    "kelly_dynamic_base_pct",
    "max_positions_spot_bull",
]


def load_results() -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    with open(RESULTS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                results.append(json.loads(line))
            except Exception:
                continue
    return results


def load_status() -> Dict[str, Any]:
    with open(STATUS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def is_spot_bull_dominant(r: Dict[str, Any], min_cycles: int = 40) -> bool:
    dist = r.get("regime_distribution", {})
    if not dist:
        return False
    # SPOT_BULL may appear as key directly or via mode
    for key, cycles in dist.items():
        if "SPOT_BULL" in key.upper() and isinstance(cycles, (int, float)) and cycles >= min_cycles:
            return True
    return False


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def safe_sharpe(r: Dict[str, Any]) -> Optional[float]:
    s = r.get("sharpe")
    if s is None:
        return None
    try:
        s = float(s)
        if abs(s) > 100:
            return None
        return s
    except Exception:
        return None


def fmt_num(value: Optional[float], decimals: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{decimals}f}"


def median_or_none(values: List[float]) -> Optional[float]:
    return median(values) if values else None


def step1_validation(status: Dict[str, Any], results: List[Dict[str, Any]]) -> str:
    total = status.get("total_jobs", len(results))
    completed = status.get("completed_jobs", len(results))
    failed = status.get("failed_jobs", 0)
    with_trades = sum(1 for r in results if r.get("trades", 0) > 0)
    spot_dominant = sum(1 for r in results if is_spot_bull_dominant(r))
    return f"""## Step 1: Sweep Validation

- **Status**: {status.get('state', 'unknown')}
- **Total jobs**: {total}
- **Completed jobs**: {completed}
- **Failed jobs**: {failed}
- **Jobs with trades > 0**: {with_trades}
- **SPOT_BULL-dominant jobs (≥40 cycles)**: {spot_dominant}
"""


def step2_aggregate(results: List[Dict[str, Any]]) -> str:
    by_profile: Dict[str, List[Dict[str, Any]]] = {p: [] for p in PROFILES}
    for r in results:
        by_profile[r.get("profile", "?")].append(r)

    lines = ["## Step 2: Aggregate by Profile", "", "| Profile | Runs | Avg PnL | Median PnL | Avg Sharpe | Median Sharpe | Avg DD% | Pos PnL% | Avg Trades |",
             "|---|---|---|---|---|---|---|---|---|"]
    best_pnl_profile = None
    best_pnl_value = -1e9
    best_sharpe_profile = None
    best_sharpe_value = -1e9
    for p in PROFILES:
        group = by_profile.get(p, [])
        if not group:
            continue
        pnls = [r.get("pnl_usdt", 0.0) for r in group]
        sharpes = [s for s in (safe_sharpe(r) for r in group) if s is not None]
        dds = [r.get("max_drawdown_pct", 0.0) for r in group if r.get("max_drawdown_pct") is not None]
        trades = [r.get("trades", 0) for r in group]
        active_trades = [t for t in trades if t > 0]
        pos_ratio = sum(1 for x in pnls if x > 0) / len(pnls) * 100 if pnls else 0
        avg_pnl = sum(pnls) / len(pnls) if pnls else 0
        med_pnl = median_or_none(pnls)
        avg_sharpe = sum(sharpes) / len(sharpes) if sharpes else None
        med_sharpe = median_or_none(sharpes)
        avg_dd = sum(dds) / len(dds) if dds else None
        avg_trades = sum(trades) / len(trades) if trades else 0
        lines.append(
            f"| {p} | {len(group)} | {fmt_num(avg_pnl)} | {fmt_num(med_pnl)} | "
            f"{fmt_num(avg_sharpe)} | {fmt_num(med_sharpe)} | {fmt_num(avg_dd)} | "
            f"{fmt_num(pos_ratio)} | {fmt_num(avg_trades)} |"
        )
        # best single result per profile
        valid = [r for r in group if r.get("trades", 0) >= 3 and r.get("pnl_usdt", 0.0) > 0]
        if not valid:
            valid = group
        top_pnl = max(valid, key=lambda x: x.get("pnl_usdt", 0.0))
        if top_pnl.get("pnl_usdt", 0.0) > best_pnl_value:
            best_pnl_value = top_pnl.get("pnl_usdt", 0.0)
            best_pnl_profile = p
        valid_sharpe = [r for r in group if safe_sharpe(r) is not None and r.get("trades", 0) >= 3]
        if valid_sharpe:
            top_sharpe = max(valid_sharpe, key=lambda x: safe_sharpe(x) or -1e9)
            sv = safe_sharpe(top_sharpe) or -1e9
            if sv > best_sharpe_value:
                best_sharpe_value = sv
                best_sharpe_profile = p

    lines.extend([
        "",
        f"**Attractor profile by total PnL**: {best_pnl_profile} (best single result ${fmt_num(best_pnl_value)})",
        f"**Attractor profile by Sharpe**: {best_sharpe_profile} (Sharpe {fmt_num(best_sharpe_value)})",
    ])
    return "\n".join(lines)


def step3_sl_tp(results: List[Dict[str, Any]]) -> str:
    with_sl_tp = [r for r in results if r.get("sl_tp", 0) > 0]
    total_sells = sum(r.get("sells", 0) for r in results)
    total_sl_tp = sum(r.get("sl_tp", 0) for r in results)
    sl_by_param: Dict[float, List[int]] = defaultdict(list)
    for r in results:
        sl = r.get("params", {}).get("stop_loss_pct")
        if sl is not None and r.get("sl_tp") is not None:
            sl_by_param[sl].append(r.get("sl_tp", 0))

    lines = [
        "## Step 3: SL/TP Exit Analysis",
        "",
        f"- **Jobs with SL/TP exits > 0**: {len(with_sl_tp)}",
        f"- **Total SL/TP exits**: {total_sl_tp}",
        f"- **Total sells**: {total_sells}",
        f"- **SL/TP / sells ratio**: {fmt_num(total_sl_tp / total_sells * 100 if total_sells else 0)}%",
        "",
        "| stop_loss_pct | Jobs | Avg SL/TP | Max SL/TP |",
        "|---|---|---|---|",
    ]
    for sl in sorted(sl_by_param.keys()):
        vals = sl_by_param[sl]
        lines.append(f"| {sl}% | {len(vals)} | {fmt_num(sum(vals)/len(vals), 1)} | {max(vals)} |")

    if not with_sl_tp:
        lines.append("")
        lines.append("**Goal check**: SL is not strangling — zero SL/TP exits recorded.")
    return "\n".join(lines)


def step4_kelly(results: List[Dict[str, Any]]) -> str:
    by_kelly: Dict[float, List[Dict[str, Any]]] = defaultdict(list)
    for r in results:
        k = r.get("params", {}).get("kelly_max_usdt")
        if k is not None:
            by_kelly[float(k)].append(r)

    lines = [
        "## Step 4: Kelly Impact Analysis",
        "",
        "| kelly_max_usdt | Jobs | Avg PnL | Median PnL | Avg Sharpe | Avg DD% | Avg CDR |",
        "|---|---|---|---|---|---|---|",
    ]
    for k in sorted(by_kelly.keys()):
        group = by_kelly[k]
        pnls = [r.get("pnl_usdt", 0.0) for r in group]
        sharpes = [s for s in (safe_sharpe(r) for r in group) if s is not None]
        dds = [r.get("max_drawdown_pct", 0.0) for r in group if r.get("max_drawdown_pct") is not None]
        cdrs = [r.get("capital_deployed_ratio") for r in group if r.get("capital_deployed_ratio") is not None]
        lines.append(
            f"| {k} | {len(group)} | {fmt_num(sum(pnls)/len(pnls))} | {fmt_num(median_or_none(pnls))} | "
            f"{fmt_num(sum(sharpes)/len(sharpes) if sharpes else None)} | "
            f"{fmt_num(sum(dds)/len(dds) if dds else None)} | {fmt_num(sum(cdrs)/len(cdrs) if cdrs else None)} |"
        )

    # linear scaling: delta PnL per +100 USD
    sorted_ks = sorted(by_kelly.keys())
    if len(sorted_ks) >= 2:
        avgs = {k: sum(r.get("pnl_usdt", 0.0) for r in by_kelly[k]) / len(by_kelly[k]) for k in sorted_ks}
        deltas = []
        for i in range(1, len(sorted_ks)):
            dk = sorted_ks[i] - sorted_ks[i - 1]
            if dk > 0:
                deltas.append((avgs[sorted_ks[i]] - avgs[sorted_ks[i - 1]]) / dk * 100)
        avg_delta = sum(deltas) / len(deltas) if deltas else 0
        max_k = max(sorted_ks)
        lines.extend([
            "",
            f"- **Avg PnL increase per +$100 Kelly**: ${fmt_num(avg_delta)}",
            f"- **Max Kelly tested**: ${max_k}",
            f"- **Projected PnL at 1.5× (${max_k*1.5})**: ${fmt_num(avgs[max_k] + avg_delta * (max_k*0.5/100))}",
            f"- **Projected PnL at 2× (${max_k*2})**: ${fmt_num(avgs[max_k] + avg_delta * (max_k*1.0/100))}",
        ])
    return "\n".join(lines)


def step5_cascade(results: List[Dict[str, Any]]) -> str:
    by_profile_window: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for r in results:
        p = r.get("profile", "?")
        w = r.get("window_id", "?")
        by_profile_window[p][w].append(r)

    def best_in_group(group: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        valid = [r for r in group if r.get("trades", 0) >= 3 and r.get("pnl_usdt", 0.0) > 0]
        if not valid:
            valid = [r for r in group if r.get("trades", 0) >= 1]
        if not valid:
            return None
        return max(valid, key=lambda x: x.get("pnl_usdt", 0.0))

    def worst_window(profile: str) -> Optional[str]:
        windows = by_profile_window.get(profile, {})
        if not windows:
            return None
        return min(windows.keys(), key=lambda w: sum(r.get("pnl_usdt", 0.0) for r in windows[w]) / max(1, len(windows[w])))

    a_best = best_in_group([r for group in by_profile_window.get("A", {}).values() for r in group])
    a_worst = worst_window("A")
    b_rescue = best_in_group(by_profile_window.get("B", {}).get(a_worst or "", [])) if a_worst else None
    b_worst = worst_window("B")
    c_rescue = best_in_group(by_profile_window.get("C", {}).get(b_worst or "", [])) if b_worst else None

    def describe(r: Optional[Dict[str, Any]]) -> str:
        if not r:
            return "N/A"
        return f"PnL=${r.get('pnl_usdt')} trades={r.get('trades')} dd={r.get('max_drawdown_pct')}% window={r.get('window_id')} params={r.get('params')}"

    lines = [
        "## Step 5: Cascading Profile Analysis (A → B → C)",
        "",
        f"- **A best overall**: {describe(a_best)}",
        f"- **A worst window**: {a_worst}",
        f"- **B rescues A-worst**: {describe(b_rescue)}",
        f"- **B worst window**: {b_worst}",
        f"- **C rescues B-worst**: {describe(c_rescue)}",
        "",
        "**Cascade verification**: DD floor should improve A→B→C.",
    ]
    return "\n".join(lines)


def step6_sensitivity(results: List[Dict[str, Any]]) -> str:
    lines = ["## Step 6: Parameter Sensitivity", ""]
    for param in PARAMS:
        by_val: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
        for r in results:
            v = r.get("params", {}).get(param)
            if v is not None:
                by_val[v].append(r)
        lines.extend([f"### {param}", "", "| Value | Jobs | Median PnL | Median Sharpe | Median DD% | Mean Trades (active) |", "|---|---|---|---|---|---|"])
        for v in sorted(by_val.keys(), key=lambda x: float(x) if isinstance(x, (int, float)) else str(x)):
            group = by_val[v]
            pnls = [r.get("pnl_usdt", 0.0) for r in group]
            sharpes = [s for s in (safe_sharpe(r) for r in group) if s is not None]
            dds = [r.get("max_drawdown_pct", 0.0) for r in group if r.get("max_drawdown_pct") is not None]
            active_trades = [r.get("trades", 0) for r in group if r.get("trades", 0) > 0]
            mean_trades = sum(active_trades) / len(active_trades) if active_trades else 0
            lines.append(
                f"| {v} | {len(group)} | {fmt_num(median_or_none(pnls))} | "
                f"{fmt_num(median_or_none(sharpes))} | {fmt_num(median_or_none(dds))} | {fmt_num(mean_trades)} |"
            )
        lines.append("")
    return "\n".join(lines)


def step7_optimal(results: List[Dict[str, Any]]) -> str:
    by_profile: Dict[str, List[Dict[str, Any]]] = {p: [] for p in PROFILES}
    for r in results:
        by_profile[r.get("profile", "?")].append(r)

    lines = ["## Step 7: Optimal Configuration", "", "### By total PnL", "", "| Profile | Window | PnL total | PnL/día | Trades | DD% | Params |", "|---|---|---|---|---|---|---|"]
    for p in PROFILES:
        group = by_profile.get(p, [])
        if not group:
            continue
        valid = [r for r in group if r.get("trades", 0) >= 3 and r.get("pnl_usdt", 0.0) > 0]
        if not valid:
            valid = group
        best = max(valid, key=lambda x: x.get("pnl_usdt", 0.0))
        lines.append(
            f"| {p} | {best.get('window_id')} | {best.get('pnl_usdt')} | {best.get('pnl_per_day_usdt')} | "
            f"{best.get('trades')} | {best.get('max_drawdown_pct')} | `{json.dumps(best.get('params'))}` |"
        )

    lines.extend(["", "### By Sharpe", "", "| Profile | Window | Sharpe | PnL total | Trades | DD% | Params |", "|---|---|---|---|---|---|---|"])
    for p in PROFILES:
        group = by_profile.get(p, [])
        valid = [r for r in group if safe_sharpe(r) is not None and r.get("trades", 0) >= 3]
        if not valid:
            continue
        best = max(valid, key=lambda x: safe_sharpe(x) or -1e9)
        lines.append(
            f"| {p} | {best.get('window_id')} | {safe_sharpe(best)} | {best.get('pnl_usdt')} | "
            f"{best.get('trades')} | {best.get('max_drawdown_pct')} | `{json.dumps(best.get('params'))}` |"
        )
    lines.append("")
    return "\n".join(lines)


def step8_kelly_uniformity(results: List[Dict[str, Any]]) -> str:
    # Compare Kelly across profiles
    by_profile_kelly: Dict[str, Dict[float, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for r in results:
        p = r.get("profile", "?")
        k = r.get("params", {}).get("kelly_max_usdt")
        if k is not None:
            by_profile_kelly[p][float(k)].append(r)

    lines = [
        "## Step 8: Kelly Uniformity Check",
        "",
        "| Profile | Kelly | Avg PnL | Avg Sharpe | Avg DD% |",
        "|---|---|---|---|---|",
    ]
    for p in PROFILES:
        for k in sorted(by_profile_kelly.get(p, {}).keys()):
            group = by_profile_kelly[p][k]
            pnls = [r.get("pnl_usdt", 0.0) for r in group]
            sharpes = [s for s in (safe_sharpe(r) for r in group) if s is not None]
            dds = [r.get("max_drawdown_pct", 0.0) for r in group if r.get("max_drawdown_pct") is not None]
            lines.append(
                f"| {p} | {k} | {fmt_num(sum(pnls)/len(pnls))} | "
                f"{fmt_num(sum(sharpes)/len(sharpes) if sharpes else None)} | "
                f"{fmt_num(sum(dds)/len(dds) if dds else None)} |"
            )

    lines.extend([
        "",
        "**Decision**: Uniform Kelly is recommended unless a profile shows materially different risk-adjusted returns. Review the table above before finalizing.",
    ])
    return "\n".join(lines)


def step9_implementation(results: List[Dict[str, Any]]) -> str:
    by_profile: Dict[str, List[Dict[str, Any]]] = {p: [] for p in PROFILES}
    for r in results:
        by_profile[r.get("profile", "?")].append(r)

    lines = [
        "## Step 9: Implementation Plan",
        "",
        "Proposed configs (by total PnL, SPOT_BULL dominant):",
        "",
        "| Profile | Params | Profile Overrides |",
        "|---|---|---|",
    ]
    for p in PROFILES:
        group = by_profile.get(p, [])
        valid = [r for r in group if r.get("trades", 0) >= 3 and r.get("pnl_usdt", 0.0) > 0]
        if not valid:
            valid = group
        best = max(valid, key=lambda x: x.get("pnl_usdt", 0.0))
        params = best.get("params", {})
        overrides = best.get("profile_overrides", {}).get(f"SPOT_BULL_{p}", {})
        lines.append(f"| {p} | `{json.dumps(params)}` | `{json.dumps(overrides)}` |")

    lines.extend([
        "",
        "**Actions required before going live**:",
        "1. Review proposed configs vs current production values.",
        "2. Apply via safe endpoint or maintenance script (dry-run first).",
        "3. Run test suite excluding heavy integration tests.",
        "4. Verify `mode_profile_harmony_snapshot` reflects new values.",
    ])
    return "\n".join(lines)


def step10_monitoring() -> str:
    return """## Step 10: Post-Implementation Monitoring (48h)

Monitor in production dashboard:

- **CDR** (Capital Deployed Ratio) — should increase toward target.
- **Sharpe** — should not fall >20% from pre-change baseline.
- **SL exit proportion** — should remain near 0%.
- **Exit composition** — SSO/PRS/PredExit/UG should dominate.

**Rollback trigger**:
- CDR doesn't improve, OR
- Sharpe drops >20%, OR
- Real drawdown exceeds projected × 1.5.
"""


def build_report() -> str:
    status = load_status()
    results = load_results()
    sections = [
        "# Post-Sweep Analysis: SPOT_BULL (2026-07-13)",
        "",
        step1_validation(status, results),
        "",
        step2_aggregate(results),
        "",
        step3_sl_tp(results),
        "",
        step4_kelly(results),
        "",
        step5_cascade(results),
        "",
        step6_sensitivity(results),
        "",
        step7_optimal(results),
        "",
        step8_kelly_uniformity(results),
        "",
        step9_implementation(results),
        "",
        step10_monitoring(),
    ]
    return "\n".join(sections)


def main() -> int:
    report = build_report()
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"Report saved to {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
