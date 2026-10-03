"""
spot_margin_sim_client.py — Spot-margin simulation client for staging.

Reuses the futures execution paths in run_trades.py but translates every order
into spot-market operations:

- LONG: market BUY + OCO SELL (real TP/SL on Binance spot).
- SHORT: synthetic short — no entry sell, instead a real OCO BUY is placed at
  SL/TP levels. When one leg fills, the synthetic short is closed and the
  acquired asset is immediately sold back to USDT so the wallet stays in USDT.

Capital is controlled by `spot_margin_capital_usdt` (default 5,000) and the
simulated leverage is recorded only for reporting; actual cash at risk equals
the notional position size (same as a 2x futures position on 2,500 margin).
"""

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from binance_client import BinanceClient


@dataclass
class SpotMarginPosition:
    symbol: str
    side: str  # "LONG" or "SHORT"
    qty: float
    entry_price: float
    sl_price: float
    tp_price: float
    order_id: str
    oco_order_list_id: Optional[str] = None
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed: bool = False
    closed_at: Optional[datetime] = None
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    _smart_meta: Dict[str, Any] = field(default_factory=dict)

    def unrealized_pnl(self, current_price: float) -> float:
        if self.side == "LONG":
            return (current_price - self.entry_price) * self.qty
        return (self.entry_price - current_price) * self.qty


