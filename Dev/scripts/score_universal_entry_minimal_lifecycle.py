"""Score frozen stop tapes with an initial stop or a four-hour full exit."""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any

from Dev.scripts.analyze_universal_entry_stop_survival import (
    REPO_ROOT, SLIPPAGE_BPS_PER_FILL, _rows, _sha256, score_minimal_lifecycle,
)


STOPS = ("1.5", "2", "2.5")
ROOT = REPO_ROOT / "Dev/benchmarks/universal_entry_selection"
SOURCES = (
    ("development_jul2025", "development", "expanded_range_v1/cal_range_scalp_2_20250715_20250729"),
    ("development_sep2025", "development", "expanded_range_v1/cal_range_scalp_3_20250909_20250923"),
    ("explored_jul01_baseline", "explored_validation", "expanded_range_oos_202607/cal_range_scalp_oos_20260701_20260716/baseline"),
    ("explored_jul01_sr", "explored_validation", "expanded_range_oos_202607/cal_range_scalp_oos_20260701_20260716/sr_pivot_breakout"),
    ("explored_jul17_baseline", "explored_validation", "expanded_range_oos_20260717/cal_range_scalp_oos_20260717_20260801/baseline"),
    ("explored_jul17_sr", "explored_validation", "expanded_range_oos_20260717/cal_range_scalp_oos_20260717_20260801/sr_pivot"),
)


def canonical_family(family: str) -> str:
    return "breakout_volatility" if family in {"breakout_expansion", "volatility_expansion"} else family


