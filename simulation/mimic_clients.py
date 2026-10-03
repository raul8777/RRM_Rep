"""MIMIC — Deterministic mocks for production clients.

These classes implement the same public interfaces as `BinanceClient` and
`FuturesClient` but execute against an in-memory market replay.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
import uuid
from simulation.mimic_protocols import SimulationClock


def _get_commission_rates() -> tuple:
    """Return (spot_rate, futures_rate, slippage_pct) from binance_commission.py.

    Falls back to conservative defaults if the module or settings are unavailable.
    """
    try:
        import binance_commission as _bc
        cfg = _bc.get_config()
        return cfg.rate_for('spot'), cfg.rate_for('futures'), cfg.slippage_pct
    except Exception:
        return 0.001, 0.0004, 0.0005


def _load_funding_rate_series(data_dir: str = "historical_data") -> Dict[str, Dict[str, float]]:
    """Load historical funding rates from parquet into {symbol: {ISO-timestamp: rate}} dict.

    Parquet files are expected at data_dir/external/funding_rate_*.parquet with
    columns: symbol, fundingTime (ms epoch), fundingRate.
    Returns an empty dict if no data is available.
    """
    import os
    ext_dir = os.path.join(data_dir, "external")
    result: Dict[str, Dict[str, float]] = {}
    if not os.path.isdir(ext_dir):
        return result
    for fname in os.listdir(ext_dir):
        if not (fname.startswith("funding_rate") and fname.endswith(".parquet")):
            continue
        try:
            import pandas as pd
            df = pd.read_parquet(os.path.join(ext_dir, fname))
            for _, row in df.iterrows():
                sym = str(row.get("symbol", ""))
                ts_raw = row.get("fundingTime", row.get("funding_time", 0))
                rate = float(row.get("fundingRate", row.get("funding_rate", 0.0)))
                # Convert ms epoch to ISO date-hour key (funding settles every 8h)
                from datetime import timezone as _tz
                if isinstance(ts_raw, datetime):
                    dt = ts_raw.astimezone(_tz.utc)
                elif hasattr(ts_raw, "to_pydatetime"):
                    dt = ts_raw.to_pydatetime().astimezone(_tz.utc)
                else:
                    dt = datetime.fromtimestamp(int(ts_raw) / 1000.0, tz=_tz.utc)
                key = dt.strftime("%Y-%m-%dT%H")
                if sym not in result:
                    result[sym] = {}
                result[sym][key] = rate
        except Exception:
            pass
    return result


class SimulationBinanceClient:
    """Mock spot client backed by a historical price feed and virtual balance."""

    PRICE_CACHE_TTL = 30
    FILTERS_CACHE_TTL = 3600

    class _Client:
        """Internal client shim so autonomous_signals.fetch_klines works."""

        def __init__(self, owner: "SimulationBinanceClient"):
            self._owner = owner

        def get_ticker(self, symbol: str) -> Dict[str, Any]:
            """Return a 24h ticker-like dict from the current bar."""
            try:
                price = self._owner._current_price(symbol)
                bars = self._owner._price_feed.get(symbol, [])
                now = self._owner.clock.now().timestamp()
                # Sum quote_volume of last 24h-ish of bars up to current clock
                last_24h = [b for b in bars if b.get("open_time", b.get("timestamp", 0)) <= now]
                last_24h = last_24h[-24:] if len(last_24h) >= 24 else last_24h
                quote_volume = sum(float(b.get("quote_volume", 0)) for b in last_24h)
                # Price change from first to last of the window
                price_change_pct = 0.0
                if last_24h:
                    first = float(last_24h[0].get("open", 0))
                    last = float(last_24h[-1].get("close", price))
                    if first > 0:
                        price_change_pct = (last - first) / first * 100
                return {
                    "symbol": symbol,
                    "lastPrice": str(price),
                    "priceChangePercent": str(price_change_pct),
                    "quoteVolume": str(quote_volume),
                }
            except Exception:
                return {
                    "symbol": symbol,
                    "lastPrice": "0.0",
                    "priceChangePercent": "0.0",
                    "quoteVolume": "0.0",
                }

        def get_symbol_info(self, symbol: str) -> Dict[str, Any]:
            return {
                "symbol": symbol,
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                    {"filterType": "NOTIONAL", "minNotional": "10.0"},
                ],
            }

        def universal_transfer(self, **kwargs: Any) -> Dict[str, Any]:
            """No-op transfer stub for BNB commission simulation."""
            return {"tranId": 0}

        def get_klines(
            self, symbol: str, interval: str = "1h", limit: int = 100,
            startTime: Optional[int] = None, endTime: Optional[int] = None,
        ) -> List[List[Any]]:
            bars = self._owner._price_feed.get(symbol, [])
            if interval == "4h":
                bars = self._resample_4h(bars)
            elif interval == "1d":
                bars = self._resample_1d(bars)
            elif interval in ("5m", "15m"):
                # D4: Try feed_obj.get_klines_resampled for sub-hourly data
                feed_obj = getattr(self._owner, '_feed_obj', None)
                if feed_obj is not None and hasattr(feed_obj, 'get_klines_resampled'):
                    bars = feed_obj.get_klines_resampled(symbol, interval)
                else:
                    mins = 5 if interval == "5m" else 15
                    bars = self._resample_subhour(bars, mins)
            # Convert bars to raw Binance kline format [open_time, open, high, low, close, volume, close_time, ...]
            raw = []
            for b in bars:
                ot = int(b.get("open_time", b.get("timestamp", 0)) * 1000)
                if startTime and ot < startTime:
                    continue
                if endTime and ot > endTime:
                    continue
                # close time depends on interval
                if interval == "4h":
                    ct = ot + 14_400_000
                elif interval == "1d":
                    ct = ot + 86_400_000
                elif interval == "5m":
                    ct = ot + 300_000
                elif interval == "15m":
                    ct = ot + 900_000
                else:
                    ct = ot + 3_600_000
                raw.append([
                    ot,
                    str(b.get("open", 0)),
                    str(b.get("high", 0)),
                    str(b.get("low", 0)),
                    str(b.get("close", 0)),
                    str(b.get("volume", 0)),
                    ct,
                    str(b.get("quote_volume", 0)),
                    int(b.get("trades", 0)),
                    str(b.get("taker_buy_base", 0)),
                    str(b.get("taker_buy_quote", 0)),
                    "0",
                ])
            # Return the last `limit` bars up to the simulated clock
            now_ms = int(self._owner.clock.now().timestamp() * 1000)
            filtered = [r for r in raw if r[0] <= now_ms]
            if len(filtered) > limit:
                filtered = filtered[-limit:]
            return filtered

        def _resample_subhour(
            self,
            bars: List[Dict[str, Any]],
            target_minutes: int,
        ) -> List[Dict[str, Any]]:
            """D4: Inline sub-hourly resample when no feed_obj is available."""
            n_slots = 60 // target_minutes
            slot_seconds = target_minutes * 60
            result = []
            for bar in sorted(bars, key=lambda x: x.get("open_time", x.get("timestamp", 0))):
                ot = float(bar.get("open_time", bar.get("timestamp", 0)))
                o = float(bar.get("open", 0))
                h = float(bar.get("high", 0))
                lo = float(bar.get("low", 0))
                c = float(bar.get("close", 0))
                vol = float(bar.get("volume", 0))
                for i in range(n_slots):
                    frac_s = i / n_slots
                    frac_e = (i + 1) / n_slots
                    sub_o = o + (c - o) * frac_s
                    sub_c = o + (c - o) * frac_e
                    result.append({
                        "open_time": ot + i * slot_seconds,
                        "open": sub_o, "high": max(sub_o, sub_c, lo + (h - lo) * frac_e),
                        "low": min(sub_o, sub_c, h - (h - lo) * (1 - frac_s)),
                        "close": sub_c,
                        "volume": vol / n_slots,
                        "quote_volume": float(bar.get("quote_volume", 0)) / n_slots,
                        "trades": int(bar.get("trades", 0)) // n_slots,
                    })
            return result

        def _resample_4h(self, bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            """Resample 1h bars into 4h bars."""
            if not bars:
                return []
            sorted_bars = sorted(bars, key=lambda x: x.get("open_time", x.get("timestamp", 0)))
            result = []
            bucket = []
            for b in sorted_bars:
                ot = b.get("open_time", b.get("timestamp", 0))
                bucket.append(b)
                if len(bucket) == 4:
                    result.append(self._aggregate_bucket(bucket, ot))
                    bucket = []
            if bucket:
                result.append(self._aggregate_bucket(bucket, bucket[-1].get("open_time", bucket[-1].get("timestamp", 0))))
            return result

        def _resample_1d(self, bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            if not bars:
                return []
            sorted_bars = sorted(bars, key=lambda x: x.get("open_time", x.get("timestamp", 0)))
            result = []
            bucket = []
            for b in sorted_bars:
                bucket.append(b)
                if len(bucket) == 24:
                    result.append(self._aggregate_bucket(bucket, b.get("open_time", b.get("timestamp", 0))))
                    bucket = []
            if bucket:
                result.append(self._aggregate_bucket(bucket, bucket[-1].get("open_time", bucket[-1].get("timestamp", 0))))
            return result

        def _aggregate_bucket(self, bucket: List[Dict[str, Any]], open_time: float) -> Dict[str, Any]:
            opens = [float(b.get("open", 0)) for b in bucket]
            highs = [float(b.get("high", 0)) for b in bucket]
            lows = [float(b.get("low", 0)) for b in bucket]
            closes = [float(b.get("close", 0)) for b in bucket]
            volumes = [float(b.get("volume", 0)) for b in bucket]
            quote_volumes = [float(b.get("quote_volume", 0)) for b in bucket]
            trades = [int(b.get("trades", 0)) for b in bucket]
            taker_buy_bases = [float(b.get("taker_buy_base", 0)) for b in bucket]
            taker_buy_quotes = [float(b.get("taker_buy_quote", 0)) for b in bucket]
            return {
                "open_time": open_time,
                "open": opens[0],
                "high": max(highs),
                "low": min(lows),
                "close": closes[-1],
                "volume": sum(volumes),
                "quote_volume": sum(quote_volumes),
                "trades": sum(trades),
                "taker_buy_base": sum(taker_buy_bases),
                "taker_buy_quote": sum(taker_buy_quotes),
            }

    def __init__(
        self,
        clock: SimulationClock,
        initial_balances: Optional[Dict[str, float]] = None,
        commission_rate: Optional[float] = None,
        slippage_pct: Optional[float] = None,
    ):
        self.clock = clock
        self.is_simulation = True
        self.balances: Dict[str, float] = dict(initial_balances or {"USDT": 2910.0})
        _spot_rate, _fut_rate, _slip = _get_commission_rates()
        self.commission_rate = commission_rate if commission_rate is not None else _spot_rate
        self.slippage_pct = slippage_pct if slippage_pct is not None else _slip
        self._price_feed: Dict[str, List[Dict[str, Any]]] = {}  # symbol -> [{ts, open, high, low, close, volume}]
        self._orders: List[Dict[str, Any]] = []
        self._price_cache: Dict[str, tuple] = {}
        self._filters_cache: Dict[str, tuple] = {}
        self._order_id = 0
        self.client = SimulationBinanceClient._Client(self)

    def set_price_feed(self, feed: Dict[str, List[Dict[str, Any]]]) -> None:
        self._price_feed = feed

    def _current_bar(self, symbol: str) -> Optional[Dict[str, Any]]:
        bars = self._price_feed.get(symbol, [])
        if not bars:
            return None
        now = self.clock.now().timestamp()
        # Last bar whose open time <= now
        bar = None
        for b in bars:
            if b.get("open_time", b.get("timestamp", 0)) <= now:
                bar = b
        return bar

    def _current_price(self, symbol: str) -> float:
        bar = self._current_bar(symbol)
        if bar is None:
            raise RuntimeError(f"No price data for {symbol} at {self.clock.now().isoformat()}")
        return float(bar.get("close", 0))

    def get_account_info(self) -> Dict[str, Any]:
        return {
            "balances": [
                {"asset": asset, "free": f"{qty:.8f}", "locked": "0.00000000"}
                for asset, qty in self.balances.items()
            ],
            "commission": self.commission_rate,
        }

    def get_account_balance(self) -> Dict[str, float]:
        return {asset: float(qty) for asset, qty in self.balances.items() if float(qty) > 0}

    def get_commission_rate(self) -> float:
        return self.commission_rate

    def _format_amount(self, amount: float) -> str:
        value = Decimal(str(amount))
        normalized = value.normalize()
        return format(normalized, "f")

    def create_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        quote_order_qty: Optional[float] = None,
    ) -> Dict[str, Any]:
        self._order_id += 1
        price = self._current_price(symbol)
        if self.slippage_pct:
            # D3: Volume-adjusted slippage — larger orders relative to bar volume get worse fills.
            bar = self._current_bar(symbol)
            bar_vol = float(bar.get("quote_volume", 0)) if bar else 0.0
            order_notional = (quote_order_qty or (quantity * price))
            vol_impact = 0.0
            if bar_vol > 0:
                # Linear market impact: 1 bp per 0.5% of bar volume
                vol_ratio = min(order_notional / bar_vol, 0.10)  # cap at 10%
                vol_impact = vol_ratio * 0.002  # 0.2% per 1% of bar volume
            effective_slip = self.slippage_pct + vol_impact
            sign = -1 if side.upper() == "BUY" else 1
            price = price * (1 + sign * effective_slip)

        asset = symbol.replace("USDT", "")
        if side.upper() == "BUY":
            if quote_order_qty:
                qty = quote_order_qty / price
                cost = quote_order_qty
            else:
                qty = quantity
                cost = qty * price
            commission = cost * self.commission_rate
            self.balances["USDT"] = self.balances.get("USDT", 0) - cost - commission
            self.balances[asset] = self.balances.get(asset, 0) + qty
            fills_qty = qty
            fills_quote = cost
        else:  # SELL
            if quote_order_qty:
                raise ValueError("SELL with quote_order_qty not supported in simulation")
            qty = quantity
            proceeds = qty * price
            commission = proceeds * self.commission_rate
            self.balances[asset] = self.balances.get(asset, 0) - qty
            self.balances["USDT"] = self.balances.get("USDT", 0) + proceeds - commission
            fills_qty = qty
            fills_quote = proceeds

        order = {
            "orderId": self._order_id,
            "symbol": symbol,
            "side": side.upper(),
            "type": "MARKET",
            "status": "FILLED",
            "quantity": self._format_amount(qty),
            "quoteOrderQty": self._format_amount(quote_order_qty) if quote_order_qty else None,
            "fills": [
                {
                    "price": self._format_amount(price),
                    "qty": self._format_amount(fills_qty),
                    "commission": self._format_amount(commission),
                    "commissionAsset": "USDT" if side.upper() == "BUY" else asset,
                }
            ],
            "simulated": True,
        }
        self._orders.append(order)
        return order

    def get_order_status(self, symbol: str, order_id: int) -> Dict[str, Any]:
        for o in self._orders:
            if o.get("orderId") == order_id and o.get("symbol") == symbol:
                return o
        return {"status": "UNKNOWN"}

    def verify_order_filled(self, order_result: dict, label: str = "") -> bool:
        if not order_result:
            return False
        return order_result.get("status") == "FILLED"

    def verify_and_fetch_fills(self, order_result: dict, label: str = "") -> Optional[Dict[str, Any]]:
        """Simulation equivalent of BinanceClient.verify_and_fetch_fills.

        Aggregates fill data from the already-executed simulated order so that
        production ``run_trades.py`` can persist entry data without modification.
        """
        if not self.verify_order_filled(order_result, label=label):
            return None
        fills = order_result.get("fills") or []
        if not fills:
            return None
        from pnl_accounting import normalize_spot_fills
        payload = normalize_spot_fills(fills, order_result.get("symbol", ""))
        payload.update({
            "order_id": order_result.get("orderId"),
            "commission": payload["commission_usdt"],
            "binance_confirmed": True,
        })
        return payload

    def get_symbol_price(self, symbol: str, use_cache: bool = True) -> float:
        now = self.clock.now().timestamp()
        if use_cache and symbol in self._price_cache:
            price, ts = self._price_cache[symbol]
            if now - ts < self.PRICE_CACHE_TTL:
                return price
        price = self._current_price(symbol)
        self._price_cache[symbol] = (price, now)
        return price

    def get_symbol_avg_price(self, symbol: str, use_cache: bool = True) -> float:
        """Return a 5-bar VWAP as the simulated average price for the symbol."""
        cache_key = f"{symbol}:avg"
        now = self.clock.now().timestamp()
        if use_cache and cache_key in self._price_cache:
            price, ts = self._price_cache[cache_key]
            if now - ts < self.PRICE_CACHE_TTL:
                return price
        bars = self._price_feed.get(symbol, [])
        if not bars:
            raise RuntimeError(f"No price data for {symbol} at {self.clock.now().isoformat()}")
        recent = [b for b in bars if b.get("open_time", b.get("timestamp", 0)) <= now][-5:]
        if not recent:
            recent = bars[-5:]
        total_vol = sum(float(b.get("volume", 0)) for b in recent)
        if total_vol > 0:
            price = sum(float(b.get("close", 0)) * float(b.get("volume", 0)) for b in recent) / total_vol
        else:
            price = float(recent[-1].get("close", 0))
        self._price_cache[cache_key] = (price, now)
        return price

    def get_symbol_filters(self, symbol: str) -> Dict[str, float]:
        """Return symbol filters with 1-hour cache."""
        now = self.clock.now().timestamp()
        if symbol in self._filters_cache:
            val, ts = self._filters_cache[symbol]
            if now - ts < self.FILTERS_CACHE_TTL:
                return val
        info = self.client.get_symbol_info(symbol)
        filters_raw = info.get("filters", []) if info else []
        result = {"step": 0.001, "min_qty": 0.001, "min_notional": 10.0}
        for f in filters_raw:
            ft = f.get("filterType", "")
            if ft == "LOT_SIZE":
                result["step"] = float(f.get("stepSize", 0.001))
                result["min_qty"] = float(f.get("minQty", 0.001))
            elif ft in ("NOTIONAL", "MIN_NOTIONAL"):
                result["min_notional"] = float(f.get("minNotional", 10.0))
        self._filters_cache[symbol] = (result, now)
        return result

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        return self.client.get_ticker(symbol)

    def get_client(self):
        """Return the internal client shim used by legacy code."""
        return self.client


@dataclass
class SimulationLeveragedPosition:
    symbol: str
    side: str
    qty: float
    entry_price: float
    sl_price: float
    tp_price: float
    order_id: str
    leverage: int = 2
    margin_usdt: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    closed: bool = False
    opened_at: float = 0.0
    peak_pnl_pct: float = 0.0
    position_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    thesis_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    close_group_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def quantity(self) -> float:
        return self.qty

    @property
    def stop_price(self) -> float:
        return self.sl_price

    @property
    def target_price(self) -> float:
        return self.tp_price


class SimulationFuturesClient:
    """Mock futures client backed by the same historical price feed."""

    # Funding is charged/credited every 8h; we apply it when the clock crosses
    # a settlement hour (00:00, 08:00, 16:00 UTC).
    _FUNDING_SETTLEMENT_HOURS = {0, 8, 16}

    def __init__(
        self,
        clock: SimulationClock,
        price_feed: Dict[str, List[Dict[str, Any]]],
        commission_rate: Optional[float] = None,
        leverage: int = 2,
        data_dir: str = "historical_data",
    ):
        _spot_rate, _fut_rate, _slip = _get_commission_rates()
        commission_rate = commission_rate if commission_rate is not None else _fut_rate
        self.clock = clock
        self.dry_run = True
        self.is_simulation = True
        self._price_feed = price_feed
        self.commission_rate = commission_rate
        self.leverage = leverage
        self._open_positions: Dict[str, Any] = {}
        self._available_usdt = 0.0
        self._hedge_mode = False
        self.db_client = None
        self._cached_settings = None
        self._active_leverage = leverage
        self.max_shorts = 5
        self.max_longs = 2
        self.max_largecap_shorts = 3
        self.largecap_symbols = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT"}
        self._binance_client_ref = None
        # D1: Historical funding rate series {symbol: {YYYY-MM-DDTHH: rate}}
        self._funding_series: Dict[str, Dict[str, float]] = _load_funding_rate_series(data_dir)
        self._last_funding_hour: Optional[int] = None  # tracks last settlement hour applied
        self._total_funding_paid: float = 0.0

    def _current_bar(self, symbol: str) -> Dict[str, Any]:
        bars = self._price_feed.get(symbol, [])
        if not bars:
            raise RuntimeError(f"No futures price data for {symbol}")
        now = self.clock.now().timestamp()
        bar = None
        for candidate in bars:
            if candidate.get("open_time", candidate.get("timestamp", 0)) <= now:
                bar = candidate
        if bar is None:
            raise RuntimeError(f"No futures price data for {symbol} at {self.clock.now().isoformat()}")
        return bar

    def _current_price(self, symbol: str) -> float:
        return float(self._current_bar(symbol).get("close", 0))

    def apply_funding_if_due(self) -> float:
        """D1: Debit/credit funding from open positions when clock crosses a settlement hour.

        Returns total funding PnL applied this call (negative = paid, positive = received).
        Binance shorts: if fundingRate > 0, short-holder RECEIVES funding.
        """
        now = self.clock.now()
        current_hour = now.hour
        if current_hour not in self._FUNDING_SETTLEMENT_HOURS:
            return 0.0
        if self._last_funding_hour == current_hour and now.date() == getattr(self, '_last_funding_date', None):
            return 0.0  # already applied this settlement window
        self._last_funding_hour = current_hour
        self._last_funding_date = now.date()

        hour_key = now.strftime("%Y-%m-%dT%H")
        total_funding = 0.0
        for key, pos in self._open_positions.items():
            if pos.closed:
                continue
            sym = pos.symbol
            rate = self._funding_series.get(sym, {}).get(hour_key, None)
            if rate is None:
                # Fallback: use a small positive rate typical for crypto (0.01%)
                rate = 0.0001
            price = self._current_price(sym)
            notional = abs(float(getattr(pos, 'quantity', 0))) * price
            if pos.side == "SHORT":
                # Short receives when rate > 0, pays when rate < 0
                funding_pnl = notional * rate
            else:
                # Long pays when rate > 0, receives when rate < 0
                funding_pnl = -notional * rate
            self._available_usdt += funding_pnl
            total_funding += funding_pnl
        self._total_funding_paid += total_funding
        return total_funding

    def enable_hedge_mode(self) -> None:
        self._hedge_mode = True

    def get_price(self, symbol: str) -> float:
        return self._current_price(symbol)

    def get_available_usdt(self) -> float:
        return self._available_usdt

    def _open_position(
        self,
        symbol: str,
        side: str,
        usdt_amount: float,
        sl_pct: float,
        tp_pct: float,
        leverage: int,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Optional[SimulationLeveragedPosition]:
        leverage = max(1, min(int(leverage or self.leverage), 2))
        margin = min(max(0.0, float(usdt_amount)), self._available_usdt)
        if margin < 10.0:
            return None
        price = self._current_price(symbol)
        notional = margin * leverage
        qty = notional / price
        is_short = side == "SHORT"
        sl_price = price * (1 + sl_pct / 100.0 if is_short else 1 - sl_pct / 100.0)
        tp_price = price * (1 - tp_pct / 100.0 if is_short else 1 + tp_pct / 100.0)
        position = SimulationLeveragedPosition(
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=price,
            sl_price=sl_price,
            tp_price=tp_price,
            order_id=f"MIMIC_{uuid.uuid4().hex[:12]}",
            leverage=leverage,
            margin_usdt=margin,
            metadata={**(metadata or {}), **kwargs},
            opened_at=self.clock.now().timestamp(),
        )
        self._available_usdt -= margin
        self._open_positions[f"{symbol}_{side}"] = position
        return position

    def open_short(
        self,
        symbol: str,
        usdt_amount: float,
        sl_pct: float = 2.0,
        tp_pct: float = 4.0,
        leverage: int = 2,
        stop_loss_pct: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Optional[SimulationLeveragedPosition]:
        return self._open_position(
            symbol, "SHORT", usdt_amount,
            float(stop_loss_pct if stop_loss_pct is not None else sl_pct),
            float(take_profit_pct if take_profit_pct is not None else tp_pct),
            leverage, metadata, **kwargs,
        )

    def open_long(
        self,
        symbol: str,
        usdt_amount: float,
        sl_pct: float = 2.0,
        tp_pct: float = 4.0,
        leverage: int = 2,
        stop_loss_pct: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Optional[SimulationLeveragedPosition]:
        return self._open_position(
            symbol, "LONG", usdt_amount,
            float(stop_loss_pct if stop_loss_pct is not None else sl_pct),
            float(take_profit_pct if take_profit_pct is not None else tp_pct),
            leverage, metadata, **kwargs,
        )

    def get_total_wallet_usdt(self) -> float:
        """Return total USDT-equivalent futures wallet value (simulation-only)."""
        total = self._available_usdt
        for pos in self._open_positions.values():
            if pos.closed:
                continue
            try:
                price = self._current_price(pos.symbol)
                unrealized = pos.qty * (
                    pos.entry_price - price if pos.side == "SHORT" else price - pos.entry_price
                )
                total += pos.margin_usdt + unrealized
            except Exception:
                pass
        return total

    def get_account_info(self) -> Dict[str, Any]:
        """Return a minimal futures account snapshot for BNB commission simulation."""
        return {
            "assets": [
                {"asset": "USDT", "availableBalance": str(self._available_usdt), "walletBalance": str(self._available_usdt)},
            ],
        }

    def check_positions(self, settings: Any, monitor_only: bool = False) -> List[Dict[str, Any]]:
        from simulation.entry_outcome_resolver import resolve_bar_exit

        # D1: Apply funding before evaluating SL/TP
        self.apply_funding_if_due()
        # Minimal simulation: close positions that hit SL or TP
        closes = []
        for key, pos in list(self._open_positions.items()):
            if pos.closed:
                continue
            bar = self._current_bar(pos.symbol)
            exit_result = resolve_bar_exit(pos.side, bar, pos.sl_price, pos.tp_price)
            if exit_result is not None:
                reason = exit_result.reason
                exit_price = float(exit_result.exit_price)
                pnl = pos.qty * (
                    pos.entry_price - exit_price if pos.side == "SHORT" else exit_price - pos.entry_price
                )
                fees = (pos.entry_price + exit_price) * pos.qty * self.commission_rate
                net = pnl - fees
                self._available_usdt += pos.margin_usdt + net
                closes.append({
                    "symbol": pos.symbol,
                    "reason": reason,
                    "side": pos.side,
                    "exit_price": exit_price,
                    "pnl_usdt": net,
                    "realized_gross_pnl": pnl,
                    "fees_usdt": fees,
                    "ambiguous_intrabar": exit_result.ambiguous_intrabar,
                    "gap_through": exit_result.gap_through,
                    "position_id": pos.position_id,
                    "close_group_id": pos.close_group_id,
                })
                pos.closed = True
        return closes

    def close_position(
        self,
        position: SimulationLeveragedPosition,
        reason: str,
        exit_price: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        if position.closed:
            return None
        price = float(exit_price if exit_price is not None else self._current_price(position.symbol))
        gross = position.qty * (
            position.entry_price - price if position.side == "SHORT" else price - position.entry_price
        )
        fees = (position.entry_price + price) * position.qty * self.commission_rate
        net = gross - fees
        self._available_usdt += position.margin_usdt + net
        position.closed = True
        return {
            "symbol": position.symbol,
            "reason": reason,
            "side": position.side,
            "exit_price": price,
            "pnl_usdt": net,
            "realized_gross_pnl": gross,
            "fees_usdt": fees,
            "position_id": position.position_id,
            "close_group_id": position.close_group_id,
        }

    def liquidate_all(self, reason: str = "window_end_liquidation") -> List[Dict[str, Any]]:
        closes = []
        for position in self._open_positions.values():
            if position.closed:
                continue
            price = self._current_price(position.symbol)
            gross = position.qty * (
                position.entry_price - price if position.side == "SHORT" else price - position.entry_price
            )
            fees = (position.entry_price + price) * position.qty * self.commission_rate
            net = gross - fees
            self._available_usdt += position.margin_usdt + net
            position.closed = True
            closes.append({
                "symbol": position.symbol,
                "reason": reason,
                "side": position.side,
                "exit_price": price,
                "pnl_usdt": net,
                "realized_gross_pnl": gross,
                "fees_usdt": fees,
                "position_id": position.position_id,
                "close_group_id": position.close_group_id,
            })
        return closes

    def reevaluate_open_positions(
        self, settings: Any, regime: str, profile_label: str,
        switch_id: Optional[str] = None, transition_policy: Optional[Dict] = None,
    ) -> int:
        return 0

    def smart_rebalance_capital(
        self,
        spot_client: Optional[Any] = None,
        target_futures_ratio: float = 0.30,
        min_transfer: float = 50.0,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """D12: Simulate spot\u2194futures capital bridge.

        Transfers virtual USDT between spot (spot_client.balances) and the
        futures available balance so the simulation reflects the same
        allocation logic that production uses.
        """
        if spot_client is None:
            return {"direction": "none", "amount": 0.0, "reason": "no spot_client provided", "success": True, "error": None}
        spot_usdt = float(spot_client.balances.get("USDT", 0.0))
        futures_usdt = self._available_usdt
        total = spot_usdt + futures_usdt
        if total <= 0:
            return {"direction": "none", "amount": 0.0, "reason": "zero capital", "success": True, "error": None}
        target_futures = total * target_futures_ratio
        delta = target_futures - futures_usdt
        if abs(delta) < min_transfer:
            return {"direction": "none", "amount": 0.0, "reason": f"delta {delta:.2f} below min {min_transfer}", "success": True, "error": None}
        if delta > 0:
            # Transfer spot \u2192 futures
            transfer = min(delta, spot_usdt)
            spot_client.balances["USDT"] = spot_usdt - transfer
            self._available_usdt += transfer
            return {"direction": "spot_to_futures", "amount": round(transfer, 2), "reason": "rebalance", "success": True, "error": None}
        else:
            # Transfer futures \u2192 spot
            transfer = min(abs(delta), futures_usdt)
            self._available_usdt -= transfer
            spot_client.balances["USDT"] = spot_usdt + transfer
            return {"direction": "futures_to_spot", "amount": round(transfer, 2), "reason": "rebalance", "success": True, "error": None}

    def get_open_positions_summary(self) -> List[Dict[str, Any]]:
        """Stub for dashboard compatibility."""
        return []


class SimulationSpotMarginClient(SimulationFuturesClient):
    def __init__(
        self,
        clock: SimulationClock,
        price_feed: Dict[str, List[Dict[str, Any]]],
        initial_usdt: float,
        commission_rate: Optional[float] = None,
        data_dir: str = "historical_data",
    ):
        super().__init__(
            clock=clock,
            price_feed=price_feed,
            commission_rate=commission_rate,
            leverage=2,
            data_dir=data_dir,
        )
        self._available_usdt = float(initial_usdt)
        self._initial_usdt = float(initial_usdt)
        self.is_simulation = True

    def get_equity_delta_usdt(self) -> float:
        return self.get_total_wallet_usdt() - self._initial_usdt
