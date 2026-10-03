"""Score philosophy proposal tapes with one shared point-in-time ATR contract."""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import random
import statistics
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from simulation.entry_outcome_resolver import resolve_contract_path
from simulation.mimic_feed import HistoricalDataFeed


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HORIZON_MINUTES = 240
DEFAULT_STOP_ATR = 1.5
DEFAULT_TARGET_ATR = 2.0
DEFAULT_NOTIONAL_USDT = 100.0
DEFAULT_FEE_BPS_PER_FILL = 5.0
SLIPPAGE_BPS_PER_FILL = (0.0, 5.0, 10.0, 20.0)
ATR_LOOKBACK_BARS = 24
BOOTSTRAP_REPLICATES = 2000


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


def fixed_atr_contract(side: str, entry_price: float, atr: float) -> Dict[str, float]:
    normalized_side = str(side or "").strip().upper()
    if normalized_side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    entry = float(entry_price)
    volatility = float(atr)
    if entry <= 0 or volatility <= 0:
        raise ValueError("entry and ATR must be positive")
    direction = 1.0 if normalized_side == "LONG" else -1.0
    return {
        "entry_price": entry,
        "stop_price": entry - direction * DEFAULT_STOP_ATR * volatility,
        "target_price": entry + direction * DEFAULT_TARGET_ATR * volatility,
        "stop_atr_multiple": DEFAULT_STOP_ATR,
        "target_atr_multiple": DEFAULT_TARGET_ATR,
    }


def _point_in_time_um_market(
    symbol: str,
    decision_time: int,
    entry_time: int,
    hourly: List[Dict[str, Any]],
    hourly_times: List[int],
    minute_open_by_time: Dict[int, float],
) -> Tuple[float | None, float | None, str | None]:
    source_time = decision_time - 3600
    source_index = bisect.bisect_left(hourly_times, source_time)
    if source_index >= len(hourly_times) or hourly_times[source_index] != source_time:
        return None, None, "SOURCE_HOUR_MISSING"
    if entry_time != decision_time:
        return None, None, "ENTRY_NOT_NEXT_HOUR_OPEN"
    context_start = source_index - ATR_LOOKBACK_BARS
    if context_start < 0:
        return None, None, "ATR_WARMUP_MISSING"
    context = hourly[context_start:source_index + 1]
    context_times = hourly_times[context_start:source_index + 1]
    if len(context) != ATR_LOOKBACK_BARS + 1:
        return None, None, "ATR_CONTEXT_INCOMPLETE"
    if any(right - left != 3600 for left, right in zip(context_times, context_times[1:])):
        return None, None, "ATR_CONTEXT_GAP"
    if entry_time not in minute_open_by_time:
        return None, None, "UM_ENTRY_MINUTE_MISSING"

    true_ranges = []
    for index in range(1, len(context)):
        bar = context[index]
        previous_close = float(context[index - 1]["close"])
        high = float(bar["high"])
        low = float(bar["low"])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    atr = statistics.mean(true_ranges)
    entry = float(minute_open_by_time[entry_time])
    if entry <= 0 or atr <= 0:
        return None, None, "INVALID_ENTRY_OR_ATR"
    return entry, atr, None


def _cluster_bootstrap_ci(rows: List[Dict[str, Any]], *, seed: int, replicates: int) -> List[float] | None:
    by_cluster: Dict[str, List[float]] = defaultdict(list)
    for row in rows:
        by_cluster[str(row["dedupe_cluster_id"])].append(float(row["net_per_100"]))
    cluster_values = [statistics.mean(values) for _, values in sorted(by_cluster.items())]
    if len(cluster_values) < 2:
        return None
    generator = random.Random(seed)
    estimates = []
    for _ in range(replicates):
        sample = [generator.choice(cluster_values) for _ in cluster_values]
        estimates.append(statistics.mean(sample))
    estimates.sort()
    lower_index = int(0.025 * (replicates - 1))
    upper_index = int(0.975 * (replicates - 1))
    return [estimates[lower_index], estimates[upper_index]]


