from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = REPO_ROOT / "Dev" / "benchmarks" / "research_data" / "research_manifest.json"


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record(path: Path, kind: str) -> Dict[str, Any]:
    return {
        "kind": kind,
        "path": str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "bytes": path.stat().st_size,
        "sha256": _digest(path),
        "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
    }


def _files(patterns: Iterable[tuple[str, str]]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    seen: set[Path] = set()
    for pattern, kind in patterns:
        for path in sorted(REPO_ROOT.glob(pattern)):
            if not path.is_file() or path in seen:
                continue
            seen.add(path)
            records.append(_record(path, kind))
    return records


def build_manifest() -> Dict[str, Any]:
    patterns = (
        ("historical_data/manifest_*.json", "historical_manifest"),
        ("historical_data/*.parquet", "ohlcv_parquet"),
        ("historical_data/external/*.parquet", "external_parquet"),
        ("historical_data/external/manifest.json", "external_manifest"),
        ("Dev/benchmarks/peaks_dataset/peaks_*.parquet", "peaks_dataset"),
        ("Dev/benchmarks/peaks_dataset/peaks_*.json", "peaks_metadata"),
        ("Dev/benchmarks/peaks_dataset/peak_quality_model/*", "peaks_model_artifact"),
        ("Dev/benchmarks/entry_profile_calibration/*.jsonl", "entry_calibration_dataset"),
        ("Dev/benchmarks/entry_profile_calibration/*.json", "entry_calibration_report"),
        ("Dev/benchmarks/entry_profile_calibration/*.pkl", "entry_model_artifact"),
        ("Dev/benchmarks/super_mimic/**/*.json", "super_mimic_report"),
        ("Dev/benchmarks/super_mimic/**/*.jsonl", "super_mimic_dataset"),
        ("Dev/benchmarks/lifecycle_sweep*/**/*.json", "lifecycle_sweep"),
        ("Dev/benchmarks/mimic_sweep*/**/*.json", "mimic_sweep"),
    )
    records = _files(patterns)
    counts: Dict[str, int] = {}
    bytes_by_kind: Dict[str, int] = {}
    for record in records:
        kind = record["kind"]
        counts[kind] = counts.get(kind, 0) + 1
        bytes_by_kind[kind] = bytes_by_kind.get(kind, 0) + int(record["bytes"])
    return {
        "manifest_version": "research_manifest_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(REPO_ROOT),
        "source_counts": counts,
        "bytes_by_kind": bytes_by_kind,
        "records": records,
        "database_sources": [
            {"table": "trades_closed", "grain": "executed close; partial and total rows"},
            {"table": "trades_open", "grain": "active position"},
            {"table": "trades_audit", "grain": "immutable decision event"},
            {"table": "entry_selection_candidates", "grain": "candidate per cycle/stage"},
            {"table": "ready_candidates", "grain": "staged candidate"},
            {"table": "ready_candidate_checks", "grain": "staged recheck"},
            {"table": "trading_runtime_state", "grain": "effective runtime state"},
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    manifest = build_manifest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "records": len(manifest["records"]), "source_counts": manifest["source_counts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
