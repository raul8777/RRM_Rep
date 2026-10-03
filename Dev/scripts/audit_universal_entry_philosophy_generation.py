"""Audit development philosophy proposal rates and reproduce them from normalized candles."""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from entry_philosophies import (
    LOOKBACK_BARS,
    generate_cross_sectional_relative_strength_proposals,
    generate_pilot_proposals,
)
from simulation.mimic_feed import HistoricalDataFeed

PILOT_ROOT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection"
DEFAULT_PROPOSALS = PILOT_ROOT / "pilot_development" / "philosophy_proposals.jsonl"
DEFAULT_REPORT = PILOT_ROOT / "pilot_development" / "philosophy_proposals.report.json"
DEFAULT_NORMALIZED_ROOT = PILOT_ROOT / "normalized" / "pilot_development"
DEFAULT_OUTPUT = PILOT_ROOT / "pilot_development" / "philosophy_generation_audit.json"
WINDOWS_ROOT = REPO_ROOT / "Dev" / "benchmarks" / "windows_calibration"
INTERVAL_SECONDS = 3600


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def count_eligible_decision_slots(
    bars: Iterable[Dict[str, Any]], *, window_start: float, window_end: float,
    horizon_hours: int = 4, interval_seconds: int = INTERVAL_SECONDS,
) -> int:
    ordered = sorted(bars, key=lambda bar: float(bar["open_time"]))
    if len(ordered) < LOOKBACK_BARS + 3:
        return 0
    times = [float(bar["open_time"]) for bar in ordered]
    horizon_seconds = horizon_hours * 3600
    eligible = 0
    for index in range(LOOKBACK_BARS + 1, len(ordered) - 1):
        decision_time = times[index] + interval_seconds
        entry_time = times[index + 1]
        if entry_time != decision_time or not window_start <= decision_time < window_end:
            continue
        if times[-1] + interval_seconds < entry_time + horizon_seconds:
            continue
        eligible += 1
    return eligible


def summarize_family_rates(
    proposals: List[Dict[str, Any]], *, covered_symbol_hours: int, eligible_decision_slots: int,
) -> Dict[str, Any]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for proposal in proposals:
        grouped[str(proposal.get("philosophy_family") or "unknown")].append(proposal)
    result = {}
    for family, rows in sorted(grouped.items()):
        clusters = {str(row.get("dedupe_cluster_id")) for row in rows}
        symbols = {str(row.get("symbol")) for row in rows}
        symbol_days = {
            (str(row.get("symbol")), datetime.fromtimestamp(float(row["decision_time"]), timezone.utc).date().isoformat())
            for row in rows
        }
        result[family] = {
            "opportunities": len(rows),
            "unique_clusters": len(clusters),
            "symbols_with_opportunity": len(symbols),
            "symbol_days_with_opportunity": len(symbol_days),
            "opportunities_per_1000_covered_symbol_hours": (
                len(rows) * 1000.0 / covered_symbol_hours if covered_symbol_hours else None
            ),
            "opportunities_per_1000_mature_decision_slots": (
                len(rows) * 1000.0 / eligible_decision_slots if eligible_decision_slots else None
            ),
        }
    return result


