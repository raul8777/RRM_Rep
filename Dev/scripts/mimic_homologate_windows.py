from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo_root)

from super_mimic.window_certification import _audit_parquet


def _covers(check: Dict[str, Any], start: pd.Timestamp, end: pd.Timestamp) -> bool:
    return bool(
        check.get("ok")
        and check.get("start")
        and check.get("end")
        and pd.Timestamp(check["start"]) <= start
        and pd.Timestamp(check["end"]) >= end
    )


def homologate_manifest(manifest: Dict[str, Any], data_dir: str, min_symbols: int = 4) -> Dict[str, Any]:
    start = pd.Timestamp(manifest["start"])
    end = pd.Timestamp(manifest["end"])
    requested = manifest.get("symbols") or manifest.get("baseline_symbols") or []
    eligible: List[str] = []
    rejected: Dict[str, Any] = {}
    for symbol in requested:
        check = _audit_parquet(os.path.join(data_dir, f"{symbol}_1h.parquet"), "1h")
        if _covers(check, start, end):
            eligible.append(symbol)
        else:
            rejected[symbol] = check.get("errors") or ["window_not_covered"]
    payload = dict(manifest)
    payload["source_window_id"] = manifest.get("window_id")
    payload["window_id"] = f"homo_{manifest.get('window_id')}"
    payload["symbols"] = eligible
    payload["baseline_symbols"] = eligible
    payload["homologation"] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "requested_symbols": len(requested),
        "eligible_symbols": len(eligible),
        "rejected_symbols": rejected,
        "minimum_symbols": min_symbols,
        "passed": len(eligible) >= min_symbols,
        "source_manifest_hash": hashlib.sha256(
            json.dumps(manifest, sort_keys=True, default=str).encode()
        ).hexdigest(),
    }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--windows-dir", default=os.path.join(repo_root, "Dev", "benchmarks", "windows"))
    parser.add_argument("--data-dir", default=os.path.join(repo_root, "historical_data"))
    parser.add_argument("--output-dir", default=os.path.join(repo_root, "Dev", "benchmarks", "windows_homologated"))
    parser.add_argument("--min-symbols", type=int, default=4)
    args = parser.parse_args()
    os.makedirs(args.output_dir, exist_ok=True)
    report = []
    for name in sorted(os.listdir(args.windows_dir)):
        if not name.endswith(".json") or name.startswith("homo_"):
            continue
        with open(os.path.join(args.windows_dir, name), "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        payload = homologate_manifest(manifest, args.data_dir, args.min_symbols)
        output = os.path.join(args.output_dir, f"{payload['window_id']}.json")
        with open(output, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        report.append({
            "window_id": payload["window_id"],
            **payload["homologation"],
            "output": output,
        })
    report_path = os.path.join(args.output_dir, "homologation_report.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({
        "total": len(report),
        "passed": sum(item["passed"] for item in report),
        "failed": sum(not item["passed"] for item in report),
        "report": report_path,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
