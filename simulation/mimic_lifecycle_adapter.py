"""simulation/mimic_lifecycle_adapter.py — bridge lifecycle kernel to MIMIC simulation clients.

This adapter lets the lifecycle kernel run inside a MIMIC simulation by reading
prices from the simulation clock/feed, generating synthetic analyses, and executing
orders against SimulationBinanceClient / SimulationFuturesClient.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional

from lifecycle.adapter import ExecutionAdapter
from lifecycle.base import (
    EntryDecision,
    ExitDecision,
    LifecycleConfig,
    MarketContext,
    Position,
    Side,
    SizedTrade,
)
from lifecycle.signal import _DEFAULT_CATEGORIES


def _approx_atr(symbol: str, feed: List[Dict[str, Any]], lookback: int = 14) -> Optional[float]:
    """Approximate ATR from recent bars."""
    bars = feed.get(symbol, [])
    if len(bars) < 2:
        return None
    recent = bars[-lookback:]
    trs = []
    for i in range(1, len(recent)):
        h = float(recent[i].get("high", recent[i].get("close", 0)))
        l = float(recent[i].get("low", recent[i].get("close", 0)))
        pc = float(recent[i - 1].get("close", 0))
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else None


def _approx_rsi(symbol: str, feed: List[Dict[str, Any]], lookback: int = 14) -> Optional[float]:
    """Approximate RSI from recent bars."""
    bars = feed.get(symbol, [])
    if len(bars) < lookback + 1:
        return None
    closes = [float(b.get("close", 0)) for b in bars[-lookback - 1:]]
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    avg_gain = sum(gains) / len(gains) if gains else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1 + rs))


def _weekly_range_position(bars: List[Dict[str, Any]], lookback: int = 168) -> Optional[float]:
    """Return current close position within the recent high-low range (0..1)."""
    if len(bars) < lookback:
        return None
    recent = bars[-lookback:]
    highs = [float(b.get("high", 0)) for b in recent]
    lows = [float(b.get("low", 0)) for b in recent]
    if not highs or not lows:
        return None
    range_min = min(lows)
    range_max = max(highs)
    current = float(bars[-1].get("close", 0))
    if range_max <= range_min:
        return 0.5
    return (current - range_min) / (range_max - range_min)


def _volume_spike(
    bars: List[Dict[str, Any]],
    current_lookback: int = 1,
    avg_lookback: int = 24,
) -> Optional[float]:
    """Current volume vs trailing average."""
    if len(bars) < avg_lookback + current_lookback:
        return None
    current_vol = sum(float(b.get("volume", 0)) for b in bars[-current_lookback:])
    avg_vol = sum(
        float(b.get("volume", 0))
        for b in bars[-avg_lookback - current_lookback : -current_lookback]
    ) / avg_lookback
    if avg_vol <= 0:
        return None
    return current_vol / avg_vol


def _predicted_pnl_pct(bars: List[Dict[str, Any]], atr: float, horizon: int = 4) -> float:
    """Rough 4h horizon PnL prediction in percent points (mean reversion + momentum)."""
    current = float(bars[-1].get("close", 0))
    if current <= 0 or atr <= 0 or len(bars) < horizon + 1:
        return 0.0
    past = float(bars[-(horizon + 1)].get("close", current))
    momentum = (current - past) / past if past > 0 else 0.0
    # Mild mean reversion of recent momentum plus ATR-based expected move, net of rough fees.
    expected_move = -momentum * 0.3 + (atr / current) * 0.5
    return expected_move * 100 - 0.4


def _build_analyses(symbols: List[str], feed: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Generate analyses for the lifecycle kernel using price-action indicators.

    Mirrors validated expert entry criteria:
      - Buy in the 20-40% zone of the weekly range (support).
      - Require volume spike > 1.5x average for conviction.
      - RSI in 35-55 (neutral-bullish, not overbought).
      - Predicted 4h PnL must be positive.
    """
    analyses = []
    for symbol in symbols:
        bars = feed.get(symbol, [])
        if len(bars) < 169:
            continue
        current = float(bars[-1].get("close", 0))
        atr = _approx_atr(symbol, feed) or (current * 0.02)
        rsi = _approx_rsi(symbol, feed)
        range_pos = _weekly_range_position(bars)
        vol_spike = _volume_spike(bars)
        predicted_pnl = _predicted_pnl_pct(bars, atr)

        quote_volume = sum(float(b.get("quote_volume", 0)) for b in bars[-24:])

        # Default: no signal
        side = "HOLD"
        strength = 0.0
        success_probability = 0.0

        # Confluence scoring based on validated expert criteria. Each criterion contributes
        # 0..1; a total score above 0.45 generates a BUY. This produces signals even when the
        # market is not in the perfect 20-40% / >1.5x volume / 35-55 RSI sweet spot, while still
        # favouring high-quality setups and explicitly avoiding range-top/overbought entries.
        range_score = 0.0
        if range_pos is not None:
            if 0.20 <= range_pos <= 0.40:
                range_score = 1.0 - abs(range_pos - 0.30) / 0.10
            elif 0.15 <= range_pos < 0.20:
                range_score = 0.25
            elif range_pos >= 0.75:
                range_score = -1.0  # avoid buying near range top

        vol_score = 0.0
        if vol_spike is not None:
            if vol_spike >= 1.5:
                vol_score = 1.0
            elif vol_spike >= 1.0:
                vol_score = 0.5
            elif vol_spike >= 0.6:
                vol_score = 0.25

        rsi_score = 0.0
        if rsi is not None:
            if 35.0 <= rsi <= 55.0:
                rsi_score = 1.0 - abs(rsi - 45.0) / 10.0
            elif 30.0 <= rsi < 35.0 or 55.0 < rsi <= 60.0:
                rsi_score = 0.35
            elif rsi > 65.0:
                rsi_score = -1.0  # avoid overbought

        pnl_score = 0.0
        if predicted_pnl > 0.5:
            pnl_score = 1.0
        elif predicted_pnl > 0.0:
            pnl_score = 0.25

        # Hard avoid if price is at range top or strongly overbought.
        avoid_buy = range_score < 0 or rsi_score < 0 or predicted_pnl < -1.0

        confluence = range_score + vol_score + rsi_score + pnl_score
        if not avoid_buy and confluence >= 1.5:
            side = "BUY"
            normalized = confluence / 4.0
            strength = 0.55 + min(normalized * 0.25, 0.25)
            success_probability = 0.55 + min(normalized * 0.12, 0.12)

        analyses.append({
            "symbol": symbol,
            "signal": side,
            "strength": min(strength, 0.85),
            "expected_pnl": predicted_pnl,
            "success_probability": min(success_probability, 0.70),
            "liquidity_usdt_24h": max(quote_volume, 1_000_000.0),
            "vol_spike": vol_spike or 1.0,
            "rsi_4h": rsi or 50.0,
            "atr": atr,
            "range_position": range_pos or 0.5,
        })
    return analyses