def summarize_disagreement_patterns(proposals: List[Dict[str, Any]]) -> Dict[str, Any]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for proposal in proposals:
        grouped[str(proposal.get("dedupe_cluster_id"))].append(proposal)
    pair_side_counts: Counter[tuple[str, str]] = Counter()
    thesis_pattern_counts: Counter[tuple[str, str, str]] = Counter()
    examples: Dict[tuple[str, str], Dict[str, Any]] = {}
    shared_clusters = 0
    clusters_with_multiple_sides = 0
    single_side_multiple_theses = 0
    for cluster_id, rows in sorted(grouped.items()):
        if len(rows) > 1:
            shared_clusters += 1
        sides = {str(row.get("side")) for row in rows}
        theses = {str(row.get("thesis_type")) for row in rows}
        clusters_with_multiple_sides += int(len(sides) > 1)
        single_side_multiple_theses += int(len(rows) > 1 and len(sides) == 1 and len(theses) > 1)
        for left, right in itertools.combinations(rows, 2):
            pair = " + ".join(sorted((str(left["philosophy_family"]), str(right["philosophy_family"]))))
            side_pattern = "+".join(sorted((str(left["side"]), str(right["side"]))))
            thesis_pattern = " <> ".join(sorted((str(left["thesis_type"]), str(right["thesis_type"]))))
            pair_side_counts[(pair, side_pattern)] += 1
            thesis_pattern_counts[(pair, side_pattern, thesis_pattern)] += 1
            key = (pair, side_pattern)
            if key not in examples:
                feature_snapshot = left.get("feature_snapshot") or {}
                examples[key] = {
                    "dedupe_cluster_id": cluster_id,
                    "symbol": left.get("symbol"),
                    "decision_time": left.get("decision_time"),
                    "features": {
                        field: feature_snapshot.get(field)
                        for field in ("return_4h", "zscore_24h", "close", "prior_24h_high", "prior_24h_low", "true_range_expansion_ratio")
                    },
                    "proposals": [
                        {field: row.get(field) for field in ("philosophy_family", "philosophy_variant", "side", "thesis_type")}
                        for row in (left, right)
                    ],
                }
    return {
        "clusters": len(grouped),
        "shared_clusters": shared_clusters,
        "clusters_with_multiple_sides": clusters_with_multiple_sides,
        "single_side_clusters_with_multiple_theses": single_side_multiple_theses,
        "pair_side_counts": {
            f"{pair} | {side_pattern}": count
            for (pair, side_pattern), count in sorted(pair_side_counts.items())
        },
        "thesis_pattern_counts": {
            f"{pair} | {side_pattern} | {thesis_pattern}": count
            for (pair, side_pattern, thesis_pattern), count in sorted(thesis_pattern_counts.items())
        },
        "representative_examples": {
            f"{pair} | {side_pattern}": example
            for (pair, side_pattern), example in sorted(examples.items())
        },
        "review_scope": "all_cluster_categories_counted; one_representative_per_pair_side_reviewed; row_by_row_review_pending",
    }


