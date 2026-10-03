"""Compare entry-time Futures stop distances without TP or lifecycle exits."""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import random
import statistics
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

from Dev.scripts.score_universal_entry_philosophies_fixed_contract import _point_in_time_um_market
from simulation.mimic_feed import HistoricalDataFeed


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HORIZON_MINUTES = 240
DEFAULT_STOP_ATR_MULTIPLES = (0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)
DEFAULT_NOTIONAL_USDT = 100.0
DEFAULT_FEE_BPS_PER_FILL = 5.0
SLIPPAGE_BPS_PER_FILL = (0.0, 5.0, 10.0, 20.0)
DEFAULT_MAX_LOSS_USDT = 18.0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rows(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"expected object at {path.name}:{line_number}")
            yield row


def _favorable_move_pct(side: str, entry: float, bars: List[Dict[str, Any]]) -> float:
    if not bars:
        return 0.0
    if side == "LONG":
        return max(0.0, (max(float(bar["high"]) for bar in bars) / entry - 1.0) * 100.0)
    return max(0.0, (entry / min(float(bar["low"]) for bar in bars) - 1.0) * 100.0)


def _adverse_move_pct(side: str, entry: float, bars: List[Dict[str, Any]]) -> float:
    if not bars:
        return 0.0
    if side == "LONG":
        return max(0.0, (1.0 - min(float(bar["low"]) for bar in bars) / entry) * 100.0)
    return max(0.0, (max(float(bar["high"]) for bar in bars) / entry - 1.0) * 100.0)


def replay_entry_stop_only(
    bars: List[Dict[str, Any]],
    *,
    side: str,
    entry_time: int,
    entry_price: float,
    stop_price: float,
    horizon_minutes: int = DEFAULT_HORIZON_MINUTES,
    bar_seconds: int = 60,
) -> Dict[str, Any]:
    """Replay one fixed entry stop; do not apply a TP or lifecycle exit."""
    normalized_side = str(side or "").strip().upper()
    if normalized_side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    if entry_price <= 0 or stop_price <= 0 or horizon_minutes <= 0 or bar_seconds <= 0:
        raise ValueError("entry, stop, horizon, and bar interval must be positive")
    if (normalized_side == "LONG" and stop_price >= entry_price) or (
        normalized_side == "SHORT" and stop_price <= entry_price
    ):
        raise ValueError("stop must be on the adverse side of entry")
    if (horizon_minutes * 60) % bar_seconds:
        raise ValueError("horizon must be divisible by bar interval")

    bars_by_time: Dict[int, Dict[str, Any]] = {}
    for bar in bars:
        timestamp = int(round(float(bar.get("open_time", bar.get("timestamp")))))
        if timestamp in bars_by_time:
            return {"label_valid": False, "reason": "DUPLICATE_BAR_TIMESTAMP"}
        bars_by_time[timestamp] = bar

    count = horizon_minutes * 60 // bar_seconds
    path = []
    for step in range(count):
        timestamp = int(entry_time) + step * bar_seconds
        bar = bars_by_time.get(timestamp)
        if bar is None:
            return {"label_valid": False, "reason": "EXPECTED_BAR_MISSING"}
        path.append(bar)

    trigger_index = None
    trigger_price = None
    gap_through = False
    for index, bar in enumerate(path):
        open_price = float(bar.get("open", entry_price))
        if normalized_side == "LONG":
            if open_price <= stop_price:
                trigger_index, trigger_price, gap_through = index, open_price, open_price < stop_price
                break
            if float(bar["low"]) <= stop_price:
                trigger_index, trigger_price = index, stop_price
                break
        else:
            if open_price >= stop_price:
                trigger_index, trigger_price, gap_through = index, open_price, open_price > stop_price
                break
            if float(bar["high"]) >= stop_price:
                trigger_index, trigger_price = index, stop_price
                break

    before_trigger = path if trigger_index is None else path[:trigger_index]
    mfe_before_trigger = _favorable_move_pct(normalized_side, entry_price, before_trigger)
    post_trigger_bars = path[trigger_index + 1:] if trigger_index is not None else []
    post_trigger_mfe = _favorable_move_pct(normalized_side, entry_price, post_trigger_bars) if trigger_index is not None else None
    full_path_mfe = _favorable_move_pct(normalized_side, entry_price, path)
    full_path_mae = _adverse_move_pct(normalized_side, entry_price, path)
    return {
        "label_valid": True,
        "stop_triggered": trigger_index is not None,
        "trigger_time": int(entry_time) + trigger_index * bar_seconds if trigger_index is not None else None,
        "stop_fill_reference_price": trigger_price,
        "gap_through": gap_through,
        "mfe_before_trigger_pct": mfe_before_trigger,
        "mfe_after_trigger_bar_pct": post_trigger_mfe,
        "full_horizon_mfe_pct": full_path_mfe,
        "full_horizon_mae_pct": full_path_mae,
        "horizon_close": float(path[-1]["close"]),
        "stop_bar_excursion_excluded": trigger_index is not None,
    }


