"""Screen a fixed 10,000-formula entry grid on frozen proposal/lifecycle tapes."""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import statistics
from collections import defaultdict
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
COSTS = (10, 20)
MINIMUM_SELECTED_PER_WINDOW = 100
CONFIRMATION_LEVELS = {
    "directional_return": (0.0, 0.001, 0.002, 0.003, 0.005, 0.0075, 0.01, 0.015, 0.02, 0.03),
    "activity": (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0),
    "rsi_or_relative_extreme": (5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0, 45.0, 50.0),
    "macd_or_relative_impulse": (0.0, 0.00001, 0.00002, 0.00003, 0.00004, 0.00005, 0.00006, 0.00007, 0.00008, 0.00009),
    "ema_displacement_atr": (0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45),
    "sma_displacement_atr": (0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45),
    "bollinger_displacement": (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9),
    "weekly_location": (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
    "sr_location": (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
    "atr_or_cross_section_regime": (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0),
}
CONFIRMATION_MODES = tuple(CONFIRMATION_LEVELS)
RELATIVE_CONFIRMATION_LEVELS = {
    "rsi_or_relative_extreme": (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75),
    "macd_or_relative_impulse": (0.0, 0.001, 0.002, 0.003, 0.005, 0.0075, 0.01, 0.015, 0.02, 0.03),
    "bollinger_displacement": (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75),
}


def confirmation_levels(family: str, mode: str) -> tuple[float, ...]:
    if family == "cross_sectional_relative_strength" and mode in RELATIVE_CONFIRMATION_LEVELS:
        return RELATIVE_CONFIRMATION_LEVELS[mode]
    return CONFIRMATION_LEVELS[mode]


def confirmation_description(family: str, mode: str, level: float) -> str:
    if family == "cross_sectional_relative_strength":
        breadth = (5, 8, 10, 12, 15, 18, 20, 23, 26, 28)
        index = confirmation_levels(family, mode).index(level)
        return {
            "directional_return": f"side-aligned 24h return >= {level:g}",
            "activity": f"cross-section breadth >= {breadth[index]} symbols",
            "rsi_or_relative_extreme": f"absolute relative z-score >= {level:g}",
            "macd_or_relative_impulse": f"side-aligned 24h return >= {level:g}",
            "ema_displacement_atr": f"side-aligned 24h return >= {level / 100.0:g}",
            "sma_displacement_atr": f"side-aligned 24h return >= {level / 100.0:g}",
            "bollinger_displacement": f"absolute relative z-score >= {level:g}",
            "weekly_location": f"cross-section breadth >= {breadth[index]} symbols",
            "sr_location": f"cross-section breadth >= {breadth[index]} symbols",
            "atr_or_cross_section_regime": f"cross-sectional return dispersion >= {level / 100.0:g}",
        }[mode]
    return {
        "directional_return": f"side-aligned 4h return >= {level:g}",
        "activity": f"source volume / trailing average >= {level:g}",
        "rsi_or_relative_extreme": f"family/side RSI rule at {level:g}",
        "macd_or_relative_impulse": f"directional MACD histogram / close >= {level:g}",
        "ema_displacement_atr": f"directional EMA distance >= {level:g} ATR (countertrend for mean reversion)",
        "sma_displacement_atr": f"directional SMA distance >= {level:g} ATR (countertrend for mean reversion)",
        "bollinger_displacement": f"directional band displacement >= {level:g} widths (countertrend for mean reversion)",
        "weekly_location": f"weekly range location threshold {level:g}",
        "sr_location": f"prior 24h S/R location threshold {level:g}",
        "atr_or_cross_section_regime": f"ATR percent >= {level:g}%",
    }[mode]


def trigger_step(family: str) -> float:
    return {
        "mean_reversion": 0.1,
        "time_series_momentum": 0.001,
        "breakout_expansion": 0.05,
        "volatility_expansion": 0.1,
        "cross_sectional_relative_strength": 0.1,
    }[family]


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


def _relative_alignment(value: float, side: str, threshold: float) -> bool:
    return value * (1.0 if side == "LONG" else -1.0) >= threshold


def confirmation_passes(mode: str, level: float, family: str, proposal: dict[str, Any]) -> bool:
    features = proposal["feature_snapshot"]
    side = proposal["side"]
    direction = 1.0 if side == "LONG" else -1.0
    is_relative = family == "cross_sectional_relative_strength"
    if mode == "directional_return":
        value = float(features["return_24h"] if is_relative else features["return_4h"])
        return _relative_alignment(value, side, level)
    if mode == "activity":
        if is_relative:
            breadth_levels = (5, 8, 10, 12, 15, 18, 20, 23, 26, 28)
            required = breadth_levels[CONFIRMATION_LEVELS[mode].index(level)]
            return int(features["cross_section_size"]) >= required
        return float(features["predictor_indicators"]["vol_spike"]["value"]) >= level
    if mode == "rsi_or_relative_extreme":
        if is_relative:
            return abs(float(features["relative_strength_zscore_24h"])) >= level
        rsi = float(features["predictor_indicators"]["rsi"]["value"])
        if family == "mean_reversion":
            return rsi <= level if side == "LONG" else rsi >= 100.0 - level
        return rsi >= level if side == "LONG" else rsi <= 100.0 - level
    if mode == "macd_or_relative_impulse":
        if is_relative:
            return _relative_alignment(float(features["return_24h"]), side, level)
        histogram = float(features["predictor_indicators"]["macd"]["histogram"])
        close = float(features["close"])
        return direction * histogram / close >= level
    if mode in {"ema_displacement_atr", "sma_displacement_atr"}:
        if is_relative:
            return _relative_alignment(float(features["return_24h"]), side, level / 100.0)
        indicator = "ema" if mode == "ema_displacement_atr" else "sma"
        center = float(features["predictor_indicators"][indicator]["value"])
        atr = float(features["atr_24h"])
        displacement = direction * (float(features["close"]) - center) / atr
        if family == "mean_reversion":
            displacement = -displacement
        return displacement >= level
    if mode == "bollinger_displacement":
        if is_relative:
            return abs(float(features["relative_strength_zscore_24h"])) >= level
        bands = features["predictor_indicators"]["bollinger_bands"]
        close = float(features["close"])
        middle = float(bands["middle"])
        upper = float(bands["upper"])
        lower = float(bands["lower"])
        if family == "mean_reversion":
            width = middle - lower if side == "LONG" else upper - middle
            displacement = (middle - close) / width if side == "LONG" else (close - middle) / width
        else:
            width = upper - middle if side == "LONG" else middle - lower
            displacement = (close - middle) / width if side == "LONG" else (middle - close) / width
        return width > 0 and displacement >= level
    if mode in {"weekly_location", "sr_location"}:
        if is_relative:
            breadth_levels = (5, 8, 10, 12, 15, 18, 20, 23, 26, 28)
            required = breadth_levels[CONFIRMATION_LEVELS[mode].index(level)]
            return int(features["cross_section_size"]) >= required
        indicator = "weekly_range" if mode == "weekly_location" else "sr_zone_position"
        position = float(features["predictor_indicators"][indicator]["value"] if isinstance(features["predictor_indicators"][indicator], dict) else features["predictor_indicators"][indicator])
        if family == "mean_reversion":
            return position <= level if side == "LONG" else position >= 1.0 - level
        return position >= level if side == "LONG" else position <= 1.0 - level
    if mode == "atr_or_cross_section_regime":
        if is_relative:
            std = float(features["cross_section_std_return_24h"])
            return std >= level / 100.0
        return float(features["predictor_indicators"]["atr_pct"]["value"]) >= level
    raise KeyError(f"unknown confirmation mode: {mode}")


def build_candidates() -> list[dict[str, Any]]:
    candidates = []
    for family, side, trigger_index, mode, confirmation_index in itertools.product(
        FAMILIES, SIDES, range(10), CONFIRMATION_MODES, range(10),
    ):
        confirmation_level = confirmation_levels(family, mode)[confirmation_index]
        candidates.append({
            "strategy_id": f"S{len(candidates) + 1:05d}",
            "family": family, "side": side,
            "trigger_level_index": trigger_index,
            "trigger_margin_min": trigger_index * trigger_step(family),
            "confirmation_mode": mode,
            "confirmation_level_index": confirmation_index,
            "confirmation_level": confirmation_level,
            "confirmation_definition": confirmation_description(family, mode, confirmation_level),
        })
    if len(candidates) != 10000 or len({row["strategy_id"] for row in candidates}) != 10000:
        raise AssertionError("formula grid must contain exactly 10,000 unique strategy specifications")
    return candidates


def candidate_selects(candidate: dict[str, Any], proposal: dict[str, Any]) -> bool:
    return (
        proposal["philosophy_family"] == candidate["family"]
        and proposal["side"] == candidate["side"]
        and signal_margin(candidate["family"], proposal) >= candidate["trigger_margin_min"]
        and confirmation_passes(
            candidate["confirmation_mode"], candidate["confirmation_level"],
            candidate["family"], proposal,
        )
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


def _score_candidate(candidate: dict[str, Any], family_rows: list[dict[str, Any]], outcomes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    selected = [row for row in family_rows if candidate_selects(candidate, row)]
    selected_ids = {row["opportunity_event_id"] for row in selected}
    selected_signature = hashlib.sha256("\n".join(sorted(selected_ids)).encode("utf-8")).hexdigest()
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
        "selection_signature_sha256": selected_signature,
        "scenarios": by_cost,
    }


def score_window(proposals: list[dict[str, Any]], outcomes: dict[str, dict[str, Any]], candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for proposal in proposals:
        grouped[(proposal["philosophy_family"], proposal["side"])].append(proposal)
    return {
        candidate["strategy_id"]: _score_candidate(
            candidate, grouped[(candidate["family"], candidate["side"])], outcomes,
        )
        for candidate in candidates
    }


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
            "grid": "5 families x 2 sides x 10 trigger margins x 10 confirmation modes x 10 confirmation thresholds = 10,000 unique formula specifications",
            "trigger_thresholds": "family-specific increments from generator baseline to +9 increments",
            "confirmation_thresholds": CONFIRMATION_LEVELS,
            "lifecycle": "existing stop-or-4h minimum lifecycle; 1.5 ATR stop",
            "costs": "5bps fee/fill; 10 and 20bps slippage/fill for ranking, outcomes read from frozen lifecycle tape",
            "primary_metric": "net per original family-side opportunity, abstentions zero",
            "development_gate": "at least 100 selected in each development window; positive net per source opportunity at 10 and 20bps in both windows",
            "inference": "exploratory ranking only; no per-candidate confidence intervals or multiple-testing correction",
            "exploratory_windows": "July results were previously examined and cannot confirm a selected strategy",
        },
        "candidate_definitions": candidates,
        "windows": {},
        "limitations": [
            "These 10,000 formulas are a fixed grid, not independent discoveries; winner selection creates severe multiple-testing bias.",
            "2025 development windows have already informed prior analysis; these results are a screen, not pristine inference.",
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
            "role": role, "proposal_count": len(proposals), **hashes, "strategies": scores,
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
    report["distinct_development_selection_sets"] = len({
        tuple(report["windows"][window]["strategies"][row["strategy_id"]]["selection_signature_sha256"] for window, *_ in DEVELOPMENT)
        for row in development_scores
    })
    passing = [row for row in report["ranking"] if row["development_gate_passed"]]
    report["development_winner"] = passing[0]["strategy_id"] if passing else None
    report_path = args.output_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report_path), "candidate_count": len(candidates), "distinct_development_selection_sets": report["distinct_development_selection_sets"], "development_winner": report["development_winner"], "top_development": report["ranking"][:10]}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
