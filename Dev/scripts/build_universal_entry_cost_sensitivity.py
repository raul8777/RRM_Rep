"""Build a non-executable cost sensitivity over bilateral development price-path labels."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
PILOT_ROOT = REPO_ROOT / "Dev/benchmarks/universal_entry_selection/pilot_development"
OUTCOMES = PILOT_ROOT / "bilateral_scaffold_outcomes.jsonl"
FUNDING_PARQUET = REPO_ROOT / "historical_data/external/funding_rate_20250109_20260630.parquet"
FUNDING_MANIFEST = FUNDING_PARQUET.parent / "manifest.json"
PUBLIC_FUNDING_RECOVERY = PILOT_ROOT / "public_production_funding_coverage.json"
FEE_ROOT = PILOT_ROOT
SLIPPAGE_SCENARIOS_BPS = (0, 5, 10, 20)
QUOTE_NOTIONAL_USDT = 100.0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path.resolve())


def _jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"non-object JSONL at line {line_number}")
            yield value


def _fee_rates(report: Dict[str, Any], mode: str, symbol: str) -> tuple[float, float]:
    venue_key = "spot_demo" if mode == "SPOT_LONG" else "usd_m_futures_testnet"
    venue = report.get("venues", {}).get(venue_key, {})
    for row in venue.get("records", []):
        if row.get("symbol") != symbol or row.get("status") != "verified":
            continue
        rates = row.get("rates", {})
        if venue_key == "spot_demo":
            standard = rates.get("standardCommission", {})
            # Ignore optional BNB discounts so the cost bound does not depend on reserves.
            return float(standard["taker"]), float(standard["taker"])
        taker = float(rates["takerCommissionRate"])
        return taker, taker
    raise ValueError(f"current taker commission unavailable for {venue_key}:{symbol}")


def _funding_history() -> tuple[Dict[str, List[tuple[int, float]]], Dict[str, Any]]:
    manifest = json.loads(FUNDING_MANIFEST.read_text(encoding="utf-8"))
    matches = [row.get("sha256") for row in manifest.get("files", []) if row.get("file") == FUNDING_PARQUET.name]
    if len(matches) != 1 or _sha256(FUNDING_PARQUET) != matches[0]:
        raise ValueError("public funding Parquet hash mismatch")
    frame = pd.read_parquet(FUNDING_PARQUET)
    times = pd.to_datetime(frame["funding_time"], utc=True, errors="raise")
    grouped: Dict[str, List[tuple[int, float]]] = defaultdict(list)
    for symbol, timestamp, rate in zip(frame["symbol"], times, frame["funding_rate"]):
        grouped[str(symbol)].append((int(timestamp.timestamp()), float(rate)))

    recovered_report = json.loads(PUBLIC_FUNDING_RECOVERY.read_text(encoding="utf-8"))
    raw_path = Path(recovered_report.get("recovered_raw_path") or "")
    if str(raw_path).startswith("/app/"):
        raw_path = REPO_ROOT / Path(str(raw_path)[5:])
    elif not raw_path.is_absolute():
        raw_path = REPO_ROOT / raw_path
    if not raw_path.is_file():
        raw_path = PILOT_ROOT / "public_production_funding_1000PEPEUSDT_20250205_20250220.json"
    if not raw_path.is_file():
        raise ValueError("public PEPE funding raw response missing")
    if _sha256(raw_path) != recovered_report.get("recovered_raw_sha256"):
        raise ValueError("public PEPE funding recovery hash mismatch")
    for row in json.loads(raw_path.read_text(encoding="utf-8")):
        grouped[str(row["symbol"])].append((int(row["fundingTime"]) // 1000, float(row["fundingRate"])))
    for symbol, events in grouped.items():
        deduped = {timestamp: rate for timestamp, rate in events}
        grouped[symbol] = sorted(deduped.items())
    return grouped, {
        "source": "public_usd_m_production_market_funding_not_testnet",
        "parquet_sha256": matches[0],
        "recovered_pepe_sha256": recovered_report["recovered_raw_sha256"],
        "source_manifest_verified": True,
    }


def build_cost_sensitivity(
    *, outcomes_path: Path = OUTCOMES,
    fee_path: Path | None = None,
    output_root: Path = PILOT_ROOT,
) -> Dict[str, Any]:
    fee_files = sorted(FEE_ROOT.glob("sandbox_commissions_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if fee_path is None:
        if not fee_files:
            raise FileNotFoundError("no current sandbox commission capture")
        fee_path = fee_files[0]
    fee_report = json.loads(fee_path.read_text(encoding="utf-8"))
    if fee_report.get("capture_type") != "current_account_specific_fees_not_historical" or fee_report.get("orders_submitted") is not False:
        raise ValueError("commission source is not a read-only current-fee snapshot")
    fees = fee_report.get("venues", {})
    for venue in ("spot_demo", "usd_m_futures_testnet"):
        block = fees.get(venue, {})
        if block.get("verified_symbols") != 26 or block.get("expected_symbols") != 26:
            raise ValueError(f"current fee coverage incomplete: {venue}")
    funding, funding_provenance = _funding_history()
    output_root.mkdir(parents=True, exist_ok=True)
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}_{uuid.uuid4().hex[:8]}"
    rows_path = output_root / f"bilateral_cost_sensitivity_{run_id}.jsonl"
    report_path = output_root / f"bilateral_cost_sensitivity_{run_id}.report.json"

    summaries: Dict[str, Dict[str, Any]] = {}
    rows_written = 0
    temp = rows_path.with_suffix(".jsonl.tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as output:
            for contract in _jsonl(outcomes_path):
                outcome = contract.get("outcome", {})
                if outcome.get("label_valid") is not True:
                    continue
                mode = str(contract.get("mode") or "")
                if mode not in {"SPOT_LONG", "FUTURES_LONG", "FUTURES_SHORT"}:
                    continue
                symbol = str(contract.get("contract_symbol") or contract.get("symbol") or "").upper()
                side = str(contract.get("side") or "").upper()
                entry_price = float(outcome["entry_price"])
                exit_price = float(outcome["exit_price"])
                gross_return = float(outcome["gross_pnl_usdt"]) / (entry_price * float(outcome["quantity"]))
                entry_rate, exit_rate = _fee_rates(fee_report, mode, symbol)
                entry_time = int(outcome["entry_time"])
                exit_time = int(outcome["exit_time"])
                applicable_funding = []
                if mode != "SPOT_LONG":
                    applicable_funding = [rate for stamp, rate in funding.get(symbol, []) if entry_time <= stamp <= exit_time]
                side_sign = 1.0 if side == "LONG" else -1.0
                public_funding_cost_pct = sum(side_sign * rate for rate in applicable_funding)
                for slippage_bps in SLIPPAGE_SCENARIOS_BPS:
                    # Constant quote notional is an explicit cost-comparison basis, not account balance.
                    entry_fee = QUOTE_NOTIONAL_USDT * entry_rate
                    exit_fee = QUOTE_NOTIONAL_USDT * exit_rate
                    slippage = 2 * QUOTE_NOTIONAL_USDT * slippage_bps / 10000.0
                    funding_cost = QUOTE_NOTIONAL_USDT * public_funding_cost_pct if mode != "SPOT_LONG" else 0.0
                    gross_usdt = QUOTE_NOTIONAL_USDT * gross_return
                    net_usdt = gross_usdt - entry_fee - exit_fee - slippage - funding_cost
                    key = f"{mode}|{side}|slippage_{slippage_bps}bps"
                    summary = summaries.setdefault(key, {"mode": mode, "side": side, "slippage_bps_per_fill": slippage_bps, "rows": 0, "gross_sum_usdt_per_100": 0.0, "net_sum_usdt_per_100": 0.0, "positive_net_rows": 0, "funding_event_rows": 0})
                    summary["rows"] += 1
                    summary["gross_sum_usdt_per_100"] += gross_usdt
                    summary["net_sum_usdt_per_100"] += net_usdt
                    summary["positive_net_rows"] += int(net_usdt > 0)
                    summary["funding_event_rows"] += int(bool(applicable_funding))
                    output.write(json.dumps({
                        "opportunity_event_id": contract.get("opportunity_event_id"),
                        "bilateral_thesis_id": contract.get("bilateral_thesis_id"),
                        "dedupe_cluster_id": contract.get("dedupe_cluster_id"),
                        "symbol": symbol, "mode": mode, "side": side,
                        "outcome": outcome.get("outcome"), "label_is_executable": False,
                        "scenario_notional_usdt": QUOTE_NOTIONAL_USDT,
                        "current_fee_source_captured_at": fee_report.get("captured_at"),
                        "entry_taker_fee_rate_current": entry_rate,
                        "exit_taker_fee_rate_current": exit_rate,
                        "slippage_bps_per_fill_assumed": slippage_bps,
                        "public_production_funding_events": len(applicable_funding),
                        "public_production_funding_cost_pct_approx": public_funding_cost_pct,
                        "gross_pnl_usdt_per_100_notional": gross_usdt,
                        "net_pnl_usdt_per_100_notional_scenario": net_usdt,
                        "costs_are_historical_account_fills": False,
                    }, ensure_ascii=False, allow_nan=False) + "\n")
                    rows_written += 1
        temp.replace(rows_path)
    finally:
        temp.unlink(missing_ok=True)

    for summary in summaries.values():
        n = summary["rows"]
        summary["mean_gross_usdt_per_100"] = summary.pop("gross_sum_usdt_per_100") / n if n else None
        summary["mean_net_usdt_per_100_scenario"] = summary.pop("net_sum_usdt_per_100") / n if n else None
    report = {
        "artifact_version": "universal_entry_cost_sensitivity_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rows_path": _display_path(rows_path),
        "rows_sha256": _sha256(rows_path),
        "outcomes_path": _display_path(outcomes_path),
        "outcomes_sha256": _sha256(outcomes_path),
        "fee_snapshot_path": _display_path(fee_path),
        "fee_snapshot_sha256": _sha256(fee_path),
        "funding_provenance": funding_provenance,
        "scenario_quote_notional_usdt": QUOTE_NOTIONAL_USDT,
        "slippage_scenarios_bps_per_fill": list(SLIPPAGE_SCENARIOS_BPS),
        "rows_written": rows_written,
        "summaries": list(summaries.values()),
        "is_executable_replay": False,
        "formula_scoring_allowed": False,
        "limitations": [
            "Price-path labels are experimental; they are not fills and cannot authorize orders.",
            "Commission rates are current account-specific rates applied as a sensitivity proxy, not historical 2025 fees.",
            "Funding uses public Production market rates and constant $100 quote notional; it is not Testnet funding/account history.",
            "Slippage is an explicit 0/5/10/20 bps-per-fill scenario, not observed execution cost.",
            "This report is cost sensitivity only: it does not calibrate probabilities, rank formulas, or select a strategy.",
        ],
    }
    temp_report = report_path.with_suffix(".json.tmp")
    temp_report.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    temp_report.replace(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outcomes", type=Path, default=OUTCOMES)
    parser.add_argument("--fees", type=Path)
    parser.add_argument("--output-root", type=Path, default=PILOT_ROOT)
    args = parser.parse_args()
    report = build_cost_sensitivity(outcomes_path=args.outcomes, fee_path=args.fees, output_root=args.output_root)
    print(json.dumps({"rows_written": report["rows_written"], "scenario_count": len(report["summaries"]), "executable": report["is_executable_replay"], "formula_scoring_allowed": report["formula_scoring_allowed"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())