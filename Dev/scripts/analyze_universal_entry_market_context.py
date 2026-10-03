"""Development-only falsification of BTC-aligned breakout SHORT entries."""
from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from Dev.scripts.analyze_universal_entry_stop_survival import REPO_ROOT, _rows, _sha256
from Dev.scripts.score_universal_entry_minimal_lifecycle import cluster_mean_summary
from simulation.mimic_feed import HistoricalDataFeed


ROOT = REPO_ROOT / "Dev/benchmarks/universal_entry_selection"
SOURCES = (
    ("development_jul2025", "expanded_range_v1/cal_range_scalp_2_20250715_20250729"),
    ("development_sep2025", "expanded_range_v1/cal_range_scalp_3_20250909_20250923"),
)


def closed_btc_return_4h(bars: list[dict[str, Any]], decision_time: int) -> float | None:
    ordered = sorted(bars, key=lambda bar: int(bar["open_time"]))
    times = [int(bar["open_time"]) for bar in ordered]
    end = bisect.bisect_right(times, decision_time - 3600)
    history = ordered[max(0, end - 5):end]
    expected_last = decision_time // 3600 * 3600 - 3600
    if len(history) != 5 or int(history[-1]["open_time"]) != expected_last:
        return None
    if any(int(bar["open_time"]) != expected_last - (4 - index) * 3600 for index, bar in enumerate(history)):
        return None
    closes = [float(bar["close"]) for bar in history]
    if any(not math.isfinite(close) or close <= 0 for close in closes):
        return None
    return closes[-1] / closes[0] - 1.0


