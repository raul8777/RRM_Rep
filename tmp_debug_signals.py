"""tmp_debug_signals.py — print why BUY signals are not generated."""
from __future__ import annotations

import json
import os
import sys

repo_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, repo_root)

from simulation.mimic_lifecycle_adapter import _build_analyses
from simulation.mimic_feed import HistoricalDataFeed


def main() -> int:
    feed = HistoricalDataFeed(os.path.join(repo_root, "historical_data"))
    window_path = os.path.join(repo_root, "Dev", "benchmarks", "windows", "win_20260415_20260430.json")
    with open(window_path, "r", encoding="utf-8") as f:
        w = json.load(f)

    available = [
        s for s in w["symbols"]
        if os.path.exists(os.path.join(repo_root, "historical_data", f"{s}_1h.parquet"))
    ]
    print(f"Available: {len(available)} symbols")

    price_feed = {s: feed.get_klines(s, "1h") for s in available}
    for s in available[:5]:
        print(f"  {s}: {len(price_feed[s])} bars")

    analyses = _build_analyses(available, price_feed)
    buy_signals = [a for a in analyses if a["signal"] == "BUY"]
    print(f"\nTotal analyses: {len(analyses)}")
    print(f"BUY signals: {len(buy_signals)}")

    if buy_signals:
        for a in buy_signals[:10]:
            print(f"  {a['symbol']}: range={a['range_position']:.2f} vol={a['vol_spike']:.2f} rsi={a['rsi_4h']:.1f} pnl={a['expected_pnl']:.2f}")
    else:
        print("\nFirst 10 HOLD signals:")
        for a in analyses[:10]:
            print(f"  {a['symbol']}: range={a['range_position']:.2f} vol={a['vol_spike']:.2f} rsi={a['rsi_4h']:.1f} pnl={a['expected_pnl']:.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
