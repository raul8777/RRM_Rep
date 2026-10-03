"""Screen 100,000 regime-switching policies using local, closed UM market context."""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from Dev.scripts.analyze_universal_entry_stop_survival import _sha256
from Dev.scripts.screen_100000_market_regime_formulas import _market_contexts, NORMALIZED_BY_SOURCE
from Dev.scripts.screen_100000_universal_entry_formulas import _read_window as _read_corrected_window, DEVELOPMENT, EXPLORED, ROOT
from simulation.mimic_feed import HistoricalDataFeed


MARKET_STATES = ("broad_risk_off", "broad_risk_on", "high_volatility", "low_volatility", "mixed")
FAMILY_SIDES = tuple(
    f"{family}|{side}"
    for family in ("mean_reversion", "breakout_expansion", "volatility_expansion", "cross_sectional_relative_strength")
    for side in ("LONG", "SHORT")
)
ACTIONS = (*FAMILY_SIDES, "cross_sectional_relative_strength|BOTH", "__ABSTAIN__")
COSTS = (10, 20)
MIN_TOTAL_SELECTED = 100
MIN_SELECTED_PER_ACTIVE_STATE = 20
TOP_EXPLORED = 1000


def classify_market_state(context: dict[str, Any]) -> str:
    btc_return = float(context["btc_return_4h"])
    bullish_breadth = float(context["market_breadth_long_4h"])
    bearish_breadth = 1.0 - bullish_breadth
    if btc_return <= -0.003 and bearish_breadth >= 0.95:
        return "broad_risk_off"
    if btc_return >= 0.003 and bullish_breadth >= 0.95:
        return "broad_risk_on"
    if float(context["market_volatility_breadth"]) >= 0.25:
        return "high_volatility"
    if float(context["market_volatility_breadth"]) <= 0.10:
        return "low_volatility"
    return "mixed"


def iter_assignments():
    for sequence, actions in enumerate(itertools.product(ACTIONS, repeat=len(MARKET_STATES)), 1):
        yield {
            "strategy_id": f"M{sequence:06d}",
            "actions_by_state": dict(zip(MARKET_STATES, actions)),
        }


def build_assignments() -> list[dict[str, Any]]:
    assignments = list(iter_assignments())
    if len(assignments) != 100000 or len({row["strategy_id"] for row in assignments}) != 100000:
        raise AssertionError("scenario-switch grid must contain exactly 100,000 unique mappings")
    return assignments


def assignment_from_id(strategy_id: str) -> dict[str, Any]:
    if not strategy_id.startswith("M") or not strategy_id[1:].isdigit():
        raise ValueError(f"invalid scenario mapping ID: {strategy_id}")
    sequence = int(strategy_id[1:]) - 1
    if not 0 <= sequence < len(ACTIONS) ** len(MARKET_STATES):
        raise ValueError(f"invalid scenario mapping ID: {strategy_id}")
    digits = [0] * len(MARKET_STATES)
    for index in range(len(MARKET_STATES) - 1, -1, -1):
        digits[index] = sequence % len(ACTIONS)
        sequence //= len(ACTIONS)
    return {
        "strategy_id": strategy_id,
        "actions_by_state": {state: ACTIONS[digit] for state, digit in zip(MARKET_STATES, digits)},
    }


def _family_mask(actions_by_state: dict[str, str]) -> int:
    families = (
        "mean_reversion", "breakout_expansion", "volatility_expansion",
        "cross_sectional_relative_strength",
    )
    present = {action.split("|", 1)[0] for action in actions_by_state.values() if action != "__ABSTAIN__"}
    return sum(1 << index for index, family in enumerate(families) if family in present)


def _choice_matches(action: str, proposal: dict[str, Any]) -> bool:
    if action == "__ABSTAIN__":
        return False
    if action == "cross_sectional_relative_strength|BOTH":
        return proposal["philosophy_family"] == "cross_sectional_relative_strength"
    return action == f"{proposal['philosophy_family']}|{proposal['side']}"


