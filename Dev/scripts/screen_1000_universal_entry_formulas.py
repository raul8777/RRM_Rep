"""Screen exactly 1,000 fixed, offline entry formulas on frozen proposal/lifecycle tapes."""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import statistics
from pathlib import Path
from typing import Any

from Dev.scripts.analyze_universal_entry_stop_survival import _rows, _sha256
from Dev.scripts.screen_ten_universal_entry_strategies import ROOT


DEVELOPMENT = (
    ("development_jul2025", "expanded_range_v1/cal_range_scalp_2_20250715_20250729", "minimal_lifecycle_v2/development_jul2025.jsonl"),
    ("development_sep2025", "expanded_range_v1/cal_range_scalp_3_20250909_20250923", "minimal_lifecycle_v2/development_sep2025.jsonl"),
)
EXPLORED = (
    ("explored_jul01_baseline", "expanded_range_oos_202607/cal_range_scalp_oos_20260701_20260716/baseline", "minimal_lifecycle_v2/explored_jul01_baseline.jsonl"),
    ("explored_jul17_baseline", "expanded_range_oos_20260717/cal_range_scalp_oos_20260717_20260801/baseline", "minimal_lifecycle_v2/explored_jul17_baseline.jsonl"),
)
FAMILIES = (
    "mean_reversion", "time_series_momentum", "breakout_expansion",
    "volatility_expansion", "cross_sectional_relative_strength",
)
SIDES = ("LONG", "SHORT")
CONFIRMATIONS = (
    "none", "directional", "activity_1", "activity_1_25", "activity_1_5",
    "extreme", "strong", "location_1", "location_2", "location_3",
)
COSTS = (10, 20)
MINIMUM_SELECTED_PER_WINDOW = 100


def threshold_increment(family: str, index: int) -> float:
    steps = {
        "mean_reversion": 0.1,
        "time_series_momentum": 0.001,
        "breakout_expansion": 0.05,
        "volatility_expansion": 0.1,
        "cross_sectional_relative_strength": 0.1,
    }
    if family not in steps or not 0 <= index < 10:
        raise ValueError("invalid family or threshold grid index")
    return index * steps[family]


def signal_margin(family: str, proposal: dict[str, Any]) -> float:
    features = proposal["feature_snapshot"]
    side = proposal["side"]
    if family == "mean_reversion":
        return abs(float(features["zscore_24h"])) - 2.0
    if family == "time_series_momentum":
        return abs(float(features["return_4h"])) - 0.005
    if family == "breakout_expansion":
        bound = float(features["prior_24h_high"] if side == "LONG" else features["prior_24h_low"])
        close = float(features["close"])
        atr = float(features["atr_24h"])
        return (close - bound if side == "LONG" else bound - close) / atr
    if family == "volatility_expansion":
        return float(features["true_range_expansion_ratio"]) - 2.0
    if family == "cross_sectional_relative_strength":
        return abs(float(features["relative_strength_zscore_24h"])) - 0.5
    raise ValueError(f"unknown family: {family}")