class SpotMarginClient:
    """Drop-in replacement for FuturesClient when spot_margin_simulation_enabled."""

    def __init__(self, api_key: str, api_secret: str, dry_run: bool = False, settings=None, db_client=None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.dry_run = dry_run
        self.settings = settings
        self.db_client = db_client
        self._spot_client: Optional[BinanceClient] = None
        self._open_positions: Dict[str, SpotMarginPosition] = {}
        self._capital_usdt: float = float(getattr(settings, "spot_margin_capital_usdt", 5000.0) if settings else 5000.0)
        self._leverage: float = float(getattr(settings, "spot_margin_simulated_leverage", 2.0) if settings else 2.0)
        self._active_leverage: int = int(self._leverage)
        self._cached_settings = settings
        self.max_shorts: int = int(getattr(settings, "futures_max_shorts", 4) if settings else 4)
        self.max_longs: int = int(getattr(settings, "futures_max_longs", 3) if settings else 3)
        self.max_largecap_shorts: int = int(getattr(settings, "futures_max_largecap_shorts", 3) if settings else 3)
        self.largecap_symbols: set = set(getattr(settings, "futures_largecap_symbols", ['BTCUSDT','ETHUSDT','BNBUSDT','XRPUSDT','SOLUSDT']))
        self._init_spot_client()

    # ── Public API expected by run_trades.py ─────────────────────────────────

    def get_available_usdt(self) -> float:
        """Return simulated buying power minus reserved notional of open positions."""
        reserved = sum(p.qty * p.entry_price for p in self._open_positions.values() if not p.closed)
        return max(0.0, self._capital_usdt - reserved)

    def _total_open_notional(self) -> float:
        """Sum of notional size of all open simulated positions."""
        return sum(p.qty * p.entry_price for p in self._open_positions.values() if not p.closed)

    def open_long(
        self,
        symbol: str,
        usdt_amount: float,
        sl_pct: float = 2.0,
        tp_pct: float = 4.0,
        leverage: int = 2,
        **kwargs,
    ) -> Optional[SpotMarginPosition]:
        leverage = int(leverage or self._leverage)
        price = self._get_price(symbol)
        if price <= 0:
            print(f"[SpotMargin/LONG] {symbol}: cannot get price")
            return None
        notional = round(usdt_amount * leverage, 6)
        # Enforce simulated capital cap: total open notional cannot exceed capital.
        current_notional = self._total_open_notional()
        if current_notional + notional > self._capital_usdt:
            notional = max(0.0, self._capital_usdt - current_notional)
            if notional < 10.0:
                print(f"[SpotMargin/LONG] {symbol}: blocked, total notional would exceed capital ${self._capital_usdt:.2f}")
                return None
            print(f"[SpotMargin/LONG] {symbol}: capped notional to ${notional:.2f} to stay within capital")
        qty = self._round_qty(symbol, notional / price)
        notional = round(qty * price, 6)
        if notional < 10.0:
            print(f"[SpotMargin/LONG] {symbol}: notional ${notional:.2f} < $10")
            return None

        entry_price = price
        sl_price = self._align_price(symbol, entry_price * (1 - sl_pct / 100))
        tp_price = self._align_price(symbol, entry_price * (1 + tp_pct / 100))

        order_id = None
        entry_order_id = None
        if not self.dry_run:
            try:
                buy = self._spot_client.create_market_order(symbol, "BUY", quantity=None, quote_order_qty=notional)
                if not self._verify_filled(buy):
                    print(f"[SpotMargin/LONG] {symbol}: buy order not filled")
                    return None
                entry_price = self._avg_fill_price(buy)
                base_asset = symbol[:-4] if symbol.endswith("USDT") else symbol
                commission_base = sum(
                    float(f.get("commission", 0)) for f in buy.get("fills", [])
                    if f.get("commissionAsset") == base_asset
                )
                qty = self._round_qty(symbol, float(buy.get("executedQty", qty)) - commission_base)
                entry_order_id = str(buy.get("orderId", ""))
                oco = self._spot_client.create_oco_order(
                    symbol=symbol,
                    side="SELL",
                    quantity=qty,
                    above_type="LIMIT_MAKER",
                    above_price=tp_price,
                    below_type="STOP_LOSS_LIMIT",
                    below_stop_price=sl_price,
                    below_price=self._align_price(symbol, sl_price * 0.99),
                    list_client_order_id=f"sml-{uuid.uuid4().hex[:8]}",
                )
                oco_id = str(oco.get("orderListId", ""))
                order_id = str(oco.get("orders", [{}])[0].get("orderId", ""))
            except Exception as exc:
                print(f"[SpotMargin/LONG] {symbol}: order failed {exc}")
                return None
        else:
            order_id = f"DRY_{uuid.uuid4().hex[:8]}"
            oco_id = None

        pos = SpotMarginPosition(
            symbol=symbol,
            side="LONG",
            qty=qty,
            entry_price=entry_price,
            sl_price=sl_price,
            tp_price=tp_price,
            order_id=order_id,
            oco_order_list_id=oco_id,
            metadata={
                "trade_type": "spot_margin_synthetic",
                "simulated_leverage": self._leverage,
                "entry_order_id": entry_order_id or order_id,
                "oco_order_list_id": oco_id,
                **kwargs,
            },
        )
        self._open_positions[self._pos_key(symbol, "LONG")] = pos
        self._persist_open(pos)
        print(f"[SpotMargin/LONG] {symbol}: opened {qty} @ {entry_price:.2f} SL={sl_price:.2f} TP={tp_price:.2f}")
        return pos

    def open_short(
        self,
        symbol: str,
        usdt_amount: float,
        sl_pct: float = 2.0,
        tp_pct: float = 4.0,
        leverage: int = 2,
        **kwargs,
    ) -> Optional[SpotMarginPosition]:
        leverage = int(leverage or self._leverage)
        price = self._get_price(symbol)
        if price <= 0:
            print(f"[SpotMargin/SHORT] {symbol}: cannot get price")
            return None
        notional = usdt_amount * leverage
        current_notional = self._total_open_notional()
        if current_notional + notional > self._capital_usdt:
            notional = max(0.0, self._capital_usdt - current_notional)
            if notional < 10.0:
                print(f"[SpotMargin/SHORT] {symbol}: blocked, total notional would exceed capital ${self._capital_usdt:.2f}")
                return None
            print(f"[SpotMargin/SHORT] {symbol}: capped notional to ${notional:.2f} to stay within capital")
        qty = self._round_qty(symbol, notional / price)
        notional = qty * price
        if notional < 10.0:
            print(f"[SpotMargin/SHORT] {symbol}: notional ${notional:.2f} < $10")
            return None

        entry_price = price
        sl_price = self._align_price(symbol, entry_price * (1 + sl_pct / 100))  # SL above entry for short
        tp_price = self._align_price(symbol, entry_price * (1 - tp_pct / 100))  # TP below entry for short

        order_id = None
        oco_id = None
        if not self.dry_run:
            try:
                # Synthetic short: place the real closing OCO BUY now.
                # SL leg is above current (stop-loss buy), TP leg is below current (limit maker buy).
                oco = self._spot_client.create_oco_order(
                    symbol=symbol,
                    side="BUY",
                    quantity=qty,
                    above_type="STOP_LOSS_LIMIT",
                    above_price=self._align_price(symbol, sl_price * 1.01),
                    above_stop_price=sl_price,
                    below_type="LIMIT_MAKER",
                    below_stop_price=tp_price,
                    below_price=tp_price,
                    list_client_order_id=f"sms-{uuid.uuid4().hex[:8]}",
                )
                oco_id = str(oco.get("orderListId", ""))
                order_id = str(oco.get("orders", [{}])[0].get("orderId", ""))
            except Exception as exc:
                print(f"[SpotMargin/SHORT] {symbol}: OCO order failed {exc}")
                return None
        else:
            order_id = f"DRY_{uuid.uuid4().hex[:8]}"

        pos = SpotMarginPosition(
            symbol=symbol,
            side="SHORT",
            qty=qty,
            entry_price=entry_price,
            sl_price=sl_price,
            tp_price=tp_price,
            order_id=order_id,
            oco_order_list_id=oco_id,
            metadata={
                "trade_type": "spot_margin_synthetic",
                "simulated_leverage": self._leverage,
                "oco_order_list_id": oco_id,
                **kwargs,
            },
        )
        self._open_positions[self._pos_key(symbol, "SHORT")] = pos
        self._persist_open(pos)
        print(f"[SpotMargin/SHORT] {symbol}: synthetic opened {qty} @ {entry_price:.2f} SL={sl_price:.2f} TP={tp_price:.2f}")
        return pos

    def close_long(self, symbol: str, qty: Optional[float] = None, **kwargs) -> bool:
        key = self._pos_key(symbol, "LONG")
        pos = self._open_positions.get(key)
        if not pos or pos.closed:
            return False
        qty = qty if qty is not None else pos.qty
        if not self.dry_run and pos.oco_order_list_id:
            self._cancel_oco(symbol, pos)
        if not self.dry_run:
            try:
                sell = self._spot_client.create_market_order(symbol, "SELL", quantity=qty)
                exit_price = self._avg_fill_price(sell)
            except Exception as exc:
                print(f"[SpotMargin/LONG] {symbol}: close sell failed {exc}")
                return False
        else:
            exit_price = self._get_price(symbol)
        pos.closed = True
        pos.closed_at = datetime.now(timezone.utc)
        pos.exit_price = exit_price
        pos.pnl = (exit_price - pos.entry_price) * pos.qty
        self._persist_closed(pos, kwargs.get("reason", "manual_close"))
        print(f"[SpotMargin/LONG] {symbol}: closed @ {exit_price:.2f} pnl={pos.pnl:.2f}")
        return True

    def close_short(self, symbol: str, qty: Optional[float] = None, **kwargs) -> bool:
        key = self._pos_key(symbol, "SHORT")
        pos = self._open_positions.get(key)
        if not pos or pos.closed:
            return False
        qty = qty if qty is not None else pos.qty
        if not self.dry_run and pos.oco_order_list_id:
            self._cancel_oco(symbol, pos)
        if not self.dry_run:
            try:
                # Buy at market to close the synthetic short, then immediately sell back to USDT.
                buy = self._spot_client.create_market_order(symbol, "BUY", quantity=qty)
                exit_price = self._avg_fill_price(buy)
                sell = self._spot_client.create_market_order(symbol, "SELL", quantity=qty)
            except Exception as exc:
                print(f"[SpotMargin/SHORT] {symbol}: close buy/sell failed {exc}")
                return False
        else:
            exit_price = self._get_price(symbol)
        pos.closed = True
        pos.closed_at = datetime.now(timezone.utc)
        pos.exit_price = exit_price
        pos.pnl = (pos.entry_price - exit_price) * pos.qty
        self._persist_closed(pos, kwargs.get("reason", "manual_close"))
        print(f"[SpotMargin/SHORT] {symbol}: synthetic closed @ {exit_price:.2f} pnl={pos.pnl:.2f}")
        return True

    def check_positions(self, **kwargs) -> List[str]:
        """No-op reconciliation for now; OCO fills will be handled via event stream."""
        return []

    def reevaluate_open_positions(self, settings, regime_mode: str, profile_label: str, **kwargs) -> int:
        return 0

    def _persist_smart_meta(self, pos: SpotMarginPosition):
        """Persist smart metadata updates to DB if available."""
        if not self.db_client or not pos.metadata:
            return
        try:
            meta = dict(pos.metadata)
            meta["smart_meta"] = pos._smart_meta
            self.db_client.execute_update(
                "UPDATE trades_open SET metadata = metadata || %s::jsonb WHERE symbol=%s AND side=%s AND metadata->>'trade_type' = 'spot_margin_synthetic'",
                (json.dumps(meta), pos.symbol, "SELL" if pos.side == "SHORT" else "BUY"),
            )
        except Exception as exc:
            print(f"[SpotMargin/DB] smart_meta persist failed {pos.symbol}: {exc}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _pos_key(symbol: str, side: str) -> str:
        return f"{symbol}_{side.upper()}"

    def _init_spot_client(self):
        if self.dry_run:
            return
        self._spot_client = BinanceClient(
            self.api_key,
            self.api_secret,
            testnet=True,
            dry_run=False,
        )

    def _get_price(self, symbol: str) -> float:
        if self.dry_run:
            # Fallback price for dry-run tests.
            return 65000.0 if "BTC" in symbol else 1.0
        return self._spot_client.get_symbol_price(symbol)

    def _round_qty(self, symbol: str, qty: float) -> float:
        if self.dry_run:
            return round(qty, 5)
        filters = self._spot_client.get_symbol_filters(symbol)
        step = filters.get("step", 0.00001)
        return round(qty / step) * step

    def _align_price(self, symbol: str, price: float) -> float:
        if self.dry_run:
            return round(price, 6)
        filters = self._spot_client.get_symbol_filters(symbol)
        tick = filters.get("tick", 0.01)
        return round(price / tick) * tick

    def _avg_fill_price(self, order: Dict) -> float:
        fills = order.get("fills", [])
        if not fills:
            return float(order.get("price", 0) or self._get_price(order.get("symbol", "")))
        total_qty = sum(float(f.get("qty", 0)) for f in fills)
        total_cost = sum(float(f.get("price", 0)) * float(f.get("qty", 0)) for f in fills)
        return total_cost / total_qty if total_qty > 0 else 0.0

    def _verify_filled(self, order: Dict) -> bool:
        if not order:
            return False
        if order.get("status") == "FILLED":
            return True
        if order.get("fills"):
            return True
        return False

    def _cancel_oco(self, symbol: str, pos: SpotMarginPosition):
        """Cancel the OCO order list by canceling one of its leg orders."""
        if self.dry_run or not self._spot_client or not pos.order_id:
            return
        try:
            import urllib.request
            import urllib.parse
            import hmac
            import hashlib

            base = self._spot_client.client.API_URL
            params = {"symbol": symbol, "orderId": pos.order_id, "timestamp": int(time.time() * 1000), "recvWindow": 60000}
            query = urllib.parse.urlencode(params)
            signature = hmac.new(self._spot_client.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            url = f"{base}/v3/order?{query}&signature={signature}"
            req = urllib.request.Request(url, method="DELETE", headers={"X-MBX-APIKEY": self._spot_client.api_key})
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp.read()
        except Exception as exc:
            print(f"[SpotMargin] cancel OCO failed {symbol}: {exc}")

    # ── Persistence ───────────────────────────────────────────────────────────

    def _persist_open(self, pos: SpotMarginPosition):
        if not self.db_client:
            return
        try:
            meta = json.dumps(pos.metadata)
            self.db_client.execute_update(
                """
                INSERT INTO trades_open
                    (symbol, side, quantity, price, entry_time, stop_loss, take_profit, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    pos.symbol,
                    "SELL" if pos.side == "SHORT" else "BUY",
                    pos.qty,
                    pos.entry_price,
                    pos.opened_at.isoformat(),
                    pos.sl_price,
                    pos.tp_price,
                    meta,
                ),
            )
        except Exception as exc:
            print(f"[SpotMargin/DB] open persist failed {pos.symbol}: {exc}")

    def _persist_closed(self, pos: SpotMarginPosition, reason: str):
        if not self.db_client:
            return
        try:
            pnl_pct = round((pos.pnl / (pos.qty * pos.entry_price)) * 100, 4) if pos.entry_price and pos.qty else 0.0
            meta = json.dumps(pos.metadata)
            self.db_client.execute_update(
                """
                INSERT INTO trades_closed
                    (symbol, side, quantity, entry_price, exit_price, pnl, pnl_percentage,
                     close_reason, entry_time, exit_time, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    pos.symbol,
                    "SELL" if pos.side == "SHORT" else "BUY",
                    pos.qty,
                    pos.entry_price,
                    pos.exit_price,
                    round(pos.pnl, 6),
                    pnl_pct,
                    reason,
                    pos.opened_at.isoformat(),
                    pos.closed_at.isoformat() if pos.closed_at else datetime.now(timezone.utc).isoformat(),
                    meta,
                ),
            )
            # Remove open row
            self.db_client.execute_update(
                "DELETE FROM trades_open WHERE ctid = (SELECT ctid FROM trades_open WHERE symbol=%s AND side=%s AND metadata->>'trade_type' = 'spot_margin_synthetic' ORDER BY entry_time ASC LIMIT 1)",
                (pos.symbol, "SELL" if pos.side == "SHORT" else "BUY"),
            )
        except Exception as exc:
            print(f"[SpotMargin/DB] close persist failed {pos.symbol}: {exc}")

    # ── Unsupported methods, no-ops ─────────────────────────────────────────────

    def check_and_fund(self, *args, **kwargs) -> bool:
        return False

    def update_sl_tp(self, *args, **kwargs) -> bool:
        return False

    def handle_order_fill_event(self, *args, **kwargs):
        pass

    def get_open_positions_summary(self, *args, **kwargs) -> List[Dict]:
        return []

    def __getattr__(self, name: str) -> Any:
        # No-op for any other FuturesClient method callers may expect.
        def _noop(*args, **kwargs):
            return None
        return _noop
