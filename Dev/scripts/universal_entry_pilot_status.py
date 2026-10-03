"""Inspect Universal Entry pilot artifacts and report phase gates without trading."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

PILOT_ROOT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection"
NORMALIZED_ROOT = PILOT_ROOT / "normalized"
PROPOSALS_PATH = PILOT_ROOT / "pilot_development" / "philosophy_proposals.jsonl"
PROPOSALS_REPORT_PATH = PROPOSALS_PATH.with_suffix(".report.json")
DEFAULT_STATUS_PATH = PILOT_ROOT / "pilot_development" / "pilot_status.json"
DEVELOPMENT_WINDOW_ID = "cal_range_scalp_1_20250205_20250219"
VALIDATION_WINDOW_ID = "cal_range_scalp_4_20251214_20251228"
EXPECTED_SYMBOLS = 26
EXPECTED_ROWS_PER_SYMBOL = 15 * 1440


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_window_status(window_name: str) -> Dict[str, Any]:
    window_root = NORMALIZED_ROOT / f"pilot_{window_name}"
    by_venue: Dict[str, Any] = {}
    for venue in ("spot", "um"):
        venue_root = window_root / venue
        manifests = sorted(venue_root.glob("*_1m.manifest.json")) if venue_root.exists() else []
        rows_by_symbol = {}
        errors = []
        for manifest_path in manifests:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                rows_by_symbol[str(manifest.get("symbol"))] = int(manifest.get("rows", 0))
                if manifest.get("venue") != venue:
                    errors.append(f"{manifest_path.name}: venue mismatch")
            except (OSError, ValueError, TypeError) as exc:
                errors.append(f"{manifest_path.name}: {type(exc).__name__}")
        complete = len(manifests) == EXPECTED_SYMBOLS and all(
            row_count == EXPECTED_ROWS_PER_SYMBOL for row_count in rows_by_symbol.values()
        )
        by_venue[venue] = {
            "symbols": len(manifests),
            "rows_per_symbol_expected": EXPECTED_ROWS_PER_SYMBOL,
            "complete": complete and not errors,
            "manifest_errors": errors,
        }
    return {
        "window": DEVELOPMENT_WINDOW_ID if window_name == "development" else VALIDATION_WINDOW_ID,
        "role": window_name,
        "venues": by_venue,
        "complete": all(item["complete"] for item in by_venue.values()),
    }


def _proposal_status() -> Dict[str, Any]:
    if not PROPOSALS_PATH.is_file() or not PROPOSALS_REPORT_PATH.is_file():
        return {"available": False, "complete": False, "reason": "proposal_artifact_missing"}
    report = json.loads(PROPOSALS_REPORT_PATH.read_text(encoding="utf-8"))
    actual_hash = _sha256(PROPOSALS_PATH)
    clusters: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    family_counts: Counter[str] = Counter()
    rows = 0
    invalid_json = 0
    with PROPOSALS_PATH.open("r", encoding="utf-8") as source:
        for line in source:
            try:
                proposal = json.loads(line)
            except json.JSONDecodeError:
                invalid_json += 1
                continue
            rows += 1
            family_counts[str(proposal.get("philosophy_family", "unknown"))] += 1
            clusters[str(proposal.get("dedupe_cluster_id", "missing"))].append(proposal)
    invalid_clusters = sum(
        len({(row.get("window_id"), row.get("symbol"), row.get("decision_time")) for row in members}) != 1
        for members in clusters.values()
    )
    hash_matches = actual_hash == report.get("proposal_file_sha256")
    return {
        "available": True,
        "complete": bool(hash_matches and invalid_json == 0 and invalid_clusters == 0),
        "rows": rows,
        "clusters": len(clusters),
        "shared_clusters": int(report.get("clusters_with_multiple_proposals", 0)),
        "clusters_with_multiple_sides": int(report.get("clusters_with_multiple_sides", 0)),
        "family_pair_counts": report.get("family_pair_counts", {}),
        "side_pattern_counts": report.get("side_pattern_counts", {}),
        "invalid_json_rows": invalid_json,
        "invalid_clusters": invalid_clusters,
        "family_counts": dict(sorted(family_counts.items())),
        "sha256": actual_hash,
        "report_hash_matches": hash_matches,
    }


def _philosophy_generation_status() -> Dict[str, Any]:
    report_path = PILOT_ROOT / "pilot_development" / "philosophy_generation_audit.json"
    if not report_path.is_file():
        return {"available": False, "audit_passed": False, "reason": "generation_audit_missing"}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    hourly_coverage = report.get("hourly_coverage", {})
    try:
        display_path = str(report_path.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        display_path = str(report_path)
    return {
        "available": True,
        "audit_passed": report.get("audit_passed") is True,
        "scope": report.get("scope"),
        "proposal_count": report.get("proposal_count", 0),
        "regenerated_proposal_count": report.get("regenerated_proposal_count", 0),
        "covered_symbol_hours": hourly_coverage.get("covered_symbol_hours", 0),
        "mature_decision_slots": hourly_coverage.get("mature_decision_slots", 0),
        "families": report.get("families", {}),
        "disagreement_review": report.get("disagreement_review", {}),
        "row_by_row_cluster_review": report.get("row_by_row_cluster_review", {}),
        "manual_cluster_review": report.get("row_by_row_cluster_review", {}).get("review_status", "not_reviewed"),
        "formula_scoring_allowed": report.get("formula_scoring_allowed") is True,
        "path": display_path,
    }


def _timestamp_epoch(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None


def audit_historical_entry_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    short_rows = 0
    missing_identity = 0
    cluster_ids = set()
    generations: Counter[str] = Counter()
    for row in rows:
        side = str(row.get("expected_side") or row.get("selected_side") or row.get("side") or "").upper()
        if side not in {"SHORT", "SELL"}:
            continue
        short_rows += 1
        symbol = str(row.get("symbol") or "").upper()
        raw_timestamp = next((row.get(key) for key in ("cycle_ts", "source_timestamp", "entry_time", "timestamp") if row.get(key) is not None), None)
        timestamp = _timestamp_epoch(raw_timestamp)
        if not symbol or timestamp is None:
            missing_identity += 1
            continue
        cluster_ids.add((symbol, int(timestamp // 60)))
        flags = row.get("entry_flags") if isinstance(row.get("entry_flags"), dict) else {}
        generations[str(flags.get("generation") or row.get("entry_generation") or "missing")] += 1
    return {
        "short_rows": short_rows,
        "unique_symbol_minute_clusters": len(cluster_ids),
        "rows_missing_identity": missing_identity,
        "generation_counts": dict(sorted(generations.items())),
        "matches_known_1154_161_diagnostic": short_rows == 1154 and len(cluster_ids) == 161 and missing_identity == 0,
    }


def _historical_cohort_status() -> Dict[str, Any]:
    super_mimic_root = REPO_ROOT / "Dev" / "benchmarks" / "super_mimic"
    artifact_paths = [super_mimic_root / "sm_20260919_133754_a0d7d7" / "artifact.json"]
    artifact_paths.extend(sorted(super_mimic_root.glob("sm_20260920_170155_*/artifact.json")))
    artifact_paths.extend(sorted(super_mimic_root.glob("sm_20260921_*/artifact.json")))
    run_summaries = []
    source_paths = set()
    cohort_paths_configured = 0
    for artifact_path in artifact_paths:
        if not artifact_path.is_file():
            continue
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        design = artifact.get("design") or {}
        cohort_value = design.get("entry_cohort_path")
        if cohort_value:
            cohort_paths_configured += 1
            cohort_path = Path(str(cohort_value))
            if str(cohort_path).startswith("/app/"):
                cohort_path = REPO_ROOT / Path(str(cohort_path)[5:])
            elif not cohort_path.is_absolute():
                cohort_path = REPO_ROOT / cohort_path
            if cohort_path.is_file():
                source_paths.add(cohort_path)
        relevant_run = (
            artifact_path.parent.name == "sm_20260919_133754_a0d7d7"
            or artifact_path.parent.name.startswith("sm_20260920_170155_")
            or artifact_path.parent.name.startswith("sm_20260921_")
        )
        if not relevant_run:
            continue
        result_path = artifact_path.parent / "mimic_sweep_results.jsonl"
        aggregate_rows = 0
        rows_with_event_identity = 0
        rows_with_generation = 0
        lineage_records: List[Dict[str, Any]] = []
        if result_path.is_file():
            with result_path.open("r", encoding="utf-8") as source:
                for line in source:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    aggregate_rows += 1
                    if row.get("cycle_ts") is not None or row.get("source_timestamp") is not None:
                        rows_with_event_identity += 1
                    flags = row.get("entry_flags")
                    if isinstance(flags, dict) and flags.get("generation"):
                        rows_with_generation += 1
                    e2e = row.get("e2e_evidence") if isinstance(row.get("e2e_evidence"), dict) else {}
                    lineage = e2e.get("lineage") if isinstance(e2e.get("lineage"), list) else []
                    for record in lineage:
                        if isinstance(record, dict):
                            lineage_records.append({
                                **record,
                                "entry_generation": (
                                    flags.get("generation") if isinstance(flags, dict) else None
                                ),
                            })
        lineage_audit = audit_historical_entry_rows(lineage_records)
        run_summaries.append({
            "run": artifact_path.parent.name,
            "status": artifact.get("status"),
            "seed": design.get("seed"),
            "entry_cohort_path": cohort_value,
            "aggregate_result_rows": aggregate_rows,
            "rows_with_event_identity": rows_with_event_identity,
            "rows_with_generation": rows_with_generation,
            "e2e_lineage_records": len(lineage_records),
            "e2e_lineage_short_rows": lineage_audit["short_rows"],
            "e2e_lineage_unique_symbol_minute_clusters": lineage_audit["unique_symbol_minute_clusters"],
            "e2e_lineage_rows_missing_identity": lineage_audit["rows_missing_identity"],
            "e2e_lineage_generation_counts": lineage_audit["generation_counts"],
            "error": artifact.get("error"),
        })

    cohort_audits = []
    for cohort_path in sorted(source_paths):
        row_values = []
        with cohort_path.open("r", encoding="utf-8") as source:
            for line in source:
                try:
                    row_values.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        cohort_audits.append({"path": str(cohort_path), **audit_historical_entry_rows(row_values)})
    reproduced = any(audit["matches_known_1154_161_diagnostic"] for audit in cohort_audits)
    d0_probe_summaries = []
    for probe_name in (
        "d0_entry_cohort_probe", "d0_entry_cohort_probe_v2",
        "d0_entry_fill_probe", "d0_entry_fill_probe_v2",
        "d0_entry_fill_probe_v3", "d0_entry_fill_probe_v4",
    ):
        report_path = super_mimic_root / probe_name / "mimic_sweep_report.json"
        if not report_path.is_file():
            continue
        report = json.loads(report_path.read_text(encoding="utf-8"))
        result_rows = report.get("results", [])
        lineages = [
            lineage
            for result in result_rows
            for lineage in ((result.get("e2e_evidence") or {}).get("lineage") or [])
            if isinstance(lineage, dict)
        ]
        d0_probe_summaries.append({
            "probe": probe_name,
            "seed": report.get("summary", {}).get("seed"),
            "rows": len(result_rows),
            "reported_trades": sum(int(row.get("trades", 0) or 0) for row in result_rows),
            "lineage_rows": len(lineages),
            "selected_side_missing": sum(row.get("selected_side") is None for row in lineages),
            "contract_missing": sum(row.get("contract_present") is not True for row in lineages),
            "generation_missing": sum(not row.get("generation") and not row.get("entry_generation") for row in lineages),
            "source_role": "diagnostic_probe_not_original_seed_cohort",
        })
    return {
        "reproduced": reproduced,
        "runs": run_summaries,
        "row_level_audits": cohort_audits,
        "reason": None if reproduced else "no_available_row_level_cohort_matches_1154_rows_and_161_symbol_minute_clusters",
        "relevant_run_manifests_scanned": len(artifact_paths),
        "relevant_run_manifests_with_entry_cohort_path": cohort_paths_configured,
        "broader_repository_manifest_search": {
            "artifact_manifests_searched": 58,
            "entry_cohort_paths_found": 0,
            "search_date": "2026-09-30",
        },
        "targeted_d0_probe_audit": d0_probe_summaries,
    }


def _contract_gap_status() -> Dict[str, Any]:
    audit_path = PILOT_ROOT / "pilot_development" / "contract_evidence_gaps.jsonl"
    report_path = audit_path.with_suffix(".report.json")
    if not audit_path.is_file() or not report_path.is_file():
        return {"available": False, "executable_contract_tape": False, "reason": "gap_audit_missing"}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    actual_hash = _sha256(audit_path)
    return {
        "available": True,
        "executable_contract_tape": False,
        "proposal_count": report.get("proposal_count"),
        "alternative_count": report.get("contract_alternative_count"),
        "counts_by_mode": report.get("counts_by_mode", {}),
        "counts_by_status": report.get("counts_by_status", {}),
        "all_alternatives_fail_closed": report.get("all_alternatives_fail_closed") is True,
        "sha256": actual_hash,
        "report_hash_matches": actual_hash == report.get("output_sha256"),
        "path": str(audit_path.relative_to(REPO_ROOT)).replace("\\", "/"),
    }


def _contract_scaffold_status() -> Dict[str, Any]:
    scaffold_path = PILOT_ROOT / "pilot_development" / "contract_scaffold.jsonl"
    report_path = scaffold_path.with_suffix(".report.json")
    if not scaffold_path.is_file() or not report_path.is_file():
        return {"available": False, "executable_contract_tape": False, "reason": "scaffold_missing"}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    actual_hash = _sha256(scaffold_path)
    return {
        "available": True,
        "executable_contract_tape": False,
        "proposal_count": report.get("proposal_count"),
        "contract_count": report.get("contract_count"),
        "counts_by_status": report.get("counts_by_status", {}),
        "all_contracts_non_executable": report.get("all_contracts_non_executable") is True,
        "policy": report.get("policy", {}),
        "sha256": actual_hash,
        "report_hash_matches": actual_hash == report.get("output_sha256"),
        "path": str(scaffold_path.relative_to(REPO_ROOT)).replace("\\", "/"),
    }


def _bilateral_contract_scaffold_status() -> Dict[str, Any]:
    scaffold_path = PILOT_ROOT / "pilot_development" / "bilateral_contract_scaffold.jsonl"
    report_path = scaffold_path.with_suffix(".report.json")
    if not scaffold_path.is_file() or not report_path.is_file():
        return {"available": False, "all_contracts_non_executable": True, "reason": "bilateral_scaffold_missing"}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    actual_hash = _sha256(scaffold_path)
    manifest_hashes = report.get("source_market_manifest_hashes", {})
    parquet_hashes = report.get("source_market_parquet_hashes", {})
    hourly_source_modes = report.get("hourly_source_modes", {})
    source_errors = []
    if not manifest_hashes or not parquet_hashes:
        source_errors.append("source_hashes_missing")
    for key, expected_hash in manifest_hashes.items():
        parts = key.split(":")
        if len(parts) != 2 or parts[0] not in {"spot", "um"} or not parts[1]:
            source_errors.append(f"invalid_manifest_key:{key}")
            continue
        venue, symbol = parts
        manifest_path = PILOT_ROOT / "normalized" / "pilot_development" / venue / f"{symbol}_1m.manifest.json"
        if not manifest_path.is_file() or _sha256(manifest_path) != expected_hash:
            source_errors.append(f"manifest_changed:{key}")
        for interval in ("1m", "1h"):
            parquet_key = f"{key}:{interval}"
            parquet_path = manifest_path.with_name(f"{symbol}_{interval}.parquet")
            expected_parquet_hash = parquet_hashes.get(parquet_key)
            if interval == "1m" and not expected_parquet_hash:
                source_errors.append(f"parquet_hash_missing:{parquet_key}")
            elif expected_parquet_hash and (not parquet_path.is_file() or _sha256(parquet_path) != expected_parquet_hash):
                source_errors.append(f"parquet_changed:{parquet_key}")
        hourly_path = manifest_path.with_name(f"{symbol}_1h.parquet")
        expected_mode = hourly_source_modes.get(key)
        if expected_mode not in {"parquet_1h", "resampled_from_1m"}:
            source_errors.append(f"hourly_source_unknown:{key}")
        elif (expected_mode == "parquet_1h") != hourly_path.is_file():
            source_errors.append(f"hourly_source_changed:{key}")
    if set(parquet_hashes) - {f"{key}:{interval}" for key in manifest_hashes for interval in ("1m", "1h")}:
        source_errors.append("unexpected_parquet_hash_keys")
    try:
        display_path = str(scaffold_path.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        display_path = str(scaffold_path)
    return {
        "available": True,
        "all_contracts_non_executable": report.get("all_contracts_non_executable") is True,
        "thesis_rows": report.get("proposal_count", 0),
        "unique_source_events": report.get("unique_source_event_count", 0),
        "bilateral_thesis_count": report.get("bilateral_thesis_count", 0),
        "contract_count": report.get("contract_count", 0),
        "counts_by_status": report.get("counts_by_status", {}),
        "policy": report.get("policy", {}),
        "sha256": actual_hash,
        "report_hash_matches": actual_hash == report.get("output_sha256"),
        "source_integrity_verified": not source_errors,
        "source_integrity_errors": source_errors,
        "path": display_path,
    }


def _replay_parity_status() -> Dict[str, Any]:
    report_path = PILOT_ROOT / "pilot_development" / "replay_parity_report.json"
    if not report_path.is_file():
        return {"available": False, "parity_passed": False, "reason": "parity_report_missing"}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return {
        "available": True,
        "parity_passed": report.get("parity_passed") is True and int(report.get("mismatches", 1)) == 0,
        "cases": report.get("cases", 0),
        "mismatches": report.get("mismatches", 0),
        "symbols": report.get("symbols", []),
        "path": str(report_path.relative_to(REPO_ROOT)).replace("\\", "/"),
    }


def _scaffold_outcome_status() -> Dict[str, Any]:
    report_path = PILOT_ROOT / "pilot_development" / "scaffold_outcomes.report.json"
    if not report_path.is_file():
        return {"available": False, "executable_outcomes": False, "reason": "scaffold_outcome_report_missing"}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return {
        "available": True,
        "executable_outcomes": report.get("label_is_executable") is True,
        "rows_written": report.get("rows_written", 0),
        "outcome_counts": report.get("outcome_counts", {}),
        "geometry_status_counts": report.get("geometry_status_counts", {}),
        "horizon_minutes": report.get("horizon_minutes"),
        "path": str(report_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "limitations": report.get("limitations", []),
    }


def _bilateral_scaffold_outcome_status() -> Dict[str, Any]:
    outcome_path = PILOT_ROOT / "pilot_development" / "bilateral_scaffold_outcomes.jsonl"
    report_path = outcome_path.with_suffix(".report.json")
    if not outcome_path.is_file() or not report_path.is_file():
        return {"available": False, "executable_outcomes": False, "reason": "bilateral_outcome_report_missing"}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    actual_hash = _sha256(outcome_path)
    scaffold_path = PILOT_ROOT / "pilot_development" / "bilateral_contract_scaffold.jsonl"
    scaffold_report_path = scaffold_path.with_suffix(".report.json")
    scaffold_report = json.loads(scaffold_report_path.read_text(encoding="utf-8")) if scaffold_report_path.is_file() else {}
    source_binding_matches = (
        scaffold_path.is_file()
        and report.get("input_sha256") == _sha256(scaffold_path)
        and bool(scaffold_report.get("source_market_parquet_hashes"))
        and report.get("scaffold_source_market_parquet_hashes") == scaffold_report.get("source_market_parquet_hashes")
    )
    try:
        display_path = str(outcome_path.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        display_path = str(outcome_path)
    return {
        "available": True,
        "executable_outcomes": report.get("label_is_executable") is True,
        "rows_written": report.get("rows_written", 0),
        "outcome_counts": report.get("outcome_counts", {}),
        "geometry_status_counts": report.get("geometry_status_counts", {}),
        "skipped_modes": report.get("skipped_modes", {}),
        "horizon_minutes": report.get("horizon_minutes"),
        "sha256": actual_hash,
        "report_hash_matches": actual_hash == report.get("output_sha256"),
        "source_binding_matches": source_binding_matches,
        "path": display_path,
        "limitations": report.get("limitations", []),
    }


def _scaffold_calibration_status() -> Dict[str, Any]:
    report_path = PILOT_ROOT / "pilot_development" / "scaffold_probability_calibration.json"
    if not report_path.is_file():
        return {"available": False, "usable_for_formula_scoring": False, "reason": "calibration_report_missing"}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return {
        "available": True,
        "usable_for_formula_scoring": report.get("usable_for_formula_scoring") is True,
        "train_rows": report.get("train_rows", 0),
        "validation_rows": report.get("validation_rows", 0),
        "train_brier": report.get("train_brier"),
        "validation_brier": report.get("validation_brier"),
        "calibration_status": report.get("calibration_status"),
        "path": str(report_path.relative_to(REPO_ROOT)).replace("\\", "/"),
    }


def _bilateral_raw_calibration_status() -> Dict[str, Any]:
    report_path = PILOT_ROOT / "pilot_development" / "bilateral_raw_calibration_audit_bilateral_scaffold.json"
    if not report_path.is_file():
        return {"available": False, "usable_for_formula_scoring": False, "reason": "bilateral_raw_calibration_report_missing"}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    try:
        display_path = str(report_path.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        display_path = str(report_path)
    return {
        "available": True,
        "joined_rows": report.get("joined_rows", 0),
        "train_rows": report.get("train_rows", 0),
        "validation_rows": report.get("validation_rows", 0),
        "train_independent_clusters": report.get("train_independent_clusters", 0),
        "validation_independent_clusters": report.get("validation_independent_clusters", 0),
        "clusters_split_across_folds": report.get("clusters_split_across_folds"),
        "train_brier_raw": report.get("train_brier_raw"),
        "validation_brier_raw": report.get("validation_brier_raw"),
        "metrics_by_thesis_mode_side": report.get("metrics_by_thesis_mode_side", {}),
        "missing_counts": report.get("missing_counts", {}),
        "include_opposite_theses": report.get("include_opposite_theses") is True,
        "all_venues": report.get("all_venues") is True,
        "calibration_status": report.get("calibration_status"),
        "usable_for_formula_scoring": report.get("usable_for_formula_scoring") is True,
        "path": display_path,
    }


def _bilateral_matrix_status() -> Dict[str, Any]:
    report_path = PILOT_ROOT / "pilot_development" / "bilateral_thesis_matrix.report.json"
    if not report_path.is_file():
        return {"available": False, "all_rows_non_executable": True, "reason": "bilateral_matrix_report_missing"}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return {
        "available": True,
        "source_event_count": report.get("source_event_count", 0),
        "matrix_rows": report.get("matrix_rows", 0),
        "side_counts": report.get("side_counts", {}),
        "thesis_counts": report.get("thesis_counts", {}),
        "all_rows_non_executable": report.get("all_rows_non_executable") is True,
        "path": str(report_path.relative_to(REPO_ROOT)).replace("\\", "/"),
    }


def _prediction_input_status() -> Dict[str, Any]:
    report_path = PILOT_ROOT / "pilot_development" / "bilateral_prediction_input_audit.json"
    if not report_path.is_file():
        return {"available": False, "point_in_time_symmetry_passed": False, "reason": "prediction_input_audit_missing"}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return {
        "available": True,
        "rows": report.get("rows", 0),
        "source_events": report.get("source_events", 0),
        "complete_bilateral_pairs": report.get("complete_bilateral_pairs", 0),
        "asymmetric_or_missing_pairs": report.get("asymmetric_or_missing_pairs", 0),
        "missing_feature_counts": report.get("missing_feature_counts", {}),
        "point_in_time_symmetry_passed": report.get("point_in_time_symmetry_passed") is True,
        "predictions_available": report.get("predictions_available") is True,
        "calibration_available": report.get("calibration_available") is True,
        "formula_scoring_allowed": report.get("formula_scoring_allowed") is True,
        "path": str(report_path.relative_to(REPO_ROOT)).replace("\\", "/"),
    }


def _raw_prediction_status() -> Dict[str, Any]:
    report_path = PILOT_ROOT / "pilot_development" / "bilateral_raw_predictions.report.json"
    if not report_path.is_file():
        return {"available": False, "raw_predictions_available": False, "reason": "raw_prediction_report_missing"}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return {
        "available": True,
        "raw_predictions_available": report.get("status_counts", {}).get("raw_available", 0) == report.get("rows", -1),
        "rows": report.get("rows", 0),
        "status_counts": report.get("status_counts", {}),
        "error_counts": report.get("error_counts", {}),
        "calibration_status": report.get("calibration_status"),
        "formula_scoring_allowed": report.get("formula_scoring_allowed") is True,
        "path": str(report_path.relative_to(REPO_ROOT)).replace("\\", "/"),
    }


def _raw_calibration_audit_status() -> Dict[str, Any]:
    report_path = PILOT_ROOT / "pilot_development" / "bilateral_raw_calibration_audit.json"
    if not report_path.is_file():
        return {"available": False, "usable_for_formula_scoring": False, "reason": "raw_calibration_audit_missing"}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return {
        "available": True,
        "joined_rows": report.get("joined_rows", 0),
        "train_rows": report.get("train_rows", 0),
        "validation_rows": report.get("validation_rows", 0),
        "train_brier_raw": report.get("train_brier_raw"),
        "validation_brier_raw": report.get("validation_brier_raw"),
        "missing_counts": report.get("missing_counts", {}),
        "calibration_status": report.get("calibration_status"),
        "usable_for_formula_scoring": report.get("usable_for_formula_scoring") is True,
        "path": str(report_path.relative_to(REPO_ROOT)).replace("\\", "/"),
    }


def _historical_funding_status() -> Dict[str, Any]:
    report_path = PILOT_ROOT / "pilot_development" / "public_production_funding_coverage.json"
    if not report_path.is_file():
        return {"available": False, "testnet_historical_funding_verified": False, "formula_scoring_allowed": False}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    local_path = REPO_ROOT / "historical_data/external/funding_rate_20250109_20260630.parquet"
    raw_path = report_path.with_name("public_production_funding_1000PEPEUSDT_20250205_20250220.json")
    hashes_match = (
        local_path.is_file() and raw_path.is_file()
        and _sha256(local_path) == report.get("local_source_sha256")
        and _sha256(raw_path) == report.get("recovered_raw_sha256")
    )
    return {
        "available": True,
        "source_venue": report.get("source_venue"),
        "source_hashes_match": hashes_match,
        "local_contracts": report.get("local_pilot_contracts", 0),
        "recovered_symbol": report.get("recovered_symbol"),
        "recovered_rows": report.get("recovered_rows", 0),
        "public_production_contracts_with_rows": (
            report.get("local_pilot_contracts", 0) + 1 if hashes_match else 0
        ),
        "testnet_historical_funding_verified": False,
        "formula_scoring_allowed": False,
        "path": str(report_path.relative_to(REPO_ROOT)).replace("\\", "/") if report_path.is_relative_to(REPO_ROOT) else str(report_path),
    }


def _sandbox_commission_status() -> Dict[str, Any]:
    paths = sorted((PILOT_ROOT / "pilot_development").glob("sandbox_commissions_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not paths:
        return {"available": False, "current_account_commissions_verified": False, "historical_fees_verified": False}
    path = paths[0]
    report = json.loads(path.read_text(encoding="utf-8"))
    try:
        captured_at = datetime.fromisoformat(str(report["captured_at"]).replace("Z", "+00:00"))
        age_seconds = (datetime.now(timezone.utc) - captured_at).total_seconds()
    except (KeyError, TypeError, ValueError):
        age_seconds = None
    venue_coverage = {
        venue: {
            "verified": report.get("venues", {}).get(venue, {}).get("verified_symbols", 0),
            "expected": report.get("venues", {}).get(venue, {}).get("expected_symbols", 0),
        }
        for venue in ("spot_demo", "usd_m_futures_testnet")
    }
    complete = all(item["verified"] == item["expected"] == 26 for item in venue_coverage.values())
    path_display = str(path.relative_to(REPO_ROOT)).replace("\\", "/") if path.is_relative_to(REPO_ROOT) else str(path)
    return {
        "available": True,
        "path": path_display,
        "sha256": _sha256(path),
        "captured_at": report.get("captured_at"),
        "capture_age_seconds": age_seconds,
        "venue_coverage": venue_coverage,
        "capture_complete": complete and report.get("read_only") is True and report.get("orders_submitted") is False,
        "current_account_commissions_verified": complete and age_seconds is not None and 0 <= age_seconds <= 300,
        "historical_fees_verified": False,
        "formula_scoring_allowed": False,
    }


def _cost_sensitivity_status() -> Dict[str, Any]:
    pilot = PILOT_ROOT / "pilot_development"
    reports = sorted(pilot.glob("bilateral_cost_sensitivity_*.report.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not reports:
        return {"available": False, "executable_replay": False, "formula_scoring_allowed": False}
    path = reports[0]
    report = json.loads(path.read_text(encoding="utf-8"))
    rows_path = Path(str(report.get("rows_path") or ""))
    if not rows_path.is_absolute():
        rows_path = REPO_ROOT / rows_path
    outcomes_path = pilot / "bilateral_scaffold_outcomes.jsonl"
    fee_path = Path(str(report.get("fee_snapshot_path") or ""))
    if not fee_path.is_absolute():
        fee_path = REPO_ROOT / fee_path
    hashes_match = (
        rows_path.is_file() and outcomes_path.is_file() and fee_path.is_file()
        and _sha256(rows_path) == report.get("rows_sha256")
        and _sha256(outcomes_path) == report.get("outcomes_sha256")
        and _sha256(fee_path) == report.get("fee_snapshot_sha256")
    )
    path_display = str(path.relative_to(REPO_ROOT)).replace("\\", "/") if path.is_relative_to(REPO_ROOT) else str(path)
    return {
        "available": True,
        "path": path_display,
        "source_hashes_match": hashes_match,
        "rows_written": report.get("rows_written", 0),
        "scenario_count": len(report.get("summaries", [])),
        "slippage_scenarios_bps_per_fill": report.get("slippage_scenarios_bps_per_fill", []),
        "fee_snapshot_path": report.get("fee_snapshot_path"),
        "current_fee_snapshot_sha256": _sha256(fee_path) if fee_path.is_file() else None,
        "funding_source": report.get("funding_provenance", {}).get("source"),
        "is_executable_replay": report.get("is_executable_replay") is True,
        "formula_scoring_allowed": report.get("formula_scoring_allowed") is True,
        "status": "verified_non_executable_cost_sensitivity" if hashes_match and report.get("is_executable_replay") is False and report.get("formula_scoring_allowed") is False else "unverified_cost_sensitivity",
    }


def _replay_calibration_status() -> Dict[str, Any]:
    pilot = PILOT_ROOT / "pilot_development"
    reports = sorted(pilot.glob("bilateral_replay_calibration_*.report.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not reports:
        return {"available": False, "simulated_replay_calibration_valid": False, "usable_for_formula_scoring": False}
    path = reports[0]
    report = json.loads(path.read_text(encoding="utf-8"))
    predictions_path = pilot / "bilateral_raw_predictions.jsonl"
    outcomes_path = pilot / "bilateral_scaffold_outcomes.jsonl"
    predictions_output = Path(str(report.get("output_predictions_path") or ""))
    if not predictions_output.is_absolute():
        predictions_output = REPO_ROOT / predictions_output
    scaffold_report_path = (pilot / "bilateral_contract_scaffold.jsonl").with_suffix(".report.json")
    outcomes_report_path = outcomes_path.with_suffix(".report.json")
    try:
        scaffold_report = json.loads(scaffold_report_path.read_text(encoding="utf-8"))
        outcomes_report = json.loads(outcomes_report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        scaffold_report = outcomes_report = {}
    hashes_match = (
        predictions_path.is_file() and outcomes_path.is_file() and predictions_output.is_file()
        and _sha256(predictions_path) == report.get("predictions_input_sha256")
        and _sha256(outcomes_path) == report.get("outcomes_input_sha256")
        and _sha256(predictions_output) == report.get("output_predictions_sha256")
        and report.get("outcomes_input_sha256") == outcomes_report.get("output_sha256")
        and report.get("outcomes_source_market_manifest_hashes") == scaffold_report.get("source_market_manifest_hashes")
        and report.get("outcomes_source_market_parquet_hashes") == scaffold_report.get("source_market_parquet_hashes")
    )
    path_display = str(path.relative_to(REPO_ROOT)).replace("\\", "/") if path.is_relative_to(REPO_ROOT) else str(path)
    validated = (
        hashes_match
        and report.get("simulated_replay_validation_passed") is True
        and report.get("clusters_crossing_folds") == 0
        and report.get("usable_for_formula_scoring") is False
        and report.get("usable_for_live_execution") is False
    )
    return {
        "available": True,
        "path": path_display,
        "source_hashes_match": hashes_match,
        "joined_rows": report.get("joined_rows", 0),
        "train_independent_clusters": report.get("train_independent_clusters", 0),
        "validation_independent_clusters": report.get("validation_independent_clusters", 0),
        "clusters_crossing_folds": report.get("clusters_crossing_folds"),
        "validation_metrics_by_venue": {
            mode: {
                "raw_brier": metrics.get("raw_validation_brier"),
                "calibrated_brier": metrics.get("calibrated_validation_brier"),
                "raw_log_loss": metrics.get("raw_validation_log_loss"),
                "calibrated_log_loss": metrics.get("calibrated_validation_log_loss"),
                "validation_clusters": metrics.get("validation_independent_clusters"),
                "fit_converged": metrics.get("converged"),
            }
            for mode, metrics in report.get("models_by_venue", {}).items()
        },
        "simulated_replay_calibration_valid": validated,
        "usable_for_formula_scoring": False,
        "usable_for_live_execution": False,
    }


def _fast_formula_screen_status() -> Dict[str, Any]:
    root = PILOT_ROOT / "comparison_runs"
    status_paths = sorted(root.glob("*/formula_screen*/status.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not status_paths:
        return {"available": False, "screen_complete": False, "winner_claim_allowed": False}
    status_path = status_paths[0]
    status = json.loads(status_path.read_text(encoding="utf-8"))
    summary_value = status.get("summary_path")
    summary_path = Path(str(summary_value)) if summary_value else status_path.parent / "summary.json"
    if not summary_path.is_absolute():
        summary_path = REPO_ROOT / summary_path
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
    reports = summary.get("family_results", {})
    report_checks = {}
    for role, report_value in reports.items():
        report_path = Path(str(report_value))
        if not report_path.is_absolute():
            report_path = REPO_ROOT / report_path
        valid = False
        if report_path.is_file():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            valid = (
                report.get("role") == role
                and report.get("winner_claim_allowed") is False
                and report.get("metrics_are_simulated_non_executable") is True
            )
        report_checks[role] = {"path": str(report_path.relative_to(REPO_ROOT)).replace("\\", "/") if report_path.is_relative_to(REPO_ROOT) else str(report_path), "verified": valid}
    report_set_valid = bool(report_checks) and all(item["verified"] for item in report_checks.values())
    complete = (
        status.get("state") == "formula_screen_complete_no_winner_claim"
        and status.get("counts", {}).get("failed") == 0
        and status.get("counts", {}).get("pending") == 0
        and summary.get("state") == "simulation_screen_complete_review_required"
        and summary.get("winner_claim_allowed") is False
        and summary.get("staging_touched") is False
        and report_set_valid
    )
    display = lambda path: str(path.relative_to(REPO_ROOT)).replace("\\", "/") if path.is_relative_to(REPO_ROOT) else str(path)
    return {
        "available": True,
        "run_id": status.get("run_id"),
        "state": status.get("state"),
        "phase": status.get("phase"),
        "updated_at": status.get("updated_at"),
        "counts": status.get("counts", {}),
        "workers": status.get("workers", {}),
        "status_path": display(status_path),
        "summary_path": display(summary_path),
        "screen_complete": complete,
        "reports_verified": report_checks,
        "families_in_tape": summary.get("families_in_tape", []),
        "pending_canonical_families": summary.get("pending_canonical_families", []),
        "formula_arms": summary.get("formula_arms", []),
        "roles_scored": summary.get("roles", []),
        "common_runtime_hard_gates_applied": False,
        "predictor_weight_cutoff_verified": False,
        "baseline_matches_exact_runtime": False,
        "multiplicity_adjusted": False,
        "decision_blocking_gaps": [
            "common_runtime_hard_gates_not_applied_to_historical_candidates",
            "predictor_learned_weight_training_cutoff_unverified",
            "baseline_is_qubo_proxy_peak_quality_missing",
            "formula_selection_forces_slots_without_no_trade_threshold",
            "multiplicity_correction_not_applied",
        ],
        "metrics_are_simulated_non_executable": summary.get("metrics_are_simulated_non_executable") is True,
        "winner_claim_allowed": summary.get("winner_claim_allowed") is True,
        "staging_touched": summary.get("staging_touched") is True,
        "orders_submitted": summary.get("orders_submitted") is True,
    }


def _prospective_baseline_status() -> Dict[str, Any]:
    pilot = PILOT_ROOT / "pilot_development"
    paths = sorted(pilot.glob("prospective_feasibility_baseline_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not paths:
        return {"available": False, "manifest_valid": False, "source_integrity_verified": False}
    path = paths[0]
    report = json.loads(path.read_text(encoding="utf-8"))
    payload = dict(report)
    expected_manifest_hash = payload.pop("manifest_sha256", None)
    actual_manifest_hash = hashlib.sha256(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
    errors = []
    if actual_manifest_hash != expected_manifest_hash:
        errors.append("baseline_manifest_hash_mismatch")
    for relative, expected in report.get("source_code_sha256", {}).items():
        source_path = REPO_ROOT / relative
        if not source_path.is_file() or _sha256(source_path) != expected:
            errors.append(f"source_code_changed:{relative}")
    for venue, symbols in report.get("development_source_manifest_sha256", {}).items():
        for symbol, expected in symbols.items():
            manifest_path = PILOT_ROOT / "normalized/pilot_development" / venue / f"{symbol}_1m.manifest.json"
            if not manifest_path.is_file() or _sha256(manifest_path) != expected:
                errors.append(f"source_manifest_changed:{venue}:{symbol}")
    expected_data = report.get("historical_diagnostic_artifacts_sha256", {})
    data_paths = {
        "development_proposals": pilot / "philosophy_proposals.jsonl",
        "historical_non_executable_scaffold": pilot / "bilateral_contract_scaffold.jsonl",
        "historical_non_executable_outcomes": pilot / "bilateral_scaffold_outcomes.jsonl",
        "raw_predictions": pilot / "bilateral_raw_predictions.jsonl",
    }
    for key, expected in expected_data.items():
        source_path = data_paths.get(key)
        if source_path is None or not source_path.is_file() or _sha256(source_path) != expected:
            errors.append(f"input_artifact_changed:{key}")
    path_display = str(path.relative_to(REPO_ROOT)).replace("\\", "/") if path.is_relative_to(REPO_ROOT) else str(path)
    return {
        "available": True,
        "path": path_display,
        "captured_at": report.get("captured_at"),
        "manifest_sha256": expected_manifest_hash,
        "manifest_hash_matches": actual_manifest_hash == expected_manifest_hash,
        "manifest_valid": actual_manifest_hash == expected_manifest_hash and report.get("artifact_version") == "universal_entry_prospective_feasibility_baseline_v1",
        "git_head": report.get("git", {}).get("head"),
        "worktree_clean": report.get("git", {}).get("worktree_clean"),
        "dirty_path_count": report.get("git", {}).get("dirty_path_count"),
        "source_integrity_verified": not errors,
        "source_integrity_errors": errors,
        "current_filter_snapshot": report.get("current_filter_snapshot", {}),
        "current_fee_snapshot": report.get("current_fee_snapshot", {}),
        "current_preflight": report.get("current_preflight", {}),
        "effective_pilot_limits": report.get("safety", {}).get("effective_limits", {}),
        "pilot_limits_verified": report.get("safety", {}).get("effective_limits_verified") is True,
        "cross_venue_single_position_guard_verified": report.get("safety", {}).get("cross_venue_single_position_guard_verified") is True,
        "formula_comparison_in_scope": report.get("formula_comparison_in_scope") is True,
        "profitability_claim_allowed": report.get("profitability_claim_allowed") is True,
    }


def _staging_preflight_status() -> Dict[str, Any]:
    pilot = PILOT_ROOT / "pilot_development"
    paths = sorted(pilot.glob("staging_preflight_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not paths:
        return {"available": False, "runtime_preflight_passed": False, "canary_eligible": False, "orders_allowed": False}
    path = paths[0]
    report = json.loads(path.read_text(encoding="utf-8"))
    try:
        captured_at = datetime.fromisoformat(str(report["captured_at"]).replace("Z", "+00:00"))
        age_seconds = (datetime.now(timezone.utc) - captured_at).total_seconds()
    except (KeyError, TypeError, ValueError):
        age_seconds = None
    checks = report.get("checks", {})
    checks_passed = all(value is True for key, value in checks.items() if key != "offline_gates_ready") if checks else False
    fresh = age_seconds is not None and 0 <= age_seconds <= 300
    return {
        "available": True,
        "path": str(path.relative_to(REPO_ROOT)).replace("\\", "/") if path.is_relative_to(REPO_ROOT) else str(path),
        "sha256": _sha256(path),
        "captured_at": report.get("captured_at"),
        "age_seconds": age_seconds,
        "fresh_within_300_seconds": fresh,
        "spot": report.get("spot", {}),
        "futures": report.get("futures", {}),
        "effective_pilot_limits": report.get("effective_pilot_limits", {}),
        "checks": checks,
        "runtime_preflight_passed": report.get("runtime_preflight_passed") is True and checks_passed,
        "offline_gates_ready": report.get("offline_gates_ready") is True,
        "canary_eligible": fresh and report.get("canary_eligible") is True,
        "orders_allowed": False,
    }


def _prospective_pilot_scope_status(
    staging_preflight: Dict[str, Any], prospective_filters: Dict[str, Any], sandbox_commissions: Dict[str, Any],
    baseline: Dict[str, Any], offline_gates: Dict[str, Any],
) -> Dict[str, Any]:
    spot_open = staging_preflight.get("spot", {}).get("open_position_count", 0)
    futures_open = staging_preflight.get("futures", {}).get("open_position_count", 0)
    no_preexisting_positions = spot_open == 0 and futures_open == 0
    canary_ready = bool(
        staging_preflight.get("available")
        and staging_preflight.get("fresh_within_300_seconds")
        and staging_preflight.get("runtime_preflight_passed") is True
        and prospective_filters.get("filters_currentness_verified") is True
        and sandbox_commissions.get("current_account_commissions_verified") is True
        and baseline.get("manifest_valid") is True
        and baseline.get("source_integrity_verified") is True
        and baseline.get("pilot_limits_verified") is True
        and baseline.get("cross_venue_single_position_guard_verified") is True
        and staging_preflight.get("checks", {}).get("pilot_order_and_position_limits_enforced") is True
        and no_preexisting_positions
        and all(value is True for value in offline_gates.values())
    )
    return {
        "approved": True,
        "approved_by_user": "2026-10-01 follow-up 'adelante'",
        "scope": "prospective_staging_functional_feasibility_only",
        "frozen_selector": "baseline_current_universal",
        "uses_original_1154_161_cohort": False,
        "historical_benchmark_replaced": False,
        "formula_comparison_in_scope": False,
        "profitability_claim_allowed": False,
        "offline_feasibility_gates": offline_gates,
        "baseline": baseline,
        "open_positions_at_preflight": {"spot": spot_open, "futures": futures_open},
        "no_preexisting_positions": no_preexisting_positions,
        "pilot_limits_verified": baseline.get("pilot_limits_verified") is True,
        "cross_venue_single_position_guard_verified": baseline.get("cross_venue_single_position_guard_verified") is True,
        "canary_ready_now": canary_ready,
        "orders_allowed": False,
    }


def _feature_source_audit_status(dataset: str) -> Dict[str, Any]:
    report_path = PILOT_ROOT / "pilot_development" / f"feature_source_audit_{dataset}.json"
    if dataset not in {"metrics", "bookDepth"} or not report_path.is_file():
        return {"available": False, "full_coverage_verified": False}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    results = report.get("results", [])
    expected = report.get("expected_archive_count", 0)
    unique_days = {(row.get("contract_symbol"), row.get("date")) for row in results}
    errors = []
    if report.get("dataset") != dataset or report.get("contract_count") != 26 or expected != 390 or len(results) != expected or len(unique_days) != expected:
        errors.append("incomplete_or_duplicate_audit_targets")
    for row in results:
        symbol, day, status = row.get("contract_symbol"), row.get("date"), row.get("status")
        if not isinstance(symbol, str) or not symbol.isalnum() or not isinstance(day, str) or len(day) != 10:
            errors.append("invalid_archive_identity")
            continue
        archive = PILOT_ROOT / "raw" / "binance_archives" / "um" / dataset / symbol / f"{symbol}-{dataset}-{day}.zip"
        if status == "missing_archive":
            if archive.is_file():
                errors.append(f"archive_appeared:{symbol}:{day}")
        elif status in {"already_verified", "partial_day"}:
            if not archive.is_file() or _sha256(archive) != row.get("sha256"):
                errors.append(f"archive_changed:{symbol}:{day}")
        else:
            errors.append(f"unverified_archive:{symbol}:{day}")
    return {
        "available": True,
        "source_venue": "binance_vision_usd_m_public_market_only",
        "expected_archive_count": expected,
        "result_counts": report.get("result_counts", {}),
        "source_files_match_report": not errors,
        "source_errors": errors,
        "full_coverage_verified": not errors and report.get("all_full_days_verified") is True and report.get("result_counts", {}).get("already_verified") == 390,
        "formula_scoring_allowed": False,
        "path": str(report_path.relative_to(REPO_ROOT)).replace("\\", "/") if report_path.is_relative_to(REPO_ROOT) else str(report_path),
    }


def _historical_filter_status() -> Dict[str, Any]:
    report_path = PILOT_ROOT / "historical_filter_snapshots" / "wayback_spot_production_2025.json"
    if not report_path.is_file():
        return {
            "status": "obtainable_pending",
            "venue_executable_at_time": "unknown_without_historical_snapshot",
            "prospective_capture_implemented_locally": True,
            "deployed_to_staging": False,
        }
    report = json.loads(report_path.read_text(encoding="utf-8"))
    captures = report.get("captures", [])
    spot_production_captures = [capture for capture in captures if capture.get("venue") == "spot_production"]
    try:
        report_display_path = str(report_path.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        report_display_path = str(report_path)
    return {
        "status": "partial_archived_spot_production_snapshots_outside_windows" if spot_production_captures else "obtainable_pending",
        "source": "internet_archive",
        "source_venue": report.get("source_venue"),
        "capture_count": len(spot_production_captures),
        "capture_timestamps": [capture.get("archive_capture_timestamp") for capture in spot_production_captures],
        "window_coverage": report.get("window_coverage", {}),
        "venue_executable_at_time": report.get("venue_executable_at_time", "unknown_for_pilot_windows"),
        "prospective_capture_implemented_locally": True,
        "deployed_to_staging": False,
        "path": report_display_path,
    }


def _current_testnet_filter_evidence(report: Dict[str, Any], now: datetime | None = None) -> Dict[str, Any]:
    clock = now or datetime.now(timezone.utc)
    reasons = []
    checks = report.get("source_server_time_checks", {}).get("usd_m_futures_testnet", {})
    source = report.get("sources", {}).get("usd_m_futures_testnet", {})
    samples = source.get("repeated_samples", {})
    try:
        captured_at = datetime.fromisoformat(report["captured_at"].replace("Z", "+00:00"))
        time_ms = checks["time_endpoint_server_time"]
        time_date = parsedate_to_datetime(checks["time_endpoint_http_headers"]["Date"])
        info_date = parsedate_to_datetime(checks["exchange_info_http_headers"]["Date"])
        age_seconds = (clock - captured_at).total_seconds()
        clock_skew_ms = (datetime.fromtimestamp(time_ms / 1000, timezone.utc) - time_date).total_seconds() * 1000
        if captured_at.tzinfo is None or not 0 <= age_seconds <= 300:
            reasons.append("capture_not_recent")
        if abs(clock_skew_ms) > 60_000 or abs((info_date - time_date).total_seconds()) > 60:
            reasons.append("http_date_and_time_disagree")
        if abs((captured_at - datetime.fromtimestamp(time_ms / 1000, timezone.utc)).total_seconds()) > 60:
            reasons.append("capture_and_time_disagree")
    except (KeyError, ValueError, TypeError, OverflowError):
        age_seconds = clock_skew_ms = None
        reasons.append("authoritative_clock_evidence_missing")
    if report.get("snapshot_version") != "binance_pilot_exchange_filters_v4" or source.get("url") != "https://testnet.binancefuture.com/fapi/v1/exchangeInfo":
        reasons.append("testnet_source_unverified")
    if (samples.get("sample_count", 0) < 3 or len(samples.get("samples", [])) < 3
            or len(samples.get("catalog_hashes_excluding_server_time", [])) != 1
            or samples.get("pilot_filter_fingerprint_count") != 26
            or samples.get("pilot_filters_stable") is not True):
        reasons.append("pilot_filters_not_stable")
    records = report.get("symbols", [])
    if len(records) != 26 or any(
        row.get("usd_m_futures_testnet", {}).get("status") != "TRADING"
        or row.get("usd_m_futures_testnet", {}).get("exchange_filters_validated") is not True
        for row in records
    ):
        reasons.append("pilot_symbol_filters_incomplete")
    if report.get("read_only") is not True or report.get("credentials_used") is not False or report.get("orders_submitted") is not False:
        reasons.append("capture_not_public_read_only")
    return {
        "verified": not reasons,
        "clock_source": "/fapi/v1/time_and_http_date_not_exchange_info_server_time",
        "capture_age_seconds": age_seconds,
        "time_endpoint_http_skew_ms": clock_skew_ms,
        "blocking_reasons": reasons,
    }


def _prospective_sandbox_filter_status() -> Dict[str, Any]:
    snapshot_root = PILOT_ROOT / "prospective_filter_snapshots"
    paths = sorted(snapshot_root.glob("exchange_filters_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not paths:
        return {"available": False, "reason": "prospective_snapshot_missing", "filters_currentness_verified": False}
    report_path = paths[0]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    testnet_currentness = _current_testnet_filter_evidence(report)
    checks = report.get("source_server_time_checks", {})
    sources = report.get("sources", {})
    venue_checks = {
        venue: checks.get(venue, {}).get("status", "timestamp_audit_missing")
        for venue in ("spot_demo", "usd_m_futures_testnet")
    }
    demo_comparison = checks.get("usd_m_futures_demo", {})
    testnet_source = sources.get("usd_m_futures_testnet", {})
    demo_source = sources.get("usd_m_futures_demo_comparison_only", {})
    testnet_samples = testnet_source.get("repeated_samples", {}).get("samples", [])
    demo_samples = demo_source.get("repeated_samples", {}).get("samples", [])

    def _fingerprints_by_symbol(samples: List[Dict[str, Any]]) -> Dict[str, str]:
        if not samples:
            return {}
        return {
            key.split(":", 1)[1]: value
            for key, value in samples[0].get("pilot_filter_fingerprints", {}).items()
            if ":" in key and value is not None
        }

    testnet_body_hashes = set(testnet_source.get("repeated_samples", {}).get("exchange_info_body_hashes", []))
    demo_body_hashes = set(demo_source.get("repeated_samples", {}).get("exchange_info_body_hashes", []))
    testnet_catalog_hashes = set(testnet_source.get("repeated_samples", {}).get("catalog_hashes_excluding_server_time", []))
    demo_catalog_hashes = set(demo_source.get("repeated_samples", {}).get("catalog_hashes_excluding_server_time", []))
    testnet_server_times = set(testnet_source.get("repeated_samples", {}).get("server_times", []))
    demo_server_times = set(demo_source.get("repeated_samples", {}).get("server_times", []))
    testnet_fingerprints = _fingerprints_by_symbol(testnet_samples)
    demo_fingerprints = _fingerprints_by_symbol(demo_samples)
    comparison = {
        "host": demo_source.get("url"),
        "comparison_only": True,
        "same_exchange_info_body_hash_set": bool(testnet_body_hashes) and testnet_body_hashes == demo_body_hashes,
        "same_catalog_hash_set_excluding_server_time": bool(testnet_catalog_hashes) and testnet_catalog_hashes == demo_catalog_hashes,
        "same_exchange_info_server_time_set": bool(testnet_server_times) and testnet_server_times == demo_server_times,
        "same_pilot_filter_fingerprints": bool(testnet_fingerprints) and testnet_fingerprints == demo_fingerprints,
        "can_substitute_for_testnet": False,
    }
    try:
        display_path = str(report_path.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        display_path = str(report_path)
    return {
        "available": True,
        "snapshot_version": report.get("snapshot_version"),
        "capture_type": report.get("capture_type"),
        "captured_at": report.get("captured_at"),
        "read_only": report.get("read_only") is True,
        "credentials_used": report.get("credentials_used") is True,
        "orders_submitted": report.get("orders_submitted") is True,
        "symbol_count": len(report.get("symbols", [])),
        "server_time_checks": venue_checks,
        "testnet_currentness_evidence": testnet_currentness,
        "historical_filters_verified": False,
        "usd_m_futures_demo_comparison": comparison,
        "testnet_repeated_response_audit": report.get("source_server_time_checks", {}).get("usd_m_futures_testnet", {}).get("repeated_response_audit", {}),
        "demo_comparison_only": {
            "server_time_status": demo_comparison.get("status", "timestamp_audit_missing"),
            "exchange_info_time_status": demo_comparison.get("exchange_info_time_status"),
            "pilot_filters_stable_across_samples": demo_comparison.get("pilot_filters_stable_across_samples"),
            "repeated_response_audit": demo_comparison.get("repeated_response_audit", {}),
        },
        "testnet_exchange_info_time_status": checks.get("usd_m_futures_testnet", {}).get("exchange_info_time_status"),
        "testnet_pilot_filters_stable_across_samples": checks.get("usd_m_futures_testnet", {}).get("pilot_filters_stable_across_samples"),
        "filters_currentness_verified": venue_checks["spot_demo"] == "fresh_within_tolerance" and testnet_currentness["verified"],
        "venue_executable_at_capture": report.get("venue_executable_at_capture", {}),
        "path": display_path,
    }


def build_status() -> Dict[str, Any]:
    development = _normalized_window_status("development")
    validation = _normalized_window_status("validation")
    proposals = _proposal_status()
    philosophy_generation = _philosophy_generation_status()
    cohort = _historical_cohort_status()
    contract_gaps = _contract_gap_status()
    contract_scaffold = _contract_scaffold_status()
    bilateral_contract_scaffold = _bilateral_contract_scaffold_status()
    replay_parity = _replay_parity_status()
    scaffold_outcomes = _scaffold_outcome_status()
    bilateral_scaffold_outcomes = _bilateral_scaffold_outcome_status()
    bilateral_scaffold_trusted = (
        bilateral_contract_scaffold.get("available")
        and bilateral_contract_scaffold.get("report_hash_matches")
        and bilateral_contract_scaffold.get("source_integrity_verified")
    )
    bilateral_outcomes_trusted = (
        bilateral_scaffold_trusted
        and bilateral_scaffold_outcomes.get("available")
        and bilateral_scaffold_outcomes.get("report_hash_matches")
        and bilateral_scaffold_outcomes.get("source_binding_matches")
    )
    scaffold_calibration = _scaffold_calibration_status()
    bilateral_raw_calibration = _bilateral_raw_calibration_status()
    bilateral_matrix = _bilateral_matrix_status()
    prediction_inputs = _prediction_input_status()
    raw_predictions = _raw_prediction_status()
    raw_calibration_audit = _raw_calibration_audit_status()
    contracts_path = PILOT_ROOT / "pilot_development" / "contracts_tape.jsonl"
    outcomes_path = PILOT_ROOT / "pilot_development" / "contract_outcomes.jsonl"
    formulas_path = PILOT_ROOT / "pilot_development" / "formula_comparison.json"
    filters_history = _historical_filter_status()
    funding_history = _historical_funding_status()
    sandbox_commissions = _sandbox_commission_status()
    cost_sensitivity = _cost_sensitivity_status()
    replay_calibration = _replay_calibration_status()
    fast_formula_screen = _fast_formula_screen_status()
    prospective_baseline = _prospective_baseline_status()
    staging_preflight = _staging_preflight_status()
    feature_sources = {dataset: _feature_source_audit_status(dataset) for dataset in ("metrics", "bookDepth")}
    prospective_filters = _prospective_sandbox_filter_status()
    prospective_offline_gates = {
        "development_inputs_complete": development.get("complete") is True,
        "proposal_lineage_complete": proposals.get("complete") is True,
        "cluster_lineage_review_passed": philosophy_generation.get("row_by_row_cluster_review", {}).get("passed") is True,
        "bilateral_scaffold_source_integrity_verified": bilateral_scaffold_trusted is True,
        "bilateral_replay_bound_to_scaffold": bilateral_outcomes_trusted is True,
        "mechanical_replay_parity_passed": replay_parity.get("parity_passed") is True,
    }
    prospective_pilot_scope = _prospective_pilot_scope_status(
        staging_preflight, prospective_filters, sandbox_commissions, prospective_baseline, prospective_offline_gates,
    )
    evidence_scopes = {
        "historical_market_replay": {
            "account_state_2025_required": False,
            "capital_basis": "predeclared_simulated_portfolio_not_historical_sandbox_balance",
            "labels_are_exchange_fills": False,
            "simulated_replay_calibration_valid": replay_calibration.get("simulated_replay_calibration_valid") is True,
            "formula_scoring_allowed": False,
        },
        "active_staging_canary": {
            "current_independent_wallets_required": True,
            "current_filters_verified": prospective_filters.get("filters_currentness_verified") is True,
            "wallet_and_position_preflight_verified": False,
            "offline_gates_passed": False,
            "orders_allowed_by_pilot": False,
        },
    }
    phases = {
        "data_and_proposals": (
            "lineage_audit_passed_contract_construction_blocked"
            if development["complete"] and proposals.get("complete") and philosophy_generation.get("audit_passed")
            else "ready_for_manual_review"
            if development["complete"] and proposals.get("complete")
            else "incomplete"
        ),
        "philosophy_generation": (
            "development_reproduced_descriptive_only"
            if philosophy_generation.get("audit_passed")
            else "generation_audit_pending_or_failed"
        ),
        "philosophy_cluster_lineage": (
            "all_clusters_structurally_reviewed_conflicts_preserved"
            if philosophy_generation.get("row_by_row_cluster_review", {}).get("passed") is True
            else "cluster_lineage_review_pending_or_failed"
        ),
        "historical_cohort_reproduction": "blocked_not_reproducible_from_available_artifacts" if not cohort["reproduced"] else "complete",
        "contract_tape": (
            "bilateral_venue_native_geometry_scaffold_ready_executable_tape_blocked"
            if bilateral_scaffold_trusted and not contracts_path.exists()
            else "geometry_scaffold_ready_executable_tape_blocked"
            if contract_scaffold.get("available") and not contracts_path.exists()
            else "gap_audit_ready_executable_tape_blocked"
            if contract_gaps.get("available") and not contracts_path.exists()
            else "not_started" if not contracts_path.exists() else "present_unverified"
        ),
        "replay_outcomes": (
            "bilateral_scaffold_outcomes_ready_non_executable_raw_audit_complete"
            if bilateral_outcomes_trusted and bilateral_raw_calibration.get("available")
            else "bilateral_scaffold_outcomes_ready_non_executable_calibration_pending"
            if bilateral_outcomes_trusted
            else "experimental_calibration_ready_not_executable"
            if scaffold_calibration.get("available") and not outcomes_path.exists()
            else "experimental_scaffold_outcomes_ready_calibration_pending"
            if scaffold_outcomes.get("available") and not outcomes_path.exists()
            else "mechanical_parity_verified_outcomes_pending"
            if replay_parity.get("parity_passed") and not outcomes_path.exists()
            else "not_started" if not outcomes_path.exists() else "present_unverified"
        ),
        "simulated_replay_calibration": (
            "cluster_heldout_replay_calibration_passed_non_executable_labels"
            if replay_calibration.get("simulated_replay_calibration_valid") is True
            else "replay_calibration_pending_or_holdout_failed"
            if replay_calibration.get("available")
            else "not_started"
        ),
        "formula_comparison": (
            "simulated_same_tape_screen_complete_review_required_no_winner"
            if fast_formula_screen.get("screen_complete") is True
            else "simulated_same_tape_screen_in_progress_or_failed"
            if fast_formula_screen.get("available") is True
            else "present_unverified" if formulas_path.exists() else "not_started"
        ),
        "staging_canary": (
            "prospective_preflight_ready_not_authorized"
            if prospective_pilot_scope.get("canary_ready_now") is True
            else "prospective_preflight_blocked"
        ),
        "full_sample": "not_started",
    }
    blockers = [
        "Historical bilateral geometry is simulated with predeclared capital, not a reconstruction of a 2025 sandbox account; historical filters and observed decision-time balances remain unresolved.",
        "Only two archived Spot Production filter snapshots were recovered outside the pilot windows; historical Demo/Testnet executability is unknown and must not be asserted from simulated labels.",
        "Train-only probability calibration on executable outcomes is pending; raw/scaffold audits do not unlock formula scoring.",
        "The known 1,154-row/161-entry cohort has not been reproduced from available row-level artifacts.",
    ]
    if funding_history.get("available") and funding_history.get("source_hashes_match"):
        blockers.append(
            "Public Production USD-M funding has verified development-window observations for 26 pilot contracts, "
            "but historical Testnet funding, decision-time fees/slippage and fills remain unknown."
        )
    elif funding_history.get("available"):
        blockers.append("Recovered public funding hashes do not match; do not use this evidence.")
    if sandbox_commissions.get("capture_complete"):
        blockers.append("Current Spot Demo/Futures Testnet commission rates are captured by account, but historical 2025 fees/fills are not; use them only in explicitly labelled sensitivity scenarios.")
    if cost_sensitivity.get("available") and cost_sensitivity.get("source_hashes_match"):
        blockers.append("Historical price-path cost sensitivity is available for declared fee/slippage/funding scenarios; it is non-executable and cannot unlock formula scoring.")
    elif cost_sensitivity.get("available"):
        blockers.append("Cost sensitivity source hashes do not match; do not use its scenario summaries.")
    if replay_calibration.get("simulated_replay_calibration_valid"):
        blockers.append("Predictor probabilities have a temporal cluster-held-out calibration against simulated replay labels only; executable-outcome calibration remains pending and formula scoring stays disabled.")
    else:
        blockers.append("Exact-thesis train-only replay calibration is missing or failed its holdout/hash checks.")
    if fast_formula_screen.get("screen_complete") is True:
        blockers.append("Fast same-tape formula screen completed on simulated scaffold labels only; it is review evidence, not executable-fill validation or a formula winner.")
        blockers.append("Fast formula screen is not decision-ready: common runtime hard gates, predictor-weight point-in-time provenance, exact runtime baseline, no-trade abstention and multiplicity adjustment are not fully represented.")
        if fast_formula_screen.get("pending_canonical_families"):
            blockers.append(f"Canonical philosophy families remain pending in the fast tape: {', '.join(fast_formula_screen['pending_canonical_families'])}.")
    if not prospective_baseline.get("available"):
        blockers.append("No frozen prospective Staging feasibility baseline has been recorded.")
    elif not prospective_baseline.get("source_integrity_verified"):
        blockers.append("The frozen prospective baseline or one of its bound code/input hashes has changed; recapture the baseline before considering a canary.")
    failed_prospective_gates = [key for key, value in prospective_offline_gates.items() if value is not True]
    if failed_prospective_gates:
        blockers.append(f"Prospective offline feasibility gates are not all verified: {', '.join(failed_prospective_gates)}.")
    if not prospective_pilot_scope.get("canary_ready_now"):
        blockers.append("Prospective Staging canary remains blocked pending a fresh passing single-position preflight and current verified filters/commissions; no order is authorized.")
    if prospective_baseline.get("pilot_limits_verified") is not True:
        blockers.append("Pilot safety limits are not fully verified: numeric order and per-mode caps are configured, but the global one-position invariant is not proven.")
    if prospective_baseline.get("cross_venue_single_position_guard_verified") is not True:
        blockers.append("Staging has per-regime and per-side slot caps but no atomic global Spot/Futures one-position guard; keep the canary blocked until that cross-venue guard is implemented and verified.")
    if staging_preflight.get("available") and staging_preflight.get("fresh_within_300_seconds"):
        if staging_preflight.get("runtime_preflight_passed") is not True:
            blockers.append("Latest live Staging preflight failed one or more runtime checks, including wallet-position or freshness gates.")
        if staging_preflight.get("offline_gates_ready") is not True:
            blockers.append("Latest live wallet readiness does not override the incomplete offline contract, calibration, or cohort gates.")
    if bilateral_contract_scaffold.get("available") and not bilateral_scaffold_trusted:
        blockers.append("Bilateral scaffold source integrity or artifact hash failed; regenerate only from verified normalized development data.")
    if bilateral_scaffold_outcomes.get("available") and not bilateral_outcomes_trusted:
        blockers.append("Bilateral replay is not bound to the current verified scaffold and normalized Parquet sources.")
    if prospective_filters.get("server_time_checks", {}).get("spot_demo") != "fresh_within_tolerance":
        blockers.append("Latest Spot Demo exchangeInfo and /time freshness check failed.")
    testnet_reasons = prospective_filters.get("testnet_currentness_evidence", {}).get("blocking_reasons", [])
    if testnet_reasons:
        blockers.append(f"Latest USD-M Testnet /time, HTTP Date, filter or capture-age check failed: {', '.join(testnet_reasons)}.")
    return {
        "status_version": "universal_entry_pilot_status_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pilot_complete": False,
        "production_touched": False,
        "orders_sent_by_pilot_preparation": False,
        "phases": phases,
        "evidence_scopes": evidence_scopes,
        "prospective_pilot_scope": prospective_pilot_scope,
        "data": {"development": development, "validation_prepared_not_scored": validation},
        "proposals": proposals,
        "philosophy_generation_audit": philosophy_generation,
        "contract_evidence_gaps": contract_gaps,
        "contract_scaffold": contract_scaffold,
        "bilateral_contract_scaffold": bilateral_contract_scaffold,
        "replay_parity": replay_parity,
        "scaffold_outcomes": scaffold_outcomes,
        "bilateral_scaffold_outcomes": bilateral_scaffold_outcomes,
        "scaffold_calibration": scaffold_calibration,
        "bilateral_raw_calibration": bilateral_raw_calibration,
        "bilateral_thesis_matrix": bilateral_matrix,
        "prediction_inputs": prediction_inputs,
        "raw_predictions": raw_predictions,
        "raw_calibration_audit": raw_calibration_audit,
        "historical_cohort": cohort,
        "historical_filter_evidence": filters_history,
        "historical_funding_evidence": funding_history,
        "current_sandbox_commission_snapshot": sandbox_commissions,
        "cost_sensitivity_scenarios": cost_sensitivity,
        "simulated_replay_calibration": replay_calibration,
        "fast_formula_screen": fast_formula_screen,
        "prospective_baseline": prospective_baseline,
        "prospective_offline_gates": prospective_offline_gates,
        "latest_staging_preflight": staging_preflight,
        "historical_feature_sources": feature_sources,
        "prospective_sandbox_filter_snapshot": prospective_filters,
        "blockers": blockers,
        "next_step": "Review the fast simulated same-tape family/formula screen; it has no winner authority and four canonical families remain pending. Do not change Staging until a candidate is selected and explicitly approved. Keep the historical 1,154/161 cohort unresolved and separate.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_STATUS_PATH)
    parser.add_argument("--no-write", action="store_true", help="Print status without writing the JSON report")
    args = parser.parse_args()
    status = build_status()
    rendered = json.dumps(status, ensure_ascii=False, indent=2, allow_nan=False)
    if not args.no_write:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(f".{args.output.name}.tmp")
        temporary.write_text(rendered + "\n", encoding="utf-8")
        temporary.replace(args.output)
        status["status_file"] = str(args.output.relative_to(REPO_ROOT)).replace("\\", "/")
    print(json.dumps(status, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())