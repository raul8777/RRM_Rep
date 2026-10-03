"""Screen 100,000 two-confirmation entry formulas with precomputed event masks."""
from __future__ import annotations

import argparse
import bisect
import hashlib
import itertools
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from Dev.scripts.analyze_universal_entry_stop_survival import REPO_ROOT, _rows, _sha256
from Dev.scripts.screen_10000_universal_entry_formulas import (
    CONFIRMATION_MODES, COSTS, DEVELOPMENT, EXPLORED,
    FAMILIES, MINIMUM_SELECTED_PER_WINDOW,
    ROOT, SIDES, confirmation_description, confirmation_levels,
    confirmation_passes, signal_margin, trigger_step,
)
from simulation.mimic_feed import HistoricalDataFeed

TRIGGER_LEVELS = tuple(range(10))
TOP_EXPLORED_COUNT = 1000
NORMALIZED_BY_SOURCE = {
    "expanded_range_v1/cal_range_scalp_2_20250715_20250729": "pilot_range2_28",
    "expanded_range_v1/cal_range_scalp_3_20250909_20250923": "pilot_range3_28",
    "expanded_range_oos_202607/cal_range_scalp_oos_20260701_20260716/baseline": "pilot_range_oos_27",
    "expanded_range_oos_20260717/cal_range_scalp_oos_20260717_20260801/baseline": "pilot_range_oos_20260717_27",
}


def prior_volume_ratio_20(bars: list[dict[str, Any]], source_candle_open_time: int) -> float:
    times = [int(round(float(bar["open_time"]))) for bar in bars]
    index = bisect.bisect_left(times, int(source_candle_open_time))
    if index >= len(bars) or times[index] != int(source_candle_open_time) or index < 20:
        raise ValueError("missing source candle or prior 20-hour volume warmup")
    previous_times = times[index - 20:index]
    if len(previous_times) != 20 or any(right - left != 3600 for left, right in zip(previous_times, previous_times[1:])):
        raise ValueError("prior volume reference contains missing hourly candles")
    if times[index] - previous_times[-1] != 3600:
        raise ValueError("signal candle is not contiguous with prior volume reference")
    prior_volumes = [float(bar.get("volume", 0.0)) for bar in bars[index - 20:index]]
    current_volume = float(bars[index].get("volume", 0.0))
    average_volume = statistics.mean(prior_volumes)
    if not math.isfinite(current_volume) or not math.isfinite(average_volume) or average_volume <= 0:
        raise ValueError("invalid source/prior volume")
    return current_volume / average_volume


def attach_prior_volume_reference(proposal: dict[str, Any], bars: list[dict[str, Any]]) -> None:
    ratio = prior_volume_ratio_20(bars, int(round(float(proposal["source_candle_open_time"]))))
    features = dict(proposal["feature_snapshot"])
    features["prior_volume_ratio_20"] = ratio
    if "predictor_indicators" in features:
        indicators = dict(features["predictor_indicators"])
        old_spike = dict(indicators["vol_spike"])
        indicators["vol_spike_snapshot_inclusive"] = old_spike
        indicators["vol_spike"] = {"value": ratio}
        features["predictor_indicators"] = indicators
    proposal["feature_snapshot"] = features


def confirmation_passes_corrected(mode: str, level: float, family: str, proposal: dict[str, Any]) -> bool:
    if mode == "activity" and family != "cross_sectional_relative_strength":
        ratio = proposal["feature_snapshot"].get("prior_volume_ratio_20")
        if ratio is None:
            raise ValueError("corrected prior-volume feature missing")
        return float(ratio) >= level
    return confirmation_passes(mode, level, family, proposal)


