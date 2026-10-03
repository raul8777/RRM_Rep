from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo_root)

from Dev.scripts.download_historical_data import download_symbol


def main() -> int:
    windows_dir = os.path.join(repo_root, "Dev", "benchmarks", "windows_calibration")
    target_regimes = {"TRANSITION", "FUTURES_SHORT"}
    results = []
    for name in sorted(os.listdir(windows_dir)):
        if not name.endswith(".json") or name == "window_plan.json":
            continue
        with open(os.path.join(windows_dir, name), "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        if manifest.get("dominant_regime") not in target_regimes:
            continue
        start = datetime.fromisoformat(manifest["start"])
        end = datetime.fromisoformat(manifest["end"])
        symbols = manifest.get("symbols", [])[:8]
        with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as executor:
            futures = {
                executor.submit(download_symbol, symbol, start, end, "1m"): symbol
                for symbol in symbols
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"symbol": symbol, "downloaded": 0, "error": str(exc)}
                result["window_id"] = manifest["window_id"]
                results.append(result)
                print(json.dumps(result))
    report = {
        "windows": len({result["window_id"] for result in results}),
        "jobs": len(results),
        "completed": sum(not result.get("error") and result.get("downloaded", 0) > 0 for result in results),
        "failed": [result for result in results if result.get("error") or not result.get("downloaded")],
        "results": results,
    }
    path = os.path.join(repo_root, "Dev", "benchmarks", "window_data_download_1m_stress.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({key: report[key] for key in ("windows", "jobs", "completed", "failed")}, indent=2))
    return 0 if not report["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
