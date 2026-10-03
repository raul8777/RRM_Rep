"""Screen 100,000 novel market-regime entry formulas using local closed UM candles only."""
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
from Dev.scripts.screen_100000_universal_entry_formulas import (
    DEVELOPMENT, EXPLORED, FAMILIES, MINIMUM_SELECTED_PER_WINDOW, ROOT, SIDES,
    _read_window, signal_margin, trigger_step,
)
from simulation.mimic_feed import HistoricalDataFeed


TRIGGER_LEVELS = tuple(range(10))
MACRO_MODES = (
    "btc_4h_direction", "btc_24h_direction", "market_4h_breadth",
    "market_24h_breadth", "market_4h_dispersion", "market_24h_dispersion",
    "btc_atr_pct", "market_median_atr_pct", "market_volatility_breadth",
    "market_volume_breadth",
)
MACRO_LEVELS = {
    "btc_4h_direction": (0.0, .001, .002, .003, .005, .0075, .01, .015, .02, .03),
    "btc_24h_direction": (0.0, .0025, .005, .0075, .01, .015, .02, .03, .04, .05),
    "market_4h_breadth": (.50, .55, .60, .65, .70, .75, .80, .85, .90, .95),
    "market_24h_breadth": (.50, .55, .60, .65, .70, .75, .80, .85, .90, .95),
    "market_4h_dispersion": (.001, .002, .003, .004, .005, .0075, .01, .015, .02, .03),
    "market_24h_dispersion": (.005, .01, .015, .02, .03, .04, .05, .075, .10, .15),
    "btc_atr_pct": (.10, .15, .20, .25, .30, .40, .50, .75, 1.0, 1.5),
    "market_median_atr_pct": (.10, .15, .20, .25, .30, .40, .50, .75, 1.0, 1.5),
    "market_volatility_breadth": (.05, .10, .15, .20, .25, .30, .40, .50, .60, .75),
    "market_volume_breadth": (.05, .10, .15, .20, .25, .30, .40, .50, .60, .75),
}
COSTS = (10, 20)
PRIOR_100K = ROOT / "one_hundred_thousand_formula_screen_v2_prior_volume_final"
NORMALIZED_BY_SOURCE = {
    "expanded_range_v1/cal_range_scalp_2_20250715_20250729": "pilot_range2_28",
    "expanded_range_v1/cal_range_scalp_3_20250909_20250923": "pilot_range3_28",
    "expanded_range_oos_202607/cal_range_scalp_oos_20260701_20260716/baseline": "pilot_range_oos_27",
    "expanded_range_oos_20260717/cal_range_scalp_oos_20260717_20260801/baseline": "pilot_range_oos_20260717_27",
}


def build_candidates() -> list[dict[str, Any]]:
    candidates = []
    for family, side, trigger_index, mode_1, level_index, mode_2 in itertools.product(
        FAMILIES, SIDES, TRIGGER_LEVELS, MACRO_MODES, range(10), MACRO_MODES,
    ):
        level = MACRO_LEVELS[mode_1][level_index]
        level_2 = MACRO_LEVELS[mode_2][-1]
        candidates.append({
            "strategy_id": f"R{len(candidates) + 1:06d}",
            "family": family, "side": side,
            "trigger_level_index": trigger_index,
            "trigger_margin_min": trigger_index * trigger_step(family),
            "market_regime_1": mode_1, "regime_1_level_index": level_index,
            "regime_1_level": level,
            "regime_1_definition": f"{mode_1} threshold={level:g}; measured from closed UM candles before entry",
            "market_regime_2": mode_2, "regime_2_level_index": 9,
            "regime_2_level": level_2,
            "regime_2_definition": f"{mode_2} strictest grid level={level_2:g}; measured from closed UM candles before entry",
        })
    if len(candidates) != 100000 or len({candidate["strategy_id"] for candidate in candidates}) != 100000:
        raise AssertionError("market-regime grid must contain exactly 100,000 unique specifications")
    return candidates


def _true_range(bar: dict[str, Any], previous_close: float) -> float:
    high, low = float(bar["high"]), float(bar["low"])
    return max(high - low, abs(high - previous_close), abs(low - previous_close))