def cluster_mean_summary(values: list[tuple[str, float]]) -> dict[str, Any]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for cluster, value in values:
        grouped[cluster].append(value)
    means = [statistics.mean(grouped[cluster]) for cluster in sorted(grouped)]
    if not means:
        return {"clusters": 0, "cluster_equal_mean": None, "cluster_equal_mean_ci95": None}
    interval = None
    if len(means) >= 2:
        rng = random.Random(8173)
        samples = sorted(statistics.mean(rng.choices(means, k=len(means))) for _ in range(500))
        interval = [samples[12], samples[487]]
    return {
        "clusters": len(means), "cluster_equal_mean": statistics.mean(means),
        "cluster_equal_mean_ci95": interval,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scenarios = {}
    for bps in SLIPPAGE_BPS_PER_FILL:
        key = f"slippage_{bps:g}bps_per_fill"
        nets = [row["lifecycle"]["scenarios"][key]["net_usdt_no_funding"] for row in rows]
        gains = sum(max(0.0, value) for value in nets)
        losses = -sum(min(0.0, value) for value in nets)
        ordered = sorted(nets)
        scenarios[key] = {
            "mean_net_usdt": statistics.mean(nets),
            "positive_close_pct": 100.0 * sum(value > 0 for value in nets) / len(nets),
            "profit_factor": gains / losses if losses else None,
            "mean_net_r": statistics.mean(row["lifecycle"]["scenarios"][key]["net_r_no_funding"] for row in rows),
            "mean_net_per_100_reference_notional": statistics.mean(row["lifecycle"]["scenarios"][key]["net_per_100_reference_notional"] for row in rows),
            "p05_net_usdt": ordered[int(0.05 * (len(ordered) - 1))],
            "worst_net_usdt": min(nets),
            "funding_sensitivity_mean_net_usdt": {
                funding: statistics.mean(row["lifecycle"]["scenarios"][key]["funding_sensitivity_net_usdt"][funding] for row in rows)
                for funding in ("cost_-1bps_per_8h_prorated", "cost_0bps_per_8h_prorated", "cost_1bps_per_8h_prorated")
            },
        }
    return {
        "proposal_rows": len(rows),
        "stop_pct": 100.0 * sum(row["lifecycle"]["exit_reason"] == "initial_stop" for row in rows) / len(rows),
        "mean_holding_hours_upper_bound": statistics.mean(row["lifecycle"]["holding_hours_upper_bound"] for row in rows),
        "notional_cap_binding_pct": 100.0 * sum(row["lifecycle"]["notional_cap_binding"] for row in rows) / len(rows),
        "mean_initial_risk_usdt": statistics.mean(row["lifecycle"]["planned_initial_risk_usdt_20bps"] for row in rows),
        "scenarios": scenarios,
        "cluster_inference_10bps": cluster_mean_summary([
            (row["cluster"], row["lifecycle"]["scenarios"]["slippage_10bps_per_fill"]["net_usdt_no_funding"])
            for row in rows
        ]),
    }


def overlap_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: dict[str, dict[tuple[str, int, str], list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        if row["stop"] == "1.5":
            events[row["family"]][(row["symbol"], row["entry_time"], row["side"])].append(
                row["lifecycle"]["scenarios"]["slippage_10bps_per_fill"]["net_per_100_reference_notional"]
            )
    results = []
    for first, second in combinations(sorted(events), 2):
        first_keys, second_keys = set(events[first]), set(events[second])
        shared = sorted(first_keys & second_keys)
        correlation = None
        if len(shared) >= 3:
            first_values = [statistics.mean(events[first][event]) for event in shared]
            second_values = [statistics.mean(events[second][event]) for event in shared]
            first_mean, second_mean = statistics.mean(first_values), statistics.mean(second_values)
            numerator = sum((left - first_mean) * (right - second_mean) for left, right in zip(first_values, second_values))
            denominator = math.sqrt(sum((value - first_mean) ** 2 for value in first_values) * sum((value - second_mean) ** 2 for value in second_values))
            correlation = numerator / denominator if denominator else None
        opposing = {(symbol, timestamp) for symbol, timestamp, side in first_keys if (symbol, timestamp, "SHORT" if side == "LONG" else "LONG") in second_keys}
        results.append({
            "families": [first, second], "first_unique_events": len(first_keys),
            "second_unique_events": len(second_keys), "same_time_symbol_side_events": len(shared),
            "jaccard_pct": 100.0 * len(shared) / len(first_keys | second_keys),
            "opposite_side_same_time_symbol_events": len(opposing),
            "shared_event_return_correlation": correlation,
        })
    return results


def concordance_filter_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    anchor_events: dict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    confirming_events = set()
    for row in rows:
        if row["stop"] != "1.5":
            continue
        key = (row["symbol"], row["entry_time"], row["side"])
        if row["family"] == "breakout_volatility":
            anchor_events[key].append(row)
        elif row["family"] == "cross_sectional_relative_strength":
            confirming_events.add(key)
    if not anchor_events or not confirming_events:
        return {"status": "not_evaluable_missing_component"}
    result: dict[str, Any] = {
        "status": "evaluated_exploratory", "anchor": "breakout_volatility",
        "confirmation": "cross_sectional_relative_strength", "stop_atr": 1.5,
        "rule": "exact symbol, entry time, and side; one event, no duplicate exposure",
        "by_side": {},
    }
    for side in ("LONG", "SHORT", "ALL"):
        anchors = {key: group for key, group in anchor_events.items() if side == "ALL" or key[2] == side}
        selected = {key: group for key, group in anchors.items() if key in confirming_events}
        if not anchors:
            continue
        scenarios = {}
        for bps in SLIPPAGE_BPS_PER_FILL:
            scenario_key = f"slippage_{bps:g}bps_per_fill"
            event_nets = {
                key: statistics.mean(row["lifecycle"]["scenarios"][scenario_key]["net_usdt_no_funding"] for row in group)
                for key, group in anchors.items()
            }
            chosen = [event_nets[key] for key in selected]
            losses = -sum(min(0.0, value) for value in chosen)
            scenarios[scenario_key] = {
                "baseline_event_mean_net_usdt": statistics.mean(event_nets.values()),
                "selected_event_mean_net_usdt": statistics.mean(chosen) if chosen else None,
                "selected_profit_factor": sum(max(0.0, value) for value in chosen) / losses if losses else None,
                "selected_positive_close_pct": 100.0 * sum(value > 0 for value in chosen) / len(chosen) if chosen else None,
                "filter_net_per_anchor_opportunity_usdt_abstentions_zero": sum(chosen) / len(anchors),
                "paired_selection_delta_per_anchor_opportunity_usdt": (sum(chosen) - sum(event_nets.values())) / len(anchors),
            }
        result["by_side"][side] = {
            "unique_anchor_events": len(anchors), "selected_unique_events": len(selected),
            "coverage_pct": 100.0 * len(selected) / len(anchors), "scenarios": scenarios,
        }
    return result


def analyze_tape(source: Path, output: Path, risk_budget: float, notional_cap: float) -> dict[str, Any]:
    source_report = json.loads((source / "report.json").read_text(encoding="utf-8"))
    tape = source / "entry_stop_survival.jsonl"
    if _sha256(tape) != source_report["output_sha256"]:
        raise ValueError("source tape hash mismatch")
    if source_report["contract"]["horizon_minutes"] != 240 or source_report["contract"]["fee_bps_per_fill_proxy"] != 5.0:
        raise ValueError("source contract differs from frozen horizon or fees")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rows = []
    ids = set()
    temporary = output.with_suffix(".jsonl.tmp")
    with temporary.open("w", encoding="utf-8") as target:
        for proposal in _rows(tape):
            event_id = proposal["opportunity_event_id"]
            if event_id in ids:
                raise ValueError("duplicate opportunity id")
            ids.add(event_id)
            results = proposal["stop_results_by_atr_multiple"]
            if not all(results.get(stop, {}).get("label_valid") is True for stop in STOPS):
                raise ValueError("unmatched or invalid stop path")
            for stop in STOPS:
                row = {
                    "event_id": event_id, "cluster": proposal.get("dedupe_cluster_id") or event_id,
                    "family": canonical_family(proposal["family"]), "variant": proposal["variant"],
                    "side": proposal["side"], "symbol": proposal["symbol"], "entry_time": proposal["entry_time"],
                    "stop": stop, "lifecycle": score_minimal_lifecycle(
                        proposal, results[stop], risk_budget_usdt=risk_budget, max_notional_usdt=notional_cap,
                    ),
                }
                rows.append(row)
                grouped[f"{row['family']}|{row['side']}|{stop}ATR"].append(row)
                target.write(json.dumps(row, allow_nan=False) + "\n")
    if len(ids) != source_report["proposal_count"]:
        raise ValueError("proposal count mismatch")
    temporary.replace(output)
    paired: dict[str, list[dict[str, Any]]] = defaultdict(list)
    event_rows: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        event_rows[row["event_id"]][row["stop"]] = row
    for event in event_rows.values():
        baseline = event["1.5"]
        for width in ("2", "2.5"):
            challenger = event[width]
            paired[f"{baseline['family']}|{baseline['side']}|{width}minus1.5ATR"].append({
                "cluster": baseline["cluster"],
                "delta_net": challenger["lifecycle"]["scenarios"]["slippage_10bps_per_fill"]["net_usdt_no_funding"] - baseline["lifecycle"]["scenarios"]["slippage_10bps_per_fill"]["net_usdt_no_funding"],
                "rescued_to_horizon": baseline["lifecycle"]["exit_reason"] == "initial_stop" and challenger["lifecycle"]["exit_reason"] == "horizon_close",
            })
    return {
        "source_tape_sha256": source_report["output_sha256"],
        "source_replay_analyzer_sha256": source_report["analyzer_sha256"],
        "output_sha256": _sha256(output), "proposal_count": len(ids), "scored_rows": len(rows),
        "groups": {key: summarize(group) for key, group in sorted(grouped.items())},
        "paired_width_deltas_10bps": {
            key: {
                "paired_proposals": len(group), "proposal_mean_delta_net_usdt": statistics.mean(row["delta_net"] for row in group),
                "rescued_to_horizon_count": sum(row["rescued_to_horizon"] for row in group),
                **cluster_mean_summary([(row["cluster"], row["delta_net"]) for row in group]),
            }
            for key, group in sorted(paired.items())
        },
        "overlap": overlap_summary(rows),
        "concordance_filter": concordance_filter_summary(rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "minimal_lifecycle_v1")
    parser.add_argument("--risk-budget-usdt", type=float, default=18.0)
    parser.add_argument("--notional-cap-usdt", type=float, default=100.0)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; use a new versioned directory")
    args.output.mkdir(parents=True)
    bundle: dict[str, Any] = {
        "artifact_version": "minimal_lifecycle_v1", "winner_claim_allowed": False,
        "scorer_sha256": _sha256(Path(__file__)),
        "economics_sha256": _sha256(REPO_ROOT / "Dev/scripts/analyze_universal_entry_stop_survival.py"),
        "contract": {
            "stops_atr": [1.5, 2.0, 2.5], "horizon_minutes": 240, "fee_bps_per_fill": 5.0,
            "risk_budget_usdt": args.risk_budget_usdt, "reference_notional_cap_usdt": args.notional_cap_usdt,
            "sizing": "ex ante risk estimated at 20bps per fill; same size for all cost scenarios; notional cap may prevent equal dollar risk",
            "funding": "signed cost sensitivities -1/0/+1bps per 8h prorated; not actual settlement funding",
            "exit": "initial stop, else last 1m close at horizon; no TP, trailing, partials or existing lifecycle",
            "unit": "independent opportunity economics, NOT a capital-constrained portfolio backtest",
            "inference": "equal-cluster bootstrap, 500 replicates, fixed seed; pointwise without multiplicity adjustment",
        },
        "phases": {
            "minimum_lifecycle": "validated", "development": "running",
            "explored_validation": "pending", "overlap": "pending",
            "combined_strategy": "pending_single_frozen_concordance_filter",
            "fresh_confirmation": "not_started", "staging": "unchanged",
        },
        "limitations": [
            "Historical market fills, filters, book depth, leverage and account margin are not reconstructed.",
            "Stop time is known only to the minute; holding time uses trigger-bar end as an upper bound.",
            "Four-hour last-bar close is a modeled reference, not a guaranteed market fill.",
            "Breakout and volatility variants can overlap; clusters are not independent portfolio positions.",
            "Overlap is exact symbol/time/side only; it does not test near-time confirmation or portfolio diversification.",
            "Shared-event return correlation can be mechanically high because entry and exit rules are identical.",
            "Previously explored July windows cannot support a pristine confirmation claim.",
        ],
        "windows": {},
    }
    report_path = args.output / "report.json"
    for label, role, relative in SOURCES:
        result = analyze_tape(ROOT / relative / "entry_stop_survival_v1", args.output / f"{label}.jsonl", args.risk_budget_usdt, args.notional_cap_usdt)
        bundle["windows"][label] = {"role": role, **result}
        if label == "development_sep2025":
            bundle["phases"]["development"] = "completed"
            bundle["phases"]["explored_validation"] = "running"
        report_path.write_text(json.dumps(bundle, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(json.dumps({"phase": role, "window": label, "proposals": result["proposal_count"], "scored_rows": result["scored_rows"]}), flush=True)
    bundle["phases"]["explored_validation"] = "completed"
    bundle["phases"]["overlap"] = "completed_diagnostic_only"
    bundle["phases"]["combined_strategy"] = "completed_single_exploratory_concordance_filter"
    report_path.write_text(json.dumps(bundle, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report_path), "phases": bundle["phases"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())