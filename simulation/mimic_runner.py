"""MIMIC — Simulation runner that executes production trading logic against historical data."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from simulation.mimic_protocols import SimulationClock
from simulation.mimic_clients import SimulationBinanceClient, SimulationFuturesClient
from simulation.mimic_news_client import SimulationNewsClient
from simulation.mimic_feed import HistoricalDataFeed
from simulation.mimic_db import SimulationDatabaseClient


class SimulationRunner:
    """Run the production trading cycle over historical data."""

    def __init__(
        self,
        settings: Any,
        symbols: List[str],
        interval: str = "1h",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        initial_usdt: float = 2910.0,
        fear_greed_value: float = 50.0,
        data_dir: str = "historical_data",
    ):
        self.settings = settings
        self.symbols = symbols
        self.interval = interval
        self.feed = HistoricalDataFeed(data_dir=data_dir)
        self.feed.load_symbols(symbols, interval)

        # Determine time range from data
        all_times = []
        for symbol in symbols:
            bars = self.feed.get_klines(symbol, interval)
            if bars:
                all_times.extend([b["open_time"] for b in bars])
        if not all_times:
            raise ValueError("No historical data loaded")
        min_ts = min(all_times)
        max_ts = max(all_times)
        self.start = start or datetime.fromtimestamp(min_ts, tz=timezone.utc)
        self.end = end or datetime.fromtimestamp(max_ts, tz=timezone.utc)

        self.clock = SimulationClock(self.start)
        self.news_client = SimulationNewsClient(fear_greed_value=fear_greed_value)
        self.price_feed: Dict[str, List[Dict[str, Any]]] = {
            s: self.feed.get_klines(s, interval) for s in symbols
        }
        self.spot_client = SimulationBinanceClient(
            clock=self.clock,
            initial_balances={"USDT": initial_usdt},
        )
        self.spot_client.set_price_feed(self.price_feed)
        self.futures_client = SimulationFuturesClient(
            clock=self.clock,
            price_feed=self.price_feed,
        )
        self.db_client = SimulationDatabaseClient()

        self.results: List[Dict[str, Any]] = []

    def build_components(self) -> Any:
        from init_components import build_components
        return build_components(
            settings=self.settings,
            binance_client=self.spot_client,
            news_client=self.news_client,
            db_client=self.db_client,
            futures_client=self.futures_client,
        )

    def run_cycle(self, components: Optional[Any] = None) -> Dict[str, Any]:
        if components is None:
            components = self.build_components()
        from run_trades import execute_once
        return execute_once(monitor_only=False, components=components)

    def run(self, max_cycles: Optional[int] = None) -> Dict[str, Any]:
        components = self.build_components()
        cycle = 0
        current = self.start
        while current <= self.end:
            self.clock._now = current
            # Invalidate autonomous signal cache so each cycle uses fresh historical data
            try:
                from autonomous_signals import AutonomousSignalGenerator
                AutonomousSignalGenerator._signal_cache.clear()
            except Exception:
                pass
            try:
                result = self.run_cycle(components=components)
                self.results.append({
                    "cycle": cycle,
                    "timestamp": self.clock.now().isoformat(),
                    "result": result,
                })
            except Exception as e:
                print(f"[MIMIC] Cycle {cycle} failed at {self.clock.now().isoformat()}: {e}")
            cycle += 1
            # Advance by interval minutes
            minutes = int(self.interval.replace("h", "")) * 60 if self.interval.endswith("h") else 60
            current = current + timedelta(minutes=minutes)
            if max_cycles is not None and cycle >= max_cycles:
                break
        return {
            "cycles": cycle,
            "final_balance": self.spot_client.get_account_balance(),
            "results": self.results,
        }


def run_mimic_simulation(
    settings: Any,
    symbols: List[str],
    interval: str = "1h",
    days: int = 7,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    initial_usdt: float = 2910.0,
    fear_greed_value: float = 50.0,
    data_dir: str = "historical_data",
) -> Dict[str, Any]:
    """Convenience entry point for a single MIMIC simulation."""
    if end is None:
        end = datetime.now(timezone.utc)
    if start is None:
        start = end - timedelta(days=days)
    runner = SimulationRunner(
        settings=settings,
        symbols=symbols,
        interval=interval,
        start=start,
        end=end,
        initial_usdt=initial_usdt,
        fear_greed_value=fear_greed_value,
        data_dir=data_dir,
    )
    return runner.run()