def _market_contexts(hourly: dict[str, list[dict[str, Any]]], decision_times: set[int]) -> dict[int, dict[str, Any]]:
    series = {}
    for symbol, bars in hourly.items():
        ordered = sorted(bars, key=lambda bar: int(bar["open_time"]))
        times = [int(round(float(bar["open_time"]))) for bar in ordered]
        closes = [float(bar["close"]) for bar in ordered]
        volumes = [float(bar.get("volume", 0.0)) for bar in ordered]
        ranges = [0.0] + [_true_range(ordered[i], closes[i - 1]) for i in range(1, len(ordered))]
        series[symbol] = (ordered, times, closes, volumes, ranges)
    if "BTCUSDT" not in series:
        raise ValueError("UM BTC hourly series required for market regimes")

    contexts = {}
    for decision in sorted(decision_times):
        source_open = decision - 3600
        by_symbol = {}
        for symbol, (bars, times, closes, volumes, ranges) in series.items():
            index = bisect.bisect_left(times, source_open)
            if index >= len(times) or times[index] != source_open:
                continue
            if index < 24 or any(times[j] - times[j - 1] != 3600 for j in range(index - 23, index + 1)):
                continue
            if closes[index - 4] <= 0 or closes[index - 24] <= 0 or closes[index] <= 0:
                continue
            previous_atr = statistics.mean(ranges[index - 24:index])
            current_atr = statistics.mean(ranges[index - 23:index + 1])
            previous_volume = statistics.mean(volumes[index - 20:index]) if index >= 20 else 0.0
            by_symbol[symbol] = {
                "return_4h": closes[index] / closes[index - 4] - 1.0,
                "return_24h": closes[index] / closes[index - 24] - 1.0,
                "atr_pct": 100.0 * current_atr / closes[index],
                "true_range_ratio": ranges[index] / previous_atr if previous_atr > 0 else 0.0,
                "volume_ratio_prior20": volumes[index] / previous_volume if previous_volume > 0 else None,
            }
        if "BTCUSDT" not in by_symbol or len(by_symbol) < 20:
            raise ValueError(f"incomplete UM context at decision={decision}: symbols={len(by_symbol)}")
        returns4 = [row["return_4h"] for row in by_symbol.values()]
        returns24 = [row["return_24h"] for row in by_symbol.values()]
        volume_ratios = [row["volume_ratio_prior20"] for row in by_symbol.values() if row["volume_ratio_prior20"] is not None]
        contexts[decision] = {
            "btc_return_4h": by_symbol["BTCUSDT"]["return_4h"],
            "btc_return_24h": by_symbol["BTCUSDT"]["return_24h"],
            "market_breadth_long_4h": sum(value > 0 for value in returns4) / len(returns4),
            "market_breadth_long_24h": sum(value > 0 for value in returns24) / len(returns24),
            "market_dispersion_4h": statistics.pstdev(returns4),
            "market_dispersion_24h": statistics.pstdev(returns24),
            "btc_atr_pct": by_symbol["BTCUSDT"]["atr_pct"],
            "market_median_atr_pct": statistics.median(row["atr_pct"] for row in by_symbol.values()),
            "market_volatility_breadth": sum(row["true_range_ratio"] >= 2.0 for row in by_symbol.values()) / len(by_symbol),
            "market_volume_breadth": sum(value >= 1.5 for value in volume_ratios) / len(volume_ratios) if volume_ratios else 0.0,
            "btc_volume_ratio_prior20": by_symbol["BTCUSDT"]["volume_ratio_prior20"],
            "context_symbol_count": len(by_symbol),
            "btc_source_open_time": source_open,
        }
    return contexts


