"""Offline, fixed-candidate screen of ten entry strategies; never configures runtime."""
from __future__ import annotations

import argparse
import json
import random
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from Dev.scripts.analyze_universal_entry_stop_survival import _rows, _sha256
from Dev.scripts.score_universal_entry_minimal_lifecycle import cluster_mean_summary


ROOT = Path(__file__).resolve().parents[2] / "Dev/benchmarks/universal_entry_selection"
WINDOWS = (
    ("development_jul2025", "development", "expanded_range_v1/cal_range_scalp_2_20250715_20250729", "minimal_lifecycle_v2/development_jul2025.jsonl"),
    ("development_sep2025", "development", "expanded_range_v1/cal_range_scalp_3_20250909_20250923", "minimal_lifecycle_v2/development_sep2025.jsonl"),
    ("explored_jul01_baseline", "explored_validation", "expanded_range_oos_202607/cal_range_scalp_oos_20260701_20260716/baseline", "minimal_lifecycle_v2/explored_jul01_baseline.jsonl"),
    ("explored_jul17_baseline", "explored_validation", "expanded_range_oos_20260717/cal_range_scalp_oos_20260717_20260801/baseline", "minimal_lifecycle_v2/explored_jul17_baseline.jsonl"),
)
STRATEGIES = {
    "01_mean_reversion_base": ("mean_reversion", "base z-score reversal at the generator's |z| >= 2 threshold"),
    "02_mean_reversion_trend_aligned": ("mean_reversion", "base reversal only when the existing 4h EMA trend points in the trade side"),
    "03_time_series_momentum_base": ("time_series_momentum", "base novel 4h momentum proposal at the generator's 0.5% threshold"),
    "04_time_series_momentum_trend_aligned": ("time_series_momentum", "base momentum only when the existing 4h EMA trend agrees with the trade side"),
    "05_breakout_base": ("breakout_expansion", "base close beyond prior 24h high/low"),
    "06_breakout_volume_confirmed": ("breakout_expansion", "base breakout with causal source-candle volume ratio >= 1.0"),
    "07_volatility_expansion_base": ("volatility_expansion", "base 2x true-range expansion and directional body proposal"),
    "08_volatility_expansion_volume_confirmed": ("volatility_expansion", "base volatility expansion with causal source-candle volume ratio >= 1.0"),
    "09_cross_sectional_relative_strength_base": ("cross_sectional_relative_strength", "base cross-sectional |z| >= 0.5"),
    "10_cross_sectional_relative_strength_high_conviction": ("cross_sectional_relative_strength", "cross-sectional |z| >= 1.0, frozen before scoring"),
}
COSTS = (0, 10, 20)
MINIMUM_EVENTS_PER_WINDOW = 100


def strategy_selects(name: str, proposal: dict[str, Any]) -> bool:
    family, _ = STRATEGIES[name]
    if proposal.get("philosophy_family") != family:
        return False
    if name in {"01_mean_reversion_base", "03_time_series_momentum_base", "05_breakout_base", "07_volatility_expansion_base", "09_cross_sectional_relative_strength_base"}:
        return True
    features = proposal["feature_snapshot"]
    if name == "02_mean_reversion_trend_aligned":
        trend = float(features["predictor_indicators"]["trend_4h"]["value"])
        return trend == (1.0 if proposal["side"] == "LONG" else -1.0)
    if name == "04_time_series_momentum_trend_aligned":
        trend = float(features["predictor_indicators"]["trend_4h"]["value"])
        return trend == (1.0 if proposal["side"] == "LONG" else -1.0)
    if name in {"06_breakout_volume_confirmed", "08_volatility_expansion_volume_confirmed"}:
        return float(features["predictor_indicators"]["vol_spike"]["value"]) >= 1.0
    if name == "10_cross_sectional_relative_strength_high_conviction":
        return abs(float(features["relative_strength_zscore_24h"])) >= 1.0
    raise KeyError(f"unknown strategy: {name}")


