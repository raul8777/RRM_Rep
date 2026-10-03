"""Compare Spot-vs-UM proposal generation and execution on matched development tapes."""
from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
from collections import defaultdict
from itertools import product
from pathlib import Path
from typing import Any

from Dev.scripts.analyze_universal_entry_stop_survival import (
    REPO_ROOT, _sha256, replay_entry_stop_only, score_minimal_lifecycle,
)
from Dev.scripts.score_universal_entry_minimal_lifecycle import cluster_mean_summary
from entry_philosophies import (
    CROSS_SECTIONAL_LOOKBACK_BARS,
    CROSS_SECTIONAL_Z_THRESHOLD,
    LOOKBACK_BARS,
    MEAN_REVERSION_Z_THRESHOLD,
    MOMENTUM_THRESHOLD,
    VOLATILITY_DIRECTIONAL_BODY_MIN,
    VOLATILITY_EXPANSION_RATIO,
    generate_cross_sectional_relative_strength_proposals,
    generate_pilot_proposals,
)
from simulation.mimic_feed import HistoricalDataFeed


ROOT = REPO_ROOT / "Dev/benchmarks/universal_entry_selection"
WINDOWS = (
    ("cal_range_scalp_2_20250715_20250729", "pilot_range2_28"),
    ("cal_range_scalp_3_20250909_20250923", "pilot_range3_28"),
)
VENUES = ("spot", "um")
FAMILIES = ("mean_reversion", "time_series_momentum", "breakout_expansion", "volatility_expansion", "cross_sectional_relative_strength")
STOP_ATR = 1.5
HORIZON_MINUTES = 240
FEE_BPS_PER_FILL = 5.0
RISK_BUDGET_USDT = 18.0
NOTIONAL_CAP_USDT = 100.0


def event_identity(proposal: dict[str, Any]) -> tuple[str, str, str, int, str]:
    return (
        str(proposal["symbol"]).upper(), str(proposal["philosophy_family"]),
        str(proposal["philosophy_variant"]), int(round(float(proposal["decision_time"]))),
        str(proposal["side"]).upper(),
    )


def _exclusive_trigger_margin(proposal: dict[str, Any]) -> tuple[str, float]:
    family = str(proposal["philosophy_family"])
    features = proposal["feature_snapshot"]
    if family == "mean_reversion":
        return "absolute_zscore_excess_over_2", abs(float(features["zscore_24h"])) - 2.0
    if family == "time_series_momentum":
        return "absolute_return_4h_excess_over_0_5pct_before_novelty_gate", abs(float(features["return_4h"])) - 0.005
    if family == "breakout_expansion":
        boundary = float(features["prior_24h_high"] if proposal["side"] == "LONG" else features["prior_24h_low"])
        close = float(features["close"])
        atr = float(features["atr_24h"])
        distance = (close - boundary if proposal["side"] == "LONG" else boundary - close) / atr
        return "signed_break_distance_in_generator_atr", distance
    if family == "volatility_expansion":
        return "true_range_ratio_excess_over_2_before_body_gate", float(features["true_range_expansion_ratio"]) - 2.0
    if family == "cross_sectional_relative_strength":
        return "absolute_relative_zscore_excess_over_0_5", abs(float(features["relative_strength_zscore_24h"])) - 0.5
    raise ValueError(f"unknown proposal family: {family}")