def confirmation_passes(name: str, family: str, proposal: dict[str, Any]) -> bool:
    if name == "none":
        return True
    features = proposal["feature_snapshot"]
    side = proposal["side"]
    if family == "cross_sectional_relative_strength":
        relative_z = float(features["relative_strength_zscore_24h"])
        return {
            "directional": float(features["return_24h"]) * (1 if side == "LONG" else -1) > 0,
            "activity_1": int(features["cross_section_size"]) >= 10,
            "activity_1_25": int(features["cross_section_size"]) >= 15,
            "activity_1_5": int(features["cross_section_size"]) >= 20,
            "extreme": abs(relative_z) >= 0.75,
            "strong": abs(relative_z) >= 1.0,
            "location_1": abs(float(features["return_24h"])) >= 0.01,
            "location_2": int(features["cross_section_size"]) >= 26,
            "location_3": int(features["cross_section_size"]) >= 28,
        }[name]

    indicators = features["predictor_indicators"]
    direction = 1.0 if side == "LONG" else -1.0
    if name == "directional":
        return float(indicators["trend_4h"]["value"]) == direction
    if name == "activity_1":
        return float(indicators["vol_spike"]["value"]) >= 1.0
    if name == "activity_1_25":
        return float(indicators["vol_spike"]["value"]) >= 1.25
    if name == "activity_1_5":
        return float(indicators["vol_spike"]["value"]) >= 1.5
    if name == "extreme":
        rsi = float(indicators["rsi"]["value"])
        if family == "mean_reversion":
            return rsi <= 40.0 if side == "LONG" else rsi >= 60.0
        return rsi >= 50.0 if side == "LONG" else rsi <= 50.0
    if name == "strong":
        return float(indicators["macd"]["histogram"]) * direction > 0
    if name == "location_1":
        close = float(features["close"])
        ema = float(indicators["ema"]["value"])
        return close >= ema if side == "LONG" else close <= ema
    if name == "location_2":
        position = float(indicators["weekly_range"]["value"])
        if family == "mean_reversion":
            return position <= 0.2 if side == "LONG" else position >= 0.8
        return position >= 0.5 if side == "LONG" else position <= 0.5
    if name == "location_3":
        position = float(indicators["sr_zone_position"])
        if family == "mean_reversion":
            return position <= 0.25 if side == "LONG" else position >= 0.75
        return position >= 0.5 if side == "LONG" else position <= 0.5
    raise KeyError(f"unknown confirmation: {name}")


def confirmation_description(name: str, family: str) -> str:
    if family == "cross_sectional_relative_strength":
        return {
            "none": "none",
            "directional": "24h return direction agrees with LONG/SHORT",
            "activity_1": "cross-section has >=10 symbols",
            "activity_1_25": "cross-section has >=15 symbols",
            "activity_1_5": "cross-section has >=20 symbols",
            "extreme": "absolute relative z-score >= 0.75",
            "strong": "absolute relative z-score >= 1.0",
            "location_1": "absolute 24h return >= 1%",
            "location_2": "cross-section has >=26 symbols",
            "location_3": "cross-section has >=28 symbols",
        }[name]
    return {
        "none": "none",
        "directional": "4h EMA trend agrees with LONG/SHORT",
        "activity_1": "source volume / prior average >=1.0",
        "activity_1_25": "source volume / prior average >=1.25",
        "activity_1_5": "source volume / prior average >=1.5",
        "extreme": "RSI exhaustion for mean reversion; otherwise directional RSI half",
        "strong": "MACD histogram agrees with LONG/SHORT",
        "location_1": "close on trade side of EMA",
        "location_2": "weekly extreme for mean reversion; otherwise weekly midpoint aligned",
        "location_3": "S/R extreme for mean reversion; otherwise S/R midpoint aligned",
    }[name]


def build_candidates() -> list[dict[str, Any]]:
    candidates = []
    for family, side, threshold_index, confirmation in itertools.product(
        FAMILIES, SIDES, range(10), CONFIRMATIONS,
    ):
        candidates.append({
            "strategy_id": f"S{len(candidates) + 1:04d}",
            "family": family,
            "side": side,
            "threshold_index": threshold_index,
            "trigger_margin_min": threshold_increment(family, threshold_index),
            "confirmation": confirmation,
            "confirmation_definition": confirmation_description(confirmation, family),
        })
    if len(candidates) != 1000 or len({row["strategy_id"] for row in candidates}) != 1000:
        raise AssertionError("candidate grid must contain exactly 1,000 unique strategies")
    return candidates


def candidate_selects(candidate: dict[str, Any], proposal: dict[str, Any]) -> bool:
    return (
        proposal["philosophy_family"] == candidate["family"]
        and proposal["side"] == candidate["side"]
        and signal_margin(candidate["family"], proposal) >= candidate["trigger_margin_min"]
        and confirmation_passes(candidate["confirmation"], candidate["family"], proposal)
    )


