from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List, Tuple

import pandas as pd

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo_root)

from super_mimic.window_certification import _audit_parquet


def _timestamps(values: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(values):
        return pd.to_datetime(values, utc=True)
    numeric = pd.to_numeric(values, errors="coerce")
    unit = "ms" if float(numeric.dropna().abs().median()) >= 100_000_000_000 else "s"
    return pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")


def _hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _symbol_coverage(data_dir: str, start: pd.Timestamp, end: pd.Timestamp) -> List[str]:
    eligible = []
    for name in sorted(os.listdir(data_dir)):
        if not name.endswith("_1h.parquet"):
            continue
        symbol = name[:-11]
        check = _audit_parquet(os.path.join(data_dir, name), "1h")
        if not check.get("ok"):
            continue
        if pd.Timestamp(check["start"]) <= start and pd.Timestamp(check["end"]) >= end:
            eligible.append(symbol)
    return eligible


def _non_overlapping(candidates: pd.Series, count: int, minimum_days: int = 18) -> List[pd.Timestamp]:
    selected: List[pd.Timestamp] = []
    for timestamp in candidates.index:
        if all(abs((timestamp - existing).days) >= minimum_days for existing in selected):
            selected.append(timestamp)
        if len(selected) >= count:
            break
    return selected


def _build_candidates(btc_path: str) -> Dict[str, List[pd.Timestamp]]:
    frame = pd.read_parquet(btc_path, columns=["open_time", "close"])
    frame["timestamp"] = _timestamps(frame["open_time"])
    daily = frame.dropna(subset=["timestamp"]).set_index("timestamp")["close"].astype(float).resample("1D").last().dropna()
    returns = daily.pct_change(14).dropna()
    volatility = daily.pct_change().rolling(14).std().dropna()
    aligned = pd.DataFrame({"return": returns, "volatility": volatility}).dropna()
    spot = aligned.sort_values(["return", "volatility"], ascending=[False, True])["return"]
    futures = aligned[aligned.index >= pd.Timestamp("2026-01-15", tz="UTC")].sort_values("return")["return"]
    preservation_pool = aligned[(aligned.index >= pd.Timestamp("2026-01-15", tz="UTC")) & (aligned["return"] < -0.03)]
    preservation = preservation_pool.assign(score=preservation_pool["return"].abs() + preservation_pool["volatility"]).sort_values("score", ascending=False)["score"]
    range_pool = aligned[aligned["return"].abs() <= 0.035]
    range_series = range_pool.assign(score=range_pool["return"].abs() + range_pool["volatility"]).sort_values("score")["score"]
    transition_score = aligned["return"].diff(7).abs().sort_values(ascending=False)
    return {
        "SPOT_BULL": _non_overlapping(spot, 5),
        "RANGE_SCALP": _non_overlapping(range_series, 5),
        "PRESERVATION": _non_overlapping(preservation, 5),
        "FUTURES_SHORT": _non_overlapping(futures, 5),
        "TRANSITION": _non_overlapping(transition_score, 4),
    }


def main() -> int:
    data_dir = os.path.join(repo_root, "historical_data")
    output_dir = os.path.join(repo_root, "Dev", "benchmarks", "windows_calibration")
    os.makedirs(output_dir, exist_ok=True)
    candidates = _build_candidates(os.path.join(data_dir, "BTCUSDT_1h.parquet"))
    report = []
    for regime, ends in candidates.items():
        for index, end in enumerate(ends, 1):
            start = end - pd.Timedelta(days=14)
            symbols = _symbol_coverage(data_dir, start, end)
            window_id = f"cal_{regime.lower()}_{index}_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}"
            manifest = {
                "window_id": window_id,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "symbols": symbols,
                "baseline_symbols": symbols,
                "dominant_regime": regime,
                "forced_regime": None if regime == "TRANSITION" else regime,
                "window_role": "validation" if index > 3 and regime != "TRANSITION" else "development",
                "data_manifest": {
                    symbol: _hash(os.path.join(data_dir, f"{symbol}_1h.parquet"))
                    for symbol in symbols
                },
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
            path = os.path.join(output_dir, f"{window_id}.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=2)
            report.append({"window_id": window_id, "regime": regime, "symbols": len(symbols), "role": manifest["window_role"]})
    with open(os.path.join(output_dir, "window_plan.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({
        "total": len(report),
        "by_regime": {regime: len(ends) for regime, ends in candidates.items()},
        "minimum_symbols": min(item["symbols"] for item in report),
        "output_dir": output_dir,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
