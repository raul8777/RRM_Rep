"""lifecycle/mimic_baseline_report.py — generate a quick MIMIC baseline report.

Usage:
    python lifecycle/mimic_baseline_report.py > Dev/benchmarks/lifecycle_baseline_report.json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from lifecycle.mimic_baseline_eval import (
    spot_bull_a_config,
    sample_spot_bull_analyses,
    sample_price_series,
    run_baseline_window,
)
from lifecycle.super_mimic_integration import lifecycle_config_to_dict


def main() -> None:
    config = spot_bull_a_config()
    analyses = sample_spot_bull_analyses()
    prices = sample_price_series()
    atr_map = {"BTCUSDT": 900.0, "SOLUSDT": 1.8, "FETUSDT": 0.04}
    rsi_map = {"BTCUSDT": 50.0, "SOLUSDT": 50.0, "FETUSDT": 50.0}

    adapter, metrics, trace = run_baseline_window(
        config,
        analyses,
        prices,
        atr_map,
        rsi_map,
        total_liquid=5000.0,
    )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "regime": config.regime,
        "profile": config.profile,
        "config": lifecycle_config_to_dict(config),
        "sweep_reference": "Dev/sweep_analysis_SPOT_BULL_20260713.md Step 7 best SPOT_BULL-A",
        "window_steps": trace["steps"],
        "metrics": metrics,
        "final_positions": [
            {
                "symbol": p.symbol,
                "side": p.side.value,
                "qty": p.quantity,
                "entry": p.entry_price,
            }
            for p in adapter.positions
        ],
        "orders": adapter.orders,
    }
    json.dump(report, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