def evaluate_gate(events: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [event for event in events if event["btc_return_4h"] is not None]
    chosen = [event for event in eligible if event["btc_return_4h"] < 0]
    rejected = [event for event in eligible if event["btc_return_4h"] >= 0]
    result: dict[str, Any] = {
        "all_unique_events": len(events), "eligible_events": len(eligible),
        "missing_context_events": len(events) - len(eligible), "selected_events": len(chosen),
        "selected_coverage_pct_of_eligible": 100.0 * len(chosen) / len(eligible) if eligible else None,
        "scenarios": {},
    }
    for bps in (0, 5, 10, 20):
        key = f"slippage_{bps}bps_per_fill"
        baseline = [event["nets"][key] for event in eligible]
        selected = [event["nets"][key] for event in chosen]
        rejected_nets = [event["nets"][key] for event in rejected]
        losses = -sum(min(0.0, value) for value in selected)
        result["scenarios"][key] = {
            "baseline_mean_net_usdt": statistics.mean(baseline) if baseline else None,
            "selected_mean_net_usdt": statistics.mean(selected) if selected else None,
            "rejected_mean_net_usdt": statistics.mean(rejected_nets) if rejected_nets else None,
            "selected_pf": sum(max(0.0, value) for value in selected) / losses if losses else None,
            "selected_positive_pct": 100.0 * sum(value > 0 for value in selected) / len(selected) if selected else None,
            "filter_net_per_eligible_opportunity_abstentions_zero": sum(selected) / len(eligible) if eligible else None,
            "selection_delta_per_eligible_opportunity": (sum(selected) - sum(baseline)) / len(eligible) if eligible else None,
        }
    result["selected_mean_net_r_10bps"] = statistics.mean(event["net_r"] for event in chosen) if chosen else None
    result["selected_cluster_summary_10bps"] = cluster_mean_summary([
        (event["cluster"], event["nets"]["slippage_10bps_per_fill"]) for event in chosen
    ])
    result["paired_selection_cluster_summary_10bps"] = cluster_mean_summary([
        (event["cluster"], 0.0 if event["btc_return_4h"] < 0 else -event["nets"]["slippage_10bps_per_fill"])
        for event in eligible
    ])
    return result


def analyze_development_window(label: str, source: Path) -> dict[str, Any]:
    bundle_path = ROOT / "minimal_lifecycle_v2/report.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    window = bundle["windows"][label]
    if window["role"] != "development":
        raise ValueError("context diagnosis must not inspect validation")
    tape = bundle_path.parent / f"{label}.jsonl"
    if _sha256(tape) != window["output_sha256"]:
        raise ValueError("lifecycle source hash mismatch")
    replay_report = json.loads((source / "entry_stop_survival_v1/report.json").read_text(encoding="utf-8"))
    if replay_report["output_sha256"] != window["source_tape_sha256"]:
        raise ValueError("original stop replay lineage mismatch")
    proposal_path = REPO_ROOT / replay_report["proposals_path"]
    if _sha256(proposal_path) != replay_report["proposals_sha256"]:
        raise ValueError("original proposal hash mismatch")
    proposals = {row["opportunity_event_id"]: row for row in _rows(proposal_path)}
    if len(proposals) != window["proposal_count"]:
        raise ValueError("original proposal count mismatch")
    normalized = REPO_ROOT / replay_report["normalized_root"]
    btc_path = normalized / "um/BTCUSDT_1m.parquet"
    btc_manifest = normalized / "um/BTCUSDT_1m.manifest.json"
    manifest = json.loads(btc_manifest.read_text(encoding="utf-8"))
    if manifest.get("venue") != "um" or manifest.get("interval") != "1m":
        raise ValueError("BTC source must be UM 1m")
    hours = HistoricalDataFeed(str(normalized / "um")).get_klines("BTCUSDT", "1h")
    grouped: dict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in _rows(tape):
        if row["stop"] == "1.5" and row["family"] == "breakout_volatility" and row["side"] == "SHORT":
            original = proposals[row["event_id"]]
            decision = int(original["decision_time"])
            if original["window_role"] != "development" or float(original["feature_available_at"]) > decision or decision > int(row["entry_time"]):
                raise ValueError("proposal context is not causal development data")
            grouped[(row["symbol"], decision, row["side"])].append(row)
    events = []
    decisions: dict[int, float | None] = {}
    for (symbol, decision_time, side), rows in sorted(grouped.items()):
        if decision_time not in decisions:
            decisions[decision_time] = closed_btc_return_4h(hours, decision_time)
        events.append({
            "symbol": symbol, "decision_time": decision_time, "side": side,
            "cluster": rows[0]["cluster"], "btc_return_4h": decisions[decision_time],
            "nets": {
                f"slippage_{bps}bps_per_fill": statistics.mean(row["lifecycle"]["scenarios"][f"slippage_{bps}bps_per_fill"]["net_usdt_no_funding"] for row in rows)
                for bps in (0, 5, 10, 20)
            },
            "net_r": statistics.mean(row["lifecycle"]["scenarios"]["slippage_10bps_per_fill"]["net_r_no_funding"] for row in rows),
        })
    return {
        "role": "development", "source_lifecycle_sha256": window["output_sha256"],
        "original_proposals_sha256": replay_report["proposals_sha256"],
        "source_contract": bundle["contract"], "btc_parquet_sha256": _sha256(btc_path),
        "btc_manifest_sha256": _sha256(btc_manifest), "gate_metrics": evaluate_gate(events),
        "events": events,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "entry_market_context_development_v1.json")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; preserve the frozen experiment and use a new path")
    report: dict[str, Any] = {
        "artifact_version": "entry_market_context_development_v1", "analyzer_sha256": _sha256(Path(__file__)),
        "hypothesis": "BTC prior four-hour return < 0 improves breakout/volatility SHORT continuation",
        "frozen_rule": {"family": "breakout_volatility", "side": "SHORT", "stop_atr": 1.5, "btc_return_4h_max_exclusive": 0.0},
        "candidate_claim_allowed": False, "validation_read": False, "windows": {},
        "limitations": [
            "Development-only exploratory diagnosis; windows have already been inspected for earlier hypotheses.",
            "BTC return uses five contiguous fully closed UM hourly closes; missing context is not a losing trade.",
            "Context is joined to original proposal decision_time and feature availability; no future BTC candle is allowed.",
            "Events are deduplicated; no portfolio exposure, account margin, or actual fill claim.",
            "One zero-threshold filter only; no threshold search or future-period retuning.",
        ],
    }
    for label, source in SOURCES:
        result = analyze_development_window(label, ROOT / source)
        report["windows"][label] = result
        print(json.dumps({"window": label, "metrics": result["gate_metrics"]}), flush=True)
    report["development_gate_passed"] = all(
        window["gate_metrics"]["selected_events"] >= 100
        and window["gate_metrics"]["scenarios"]["slippage_10bps_per_fill"]["selected_mean_net_usdt"] > 0
        and window["gate_metrics"]["scenarios"]["slippage_20bps_per_fill"]["selected_mean_net_usdt"] > 0
        and window["gate_metrics"]["selected_cluster_summary_10bps"]["cluster_equal_mean_ci95"][0] > 0
        for window in report["windows"].values()
    )
    report["next_gate"] = "freeze_for_fresh_confirmation" if report["development_gate_passed"] else "hypothesis_not_supported_do_not_retune"
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(args.output), "next_gate": report["next_gate"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())