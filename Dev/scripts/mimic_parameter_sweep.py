"""MIMIC parameter sweep — compare configurations over historical data."""
import os
import sys
import json
import itertools
from datetime import datetime, timezone
from copy import deepcopy
from typing import Any, Dict, List

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo_root)

from config import get_settings
from simulation.mimic_runner import SimulationRunner


SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "ADAUSDT", "SOLUSDT", "XLMUSDT", "UNIUSDT", "BCHUSDT", "NEARUSDT"]
START = datetime(2026, 3, 4, 12, tzinfo=timezone.utc)
END = datetime(2026, 3, 5, 12, tzinfo=timezone.utc)


PARAM_GRID = {
    "min_signal_strength": [0.45, 0.65],
    "min_success_probability": [0.50, 0.60],
    "stop_loss_pct": [3.36],
    "kelly_max_usdt": [1000.0],
}


def _base_settings() -> Any:
    settings = get_settings()
    # Reset all fields we will sweep to deterministic defaults before applying params
    settings.dry_run = True
    settings.allow_real_trading = False
    settings.binance_use_testnet = True
    settings.agent_mode = "auto"
    settings.market_regime_enabled = True
    settings.futures_enabled = False
    settings.screener_enabled = False
    settings.screener_top_n_enabled = False
    settings.analysis_symbols = SYMBOLS
    settings.min_signal_strength = 0.60
    settings.min_success_probability = 0.60
    settings.stop_loss_pct = 3.36
    settings.kelly_max_usdt = 1000.0
    settings.kelly_base_usdt = 100.0
    settings.max_positions_spot_bull = 8
    settings.max_positions_range_scalp = 6
    settings.category_max_positions_enabled = False
    return settings


def _apply_params(settings: Any, params: Dict[str, Any]) -> Any:
    for key, value in params.items():
        setattr(settings, key, value)
    return settings


def _run_simulation(params: Dict[str, Any]) -> Dict[str, Any]:
    settings = _apply_params(_base_settings(), params)
    runner = SimulationRunner(
        settings=settings,
        symbols=SYMBOLS,
        interval="1h",
        start=START,
        end=END,
        initial_usdt=2910.0,
        fear_greed_value=50.0,
        data_dir=os.path.join(repo_root, "historical_data"),
    )
    result = runner.run()

    final_balance = result.get("final_balance", {})
    initial_usdt = 2910.0
    usdt_now = float(final_balance.get("USDT", 0.0))
    holdings_value = 0.0
    for asset, qty in final_balance.items():
        if asset == "USDT":
            continue
        # Approximate holding value using last known price from spot client
        price = runner.spot_client.get_symbol_price(f"{asset}USDT", use_cache=False)
        holdings_value += float(qty) * price

    total_value = usdt_now + holdings_value
    pnl_pct = (total_value - initial_usdt) / initial_usdt * 100.0

    buy_count = 0
    sell_count = 0
    sl_tp_count = 0
    for cycle in result.get("results", []):
        r = cycle.get("result", {})
        buy_count += len(r.get("buy_orders", []) or [])
        sell_count += len(r.get("sell_orders", []) or [])
        sl_tp_count += len(r.get("sl_tp_closes", []) or [])

    return {
        "params": params,
        "cycles": result.get("cycles", 0),
        "final_balance": final_balance,
        "total_value": round(total_value, 2),
        "pnl_usdt": round(total_value - initial_usdt, 2),
        "pnl_pct": round(pnl_pct, 4),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "sl_tp_count": sl_tp_count,
    }


def main():
    keys = list(PARAM_GRID.keys())
    values = [PARAM_GRID[k] for k in keys]
    combinations = [dict(zip(keys, combo)) for combo in itertools.product(*values)]

    print(f"Running MIMIC parameter sweep with {len(combinations)} configurations...")
    reports: List[Dict[str, Any]] = []
    for i, params in enumerate(combinations, 1):
        print(f"\n[{i}/{len(combinations)}] params={params}")
        try:
            report = _run_simulation(params)
            reports.append(report)
            print(f"  -> pnl={report['pnl_usdt']:.2f} USDT ({report['pnl_pct']:.2f}%), buys={report['buy_count']}, sells={report['sell_count']}")
        except Exception as e:
            print(f"  -> FAILED: {e}")
            reports.append({"params": params, "error": str(e)})

    reports.sort(key=lambda x: x.get("pnl_pct", -1e9), reverse=True)

    # Summary statistics
    valid_reports = [r for r in reports if "pnl_pct" in r]
    summary = {
        "total_combinations": len(combinations),
        "successful_runs": len(valid_reports),
        "failed_runs": len(reports) - len(valid_reports),
        "best_pnl_pct": max((r.get("pnl_pct", -1e9) for r in valid_reports), default=None),
        "worst_pnl_pct": min((r.get("pnl_pct", 1e9) for r in valid_reports), default=None),
        "avg_pnl_pct": round(sum(r.get("pnl_pct", 0.0) for r in valid_reports) / len(valid_reports), 4) if valid_reports else None,
        "best_config": reports[0].get("params") if reports else None,
        "best_pnl_usdt": reports[0].get("pnl_usdt") if reports else None,
    }

    out_path = os.path.join(repo_root, "Dev", "benchmarks", "mimic_sweep_report.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "start": START.isoformat(),
            "end": END.isoformat(),
            "symbols": SYMBOLS,
            "combinations": len(combinations),
            "summary": summary,
            "results": reports,
        }, f, indent=2, ensure_ascii=False)
    print(f"\nSweep report saved to {out_path}")
    print(f"Best config: {summary.get('best_config')} -> pnl={summary.get('best_pnl_usdt')} USDT")
    print(f"Summary: {summary}")


if __name__ == "__main__":
    main()
