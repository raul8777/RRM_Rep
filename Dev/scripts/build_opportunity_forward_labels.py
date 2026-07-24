from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from opportunity_labels import build_forward_labels, round_trip_cost_pct
DEFAULT_INPUT = REPO_ROOT / "historical_data"
DEFAULT_OUTPUT = REPO_ROOT / "Dev" / "benchmarks" / "research_data" / "opportunity_forward_labels_1h.jsonl"


def _records(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    required = ("open_time", "high", "low", "close")
    if any(column not in frame.columns for column in required):
        return []
    frame = frame.loc[:, required].copy()
    frame = frame.dropna().sort_values("open_time").drop_duplicates("open_time")
    return frame.to_dict(orient="records")


def _files(input_dir: Path) -> Iterable[Path]:
    yield from sorted(input_dir.glob("*_1h.parquet"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--side", choices=("LONG", "SHORT"), default="LONG")
    parser.add_argument("--commission-rate", type=float, default=0.0005)
    parser.add_argument("--slippage-pct", type=float, default=0.00052)
    args = parser.parse_args()

    cost = round_trip_cost_pct(args.commission_rate, args.slippage_pct)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    symbols = 0
    rows = 0
    with args.output.open("w", encoding="utf-8") as handle:
        for path in _files(args.input_dir):
            records = _records(pd.read_parquet(path))
            labels = build_forward_labels(records, horizons_bars=(2, 4), side=args.side, round_trip_cost_pct_value=cost)
            if not labels:
                continue
            symbols += 1
            symbol = path.stem.removesuffix("_1h")
            for label in labels:
                label.update({
                    "dataset_version": "opportunity_forward_labels_v1",
                    "symbol": symbol,
                    "interval": "1h",
                    "horizons_minutes": {"2h": 120, "4h": 240},
                })
                handle.write(json.dumps(label, ensure_ascii=False, default=str) + "\n")
                rows += 1
    print(json.dumps({
        "dataset_version": "opportunity_forward_labels_v1",
        "symbols": symbols,
        "rows": rows,
        "side": args.side,
        "round_trip_cost_pct": cost,
        "output": str(args.output),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
