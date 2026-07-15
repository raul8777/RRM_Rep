"""MIMIC — Paper trade mode: run production cycle against live prices without sending real orders.

Accumulates fictitious PnL over a configurable window (default 48h) using
the same SimulationBinanceClient but seeded with live prices fetched from
Binance at each cycle instead of historical parquet data.

Lifecycle:
  start()  → spawns background thread, writes state to mimic_paper_state.json
  status() → returns current PnL, open positions, elapsed time
  stop()   → graceful stop, writes final report to Dev/benchmarks/mimic_paper_<ts>.json
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional


_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Dev", "benchmarks", "mimic_paper_state.json",
)
_REPORT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Dev", "benchmarks",
)

_DEFAULT_DURATION_HOURS = 48
_DEFAULT_CYCLE_MINUTES = 60
_DEFAULT_INITIAL_USDT = 2910.0


def _write_state(state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
    with open(_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _read_state() -> Dict[str, Any]:
    if not os.path.exists(_STATE_PATH):
        return {"status": "idle"}
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"status": "idle"}


class PaperTradeRunner:
    """Runs paper trade cycles in a background thread."""

    def __init__(
        self,
        settings: Any,
        symbols: List[str],
        binance_client: Any,
        duration_hours: float = _DEFAULT_DURATION_HOURS,
        cycle_minutes: int = _DEFAULT_CYCLE_MINUTES,
        initial_usdt: float = _DEFAULT_INITIAL_USDT,
    ):
        self.settings = settings
        self.symbols = symbols
        self.binance_client = binance_client
        self.duration_hours = duration_hours
        self.cycle_minutes = cycle_minutes
        self.initial_usdt = initial_usdt
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._results: List[Dict[str, Any]] = []

    def _seed_price_feed(self) -> Dict[str, List[Dict[str, Any]]]:
        """Build a single-bar price feed from current live prices for paper mode."""
        feed: Dict[str, List[Dict[str, Any]]] = {}
        now_ts = datetime.now(timezone.utc).timestamp()
        for sym in self.symbols:
            try:
                price = float(self.binance_client.get_symbol_price(sym))
                feed[sym] = [{
                    "open_time": now_ts,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 1.0,
                    "quote_volume": price * 1.0,
                    "trades": 1,
                    "taker_buy_base": 0.5,
                    "taker_buy_quote": price * 0.5,
                }]
            except Exception:
                pass
        return feed

    def _run(self) -> None:
        from simulation.mimic_protocols import SimulationClock
        from simulation.mimic_clients import SimulationBinanceClient, SimulationFuturesClient
        from simulation.mimic_news_client import SimulationNewsClient
        from simulation.mimic_db import SimulationDatabaseClient
        from init_components import build_components
        from run_trades import execute_once

        started_at = datetime.now(timezone.utc)
        end_at = started_at + timedelta(hours=self.duration_hours)

        clock = SimulationClock(started_at)
        spot_client = SimulationBinanceClient(
            clock=clock,
            initial_balances={"USDT": self.initial_usdt},
        )
        news_client = SimulationNewsClient(fear_greed_value=50.0)
        db_client = SimulationDatabaseClient()

        price_feed = self._seed_price_feed()
        spot_client.set_price_feed(price_feed)
        futures_client = SimulationFuturesClient(clock=clock, price_feed=price_feed)

        components = build_components(
            settings=self.settings,
            binance_client=spot_client,
            news_client=news_client,
            db_client=db_client,
            futures_client=futures_client,
        )

        cycle = 0
        state = {
            "status": "running",
            "started_at": started_at.isoformat(),
            "end_at": end_at.isoformat(),
            "duration_hours": self.duration_hours,
            "cycle_minutes": self.cycle_minutes,
            "initial_usdt": self.initial_usdt,
            "cycles": 0,
            "pnl_usdt": 0.0,
            "pnl_pct": 0.0,
            "current_value": self.initial_usdt,
            "open_positions": [],
            "results": [],
        }
        _write_state(state)

        while not self._stop_event.is_set():
            now = datetime.now(timezone.utc)
            if now >= end_at:
                break

            # Refresh price feed from live data
            live_feed = self._seed_price_feed()
            spot_client.set_price_feed(live_feed)
            futures_client._price_feed = live_feed
            clock._now = now

            try:
                result = execute_once(monitor_only=False, components=components)
                balance = spot_client.get_account_balance()
                total = float(balance.get("USDT", 0.0))
                for asset, qty in balance.items():
                    if asset == "USDT":
                        continue
                    sym = f"{asset}USDT"
                    try:
                        price = float(self.binance_client.get_symbol_price(sym))
                        total += float(qty) * price
                    except Exception:
                        pass

                self._results.append({
                    "cycle": cycle,
                    "timestamp": now.isoformat(),
                    "total_value": total,
                    "usdt_balance": float(balance.get("USDT", 0.0)),
                    "result": result,
                })
                pnl_usdt = round(total - self.initial_usdt, 2)
                pnl_pct = round(pnl_usdt / self.initial_usdt * 100, 4) if self.initial_usdt > 0 else 0.0
                state.update({
                    "cycles": cycle + 1,
                    "pnl_usdt": pnl_usdt,
                    "pnl_pct": pnl_pct,
                    "current_value": round(total, 2),
                    "last_cycle_at": now.isoformat(),
                    "results": self._results[-20:],  # keep last 20 for state file
                })
                _write_state(state)
            except Exception as e:
                state["last_error"] = str(e)
                _write_state(state)

            cycle += 1
            self._stop_event.wait(timeout=self.cycle_minutes * 60)

        # Write final report
        state["status"] = "completed"
        state["completed_at"] = datetime.now(timezone.utc).isoformat()
        _write_state(state)

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        report_path = os.path.join(_REPORT_DIR, f"mimic_paper_{ts}.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump({**state, "results": self._results}, f, indent=2, ensure_ascii=False)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="mimic-paper-trader")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)


# Process-global runner instance
_runner: Optional[PaperTradeRunner] = None


def start_paper_trade(
    settings: Any,
    symbols: List[str],
    binance_client: Any,
    duration_hours: float = _DEFAULT_DURATION_HOURS,
    cycle_minutes: int = _DEFAULT_CYCLE_MINUTES,
    initial_usdt: float = _DEFAULT_INITIAL_USDT,
) -> Dict[str, Any]:
    """Start a new paper trade session. Stops any existing one first."""
    global _runner
    if _runner is not None:
        _runner.stop()
    _runner = PaperTradeRunner(
        settings=settings,
        symbols=symbols,
        binance_client=binance_client,
        duration_hours=duration_hours,
        cycle_minutes=cycle_minutes,
        initial_usdt=initial_usdt,
    )
    _runner.start()
    return get_paper_status()


def stop_paper_trade() -> Dict[str, Any]:
    global _runner
    if _runner is not None:
        _runner.stop()
        _runner = None
    return get_paper_status()


def get_paper_status() -> Dict[str, Any]:
    return _read_state()
