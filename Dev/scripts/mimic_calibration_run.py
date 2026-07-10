"""MIMIC single-run calibration — run one configuration over a historical window and report metrics."""
import os
import sys
import json
from datetime import datetime, timezone

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo_root)

from config import get_settings
from simulation.mimic_runner import SimulationRunner


SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "ADAUSDT", "SOLUSDT", "XLMUSDT", "UNIUSDT", "BCHUSDT", "NEARUSDT"]
START = datetime(2026, 3, 4, 12, tzinfo=timezone.utc)
END = datetime(2026, 3, 31, tzinfo=timezone.utc)
INITIAL_USDT = 2910.0


def _clear_global_caches():
    try:
        from autonomous_signals import AutonomousSignalGenerator
        AutonomousSignalGenerator._signal_cache.clear()
    except Exception:
        pass
    try:
        from pair_screener import invalidate_cache
        invalidate_cache()
    except Exception:
        pass


def run_config(
    min_signal_strength: float = 0.45,
    min_success_probability: float = 0.50,
    stop_loss_pct: float = 3.36,
    kelly_max_usdt: float = 1000.0,
    label: str = "baseline",
) -> dict:
    _clear_global_caches()
    settings = get_settings()
    settings.dry_run = True
    settings.allow_real_trading = False
    settings.binance_use_testnet = True
    settings.agent_mode = "auto"
    settings.market_regime_enabled = True
    settings.futures_enabled = False
    settings.screener_enabled = False
    settings.screener_top_n_enabled = False
    settings.analysis_symbols = SYMBOLS
    settings.min_signal_strength = min_signal_strength
    settings.min_success_probability = min_success_probability
    settings.stop_loss_pct = stop_loss_pct
    settings.kelly_max_usdt = kelly_max_usdt
    settings.kelly_base_usdt = 100.0
    settings.max_positions_spot_bull = 8
    settings.max_positions_range_scalp = 6
    settings.category_max_positions_enabled = False

    runner = SimulationRunner(
        settings=settings,
        symbols=SYMBOLS,
        interval="1h",
        start=START,
        end=END,
        initial_usdt=INITIAL_USDT,
        fear_greed_value=50.0,
        data_dir=os.path.join(repo_root, "historical_data"),
    )
    result = runner.run(max_cycles=20)

    final_balance = result.get("final_balance", {})
    usdt = float(final_balance.get("USDT", 0.0))
    holdings_value = 0.0
    for asset, qty in final_balance.items():
        if asset == "USDT":
            continue
        price = runner.spot_client.get_symbol_price(f"{asset}USDT", use_cache=False)
        holdings_value += float(qty) * price
    total_value = usdt + holdings_value
    pnl_usdt = total_value - INITIAL_USDT
    pnl_pct = pnl_usdt / INITIAL_USDT * 100.0

    buy_count = sell_count = sl_tp_count = 0
    for cycle in result.get("results", []):
        r = cycle.get("result", {})
        buy_count += len(r.get("buy_orders", []) or [])
        sell_count += len(r.get("sell_orders", []) or [])
        sl_tp_count += len(r.get("sl_tp_closes", []) or [])

    return {
        "label": label,
        "params": {
            "min_signal_strength": min_signal_strength,
            "min_success_probability": min_success_probability,
            "stop_loss_pct": stop_loss_pct,
            "kelly_max_usdt": kelly_max_usdt,
        },
        "start": START.isoformat(),
        "end": END.isoformat(),
        "cycles": result.get("cycles", 0),
        "final_balance": final_balance,
        "total_value": round(total_value, 2),
        "pnl_usdt": round(pnl_usdt, 2),
        "pnl_pct": round(pnl_pct, 4),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "sl_tp_count": sl_tp_count,
    }


def main():
    configs = [
        (0.45, 0.50, 3.36, 1000.0, "relaxed"),
        (0.50, 0.52, 3.36, 1000.0, "canonical"),
        (0.55, 0.55, 3.36, 1000.0, "strict"),
        (0.45, 0.50, 3.36, 1200.0, "relaxed_more_capital"),
    ]

    reports = []
    for min_str, min_prob, sl, kmax, label in configs:
        print(f"\nRunning {label}: str={min_str} prob={min_prob} sl={sl} kmax={kmax}")
        try:
            report = run_config(min_str, min_prob, sl, kmax, label)
            reports.append(report)
            print(f"  -> pnl={report['pnl_usdt']:.2f} USDT ({report['pnl_pct']:.2f}%), buys={report['buy_count']}, sells={report['sell_count']}")
        except Exception as e:
            print(f"  -> FAILED: {e}")
            reports.append({"label": label, "error": str(e)})

    reports.sort(key=lambda x: x.get("pnl_pct", -1e9), reverse=True)

    out_path = os.path.join(repo_root, "Dev", "benchmarks", "mimic_calibration_30d.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "reports": reports,
        }, f, indent=2, ensure_ascii=False)
    print(f"\nCalibration report saved to {out_path}")
    if reports and "pnl_usdt" in reports[0]:
        print(f"Best config: {reports[0]['label']} -> pnl={reports[0]['pnl_usdt']} USDT")


if __name__ == "__main__":
    main()
