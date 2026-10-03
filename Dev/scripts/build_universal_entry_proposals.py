"""Build development-only point-in-time proposals for the RANGE_SCALP pilot."""
from __future__ import annotations

import argparse
import itertools
import hashlib
import json
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from entry_philosophies import (
    generate_cross_sectional_relative_strength_proposals,
    generate_pilot_proposals,
)
from simulation.mimic_feed import HistoricalDataFeed


WINDOWS_DIR = REPO_ROOT / "Dev" / "benchmarks" / "windows_calibration"
NORMALIZED_ROOT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "normalized" / "pilot_development"
DEFAULT_WINDOW_ID = "cal_range_scalp_1_20250205_20250219"
DEFAULT_OUTPUT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "pilot_development" / "philosophy_proposals.jsonl"
INTERVAL_SECONDS = 3600


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def audit_proposal_clusters(proposals: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    grouped: Dict[str, list[Dict[str, Any]]] = defaultdict(list)
    for proposal in proposals:
        grouped[str(proposal["dedupe_cluster_id"])].append(proposal)

    audit = []
    for cluster_id, members in sorted(grouped.items()):
        source_observations = {
            (member["window_id"], member["symbol"], float(member["decision_time"]))
            for member in members
        }
        if len(source_observations) != 1:
            raise ValueError(f"dedupe_cluster_id spans multiple source observations: {cluster_id}")
        window_id, symbol, decision_time = next(iter(source_observations))
        audit.append({
            "dedupe_cluster_id": cluster_id,
            "window_id": window_id,
            "symbol": symbol,
            "decision_time": decision_time,
            "proposal_count": len(members),
            "opportunity_event_ids": sorted(member["opportunity_event_id"] for member in members),
            "philosophies": sorted({member["philosophy_family"] for member in members}),
            "sides": sorted({member["side"] for member in members}),
            "thesis_types": sorted({member["thesis_type"] for member in members}),
        })
    return audit


def build_development_proposals(
    *,
    window_id: str = DEFAULT_WINDOW_ID,
    normalized_root: Path = NORMALIZED_ROOT,
    output_path: Path = DEFAULT_OUTPUT,
    replace: bool = False,
) -> Dict[str, Any]:
    window_path = WINDOWS_DIR / f"{window_id}.json"
    window = json.loads(window_path.read_text(encoding="utf-8"))
    window_role = str(window.get("window_role") or "")
    if window_role not in {"development", "validation"} or str(window.get("dominant_regime", "")).upper() != "RANGE_SCALP":
        raise ValueError("the fast proposal builder accepts only RANGE_SCALP development or validation windows")
    report_path = output_path.with_suffix(".report.json")
    if not replace:
        if output_path.exists():
            raise FileExistsError(f"proposal output exists; pass --replace to replace it: {output_path}")
        if report_path.exists():
            raise FileExistsError(f"proposal report exists; pass --replace to replace it: {report_path}")

    venue_root = normalized_root
    spot_root = venue_root / "spot"
    um_root = venue_root / "um"
    symbols = [
        symbol for symbol in sorted(set(window.get("symbols", [])))
        if (spot_root / f"{symbol}_1m.manifest.json").is_file()
        and (um_root / f"{symbol}_1m.manifest.json").is_file()
    ]
    if not symbols:
        raise RuntimeError("no symbols have normalized 1m data for both Spot and USD-M")

    start = datetime.fromisoformat(window["start"]).timestamp()
    end = datetime.fromisoformat(window["end"]).timestamp()
    feed = HistoricalDataFeed(str(spot_root))
    proposals = []
    bars_by_symbol = {}
    for symbol in symbols:
        bars = feed.get_klines(symbol, "1h")
        bars_by_symbol[symbol] = bars
        proposals.extend(generate_pilot_proposals(
            symbol,
            bars,
            window_id=window_id,
            window_role=window_role,
            window_start=start,
            window_end=end,
            horizon_hours=4,
            interval_seconds=INTERVAL_SECONDS,
        ))
    proposals.extend(generate_cross_sectional_relative_strength_proposals(
        bars_by_symbol,
        window_id=window_id,
        window_role=window_role,
        window_start=start,
        window_end=end,
        horizon_hours=4,
        interval_seconds=INTERVAL_SECONDS,
        minimum_symbols=5,
    ))

    proposals.sort(key=lambda row: (
        row["decision_time"], row["symbol"], row["philosophy_family"], row["philosophy_variant"],
    ))
    event_ids = [row["opportunity_event_id"] for row in proposals]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("proposal generator emitted duplicate opportunity_event_id values")
    cluster_audit = audit_proposal_clusters(proposals)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as target:
            for row in proposals:
                target.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)

    family_counts = Counter(row["philosophy_family"] for row in proposals)
    cluster_sizes: Dict[str, int] = defaultdict(int)
    for row in proposals:
        cluster_sizes[row["dedupe_cluster_id"]] += 1
    family_pair_counts: Counter[str] = Counter()
    side_pattern_counts: Counter[str] = Counter()
    multi_side_clusters = 0
    review_examples: Dict[str, list[Dict[str, Any]]] = defaultdict(list)
    for cluster in cluster_audit:
        families = cluster["philosophies"]
        sides = cluster["sides"]
        if cluster["proposal_count"] > 1:
            side_pattern_counts["+".join(sides)] += 1
        if len(sides) > 1:
            multi_side_clusters += 1
        for left, right in itertools.combinations(families, 2):
            pair = f"{left} + {right}"
            family_pair_counts[pair] += 1
            if len(review_examples[pair]) < 3:
                review_examples[pair].append({
                    "dedupe_cluster_id": cluster["dedupe_cluster_id"],
                    "symbol": cluster["symbol"],
                    "decision_time": cluster["decision_time"],
                    "sides": sides,
                    "thesis_types": cluster["thesis_types"],
                })
    report = {
        "artifact_version": "universal_entry_philosophy_proposals_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_id": window_id,
        "window_role": window_role,
        "regime": "RANGE_SCALP",
        "symbols": symbols,
        "warmup_bars": 25,
        "horizon_minutes": 240,
        "proposal_count": len(proposals),
        "unique_event_count": len(set(event_ids)),
        "dedupe_cluster_count": len(cluster_sizes),
        "clusters_with_multiple_proposals": sum(size > 1 for size in cluster_sizes.values()),
        "clusters_with_multiple_sides": multi_side_clusters,
        "max_proposals_per_cluster": max(cluster_sizes.values(), default=0),
        "invalid_cluster_count": 0,
        "family_pair_counts": dict(sorted(family_pair_counts.items())),
        "side_pattern_counts": dict(sorted(side_pattern_counts.items())),
        "cluster_review_examples": dict(sorted(review_examples.items())),
        "cluster_audit": cluster_audit,
        "family_counts": dict(sorted(family_counts.items())),
        "source_manifest_hashes": {
            f"spot:{symbol}": _sha256(spot_root / f"{symbol}_1m.manifest.json")
            for symbol in symbols
        } | {
            f"um:{symbol}": _sha256(um_root / f"{symbol}_1m.manifest.json")
            for symbol in symbols
        },
        "generator_sha256": _sha256(REPO_ROOT / "entry_philosophies.py"),
        "proposal_file": str(output_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "proposal_file_sha256": _sha256(output_path),
        "limitations": [
            "Intermediate proposals only; no calibrated labels or executable venue contracts are attached.",
            "Spot candles generate the shared thesis stream; separate USD-M candles are retained for later contract replay.",
            "This artifact contains proposals only; it is not an outcome score or a family winner.",
        ],
    }
    _write_json_atomic(report_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-id", default=DEFAULT_WINDOW_ID)
    parser.add_argument("--normalized-root", type=Path, default=NORMALIZED_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    try:
        report = build_development_proposals(
            window_id=args.window_id,
            normalized_root=args.normalized_root,
            output_path=args.output,
            replace=args.replace,
        )
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())