def _regime_passes(candidate: dict[str, Any], context: dict[str, Any]) -> bool:
    mode = candidate["market_regime"]
    level = candidate["regime_level"]
    side = candidate["side"]
    aligned = lambda value: value >= 0 if side == "LONG" else value <= 0
    if mode == "btc_4h_direction":
        value = context["btc_return_4h"]
        return aligned(value) and abs(value) >= level
    if mode == "btc_24h_direction":
        value = context["btc_return_24h"]
        return aligned(value) and abs(value) >= level
    if mode == "market_4h_breadth":
        bullish = context["market_breadth_long_4h"]
        return bullish >= level if side == "LONG" else 1.0 - bullish >= level
    if mode == "market_24h_breadth":
        bullish = context["market_breadth_long_24h"]
        return bullish >= level if side == "LONG" else 1.0 - bullish >= level
    if mode == "market_4h_dispersion":
        return context["market_dispersion_4h"] >= level
    if mode == "market_24h_dispersion":
        return context["market_dispersion_24h"] >= level
    return context[mode] >= level


def _load_previous_selection_signatures() -> tuple[dict[str, dict[tuple[str, str], set[str]]], dict[tuple[str, str], set[tuple[str, str]]]]:
    signatures_by_window: dict[str, dict[tuple[str, str], set[str]]] = defaultdict(lambda: defaultdict(set))
    paired_signatures: dict[tuple[str, str], dict[str, dict[str, str]]] = defaultdict(dict)
    for label, _, _ in DEVELOPMENT:
        path = PRIOR_100K / f"{label}.jsonl"
        if not path.is_file():
            raise FileNotFoundError(f"corrected previous 100k formula detail missing: {path}")
        for row in _rows(path):
            key = (row["family"], row["side"])
            signature = row["selection_signature_sha256"]
            signatures_by_window[label][key].add(signature)
            paired_signatures[key].setdefault(row["strategy_id"], {})[label] = signature
    paired_sets = {
        key: {
            (signatures[DEVELOPMENT[0][0]], signatures[DEVELOPMENT[1][0]])
            for signatures in by_strategy.values()
            if all(label in signatures for label, *_ in DEVELOPMENT)
        }
        for key, by_strategy in paired_signatures.items()
    }
    return signatures_by_window, paired_sets