def build_state_components(
    proposals: list[dict[str, Any]],
    outcomes: dict[str, dict[str, Any]],
    context_by_decision: dict[int, dict[str, Any]],
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, int], int]:
    clusters_by_state: dict[str, set[str]] = {state: set() for state in MARKET_STATES}
    selected_by_state_action: dict[str, dict[str, dict[str, dict[str, Any]]]] = {
        state: {action: {} for action in ACTIONS} for state in MARKET_STATES
    }
    all_clusters = set()
    for proposal in proposals:
        decision = int(proposal["decision_time"])
        if decision not in context_by_decision:
            raise ValueError(f"missing causal context at decision time {decision}")
        state = classify_market_state(context_by_decision[decision])
        cluster = str(proposal.get("dedupe_cluster_id") or proposal["opportunity_event_id"])
        event_id = proposal["opportunity_event_id"]
        all_clusters.add(cluster)
        clusters_by_state[state].add(cluster)
        outcome = outcomes[event_id]
        row = {
            "cluster": cluster,
            "event_id": event_id,
            "net10": float(outcome["lifecycle"]["scenarios"]["slippage_10bps_per_fill"]["net_usdt_no_funding"]),
            "net20": float(outcome["lifecycle"]["scenarios"]["slippage_20bps_per_fill"]["net_usdt_no_funding"]),
        }
        matching_actions = [action for action in ACTIONS if _choice_matches(action, proposal)]
        for action in matching_actions:
            if cluster in selected_by_state_action[state][action]:
                raise ValueError(f"action {action} emits more than one proposal for cluster {cluster}")
            selected_by_state_action[state][action][cluster] = row

    components: dict[str, dict[str, dict[str, Any]]] = {state: {} for state in MARKET_STATES}
    for state in MARKET_STATES:
        for action in ACTIONS:
            rows = list(selected_by_state_action[state][action].values())
            rows.sort(key=lambda row: row["cluster"])
            components[state][action] = {
                "selected_events": len(rows),
                "clusters": [row["cluster"] for row in rows],
                "selection_sha256": hashlib.sha256("\n".join(row["cluster"] for row in rows).encode("utf-8")).hexdigest(),
                "net_sums": {
                    10: sum(row["net10"] for row in rows),
                    20: sum(row["net20"] for row in rows),
                },
                "gains": {
                    10: sum(max(0.0, row["net10"]) for row in rows),
                    20: sum(max(0.0, row["net20"]) for row in rows),
                },
                "losses": {
                    10: -sum(min(0.0, row["net10"]) for row in rows),
                    20: -sum(min(0.0, row["net20"]) for row in rows),
                },
            }
    return components, {state: len(clusters) for state, clusters in clusters_by_state.items()}, len(all_clusters)


def _score_assignment(
    assignment: dict[str, Any],
    components: dict[str, dict[str, dict[str, Any]]],
    state_opportunity_counts: dict[str, int],
    total_opportunities: int,
    best_static: dict[int, float],
) -> dict[str, Any]:
    actions = assignment["actions_by_state"]
    chosen = [components[state][actions[state]] for state in MARKET_STATES]
    selected_events = sum(component["selected_events"] for component in chosen)
    if selected_events != sum(len(component["clusters"]) for component in chosen):
        raise AssertionError("state component event count mismatch")
    selected_clusters = [cluster for component in chosen for cluster in component["clusters"]]
    if len(selected_clusters) != len(set(selected_clusters)):
        raise ValueError("mutually exclusive market states selected the same opportunity twice")

    by_cost = {}
    for cost in COSTS:
        net_sum = sum(component["net_sums"][cost] for component in chosen)
        gains = sum(component["gains"][cost] for component in chosen)
        losses = sum(component["losses"][cost] for component in chosen)
        state_results = {}
        for state, component in zip(MARKET_STATES, chosen):
            state_count = state_opportunity_counts[state]
            state_results[state] = {
                "action": actions[state],
                "source_opportunities": state_count,
                "selected_events": component["selected_events"],
                "selected_coverage_pct": 100.0 * component["selected_events"] / state_count if state_count else None,
                "net_per_state_source_opportunity": component["net_sums"][cost] / state_count if state_count else None,
            }
        by_cost[cost] = {
            "net_per_unique_source_opportunity_abstentions_zero": net_sum / total_opportunities if total_opportunities else None,
            "selected_conditional_mean_net_usdt": net_sum / selected_events if selected_events else None,
            "selected_profit_factor": gains / losses if losses else None,
            "best_static_net_per_unique_source_opportunity": best_static[cost],
            "paired_delta_vs_best_static_per_opportunity": (net_sum / total_opportunities - best_static[cost]) if total_opportunities else None,
            "by_state": state_results,
        }
    state_signature = tuple(
        (state, actions[state], components[state][actions[state]]["selection_sha256"])
        for state in MARKET_STATES
    )
    return {
        **assignment,
        "selected_events": selected_events,
        "unique_source_opportunities": total_opportunities,
        "coverage_pct": 100.0 * selected_events / total_opportunities if total_opportunities else None,
        "genuine_switch": len(set(actions.values())) > 1,
        "selection_signature": state_signature,
        "scenarios": by_cost,
    }


