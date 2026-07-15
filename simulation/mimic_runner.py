"""MIMIC — Simulation runner that executes production trading logic against historical data.

This runner replicates the production cycle as faithfully as possible:
- Regime is detected dynamically using historical BTC data.
- Profile can be forced so each simulation evaluates one A/B/C variant.
- Cycle interval is taken from execute_once's `next_cycle_minutes` (production
  scheduling) rather than a fixed historical bar step.
- Transition metrics are collected so the sweep can optimize PnL over time.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from simulation.mimic_protocols import SimulationClock
from simulation.mimic_clients import SimulationBinanceClient, SimulationFuturesClient
from simulation.mimic_news_client import SimulationNewsClient
from simulation.mimic_feed import HistoricalDataFeed
from simulation.mimic_db import SimulationDatabaseClient
from simulation.mimic_historical_screener import HistoricalScreener


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
        forced_profile: Optional[str] = None,
        profile_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        self.settings = settings
        # Ensure BTC is available for regime detection
        self.symbols = list(dict.fromkeys([*symbols, "BTCUSDT"]))
        self.interval = interval
        self.data_dir = data_dir
        self.feed = HistoricalDataFeed(data_dir=data_dir)
        # Load primary interval plus the three intervals used by market_regime
        self.feed.load_symbols(self.symbols, interval)
        for _iv in ("4h", "1d"):
            try:
                self.feed.load_symbols(["BTCUSDT"], _iv)
            except Exception:
                pass

        # Determine time range from data
        all_times = []
        for symbol in self.symbols:
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
        self.news_client = SimulationNewsClient(
            fear_greed_value=fear_greed_value,
            data_dir=data_dir,
        )
        self.price_feed: Dict[str, List[Dict[str, Any]]] = {
            s: self.feed.get_klines(s, interval) for s in self.symbols
        }
        self.btc_feeds: Dict[str, List[Dict[str, Any]]] = {
            _iv: self.feed.get_klines("BTCUSDT", _iv) for _iv in ("1h", "4h", "1d")
        }
        self.spot_client = SimulationBinanceClient(
            clock=self.clock,
            initial_balances={"USDT": initial_usdt},
        )
        self.spot_client.set_price_feed(self.price_feed)
        self.spot_client._feed_obj = self.feed  # D4: expose feed for sub-hourly resampling
        self.futures_client = SimulationFuturesClient(
            clock=self.clock,
            price_feed=self.price_feed,
            data_dir=data_dir,
        )
        # D2: Historical screener for regime-aware symbol selection
        self._historical_screener = HistoricalScreener(
            ticker_dir=os.path.join(data_dir, "external"),
        )
        self.db_client = SimulationDatabaseClient(
            forced_profile=forced_profile,
            profile_overrides=profile_overrides,
        )

        self.results: List[Dict[str, Any]] = []
        self._last_mode: Optional[str] = None
        self._transition_log: List[Dict[str, Any]] = []

    def build_components(self) -> Any:
        from init_components import build_components
        return build_components(
            settings=self.settings,
            binance_client=self.spot_client,
            news_client=self.news_client,
            db_client=self.db_client,
            futures_client=self.futures_client,
        )

    def _invalidate_caches(self) -> None:
        """Clear production caches that would otherwise leak between cycles."""
        try:
            from autonomous_signals import AutonomousSignalGenerator
            AutonomousSignalGenerator._signal_cache.clear()
        except Exception:
            pass
        try:
            from analysis_cache import analysis_cache
            analysis_cache.clear()
        except Exception:
            pass
        try:
            from pair_screener import invalidate_cache as invalidate_screener
            invalidate_screener()
        except Exception:
            pass

    def _inject_historical_screener(self) -> None:
        """D2: Monkey-patch pair_screener to serve historical data when screener_enabled=True."""
        screener = self._historical_screener
        clock = self.clock
        settings = self.settings
        try:
            import pair_screener as _ps
            _original_screen = getattr(_ps, '_original_screen_fn', None)
            if _original_screen is None:
                # First time: save original and replace
                _ps._original_screen_fn = getattr(_ps, 'screen_pairs', None)

            def _historical_screen(regime=None, **kwargs):
                regime = regime or str(getattr(settings, 'current_regime_mode', 'SPOT_BULL'))
                rows = screener.screen(clock.now(), regime, settings)
                return [r['symbol'] for r in rows]

            if hasattr(_ps, 'screen_pairs'):
                _ps.screen_pairs = _historical_screen
        except Exception:
            pass

    def run_cycle(self, components: Optional[Any] = None) -> Dict[str, Any]:
        if components is None:
            components = self.build_components()
        from run_trades import execute_once
        return execute_once(monitor_only=False, components=components)

    def run(self, max_cycles: Optional[int] = None) -> Dict[str, Any]:
        components = self.build_components()
        cycle = 0
        current = self.start
        next_cycle_minutes: Optional[int] = None
        last_successful_step: Optional[int] = None
        while current <= self.end:
            self.clock._now = current
            self._invalidate_caches()
            self._inject_historical_screener()  # D2: update screener for current clock
            self.news_client.update_for_date(current)
            try:
                result = self.run_cycle(components=components)
                _balance = self.spot_client.get_account_balance()
                _total = float(_balance.get("USDT", 0.0))
                for _asset, _qty in _balance.items():
                    if _asset == "USDT":
                        continue
                    _price = self.spot_client.get_symbol_price(f"{_asset}USDT", use_cache=False)
                    _total += float(_qty) * _price

                _mode = str(result.get("regime_mode", "SPOT_BULL")).upper()
                if self._last_mode is not None and _mode != self._last_mode:
                    self._transition_log.append({
                        "cycle": cycle,
                        "timestamp": self.clock.now().isoformat(),
                        "from_mode": self._last_mode,
                        "to_mode": _mode,
                        "policy": result.get("transition_policy", {}),
                    })
                self._last_mode = _mode

                _usdt_balance = float(_balance.get("USDT", 0.0))
                self.results.append({
                    "cycle": cycle,
                    "timestamp": self.clock.now().isoformat(),
                    "result": result,
                    "total_value": _total,
                    "usdt_balance": _usdt_balance,
                    "regime_mode": _mode,
                    "next_cycle_minutes": result.get("next_cycle_minutes"),
                })
                next_cycle_minutes = result.get("next_cycle_minutes")
                if next_cycle_minutes is not None:
                    last_successful_step = next_cycle_minutes
            except Exception as e:
                print(f"[MIMIC] Cycle {cycle} failed at {self.clock.now().isoformat()}: {e}")
                # Fall back to the last successful cycle interval to stay close to production scheduling
                next_cycle_minutes = None

            cycle += 1
            # Advance by the production-suggested cycle interval, or fall back to last known step
            step_minutes = next_cycle_minutes
            if step_minutes is None:
                step_minutes = last_successful_step
            if step_minutes is None:
                step_minutes = int(self.interval.replace("h", "")) * 60 if self.interval.endswith("h") else 60
            current = current + timedelta(minutes=step_minutes)
            if max_cycles is not None and cycle >= max_cycles:
                break

        return {
            "cycles": cycle,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "final_balance": self.spot_client.get_account_balance(),
            "results": self.results,
            "transitions": self._transition_log,
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
    forced_profile: Optional[str] = None,
    profile_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
    max_cycles: Optional[int] = None,
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
        forced_profile=forced_profile,
        profile_overrides=profile_overrides,
    )
    return runner.run(max_cycles=max_cycles)
