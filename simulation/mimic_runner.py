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
        lifecycle_exit_policy: Optional[Any] = None,
        lifecycle_time_stop_reason: Optional[str] = None,
        entry_time_stop_hours: Optional[float] = None,
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
        spot_initial_usdt = initial_usdt * 0.50
        futures_initial_usdt = initial_usdt - spot_initial_usdt
        self.spot_client = SimulationBinanceClient(
            clock=self.clock,
            initial_balances={"USDT": spot_initial_usdt},
        )
        self.spot_client.set_price_feed(self.price_feed)
        self.spot_client._feed_obj = self.feed  # D4: expose feed for sub-hourly resampling
        self.futures_client = SimulationFuturesClient(
            clock=self.clock,
            price_feed=self.price_feed,
            data_dir=data_dir,
        )
        self.futures_client._available_usdt = futures_initial_usdt
        from simulation.mimic_clients import SimulationSpotMarginClient
        self.spot_margin_client = SimulationSpotMarginClient(
            clock=self.clock,
            price_feed=self.price_feed,
            initial_usdt=spot_initial_usdt,
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

        # Wire the global analysis cache to the simulation DB so production caches
        # do not attempt to connect to a real PostgreSQL instance.
        try:
            from analysis_cache import analysis_cache as _mimic_analysis_cache
            _mimic_analysis_cache.set_db_client(self.db_client)
        except Exception:
            pass

        self.results: List[Dict[str, Any]] = []
        self._last_mode: Optional[str] = None
        self._transition_log: List[Dict[str, Any]] = []
        self.lifecycle_exit_policy = lifecycle_exit_policy
        self.lifecycle_time_stop_reason = lifecycle_time_stop_reason
        self.entry_time_stop_hours = entry_time_stop_hours

    def build_components(self) -> Any:
        from init_components import build_components
        return build_components(
            settings=self.settings,
            binance_client=self.spot_client,
            news_client=self.news_client,
            db_client=self.db_client,
            futures_client=self.futures_client,
            spot_margin_client=self.spot_margin_client,
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
        try:
            import run_trades
            run_trades._CAPITAL_RESERVATIONS.clear()
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

    def _lifecycle_atr(self, symbol: str, lookback: int = 14) -> Optional[float]:
        now = self.clock.now().timestamp()
        bars = [
            bar for bar in self.price_feed.get(symbol, [])
            if bar.get("open_time", bar.get("timestamp", 0)) <= now
        ][-(lookback + 1):]
        if len(bars) < 2:
            return None
        ranges = []
        for index in range(1, len(bars)):
            high = float(bars[index].get("high", bars[index].get("close", 0)))
            low = float(bars[index].get("low", bars[index].get("close", 0)))
            previous = float(bars[index - 1].get("close", 0))
            ranges.append(max(high - low, abs(high - previous), abs(low - previous)))
        return sum(ranges) / len(ranges) if ranges else None

    def monitor_lifecycle_exits(self, regime: str) -> List[Dict[str, Any]]:
        if self.lifecycle_exit_policy is None:
            return []
        from lifecycle.base import Action, MarketContext, Position, Side
        from lifecycle.exit import ExitEngine

        positions = []
        source_positions = []
        for source in self.futures_client._open_positions.values():
            if source.closed:
                continue
            price = self.futures_client.get_price(source.symbol)
            pnl_pct = (
                (source.entry_price - price) / source.entry_price
                if source.side == "SHORT"
                else (price - source.entry_price) / source.entry_price
            )
            source.peak_pnl_pct = max(source.peak_pnl_pct, pnl_pct)
            source_positions.append(source)
            positions.append(Position(
                symbol=source.symbol,
                side=Side.SHORT if source.side == "SHORT" else Side.LONG,
                quantity=source.qty,
                entry_price=source.entry_price,
                entry_time=source.opened_at,
                position_usdt=source.margin_usdt,
                category="FUTURES",
                regime_at_entry=str(source.metadata.get("regime_mode") or regime),
                profile_at_entry=str(source.metadata.get("profile_label") or "UNKNOWN"),
                metadata=source.metadata,
                peak_pnl_pct=source.peak_pnl_pct,
            ))
        if not positions:
            return []
        now = self.clock.now().timestamp()
        context = MarketContext(
            timestamp=now,
            regime=regime,
            regime_confidence={regime: 1.0},
            btc_7d_pct=0.0,
            btc_30d_pct=0.0,
            btc_day_pct=0.0,
            fear_greed_index=50.0,
            btc_dominance=50.0,
        )
        engine = ExitEngine(
            price_resolver=self.futures_client.get_price,
            atr_resolver=self._lifecycle_atr,
        )
        decisions = engine.evaluate(positions, context, self.lifecycle_exit_policy, now)
        closes = []
        for source, decision in zip(source_positions, decisions):
            if decision.action != Action.CLOSE_ALL:
                continue
            reason = decision.reason
            if reason == "time stop" and self.lifecycle_time_stop_reason:
                reason = self.lifecycle_time_stop_reason
            close = self.futures_client.close_position(source, reason)
            if close:
                close["lifecycle_priority"] = decision.priority
                close["lifecycle_source"] = "e2e_exit_policy"
                closes.append(close)
        return closes

    def enforce_entry_exits(self, components: Optional[Any] = None) -> Dict[str, List[Dict[str, Any]]]:
        """Enforce the minimal exit contract for entry-stage simulations.

        Production ``run_trades`` gates futures monitoring behind
        ``futures_enabled`` which is disabled inside MIMIC, so positions would
        otherwise ride until ``window_end_liquidation``. This enforces, every
        cycle, only the mechanics an entry calibration needs:

        - Futures SL/TP intrabar via ``check_positions`` (contract prices).
        - Spot SL/TP via ``position_manager.check_stop_loss_take_profit``.
        - Optional hard time stop (``entry_time_stop_hours``) for futures and
          spot positions, closing with reason ``entry_time_stop``.
        """
        closes: Dict[str, List[Dict[str, Any]]] = {"futures_closes": [], "sl_tp_closes": []}
        now = self.clock.now().timestamp()
        horizon_s = (self.entry_time_stop_hours or 0.0) * 3600.0

        try:
            closes["futures_closes"].extend(self.futures_client.check_positions(self.settings))
        except Exception:
            pass
        try:
            closes["futures_closes"].extend(self.spot_margin_client.check_positions(self.settings))
        except Exception:
            pass

        position_manager = getattr(components, "position_manager", None)
        if position_manager is not None:
            try:
                prices = {
                    symbol: self.spot_client.get_symbol_price(symbol, use_cache=False)
                    for symbol in position_manager.get_all_positions().keys()
                }
                closed = position_manager.check_stop_loss_take_profit(prices)
                for cp in closed or []:
                    closes["sl_tp_closes"].append({
                        "symbol": cp.symbol,
                        "reason": cp.close_reason,
                        "price": float(cp.exit_price or 0),
                        "order": {},
                        "pnl_usdt": round(float(cp.pnl or 0), 4),
                        "pnl_pct": round(float(cp.pnl_percentage or 0) * 100, 4),
                    })
            except Exception:
                pass

        if horizon_s > 0:
            for pos in list(self.futures_client._open_positions.values()):
                if pos.closed or now - float(pos.opened_at or now) < horizon_s:
                    continue
                close = self.futures_client.close_position(pos, "entry_time_stop")
                if close:
                    closes["futures_closes"].append(close)
            if position_manager is not None:
                for symbol, pos_list in list(position_manager.get_all_positions().items()):
                    stale = False
                    for pos in pos_list:
                        entry_time = getattr(pos, "entry_time", None)
                        if isinstance(entry_time, datetime):
                            age = now - entry_time.timestamp()
                        else:
                            age = now - float(entry_time or now)
                        if age >= horizon_s:
                            stale = True
                    if stale:
                        price = self.spot_client.get_symbol_price(symbol, use_cache=False)
                        if price and price > 0:
                            closed = position_manager.close_all_positions_for_symbol(
                                symbol, price, close_reason="entry_time_stop",
                                commission_rate=getattr(self.settings, "binance_spot_commission_rate", 0.001),
                                exit_context={"regime_mode": self._last_mode},
                            )
                            for cp in closed or []:
                                closes["sl_tp_closes"].append({
                                    "symbol": cp.symbol,
                                    "reason": cp.close_reason,
                                    "price": float(cp.exit_price or 0),
                                    "order": {},
                                    "pnl_usdt": round(float(cp.pnl or 0), 4),
                                    "pnl_pct": round(float(cp.pnl_percentage or 0) * 100, 4),
                                })
        return closes

    def run_cycle(self, components: Optional[Any] = None) -> Dict[str, Any]:
        if components is None:
            components = self.build_components()
        from run_trades import execute_once
        result = execute_once(monitor_only=False, components=components)
        lifecycle_closes = self.monitor_lifecycle_exits(str(result.get("regime_mode") or "SPOT_BULL"))
        if lifecycle_closes:
            result.setdefault("futures_closes", []).extend(lifecycle_closes)
        entry_closes = self.enforce_entry_exits(components)
        if entry_closes["futures_closes"]:
            result.setdefault("futures_closes", []).extend(entry_closes["futures_closes"])
        if entry_closes["sl_tp_closes"]:
            result.setdefault("sl_tp_closes", []).extend(entry_closes["sl_tp_closes"])
        return result

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