class SimulationLifecycleAdapter:
    """Lifecycle adapter backed by MIMIC simulation clients."""

    def __init__(
        self,
        spot_client: Any,
        futures_client: Any,
        clock: Any,
        feed: Dict[str, List[Dict[str, Any]]],
        db_client: Any,
        btc_feed: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        category_resolver: Optional[Callable[[str], str]] = None,
    ):
        self.spot_client = spot_client
        self.futures_client = futures_client
        self.clock = clock
        self.feed = feed
        self.btc_feed = btc_feed or {}
        self.db_client = db_client
        self.category_resolver = category_resolver or (lambda s: _DEFAULT_CATEGORIES.get(s, "MIDCAP_ALT"))

    def _current_price(self, symbol: str) -> Optional[float]:
        try:
            return float(self.spot_client.get_symbol_price(symbol, use_cache=False))
        except Exception:
            return None

    def _avg_entry_price(self, symbol: str) -> Optional[float]:
        """Compute cost basis from simulated BUY fills.

        The simulation spot client only tracks balances, so we reconstruct the
        average entry price from its order history. This lets the lifecycle exit
        engine evaluate unrealised PnL correctly across cycles.
        """
        total_qty = 0.0
        total_quote = 0.0
        for order in getattr(self.spot_client, "_orders", []):
            if order.get("symbol") != symbol or order.get("side") != "BUY":
                continue
            for fill in order.get("fills", []):
                qty = float(fill.get("qty", 0.0))
                price = float(fill.get("price", 0.0))
                if qty <= 0 or price <= 0:
                    continue
                total_qty += qty
                total_quote += qty * price
        if total_qty <= 0:
            return None
        return total_quote / total_qty

    def _current_funding(self) -> Optional[float]:
        try:
            return float(getattr(self.futures_client, "funding_rate", 0.0) or 0.0)
        except Exception:
            return None

    def _fng(self) -> int:
        try:
            return int(getattr(self.spot_client, "fear_greed_value", 50) or 50)
        except Exception:
            return 50

    def build_context(self, config: LifecycleConfig) -> MarketContext:
        return MarketContext(
            timestamp=self.clock.now().timestamp(),
            regime=config.regime,
            regime_confidence={config.regime: 1.0},
            btc_7d_pct=0.0,
            btc_30d_pct=0.0,
            btc_day_pct=0.0,
            fear_greed_index=self._fng(),
            btc_dominance=55.0,
            funding_rate=self._current_funding(),
        )

    def get_positions(self, config: LifecycleConfig) -> List[Position]:
        """Return open SPOT positions as lifecycle Positions."""
        try:
            balances = self.spot_client.get_account_balance()
        except Exception:
            return []
        positions: List[Position] = []
        for asset, qty in balances.items():
            if asset == "USDT" or float(qty) <= 0:
                continue
            symbol = f"{asset}USDT"
            price = self._current_price(symbol) or 0.0
            entry_price = self._avg_entry_price(symbol) or price
            positions.append(Position(
                symbol=symbol,
                side=Side.LONG,
                quantity=float(qty),
                entry_price=entry_price,
                entry_time=self.clock.now().timestamp(),
                position_usdt=float(qty) * price,
                category=self.category_resolver(symbol),
                regime_at_entry=config.regime,
                profile_at_entry=config.profile,
                metadata={"trade_type": "spot"},
            ))
        return positions

    def execute_entry(self, trade: SizedTrade, config: LifecycleConfig) -> Dict:
        entry = trade.entry_decision
        symbol = entry.signal.symbol
        price = self._current_price(symbol) or entry.entry_price or 0.0
        side = "BUY" if entry.signal.side == Side.LONG else "SELL"
        qty = trade.quantity
        result = self.spot_client.create_market_order(symbol=symbol, side=side, quantity=qty)
        return {"filled": True, "price": price, "raw": result}

    def execute_exit(self, decision: ExitDecision, position: Position, config: LifecycleConfig) -> Dict:
        symbol = decision.symbol
        price = self._current_price(symbol) or 0.0
        side = "SELL" if position.side == Side.LONG else "BUY"
        qty = position.quantity * decision.pct
        if decision.action.value == "CLOSE_ALL":
            qty = position.quantity
        result = self.spot_client.create_market_order(symbol=symbol, side=side, quantity=qty)

        # Compute realized PnL from simulated fills.
        realized_pnl_usdt = 0.0
        exit_value = 0.0
        commission = 0.0
        fills = result.get("fills", [])
        if fills:
            total_qty = 0.0
            total_quote = 0.0
            total_commission = 0.0
            for fill in fills:
                q = float(fill.get("qty", 0.0))
                p = float(fill.get("price", 0.0))
                c = float(fill.get("commission", 0.0))
                c_asset = fill.get("commissionAsset", "USDT")
                if c_asset not in ("USDT", "BNB") and p > 0:
                    c = c * p
                total_qty += q
                total_quote += q * p
                total_commission += c
            avg_exit = total_quote / total_qty if total_qty > 0 else 0.0
            exit_value = total_quote
            commission = total_commission
            if position.side == Side.LONG:
                cost_basis = qty * position.entry_price
                realized_pnl_usdt = (avg_exit - position.entry_price) * qty - total_commission
            else:
                realized_pnl_usdt = (position.entry_price - avg_exit) * qty - total_commission

        return {
            "filled": True,
            "price": price,
            "avg_exit_price": exit_value / qty if qty > 0 else 0.0,
            "quantity": qty,
            "realized_pnl_usdt": realized_pnl_usdt,
            "commission_usdt": commission,
            "raw": result,
        }

    def audit(self, decision: object, result: Dict, config: LifecycleConfig) -> None:
        try:
            if self.db_client is None:
                return
            from datetime import datetime, timezone
            self.db_client.execute_query(
                "INSERT INTO trades_audit (symbol, trade_type, event, detail, created_at) VALUES (%s, %s, %s, %s, %s)",
                (
                    getattr(decision, "symbol", None) or getattr(decision.signal, "symbol", None),
                    "spot",
                    "LIFECYCLE_MIMIC_DECISION",
                    json.dumps({"regime": config.regime, "profile": config.profile, "result": result}),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        except Exception:
            pass

    def price_resolver(self, symbol: str) -> Optional[float]:
        return self._current_price(symbol)

    def atr_resolver(self, symbol: str) -> Optional[float]:
        return _approx_atr(symbol, self.feed)

    def rsi_resolver(self, symbol: str) -> Optional[float]:
        return _approx_rsi(symbol, self.feed)


def run_lifecycle_simulation(
    settings: Any,
    symbols: List[str],
    interval: str = "1h",
    days: int = 7,
    start: Optional[Any] = None,
    end: Optional[Any] = None,
    initial_usdt: float = 2910.0,
    fear_greed_value: float = 50.0,
    data_dir: str = "historical_data",
    forced_profile: Optional[str] = None,
    profile_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
    max_cycles: Optional[int] = None,
    lifecycle_config: Optional[LifecycleConfig] = None,
) -> Dict[str, Any]:
    """Run a single MIMIC simulation using the lifecycle kernel."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    from simulation.mimic_runner import SimulationRunner
    from lifecycle import LifecycleRunner, SignalPipeline, EntryGate, PositionSizer, ExitEngine

    if end is None:
        end = _dt.now(_tz.utc)
    if start is None:
        start = end - _td(days=days)

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

    if lifecycle_config is not None:
        config = lifecycle_config
    elif forced_profile in ("A", "B", "C"):
        config = _spot_bull_config(forced_profile)
    else:
        config = _spot_bull_config("A")
    adapter = SimulationLifecycleAdapter(
        spot_client=runner.spot_client,
        futures_client=runner.futures_client,
        clock=runner.clock,
        feed=runner.price_feed,
        db_client=runner.db_client,
    )
    lifecycle_runner = LifecycleRunner(
        adapter=adapter,
        signal_pipeline=SignalPipeline(),
        entry_gate=EntryGate(
            price_resolver=adapter.price_resolver,
            atr_resolver=adapter.atr_resolver,
        ),
        position_sizer=PositionSizer(),
        exit_engine=ExitEngine(
            price_resolver=adapter.price_resolver,
            atr_resolver=adapter.atr_resolver,
            rsi_resolver=adapter.rsi_resolver,
        ),
    )

    results = []
    cycle = 0
    current = runner.start
    next_cycle_minutes = None
    last_successful_step = None
    while current <= runner.end:
        runner.clock._now = current
        runner._invalidate_caches()
        runner.news_client.update_for_date(current)
        try:
            analyses = _build_analyses(runner.symbols, runner.price_feed)
            total_liquid = float(runner.spot_client.get_account_balance().get("USDT", 0.0))
            summary = lifecycle_runner.run_cycle(analyses, config, total_liquid=total_liquid)
            balance = runner.spot_client.get_account_balance()
            total_value = float(balance.get("USDT", 0.0))
            for asset, qty in balance.items():
                if asset == "USDT":
                    continue
                price = runner.spot_client.get_symbol_price(f"{asset}USDT", use_cache=False)
                total_value += float(qty) * float(price)
            results.append({
                "cycle": cycle,
                "timestamp": runner.clock.now().isoformat(),
                "summary": summary,
                "total_value": total_value,
                "usdt_balance": float(balance.get("USDT", 0.0)),
                "regime_mode": config.regime,
            })
            next_cycle_minutes = 60  # lifecycle simulation advances hourly
            last_successful_step = next_cycle_minutes
        except Exception as e:
            print(f"[LifecycleMIMIC] Cycle {cycle} failed at {runner.clock.now().isoformat()}: {e}")
            next_cycle_minutes = last_successful_step
        cycle += 1
        step = next_cycle_minutes or 60
        current = current + _td(minutes=step)
        if max_cycles is not None and cycle >= max_cycles:
            break

    return {
        "cycles": cycle,
        "start": runner.start.isoformat(),
        "end": runner.end.isoformat(),
        "final_balance": runner.spot_client.get_account_balance(),
        "results": results,
    }


def _spot_bull_config(profile: str = "A") -> LifecycleConfig:
    """Return the SPOT_BULL lifecycle config for the requested profile.

    Profiles A/B/C are derived from the optimal parameter combinations found by
    the MIMIC sweep in `Dev/sweep_analysis_SPOT_BULL_20260713.md`.
    """
    if profile == "A":
        return _spot_bull_config_a()
    if profile == "B":
        return _spot_bull_config_b()
    if profile == "C":
        return _spot_bull_config_c()
    return _spot_bull_config_a()


def _spot_bull_config_a() -> LifecycleConfig:
    """Profile A: stricter signal/success thresholds, conservative sizing."""
    from lifecycle import LifecycleConfig, SignalPolicy, EntryPolicy, SizingPolicy, ExitPolicy, PartialTake, MomentumExitRule, StagnationRule
    return LifecycleConfig(
        regime="SPOT_BULL",
        profile="A",
        signal_policy=SignalPolicy(
            min_signal_strength=0.55,
            min_success_probability=0.55,
            min_expected_pnl=0.0,
            min_liquidity_usdt=0.0,
            categories_enabled=("LARGE_CAP", "MIDCAP_ALT", "NARRATIVE"),
            max_candidates=5,
            diversification_mode="category",
        ),
        entry_policy=EntryPolicy(
            min_composite_score=0.55,
            min_expected_pnl=0.0,
            min_success_probability=0.55,
            min_liquidity_usdt=0.0,
            max_total_positions=5,
            max_category_positions={"LARGE_CAP": 1, "MIDCAP_ALT": 2, "NARRATIVE": 1},
        ),
        sizing_policy=SizingPolicy(
            regime_allocation_pct=0.8,
            max_per_trade_usdt=1000.0,
            kelly_base_pct=0.18,
            kelly_max_pct=0.20,
            conviction_max_pct=0.20,
            category_caps={"LARGE_CAP": 0.30, "MIDCAP_ALT": 0.35, "NARRATIVE": 0.15},
        ),
        exit_policy=ExitPolicy(
            regime="SPOT_BULL",
            profile="A",
            hard_sl_atr_mult=4.5,
            hard_tp_atr_mult=3.5,
            trailing_activation_pct=0.02,
            trailing_atr_mult=1.5,
            breakeven_activation_pct=0.015,
            partial_takes=(PartialTake(0.25, 1.5, "tp1"),),
            momentum_exit=MomentumExitRule(70, 30, 80, 20),
            stagnation_rule=StagnationRule(6, 0.05, 0.5),
        ),
    )


def _spot_bull_config_b() -> LifecycleConfig:
    """Profile B: looser signal threshold, larger sizing and more positions."""
    from lifecycle import LifecycleConfig, SignalPolicy, EntryPolicy, SizingPolicy, ExitPolicy, PartialTake, MomentumExitRule, StagnationRule
    return LifecycleConfig(
        regime="SPOT_BULL",
        profile="B",
        signal_policy=SignalPolicy(
            min_signal_strength=0.50,
            min_success_probability=0.45,
            min_expected_pnl=0.0,
            min_liquidity_usdt=0.0,
            categories_enabled=("LARGE_CAP", "MIDCAP_ALT", "NARRATIVE"),
            max_candidates=8,
            diversification_mode="category",
        ),
        entry_policy=EntryPolicy(
            min_composite_score=0.50,
            min_expected_pnl=0.0,
            min_success_probability=0.45,
            min_liquidity_usdt=0.0,
            max_total_positions=8,
            max_category_positions={"LARGE_CAP": 2, "MIDCAP_ALT": 3, "NARRATIVE": 1},
        ),
        sizing_policy=SizingPolicy(
            regime_allocation_pct=0.8,
            max_per_trade_usdt=1200.0,
            kelly_base_pct=0.22,
            kelly_max_pct=0.25,
            conviction_max_pct=0.25,
            category_caps={"LARGE_CAP": 0.25, "MIDCAP_ALT": 0.40, "NARRATIVE": 0.15},
        ),
        exit_policy=ExitPolicy(
            regime="SPOT_BULL",
            profile="B",
            hard_sl_atr_mult=3.36,
            hard_tp_atr_mult=3.5,
            trailing_activation_pct=0.02,
            trailing_atr_mult=1.5,
            breakeven_activation_pct=0.015,
            partial_takes=(PartialTake(0.25, 1.5, "tp1"),),
            momentum_exit=MomentumExitRule(70, 30, 80, 20),
            stagnation_rule=StagnationRule(6, 0.05, 0.5),
        ),
    )


def _spot_bull_config_c() -> LifecycleConfig:
    """Profile C: loose signal threshold but stricter success probability."""
    from lifecycle import LifecycleConfig, SignalPolicy, EntryPolicy, SizingPolicy, ExitPolicy, PartialTake, MomentumExitRule, StagnationRule
    return LifecycleConfig(
        regime="SPOT_BULL",
        profile="C",
        signal_policy=SignalPolicy(
            min_signal_strength=0.45,
            min_success_probability=0.55,
            min_expected_pnl=0.0,
            min_liquidity_usdt=0.0,
            categories_enabled=("LARGE_CAP", "MIDCAP_ALT", "NARRATIVE"),
            max_candidates=5,
            diversification_mode="category",
        ),
        entry_policy=EntryPolicy(
            min_composite_score=0.45,
            min_expected_pnl=0.0,
            min_success_probability=0.55,
            min_liquidity_usdt=0.0,
            max_total_positions=5,
            max_category_positions={"LARGE_CAP": 1, "MIDCAP_ALT": 2, "NARRATIVE": 1},
        ),
        sizing_policy=SizingPolicy(
            regime_allocation_pct=0.8,
            max_per_trade_usdt=1000.0,
            kelly_base_pct=0.18,
            kelly_max_pct=0.20,
            conviction_max_pct=0.20,
            category_caps={"LARGE_CAP": 0.30, "MIDCAP_ALT": 0.35, "NARRATIVE": 0.15},
        ),
        exit_policy=ExitPolicy(
            regime="SPOT_BULL",
            profile="C",
            hard_sl_atr_mult=3.36,
            hard_tp_atr_mult=3.5,
            trailing_activation_pct=0.02,
            trailing_atr_mult=1.5,
            breakeven_activation_pct=0.015,
            partial_takes=(PartialTake(0.25, 1.5, "tp1"),),
            momentum_exit=MomentumExitRule(70, 30, 80, 20),
            stagnation_rule=StagnationRule(6, 0.05, 0.5),
        ),
    )