def score_window(
    proposals: list[dict[str, Any]],
    outcomes: dict[str, dict[str, Any]],
    contexts: dict[int, dict[str, Any]],
    assignments: Iterable[dict[str, Any]],
    detail_path: Path,
) -> dict[str, Any]:
    components, state_opportunity_counts, total_opportunities = build_state_components(proposals, outcomes, contexts)
    best_static = {}
    for cost in COSTS:
        static_net = {
            action: sum(components[state][action]["net_sums"][cost] for state in MARKET_STATES) / total_opportunities
            for action in ACTIONS if action != "__ABSTAIN__"
        }
        best_static[cost] = max(static_net.values()) if static_net else 0.0
    assignment_count = 0
    with detail_path.open("w", encoding="utf-8", newline="\n") as target:
        for assignment in assignments:
            score = _score_assignment(assignment, components, state_opportunity_counts, total_opportunities, best_static)
            target.write(json.dumps(score, ensure_ascii=False, allow_nan=False) + "\n")
            assignment_count += 1
    metadata = {
        "assignment_count": assignment_count,
        "detail_sha256": _sha256(detail_path),
        "state_source_opportunities": state_opportunity_counts,
        "total_unique_source_opportunities": total_opportunities,
        "best_static_family_side_action": {
            str(cost): max(
                (action for action in ACTIONS if action != "__ABSTAIN__"),
                key=lambda action: sum(components[state][action]["net_sums"][cost] for state in MARKET_STATES) / total_opportunities,
            ) for cost in COSTS
        },
        "best_static_net_per_source_opportunity": {str(cost): value for cost, value in best_static.items()},
    }
    return metadata


def _read_window(source_relative: str, outcomes_relative: str, role: str):
    proposals, outcomes, hashes = _read_corrected_window(source_relative, outcomes_relative, role)
    normalized_name = NORMALIZED_BY_SOURCE[source_relative]
    normalized_um = ROOT / "normalized" / normalized_name / "um"
    symbols = sorted({row["symbol"] for row in proposals} | {"BTCUSDT"})
    feed = HistoricalDataFeed(str(normalized_um))
    hourly = {}
    source_hashes = {}
    for symbol in symbols:
        manifest_path = normalized_um / f"{symbol}_1m.manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("venue") != "um" or manifest.get("interval") != "1m":
            raise ValueError(f"UM scenario source manifest mismatch: {symbol}")
        hourly[symbol] = sorted(feed.get_klines(symbol, "1h"), key=lambda bar: int(bar["open_time"]))
        if len(hourly[symbol]) != int(manifest["rows"]) // 60:
            raise ValueError(f"incomplete local UM context tape: {symbol}")
        source_hashes[symbol] = {
            "manifest_sha256": _sha256(manifest_path),
            "parquet_sha256": _sha256(normalized_um / f"{symbol}_1m.parquet"),
        }
    contexts = _market_contexts(hourly, {int(row["decision_time"]) for row in proposals})
    if len(contexts) != len({int(row["decision_time"]) for row in proposals}):
        raise ValueError("missing closed market context for scenario policy")
    return proposals, outcomes, contexts, {**hashes, "um_context_source_hashes": source_hashes}