def _summarize_exclusive_margins(proposals: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    for proposal in proposals:
        metric, margin = _exclusive_trigger_margin(proposal)
        grouped[(str(proposal["philosophy_family"]), metric)].append(margin)
    result = {}
    for (family, metric), values in sorted(grouped.items()):
        ordered = sorted(values)
        result[f"{family}|{metric}"] = {
            "event_count": len(ordered),
            "p10": ordered[int(0.10 * (len(ordered) - 1))],
            "median": statistics.median(ordered),
            "p90": ordered[int(0.90 * (len(ordered) - 1))],
        }
    return result


def _mean_std(values: list[float]) -> tuple[float, float]:
    mean = statistics.mean(values)
    return mean, math.sqrt(statistics.mean((value - mean) ** 2 for value in values))


def _build_observation_features(
    bars_by_symbol: dict[str, list[dict[str, Any]]], *, window_start: int, window_end: int,
) -> dict[tuple[str, int], dict[str, Any]]:
    observations: dict[tuple[str, int], dict[str, Any]] = {}
    cross_returns: dict[int, list[tuple[str, float]]] = defaultdict(list)
    for symbol, bars in bars_by_symbol.items():
        ordered = sorted(bars, key=lambda bar: float(bar["open_time"]))
        times = [int(round(float(bar["open_time"]))) for bar in ordered]
        closes = [float(bar["close"]) for bar in ordered]
        true_ranges = [0.0]
        for index in range(1, len(ordered)):
            previous_close = closes[index - 1]
            high, low = float(ordered[index]["high"]), float(ordered[index]["low"])
            true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        for index in range(CROSS_SECTIONAL_LOOKBACK_BARS, len(ordered) - 1):
            decision_time = times[index] + 3600
            entry_time = times[index + 1]
            if entry_time != decision_time or not window_start <= decision_time < window_end:
                continue
            if times[index] - times[index - CROSS_SECTIONAL_LOOKBACK_BARS] != CROSS_SECTIONAL_LOOKBACK_BARS * 3600:
                continue
            if times[-1] + 3600 < entry_time + HORIZON_MINUTES * 60:
                continue
            previous_close_24h = closes[index - CROSS_SECTIONAL_LOOKBACK_BARS]
            close = closes[index]
            if previous_close_24h <= 0 or close <= 0:
                continue
            return_24h = close / previous_close_24h - 1.0
            cross_returns[decision_time].append((symbol, return_24h))

            rolling = closes[index - LOOKBACK_BARS + 1:index + 1]
            rolling_mean, rolling_std = _mean_std(rolling)
            previous_high = max(float(bar["high"]) for bar in ordered[index - LOOKBACK_BARS:index])
            previous_low = min(float(bar["low"]) for bar in ordered[index - LOOKBACK_BARS:index])
            previous_atr = statistics.mean(true_ranges[index - LOOKBACK_BARS:index])
            body_return = (float(ordered[index]["close"]) - float(ordered[index]["open"])) / close
            volatility_ratio = true_ranges[index] / previous_atr if previous_atr > 0 else 0.0
            previous_returns = [
                closes[position] / closes[position - 4] - 1.0
                for position in range(max(4, index - LOOKBACK_BARS + 1), index)
                if closes[position - 4] > 0
            ]
            observations[(symbol, decision_time)] = {
                "source_close": close,
                "return_4h": close / closes[index - 4] - 1.0 if closes[index - 4] > 0 else 0.0,
                "previous_returns": previous_returns,
                "zscore_24h": (close - rolling_mean) / rolling_std if rolling_std > 0 else 0.0,
                "prior_24h_high": previous_high,
                "prior_24h_low": previous_low,
                "atr_24h": previous_atr,
                "true_range_expansion_ratio": volatility_ratio,
                "body_return": body_return,
                "return_24h": return_24h,
                "pilot_generator_eligible": index >= LOOKBACK_BARS + 1,
            }

    for decision_time, rows in cross_returns.items():
        if len(rows) < 5:
            continue
        returns = [value for _, value in rows]
        mean_return, std_return = _mean_std(returns)
        if std_return <= 0:
            continue
        for symbol, return_24h in rows:
            key = (symbol, decision_time)
            if key in observations:
                observations[key]["relative_strength_zscore_24h"] = (return_24h - mean_return) / std_return
                observations[key]["cross_section_size"] = len(rows)
    return observations


def _classify_observation(features: dict[str, Any]) -> dict[tuple[str, str], str]:
    signals: dict[tuple[str, str], str] = {}
    if features["pilot_generator_eligible"]:
        zscore = float(features["zscore_24h"])
        if zscore <= -MEAN_REVERSION_Z_THRESHOLD:
            signals[("mean_reversion", "zscore_24h_2")] = "LONG"
        elif zscore >= MEAN_REVERSION_Z_THRESHOLD:
            signals[("mean_reversion", "zscore_24h_2")] = "SHORT"

        momentum_return = float(features["return_4h"])
        previous_returns = features["previous_returns"]
        if momentum_return >= MOMENTUM_THRESHOLD and (not previous_returns or max(previous_returns) < MOMENTUM_THRESHOLD):
            signals[("time_series_momentum", "return_4h_0_5pct")] = "LONG"
        elif momentum_return <= -MOMENTUM_THRESHOLD and (not previous_returns or min(previous_returns) > -MOMENTUM_THRESHOLD):
            signals[("time_series_momentum", "return_4h_0_5pct")] = "SHORT"

        close = float(features["source_close"])
        if close > float(features["prior_24h_high"]):
            signals[("breakout_expansion", "prior_24h_high_low_break")] = "LONG"
        elif close < float(features["prior_24h_low"]):
            signals[("breakout_expansion", "prior_24h_high_low_break")] = "SHORT"

        if (float(features["true_range_expansion_ratio"]) >= VOLATILITY_EXPANSION_RATIO
                and abs(float(features["body_return"])) >= VOLATILITY_DIRECTIONAL_BODY_MIN):
            signals[("volatility_expansion", "true_range_24h_2x")] = (
                "LONG" if float(features["body_return"]) > 0 else "SHORT"
            )

    relative_zscore = features.get("relative_strength_zscore_24h")
    if relative_zscore is not None and abs(float(relative_zscore)) >= CROSS_SECTIONAL_Z_THRESHOLD:
        signals[("cross_sectional_relative_strength", "relative_return_24h_zscore_0_5")] = (
            "LONG" if float(relative_zscore) > 0 else "SHORT"
        )
    return signals


def _validate_feature_signal_parity(
    features: dict[tuple[str, int], dict[str, Any]], proposals: list[dict[str, Any]],
) -> dict[str, int]:
    expected: dict[tuple[str, int], dict[tuple[str, str], str]] = defaultdict(dict)
    for proposal in proposals:
        key = (str(proposal["symbol"]), int(proposal["decision_time"]))
        expected[key][(str(proposal["philosophy_family"]), str(proposal["philosophy_variant"]))] = str(proposal["side"])
    mismatches: dict[str, int] = defaultdict(int)
    for key, snapshot in features.items():
        if _classify_observation(snapshot) != expected.get(key, {}):
            actual = _classify_observation(snapshot)
            families = {family for family, _ in actual} | {family for family, _ in expected.get(key, {})}
            for family in families:
                actual_family = {variant: side for (candidate, variant), side in actual.items() if candidate == family}
                expected_family = {variant: side for (candidate, variant), side in expected.get(key, {}).items() if candidate == family}
                if actual_family != expected_family:
                    mismatches[family] += 1
    extra_proposal_keys = set(expected) - set(features)
    for key in extra_proposal_keys:
        for family, _ in expected[key]:
            mismatches[family] += 1
    return dict(mismatches)


def _correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2:
        return None
    left_mean, right_mean = statistics.mean(left), statistics.mean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    denominator = math.sqrt(
        sum((a - left_mean) ** 2 for a in left) * sum((b - right_mean) ** 2 for b in right)
    )
    return numerator / denominator if denominator > 0 else None


def paired_feature_diagnostic(
    spot: dict[tuple[str, int], dict[str, Any]],
    um: dict[tuple[str, int], dict[str, Any]],
) -> dict[str, Any]:
    keys = sorted(set(spot) & set(um))
    feature_pairs = {
        "zscore_24h": [(float(spot[key]["zscore_24h"]), float(um[key]["zscore_24h"])) for key in keys],
        "return_4h": [(float(spot[key]["return_4h"]), float(um[key]["return_4h"])) for key in keys],
        "true_range_expansion_ratio": [(float(spot[key]["true_range_expansion_ratio"]), float(um[key]["true_range_expansion_ratio"])) for key in keys],
        "body_return": [(float(spot[key]["body_return"]), float(um[key]["body_return"])) for key in keys],
        "relative_strength_zscore_24h": [
            (float(spot[key]["relative_strength_zscore_24h"]), float(um[key]["relative_strength_zscore_24h"]))
            for key in keys
            if "relative_strength_zscore_24h" in spot[key] and "relative_strength_zscore_24h" in um[key]
        ],
        "break_distance_long_atr": [
            ((float(row["source_close"]) - float(row["prior_24h_high"])) / float(row["atr_24h"]),
             (float(um[key]["source_close"]) - float(um[key]["prior_24h_high"])) / float(um[key]["atr_24h"]))
            for key, row in ((key, spot[key]) for key in keys)
        ],
        "break_distance_short_atr": [
            ((float(row["prior_24h_low"]) - float(row["source_close"])) / float(row["atr_24h"]),
             (float(um[key]["prior_24h_low"]) - float(um[key]["source_close"])) / float(um[key]["atr_24h"]))
            for key, row in ((key, spot[key]) for key in keys)
        ],
    }
    feature_summary = {}
    for name, pairs in feature_pairs.items():
        left = [pair[0] for pair in pairs]
        right = [pair[1] for pair in pairs]
        differences = sorted(b - a for a, b in pairs)
        absolute_differences = sorted(abs(value) for value in differences)
        feature_summary[name] = {
            "paired_observations": len(pairs),
            "pearson_correlation": _correlation(left, right),
            "spot_mean": statistics.mean(left) if left else None,
            "um_mean": statistics.mean(right) if right else None,
            "um_minus_spot_mean": statistics.mean(differences) if differences else None,
            "abs_delta_p50": statistics.median(absolute_differences) if absolute_differences else None,
            "abs_delta_p90": absolute_differences[int(0.90 * (len(absolute_differences) - 1))] if absolute_differences else None,
        }

    labels = {venue: {} for venue in VENUES}
    for venue, source in (("spot", spot), ("um", um)):
        for key, features in source.items():
            labels[venue][key] = _classify_observation(features)
    family_agreement = {}
    for family in FAMILIES:
        comparable = 0
        spot_triggered = 0
        um_triggered = 0
        disagreements = 0
        side_flips = 0
        for key in keys:
            spot_side = next((side for (candidate, _), side in labels["spot"][key].items() if candidate == family), None)
            um_side = next((side for (candidate, _), side in labels["um"][key].items() if candidate == family), None)
            comparable += 1
            spot_triggered += spot_side is not None
            um_triggered += um_side is not None
            disagreements += spot_side != um_side
            side_flips += spot_side is not None and um_side is not None and spot_side != um_side
        family_agreement[family] = {
            "paired_observations": comparable,
            "spot_triggered": spot_triggered, "um_triggered": um_triggered,
            "trigger_state_or_side_disagreements": disagreements,
            "same_observation_side_flips": side_flips,
            "disagreement_rate_pct": 100.0 * disagreements / comparable if comparable else None,
        }
    return {
        "paired_observation_count": len(keys),
        "feature_differences": feature_summary,
        "signal_rule_agreement_by_family": family_agreement,
    }


def compare_signal_sets(spot: list[dict[str, Any]], um: list[dict[str, Any]]) -> dict[str, Any]:
    spot_by_key = {event_identity(row): row for row in spot}
    um_by_key = {event_identity(row): row for row in um}
    spot_keys, um_keys = set(spot_by_key), set(um_by_key)
    shared_sides = {(key[0], key[1], key[2], key[3]) for key in spot_keys & um_keys}
    spot_observations = {(key[0], key[1], key[2], key[3]) for key in spot_keys}
    um_observations = {(key[0], key[1], key[2], key[3]) for key in um_keys}
    spot_side_by_observation = {key[:4]: key[4] for key in spot_keys}
    um_side_by_observation = {key[:4]: key[4] for key in um_keys}
    spot_only_keys = spot_keys - um_keys
    um_only_keys = um_keys - spot_keys
    side_conflicts = {
        observation for observation in spot_observations & um_observations
        if spot_side_by_observation[observation] != um_side_by_observation[observation]
    }
    union = spot_keys | um_keys
    return {
        "spot_signal_count": len(spot_keys), "um_signal_count": len(um_keys),
        "exact_event_side_agreement": len(spot_keys & um_keys),
        "exact_event_side_jaccard_pct": 100.0 * len(spot_keys & um_keys) / len(union) if union else None,
        "same_observation_both_signal_sides_conflict_count": len(side_conflicts),
        "shared_exact_event_side_count_by_family": {
            family: sum(key[1] == family for key in spot_keys & um_keys) for family in FAMILIES
        },
        "spot_only_count_by_family": {
            family: sum(key[1] == family for key in spot_keys - um_keys) for family in FAMILIES
        },
        "um_only_count_by_family": {
            family: sum(key[1] == family for key in um_only_keys) for family in FAMILIES
        },
        "exclusive_trigger_margin_summary": {
            "spot_only": _summarize_exclusive_margins([spot_by_key[key] for key in sorted(spot_only_keys)]),
            "um_only": _summarize_exclusive_margins([um_by_key[key] for key in sorted(um_only_keys)]),
        },
        "side_conflict_examples": [
            {"symbol": row[0], "family": row[1], "variant": row[2], "decision_time": row[3],
             "spot_side": spot_side_by_observation[row], "um_side": um_side_by_observation[row]}
            for row in sorted(side_conflicts)[:20]
        ],
        "matched_keys": [list(key) for key in sorted(spot_keys & um_keys)],
        "spot_by_key": spot_by_key,
        "um_by_key": um_by_key,
    }


def paired_delta_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "paired_events": len(rows),
        "mean_delta_net_usdt": statistics.mean(row["delta"] for row in rows) if rows else None,
        "cluster_summary": cluster_mean_summary([(row["cluster"], row["delta"]) for row in rows]),
    }