def _stop_net_per_100(
    *,
    side: str,
    entry: float,
    fill_reference: float,
    notional: float,
    fee_bps: float,
    slippage_bps: float,
) -> float:
    quantity = notional / entry
    direction = 1.0 if side == "LONG" else -1.0
    adverse_slippage = fill_reference * (1.0 - slippage_bps / 10_000.0) if side == "LONG" else fill_reference * (1.0 + slippage_bps / 10_000.0)
    gross = direction * (fill_reference - entry) * quantity
    costs = (entry + adverse_slippage) * quantity * fee_bps / 10_000.0
    slip_cost = (entry + fill_reference) * quantity * slippage_bps / 10_000.0
    return (gross - costs - slip_cost) / notional * 100.0


def _notional_for_max_loss_budget(
    *,
    max_loss_usdt: float,
    stop_distance_frac: float,
    fee_bps: float,
    slippage_bps: float,
) -> float:
    if max_loss_usdt <= 0 or stop_distance_frac <= 0:
        return 0.0
    round_trip_cost = 2.0 * (fee_bps + slippage_bps) / 10_000.0
    return max_loss_usdt / (stop_distance_frac + round_trip_cost)


def score_minimal_lifecycle(
    proposal: Dict[str, Any],
    stop_result: Dict[str, Any],
    *,
    horizon_minutes: int = 240,
    fee_bps_per_fill: float = 5.0,
    risk_budget_usdt: float = 18.0,
    max_notional_usdt: float = 100.0,
) -> Dict[str, Any]:
    if stop_result.get("label_valid") is not True:
        raise ValueError("minimal lifecycle requires a valid complete path")
    side = proposal["side"]
    entry = float(proposal["entry_price_um"])
    stop = float(stop_result["stop_price_theoretical_unrounded"])
    if side not in {"LONG", "SHORT"} or entry <= 0 or stop <= 0:
        raise ValueError("invalid side or prices")
    if (side == "LONG" and stop >= entry) or (side == "SHORT" and stop <= entry):
        raise ValueError("stop must be adverse to entry")
    if min(risk_budget_usdt, max_notional_usdt, horizon_minutes) <= 0 or fee_bps_per_fill < 0:
        raise ValueError("invalid sizing, horizon, or fees")
    direction = 1.0 if side == "LONG" else -1.0
    fee = fee_bps_per_fill / 10_000.0

    def net_fraction(exit_reference: float, slippage: float) -> float:
        entry_fill = entry * (1.0 + direction * slippage)
        exit_fill = exit_reference * (1.0 - direction * slippage)
        return (direction * (exit_fill - entry_fill) - fee * (entry_fill + exit_fill)) / entry

    planned_loss_fraction = -net_fraction(stop, max(SLIPPAGE_BPS_PER_FILL) / 10_000.0)
    budget_notional = risk_budget_usdt / planned_loss_fraction
    notional = min(max_notional_usdt, budget_notional)
    initial_risk = notional * planned_loss_fraction
    triggered = bool(stop_result["stop_triggered"])
    exit_reference = float(stop_result["stop_fill_reference_price"] if triggered else stop_result["horizon_close"])
    if exit_reference <= 0:
        raise ValueError("exit reference must be positive")
    entry_time = int(proposal["entry_time"])
    exit_time = int(stop_result["trigger_time"]) + 60 if triggered else entry_time + horizon_minutes * 60
    if not entry_time < exit_time <= entry_time + horizon_minutes * 60:
        raise ValueError("exit must be inside the complete horizon")
    held_hours = (exit_time - entry_time) / 3600.0
    scenarios = {}
    for slippage_bps in SLIPPAGE_BPS_PER_FILL:
        net = notional * net_fraction(exit_reference, slippage_bps / 10_000.0)
        scenarios[f"slippage_{slippage_bps:g}bps_per_fill"] = {
            "net_usdt_no_funding": net,
            "net_r_no_funding": net / initial_risk,
            "net_per_100_reference_notional": net / notional * 100.0,
            "funding_sensitivity_net_usdt": {
                f"cost_{funding_bps:g}bps_per_8h_prorated": net - notional * funding_bps / 10_000.0 * held_hours / 8.0
                for funding_bps in (-1.0, 0.0, 1.0)
            },
        }
    return {
        "exit_reason": "initial_stop" if triggered else "horizon_close",
        "exit_reference_price": exit_reference,
        "exit_time_upper_bound": exit_time,
        "holding_hours_upper_bound": held_hours,
        "reference_notional_usdt": notional,
        "uncapped_risk_budget_notional_usdt": budget_notional,
        "planned_initial_risk_usdt_20bps": initial_risk,
        "notional_cap_binding": budget_notional > max_notional_usdt,
        "gap_through": bool(stop_result["gap_through"]),
        "scenarios": scenarios,
        "label_is_executable_fill": False,
    }