def audit_cluster_lineage(
    proposals: List[Dict[str, Any]], *, expected_window_role: str = "development",
) -> Dict[str, Any]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for proposal in proposals:
        grouped[str(proposal.get("dedupe_cluster_id"))].append(proposal)
    cluster_rows = []
    feature_mismatch_count = 0
    identity_error_count = 0
    point_in_time_error_count = 0
    shared_clusters = 0
    multi_side_clusters = 0
    for cluster_id, rows in sorted(grouped.items()):
        identity = {
            (row.get("window_id"), row.get("symbol"), float(row.get("decision_time", 0)))
            for row in rows
        }
        identity_consistent = len(identity) == 1
        feature_hashes_by_family: Dict[str, set[str]] = defaultdict(set)
        for row in rows:
            family = str(row.get("philosophy_family") or "unknown")
            feature_hashes_by_family[family].add(hashlib.sha256(json.dumps(
                row.get("feature_snapshot"), sort_keys=True, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")).hexdigest())
        features_identical = all(len(hashes) == 1 for hashes in feature_hashes_by_family.values())
        all_feature_hashes = {value for hashes in feature_hashes_by_family.values() for value in hashes}
        point_in_time_consistent = all(
            row.get("window_role") == expected_window_role
            and float(row.get("feature_available_at", float("inf"))) <= float(row.get("decision_time", 0))
            and float(row.get("entry_time", float("-inf"))) == float(row.get("decision_time", 0))
            and row.get("label_status") == "pending_contract_and_outcome_replay"
            for row in rows
        )
        sides = sorted({str(row.get("side")) for row in rows})
        shared_clusters += int(len(rows) > 1)
        multi_side_clusters += int(len(sides) > 1)
        feature_mismatch_count += int(not features_identical)
        identity_error_count += int(not identity_consistent)
        point_in_time_error_count += int(not point_in_time_consistent)
        window_id, symbol, decision_time = next(iter(identity)) if identity else (None, None, None)
        cluster_rows.append({
            "dedupe_cluster_id": cluster_id,
            "window_id": window_id,
            "symbol": symbol,
            "decision_time": decision_time,
            "proposal_count": len(rows),
            "philosophies": sorted({str(row.get("philosophy_family")) for row in rows}),
            "sides": sides,
            "thesis_types": sorted({str(row.get("thesis_type")) for row in rows}),
            "identity_consistent": identity_consistent,
            "feature_snapshots_identical": features_identical,
            "feature_snapshot_sha256": next(iter(all_feature_hashes)) if len(all_feature_hashes) == 1 else None,
            "family_feature_snapshot_sha256": {
                family: next(iter(hashes)) if len(hashes) == 1 else None
                for family, hashes in sorted(feature_hashes_by_family.items())
            },
            "feature_snapshot_policy": "consistent_within_family; cross_family_features_may_differ",
            "point_in_time_consistent": point_in_time_consistent,
            "members": [
                {
                    "opportunity_event_id": row.get("opportunity_event_id"),
                    "philosophy_family": row.get("philosophy_family"),
                    "philosophy_variant": row.get("philosophy_variant"),
                    "side": row.get("side"),
                    "thesis_type": row.get("thesis_type"),
                    "feature_available_at": row.get("feature_available_at"),
                    "entry_time": row.get("entry_time"),
                    "entry_price": row.get("entry_price"),
                }
                for row in rows
            ],
        })
    passed = not feature_mismatch_count and not identity_error_count and not point_in_time_error_count
    return {
        "review_status": "structural_review_passed_thesis_conflicts_preserved" if passed else "structural_review_failed",
        "passed": passed,
        "reviewed_clusters": len(cluster_rows),
        "shared_clusters": shared_clusters,
        "clusters_with_opposing_sides": multi_side_clusters,
        "feature_mismatch_clusters": feature_mismatch_count,
        "identity_error_clusters": identity_error_count,
        "point_in_time_error_clusters": point_in_time_error_count,
        "thesis_policy": "retain_each_family_thesis_as_a_separate_candidate; do_not_adjudicate_without_contract_outcomes",
        "clusters": cluster_rows,
    }


def audit_generation(
    proposals_path: Path = DEFAULT_PROPOSALS,
    proposal_report_path: Path = DEFAULT_REPORT,
    normalized_root: Path = DEFAULT_NORMALIZED_ROOT,
    output_path: Path = DEFAULT_OUTPUT,
) -> Dict[str, Any]:
    proposals = _read_jsonl(proposals_path)
    proposal_report = json.loads(proposal_report_path.read_text(encoding="utf-8"))
    window_id = str(proposal_report["window_id"])
    window_path = WINDOWS_ROOT / f"{window_id}.json"
    window = json.loads(window_path.read_text(encoding="utf-8"))
    expected_role = str(window.get("window_role") or "")
    if expected_role not in {"development", "validation"} or proposal_report.get("window_role") != expected_role:
        raise ValueError("proposal report role must match its declared development or validation window")

    window_start = datetime.fromisoformat(window["start"]).timestamp()
    window_end = datetime.fromisoformat(window["end"]).timestamp()
    symbols = list(proposal_report["symbols"])
    feed = HistoricalDataFeed(str(normalized_root / "spot"))
    bars_by_symbol = {symbol: feed.get_klines(symbol, "1h") for symbol in symbols}
    expected_hours = int((window_end - window_start) / INTERVAL_SECONDS)
    covered_symbol_hours = 0
    complete_symbols = 0
    eligible_slots = 0
    coverage_by_symbol = {}
    for symbol, bars in bars_by_symbol.items():
        in_window = [bar for bar in bars if window_start <= float(bar["open_time"]) < window_end]
        times = [float(bar["open_time"]) for bar in in_window]
        expected_times = [window_start + offset * INTERVAL_SECONDS for offset in range(expected_hours)]
        complete = times == expected_times
        complete_symbols += int(complete)
        covered_symbol_hours += len(in_window)
        eligible_slots += count_eligible_decision_slots(
            bars, window_start=window_start, window_end=window_end,
            horizon_hours=int(proposal_report["horizon_minutes"]) // 60,
        )
        coverage_by_symbol[symbol] = {"covered_hours": len(in_window), "expected_hours": expected_hours, "complete": complete}

    expected: Dict[str, Dict[str, Any]] = {}
    for symbol, bars in bars_by_symbol.items():
        for proposal in generate_pilot_proposals(
            symbol, bars, window_id=window_id, window_role=expected_role,
            window_start=window_start, window_end=window_end,
            horizon_hours=int(proposal_report["horizon_minutes"]) // 60,
            interval_seconds=INTERVAL_SECONDS,
        ):
            expected[str(proposal["opportunity_event_id"])] = proposal
    for proposal in generate_cross_sectional_relative_strength_proposals(
        bars_by_symbol,
        window_id=window_id,
        window_role=expected_role,
        window_start=window_start,
        window_end=window_end,
        horizon_hours=int(proposal_report["horizon_minutes"]) // 60,
        interval_seconds=INTERVAL_SECONDS,
        minimum_symbols=5,
    ):
        expected[str(proposal["opportunity_event_id"])] = proposal
    actual = {str(proposal.get("opportunity_event_id")): proposal for proposal in proposals}
    event_id_counts = Counter(str(proposal.get("opportunity_event_id")) for proposal in proposals)
    duplicate_event_ids = sorted(event_id for event_id, count in event_id_counts.items() if count > 1)
    missing_from_artifact = sorted(set(expected) - set(actual))
    unexpected_in_artifact = sorted(set(actual) - set(expected))
    payload_mismatches = sorted(
        event_id for event_id in set(expected) & set(actual)
        if expected[event_id] != actual[event_id]
    )

    manifest_hashes = proposal_report.get("source_manifest_hashes", {})
    manifest_mismatches = []
    for symbol in symbols:
        for venue in ("spot", "um"):
            path = normalized_root / venue / f"{symbol}_1m.manifest.json"
            expected_hash = manifest_hashes.get(f"{venue}:{symbol}")
            if not path.is_file() or _sha256(path) != expected_hash:
                manifest_mismatches.append(f"{venue}:{symbol}")

    grouped_clusters: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    cluster_identity_errors = []
    for row in proposals:
        cluster_id = str(row.get("dedupe_cluster_id"))
        grouped_clusters[cluster_id].append(row)
    for cluster_id, rows in grouped_clusters.items():
        identities = {(row.get("window_id"), row.get("symbol"), float(row.get("decision_time", 0))) for row in rows}
        if len(identities) != 1:
            cluster_identity_errors.append(cluster_id)

    pair_counts: Counter[str] = Counter()
    side_patterns: Counter[str] = Counter()
    for rows in grouped_clusters.values():
        families = sorted({str(row["philosophy_family"]) for row in rows})
        if len(rows) > 1:
            side_patterns["+".join(sorted({str(row["side"]) for row in rows}))] += 1
        for left, right in itertools.combinations(families, 2):
            pair_counts[f"{left} + {right}"] += 1

    family_metrics = summarize_family_rates(
        proposals, covered_symbol_hours=covered_symbol_hours,
        eligible_decision_slots=eligible_slots,
    )
    disagreement_patterns = summarize_disagreement_patterns(proposals)
    cluster_lineage_review = audit_cluster_lineage(proposals, expected_window_role=expected_role)
    proposal_hash_matches = _sha256(proposals_path) == proposal_report.get("proposal_file_sha256")
    generator_path = REPO_ROOT / "entry_philosophies.py"
    generator_hash_matches = _sha256(generator_path) == proposal_report.get("generator_sha256")
    report = {
        "artifact_version": "universal_entry_philosophy_generation_audit_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": f"{expected_role}_opportunity_generation_only",
        "window_id": window_id,
        "window_role": expected_role,
        "validation_read": expected_role == "validation",
        "outcomes_used": False,
        "venue_executability_claimed": False,
        "formula_scoring_allowed": False,
        "proposal_count": len(proposals),
        "regenerated_proposal_count": len(expected),
        "proposal_artifact_sha256": _sha256(proposals_path),
        "proposal_hash_matches_source_report": proposal_hash_matches,
        "generator_sha256": _sha256(generator_path),
        "generator_hash_matches_source_report": generator_hash_matches,
        "source_manifest_mismatches": manifest_mismatches,
        "symbols": len(symbols),
        "hourly_coverage": {
            "complete_symbols": complete_symbols,
            "expected_symbols": len(symbols),
            "covered_symbol_hours": covered_symbol_hours,
            "expected_symbol_hours": expected_hours * len(symbols),
            "mature_decision_slots": eligible_slots,
            "complete": complete_symbols == len(symbols) and covered_symbol_hours == expected_hours * len(symbols),
            "by_symbol": coverage_by_symbol,
        },
        "families": family_metrics,
        "overlap": {
            "clusters": len(grouped_clusters),
            "shared_clusters": sum(len(rows) > 1 for rows in grouped_clusters.values()),
            "side_patterns": dict(sorted(side_patterns.items())),
            "family_pair_counts": dict(sorted(pair_counts.items())),
            "identity_errors": cluster_identity_errors,
        },
        "disagreement_review": disagreement_patterns,
        "row_by_row_cluster_review": cluster_lineage_review,
        "reproduction": {
            "missing_from_artifact": len(missing_from_artifact),
            "unexpected_in_artifact": len(unexpected_in_artifact),
            "payload_mismatches": len(payload_mismatches),
            "duplicate_event_ids": len(duplicate_event_ids),
            "examples": {
                "missing": missing_from_artifact[:10],
                "unexpected": unexpected_in_artifact[:10],
                "payload_mismatch": payload_mismatches[:10],
                "duplicate_event_ids": duplicate_event_ids[:10],
            },
        },
        "audit_passed": (
            proposal_hash_matches and generator_hash_matches and not manifest_mismatches
            and complete_symbols == len(symbols) and not missing_from_artifact
            and not unexpected_in_artifact and not payload_mismatches and not duplicate_event_ids
            and not cluster_identity_errors and cluster_lineage_review["passed"]
        ),
        "limitations": [
            "Rates describe generated proposals, not confirmed theses, executable contracts, outcomes, or edge.",
            "Rates use complete covered symbol-hours and mature decision slots; warm-up and horizon rules are reported separately.",
            "Validation artifacts are scored only after variants are frozen; their role is reported explicitly.",
            "This reproduces the recorded generator; it does not replace manual review of family disagreements.",
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(output_path)
    report["output"] = str(output_path.relative_to(REPO_ROOT)).replace("\\", "/")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposals", type=Path, default=DEFAULT_PROPOSALS)
    parser.add_argument("--proposal-report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--normalized-root", type=Path, default=DEFAULT_NORMALIZED_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        report = audit_generation(args.proposals, args.proposal_report, args.normalized_root, args.output)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps({key: report[key] for key in (
        "output", "audit_passed", "proposal_count", "regenerated_proposal_count",
        "hourly_coverage", "families", "reproduction", "formula_scoring_allowed",
    )}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())