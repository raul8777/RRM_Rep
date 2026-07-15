"""MIMIC — Run a single realistic simulation with dynamic regime detection.

Usage:
    python Dev/scripts/mimic_single_simulation.py \
        --start 2026-06-19 --end 2026-06-26 --symbols ADAUSDT,BNBUSDT,BTCUSDT \
        --profile A --data-dir historical_data
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo_root)

from config import get_settings
from simulation.mimic_runner import run_mimic_simulation


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a single MIMIC realistic simulation")
    parser.add_argument("--start", required=True, help="Start date ISO")
    parser.add_argument("--end", required=True, help="End date ISO")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT,BNBUSDT,ADAUSDT,SOLUSDT",
                        help="Comma-separated symbol list")
    parser.add_argument("--profile", choices=["A", "B", "C"], default="B",
                        help="Force profile for the simulation")
    parser.add_argument("--data-dir", default="historical_data")
    parser.add_argument("--initial-usdt", type=float, default=2910.0)
    parser.add_argument("--max-cycles", type=int, default=None)
    parser.add_argument("--market-regime-enabled", action="store_true", default=True,
                        help="Enable dynamic regime detection (default)")
    parser.add_argument("--output", default="Dev/benchmarks/mimic_single_result.json")
    args = parser.parse_args()

    settings = get_settings()
    settings.market_regime_enabled = args.market_regime_enabled
    settings.agent_mode = "auto"
    settings.dry_run = True
    settings.allow_real_trading = False
    settings.screener_enabled = False
    settings.screener_top_n_enabled = False

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    settings.analysis_symbols = list(symbols)
    settings.futures_symbols = [s for s in symbols if s in ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT")] or list(symbols)
    settings.narrative_tokens = [s for s in symbols if s in settings.narrative_tokens] or list(symbols)

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    result = run_mimic_simulation(
        settings=settings,
        symbols=symbols,
        interval="1h",
        start=start,
        end=end,
        initial_usdt=args.initial_usdt,
        data_dir=args.data_dir,
        forced_profile=args.profile,
        max_cycles=args.max_cycles,
    )

    # Strip non-serializable objects and summarize
    summary = {
        "start": result.get("start"),
        "end": result.get("end"),
        "cycles": result.get("cycles"),
        "final_balance": result.get("final_balance"),
        "transitions": result.get("transitions", []),
        "regime_distribution": {},
    }
    for r in result.get("results", []):
        mode = r.get("regime_mode", "UNKNOWN")
        summary["regime_distribution"][mode] = summary["regime_distribution"].get(mode, 0) + 1

    initial_usdt = args.initial_usdt
    final_total = summary["final_balance"].get("USDT", initial_usdt)
    for asset, qty in summary["final_balance"].items():
        if asset == "USDT":
            continue
        # approximate
        pass
    summary["pnl_pct"] = round((final_total - initial_usdt) / initial_usdt * 100, 2)
    summary["pnl_usdt"] = round(final_total - initial_usdt, 2)

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