def _post_stop_recovery_after_costs(
    *,
    stop_triggered: bool,
    post_trigger_mfe_pct: float,
    fee_bps_per_fill: float,
    slippage_bps_per_fill: float,
) -> bool:
    round_trip_cost_pct = 2.0 * (fee_bps_per_fill + slippage_bps_per_fill) / 100.0
    return stop_triggered and post_trigger_mfe_pct >= round_trip_cost_pct


def _cluster_bootstrap_rate(
    rows: List[Dict[str, Any]], predicate, *, seed: int, replicates: int,
) -> List[float] | None:
    grouped: Dict[str, List[int]] = defaultdict(list)
    for row in rows:
        grouped[str(row["dedupe_cluster_id"])].append(int(predicate(row)))
    values = [statistics.mean(cluster) for _, cluster in sorted(grouped.items())]
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    samples = sorted(statistics.mean(rng.choice(values) for _ in values) for _ in range(replicates))
    return [samples[int(0.025 * (replicates - 1))], samples[int(0.975 * (replicates - 1))]]


def analyze_entry_stop_widths(
    proposals_path: Path,
    normalized_root: Path,
    output_root: Path,
    *,
    stop_atr_multiples: tuple[float, ...] = DEFAULT_STOP_ATR_MULTIPLES,
    horizon_minutes: int = DEFAULT_HORIZON_MINUTES,
    notional_usdt: float = DEFAULT_NOTIONAL_USDT,
    fee_bps_per_fill: float = DEFAULT_FEE_BPS_PER_FILL,
    max_loss_usdt: float = DEFAULT_MAX_LOSS_USDT,
    replace: bool = False,
) -> Dict[str, Any]:
    proposals_path = Path(proposals_path).resolve()
    normalized_root = Path(normalized_root).resolve()
    output_root = Path(output_root).resolve()
    output_path = output_root / "entry_stop_survival.jsonl"
    report_path = output_root / "report.json"
    if not proposals_path.is_file():
        raise FileNotFoundError(proposals_path)
    if not stop_atr_multiples or any(not math.isfinite(x) or x <= 0 for x in stop_atr_multiples):
        raise ValueError("stop ATR multiples must be finite and positive")
    if not replace and (output_path.exists() or report_path.exists()):
        raise FileExistsError(f"stop survival output exists: {output_root}")

    proposals = list(_rows(proposals_path))
    ids = [str(row.get("opportunity_event_id") or "") for row in proposals]
    if not all(ids) or len(ids) != len(set(ids)):
        raise ValueError("proposal tape contains missing or duplicate event ids")
    for row in proposals:
        if str(row.get("window_role") or "") not in {"development", "validation"}:
            raise ValueError("proposal tape has an unsupported window role")
        if float(row.get("feature_available_at") or 0.0) > float(row.get("decision_time") or 0.0):
            raise ValueError(f"future feature availability: {row['opportunity_event_id']}")

    symbols = sorted({str(row["symbol"]).upper() for row in proposals})
    feed = HistoricalDataFeed(str(normalized_root / "um"))
    hourly_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    hourly_times: Dict[str, List[int]] = {}
    minute_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    minute_times: Dict[str, List[int]] = {}
    minute_open: Dict[str, Dict[int, float]] = {}
    source_hashes = {}
    for symbol in symbols:
        parquet = normalized_root / "um" / f"{symbol}_1m.parquet"
        manifest = normalized_root / "um" / f"{symbol}_1m.manifest.json"
        if not parquet.is_file() or not manifest.is_file():
            raise FileNotFoundError(f"missing UM 1m source for {symbol}")
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
        if manifest_data.get("venue") != "um" or manifest_data.get("interval") != "1m":
            raise ValueError(f"UM source manifest mismatch for {symbol}")
        source_hashes[f"um:{symbol}:1m"] = _sha256(parquet)
        source_hashes[f"um:{symbol}:1m_manifest"] = _sha256(manifest)
        minutes = sorted(feed.get_klines(symbol, "1m"), key=lambda bar: float(bar["open_time"]))
        hours = sorted(feed.get_klines(symbol, "1h"), key=lambda bar: float(bar["open_time"]))
        opens = {}
        for bar in minutes:
            timestamp = int(round(float(bar["open_time"])))
            if timestamp in opens:
                raise ValueError(f"duplicate UM minute timestamp for {symbol}")
            opens[timestamp] = float(bar["open"])
        minute_by_symbol[symbol] = minutes
        minute_times[symbol] = [int(round(float(bar["open_time"]))) for bar in minutes]
        minute_open[symbol] = opens
        hourly_by_symbol[symbol] = hours
        hourly_times[symbol] = [int(round(float(bar["open_time"]))) for bar in hours]

    output_root.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    grouped: Dict[tuple[str, str, str, float], List[Dict[str, Any]]] = defaultdict(list)
    reject_counts = Counter()
    rows_written = 0
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as target:
            for proposal in proposals:
                symbol = str(proposal["symbol"]).upper()
                side = str(proposal.get("side") or "").upper()
                if side not in {"LONG", "SHORT"}:
                    reject_counts["invalid_side"] += 1
                    continue
                decision = int(round(float(proposal["decision_time"])))
                entry_time = int(round(float(proposal.get("entry_time") or decision)))
                entry, atr, rejection = _point_in_time_um_market(
                    symbol, decision, entry_time, hourly_by_symbol[symbol],
                    hourly_times[symbol], minute_open[symbol],
                )
                base_row = {
                    "opportunity_event_id": proposal["opportunity_event_id"],
                    "dedupe_cluster_id": str(proposal.get("dedupe_cluster_id") or ""),
                    "window_id": proposal.get("window_id"),
                    "window_role": proposal.get("window_role"),
                    "family": proposal.get("philosophy_family"),
                    "variant": proposal.get("philosophy_variant"),
                    "thesis_type": proposal.get("thesis_type"),
                    "symbol": symbol,
                    "side": side,
                    "decision_time": decision,
                    "entry_time": entry_time,
                    "label_is_executable_fill": False,
                }
                if rejection:
                    reject_counts[rejection] += 1
                    base_row.update({"label_valid": False, "rejection_reason": rejection})
                    target.write(json.dumps(base_row, ensure_ascii=False, allow_nan=False) + "\n")
                    rows_written += 1
                    continue

                notional = float(notional_usdt)
                quantity = notional / float(entry)
                stop_results = {}
                for multiple in stop_atr_multiples:
                    stop_price = float(entry) - multiple * float(atr) if side == "LONG" else float(entry) + multiple * float(atr)
                    stop_frac = abs(float(entry) - stop_price) / float(entry)
                    if stop_price <= 0:
                        reject_counts["nonpositive_stop"] += 1
                        continue
                    start = bisect.bisect_left(minute_times[symbol], entry_time)
                    path = minute_by_symbol[symbol][start:start + horizon_minutes]
                    replay = replay_entry_stop_only(
                        path, side=side, entry_time=entry_time, entry_price=float(entry),
                        stop_price=stop_price, horizon_minutes=horizon_minutes,
                    )
                    threshold_key = f"{multiple:g}"
                    scenario_results = {}
                    if replay.get("label_valid") is True:
                        for slippage in SLIPPAGE_BPS_PER_FILL:
                            stop_net = (
                                _stop_net_per_100(
                                    side=side, entry=float(entry),
                                    fill_reference=float(replay["stop_fill_reference_price"]),
                                    notional=notional, fee_bps=fee_bps_per_fill,
                                    slippage_bps=slippage,
                                ) if replay["stop_triggered"] else None
                            )
                            scenario_results[f"slippage_{slippage:g}bps_per_fill"] = {
                                "stop_exit_net_per_100": stop_net,
                                "stop_risk_usdt_per_100_notional": notional * stop_frac,
                                "notional_for_max_loss_budget_usdt": _notional_for_max_loss_budget(
                                    max_loss_usdt=max_loss_usdt,
                                    stop_distance_frac=stop_frac,
                                    fee_bps=fee_bps_per_fill,
                                    slippage_bps=slippage,
                                ),
                                "stop_exit_net_usdt_at_max_loss_budget": (
                                    stop_net * _notional_for_max_loss_budget(
                                        max_loss_usdt=max_loss_usdt,
                                        stop_distance_frac=stop_frac,
                                        fee_bps=fee_bps_per_fill,
                                        slippage_bps=slippage,
                                    ) / 100.0 if stop_net is not None else None
                                ),
                            }
                        risk_pct = stop_frac * 100.0
                        mfe_before_r = float(replay["mfe_before_trigger_pct"]) / risk_pct if risk_pct else 0.0
                        full_mfe_r = float(replay["full_horizon_mfe_pct"]) / risk_pct if risk_pct else 0.0
                        mae_r = float(replay["full_horizon_mae_pct"]) / risk_pct if risk_pct else 0.0
                        post_trigger_mfe_pct = float(replay["mfe_after_trigger_bar_pct"] or 0.0)
                        stop_results[threshold_key] = {
                            **replay,
                            "stop_atr_multiple": multiple,
                            "stop_price_theoretical_unrounded": stop_price,
                            "stop_distance_pct": risk_pct,
                            "stop_risk_usdt_per_100_notional": notional * stop_frac,
                            "mfe_before_trigger_r": mfe_before_r,
                            "full_horizon_mfe_r": full_mfe_r,
                            "full_horizon_mae_r": mae_r,
                            "mfe_0_25r_before_stop": mfe_before_r >= 0.25,
                            "mfe_0_5r_before_stop": mfe_before_r >= 0.5,
                            "mfe_1r_before_stop": mfe_before_r >= 1.0,
                            "post_stop_recovery_0_5r_counterfactual": (
                                replay["stop_triggered"] and mfe_before_r < 0.5
                                and post_trigger_mfe_pct / risk_pct >= 0.5
                            ),
                            "post_stop_recovery_after_costs_counterfactual": {
                                f"slippage_{slippage:g}bps_per_fill": (
                                    _post_stop_recovery_after_costs(
                                        stop_triggered=replay["stop_triggered"],
                                        post_trigger_mfe_pct=post_trigger_mfe_pct,
                                        fee_bps_per_fill=fee_bps_per_fill,
                                        slippage_bps_per_fill=slippage,
                                    )
                                )
                                for slippage in SLIPPAGE_BPS_PER_FILL
                            },
                            "stop_scenarios": scenario_results,
                            "historical_exchange_filter_status": "unknown_not_reconstructed",
                        }
                        grouped[(str(proposal.get("philosophy_family") or "unknown"), str(proposal.get("philosophy_variant") or "unknown"), side, multiple)].append({
                            "dedupe_cluster_id": base_row["dedupe_cluster_id"],
                            **stop_results[threshold_key],
                        })
                    else:
                        stop_results[threshold_key] = replay
                base_row.update({
                    "label_valid": all(item.get("label_valid") is True for item in stop_results.values()),
                    "entry_price_um": entry,
                    "atr_24_closed_hours_um": atr,
                    "stop_results_by_atr_multiple": stop_results,
                })
                target.write(json.dumps(base_row, ensure_ascii=False, allow_nan=False) + "\n")
                rows_written += 1
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)

    reports = {}
    for (family, variant, side, multiple), rows in sorted(grouped.items()):
        triggered = [row for row in rows if row["stop_triggered"]]
        recovery = [row for row in triggered if row["post_stop_recovery_0_5r_counterfactual"]]
        stop_nets = [row["stop_scenarios"]["slippage_10bps_per_fill"]["stop_exit_net_per_100"] for row in triggered]
        budget_stop_nets = [row["stop_scenarios"]["slippage_10bps_per_fill"]["stop_exit_net_usdt_at_max_loss_budget"] for row in triggered]
        budget_notionals = [row["stop_scenarios"]["slippage_10bps_per_fill"]["notional_for_max_loss_budget_usdt"] for row in rows]
        key = f"{family}|{variant}|{side}|{multiple:g}ATR"
        reports[key] = {
            "valid_attempts": len(rows),
            "independent_clusters": len({row["dedupe_cluster_id"] for row in rows}),
            "stop_triggered": len(triggered),
            "stop_trigger_rate_pct": 100.0 * len(triggered) / len(rows) if rows else None,
            "stopped_before_0_25r_pct": 100.0 * sum(row["mfe_before_trigger_r"] < 0.25 for row in triggered) / len(triggered) if triggered else None,
            "stopped_before_0_5r_pct": 100.0 * sum(row["mfe_before_trigger_r"] < 0.5 for row in triggered) / len(triggered) if triggered else None,
            "stopped_before_1r_pct": 100.0 * sum(row["mfe_before_trigger_r"] < 1.0 for row in triggered) / len(triggered) if triggered else None,
            "post_stop_recovery_0_5r_counterfactual_pct": 100.0 * len(recovery) / len(triggered) if triggered else None,
            **{
                f"post_stop_recovery_after_costs_slippage_{slippage:g}bps_per_fill_pct": (
                    100.0 * sum(row["post_stop_recovery_after_costs_counterfactual"][f"slippage_{slippage:g}bps_per_fill"] for row in triggered) / len(triggered)
                    if triggered else None
                )
                for slippage in SLIPPAGE_BPS_PER_FILL
            },
            "full_horizon_mfe_0_5r_pct": 100.0 * sum(row["full_horizon_mfe_r"] >= 0.5 for row in rows) / len(rows) if rows else None,
            "median_stop_risk_usdt_per_100_notional": statistics.median(row["stop_risk_usdt_per_100_notional"] for row in rows) if rows else None,
            "mean_stop_exit_net_per_100_when_triggered_10bps": statistics.mean(stop_nets) if stop_nets else None,
            "median_notional_for_max_loss_budget_10bps": statistics.median(budget_notionals) if budget_notionals else None,
            "mean_stop_exit_net_usdt_at_max_loss_budget_when_triggered_10bps": statistics.mean(budget_stop_nets) if budget_stop_nets else None,
            "cluster_bootstrap_stop_before_0_5r_ci95": _cluster_bootstrap_rate(triggered, lambda row: row["mfe_before_trigger_r"] < 0.5, seed=4102, replicates=1000),
        }

    report = {
        "artifact_version": "universal_entry_stop_survival_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "proposals_path": str(proposals_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "proposals_sha256": _sha256(proposals_path),
        "analyzer_sha256": _sha256(Path(__file__).resolve()),
        "normalized_root": str(normalized_root.relative_to(REPO_ROOT)).replace("\\", "/"),
        "source_market_hashes": source_hashes,
        "output_path": str(output_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "output_sha256": _sha256(output_path),
        "proposal_count": len(proposals),
        "rows_written": rows_written,
        "rejection_counts": dict(reject_counts),
        "contract": {
            "venue": "USD-M Futures historical candles; exchange fills not present",
            "entry": "UM 1m open at next contiguous decision_time",
            "atr": "mean true range of last 24 fully closed UM 1h bars available at decision_time",
            "stop_atr_multiples": list(stop_atr_multiples),
            "horizon_minutes": horizon_minutes,
            "quote_notional_usdt": notional_usdt,
            "max_loss_reference_usdt": max_loss_usdt,
            "fee_bps_per_fill_proxy": fee_bps_per_fill,
            "slippage_bps_per_fill": list(SLIPPAGE_BPS_PER_FILL),
            "funding_cost": 0.0,
            "same_minute_stop_rule": "gap-through reference uses bar open; otherwise trigger-price reference; trigger-bar favorable wick is excluded from pre-trigger and post-trigger MFE",
            "take_profit_or_lifecycle_exit": "not modeled",
            "exchange_tick_and_historical_filters": "not reconstructed; theoretical stop levels only",
            "risk_budget_notional": "mechanical sizing to the configured max-loss budget before margin, leverage, exchange filters, and gap risk; not an executable account size",
        },
        "metrics_by_family_variant_side_stop": reports,
        "labels_are_executable_fills": False,
        "winner_claim_allowed": False,
        "limitations": [
            "A stop trigger is not a guaranteed market fill; realized gaps, spread, depth and latency are unknown.",
            "Historical exchange tick/trigger filters, Testnet account state, fees and funding are not reconstructed.",
            "Post-stop recovery is measured only on bars after the trigger bar; it is counterfactual and not a profit the closed position could realize.",
            "No TP, trailing stop, partial close or lifecycle policy is simulated; this isolates only the initial entry stop.",
            "The 1.5 ATR floor is an internal strategy gate, not a universal Binance minimum stop distance.",
        ],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_temp = report_path.with_name(f".{report_path.name}.{uuid.uuid4().hex}.tmp")
    report_temp.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    report_temp.replace(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposals", type=Path, required=True)
    parser.add_argument("--normalized-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stop-atr-multiples", default=",".join(str(x) for x in DEFAULT_STOP_ATR_MULTIPLES))
    parser.add_argument("--horizon-minutes", type=int, default=DEFAULT_HORIZON_MINUTES)
    parser.add_argument("--notional-usdt", type=float, default=DEFAULT_NOTIONAL_USDT)
    parser.add_argument("--fee-bps-per-fill", type=float, default=DEFAULT_FEE_BPS_PER_FILL)
    parser.add_argument("--max-loss-usdt", type=float, default=DEFAULT_MAX_LOSS_USDT)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    multiples = tuple(float(value) for value in args.stop_atr_multiples.split(",") if value.strip())
    try:
        report = analyze_entry_stop_widths(
            args.proposals, args.normalized_root, args.output,
            stop_atr_multiples=multiples, horizon_minutes=args.horizon_minutes,
            notional_usdt=args.notional_usdt, fee_bps_per_fill=args.fee_bps_per_fill,
            max_loss_usdt=args.max_loss_usdt, replace=args.replace,
        )
    except (OSError, ValueError, KeyError) as exc:
        parser.error(f"entry stop survival analysis failed: {type(exc).__name__}: {str(exc)[:300]}")
    print(json.dumps({"proposal_count":report["proposal_count"],"rows_written":report["rows_written"],"report_path":report["report_path"],"winner_claim_allowed":report["winner_claim_allowed"]},ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())