class FamilySideRegimeMasks:
    def __init__(self, proposals: list[dict[str, Any]], outcomes: dict[str, dict[str, Any]], contexts: dict[int, dict[str, Any]], family: str, side: str):
        self.proposals = [row for row in proposals if row["philosophy_family"] == family and row["side"] == side]
        self.family, self.side = family, side
        self.count = len(self.proposals)
        self.outcomes = [outcomes[row["opportunity_event_id"]] for row in self.proposals]
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
        self.regime_masks = {}
        for mode in MACRO_MODES:
            for level_index, level in enumerate(MACRO_LEVELS[mode]):
                mask = 0
                for index, proposal in enumerate(self.proposals):
                    context = contexts.get(int(round(float(proposal["decision_time"]))))
                    if context is None:
                        raise ValueError("market regime context missing for a proposal")
                    candidate = {"market_regime": mode, "regime_level": level, "side": side}
                    if _regime_passes(candidate, context):
                        mask |= 1 << index
                self.regime_masks[(mode, level_index)] = mask
        if len(self.regime_masks) != len(MACRO_MODES) * 10:
            raise AssertionError("each market-regime mode must have ten threshold masks")
        self.regime_max_masks = {mode: self.regime_masks[(mode, 9)] for mode in MACRO_MODES}
        self.cache = {}
        self.signature_cache = {}

    def score(self, candidate: dict[str, Any]) -> dict[str, Any]:
        mask = (
            self.trigger_masks[candidate["trigger_level_index"]]
            & self.regime_masks[(candidate["market_regime_1"], candidate["regime_1_level_index"])]
            & self.regime_max_masks[candidate["market_regime_2"]]
        )
        if mask not in self.cache:
            indices = []
            active = mask
            while active:
                bit = active & -active
                indices.append(bit.bit_length() - 1)
                active ^= bit
            scores = {}
            for bps in COSTS:
                selected = [self.baseline[bps][index] for index in indices]
                gains = sum(max(0.0, value) for value in selected)
                losses = -sum(min(0.0, value) for value in selected)
                scores[str(bps)] = {
                    "net_per_family_opportunity": sum(selected) / self.count if self.count else None,
                    "selected_mean_net": statistics.mean(selected) if selected else None,
                    "selected_pf": gains / losses if losses else None,
                }
            self.cache[mask] = {"selected_events": len(indices), "coverage_pct": 100.0 * len(indices) / self.count if self.count else 0.0, "net10": scores["10"]["net_per_family_opportunity"], "net20": scores["20"]["net_per_family_opportunity"], "scores": scores}
            encoded = (self.family + "|" + self.side + "|").encode() + mask.to_bytes(max(1, (self.count + 7) // 8), "little")
            self.signature_cache[mask] = hashlib.sha256(encoded).hexdigest()
        return {**candidate, **self.cache[mask], "selection_signature_sha256": self.signature_cache[mask]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "market_regime_100k_screen_v1")
    parser.add_argument("--exploratory-top-count", type=int, default=1000)
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        parser.error("output exists; use a new versioned directory")
    output_dir.mkdir(parents=True)
    if not 0 <= args.exploratory_top_count <= 10000:
        parser.error("exploratory top count must be 0..10000")
    candidates = build_candidates()
    previous_window_signatures, previous_paired_sets = _load_previous_selection_signatures()
    candidate_grid = output_dir / "candidate_grid.jsonl"
    candidate_grid.write_text("".join(json.dumps(candidate, allow_nan=False) + "\n" for candidate in candidates), encoding="utf-8")
    report: dict[str, Any] = {
        "artifact_version": "market_regime_100k_screen_v1",
        "scorer_sha256": _sha256(Path(__file__)), "candidate_grid_sha256": _sha256(candidate_grid),
        "candidate_count": len(candidates), "winner_claim_allowed": False, "development_winner": None,
        "contract": {
            "grid": "five proposal families x two sides x ten trigger margins x ten primary cross-market regimes x ten primary thresholds x ten secondary regimes with strictest level = 100,000",
            "context_source": "local UM hourly OHLCV only; decision uses last closed hour; no CoinDesk/API calls",
            "volume": "UM signal-close volume / prior 20 closed UM hourly bars; signal candle excluded",
            "lifecycle": "existing corrected volume 1.5ATR stop-or-four-hour outcomes",
            "costs": "5bps fee/fill, 10/20bps slippage/fill",
            "ranking": "worst development net per original family-side proposal, abstentions zero",
            "repeat_policy": "flag and exclude from novel eligible gate any candidate whose paired development selection masks match an earlier corrected 100k mask pair",
            "exploratory": "July windows already explored; diagnostic only",
        },
        "windows": {}, "limitations": [
            "Macro regime gates are a fixed research grid and not tested runtime policy.",
            "No external data source, funding feed, order-book data, or fresh validation window is accessed.",
            "Candidates are not independent; threshold search creates multiple-testing bias and no candidate-level CI is computed.",
            "No portfolio-level capital constraint, actual exchange fills, or active staging changes are simulated.",
        ],
    }
    dev_scores: dict[str, dict[str, dict[str, Any]]] = {}
    dev_meta = {}
    for label, source_rel, outcomes_rel in DEVELOPMENT:
        proposals, outcomes, hashes = _read_window(source_rel, outcomes_rel, "development")
        normalized = ROOT / "normalized" / NORMALIZED_BY_SOURCE[source_rel] / "um"
        symbols = sorted({row["symbol"] for row in proposals} | {"BTCUSDT"})
        feed = HistoricalDataFeed(str(normalized))
        hourly, input_hashes = {}, {}
        for symbol in symbols:
            manifest_path = normalized / f"{symbol}_1m.manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("venue") != "um" or manifest.get("interval") != "1m":
                raise ValueError(f"UM macro source manifest mismatch: {symbol}")
            hourly[symbol] = sorted(feed.get_klines(symbol, "1h"), key=lambda bar: int(bar["open_time"]))
            if len(hourly[symbol]) != int(manifest["rows"]) // 60:
                raise ValueError(f"UM macro history incomplete: {symbol}")
            input_hashes[symbol] = {"manifest": _sha256(manifest_path), "parquet": _sha256(normalized / f"{symbol}_1m.parquet")}
        contexts = _market_contexts(hourly, {int(row["decision_time"]) for row in proposals})
        if len(contexts) != len({int(row["decision_time"]) for row in proposals}):
            raise ValueError("some proposals lack a closed market-regime context")
        masks = {(family, side): FamilySideRegimeMasks(proposals, outcomes, contexts, family, side) for family in FAMILIES for side in SIDES}
        prior_ids = defaultdict(set)
        prior_signatures = previous_window_signatures[label]
        for candidate in candidates:
            family_side = (candidate["family"], candidate["side"])
            sig = masks[family_side].score(candidate)["selection_signature_sha256"]
            if sig in prior_signatures.get(family_side, set()):
                prior_ids[candidate["strategy_id"]].add(label)
        index, repeated_any = {}, set()
        detail = output_dir / f"{label}.jsonl"
        with detail.open("w", encoding="utf-8", newline="\n") as target:
            for candidate in candidates:
                family_side = (candidate["family"], candidate["side"])
                score = masks[family_side].score(candidate)
                repeated = label in prior_ids[candidate["strategy_id"]]
                if repeated:
                    repeated_any.add(candidate["strategy_id"])
                row = {**candidate, **score, "selection_repeats_prior_100k_in_this_window": repeated}
                index[candidate["strategy_id"]] = row
                target.write(json.dumps(row, allow_nan=False) + "\n")
        dev_scores[label] = index
        dev_meta[label] = {"role": "development", "proposal_count": len(proposals), **hashes, "um_context_source_hashes": input_hashes, "detail_sha256": _sha256(detail), "unique_masks_by_family_side": {f"{fam}|{side}": len(masks[(fam, side)].cache) for fam in FAMILIES for side in SIDES}}
        print(json.dumps({"window": label, "role": "development", "proposals": len(proposals), "formulas": len(index), "prior_mask_overlaps": len(repeated_any)}), flush=True)
        del feed, proposals, outcomes, contexts, hourly, masks

    # The exact-pair duplicate test is against a per-strategy pair from the earlier corrected 100k grid.
    prior_pairs: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    old_by_window = {}
    for label, _, _ in DEVELOPMENT:
        old_by_window[label] = {}
        old_path = PRIOR_100K / f"{label}.jsonl"
        for row in _rows(old_path):
            old_by_window[label][row["strategy_id"]] = ((row["family"], row["side"]), row["selection_signature_sha256"])
    jul_label, sep_label = DEVELOPMENT[0][0], DEVELOPMENT[1][0]
    for sid, (fam_side, sig) in old_by_window[jul_label].items():
        second = old_by_window[sep_label].get(sid)
        if second and second[0] == fam_side:
            prior_pairs[fam_side].add((sig, second[1]))

    ranking = []
    signatures_by_candidate = {}
    for candidate in candidates:
        sid = candidate["strategy_id"]
        dev_rows = [dev_scores[label][sid] for label, *_ in DEVELOPMENT]
        sig_pair = tuple(row["selection_signature_sha256"] for row in dev_rows)
        signatures_by_candidate[sid] = sig_pair
        eligible = all(row["selected_events"] >= MINIMUM_SELECTED_PER_WINDOW for row in dev_rows)
        exact_prior_pair_repeat = sig_pair in prior_pairs[(candidate["family"], candidate["side"])]
        novel = not exact_prior_pair_repeat
        net10 = [row["net10"] for row in dev_rows]
        net20 = [row["net20"] for row in dev_rows]
        passed = eligible and novel and all(value > 0 for value in net10 + net20)
        ranking.append({
            "strategy_id": sid, "screen_eligible": eligible, "novel_vs_prior_corrected_100k": novel,
            "exact_prior_paired_selection_repeat": exact_prior_pair_repeat,
            "worst_development_net_per_opportunity_20bps": min(net20) if eligible else None,
            "development_gate_passed": passed,
        })
    unique_new_selection_sets = len(set(signatures_by_candidate.values()))
    ranking.sort(key=lambda row: (
        row["screen_eligible"] and row["novel_vs_prior_corrected_100k"],
        row["worst_development_net_per_opportunity_20bps"] if row["screen_eligible"] else float("-inf"),
    ), reverse=True)
    report["development_windows"] = dev_meta
    report["distinct_development_selection_sets"] = unique_new_selection_sets
    report["overlap_with_prior_corrected_100k"] = {
        "same_paired_selection_signatures": sum(row["exact_prior_paired_selection_repeat"] for row in ranking),
        "excluded_from_development_gate": sum(not row["novel_vs_prior_corrected_100k"] for row in ranking),
        "prior_screen_path": str(PRIOR_100K.relative_to(REPO_ROOT)).replace("\\", "/"),
    }
    report["development_ranking_top_1000"] = ranking[:1000]
    report["development_gate_pass_count"] = sum(row["development_gate_passed"] for row in ranking)
    report["exploratory_windows"] = {}
    passing = next((row for row in ranking if row["development_gate_passed"]), None)
    report["development_winner"] = passing["strategy_id"] if passing else None
    print(json.dumps({"development_unique_sets": unique_new_selection_sets, "exact_prior_pair_repeats": report["overlap_with_prior_corrected_100k"]["same_paired_selection_signatures"], "gate_passes": report["development_gate_pass_count"]}), flush=True)

    top_ids = {row["strategy_id"] for row in ranking[:args.exploratory_top_count]}
    family_leaders = {}
    for row in ranking:
        if not row["screen_eligible"] or not row["novel_vs_prior_corrected_100k"]:
            continue
        family = next(candidate["family"] for candidate in candidates if candidate["strategy_id"] == row["strategy_id"])
        if family not in family_leaders or row["worst_development_net_per_opportunity_20bps"] > family_leaders[family]["worst_development_net_per_opportunity_20bps"]:
            family_leaders[family] = row
    top_ids.update(row["strategy_id"] for row in family_leaders.values())
    report["exploratory_candidate_ids"] = sorted(top_ids)
    candidate_by_id = {candidate["strategy_id"]: candidate for candidate in candidates}
    for label, source_rel, outcomes_rel in EXPLORED:
        proposals, outcomes, hashes = _read_window(source_rel, outcomes_rel, "explored_validation")
        normalized = ROOT / "normalized" / NORMALIZED_BY_SOURCE[source_rel] / "um"
        symbols = sorted({row["symbol"] for row in proposals} | {"BTCUSDT"})
        feed = HistoricalDataFeed(str(normalized))
        hourly, input_hashes = {}, {}
        for symbol in symbols:
            manifest_path = normalized / f"{symbol}_1m.manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            hourly[symbol] = sorted(feed.get_klines(symbol, "1h"), key=lambda bar: int(bar["open_time"]))
            input_hashes[symbol] = {"manifest": _sha256(manifest_path), "parquet": _sha256(normalized / f"{symbol}_1m.parquet")}
        contexts = _market_contexts(hourly, {int(row["decision_time"]) for row in proposals})
        masks = {(family, side): FamilySideRegimeMasks(proposals, outcomes, contexts, family, side) for family in FAMILIES for side in SIDES}
        detail_path = output_dir / f"{label}_top{args.exploratory_top_count}_plus_family_leaders.jsonl"
        with detail_path.open("w", encoding="utf-8", newline="\n") as target:
            for sid in sorted(top_ids):
                candidate = candidate_by_id[sid]
                target.write(json.dumps(masks[(candidate["family"], candidate["side"])].score(candidate), allow_nan=False) + "\n")
        report["exploratory_windows"][label] = {"role": "explored_validation", "proposal_count": len(proposals), **hashes, "um_context_source_hashes": input_hashes, "formula_count": len(top_ids), "detail_sha256": _sha256(detail_path)}
        print(json.dumps({"exploratory_window": label, "formulas": len(top_ids)}), flush=True)
        del feed, proposals, outcomes, contexts, hourly, masks

    grid_relative = str(candidate_grid.relative_to(REPO_ROOT)).replace("\\", "/")
    report["candidate_grid_path"] = grid_relative
    report["candidate_grid_sha256"] = _sha256(candidate_grid)
    (output_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(output_dir / 'report.json'), "candidates": len(candidates), "winner": report["development_winner"], "gate_passes": report["development_gate_pass_count"], "exact_previous_screen_pair_repeats": report["overlap_with_prior_corrected_100k"]["same_paired_selection_signatures"]}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())