def _existing_window_metadata(
    label: str, source_relative: str, outcome_relative: str, detail_path: Path,
) -> dict[str, Any]:
    proposal_path = ROOT / source_relative / "philosophy_proposals.jsonl"
    outcome_path = ROOT / outcome_relative
    symbols = set()
    with proposal_path.open(encoding="utf-8") as source:
        for line in source:
            symbols.add(json.loads(line)["symbol"])

    normalized = ROOT / "normalized" / NORMALIZED_BY_SOURCE[source_relative]
    spot_hashes = {}
    um_hashes = {}
    for venue, venue_symbols, destination in (
        ("spot", sorted(symbols), spot_hashes),
        ("um", sorted(symbols | {"BTCUSDT"}), um_hashes),
    ):
        venue_dir = normalized / venue
        for symbol in venue_symbols:
            manifest_path = venue_dir / f"{symbol}_1m.manifest.json"
            parquet_path = venue_dir / f"{symbol}_1m.parquet"
            destination[symbol] = {
                "manifest_sha256": _sha256(manifest_path),
                ("one_minute_parquet_sha256" if venue == "spot" else "parquet_sha256"): _sha256(parquet_path),
            }

    first_row = None
    assignment_count = 0
    with detail_path.open(encoding="utf-8") as detail:
        for line in detail:
            if first_row is None:
                first_row = json.loads(line)
            assignment_count += 1
    if first_row is None:
        raise ValueError(f"empty cached scenario detail: {detail_path}")
    return {
        "role": "development",
        "proposals_sha256": _sha256(proposal_path),
        "lifecycle_sha256": _sha256(outcome_path),
        "volume_reference": "Spot source-candle volume / mean of immediately preceding 20 contiguous closed 1h candles; signal candle excluded",
        "spot_volume_source_hashes": spot_hashes,
        "um_context_source_hashes": um_hashes,
        "assignment_count": assignment_count,
        "detail_sha256": _sha256(detail_path),
        "state_source_opportunities": {
            state: first_row["scenarios"]["20"]["by_state"][state]["source_opportunities"]
            for state in MARKET_STATES
        },
        "total_unique_source_opportunities": first_row["unique_source_opportunities"],
        "best_static_net_per_source_opportunity": {
            str(cost): first_row["scenarios"][str(cost)]["best_static_net_per_unique_source_opportunity"]
            for cost in COSTS
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "scenario_switch_100k_v1")
    parser.add_argument("--rank-existing-dir", type=Path)
    parser.add_argument("--exploratory-top-count", type=int, default=1000)
    args = parser.parse_args()
    assignment_count = len(ACTIONS) ** len(MARKET_STATES)
    if assignment_count != 100000:
        raise AssertionError("scenario-switch product must contain exactly 100,000 unique mappings")
    if not 0 <= args.exploratory_top_count <= 10000:
        parser.error("exploratory top count must be between 0 and 10,000")
    development_metadata = {}
    if args.rank_existing_dir:
        output_dir = args.rank_existing_dir.resolve()
        if not output_dir.is_dir() or (output_dir / "report.json").exists():
            parser.error("rank-existing-dir must be a completed score directory without report.json")
        grid_path = output_dir / "scenario_assignment_grid.jsonl"
        if not grid_path.is_file():
            parser.error("rank-existing-dir is missing scenario_assignment_grid.jsonl")
        with grid_path.open(encoding="utf-8") as grid_source:
            grid_rows = sum(1 for _ in grid_source)
        if grid_rows != assignment_count:
            parser.error(f"cached scenario grid has {grid_rows} rows; expected {assignment_count}")
        for label, source, outcome_relative in DEVELOPMENT:
            detail_path = output_dir / f"{label}.jsonl"
            if not detail_path.is_file():
                parser.error(f"rank-existing-dir is missing {detail_path.name}")
            metadata = _existing_window_metadata(label, source, outcome_relative, detail_path)
            if metadata["assignment_count"] != assignment_count:
                parser.error(f"cached detail {detail_path.name} has an incomplete assignment grid")
            development_metadata[label] = metadata
    else:
        output_dir = args.output_dir.resolve()
        if output_dir.exists():
            parser.error("output exists; specify a new versioned directory")
        output_dir.mkdir(parents=True)
        grid_path = output_dir / "scenario_assignment_grid.jsonl"
        with grid_path.open("w", encoding="utf-8", newline="\n") as target:
            for assignment in iter_assignments():
                target.write(json.dumps(assignment, ensure_ascii=False, allow_nan=False) + "\n")
        for label, source, outcome_relative in DEVELOPMENT:
            proposals, outcomes, contexts, hashes = _read_window(source, outcome_relative, "development")
            detail_path = output_dir / f"{label}.jsonl"
            metadata = score_window(proposals, outcomes, contexts, iter_assignments(), detail_path)
            development_metadata[label] = {"role": "development", **hashes, **metadata}
            print(json.dumps({"window": label, "role": "development", "proposals": len(proposals), "mappings": metadata["assignment_count"], "scenario_clusters": metadata["state_source_opportunities"]}), flush=True)
            del proposals, outcomes, contexts

    ranked = []
    development_selection_pairs = set()
    first_detail = output_dir / f"{DEVELOPMENT[0][0]}.jsonl"
    second_detail = output_dir / f"{DEVELOPMENT[1][0]}.jsonl"
    with first_detail.open(encoding="utf-8") as first_source, second_detail.open(encoding="utf-8") as second_source:
        for first_line, second_line in itertools.zip_longest(first_source, second_source):
            if first_line is None or second_line is None:
                raise ValueError("development detail files have different mapping counts")
            dev = [json.loads(first_line), json.loads(second_line)]
            sid = dev[0]["strategy_id"]
            if dev[1]["strategy_id"] != sid:
                raise ValueError("development mapping order mismatch")
            pair_blob = json.dumps([row["selection_signature"] for row in dev], separators=(",", ":"))
            development_selection_pairs.add(hashlib.sha256(pair_blob.encode("utf-8")).hexdigest())
            genuine_switch = all(row["genuine_switch"] for row in dev)
            actions_by_state = dev[0]["actions_by_state"]
            sampled_states = all(
                action == "__ABSTAIN__" or row["scenarios"]["20"]["by_state"][state]["selected_events"] >= MIN_SELECTED_PER_ACTIVE_STATE
                for row in dev
                for state, action in row["actions_by_state"].items()
            )
            eligible = genuine_switch and sampled_states and all(row["selected_events"] >= MIN_TOTAL_SELECTED for row in dev)
            net_pass = all(row["scenarios"][str(cost)]["net_per_unique_source_opportunity_abstentions_zero"] > 0 for row in dev for cost in COSTS)
            beats_static = all(
                row["scenarios"][str(cost)]["net_per_unique_source_opportunity_abstentions_zero"]
                > row["scenarios"][str(cost)]["best_static_net_per_unique_source_opportunity"]
                for row in dev for cost in COSTS
            )
            state_net_pass = all(
                action == "__ABSTAIN__" or all(row["scenarios"][str(cost)]["by_state"][state]["net_per_state_source_opportunity"] > 0 for cost in COSTS)
                for row in dev for state, action in row["actions_by_state"].items()
            )
            passed = eligible and net_pass and beats_static and state_net_pass
            worst20 = min(row["scenarios"]["20"]["net_per_unique_source_opportunity_abstentions_zero"] for row in dev)
            ranked.append({
                "strategy_id": sid, "screen_eligible": eligible,
                "family_mask": _family_mask(actions_by_state),
                "genuine_switch": genuine_switch,
                "worst_development_net_per_opportunity_20bps": worst20 if eligible else None,
                "development_gate_passed": passed,
            })
    ranked.sort(key=lambda row: (
        row["screen_eligible"],
        row["worst_development_net_per_opportunity_20bps"] if row["worst_development_net_per_opportunity_20bps"] is not None else float("-inf"),
    ), reverse=True)

    report = {
        "artifact_version": "scenario_switch_100k_v1",
        "scorer_sha256": _sha256(Path(__file__)), "candidate_grid_sha256": _sha256(grid_path),
        "candidate_count": assignment_count, "distinct_development_selection_pairs": len(development_selection_pairs),
        "development_gate_pass_count": sum(row["development_gate_passed"] for row in ranked),
        "development_winner": next((row["strategy_id"] for row in ranked if row["development_gate_passed"]), None),
        "winner_claim_allowed": False,
        "contract": {
            "scenarios": MARKET_STATES,
            "actions_per_scenario": ACTIONS,
            "grid": "10 choices per scenario across five mutually-exclusive states, 10^5 scenario-switch mappings",
            "scenario_definitions": {
                "broad_risk_off": "BTC UM 4h return <= -0.3% and >=95% of market down",
                "broad_risk_on": "BTC UM 4h return >= +0.3% and >=95% of market up",
                "high_volatility": "after broad direction states, >=25% of market symbols at >=2x 1h true-range ratio",
                "low_volatility": "after directional states, <=10% of symbols at >=2x true-range ratio",
                "mixed": "remaining closed-hour states",
            },
            "context_timing": "UM hourly candles only; last fully closed hour before decision",
            "choice_definition": "one fixed family-side, RS both-sides, or abstain action per market state",
            "lifecycle": "corrected 1.5ATR stop-or-four-hour close; fee5bps/fill; slip10/20bps",
            "primary_metric": "net per unique source symbol-time opportunity, abstentions zero",
            "gate": ">=100 total selected per dev window, >=20 per active state/window, positive net10/net20 overall and per active state in both dev windows, and beats best single static family-side rule at both costs",
            "validation": "July tapes already explored, diagnostic only",
        },
        "development_windows": development_metadata,
        "ranking_top_1000": [
            {**row, "actions_by_state": assignment_from_id(row["strategy_id"])["actions_by_state"]}
            for row in ranked[:1000]
        ],
        "eligible_count": sum(row["screen_eligible"] for row in ranked),
        "limitations": [
            "Regime boundaries were predeclared from the local v4 regime screen; reusing them is exploratory, not independent confirmation.",
            "100,000 mappings are highly dependent; no multiplicity-adjusted inference is computed.",
            "Choice set omits time-series momentum because its development family-side samples are below reliable minimums.",
            "Opportunity PnL sums assume independent tickets and do not enforce concurrent capital, portfolio concentration, or wallet margin.",
            "No external API, funding service, exchange order books, live fills, or runtime/staging path is used.",
        ],
        "exploratory_windows": {},
    }
    top_ids = {row["strategy_id"] for row in ranked[:args.exploratory_top_count]}
    best_families = {}
    for family_index, family in enumerate(("mean_reversion", "breakout_expansion", "volatility_expansion", "cross_sectional_relative_strength")):
        eligible_rows = []
        for row in ranked:
            if row["screen_eligible"] and row["family_mask"] & (1 << family_index):
                eligible_rows.append(row)
        if eligible_rows:
            best_families[family] = eligible_rows[0]
    top_ids.update(row["strategy_id"] for row in best_families.values())
    report["exploratory_strategy_ids"] = sorted(top_ids)
    for label, source, outcome_relative in EXPLORED:
        proposals, outcomes, contexts, hashes = _read_window(source, outcome_relative, "explored_validation")
        top_assignments = [assignment_from_id(sid) for sid in sorted(top_ids)]
        detail_path = output_dir / f"{label}_top_scenario_switches.jsonl"
        metadata = score_window(proposals, outcomes, contexts, top_assignments, detail_path)
        report["exploratory_windows"][label] = {"role": "explored_validation", **hashes, **metadata}
        print(json.dumps({"window": label, "role": "explored_validation", "mappings": metadata["assignment_count"]}), flush=True)
        del proposals, outcomes, contexts, top_assignments
    report_path = output_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report_path), "mappings": assignment_count, "eligible": report["eligible_count"], "gate_passes": report["development_gate_pass_count"], "winner": report["development_winner"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())