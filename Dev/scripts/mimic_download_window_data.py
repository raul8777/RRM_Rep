from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, Tuple

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo_root)

from Dev.scripts.download_historical_data import download_symbol


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--windows-dir", default=os.path.join(repo_root, "Dev", "benchmarks", "windows_calibration"))
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    coverage: Dict[str, Tuple[datetime, datetime]] = {}
    for name in os.listdir(args.windows_dir):
        if not name.endswith(".json") or name == "window_plan.json":
            continue
        with open(os.path.join(args.windows_dir, name), "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        start = datetime.fromisoformat(manifest["start"])
        end = datetime.fromisoformat(manifest["end"])
        for symbol in manifest.get("symbols", []):
            current = coverage.get(symbol)
            coverage[symbol] = (
                min(start, current[0]) if current else start,
                max(end, current[1]) if current else end,
            )
    results = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 10))) as executor:
        futures = {
            executor.submit(download_symbol, symbol, start, end, args.interval): (symbol, start, end)
            for symbol, (start, end) in coverage.items()
        }
        for future in as_completed(futures):
            symbol, start, end = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {"symbol": symbol, "error": str(exc), "downloaded": 0}
            result["requested_start"] = start.isoformat()
            result["requested_end"] = end.isoformat()
            results.append(result)
            print(json.dumps(result, default=str))
    report = {
        "interval": args.interval,
        "workers": max(1, min(args.workers, 10)),
        "symbols": len(coverage),
        "completed": sum(not result.get("error") and result.get("downloaded", 0) > 0 for result in results),
        "failed": [result for result in results if result.get("error") or not result.get("downloaded")],
        "results": results,
    }
    report_path = os.path.join(repo_root, "Dev", "benchmarks", f"window_data_download_{args.interval}.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
    print(json.dumps({key: report[key] for key in ("interval", "workers", "symbols", "completed", "failed")}, indent=2))
    return 0 if not report["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
