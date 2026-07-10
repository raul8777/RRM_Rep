"""Regression test for MIMIC simulation baseline.

Runs a short MIMIC simulation and checks that key metrics stay within a
 tolerance band against a recorded baseline. This guards against accidental
 changes in production logic that break the simulator.
"""
import os
import sys
import json
import pytest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import get_settings
from simulation.mimic_runner import SimulationRunner


SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "ADAUSDT", "SOLUSDT", "XLMUSDT", "UNIUSDT", "BCHUSDT", "NEARUSDT"]
START = datetime(2026, 3, 4, 12, tzinfo=timezone.utc)
END = datetime(2026, 3, 5, 12, tzinfo=timezone.utc)
BASELINE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "Dev", "benchmarks", "mimic_baseline.json")

TOLERANCE = {
    "total_value": 50.0,  # USDT
    "pnl_usdt": 50.0,
    "buy_count": 2,
    "sell_count": 2,
}


@pytest.fixture
def simulation_result():
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
    settings.min_signal_strength = 0.45
    settings.min_success_probability = 0.50
    settings.stop_loss_pct = 3.36
    settings.kelly_max_usdt = 1000.0

    runner = SimulationRunner(
        settings=settings,
        symbols=SYMBOLS,
        interval="1h",
        start=START,
        end=END,
        initial_usdt=2910.0,
        fear_greed_value=50.0,
        data_dir=os.path.join(os.path.dirname(os.path.dirname(__file__)), "historical_data"),
    )
    return runner.run()


def _compute_metrics(result: dict) -> dict:
    final_balance = result.get("final_balance", {})
    usdt = float(final_balance.get("USDT", 0.0))
    holdings = 0.0
    for asset, qty in final_balance.items():
        if asset == "USDT":
            continue
        # Value approximation not needed for baseline; use raw balance for regression
        holdings += float(qty)

    buy_count = sell_count = sl_tp_count = 0
    for cycle in result.get("results", []):
        r = cycle.get("result", {})
        buy_count += len(r.get("buy_orders", []) or [])
        sell_count += len(r.get("sell_orders", []) or [])
        sl_tp_count += len(r.get("sl_tp_closes", []) or [])

    return {
        "total_value": round(usdt, 2),
        "holdings_qty": round(holdings, 6),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "sl_tp_count": sl_tp_count,
        "cycles": result.get("cycles", 0),
    }


def test_mimic_regression(simulation_result, request):
    metrics = _compute_metrics(simulation_result)

    # Record/update baseline if requested via --mimic-update-baseline
    update_baseline = request.config.getoption("--mimic-update-baseline", False)
    if update_baseline:
        os.makedirs(os.path.dirname(BASELINE_PATH), exist_ok=True)
        with open(BASELINE_PATH, "w", encoding="utf-8") as f:
            json.dump(metrics, f, indent=2)
        pytest.skip("Baseline updated")

    if not os.path.exists(BASELINE_PATH):
        pytest.skip(f"Baseline not found at {BASELINE_PATH}; run with --mimic-update-baseline to create it")

    with open(BASELINE_PATH, "r", encoding="utf-8") as f:
        baseline = json.load(f)

    for key, tol in TOLERANCE.items():
        actual = metrics.get(key)
        expected = baseline.get(key)
        if actual is None or expected is None:
            continue
        assert abs(actual - expected) <= tol, (
            f"Regression mismatch for {key}: actual={actual}, expected={expected}, tolerance={tol}"
        )