def build_candidates() -> list[dict[str, Any]]:
    candidates = []
    for family, side, trigger_index, first_mode, first_level_index, second_mode in itertools.product(
        FAMILIES, SIDES, TRIGGER_LEVELS, CONFIRMATION_MODES, range(10), CONFIRMATION_MODES,
    ):
        first_level = confirmation_levels(family, first_mode)[first_level_index]
        second_level = confirmation_levels(family, second_mode)[-1]
        candidates.append({
            "strategy_id": f"F{len(candidates) + 1:06d}",
            "family": family, "side": side,
            "trigger_level_index": trigger_index,
            "trigger_margin_min": trigger_index * trigger_step(family),
            "confirmation_1_mode": first_mode,
            "confirmation_1_level_index": first_level_index,
            "confirmation_1_level": first_level,
            "confirmation_1_definition": confirmation_description(family, first_mode, first_level),
            "confirmation_2_mode": second_mode,
            "confirmation_2_level_index": 9,
            "confirmation_2_level": second_level,
            "confirmation_2_definition": confirmation_description(family, second_mode, second_level),
        })
    expected = len(FAMILIES) * len(SIDES) * len(TRIGGER_LEVELS) * len(CONFIRMATION_MODES) * 10 * len(CONFIRMATION_MODES)
    ids = {candidate["strategy_id"] for candidate in candidates}
    if len(candidates) != 100000 or len(ids) != expected:
        raise AssertionError(f"expected 100,000 unique formula specifications, got {len(candidates)}")
    for candidate in candidates:
        if candidate["confirmation_1_mode"] == "activity" and candidate["family"] != "cross_sectional_relative_strength":
            candidate["confirmation_1_definition"] = (
                f"source volume / mean(previous 20 closed 1h volumes) >= {candidate['confirmation_1_level']:g}"
            )
        if candidate["confirmation_2_mode"] == "activity" and candidate["family"] != "cross_sectional_relative_strength":
            candidate["confirmation_2_definition"] = (
                f"source volume / mean(previous 20 closed 1h volumes) >= {candidate['confirmation_2_level']:g}"
            )
    return candidates


def _read_window(source_relative: str, outcomes_relative: str, role: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, str]]:
    source = ROOT / source_relative / "philosophy_proposals.jsonl"
    outcomes_path = ROOT / outcomes_relative
    source_rows = list(_rows(source))
    expected_role = "development" if role == "development" else "validation"
    if not source_rows or any(row.get("window_role") != expected_role for row in source_rows):
        raise ValueError(f"source role mismatch for {source}")
    by_id = {row["opportunity_event_id"]: row for row in source_rows}
    if len(by_id) != len(source_rows):
        raise ValueError(f"duplicate proposal IDs in {source}")
    normalized_name = NORMALIZED_BY_SOURCE.get(source_relative)
    if normalized_name is None:
        raise ValueError(f"no normalized feature source is registered for {source_relative}")
    normalized = ROOT / "normalized" / normalized_name / "spot"
    manifest_hashes = {}
    volume_feed = HistoricalDataFeed(str(normalized))
    hourly_by_symbol = {}
    for symbol in sorted({row["symbol"] for row in source_rows}):
        manifest_path = normalized / f"{symbol}_1m.manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("venue") != "spot" or manifest.get("interval") != "1m":
            raise ValueError(f"volume-reference manifest mismatch: {symbol}")
        bars = sorted(volume_feed.get_klines(symbol, "1h"), key=lambda bar: float(bar["open_time"]))
        if not bars or len(bars) != int(manifest["rows"]) // 60:
            raise ValueError(f"incomplete Spot hourly source for volume reference: {symbol}")
        hourly_by_symbol[symbol] = bars
        manifest_hashes[symbol] = {
            "manifest_sha256": _sha256(manifest_path),
            "one_minute_parquet_sha256": _sha256(normalized / f"{symbol}_1m.parquet"),
        }
    for proposal in source_rows:
        bars = hourly_by_symbol[proposal["symbol"]]
        attach_prior_volume_reference(proposal, bars)
    outcomes = {}
    for row in _rows(outcomes_path):
        if row["stop"] != "1.5":
            continue
        if row["event_id"] in outcomes:
            raise ValueError(f"duplicate lifecycle event {row['event_id']}")
        outcomes[row["event_id"]] = row
    if set(by_id) != set(outcomes):
        raise ValueError(f"proposal/lifecycle join mismatch for {source}")
    return source_rows, outcomes, {
        "proposals_sha256": _sha256(source),
        "lifecycle_sha256": _sha256(outcomes_path),
        "volume_reference": "Spot source-candle volume / mean of immediately preceding 20 contiguous closed 1h candles; signal candle excluded",
        "spot_volume_source_hashes": manifest_hashes,
    }