def _summarize(rows: List[Dict[str, Any]], *, total_proposals: int, seed: int, replicates: int) -> Dict[str, Any]:
    valid = [row for row in rows if row.get("label_valid") is True]
    outcomes = Counter(str(row.get("outcome")) for row in valid)
    nets = [float(row["net_per_100"]) for row in valid]
    winners = [value for value in nets if value > 0]
    losers = [value for value in nets if value < 0]
    positive_sum = sum(winners)
    negative_sum = abs(sum(losers))
    clusters = {str(row["dedupe_cluster_id"]) for row in valid}
    return {
        "proposal_count": total_proposals,
        "valid_attempts": len(valid),
        "independent_clusters": len(clusters),
        "coverage_pct": 100.0 * len(valid) / total_proposals if total_proposals else 0.0,
        "TP": outcomes["TP"],
        "SL": outcomes["SL"],
        "EXPIRY": outcomes["EXPIRY"],
        "tp_first_rate_pct": 100.0 * outcomes["TP"] / len(valid) if valid else None,
        "net_positive_rate_pct": 100.0 * len(winners) / len(valid) if valid else None,
        "mean_net_per_100": statistics.mean(nets) if nets else None,
        "mean_net_per_proposal_including_abstentions": sum(nets) / total_proposals if total_proposals else None,
        "net_profit_factor": positive_sum / negative_sum if negative_sum else None,
        "cluster_bootstrap_mean_net_ci95": _cluster_bootstrap_ci(valid, seed=seed, replicates=replicates),
    }


