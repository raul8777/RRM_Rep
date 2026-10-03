"""Build a non-executable four-venue contract scaffold from point-in-time proposals."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set

from simulation.mimic_feed import HistoricalDataFeed

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_INPUT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "pilot_development" / "philosophy_proposals.jsonl"
DEFAULT_OUTPUT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "pilot_development" / "contract_scaffold.jsonl"
DEFAULT_NORMALIZED_ROOT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "normalized" / "pilot_development"
STOP_ATR_MULTIPLE = 1.5
REWARD_ATR_MULTIPLE = 2.0
CONTRACT_MODES = (
    ("SPOT_LONG", "LONG", "spot"),
    ("FUTURES_LONG", "LONG", "um"),
    ("FUTURES_SHORT", "SHORT", "um"),
    ("SPOT_MARGIN_SHORT", "SHORT", "spot_margin"),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(resolved)


def _rows(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid proposal JSON at line {line_number}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"proposal at line {line_number} is not an object")
            yield row


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _venue_market_snapshot(
    symbol: str,
    decision_time: Any,
    entry_time: Any,
    *,
    venue: str,
    hourly_bars: List[Dict[str, Any]],
    minute_open_by_time: Dict[int, float],
    ambiguous_minute_times: Set[int],
) -> Dict[str, Any]:
    try:
        decision = float(decision_time)
        entry = float(entry_time)
    except (TypeError, ValueError):
        return {"status": "missing_point_in_time_venue_bars", "reason": "invalid_decision_or_entry_time"}
    source_indices = [index for index, bar in enumerate(hourly_bars) if float(bar["open_time"]) + 3600 == decision]
    if len(source_indices) != 1:
        return {"status": "missing_point_in_time_venue_bars", "reason": "source_hour_not_unique"}
    source_index = source_indices[0]
    if source_index < 25:
        return {"status": "missing_point_in_time_venue_bars", "reason": "insufficient_venue_warmup"}
    context = hourly_bars[source_index - 24:source_index + 1]
    context_times = [float(bar["open_time"]) for bar in context]
    if len(context) != 25 or any(right - left != 3600 for left, right in zip(context_times, context_times[1:])):
        return {"status": "missing_point_in_time_venue_bars", "reason": "noncontiguous_venue_hourly_context"}
    source_close = float(context[-1]["close"])
    rolling_closes = [float(bar["close"]) for bar in context[-24:]]
    rolling_mean = sum(rolling_closes) / len(rolling_closes)
    previous_bars = context[:-1]
    true_ranges = []
    previous_close = float(previous_bars[0]["close"])
    for bar in previous_bars[1:]:
        high = float(bar["high"])
        low = float(bar["low"])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        previous_close = float(bar["close"])
    if len(true_ranges) != 23:
        return {"status": "missing_point_in_time_venue_bars", "reason": "atr_context_incomplete"}
    first_bar = previous_bars[0]
    first_previous = float(hourly_bars[source_index - 25]["close"]) if source_index >= 25 else float(first_bar["open"])
    first_high = float(first_bar["high"])
    first_low = float(first_bar["low"])
    true_ranges.insert(0, max(first_high - first_low, abs(first_high - first_previous), abs(first_low - first_previous)))
    atr = sum(true_ranges) / 24

    entry_timestamp = int(entry)
    if entry_timestamp in ambiguous_minute_times or entry_timestamp not in minute_open_by_time:
        return {"status": "missing_point_in_time_venue_bars", "reason": "venue_entry_minute_not_unique"}
    entry_price = float(minute_open_by_time[entry_timestamp])
    if entry_price <= 0 or atr <= 0 or source_close <= 0:
        return {"status": "missing_point_in_time_venue_bars", "reason": "nonpositive_venue_market_value"}
    return {
        "status": "venue_market_snapshot_ready",
        "venue": venue,
        "venue_symbol": symbol,
        "decision_time": decision,
        "entry_time": entry,
        "entry_price": entry_price,
        "entry_price_source": "venue_1m_open_at_entry_time",
        "atr_24h": atr,
        "rolling_mean_24h": rolling_mean,
        "prior_24h_high": max(float(bar["high"]) for bar in previous_bars),
        "prior_24h_low": min(float(bar["low"]) for bar in previous_bars),
        "source_hour_close": source_close,
        "source_hourly_bars": 25,
    }


def _geometry(proposal: Dict[str, Any], side: str, market: Dict[str, Any]) -> Dict[str, Any]:
    if market.get("status") != "venue_market_snapshot_ready":
        return {
            "status": "missing_point_in_time_venue_bars",
            "reason": market.get("reason", "venue_market_snapshot_unavailable"),
            "entry_price": None,
            "stop_price": None,
            "target_price": None,
        }
    entry = _number(market.get("entry_price"))
    atr = _number(market.get("atr_24h"))
    rolling_mean = _number(market.get("rolling_mean_24h"))
    prior_high = _number(market.get("prior_24h_high"))
    prior_low = _number(market.get("prior_24h_low"))
    proposal_side = str(proposal.get("side") or "").upper()
    thesis = str(proposal.get("thesis_type") or "")
    if entry is None or entry <= 0 or atr is None or atr <= 0:
        return {"status": "missing_point_in_time_entry_or_atr", "entry_price": entry, "stop_price": None, "target_price": None}
    if side != proposal_side:
        return {
            "status": "independent_thesis_pending",
            "entry_price": entry,
            "stop_price": None,
            "target_price": None,
            "source_thesis": thesis,
        }

    if side == "LONG":
        if "MEAN_REVERSION" in thesis and rolling_mean is not None:
            target = rolling_mean
            stop = entry - STOP_ATR_MULTIPLE * atr
        elif "BREAKOUT" in thesis and prior_high is not None:
            target = max(entry + REWARD_ATR_MULTIPLE * atr, prior_high + REWARD_ATR_MULTIPLE * atr)
            stop = min(entry - STOP_ATR_MULTIPLE * atr, prior_high - STOP_ATR_MULTIPLE * atr)
        else:
            target = entry + REWARD_ATR_MULTIPLE * atr
            stop = entry - STOP_ATR_MULTIPLE * atr
    else:
        if "MEAN_REVERSION" in thesis and rolling_mean is not None:
            target = rolling_mean
            stop = entry + STOP_ATR_MULTIPLE * atr
        elif "BREAKDOWN" in thesis and prior_low is not None:
            target = min(entry - REWARD_ATR_MULTIPLE * atr, prior_low - REWARD_ATR_MULTIPLE * atr)
            stop = max(entry + STOP_ATR_MULTIPLE * atr, prior_low + STOP_ATR_MULTIPLE * atr)
        else:
            target = entry - REWARD_ATR_MULTIPLE * atr
            stop = entry + STOP_ATR_MULTIPLE * atr
    geometry_valid = (stop < entry < target) if side == "LONG" else (target < entry < stop)
    return {
        "status": "derived_point_in_time_geometry" if geometry_valid else "invalid_derived_geometry",
        "invalid_reason": None if geometry_valid else "target_or_stop_not_favorable_for_side",
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
        "stop_atr_multiple": abs(entry - stop) / atr,
        "target_atr_multiple": abs(target - entry) / atr,
        "geometry_policy": {
            "minimum_stop_atr_multiple": STOP_ATR_MULTIPLE,
            "base_reward_atr_multiple": REWARD_ATR_MULTIPLE,
            "target_rule": "thesis_specific_point_in_time_level_or_base_reward",
        },
        "geometry_valid": geometry_valid,
        "geometry_source": "venue_specific_point_in_time_ohlcv_v1",
        "venue": market.get("venue"),
        "venue_symbol": market.get("venue_symbol"),
        "entry_price_source": market.get("entry_price_source"),
        "source_hour_close": market.get("source_hour_close"),
        "source_hourly_bars": market.get("source_hourly_bars"),
    }


def build_scaffold(
    input_path: Path = DEFAULT_INPUT,
    output_path: Path = DEFAULT_OUTPUT,
    *,
    replace: bool = False,
    normalized_root: Path = DEFAULT_NORMALIZED_ROOT,
) -> Dict[str, Any]:
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    report_path = output_path.with_suffix(".report.json")
    if not replace and (output_path.exists() or report_path.exists()):
        raise FileExistsError(f"scaffold exists; pass --replace: {output_path}")
    preflight_ids = set()
    source_event_ids = set()
    for proposal in _rows(input_path):
        event_id = str(proposal.get("opportunity_event_id") or "")
        thesis_id = str(proposal.get("bilateral_thesis_id") or event_id)
        if not event_id or not thesis_id or thesis_id in preflight_ids:
            raise ValueError("duplicate or missing opportunity_event_id/bilateral_thesis_id")
        preflight_ids.add(thesis_id)
        source_event_ids.add(event_id)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    seen = set()
    proposal_count = 0
    status_counts: Counter[str] = Counter()
    mode_counts: Counter[str] = Counter()
    market_data_cache: Dict[tuple[str, str, float, float], Dict[str, Any]] = {}
    market_series_cache: Dict[tuple[str, str], tuple[List[Dict[str, Any]], Dict[int, float], Set[int]]] = {}
    manifest_cache: Dict[tuple[str, str], Dict[str, Any]] = {}
    source_manifest_hashes: Dict[str, str] = {}
    source_parquet_hashes: Dict[str, str] = {}
    hourly_source_modes: Dict[str, str] = {}
    feeds = {
        "spot": HistoricalDataFeed(str(normalized_root / "spot")),
        "um": HistoricalDataFeed(str(normalized_root / "um")),
    }
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as target:
            for proposal in _rows(input_path):
                event_id = str(proposal.get("opportunity_event_id") or "")
                thesis_id = str(proposal.get("bilateral_thesis_id") or event_id)
                if not event_id or not thesis_id or thesis_id in seen:
                    raise ValueError("duplicate or missing opportunity_event_id/bilateral_thesis_id")
                seen.add(thesis_id)
                contracts = []
                for mode, side, venue in CONTRACT_MODES:
                    data_venue = "um" if venue == "um" else "spot"
                    try:
                        decision_time = float(proposal.get("decision_time"))
                        entry_time = float(proposal.get("entry_time", decision_time))
                    except (TypeError, ValueError):
                        decision_time = entry_time = float("nan")
                    manifest_key = (data_venue, str(proposal.get("symbol") or ""))
                    if manifest_key not in manifest_cache:
                        manifest_path = normalized_root / data_venue / f"{manifest_key[1]}_1m.manifest.json"
                        manifest_cache[manifest_key] = {}
                        if manifest_path.is_file():
                            manifest_cache[manifest_key] = {
                                "contract_symbol": json.loads(manifest_path.read_text(encoding="utf-8")).get("contract_symbol"),
                                "sha256": _sha256(manifest_path),
                            }
                            source_manifest_hashes[f"{data_venue}:{manifest_key[1]}"] = manifest_cache[manifest_key]["sha256"]
                        for interval in ("1m", "1h"):
                            parquet_path = normalized_root / data_venue / f"{manifest_key[1]}_{interval}.parquet"
                            if interval == "1m" and not parquet_path.is_file():
                                raise FileNotFoundError(parquet_path)
                            if parquet_path.is_file():
                                source_parquet_hashes[f"{data_venue}:{manifest_key[1]}:{interval}"] = _sha256(parquet_path)
                            if interval == "1h":
                                hourly_source_modes[f"{data_venue}:{manifest_key[1]}"] = "parquet_1h" if parquet_path.is_file() else "resampled_from_1m"
                    manifest_info = manifest_cache[manifest_key]
                    venue_symbol = str(proposal.get("symbol") or "")
                    if manifest_info.get("contract_symbol"):
                        venue_symbol = str(manifest_info["contract_symbol"])
                    if mode == "SPOT_MARGIN_SHORT":
                        geometry = {
                            "status": "venue_policy_disabled",
                            "reason": "spot_margin_short_disabled_by_pilot_policy",
                            "entry_price": None,
                            "stop_price": None,
                            "target_price": None,
                        }
                        status = "venue_policy_disabled_and_historical_state_unknown"
                    else:
                        market_key = (data_venue, str(proposal.get("symbol") or ""), decision_time, entry_time)
                        if market_key not in market_data_cache:
                            series_key = (data_venue, market_key[1])
                            if series_key not in market_series_cache:
                                hourly_bars = feeds[data_venue].get_klines(series_key[1], "1h")
                                minute_bars = feeds[data_venue].get_klines(series_key[1], "1m")
                                minute_open_by_time: Dict[int, float] = {}
                                ambiguous_minute_times: Set[int] = set()
                                for bar in minute_bars:
                                    timestamp = int(float(bar["open_time"]))
                                    if timestamp in minute_open_by_time:
                                        ambiguous_minute_times.add(timestamp)
                                    else:
                                        minute_open_by_time[timestamp] = float(bar["open"])
                                market_series_cache[series_key] = (hourly_bars, minute_open_by_time, ambiguous_minute_times)
                            hourly_bars, minute_open_by_time, ambiguous_minute_times = market_series_cache[series_key]
                            market_data_cache[market_key] = _venue_market_snapshot(
                                market_key[1], decision_time, entry_time,
                                venue=data_venue,
                                hourly_bars=hourly_bars,
                                minute_open_by_time=minute_open_by_time,
                                ambiguous_minute_times=ambiguous_minute_times,
                            )
                        geometry = _geometry(proposal, side, market_data_cache[market_key])
                        if geometry["status"] == "independent_thesis_pending":
                            status = geometry["status"]
                        elif geometry["status"] != "derived_point_in_time_geometry":
                            status = geometry["status"]
                        else:
                            status = "geometry_scaffold_only_missing_probability_filters_costs_account"
                    status_counts[status] += 1
                    mode_counts[mode] += 1
                    contracts.append({
                        "mode": mode,
                        "side": side,
                        "venue": venue,
                        "contract_symbol": venue_symbol,
                        "source_market_manifest_key": f"{data_venue}:{manifest_key[1]}",
                        "source_market_manifest_sha256": manifest_info.get("sha256"),
                        "geometry": geometry,
                        "probability": None,
                        "calibration_status": "pending_train_fold_calibration",
                        "venue_executable_at_time": None,
                        "selection_eligible": False,
                        "missing_evidence": [
                            "calibrated_probability",
                            "historical_exchange_filter_snapshot",
                            "historical_account_balance_or_borrow_state",
                            "entry_cost_snapshot",
                        ],
                    })
                target.write(json.dumps({
                    "opportunity_event_id": event_id,
                    "bilateral_thesis_id": proposal.get("bilateral_thesis_id"),
                    "dedupe_cluster_id": proposal.get("dedupe_cluster_id"),
                    "window_id": proposal.get("window_id"),
                    "window_role": proposal.get("window_role"),
                    "symbol": proposal.get("symbol"),
                    "decision_time": proposal.get("decision_time"),
                    "philosophy_family": proposal.get("philosophy_family"),
                    "philosophy_variant": proposal.get("philosophy_variant"),
                    "thesis_role": proposal.get("thesis_role", "observed"),
                    "thesis_type": proposal.get("thesis_type"),
                    "proposal_side": proposal.get("side"),
                    "entry_time": proposal.get("entry_time"),
                    "feature_available_at": proposal.get("feature_available_at"),
                    "contracts": contracts,
                    "tape_eligible": False,
                }, ensure_ascii=False, allow_nan=False) + "\n")
                proposal_count += 1
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)
    report = {
        "artifact_version": "universal_entry_contract_scaffold_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_path": _display_path(input_path),
        "input_sha256": _sha256(input_path),
        "normalized_root": _display_path(normalized_root),
        "market_source_intervals": ["1h", "1m"],
        "source_market_manifest_hashes": dict(sorted(source_manifest_hashes.items())),
        "source_market_parquet_hashes": dict(sorted(source_parquet_hashes.items())),
        "hourly_source_modes": dict(sorted(hourly_source_modes.items())),
        "output_path": _display_path(output_path),
        "output_sha256": _sha256(output_path),
        "proposal_count": proposal_count,
        "unique_source_event_count": len(source_event_ids),
        "bilateral_thesis_count": sum(bool(row.get("bilateral_thesis_id")) for row in _rows(input_path)),
        "contract_count": proposal_count * len(CONTRACT_MODES),
        "counts_by_mode": dict(sorted(mode_counts.items())),
        "counts_by_status": dict(sorted(status_counts.items())),
        "all_contracts_non_executable": True,
        "policy": {"stop_atr_multiple": STOP_ATR_MULTIPLE, "reward_atr_multiple": REWARD_ATR_MULTIPLE},
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--normalized-root", type=Path, default=DEFAULT_NORMALIZED_ROOT)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    try:
        report = build_scaffold(args.input, args.output, replace=args.replace, normalized_root=args.normalized_root)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