class FamilySideMasks:
    def __init__(self, proposals: list[dict[str, Any]], outcomes: dict[str, dict[str, Any]], family: str, side: str):
        self.proposals = [p for p in proposals if p["philosophy_family"] == family and p["side"] == side]
        self.family = family
        self.side = side
        self.count = len(self.proposals)
        self.full_mask = (1 << self.count) - 1
        self.outcomes = [outcomes[p["opportunity_event_id"]] for p in self.proposals]
        self.clusters = [row["cluster"] for row in self.outcomes]
        self.baseline = {
            bps: [row["lifecycle"]["scenarios"][f"slippage_{bps}bps_per_fill"]["net_usdt_no_funding"] for row in self.outcomes]
            for bps in COSTS
        }
        self.baseline_sums = {bps: sum(values) for bps, values in self.baseline.items()}
        self.trigger_masks = {}
        for trigger_index in TRIGGER_LEVELS:
            threshold = trigger_index * trigger_step(family)
            mask = 0
            for index, proposal in enumerate(self.proposals):
                if signal_margin(family, proposal) >= threshold:
                    mask |= 1 << index
            self.trigger_masks[trigger_index] = mask
        self.confirm_masks = {}
        for mode in CONFIRMATION_MODES:
            levels = confirmation_levels(family, mode)
            for level_index, level in enumerate(levels):
                mask = 0
                for index, proposal in enumerate(self.proposals):
                    if confirmation_passes_corrected(mode, level, family, proposal):
                        mask |= 1 << index
                self.confirm_masks[(mode, level_index)] = mask
        self.confirm_max_masks = {
            mode: self.confirm_masks[(mode, 9)] for mode in CONFIRMATION_MODES
        }
        self.score_cache: dict[int, dict[str, Any]] = {}
        self.signature_cache: dict[int, str] = {}

    def score(self, candidate: dict[str, Any]) -> dict[str, Any]:
        mask = (
            self.trigger_masks[candidate["trigger_level_index"]]
            & self.confirm_masks[(candidate["confirmation_1_mode"], candidate["confirmation_1_level_index"])]
            & self.confirm_max_masks[candidate["confirmation_2_mode"]]
        )
        if mask not in self.score_cache:
            selected_indices = []
            active = mask
            while active:
                lowest_bit = active & -active
                selected_indices.append(lowest_bit.bit_length() - 1)
                active ^= lowest_bit
            selected_count = len(selected_indices)
            costs = {}
            for bps in COSTS:
                baseline = self.baseline[bps]
                selected = [baseline[index] for index in selected_indices]
                selected_sum = sum(selected)
                losses = -sum(min(0.0, value) for value in selected)
                costs[f"slippage_{bps}bps_per_fill"] = {
                    "baseline_family_net_per_opportunity": self.baseline_sums[bps] / self.count if self.count else None,
                    "selected_conditional_mean_net_usdt": statistics.mean(selected) if selected else None,
                    "selected_conditional_profit_factor": sum(max(0.0, value) for value in selected) / losses if losses else None,
                    "selected_net_per_family_opportunity_abstentions_zero": selected_sum / self.count if self.count else None,
                    "paired_delta_per_family_opportunity": (selected_sum - self.baseline_sums[bps]) / self.count if self.count else None,
                }
            self.score_cache[mask] = {
                "selected_events": selected_count,
                "coverage_pct": 100.0 * selected_count / self.count if self.count else 0.0,
                "net10_per_source_opportunity": costs["slippage_10bps_per_fill"]["selected_net_per_family_opportunity_abstentions_zero"],
                "net20_per_source_opportunity": costs["slippage_20bps_per_fill"]["selected_net_per_family_opportunity_abstentions_zero"],
                "selected_mean10": costs["slippage_10bps_per_fill"]["selected_conditional_mean_net_usdt"],
                "selected_pf10": costs["slippage_10bps_per_fill"]["selected_conditional_profit_factor"],
            }
            self.signature_cache[mask] = hashlib.sha256(
                (self.family + "|" + self.side + "|").encode("utf-8")
                + mask.to_bytes(max(1, (self.count + 7) // 8), byteorder="little")
            ).hexdigest()
        return {
            "strategy_id": candidate["strategy_id"],
            **self.score_cache[mask],
            "selection_signature_sha256": self.signature_cache[mask],
        }


def prepare_window(proposals: list[dict[str, Any]], outcomes: dict[str, dict[str, Any]]) -> dict[tuple[str, str], FamilySideMasks]:
    return {
        (family, side): FamilySideMasks(proposals, outcomes, family, side)
        for family in FAMILIES for side in SIDES
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "one_hundred_thousand_formula_screen_v2")
    parser.add_argument("--exploratory-top-count", type=int, default=TOP_EXPLORED_COUNT)
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        parser.error("output directory exists; use a new versioned path")
    output_dir.mkdir(parents=True)
    if args.exploratory_top_count < 0 or args.exploratory_top_count > 10000:
        parser.error("exploratory top count must be between 0 and 10,000")
    candidates = build_candidates()
    candidate_by_id = {candidate["strategy_id"]: candidate for candidate in candidates}
    candidate_path = output_dir / "candidate_grid.jsonl"
    with candidate_path.open("w", encoding="utf-8", newline="\n") as target:
        for candidate in candidates:
            target.write(json.dumps(candidate, ensure_ascii=False, allow_nan=False) + "\n")

    development_index: dict[str, dict[str, dict[str, Any]]] = {}
    development_meta: dict[str, dict[str, Any]] = {}
    for label, source, outcomes_path in DEVELOPMENT:
        proposals, outcomes, hashes = _read_window(source, outcomes_path, "development")
        masks = prepare_window(proposals, outcomes)
        index: dict[str, dict[str, Any]] = {}
        development_meta[label] = {"proposal_count": len(proposals), **hashes, "mask_cache_unique_per_family_side": {f"{family}|{side}": len(masks[(family, side)].score_cache) for family in FAMILIES for side in SIDES}}
        detail_path = output_dir / f"{label}.jsonl"
        with detail_path.open("w", encoding="utf-8", newline="\n") as target:
            for candidate in candidates:
                score = masks[(candidate["family"], candidate["side"])].score(candidate)
                index[candidate["strategy_id"]] = {
                    "selected_events": score["selected_events"],
                    "selection_signature_sha256": score["selection_signature_sha256"],
                    "net10_per_source_opportunity": score["net10_per_source_opportunity"],
                    "net20_per_source_opportunity": score["net20_per_source_opportunity"],
                }
                target.write(json.dumps({**candidate, **score}, ensure_ascii=False, allow_nan=False) + "\n")
        development_meta[label]["detail_sha256"] = _sha256(detail_path)
        development_meta[label]["unique_selection_masks"] = sum(len(masks[(family, side)].score_cache) for family in FAMILIES for side in SIDES)
        development_index[label] = index
        print(json.dumps({"window": label, "role": "development", "proposals": len(proposals), "formulas": len(index), "unique_selection_masks": development_meta[label]["unique_selection_masks"]}, ensure_ascii=False), flush=True)
        del proposals, outcomes, masks

    development_selection_sets = set()
    ranking = []
    for candidate in candidates:
        sid = candidate["strategy_id"]
        dev = [development_index[label][sid] for label, _, _ in DEVELOPMENT]
        signatures = tuple(result["selection_signature_sha256"] for result in dev)
        development_selection_sets.add(signatures)
        eligible = all(result["selected_events"] >= MINIMUM_SELECTED_PER_WINDOW for result in dev)
        net10 = [result["net10_per_source_opportunity"] for result in dev]
        net20 = [result["net20_per_source_opportunity"] for result in dev]
        passed = eligible and all(value > 0 for value in net10 + net20)
        ranking.append({
            "strategy_id": sid, "screen_eligible": eligible,
            "worst_development_net_per_opportunity_20bps": min(net20) if eligible else None,
            "development_gate_passed": passed,
        })
    ranking.sort(key=lambda row: (
        row["screen_eligible"],
        row["worst_development_net_per_opportunity_20bps"] if row["screen_eligible"] else float("-inf"),
    ), reverse=True)
    family_leaders: dict[str, dict[str, Any]] = {}
    for row in ranking:
        if not row["screen_eligible"]:
            continue
        family = candidate_by_id[row["strategy_id"]]["family"]
        current = family_leaders.get(family)
        if current is None or row["worst_development_net_per_opportunity_20bps"] > current["worst_development_net_per_opportunity_20bps"]:
            family_leaders[family] = row

    report: dict[str, Any] = {
        "artifact_version": "one_hundred_thousand_universal_entry_formulas_v2",
        "scorer_sha256": _sha256(Path(__file__)), "candidate_grid_sha256": _sha256(candidate_path),
        "candidate_count": len(candidates), "distinct_development_selection_sets": len(development_selection_sets),
        "eligible_candidate_count": sum(row["screen_eligible"] for row in ranking),
        "development_gate_pass_count": sum(row["development_gate_passed"] for row in ranking),
        "winner_claim_allowed": False, "development_winner": None,
        "contract": {
            "grid": "5 families x 2 sides x 10 trigger margins x 10 primary confirmation modes x 10 primary thresholds x 10 secondary confirmation modes with strictest fixed secondary threshold = 100,000 formula specifications",
            "secondary_confirmation": "one additional conjunctive filter is always applied; no single-confirmation formulas from earlier 1k/10k/100k screens are repeated by specification",
            "costs": "5bps fee/fill; 10/20bps slippage/fill",
            "volume_confirmation": "current closed signal-candle volume divided by mean of 20 immediately preceding closed hourly bars; the signal candle is excluded",
            "volume_data_gap_policy": "fail closed if the source candle or any preceding 20 hourly volume bars are missing or non-contiguous",
            "lifecycle": "1.5 ATR initial stop, else four-hour full close; existing frozen minimum-lifecycle outcomes",
            "ranking_metric": "net per original family-side opportunity, abstentions zero",
            "development_gate": "at least 100 selected in each development window and positive net per source opportunity at 10 and 20bps in both",
            "inference": "exploratory multiple search; no per-candidate confidence intervals or family-wise correction",
            "exploratory_july": "already explored; only top development candidates are scored, never treated as fresh confirmation",
        },
        "candidate_grid_path": str(candidate_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "development_windows": development_meta,
        "development_ranking": ranking,
        "development_top_1000": [
            {**candidate_by_id[row["strategy_id"]], **row} for row in ranking[:1000]
        ],
        "best_development_candidate_by_family": {
            family: {**candidate_by_id[row["strategy_id"]], **row}
            for family, row in sorted(family_leaders.items())
        },
        "exploratory_top_count": args.exploratory_top_count,
        "exploratory_windows": {},
        "limitations": [
            "The 100,000-formula search is not 100,000 independent experiments; effective distinct selection sets are counted.",
            "Development inputs have been previously viewed and serve as a screening set, not pristine inferential evidence.",
            "July windows are previously explored and cannot confirm a selected formula.",
            "The grid reuses existing proposal families and feature snapshots; it cannot repair candidate-generation blind spots.",
            "No common-capital portfolio, exchange filters, actual fills, venue-specific fees, or settled funding is modelled.",
            "No runtime or Staging/Production changes are made.",
        ],
    }
    passing = next((row for row in ranking if row["development_gate_passed"]), None)
    report["development_winner"] = passing["strategy_id"] if passing else None

    if args.exploratory_top_count:
        top_ids = {row["strategy_id"] for row in ranking[:args.exploratory_top_count]}
        top_ids.update(row["strategy_id"] for row in family_leaders.values())
        top_candidates = [candidate for candidate in candidates if candidate["strategy_id"] in top_ids]
        report["exploratory_candidate_ids"] = sorted(top_ids)
        for label, source, outcomes_path in EXPLORED:
            proposals, outcomes, hashes = _read_window(source, outcomes_path, "explored_validation")
            masks = prepare_window(proposals, outcomes)
            detail_path = output_dir / f"{label}_top{args.exploratory_top_count}_plus_family_leaders.jsonl"
            with detail_path.open("w", encoding="utf-8", newline="\n") as target:
                for candidate in top_candidates:
                    score = masks[(candidate["family"], candidate["side"])].score(candidate)
                    target.write(json.dumps({**candidate, **score}, ensure_ascii=False, allow_nan=False) + "\n")
            report["exploratory_windows"][label] = {
                "role": "explored_validation", "proposal_count": len(proposals), **hashes,
                "formula_count": len(top_candidates), "detail_sha256": _sha256(detail_path),
            }
            print(json.dumps({"window": label, "role": "explored_validation", "formulas": len(top_candidates)}), flush=True)
            del proposals, outcomes, masks

    report_path = output_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "report": str(report_path), "candidate_count": report["candidate_count"],
        "distinct_development_selection_sets": report["distinct_development_selection_sets"],
        "eligible_candidate_count": report["eligible_candidate_count"],
        "development_gate_pass_count": report["development_gate_pass_count"],
        "development_winner": report["development_winner"],
        "top_development": ranking[:10],
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