def score_proposal_tape(
    proposals_path: Path,
    normalized_root: Path,
    output_root: Path,
    *,
    replace: bool = False,
    horizon_minutes: int = DEFAULT_HORIZON_MINUTES,
    notional_usdt: float = DEFAULT_NOTIONAL_USDT,
    fee_bps_per_fill: float = DEFAULT_FEE_BPS_PER_FILL,
    bootstrap_replicates: int = BOOTSTRAP_REPLICATES,
) -> Dict[str, Any]:
    proposals_path = Path(proposals_path).resolve()
    normalized_root = Path(normalized_root).resolve()
    output_root = Path(output_root).resolve()
    output_path = output_root / "fixed_contract_outcomes.jsonl"
    report_path = output_root / "report.json"
    if not proposals_path.is_file():
        raise FileNotFoundError(proposals_path)
    if horizon_minutes <= 0 or horizon_minutes % 60:
        raise ValueError("horizon_minutes must be a positive multiple of 60")
    if notional_usdt <= 0 or fee_bps_per_fill < 0 or bootstrap_replicates < 100:
        raise ValueError("invalid notional, fee, or bootstrap configuration")
    if not replace and (output_path.exists() or report_path.exists()):
        raise FileExistsError(f"fixed-contract output exists: {output_root}")

    proposals = list(_rows(proposals_path))
    event_ids = [str(row.get("opportunity_event_id") or "") for row in proposals]
    if not all(event_ids) or len(event_ids) != len(set(event_ids)):
        raise ValueError("proposal tape contains missing or duplicate opportunity_event_id")
    for row in proposals:
        if str(row.get("window_role") or "") not in {"development", "validation"}:
            raise ValueError("proposal tape has an unsupported window_role")
        if float(row.get("feature_available_at") or 0.0) > float(row.get("decision_time") or 0.0):
            raise ValueError(f"future feature availability: {row['opportunity_event_id']}")

    symbols = sorted({str(row["symbol"]).upper() for row in proposals})
    feed = HistoricalDataFeed(str(normalized_root / "um"))
    hourly_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    hourly_times_by_symbol: Dict[str, List[int]] = {}
    minute_open_by_symbol: Dict[str, Dict[int, float]] = {}
    minute_bars_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    minute_times_by_symbol: Dict[str, List[int]] = {}
    source_hashes: Dict[str, str] = {}
    for symbol in symbols:
        parquet = normalized_root / "um" / f"{symbol}_1m.parquet"
        manifest = normalized_root / "um" / f"{symbol}_1m.manifest.json"
        if not parquet.is_file() or not manifest.is_file():
            raise FileNotFoundError(f"missing UM 1m market source for {symbol}")
        source_hashes[f"um:{symbol}:1m"] = _sha256(parquet)
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
        minute_bars = sorted(feed.get_klines(symbol, "1m"), key=lambda bar: float(bar["open_time"]))
        hourly_bars = sorted(feed.get_klines(symbol, "1h"), key=lambda bar: float(bar["open_time"]))
        minute_open: Dict[int, float] = {}
        duplicate_times = set()
        for bar in minute_bars:
            timestamp = int(round(float(bar["open_time"])))
            if timestamp in minute_open:
                duplicate_times.add(timestamp)
            minute_open[timestamp] = float(bar["open"])
        if duplicate_times:
            raise ValueError(f"duplicate UM 1m timestamps for {symbol}")
        hourly_by_symbol[symbol] = hourly_bars
        hourly_times_by_symbol[symbol] = [int(round(float(bar["open_time"]))) for bar in hourly_bars]
        minute_open_by_symbol[symbol] = minute_open
        minute_bars_by_symbol[symbol] = minute_bars
        minute_times_by_symbol[symbol] = [int(round(float(bar["open_time"]))) for bar in minute_bars]
        source_hashes[f"um:{symbol}:1m_manifest"] = _sha256(manifest)
        if manifest_data.get("venue") != "um" or manifest_data.get("interval") != "1m":
            raise ValueError(f"UM source manifest mismatch for {symbol}")

    output_root.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    grouped_rows: Dict[Tuple[str, str, str, float], List[Dict[str, Any]]] = defaultdict(list)
    rejection_counts = Counter()
    rows_written = 0
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as target:
            for proposal in proposals:
                symbol = str(proposal["symbol"]).upper()
                side = str(proposal.get("side") or "").upper()
                if side not in {"LONG", "SHORT"}:
                    rejection_counts["invalid_side"] += 1
                    continue
                decision_time = int(round(float(proposal["decision_time"])))
                entry_time = int(round(float(proposal.get("entry_time") or decision_time)))
                entry, atr, rejection = _point_in_time_um_market(
                    symbol,
                    decision_time,
                    entry_time,
                    hourly_by_symbol[symbol],
                    hourly_times_by_symbol[symbol],
                    minute_open_by_symbol[symbol],
                )
                base_row: Dict[str, Any] = {
                    "opportunity_event_id": proposal["opportunity_event_id"],
                    "dedupe_cluster_id": str(proposal.get("dedupe_cluster_id") or ""),
                    "window_id": proposal.get("window_id"),
                    "window_role": proposal.get("window_role"),
                    "family": proposal.get("philosophy_family"),
                    "variant": proposal.get("philosophy_variant"),
                    "thesis_type": proposal.get("thesis_type"),
                    "symbol": symbol,
                    "side": side,
                    "decision_time": decision_time,
                    "entry_time": entry_time,
                    "label_status": "experimental_fixed_contract_not_executable_fill",
                }
                if rejection:
                    rejection_counts[rejection] += 1
                    base_row.update({"label_valid": False, "rejection_reason": rejection})
                    target.write(json.dumps(base_row, ensure_ascii=False, allow_nan=False) + "\n")
                    rows_written += 1
                    continue

                contract = fixed_atr_contract(side, float(entry), float(atr))
                quantity = notional_usdt / float(entry)
                minute_bars = minute_bars_by_symbol[symbol]
                minute_times = minute_times_by_symbol[symbol]
                start_index = bisect.bisect_left(minute_times, entry_time)
                end_index = bisect.bisect_left(minute_times, entry_time + horizon_minutes * 60)
                path = minute_bars[start_index:end_index]
                scenario_results = {}
                for slippage in SLIPPAGE_BPS_PER_FILL:
                    result = resolve_contract_path(
                        path,
                        side=side,
                        entry_time=entry_time,
                        entry_price=float(entry),
                        stop_price=contract["stop_price"],
                        target_price=contract["target_price"],
                        horizon_minutes=horizon_minutes,
                        quantity=quantity,
                        entry_fee_bps=fee_bps_per_fill,
                        exit_fee_bps=fee_bps_per_fill,
                        slippage_bps=slippage,
                        funding_cost_usdt=0.0,
                        bar_seconds=60,
                        ambiguous_policy="stop_first",
                    )
                    scenario_results[f"slippage_{slippage:g}bps_per_fill"] = {
                        "outcome": result.get("outcome"),
                        "label_valid": result.get("label_valid") is True,
                        "net_pnl_per_100": float(result.get("net_pnl_usdt") or 0.0) / notional_usdt * 100.0 if result.get("label_valid") is True else None,
                        "exit_time": result.get("exit_time"),
                        "ambiguous_intrabar": result.get("ambiguous_intrabar", False),
                        "gap_through": result.get("gap_through", False),
                    }
                primary = scenario_results["slippage_10bps_per_fill"]
                base_row.update({
                    "label_valid": primary["label_valid"],
                    "rejection_reason": None if primary["label_valid"] else "REPLAY_CENSORED_OR_DATA_GAP",
                    "entry_price_um": float(entry),
                    "atr_24_closed_hours_um": float(atr),
                    "stop_price": contract["stop_price"],
                    "target_price": contract["target_price"],
                    "stop_atr_multiple": DEFAULT_STOP_ATR,
                    "target_atr_multiple": DEFAULT_TARGET_ATR,
                    "quantity_at_fixed_quote_notional": quantity,
                    "outcomes_by_cost_scenario": scenario_results,
                })
                if primary["label_valid"]:
                    group_key = (
                        str(proposal.get("philosophy_family") or "unknown"),
                        str(proposal.get("philosophy_variant") or "unknown"),
                        side,
                        f"slippage_{10:g}bps_per_fill",
                        float(proposal.get("ev_horizon_minutes") or horizon_minutes),
                    )
                    grouped_rows[group_key].append({
                        "dedupe_cluster_id": base_row["dedupe_cluster_id"],
                        "outcome": primary["outcome"],
                        "label_valid": True,
                        "net_per_100": primary["net_pnl_per_100"],
                    })
                else:
                    rejection_counts["replay_censored_or_gap"] += 1
                target.write(json.dumps(base_row, ensure_ascii=False, allow_nan=False) + "\n")
                rows_written += 1
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)

    report_groups = {}
    for (family, variant, side, scenario, horizon), rows in sorted(grouped_rows.items()):
        total_proposals = sum(
            1 for proposal in proposals
            if str(proposal.get("philosophy_family") or "unknown") == family
            and str(proposal.get("philosophy_variant") or "unknown") == variant
            and str(proposal.get("side") or "").upper() == side
        )
        report_groups[f"{family}|{variant}|{side}|{scenario}|{horizon:g}m"] = _summarize(
            rows,
            total_proposals=total_proposals,
            seed=4071,
            replicates=bootstrap_replicates,
        )
    report = {
        "artifact_version": "universal_entry_philosophy_fixed_contract_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "proposals_path": str(proposals_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "proposals_sha256": _sha256(proposals_path),
        "normalized_root": str(normalized_root.relative_to(REPO_ROOT)).replace("\\", "/"),
        "source_market_hashes": source_hashes,
        "output_path": str(output_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "output_sha256": _sha256(output_path),
        "proposals": len(proposals),
        "rows_written": rows_written,
        "rejection_counts": dict(rejection_counts),
        "contract": {
            "entry": "UM 1m open at next contiguous decision_time",
            "atr": "mean true range of last 24 fully closed UM 1h bars available at decision_time",
            "stop_atr_multiple": DEFAULT_STOP_ATR,
            "target_atr_multiple": DEFAULT_TARGET_ATR,
            "horizon_minutes": horizon_minutes,
            "quote_notional_usdt": notional_usdt,
            "fee_bps_per_fill": fee_bps_per_fill,
            "slippage_bps_per_fill": list(SLIPPAGE_BPS_PER_FILL),
            "funding_cost": 0.0,
            "same_bar_policy": "stop_first",
        },
        "metrics_by_family_side_scenario_horizon": report_groups,
        "labels_are_executable_fills": False,
        "winner_claim_allowed": False,
        "limitations": [
            "All rows are experimental fixed-contract replay outcomes, not exchange fills.",
            "Historical filters, account state, executable fees, funding and fills are not reconstructed.",
            "Different opportunity generators have different candidate coverage; compare coverage and net per proposal, not TP-first alone.",
            "The fixed contract isolates entry direction/selection from thesis-specific target geometry; it is not a production exit recommendation.",
        ],
    }
    temporary_report = report_path.with_name(f".{report_path.name}.{uuid.uuid4().hex}.tmp")
    temporary_report.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    temporary_report.replace(report_path)
    report["report_path"] = str(report_path.relative_to(REPO_ROOT)).replace("\\", "/")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposals", type=Path, required=True)
    parser.add_argument("--normalized-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--horizon-minutes", type=int, default=DEFAULT_HORIZON_MINUTES)
    parser.add_argument("--notional-usdt", type=float, default=DEFAULT_NOTIONAL_USDT)
    parser.add_argument("--fee-bps-per-fill", type=float, default=DEFAULT_FEE_BPS_PER_FILL)
    parser.add_argument("--bootstrap-replicates", type=int, default=BOOTSTRAP_REPLICATES)
    args = parser.parse_args()
    try:
        report = score_proposal_tape(
            args.proposals,
            args.normalized_root,
            args.output,
            replace=args.replace,
            horizon_minutes=args.horizon_minutes,
            notional_usdt=args.notional_usdt,
            fee_bps_per_fill=args.fee_bps_per_fill,
            bootstrap_replicates=args.bootstrap_replicates,
        )
    except (OSError, ValueError, KeyError) as exc:
        parser.error(f"fixed-contract scoring failed: {type(exc).__name__}: {str(exc)[:300]}")
    print(json.dumps({
        "proposals": report["proposals"],
        "rows_written": report["rows_written"],
        "report_path": report["report_path"],
        "winner_claim_allowed": report["winner_claim_allowed"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())