def _load_venue(normalized: Path, venue: str, symbols: list[str]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    feed = HistoricalDataFeed(str(normalized / venue))
    hourly: dict[str, list[dict[str, Any]]] = {}
    hashes: dict[str, str] = {}
    for symbol in symbols:
        parquet = normalized / venue / f"{symbol}_1m.parquet"
        manifest_path = normalized / venue / f"{symbol}_1m.manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("venue") != venue or manifest.get("interval") != "1m":
            raise ValueError(f"source manifest mismatch: {venue}/{symbol}")
        bars_1h = sorted(feed.get_klines(symbol, "1h"), key=lambda bar: float(bar["open_time"]))
        expected_minutes = int(manifest["rows"])
        if expected_minutes % 60 or len(bars_1h) != expected_minutes // 60:
            raise ValueError(f"incomplete {venue} source for {symbol}")
        hourly[symbol] = bars_1h
        hashes[f"{venue}:{symbol}:1m"] = _sha256(parquet)
        hashes[f"{venue}:{symbol}:1m_manifest"] = _sha256(manifest_path)
    return hourly, hashes


def _generate_proposals(
    bars_by_symbol: dict[str, list[dict[str, Any]]], *, window_id: str,
    window_start: int, window_end: int,
) -> list[dict[str, Any]]:
    proposals = []
    for symbol, bars in bars_by_symbol.items():
        proposals.extend(generate_pilot_proposals(
            symbol, bars, window_id=window_id, window_role="development",
            window_start=window_start, window_end=window_end,
            horizon_hours=4, interval_seconds=3600,
        ))
    proposals.extend(generate_cross_sectional_relative_strength_proposals(
        bars_by_symbol, window_id=window_id, window_role="development",
        window_start=window_start, window_end=window_end,
        horizon_hours=4, interval_seconds=3600, minimum_symbols=5,
    ))
    proposals.sort(key=lambda row: event_identity(row))
    keys = [event_identity(row) for row in proposals]
    if len(keys) != len(set(keys)):
        raise ValueError("generator emitted duplicate event identity")
    if any(row["window_role"] != "development" or row["feature_available_at"] > row["decision_time"] for row in proposals):
        raise ValueError("noncausal or non-development proposal")
    return proposals


def _execution_market(minute: list[dict[str, Any]], minute_times: list[int], hourly: list[dict[str, Any]], proposal: dict[str, Any]) -> tuple[float, float, list[dict[str, Any]]]:
    decision = int(round(float(proposal["decision_time"])))
    entry_time = int(round(float(proposal["entry_time"])))
    if entry_time != decision:
        raise ValueError("proposal entry is not the next contiguous hourly open")
    hour_times = [int(round(float(bar["open_time"]))) for bar in hourly]
    closed_hour = decision - 3600
    hour_index = bisect.bisect_left(hour_times, closed_hour)
    if hour_index >= len(hour_times) or hour_times[hour_index] != closed_hour or hour_index < 24:
        raise ValueError("execution ATR warmup/last-closed hour missing")
    context_times = hour_times[hour_index - 24:hour_index + 1]
    context = hourly[hour_index - 24:hour_index + 1]
    if any(right - left != 3600 for left, right in zip(context_times, context_times[1:])):
        raise ValueError("execution ATR contains missing hourly bars")
    true_ranges = []
    for index in range(1, len(context)):
        bar = context[index]
        previous_close = float(context[index - 1]["close"])
        high, low = float(bar["high"]), float(bar["low"])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    atr = statistics.mean(true_ranges)
    start = bisect.bisect_left(minute_times, entry_time)
    if start >= len(minute) or minute_times[start] != entry_time:
        raise ValueError("execution venue minute entry missing")
    path = minute[start:start + HORIZON_MINUTES]
    if len(path) != HORIZON_MINUTES or any(right - left != 60 for left, right in zip(minute_times[start:start + HORIZON_MINUTES], minute_times[start + 1:start + HORIZON_MINUTES])):
        raise ValueError("execution venue four-hour minute path incomplete")
    entry = float(path[0]["open"])
    if not math.isfinite(entry) or entry <= 0 or atr <= 0:
        raise ValueError("invalid execution entry or ATR")
    return entry, atr, path


def _score_event(proposal: dict[str, Any], venue: str, market: tuple[float, float, list[dict[str, Any]]]) -> dict[str, Any]:
    entry, atr, path = market
    direction = 1.0 if proposal["side"] == "LONG" else -1.0
    stop = entry - direction * STOP_ATR * atr
    replay = replay_entry_stop_only(
        path, side=proposal["side"], entry_time=int(proposal["entry_time"]),
        entry_price=entry, stop_price=stop, horizon_minutes=HORIZON_MINUTES,
    )
    if replay.get("label_valid") is not True:
        raise ValueError(f"invalid {venue} replay path: {replay.get('reason')}")
    replay["stop_price_theoretical_unrounded"] = stop
    economic_proposal = {
        "side": proposal["side"], "entry_price_um": entry,
        "entry_time": int(proposal["entry_time"]),
    }
    lifecycle = score_minimal_lifecycle(
        economic_proposal, replay, horizon_minutes=HORIZON_MINUTES,
        fee_bps_per_fill=FEE_BPS_PER_FILL,
        risk_budget_usdt=RISK_BUDGET_USDT,
        max_notional_usdt=NOTIONAL_CAP_USDT,
    )
    return {"venue": venue, "entry": entry, "atr": atr, "stop": stop, "replay": replay, "lifecycle": lifecycle}


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scenarios = {}
    for bps in (0, 5, 10, 20):
        key = f"slippage_{bps}bps_per_fill"
        nets = [row["execution"]["lifecycle"]["scenarios"][key]["net_usdt_no_funding"] for row in rows]
        losses = -sum(min(0.0, value) for value in nets)
        scenarios[key] = {
            "mean_net_usdt": statistics.mean(nets) if nets else None,
            "mean_net_per_100_reference_notional": statistics.mean(row["execution"]["lifecycle"]["scenarios"][key]["net_per_100_reference_notional"] for row in rows) if rows else None,
            "mean_net_r": statistics.mean(row["execution"]["lifecycle"]["scenarios"][key]["net_r_no_funding"] for row in rows) if rows else None,
            "profit_factor": sum(max(0.0, value) for value in nets) / losses if losses else None,
            "positive_pct": 100.0 * sum(value > 0 for value in nets) / len(nets) if nets else None,
        }
    return {
        "event_count": len(rows),
        "stop_pct": 100.0 * sum(row["execution"]["lifecycle"]["exit_reason"] == "initial_stop" for row in rows) / len(rows) if rows else None,
        "scenarios": scenarios,
        "cluster_summary_10bps": cluster_mean_summary([
            (row["cluster"], row["execution"]["lifecycle"]["scenarios"]["slippage_10bps_per_fill"]["net_usdt_no_funding"])
            for row in rows
        ]),
    }


def analyze_window(window_id: str, normalized_name: str, output_path: Path) -> dict[str, Any]:
    normalized = ROOT / "normalized" / normalized_name
    spot_manifest_paths = sorted((normalized / "spot").glob("*_1m.manifest.json"))
    um_manifest_paths = sorted((normalized / "um").glob("*_1m.manifest.json"))
    spot_symbols = {path.name.removesuffix("_1m.manifest.json") for path in spot_manifest_paths}
    um_symbols = {path.name.removesuffix("_1m.manifest.json") for path in um_manifest_paths}
    if spot_symbols != um_symbols or len(spot_symbols) != 28:
        raise ValueError("development venue universes are not an identical 28-symbol set")
    symbols = sorted(spot_symbols)
    hourly_markets = {}
    source_hashes = {}
    for venue in VENUES:
        hours, hashes = _load_venue(normalized, venue, symbols)
        hourly_markets[venue] = hours
        source_hashes.update(hashes)
    for symbol in symbols:
        spot_hours = [int(round(float(bar["open_time"]))) for bar in hourly_markets["spot"][symbol]]
        um_hours = [int(round(float(bar["open_time"]))) for bar in hourly_markets["um"][symbol]]
        if spot_hours != um_hours:
            raise ValueError(f"venue hourly timestamps differ for {symbol}")
    window_start = min(int(round(float(rows[0]["open_time"]))) for rows in hourly_markets["spot"].values())
    window_end = max(int(round(float(rows[-1]["open_time"]))) for rows in hourly_markets["spot"].values()) + 3600
    generated = {
        venue: _generate_proposals(
            hourly_markets[venue], window_id=window_id,
            window_start=window_start, window_end=window_end,
        )
        for venue in VENUES
    }
    observation_features = {
        venue: _build_observation_features(
            hourly_markets[venue], window_start=window_start, window_end=window_end,
        )
        for venue in VENUES
    }
    signal_reconstruction_mismatches = {
        venue: _validate_feature_signal_parity(observation_features[venue], generated[venue])
        for venue in VENUES
    }
    if any(signal_reconstruction_mismatches.values()):
        raise ValueError(f"paired feature reconstruction does not match generator: {signal_reconstruction_mismatches}")
    feature_diagnostic = paired_feature_diagnostic(observation_features["spot"], observation_features["um"])
    comparison = compare_signal_sets(generated["spot"], generated["um"])
    proposals_by_venue_symbol = {
        venue: {
            symbol: [row for row in generated[venue] if row["symbol"] == symbol]
            for symbol in symbols
        }
        for venue in VENUES
    }
    group_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    per_event: dict[tuple[str, tuple[str, str, str, int, str], str], dict[str, Any]] = {}
    output_rows = []
    for symbol in symbols:
        minute_by_venue = {}
        minute_times_by_venue = {}
        feeds = {}
        for venue in VENUES:
            feed = HistoricalDataFeed(str(normalized / venue))
            bars = sorted(feed.get_klines(symbol, "1m"), key=lambda bar: float(bar["open_time"]))
            feeds[venue] = feed
            minute_by_venue[venue] = bars
            minute_times_by_venue[venue] = [int(round(float(bar["open_time"])) ) for bar in bars]
        if minute_times_by_venue["spot"] != minute_times_by_venue["um"]:
            raise ValueError(f"venue minute timestamps differ for {symbol}")
        for signal_venue in VENUES:
            for proposal in proposals_by_venue_symbol[signal_venue][symbol]:
                event = event_identity(proposal)
                for execution_venue in VENUES:
                    entry, atr, path = _execution_market(
                        minute_by_venue[execution_venue], minute_times_by_venue[execution_venue],
                        hourly_markets[execution_venue][symbol], proposal,
                    )
                    score = _score_event(proposal, execution_venue, (entry, atr, path))
                    row = {
                        "signal_venue": signal_venue, "execution_venue": execution_venue,
                        "event_identity": list(event), "cluster": proposal["dedupe_cluster_id"],
                        "family": proposal["philosophy_family"], "variant": proposal["philosophy_variant"],
                        "side": proposal["side"], "symbol": proposal["symbol"],
                        "decision_time": int(proposal["decision_time"]), "feature_snapshot": proposal["feature_snapshot"],
                        "execution": score,
                    }
                    output_rows.append(row)
                    key = f"{signal_venue}|{execution_venue}|{proposal['philosophy_family']}|{proposal['side']}"
                    group_rows[key].append(row)
                    per_event[(signal_venue, event, execution_venue)] = row
        del feeds, minute_by_venue, minute_times_by_venue

    execution_deltas: dict[str, list[dict[str, Any]]] = defaultdict(list)
    signal_deltas: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for signal_venue in VENUES:
        events = [event_identity(proposal) for proposal in generated[signal_venue]]
        for event in events:
            spot_row = per_event.get((signal_venue, event, "spot"))
            um_row = per_event.get((signal_venue, event, "um"))
            if spot_row is None or um_row is None:
                raise ValueError("same-signal execution-venue pair missing")
            key = f"{signal_venue}|{event[1]}|{event[4]}|UM-minus-Spot"
            spot_net = spot_row["execution"]["lifecycle"]["scenarios"]["slippage_10bps_per_fill"]["net_usdt_no_funding"]
            um_net = um_row["execution"]["lifecycle"]["scenarios"]["slippage_10bps_per_fill"]["net_usdt_no_funding"]
            execution_deltas[key].append({"cluster": spot_row["cluster"], "delta": um_net - spot_net})
    common = [tuple(key) for key in comparison["matched_keys"]]
    for execution_venue in VENUES:
        for event in common:
            spot_row = per_event[("spot", event, execution_venue)]
            um_row = per_event[("um", event, execution_venue)]
            key = f"Spot-vs-UM-signal|{execution_venue}|{event[1]}|{event[4]}"
            spot_net = spot_row["execution"]["lifecycle"]["scenarios"]["slippage_10bps_per_fill"]["net_usdt_no_funding"]
            um_net = um_row["execution"]["lifecycle"]["scenarios"]["slippage_10bps_per_fill"]["net_usdt_no_funding"]
            signal_deltas[key].append({"cluster": spot_row["cluster"], "delta": um_net - spot_net})

    signal_report = {key: value for key, value in comparison.items() if key not in {"matched_keys", "spot_by_key", "um_by_key"}}
    return {
        "window_role": "development", "window_id": window_id,
        "window_start": window_start, "window_end": window_end,
        "symbol_count": len(symbols), "source_market_hashes": source_hashes,
        "signal_comparison": signal_report,
        "paired_feature_diagnostic": feature_diagnostic,
        "signal_reconstruction_mismatches": signal_reconstruction_mismatches,
        "groups": {key: _summarize(rows) for key, rows in sorted(group_rows.items())},
        "same_signal_execution_venue_deltas_10bps": {key: paired_delta_summary(rows) for key, rows in sorted(execution_deltas.items())},
        "matched_signal_source_deltas_10bps": {key: paired_delta_summary(rows) for key, rows in sorted(signal_deltas.items())},
        "labels_are_executable_fills": False,
        "scored_rows": len(output_rows),
        "events_scored": sum(len(rows) for rows in generated.values()),
        "output_rows": output_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "signal_execution_basis_development_v1")
    args = parser.parse_args()
    output_dir = args.output_dir
    if output_dir.exists():
        parser.error("output directory exists; preserve prior artifacts and use a new version")
    output_dir.mkdir(parents=True)
    report: dict[str, Any] = {
        "artifact_version": "signal_execution_basis_development_v1",
        "analyzer_sha256": _sha256(Path(__file__)), "candidate_claim_allowed": False,
        "contract": {
            "signal_sources": ["Spot 1h OHLCV", "UM 1h OHLCV"],
            "execution_sources": ["Spot 1m OHLCV", "UM 1m OHLCV"],
            "entry": "next contiguous 1h boundary using execution-venue 1m open",
            "stop": "1.5 ATR from execution venue's last 24 fully closed 1h true ranges",
            "exit": "initial stop or four-hour horizon close, full quantity",
            "fee_bps_per_fill": FEE_BPS_PER_FILL,
            "slippage_bps_per_fill": [0, 5, 10, 20],
            "risk_budget_usdt": RISK_BUDGET_USDT,
            "reference_notional_cap_usdt": NOTIONAL_CAP_USDT,
            "unit": "independent opportunity outcomes, not a shared-capital portfolio",
            "paired_deltas": "same exact event or same generated event across execution venues; 500-replicate equal-cluster bootstrap, pointwise",
        },
        "development_gate_passed": False, "validation_read": False,
        "limitations": [
            "This diagnoses source/execution venue effects, not a tradable edge or actual order fills.",
            "The signal generator's full indicator set and cross-sectional ranking are recomputed independently per venue.",
            "Common 5bps fee proxy does not reproduce venue-specific account fees or funding.",
            "Execution venue changes entry, ATR/stop price and exit path together; this is the combined venue execution effect.",
            "Exact event agreement requires same symbol, family, variant, decision time, and side.",
            "No validation/holdout data is opened by this development-only runner.",
        ],
        "windows": {},
    }
    for window_id, normalized_name in WINDOWS:
        result = analyze_window(window_id, normalized_name, output_dir / f"{window_id}.jsonl")
        rows = result.pop("output_rows")
        jsonl_path = output_dir / f"{window_id}.jsonl"
        with jsonl_path.open("w", encoding="utf-8", newline="\n") as target:
            for row in rows:
                target.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        result["output_sha256"] = _sha256(jsonl_path)
        report["windows"][window_id] = result
        (output_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        print(json.dumps({"window": window_id, "signals_spot": result["signal_comparison"]["spot_signal_count"], "signals_um": result["signal_comparison"]["um_signal_count"], "exact_agreement": result["signal_comparison"]["exact_event_side_agreement"], "scored_rows": result["scored_rows"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
