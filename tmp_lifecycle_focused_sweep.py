"""Focused lifecycle kernel sweep on a single window to validate exit fix.

Runs a small grid (3 profiles x 3 param sets) on win_20260415_20260430
with max 361 cycles and reports trade/exit metrics.
"""
import os
import sys
import json
from datetime import datetime, timezone
from itertools import product

repo_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, repo_root)

from config import get_settings
from simulation.mimic_lifecycle_adapter import run_lifecycle_simulation

WINDOW = {
    "window_id": "win_20260415_20260430",
    "start": "2026-04-15T00:00:00+00:00",
    "end": "2026-04-30T00:00:00+00:00",
    "symbols": ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "UNIUSDT", "NEARUSDT", "AAVEUSDT"],
}

PARAM_SETS = [
    {"min_signal_strength": 0.50, "min_success_probability": 0.50, "stop_loss_pct": 2.5, "kelly_max_usdt": 1000.0, "kelly_dynamic_base_pct": 0.22, "max_positions_spot_bull": 5},
    {"min_signal_strength": 0.55, "min_success_probability": 0.55, "stop_loss_pct": 3.36, "kelly_max_usdt": 1000.0, "kelly_dynamic_base_pct": 0.18, "max_positions_spot_bull": 5},
    {"min_signal_strength": 0.60, "min_success_probability": 0.60, "stop_loss_pct": 4.5, "kelly_max_usdt": 1200.0, "kelly_dynamic_base_pct": 0.18, "max_positions_spot_bull": 8},
]

PROFILE_MULTIPLIERS = {"A": 0.8, "B": 1.0, "C": 1.25}


def _build_profile_overrides(profile: str, base: dict) -> dict:
    mult = PROFILE_MULTIPLIERS[profile]
    cycles = {
        "cycle_spot_bull": int(round(10 * mult)),
        "cycle_range_scalp": int(round(3 * mult)),
        "cycle_preservation": int(round(45 * mult)),
        "cycle_futures_short": int(round(4 * mult)),
    }
    return {
        f"SPOT_BULL_{profile}": cycles,
        f"RANGE_SCALP_{profile}": cycles,
        f"PRESERVATION_{profile}": cycles,
        f"FUTURES_SHORT_{profile}": cycles,
    }


def _run(profile: str, params: dict):
    settings = get_settings()
    settings.dry_run = True
    settings.allow_real_trading = False
    return run_lifecycle_simulation(
        settings=settings,
        symbols=WINDOW["symbols"],
        interval="1h",
        start=datetime.fromisoformat(WINDOW["start"]),
        end=datetime.fromisoformat(WINDOW["end"]),
        initial_usdt=2910.0,
        forced_profile=profile,
        lifecycle_config=None,
        profile_overrides=_build_profile_overrides(profile, params),
        data_dir=os.path.join(repo_root, "historical_data"),
        max_cycles=361,
    )


def main():
    results = []
    for profile in ["A", "B", "C"]:
        for params in PARAM_SETS:
            res = _run(profile, params)
            summary = res.get("summary", {})
            record = {
                "window": WINDOW["window_id"],
                "profile": profile,
                "params": params,
                "pnl_usdt": summary.get("pnl_usdt", 0.0),
                "pnl_pct": summary.get("pnl_pct", 0.0),
                "trades": summary.get("trades", 0),
                "buy_orders": summary.get("buy_orders", 0),
                "sell_orders": summary.get("sell_orders", 0),
                "max_drawdown_pct": summary.get("max_drawdown_pct", 0.0),
                "final_balance_usdt": summary.get("final_balance_usdt", 0.0),
                "cycles": res.get("cycles", 0),
            }
            results.append(record)
            print(json.dumps(record, indent=2, ensure_ascii=False))

    # Summary
    total_trades = sum(r["trades"] for r in results)
    avg_pnl_pct = sum(r["pnl_pct"] for r in results) / len(results)
    best = max(results, key=lambda x: x["pnl_pct"])
    print(f"\n=== SUMMARY ===")
    print(f"runs: {len(results)}")
    print(f"avg pnl_pct: {avg_pnl_pct:.2f}%")
    print(f"total trades: {total_trades}")
    print(f"best: profile={best['profile']} pnl_pct={best['pnl_pct']:.2f}% trades={best['trades']}")

    out_path = os.path.join(repo_root, "Dev", "benchmarks", "lifecycle_focused_sweep_20260713.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "results": results}, f, indent=2, ensure_ascii=False)
    print(f"saved: {out_path}")


if __name__ == "__main__":
    main()