def _metric_summary(rows: list[dict[str, Any]], baseline_count: int) -> dict[str, Any]:
    selected = [row for row in rows if row["selected"]]
    result: dict[str, Any] = {
        "source_family_opportunities": baseline_count,
        "selected_events": len(selected),
        "coverage_pct": 100.0 * len(selected) / baseline_count if baseline_count else None,
        "scenarios": {},
    }
    for bps in COSTS:
        scenario = f"slippage_{bps}bps_per_fill"
        baseline = [row["net_by_cost"][scenario] for row in rows]
        chosen = [row["net_by_cost"][scenario] for row in selected]
        per_opportunity = [row["net_by_cost"][scenario] if row["selected"] else 0.0 for row in rows]
        baseline_losses = -sum(min(0.0, value) for value in baseline)
        chosen_losses = -sum(min(0.0, value) for value in chosen)
        deltas = [selected_value - base_value for selected_value, base_value in zip(per_opportunity, baseline)]
        result["scenarios"][scenario] = {
            "baseline_family_mean_net_usdt": statistics.mean(baseline) if baseline else None,
            "selected_mean_net_usdt": statistics.mean(chosen) if chosen else None,
            "selected_mean_net_r": statistics.mean(row["net_r_10bps"] for row in selected) if selected else None,
            "selected_profit_factor": sum(max(0.0, value) for value in chosen) / chosen_losses if chosen_losses else None,
            "selected_positive_pct": 100.0 * sum(value > 0 for value in chosen) / len(chosen) if chosen else None,
            "net_per_source_family_opportunity_abstentions_zero": sum(per_opportunity) / baseline_count if baseline_count else None,
            "paired_delta_per_source_family_opportunity": statistics.mean(deltas) if deltas else None,
            "paired_delta_cluster_ci95": cluster_mean_summary([
                (row["cluster"], delta) for row, delta in zip(rows, deltas)
            ])["cluster_equal_mean_ci95"],
            "baseline_family_profit_factor": sum(max(0.0, value) for value in baseline) / baseline_losses if baseline_losses else None,
        }
    return result