def _read_window(source_relative: str, outcomes_relative: str, role: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, str]]:
    source = ROOT / source_relative / "philosophy_proposals.jsonl"
    outcomes = ROOT / outcomes_relative
    source_rows = list(_rows(source))
    expected_role = "development" if role == "development" else "validation"
    if not source_rows or any(row.get("window_role") != expected_role for row in source_rows):
        raise ValueError(f"source proposal role mismatch: {source}")
    by_id = {row["opportunity_event_id"]: row for row in source_rows}
    if len(by_id) != len(source_rows):
        raise ValueError(f"duplicate event ids in source: {source}")
    outcome_by_id = {}
    for row in _rows(outcomes):
        if row["stop"] != "1.5":
            continue
        if row["event_id"] in outcome_by_id:
            raise ValueError(f"duplicate minimum lifecycle row: {row['event_id']}")
        outcome_by_id[row["event_id"]] = row
    if set(by_id) != set(outcome_by_id):
        raise ValueError(f"proposal/lifecycle join mismatch for {source}")
    return source_rows, outcome_by_id, {"proposals_sha256": _sha256(source), "lifecycle_sha256": _sha256(outcomes)}


def _score_candidate(candidate: dict[str, Any], proposals: list[dict[str, Any]], outcomes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    family_rows = [row for row in proposals if row["philosophy_family"] == candidate["family"] and row["side"] == candidate["side"]]
    selected = [row for row in family_rows if candidate_selects(candidate, row)]
    selected_ids = {row["opportunity_event_id"] for row in selected}
    by_cost = {}
    for bps in COSTS:
        key = f"slippage_{bps}bps_per_fill"
        baseline_net = [outcomes[row["opportunity_event_id"]]["lifecycle"]["scenarios"][key]["net_usdt_no_funding"] for row in family_rows]
        selected_net = [outcomes[row["opportunity_event_id"]]["lifecycle"]["scenarios"][key]["net_usdt_no_funding"] for row in selected]
        all_opportunity_net = [
            outcomes[row["opportunity_event_id"]]["lifecycle"]["scenarios"][key]["net_usdt_no_funding"]
            if row["opportunity_event_id"] in selected_ids else 0.0
            for row in family_rows
        ]
        gains = sum(max(0.0, value) for value in selected_net)
        losses = -sum(min(0.0, value) for value in selected_net)
        paired_delta = [value - base for value, base in zip(all_opportunity_net, baseline_net)]
        by_cost[key] = {
            "baseline_family_net_per_opportunity": statistics.mean(baseline_net) if baseline_net else None,
            "selected_conditional_mean_net_usdt": statistics.mean(selected_net) if selected_net else None,
            "selected_conditional_profit_factor": gains / losses if losses else None,
            "selected_net_per_family_opportunity_abstentions_zero": statistics.mean(all_opportunity_net) if all_opportunity_net else None,
            "paired_delta_per_family_opportunity": statistics.mean(paired_delta) if paired_delta else None,
        }
    return {
        **candidate,
        "family_opportunities": len(family_rows),
        "selected_events": len(selected),
        "coverage_pct": 100.0 * len(selected) / len(family_rows) if family_rows else 0.0,
        "scenarios": by_cost,
    }


def score_window(
    proposals: list[dict[str, Any]], outcomes: dict[str, dict[str, Any]], candidates: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {candidate["strategy_id"]: _score_candidate(candidate, proposals, outcomes) for candidate in candidates}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "one_thousand_formula_screen_v1")
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error("output exists; use a new versioned directory")
    args.output_dir.mkdir(parents=True)
    candidates = build_candidates()
    report: dict[str, Any] = {
        "artifact_version": "one_thousand_universal_entry_formulas_v1",
        "scorer_sha256": _sha256(Path(__file__)), "candidate_count": len(candidates),
        "winner_claim_allowed": False, "development_winner": None,
        "contract": {
            "grid": "5 families x 2 sides x 10 trigger margins x 10 fixed confirmation rules = 1,000 unique formulas",
            "trigger_steps": "family-specific increments above current generator threshold, from base to +9 increments",
            "confirmations": CONFIRMATIONS,
            "lifecycle": "existing stop-or-4h minimum lifecycle; 1.5 ATR stop",
            "costs": "5bps fee/fill; 10 and 20bps slippage/fill for gates, 0/5/10/20 retained in source lifecycle",
            "primary_metric": "net per original family-side opportunity, abstentions zero",
            "development_gate": "at least 100 selected in each development window; positive net per source opportunity at 10 and 20bps in both development windows",
            "inference": "exploratory rank only; 1,000-way search has no per-candidate confidence intervals or multiple-testing correction",
            "exploratory_windows": "July results were previously examined and cannot confirm a selected strategy",
        },
        "candidate_definitions": candidates,
        "windows": {},
        "limitations": [
            "These 1,000 formulas are a fixed search grid, not independent discoveries; winner selection creates multiple-testing bias.",
            "2025 development windows have already informed prior analysis; the predefined gates are screening, not pristine inference.",
            "July 2026 periods are already explored and are diagnostic only.",
            "Candidates only filter existing philosophy proposal tapes; they do not create ideas outside those generators.",
            "No shared-capital portfolio, exchange order filters, live fills, or settled funding is simulated.",
            "No strategy/configuration or trading service is changed.",
        ],
    }
    windows = (
        *((label, "development", source, outcomes) for label, source, outcomes in DEVELOPMENT),
        *((label, "explored_validation", source, outcomes) for label, source, outcomes in EXPLORED),
    )
    for label, role, source_relative, outcomes_relative in windows:
        proposals, outcomes, hashes = _read_window(source_relative, outcomes_relative, role)
        scores = score_window(proposals, outcomes, candidates)
        report["windows"][label] = {
            "role": role,
            "proposal_count": len(proposals),
            **hashes,
            "strategies": scores,
        }
        detail_path = args.output_dir / f"{label}.jsonl"
        detail_path.write_text(
            "".join(json.dumps({"strategy_id": key, **value}, allow_nan=False) + "\n" for key, value in scores.items()),
            encoding="utf-8",
        )
        report["windows"][label]["detail_sha256"] = _sha256(detail_path)
        print(json.dumps({"window": label, "role": role, "proposals": len(proposals), "strategies": len(scores)}), flush=True)

    development_scores = []
    for candidate in candidates:
        label = candidate["strategy_id"]
        dev = [report["windows"][window]["strategies"][label] for window, *_ in DEVELOPMENT]
        eligible = all(result["selected_events"] >= MINIMUM_SELECTED_PER_WINDOW for result in dev)
        score20 = [result["scenarios"]["slippage_20bps_per_fill"]["selected_net_per_family_opportunity_abstentions_zero"] for result in dev]
        score10 = [result["scenarios"]["slippage_10bps_per_fill"]["selected_net_per_family_opportunity_abstentions_zero"] for result in dev]
        passed = eligible and all(value > 0 for value in score10 + score20)
        development_scores.append({
            "strategy_id": label, "screen_eligible": eligible,
            "worst_development_net_per_opportunity_20bps": min(score20) if eligible else None,
            "development_gate_passed": passed,
        })
    report["ranking"] = sorted(
        development_scores,
        key=lambda row: (row["screen_eligible"], row["worst_development_net_per_opportunity_20bps"] if row["screen_eligible"] else float("-inf")),
        reverse=True,
    )
    passing = [row for row in report["ranking"] if row["development_gate_passed"]]
    report["development_winner"] = passing[0]["strategy_id"] if passing else None
    report_path = args.output_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report_path), "candidate_count": len(candidates), "development_winner": report["development_winner"], "top_development": report["ranking"][:10]}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())