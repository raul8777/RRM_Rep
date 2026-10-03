"""Measure favorable price excursions before the original TP/SL/expiry label."""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import statistics
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from simulation.entry_outcome_resolver import resolve_contract_path
from simulation.mimic_feed import HistoricalDataFeed


REPO_ROOT = Path(__file__).resolve().parents[2]
PILOT_ROOT = REPO_ROOT / "Dev/benchmarks/universal_entry_selection"
DEFAULT_RUN_ID = "fast-20261001-003"
DEFAULT_ROLE = "validation"
THRESHOLDS_PCT = (0.5, 1.0, 1.5, 2.0, 3.0, 5.0)
SLIPPAGE_BPS_PER_FILL = (0.0, 5.0, 10.0, 20.0)
HORIZON_MINUTES = 240


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"expected object at {path.name}:{line_number}")
            yield row


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _bar_mfe_pct(bars: List[Dict[str, Any]], *, side: str, entry_price: float) -> float:
    if not bars:
        return 0.0
    if side == "LONG":
        favorable = max(float(bar.get("high", entry_price)) for bar in bars)
        return max(0.0, (favorable / entry_price - 1.0) * 100.0)
    if side == "SHORT":
        favorable = min(float(bar.get("low", entry_price)) for bar in bars)
        return max(0.0, (entry_price / favorable - 1.0) * 100.0)
    raise ValueError(f"unsupported side: {side}")


def measure_mfe_bounds(
    bars: List[Dict[str, Any]], *, side: str, entry_time: int, exit_time: int,
    entry_price: float, stop_price: float, exit_price: float, outcome: str,
    gap_through: bool, bar_times: List[int] | None = None,
) -> Tuple[float, float]:
    """Return conservative/optimistic MFE before exit; SL-bar order is bounded."""
    side = str(side).upper()
    if entry_price <= 0 or stop_price <= 0 or exit_price <= 0:
        raise ValueError("entry, stop, and exit prices must be positive")
    times = bar_times if bar_times is not None else [int(round(float(bar["open_time"]))) for bar in bars]
    start = bisect.bisect_left(times, int(entry_time))
    end_exclusive = bisect.bisect_left(times, int(exit_time))
    before_exit_bar = bars[start:end_exclusive]
    exit_index = bisect.bisect_left(times, int(exit_time))
    exit_bar = bars[exit_index] if exit_index < len(bars) and times[exit_index] == int(exit_time) else None

    if outcome == "SL":
        conservative = _bar_mfe_pct(before_exit_bar, side=side, entry_price=entry_price)
        include_stop_bar = False
        if exit_bar is not None and not gap_through:
            open_price = float(exit_bar.get("open", entry_price))
            include_stop_bar = open_price > stop_price if side == "LONG" else open_price < stop_price
        optimistic_bars = before_exit_bar + ([exit_bar] if include_stop_bar else [])
        optimistic = _bar_mfe_pct(optimistic_bars, side=side, entry_price=entry_price)
        return conservative, max(conservative, optimistic)

    through_exit = bars[start:bisect.bisect_right(times, int(exit_time))] if outcome == "TP" else before_exit_bar
    excursion = _bar_mfe_pct(through_exit, side=side, entry_price=entry_price)
    if outcome == "TP":
        realized_target_move = max(0.0, (exit_price / entry_price - 1.0) * 100.0) if side == "LONG" else max(0.0, (entry_price / exit_price - 1.0) * 100.0)
        excursion = min(excursion, realized_target_move)
    return excursion, excursion


def _percentiles(values: List[float]) -> Dict[str, float | None]:
    if not values:
        return {"min": None, "p10": None, "median": None, "p90": None, "max": None, "mean": None}
    ordered = sorted(values)
    def percentile(p: float) -> float:
        index = (len(ordered) - 1) * p
        low, high = int(index), min(int(index) + 1, len(ordered) - 1)
        fraction = index - low
        return ordered[low] * (1.0 - fraction) + ordered[high] * fraction
    return {
        "min": ordered[0], "p10": percentile(0.10), "median": percentile(0.50),
        "p90": percentile(0.90), "max": ordered[-1], "mean": statistics.mean(ordered),
    }


def _tighter_target_price(side: str, entry_price: float, original_target: float, threshold_pct: float) -> float:
    threshold_target = entry_price * (1.0 + threshold_pct / 100.0) if side == "LONG" else entry_price * (1.0 - threshold_pct / 100.0)
    return min(original_target, threshold_target) if side == "LONG" else max(original_target, threshold_target)


