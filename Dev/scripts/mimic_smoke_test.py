"""MIMIC smoke test — validates the simulation engine runs without crashing."""
import os
import sys

# Force UTF-8 output on Windows to avoid charmap codec errors
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo_root)

from datetime import datetime, timezone, timedelta
from config import get_settings
from simulation.mimic_runner import SimulationRunner


def main():
    settings = get_settings()
    # Force safe simulation flags
    settings.dry_run = True
    settings.allow_real_trading = False
    settings.binance_use_testnet = True
    settings.agent_mode = "auto"
    settings.market_regime_enabled = True
    settings.futures_enabled = False  # Start spot-only to reduce complexity
    # Data available in historical_data/ ranges from 2026-03-03 to 2026-07-01
    # Use a window with valid BUY signals (March 2026) to validate trade execution
    symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "ADAUSDT", "SOLUSDT", "XLMUSDT", "UNIUSDT", "BCHUSDT", "NEARUSDT"]

    settings.screener_enabled = False  # Use static symbols from the feed
    settings.screener_top_n_enabled = False
    settings.analysis_symbols = symbols
    settings.min_signal_strength = 0.45  # Lower threshold for simulation to validate execution
    settings.min_success_probability = 0.52
    start = datetime(2026, 3, 4, 12, tzinfo=timezone.utc)
    end = datetime(2026, 3, 5, 12, tzinfo=timezone.utc)

    runner = SimulationRunner(
        settings=settings,
        symbols=symbols,
        interval="1h",
        start=start,
        end=end,
        initial_usdt=2910.0,
        fear_greed_value=50.0,
        data_dir=os.path.join(repo_root, "historical_data"),
    )
    result = runner.run(max_cycles=50)
    print("MIMIC smoke test result:")
    print(f"  cycles run: {result['cycles']}")
    print(f"  final balance: {result['final_balance']}")
    print(f"  first cycle result: {result['results'][0]['result'] if result['results'] else None}")


if __name__ == "__main__":
    main()
