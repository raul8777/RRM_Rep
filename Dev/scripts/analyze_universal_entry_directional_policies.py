"""Compare predeclared direction policies on paired simulated Futures contracts."""
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
from typing import Any, Dict, List, Tuple

from simulation.mimic_feed import HistoricalDataFeed


REPO_ROOT = Path(__file__).resolve().parents[2]
PILOT_ROOT = REPO_ROOT / "Dev/benchmarks/universal_entry_selection"
DEFAULT_RUN_ID = "fast-20261001-003"
DEFAULT_ROLE = "validation"
MOMENTUM_THRESHOLD = 0.005
MEAN_REVERSION_Z_THRESHOLD = 2.0
RELATIVE_STRENGTH_Z_THRESHOLD = 0.5
FEE_BPS_PER_FILL = 5.0
SLIPPAGE_BPS_PER_FILL = (0.0, 10.0, 20.0)
BOOTSTRAP_REPLICATES = 2000
POLICIES = (
    "original_thesis_side",
    "always_invert_original_side",
    "time_series_momentum_follow_4h_0_5pct",
    "mean_reversion_fade_zscore_2",
    "relative_strength_follow_zscore_0_5",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _decision_features(bars_by_symbol: Dict[str, List[Dict[str, Any]]]) -> Dict[Tuple[str, int], Dict[str, float]]:
    """Compute fully closed 1h features at decision time; no future bars are read."""
    features: Dict[Tuple[str, int], Dict[str, float]] = {}
    observations_by_time: Dict[int, Dict[str, float]] = defaultdict(dict)
    for symbol, bars in bars_by_symbol.items():
        ordered = sorted(bars, key=lambda bar: float(bar["open_time"]))
        times = [int(round(float(bar["open_time"]))) for bar in ordered]
        closes = [float(bar["close"]) for bar in ordered]
        for index in range(24, len(ordered) - 1):
            decision_time = times[index] + 3600
            if times[index + 1] != decision_time or times[index] - times[index - 24] != 24 * 3600:
                continue
            if closes[index] <= 0 or closes[index - 4] <= 0 or closes[index - 24] <= 0:
                continue
            rolling = closes[index - 23:index + 1]
            mean = statistics.mean(rolling)
            deviation = statistics.pstdev(rolling)
            ret_4h = closes[index] / closes[index - 4] - 1.0
            ret_24h = closes[index] / closes[index - 24] - 1.0
            zscore = (closes[index] - mean) / deviation if deviation > 0 else 0.0
            features[(symbol, decision_time)] = {
                "return_4h": ret_4h,
                "return_24h": ret_24h,
                "zscore_24h": zscore,
            }
            observations_by_time[decision_time][symbol] = ret_24h
    for decision_time, observations in observations_by_time.items():
        values = list(observations.values())
        if len(values) < 5:
            continue
        mean = statistics.mean(values)
        deviation = statistics.pstdev(values)
        if deviation <= 0:
            continue
        for symbol, ret_24h in observations.items():
            features[(symbol, decision_time)]["relative_strength_zscore_24h"] = (ret_24h - mean) / deviation
    return features


def choose_direction(policy: str, original_side: str, features: Dict[str, float]) -> str | None:
    side = str(original_side).upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("original side must be LONG or SHORT")
    if policy == "original_thesis_side":
        return side
    if policy == "always_invert_original_side":
        return "SHORT" if side == "LONG" else "LONG"
    if policy == "time_series_momentum_follow_4h_0_5pct":
        move = features.get("return_4h")
        if move is None or abs(move) < MOMENTUM_THRESHOLD:
                return None
        return "LONG" if move > 0 else "SHORT"
    if policy == "mean_reversion_fade_zscore_2":
        zscore = features.get("zscore_24h")
        if zscore is None or abs(zscore) < MEAN_REVERSION_Z_THRESHOLD:
            return None
        return "SHORT" if zscore > 0 else "LONG"
    if policy == "relative_strength_follow_zscore_0_5":
        relative = features.get("relative_strength_zscore_24h")
        if relative is None or abs(relative) < RELATIVE_STRENGTH_Z_THRESHOLD:
            return None
        return "LONG" if relative > 0 else "SHORT"
    raise ValueError(f"unknown direction policy: {policy}")


def _paired_policy_candidates(run_dir: Path, role: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    replay_root = run_dir / "replay" / role
    matrix_path = replay_root / "bilateral_thesis_matrix.jsonl"
    scaffold_path = replay_root / "bilateral_contract_scaffold.jsonl"
    outcome_path = replay_root / "bilateral_scaffold_outcomes.jsonl"
    scaffold_report_path = scaffold_path.with_suffix(".report.json")
    outcome_report_path = outcome_path.with_suffix(".report.json")
    for path in (matrix_path, scaffold_path, outcome_path, scaffold_report_path, outcome_report_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    scaffold_report = json.loads(scaffold_report_path.read_text(encoding="utf-8"))
    outcome_report = json.loads(outcome_report_path.read_text(encoding="utf-8"))
    if _sha256(matrix_path) != scaffold_report.get("input_sha256"):
        raise ValueError("directional analysis matrix hash differs from scaffold report")
    if _sha256(scaffold_path) != scaffold_report.get("output_sha256"):
        raise ValueError("directional analysis scaffold hash differs from scaffold report")
    if _sha256(scaffold_path) != outcome_report.get("input_sha256"):
        raise ValueError("directional analysis scaffold hash differs from outcome report")
    if _sha256(outcome_path) != outcome_report.get("output_sha256"):
        raise ValueError("directional analysis outcome hash differs from outcome report")
    if outcome_report.get("scaffold_source_market_manifest_hashes") != scaffold_report.get("source_market_manifest_hashes"):
        raise ValueError("directional analysis manifest provenance differs from scaffold")
    if outcome_report.get("scaffold_source_market_parquet_hashes") != scaffold_report.get("source_market_parquet_hashes"):
        raise ValueError("directional analysis Parquet provenance differs from scaffold")
    if scaffold_report.get("all_contracts_non_executable") is not True or outcome_report.get("label_is_executable") is not False:
        raise ValueError("directional analysis accepts only explicitly non-executable replay labels")

    normalized_root = PILOT_ROOT / "normalized" / f"pilot_{role}"
    for key, expected in outcome_report.get("scaffold_source_market_parquet_hashes", {}).items():
        venue, symbol, interval = key.split(":", 2)
        source = normalized_root / venue / f"{symbol}_{interval}.parquet"
        if not source.is_file() or _sha256(source) != expected:
            raise ValueError(f"directional analysis source Parquet hash mismatch: {key}")
    for key, expected in outcome_report.get("scaffold_source_market_manifest_hashes", {}).items():
        venue, symbol = key.split(":", 1)
        source = normalized_root / venue / f"{symbol}_1m.manifest.json"
        if not source.is_file() or _sha256(source) != expected:
            raise ValueError(f"directional analysis source manifest hash mismatch: {key}")

    matrix_rows = _read_jsonl(matrix_path)
    scaffolds = _read_jsonl(scaffold_path)
    outcomes = _read_jsonl(outcome_path)
    observed_by_event = {}
    opposite_by_event = {}
    for row in matrix_rows:
        destination = observed_by_event if row.get("thesis_role") == "observed" else opposite_by_event
        destination[row.get("opportunity_event_id")] = row
    scaffold_by_thesis = {row["bilateral_thesis_id"]: row for row in scaffolds}
    outcome_by_thesis_mode = {(row["bilateral_thesis_id"], row["mode"]): row for row in outcomes}
    symbols = sorted({row["symbol"] for row in matrix_rows})
    feed = HistoricalDataFeed(str(normalized_root / "spot"))
    bars_by_symbol = {symbol: feed.get_klines(symbol, "1h") for symbol in symbols}
    feature_map = _decision_features(bars_by_symbol)

    candidates = []
    counts = Counter()
    pair_side_counts = Counter()
    for event_id, observed in observed_by_event.items():
        opposite = opposite_by_event.get(event_id)
        if opposite is None:
            counts["event_without_opposite_thesis"] += 1
            continue
        long_thesis = observed if observed.get("side") == "LONG" else opposite
        short_thesis = observed if observed.get("side") == "SHORT" else opposite
        long_outcome = outcome_by_thesis_mode.get((long_thesis["bilateral_thesis_id"], "FUTURES_LONG"))
        short_outcome = outcome_by_thesis_mode.get((short_thesis["bilateral_thesis_id"], "FUTURES_SHORT"))
        if long_outcome is None or short_outcome is None:
            counts["paired_futures_outcome_missing"] += 1
            continue

        contracts = []
        valid_pair = True
        for thesis, outcome, mode in ((long_thesis, long_outcome, "FUTURES_LONG"), (short_thesis, short_outcome, "FUTURES_SHORT")):
            outcome_detail = outcome.get("outcome") if isinstance(outcome.get("outcome"), dict) else {}
            geometry = outcome.get("geometry") if isinstance(outcome.get("geometry"), dict) else {}
            scaffold = scaffold_by_thesis.get(thesis["bilateral_thesis_id"], {})
            contract = next((row for row in scaffold.get("contracts", []) if row.get("mode") == mode), None)
            contract_geometry = (contract or {}).get("geometry") or {}
            if outcome.get("executable_at_decision") is not False or outcome.get("label_status") != "experimental_scaffold_outcome_not_executable_fill":
                raise ValueError("direction pair contains a label that is not explicitly simulated")
            if outcome_detail.get("label_valid") is not True or geometry.get("geometry_valid") is not True or contract_geometry.get("geometry_valid") is not True:
                valid_pair = False
                break
            if any(str(outcome.get(field)) != str(thesis.get(field)) for field in ("opportunity_event_id", "dedupe_cluster_id", "side")):
                raise ValueError(f"direction-pair identity mismatch: {event_id}")
            entry = float(geometry["entry_price"])
            quantity = float(outcome_detail.get("quantity") or 0.0)
            if entry <= 0 or quantity <= 0:
                valid_pair = False
                break
            notional = entry * quantity
            contracts.append({
                "side": thesis["side"],
                "mode": mode,
                "outcome": outcome_detail["outcome"],
                "gross_pnl_per_100": float(outcome_detail.get("gross_pnl_usdt") or 0.0) / notional * 100.0,
                "entry_time": float(outcome_detail.get("entry_time") or outcome.get("decision_time") or 0.0),
                "exit_time": float(outcome_detail.get("exit_time") or 0.0),
                "entry_price": entry,
                "exit_price": float(outcome_detail.get("exit_price") or entry),
                "quantity": quantity,
                "contract_thesis_type": thesis.get("thesis_type"),
            })
        if not valid_pair:
            counts["pair_invalid_geometry_or_label"] += 1
            continue
        decision_time = int(round(float(observed.get("decision_time") or 0.0)))
        features = feature_map.get((str(observed["symbol"]), decision_time))
        if features is None:
            counts["paired_event_features_missing"] += 1
            continue
        pair_side_counts[f"{observed['side']}:{observed.get('philosophy_family')}"] += 1
        candidates.append({
            "opportunity_event_id": event_id,
            "dedupe_cluster_id": str(observed.get("dedupe_cluster_id") or ""),
            "window_id": observed.get("window_id"),
            "window_role": role,
            "decision_time": decision_time,
            "symbol": str(observed["symbol"]),
            "family": str(observed.get("philosophy_family") or "unknown"),
            "variant": str(observed.get("philosophy_variant") or "unknown"),
            "observed_side": str(observed["side"]).upper(),
            "observed_thesis_type": observed.get("thesis_type"),
            "features": features,
            "contracts": {contract["side"]: contract for contract in contracts},
        })

    report_inputs = {
        "matrix_sha256": _sha256(matrix_path),
        "scaffold_sha256": _sha256(scaffold_path),
        "outcomes_sha256": _sha256(outcome_path),
        "source_parquet_hashes": outcome_report.get("scaffold_source_market_parquet_hashes", {}),
    }
    return candidates, {"counts": dict(counts), "pair_side_counts": dict(pair_side_counts), "input_hashes": report_inputs}


def _policy_choice(policy: str, row: Dict[str, Any]) -> str | None:
    return choose_direction(policy, row["observed_side"], row["features"])


def _policy_metrics(
    rows: List[Dict[str, Any]], *, policy: str, slippage_bps: float,
) -> Dict[str, Any]:
    attempts = 0
    tp = sl = expiry = net_positive = 0
    net_values = []
    cluster_net = defaultdict(float)
    cluster_attempts = Counter()
    decisions_by_family = Counter()
    selected_sides = Counter()
    for row in rows:
        side = _policy_choice(policy, row)
        if side is None:
            continue
        contract = row["contracts"][side]
        attempts += 1
        selected_sides[side] += 1
        result = contract["outcome"]
        tp += int(result == "TP")
        sl += int(result == "SL")
        expiry += int(result == "EXPIRY")
        fee_bps = 5.0
        entry = contract["entry_price"]
        exit_price = contract["exit_price"]
        gross = contract["gross_pnl_per_100"]
        round_trip_cost = 100.0 * (entry + exit_price) / entry * (fee_bps + slippage_bps) / 10000.0
        net = gross - round_trip_cost
        net_values.append(net)
        net_positive += int(net > 0.0)
        cluster = row["dedupe_cluster_id"]
        cluster_net[cluster] += net
        cluster_attempts[cluster] += 1
        decisions_by_family[row["family"]] += 1
    n_rows = len(rows)
    return {
        "policy": policy,
        "decision_opportunities": n_rows,
        "selected_attempts": attempts,
        "coverage_pct": 100.0 * attempts / n_rows if n_rows else None,
        "TP": tp,
        "SL": sl,
        "EXPIRY": expiry,
        "win_rate_TP_pct": 100.0 * tp / attempts if attempts else None,
        "net_positive_rate_pct": 100.0 * net_positive / attempts if attempts else None,
        "mean_net_per_100": statistics.mean(net_values) if net_values else None,
        "mean_net_per_opportunity_including_abstentions": sum(net_values) / n_rows if n_rows else None,
        "unique_source_clusters_selected": len(cluster_net),
        "selected_decisions_by_source_family": dict(decisions_by_family),
        "cluster_net_values": dict(cluster_net),
        "cluster_attempt_counts": dict(cluster_attempts),
        "selected_sides": dict(selected_sides),
    }


def _paired_policy_comparison(
    baseline: Dict[str, Any], challenger: Dict[str, Any], *, seed: int, replicates: int,
) -> Dict[str, Any]:
    clusters = sorted(set(baseline) | set(challenger))
    if len(clusters) < 2:
        return {"clusters": len(clusters), "mean_delta_per_opportunity": None, "ci95": None}
    differences = [challenger.get(key, 0.0) - baseline.get(key, 0.0) for key in clusters]
    rng = random.Random(seed)
    samples = sorted(statistics.mean(rng.choices(differences, k=len(differences))) for _ in range(replicates))
    return {
        "clusters": len(clusters),
        "mean_delta_net_per_cluster": statistics.mean(differences),
        "bootstrap_ci95": [samples[int(0.025 * (replicates - 1))], samples[int(0.975 * (replicates - 1))]],
        "replicates": replicates,
    }

def build_directional_policy_report(
    run_dir: Path,
    *,
    role: str = DEFAULT_ROLE,
    output_root: Path | None = None,
    bootstrap_replicates: int = BOOTSTRAP_REPLICATES,
) -> Dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    if role not in {"development", "validation"}:
        raise ValueError("role must be development or validation")
    if bootstrap_replicates < 100:
        raise ValueError("at least 100 bootstrap replicates are required")
    candidates, source_audit = _paired_policy_candidates(run_dir, role)
    by_family: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        by_family[row["family"]].append(row)

    families = {}
    for family, rows in sorted(by_family.items()):
        policy_results = {}
        for policy in POLICIES:
            policy_results[policy] = {}
            for slippage in SLIPPAGE_BPS_PER_FILL:
                metrics = _policy_metrics(rows, policy=policy, slippage_bps=slippage)
                policy_results[policy][f"slippage_{slippage:g}bps_per_fill"] = metrics
        paired = {}
        baseline_by_slippage = {
            slippage: _policy_metrics(rows, policy="original_thesis_side", slippage_bps=slippage)
            for slippage in SLIPPAGE_BPS_PER_FILL
        }
        for policy in POLICIES:
            if policy == "original_thesis_side":
                continue
            paired[policy] = {}
            for slippage in SLIPPAGE_BPS_PER_FILL:
                challenger = policy_results[policy][f"slippage_{slippage:g}bps_per_fill"]
                baseline = baseline_by_slippage[slippage]
                paired[policy][f"slippage_{slippage:g}bps_per_fill"] = _paired_policy_comparison(
                    baseline["cluster_net_values"],
                    challenger["cluster_net_values"],
                    seed=20261002 + sum(ord(c) for c in f"{role}|{family}|{policy}|{slippage}"),
                    replicates=bootstrap_replicates,
                )
        for policies in policy_results.values():
            for metrics in policies.values():
                metrics.pop("cluster_net_values", None)
                metrics.pop("cluster_attempt_counts", None)
        families[family] = {
            "paired_decision_events": len(rows),
            "policies": policy_results,
            "paired_cluster_bootstrap_vs_observed_side": paired,
        }

    report = {
        "artifact_version": "universal_entry_directional_policy_screen_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_run_id": run_dir.name,
        "role": role,
        "paired_events_with_valid_futures_long_and_short": len(candidates),
        "source_pair_audit": source_audit,
        "policies": {
            "observed_side_control": "use the source philosophy's original proposed side",
            "always_invert_original_side": "opposite of the original proposed side",
            "time_series_momentum_follow_4h_0_5pct": f"follow closed Spot 1h return over 4h if abs(return)>= {MOMENTUM_THRESHOLD:.3%}; otherwise abstain",
            "mean_reversion_fade_zscore_2": f"fade closed Spot 1h 24-bar price zscore if abs(z)>= {MEAN_REVERSION_Z_THRESHOLD}; otherwise abstain",
            "relative_strength_follow_zscore_0_5": f"follow same-time cross-sectional 24h return zscore if abs(z)>= {RELATIVE_STRENGTH_Z_THRESHOLD}; otherwise abstain",
        },
        "feature_contract": {
            "source": "normalized Spot 1h candles only",
            "as_of": "last fully closed hourly bar at decision_time; no bars after decision are used for policy features",
            "momentum_threshold": MOMENTUM_THRESHOLD,
            "mean_reversion_z_threshold": MEAN_REVERSION_Z_THRESHOLD,
            "relative_strength_z_threshold": RELATIVE_STRENGTH_Z_THRESHOLD,
        },
        "cost_scenarios": {
            "futures_fee_bps_per_fill_proxy": FEE_BPS_PER_FILL,
            "slippage_bps_per_fill": list(SLIPPAGE_BPS_PER_FILL),
            "funding_cost": 0.0,
        },
        "families": families,
        "limitations": [
            "This compares direction rules on events where both synthetic UM directions have valid geometry and labels; all results are simulated, not fills.",
            "Each policy selects at most one Futures direction per source event. No Spot/Futures route comparison is made.",
            "Policies are fixed rule hypotheses, not fitted winners. WR is TP-label rate among non-abstained decisions; net-positive rate is reported separately.",
            "Fees are current Futures taker-rate proxies, slippage is scenario-based, funding is omitted, and historical filters/account state are unknown.",
            "Development and validation are reported separately; do not tune thresholds against validation and then cite its WR as OOS.",
        ],
        "labels_are_executable_fills": False,
        "winner_claim_allowed": False,
        "staging_touched": False,
        "orders_submitted": False,
    }
    output_root = Path(output_root).resolve() if output_root else run_dir / "directional_policy_screen_v1" / role
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / "report.json"
    _atomic_json(output_path, report)
    report["report_path"] = str(output_path.relative_to(REPO_ROOT)).replace("\\", "/") if output_path.is_relative_to(REPO_ROOT) else str(output_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--role", choices=("development", "validation"), default=DEFAULT_ROLE)
    parser.add_argument("--bootstrap-replicates", type=int, default=BOOTSTRAP_REPLICATES)
    parser.add_argument("--run-root", type=Path, default=PILOT_ROOT / "comparison_runs")
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    run_dir = args.run_root / args.run_id
    try:
        report = build_directional_policy_report(
            run_dir,
            role=args.role,
            output_root=args.output_root,
            bootstrap_replicates=args.bootstrap_replicates,
        )
    except (OSError, ValueError, KeyError) as exc:
        parser.error(f"directional policy analysis failed: {type(exc).__name__}: {str(exc)[:300]}")
    print(json.dumps({
        "run_id": report["source_run_id"], "role": report["role"],
        "paired_events": report["paired_events_with_valid_futures_long_and_short"],
        "families": sorted(report["families"]), "report_path": report["report_path"],
        "labels_are_executable_fills": report["labels_are_executable_fills"],
        "winner_claim_allowed": report["winner_claim_allowed"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())