def _level_distance_pct(entry_price: float, level_price: float) -> float:
    if entry_price <= 0 or level_price <= 0:
        raise ValueError("entry and level prices must be positive")
    return abs(level_price - entry_price) / entry_price * 100.0


def _replay_threshold_exit(
    bars: List[Dict[str, Any]], *, side: str, entry_time: int, entry_price: float,
    stop_price: float, target_price: float, quantity: float, entry_fee_bps: float,
    slippage_bps: float,
) -> Dict[str, Any]:
    return resolve_contract_path(
        bars,
        side=side,
        entry_time=entry_time,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        horizon_minutes=HORIZON_MINUTES,
        quantity=quantity,
        entry_fee_bps=entry_fee_bps,
        exit_fee_bps=entry_fee_bps,
        slippage_bps=slippage_bps,
        funding_cost_usdt=0.0,
        bar_seconds=60,
        ambiguous_policy="stop_first",
    )


def build_early_profit_opportunity_report(
    run_dir: Path,
    *,
    role: str = DEFAULT_ROLE,
    output_root: Path | None = None,
) -> Dict[str, Any]:
    if role not in {"development", "validation"}:
        raise ValueError("role must be development or validation")
    run_dir = Path(run_dir).resolve()
    output_root = Path(output_root).resolve() if output_root is not None else None
    replay_root = run_dir / "replay" / role
    outcome_path = replay_root / "bilateral_scaffold_outcomes.jsonl"
    scaffold_path = replay_root / "bilateral_contract_scaffold.jsonl"
    outcome_report_path = outcome_path.with_suffix(".report.json")
    scaffold_report_path = scaffold_path.with_suffix(".report.json")
    matrix_path = replay_root / "bilateral_thesis_matrix.jsonl"
    for path in (outcome_path, scaffold_path, outcome_report_path, scaffold_report_path, matrix_path):
        if not path.is_file():
            raise FileNotFoundError(path)

    outcome_report = json.loads(outcome_report_path.read_text(encoding="utf-8"))
    scaffold_report = json.loads(scaffold_report_path.read_text(encoding="utf-8"))
    if _sha256(outcome_path) != outcome_report.get("output_sha256"):
        raise ValueError("outcome tape hash differs from its report")
    if _sha256(scaffold_path) != outcome_report.get("input_sha256"):
        raise ValueError("scaffold hash differs from outcome report")
    normalized_root = PILOT_ROOT / "normalized" / f"pilot_{role}"
    parquet_hashes = outcome_report.get("scaffold_source_market_parquet_hashes", {})
    if not parquet_hashes:
        raise ValueError("outcome report lacks source Parquet hashes")
    for key, expected in parquet_hashes.items():
        venue, symbol, interval = key.split(":", 2)
        source = normalized_root / venue / f"{symbol}_{interval}.parquet"
        if not source.is_file() or _sha256(source) != expected:
            raise ValueError(f"source Parquet hash mismatch: {key}")

    matrix = {row["bilateral_thesis_id"]: row for row in _read_jsonl(matrix_path)}
    valid_outcomes = []
    counts = Counter()
    for row in _read_jsonl(outcome_path):
        result = row.get("outcome") if isinstance(row.get("outcome"), dict) else {}
        geometry = row.get("geometry") if isinstance(row.get("geometry"), dict) else {}
        if result.get("label_valid") is not True:
            counts["invalid_or_censored_label"] += 1
            continue
        if geometry.get("geometry_valid") is not True:
            counts["invalid_geometry"] += 1
            continue
        if row.get("label_status") != "experimental_scaffold_outcome_not_executable_fill" or row.get("executable_at_decision") is not False:
            raise ValueError("unexpected executable/fill label in simulation analysis")
        thesis = matrix.get(str(row.get("bilateral_thesis_id") or ""))
        if thesis is None or thesis.get("thesis_role") != "observed":
            counts["non_observed_or_missing_thesis"] += 1
            continue
        contract_symbol = geometry.get("venue_symbol") or row.get("contract_symbol") or row.get("symbol")
        venue = geometry.get("venue") or ("um" if str(row.get("mode", "")).startswith("FUTURES") else "spot")
        valid_outcomes.append({
            **row,
            "philosophy_family": thesis.get("philosophy_family", "unknown"),
            "philosophy_variant": thesis.get("philosophy_variant", "unknown"),
            "venue_dir": "um" if venue == "um" else "spot",
            "feed_symbol": str(contract_symbol),
        })
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in valid_outcomes:
        grouped[(row["venue_dir"], row["feed_symbol"])].append(row)

    output_root = output_root or run_dir / "early_profit_threshold_analysis" / role
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / "early_profit_opportunities.jsonl"
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    output_counts: Dict[str, Dict[str, Dict[str, Dict[str, Dict[str, int]]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(int))))
    )
    mfe_values: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    tp_percentages: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    row_denominators: Dict[str, Dict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    exit_policy_stats: Dict[Tuple[str, str, float, float], Dict[str, Any]] = {}
    row_count = 0

    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as target:
            for (venue_dir, symbol), symbol_rows in sorted(grouped.items()):
                feed = HistoricalDataFeed(str(normalized_root / venue_dir))
                bars = feed.get_klines(symbol, "1m")
                bars = sorted(bars, key=lambda bar: float(bar.get("open_time", 0.0)))
                bar_times = [int(round(float(bar["open_time"]))) for bar in bars]
                for row in symbol_rows:
                    result = row["outcome"]
                    geometry = row["geometry"]
                    side = str(row.get("side") or "").upper()
                    entry_price = float(geometry["entry_price"])
                    stop_price = float(geometry["stop_price"])
                    target_price = float(geometry["target_price"])
                    entry_time = int(round(float(result.get("entry_time") or row.get("decision_time"))))
                    exit_time = int(round(float(result.get("exit_time") or entry_time + HORIZON_MINUTES * 60)))
                    outcome = str(result.get("outcome") or "unknown")
                    mfe_lower, mfe_upper = measure_mfe_bounds(
                        bars,
                        side=side,
                        entry_time=entry_time,
                        exit_time=exit_time,
                        entry_price=entry_price,
                        stop_price=stop_price,
                        exit_price=float(result.get("exit_price") or entry_price),
                        outcome=outcome,
                        gap_through=result.get("gap_through") is True,
                        bar_times=bar_times,
                    )
                    tp_pct = _level_distance_pct(entry_price, target_price)
                    stop_pct = _level_distance_pct(entry_price, stop_price)
                    family = str(row["philosophy_family"])
                    mode = str(row.get("mode") or "unknown")
                    mfe_values[family][mode].append(mfe_lower)
                    tp_percentages[family][mode].append(tp_pct)
                    row_denominators[family][mode][outcome] += 1
                    thresholds = {}
                    for threshold in THRESHOLDS_PCT:
                        conservative = mfe_lower >= threshold
                        possible = mfe_upper >= threshold
                        counts_key = f"{threshold:g}"
                        bucket = output_counts[family][mode][outcome][counts_key]
                        bucket["conservative"] += int(conservative)
                        bucket["possible_in_stop_bar"] += int(possible and not conservative)
                        bucket["upper_bound"] += int(possible)
                        thresholds[counts_key] = {
                            "reached_before_exit_conservative": conservative,
                            "possible_only_if_stop_bar_high_low_preceded_stop": possible and not conservative,
                            "upper_bound_reached": possible,
                        }
                    start_bar = bisect.bisect_left(bar_times, entry_time)
                    end_bar = bisect.bisect_right(bar_times, exit_time)
                    path_bars = bars[start_bar:end_bar]
                    quantity = 100.0 / entry_price
                    fee_bps_per_fill = 10.0 if row.get("mode") == "SPOT_LONG" else 5.0
                    for slippage_bps in SLIPPAGE_BPS_PER_FILL:
                        baseline_exit = _replay_threshold_exit(
                            path_bars, side=side, entry_time=entry_time, entry_price=entry_price,
                            stop_price=stop_price, target_price=target_price, quantity=quantity,
                            entry_fee_bps=fee_bps_per_fill, slippage_bps=slippage_bps,
                        )
                        for threshold in THRESHOLDS_PCT:
                            tighter_target = _tighter_target_price(side, entry_price, target_price, threshold)
                            threshold_exit = _replay_threshold_exit(
                                path_bars, side=side, entry_time=entry_time, entry_price=entry_price,
                                stop_price=stop_price, target_price=tighter_target, quantity=quantity,
                                entry_fee_bps=fee_bps_per_fill, slippage_bps=slippage_bps,
                            )
                            policy_key = (family, mode, threshold, slippage_bps)
                            policy = exit_policy_stats.setdefault(policy_key, {
                                "family": family, "mode": mode, "threshold_price_move_pct": threshold,
                                "slippage_bps_per_fill": slippage_bps, "fee_bps_per_fill_proxy": fee_bps_per_fill,
                                "rows": 0, "baseline_net_sum_per_100": 0.0, "threshold_net_sum_per_100": 0.0,
                                "baseline_net_positive": 0, "threshold_net_positive": 0,
                                "baseline_outcomes": Counter(), "threshold_outcomes": Counter(),
                                "original_sl_to_positive_threshold_exit": 0,
                                "original_sl_to_threshold_tp": 0, "original_tp_reduced_by_tighter_target": 0,
                                "censored_threshold_replay": 0,
                            })
                            if threshold_exit.get("label_valid") is not True or baseline_exit.get("label_valid") is not True:
                                policy["censored_threshold_replay"] += 1
                                continue
                            baseline_net = float(baseline_exit.get("net_pnl_usdt", 0.0))
                            threshold_net = float(threshold_exit.get("net_pnl_usdt", 0.0))
                            policy["rows"] += 1
                            policy["baseline_net_sum_per_100"] += baseline_net
                            policy["threshold_net_sum_per_100"] += threshold_net
                            policy["baseline_net_positive"] += int(baseline_net > 0.0)
                            policy["threshold_net_positive"] += int(threshold_net > 0.0)
                            policy["baseline_outcomes"][str(baseline_exit.get("outcome"))] += 1
                            policy["threshold_outcomes"][str(threshold_exit.get("outcome"))] += 1
                            policy["original_sl_to_positive_threshold_exit"] += int(outcome == "SL" and threshold_exit.get("outcome") == "TP" and threshold_net > 0.0)
                            policy["original_sl_to_threshold_tp"] += int(outcome == "SL" and threshold_exit.get("outcome") == "TP")
                            policy["original_tp_reduced_by_tighter_target"] += int(outcome == "TP" and threshold_exit.get("outcome") == "TP" and float(threshold_exit.get("exit_price", entry_price)) != float(baseline_exit.get("exit_price", entry_price)))
                    target.write(json.dumps({
                        "opportunity_event_id": row.get("opportunity_event_id"),
                        "bilateral_thesis_id": row.get("bilateral_thesis_id"),
                        "dedupe_cluster_id": row.get("dedupe_cluster_id"),
                        "family": family,
                        "variant": row["philosophy_variant"],
                        "mode": mode,
                        "venue": venue_dir,
                        "symbol": row.get("symbol"),
                        "side": side,
                        "outcome": outcome,
                        "decision_time": row.get("decision_time"),
                        "entry_time": entry_time,
                        "exit_time": exit_time,
                        "entry_price": entry_price,
                        "original_tp_price": target_price,
                        "original_sl_price": stop_price,
                        "original_tp_price_move_pct": tp_pct,
                        "original_sl_price_move_pct": stop_pct,
                        "mfe_price_move_pct_conservative": mfe_lower,
                        "mfe_price_move_pct_upper_bound": mfe_upper,
                        "thresholds": thresholds,
                        "label_is_executable_fill": False,
                    }, ensure_ascii=False, allow_nan=False) + "\n")
                    row_count += 1
                del feed, bars
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)

    summaries = {}
    for family in sorted(set(mfe_values) | set(tp_percentages)):
        summaries[family] = {}
        for mode in sorted(set(mfe_values[family]) | set(tp_percentages[family])):
            outcome_counts = output_counts[family][mode]
            by_outcome = {}
            for outcome, threshold_counts in outcome_counts.items():
                n = row_denominators[family][mode][outcome]
                by_outcome[outcome] = {
                    threshold: {
                        **counts_by_threshold,
                        "denominator": n,
                        "conservative_rate": counts_by_threshold["conservative"] / n if n else None,
                        "upper_bound_rate": counts_by_threshold["upper_bound"] / n if n else None,
                    }
                    for threshold, counts_by_threshold in threshold_counts.items()
                }
            summaries[family][mode] = {
                "valid_rows": len(mfe_values[family][mode]),
                "original_tp_price_move_pct": _percentiles(tp_percentages[family][mode]),
                "mfe_price_move_pct_conservative": _percentiles(mfe_values[family][mode]),
                "counts_by_original_outcome": by_outcome,
            }
    exit_policy_report = {}
    for (family, mode, threshold, slippage), values in sorted(exit_policy_stats.items()):
        n = values["rows"]
        values["baseline_mean_net_usdt_per_100"] = values["baseline_net_sum_per_100"] / n if n else None
        values["threshold_mean_net_usdt_per_100"] = values["threshold_net_sum_per_100"] / n if n else None
        values["mean_net_delta_usdt_per_100"] = (values["threshold_net_sum_per_100"] - values["baseline_net_sum_per_100"]) / n if n else None
        values["baseline_positive_net_rate"] = values["baseline_net_positive"] / n if n else None
        values["threshold_positive_net_rate"] = values["threshold_net_positive"] / n if n else None
        values["baseline_outcomes"] = dict(values["baseline_outcomes"])
        values["threshold_outcomes"] = dict(values["threshold_outcomes"])
        exit_policy_report[f"{family}|{mode}|tp_{threshold:g}pct|slip_{slippage:g}bps"] = values
    report = {
        "artifact_version": "universal_entry_early_profit_opportunities_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_run_id": run_dir.name,
        "role": role,
        "horizon_minutes": outcome_report.get("horizon_minutes"),
        "thresholds_price_move_pct": list(THRESHOLDS_PCT),
        "threshold_basis": "underlying-price move / gross notional, not return on margin and not realized return",
        "stop_bar_policy": {
            "conservative": "exclude the SL exit bar; only earlier bars count as definitely before stop",
            "upper_bound": "include the SL bar only when it opened strictly before the stop; OHLC cannot reveal high/low order within a stop bar",
            "gap_through": "exclude all post-open excursions from the gap stop bar",
            "TP_exit": "cap MFE at the original TP fill price; no excursion after the original TP is counted",
        },
        "source_files": {
            "outcomes_path": str(outcome_path.relative_to(REPO_ROOT)).replace("\\", "/"),
            "outcomes_sha256": _sha256(outcome_path),
            "scaffold_sha256": _sha256(scaffold_path),
            "matrix_sha256": _sha256(matrix_path),
        },
        "valid_input_rows": len(valid_outcomes),
        "output_rows": row_count,
        "rejected_row_counts": dict(counts),
        "summary_by_family_mode": summaries,
        "threshold_exit_policy_sensitivity": exit_policy_report,
        "output_path": str(output_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "output_sha256": _sha256(output_path),
        "label_is_executable_fill": False,
        "winner_claim_allowed": False,
        "limitations": [
            "A reached threshold is a price-path opportunity, not a realized close or proven lifecycle execution.",
            "All labels are non-executable scaffold replay outcomes; historical filters, account balances, fees and fills are unknown.",
            "Thresholds are underlying price/notional moves; Futures return on margin depends on unknown historical leverage.",
            "Same-minute stop-bar ordering is bounded, not inferred from OHLC.",
            "Fees, slippage and latency are not deducted; a later lifecycle study must replay executable exits and costs.",
        ],
    }
    report_path = output_root / "report.json"
    _atomic_json(report_path, report)
    report["report_path"] = str(report_path.relative_to(REPO_ROOT)).replace("\\", "/")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--role", choices=("development", "validation"), default=DEFAULT_ROLE)
    parser.add_argument("--run-root", type=Path, default=PILOT_ROOT / "comparison_runs")
    args = parser.parse_args()
    run_dir = args.run_root / args.run_id
    if not run_dir.is_dir():
        parser.error(f"comparison run not found: {run_dir}")
    try:
        report = build_early_profit_opportunity_report(run_dir, role=args.role)
    except (OSError, ValueError, KeyError) as exc:
        parser.error(f"early-profit opportunity analysis failed: {type(exc).__name__}: {str(exc)[:300]}")
    print(json.dumps({
        "run_id": report["source_run_id"], "role": report["role"],
        "valid_input_rows": report["valid_input_rows"], "output_rows": report["output_rows"],
        "report_path": report["report_path"], "report_sha256": _sha256(Path(REPO_ROOT / report["report_path"])),
        "winner_claim_allowed": report["winner_claim_allowed"], "label_is_executable_fill": report["label_is_executable_fill"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())