def screen_window(label: str, role: str, source_relative: str, outcomes_relative: str, output_dir: Path) -> dict[str, Any]:
    source = ROOT / source_relative / "philosophy_proposals.jsonl"
    outcomes = ROOT / outcomes_relative
    source_rows = list(_rows(source))
    if not source_rows:
        raise ValueError(f"empty proposal source: {source}")
    if any(row.get("window_role") != ("development" if role == "development" else "validation") for row in source_rows):
        raise ValueError(f"proposal role mismatch for {label}")
    source_by_id = {row["opportunity_event_id"]: row for row in source_rows}
    if len(source_by_id) != len(source_rows):
        raise ValueError(f"duplicate source event ids for {label}")

    outcome_by_id = {}
    for row in _rows(outcomes):
        if row["stop"] != "1.5":
            continue
        event_id = row["event_id"]
        if event_id in outcome_by_id:
            raise ValueError(f"duplicate minimum-lifecycle outcome for {event_id}")
        outcome_by_id[event_id] = row
    if set(source_by_id) != set(outcome_by_id):
        raise ValueError(f"source/outcome event join is not one-to-one in {label}")

    rows_by_strategy: dict[str, list[dict[str, Any]]] = {name: [] for name in STRATEGIES}
    source_counts = defaultdict(int)
    for event_id, proposal in source_by_id.items():
        family = proposal["philosophy_family"]
        source_counts[family] += 1
        outcome = outcome_by_id[event_id]
        if outcome["family"] not in {family, "breakout_volatility"}:
            raise ValueError(f"source/outcome family mismatch: {event_id}")
        lifecycle = outcome["lifecycle"]
        if lifecycle["label_is_executable_fill"]:
            raise ValueError("historical candles must remain labeled non-executable")
        net_by_cost = {
            f"slippage_{bps}bps_per_fill": lifecycle["scenarios"][f"slippage_{bps}bps_per_fill"]["net_usdt_no_funding"]
            for bps in COSTS
        }
        net_r_10bps = lifecycle["scenarios"]["slippage_10bps_per_fill"]["net_r_no_funding"]
        for name, (strategy_family, _) in STRATEGIES.items():
            if family != strategy_family:
                continue
            rows_by_strategy[name].append({
                "event_id": event_id, "cluster": outcome["cluster"],
                "selected": strategy_selects(name, proposal),
                "net_by_cost": net_by_cost, "net_r_10bps": net_r_10bps,
            })

    strategy_results = {}
    detail_path = output_dir / f"{label}.jsonl"
    with detail_path.open("w", encoding="utf-8", newline="\n") as target:
        for name, rows in rows_by_strategy.items():
            family = STRATEGIES[name][0]
            baseline_count = source_counts[family]
            metrics = _metric_summary(rows, baseline_count)
            strategy_results[name] = {
                "source_family": family,
                "rule": STRATEGIES[name][1],
                "metrics": metrics,
            }
            for row in rows:
                target.write(json.dumps({"strategy": name, **row}, allow_nan=False) + "\n")
    return {
        "role": role,
        "proposal_count": len(source_rows),
        "source_sha256": _sha256(source),
        "lifecycle_source_sha256": _sha256(outcomes),
        "detail_sha256": _sha256(detail_path),
        "family_candidate_counts": dict(source_counts),
        "strategies": strategy_results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "ten_strategy_screen_v1")
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error("output directory exists; preserve it and specify a new version")
    args.output_dir.mkdir(parents=True)
    report: dict[str, Any] = {
        "artifact_version": "ten_universal_entry_strategies_v1",
        "scorer_sha256": _sha256(Path(__file__)),
        "candidate_claim_allowed": False,
        "contract": {
            "candidates": STRATEGIES,
            "sizing": "existing frozen minimum-lifecycle output: $18 budget, $100 notional cap, same size over scenarios",
            "lifecycle": "fixed 1.5ATR stop or 4h horizon close; no TP/trailing/partials",
            "costs": "5bps fee per fill and 0/10/20bps slippage per fill; funding not included in ranking metric",
            "selection_metric": "net per original family opportunity with abstentions=0, plus conditional return/PF and paired cluster CI",
            "confirmation": "July 2026 windows are previously explored, not pristine holdouts",
        },
        "ranking": [],
        "windows": {},
        "limitations": [
            "The ten candidates are a fixed offline screen, not proven profitable strategies.",
            "No thresholds were fitted on the validation slices; all listed overlays are fixed generator-scale rules.",
            "The development windows and July comparisons have already been inspected in prior analyses; all validation-era results are exploratory.",
            "Per-opportunity results are not a capital-constrained portfolio simulation.",
            "Historical fills, tick sizes, trigger mechanics, fees by account and settled funding are not reconstructed.",
            "No runtime strategy/configuration, Staging service, or order path is changed.",
        ],
    }
    for label, role, source, lifecycle in WINDOWS:
        report["windows"][label] = screen_window(label, role, source, lifecycle, args.output_dir)

    development_labels = [label for label, role, _, _ in WINDOWS if role == "development"]
    ranking = []
    for name in STRATEGIES:
        metrics = [report["windows"][label]["strategies"][name]["metrics"] for label in development_labels]
        mean20 = [row["scenarios"]["slippage_20bps_per_fill"]["net_per_source_family_opportunity_abstentions_zero"] for row in metrics]
        enough_coverage = all(row["selected_events"] >= MINIMUM_EVENTS_PER_WINDOW for row in metrics)
        passed = all(
            row["selected_events"] >= MINIMUM_EVENTS_PER_WINDOW
            and row["scenarios"]["slippage_10bps_per_fill"]["net_per_source_family_opportunity_abstentions_zero"] > 0
            and row["scenarios"]["slippage_20bps_per_fill"]["net_per_source_family_opportunity_abstentions_zero"] > 0
            and (row["scenarios"]["slippage_10bps_per_fill"]["paired_delta_cluster_ci95"] or [None])[0] is not None
            and row["scenarios"]["slippage_10bps_per_fill"]["paired_delta_cluster_ci95"][0] > 0
            for row in metrics
        )
        ranking.append({
            "strategy": name,
            "screen_eligible": enough_coverage,
            "score_worst_development_20bps_net_per_family_opportunity": min(mean20) if enough_coverage else None,
            "development_gate_passed": passed,
        })
    report["ranking"] = sorted(
        ranking,
        key=lambda row: (
            row["screen_eligible"],
            row["score_worst_development_20bps_net_per_family_opportunity"]
            if row["score_worst_development_20bps_net_per_family_opportunity"] is not None else float("-inf"),
        ),
        reverse=True,
    )
    report["development_winner"] = next(
        (row["strategy"] for row in report["ranking"] if row["development_gate_passed"]), None,
    )
    report["winner_claim_allowed"] = False
    report_path = args.output_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report_path), "development_winner": report["development_winner"], "ranking": report["ranking"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())