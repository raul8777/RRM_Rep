"""
Trade Utilities Module

Shared low-level helpers used by run_trades.py, portfolio_manager.py and
any other module that needs quantity alignment or symbol-filter lookups.
Centralising these prevents duplicated code and ensures the 1-hour filter
cache in BinanceClient is always used.
"""

import math
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from binance_client import BinanceClient


def align_quantity_to_step(quantity: float, step: float) -> float:
    """Align a quantity to the exchange LOT_SIZE step size (truncate, not round)."""
    if step <= 0 or quantity <= 0:
        return 0.0
    q = Decimal(str(quantity))
    step_dec = Decimal(str(step))
    aligned = (q // step_dec) * step_dec
    return float(aligned.normalize()) if aligned > 0 else 0.0


def get_symbol_filters(client: "BinanceClient", symbol: str) -> Dict[str, float]:
    """
    Return LOT_SIZE / NOTIONAL filters for *symbol*, using the client's
    built-in 1-hour cache so repeated calls within a trade cycle are free.
    """
    return client.get_symbol_filters(symbol)


def is_quantity_valid(qty: float, price: float, filters: Dict[str, float]) -> tuple:
    """
    Check whether *qty* passes all exchange filter constraints.

    Returns:
        (is_valid: bool, reason: str)  – reason is empty string when valid.
    """
    if qty <= 0:
        return False, f"qty={qty} is zero or negative"
    if qty < filters["min_qty"]:
        return False, f"qty={qty} below min_qty={filters['min_qty']}"
    notional = qty * price
    if notional < filters["min_notional"]:
        return False, f"notional={notional:.4f} below min_notional={filters['min_notional']}"
    return True, ""


def compute_commission(quantity: float, price: float, settings, trade_type: str = "spot") -> float:
    """Calculate entry/exit commission in USDT using the centralized Binance module."""
    from binance_commission import entry_commission
    return entry_commission(quantity, price, trade_type, settings)


def get_binance_asset_balance(client: "BinanceClient", asset: str) -> Dict[str, Any]:
    """Return typed Spot balances; an API failure is never represented as zero."""
    try:
        if hasattr(client, "get_account_balance_details"):
            try:
                details = client.get_account_balance_details()
            except (AttributeError, NotImplementedError):
                details = None
            if details is not None:
                row = details.get(asset, {"free": 0.0, "locked": 0.0, "total": 0.0})
                free = float(row["free"])
                locked = float(row["locked"])
                total = float(row["total"])
                if min(free, locked, total) < 0 or abs(total - free - locked) > 1e-8:
                    raise ValueError(f"invalid balance breakdown for {asset}")
                return {"ok": True, "free": free, "locked": locked, "total": total, "source": "free_locked"}

        balances = client.get_account_balance()
        free = float(balances.get(asset, 0.0))
        if free < 0:
            raise ValueError(f"negative free balance for {asset}")
        return {"ok": True, "free": free, "locked": 0.0, "total": free, "source": "free_only_legacy"}
    except Exception as exc:
        return {
            "ok": False,
            "free": None,
            "locked": None,
            "total": None,
            "source": "api_error",
            "error": str(exc),
        }


def has_recent_binance_sell_proof(
    client: "BinanceClient",
    symbol: str,
    *,
    minimum_qty: float,
    since_time: Any = None,
    hours: int = 24,
) -> bool:
    """Require recent SELL fills covering the tracked quantity; fail closed."""
    try:
        trades = client.get_my_trades(symbol=symbol, limit=50)
        if not isinstance(trades, list):
            return False
        cutoff_ms = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp() * 1000)
        if since_time:
            if isinstance(since_time, datetime):
                since_dt = since_time
            else:
                since_dt = datetime.fromisoformat(str(since_time).replace("Z", "+00:00"))
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
            cutoff_ms = max(cutoff_ms, int(since_dt.timestamp() * 1000))
        sold_qty = 0.0
        for trade in trades:
            if not isinstance(trade, dict) or bool(trade.get("isBuyer", True)):
                continue
            if int(trade.get("time", 0)) < cutoff_ms:
                continue
            sold_qty += float(trade.get("qty", 0.0) or trade.get("quantity", 0.0) or 0.0)
        tolerance = max(1e-8, float(minimum_qty) * 0.001)
        return sold_qty + tolerance >= float(minimum_qty)
    except Exception:
        return False


def get_binance_sell_qty(
    client: "BinanceClient",
    symbol: str,
    db_qty: float,
    current_price: float,
    min_notional_usdt: float = 5.0,
) -> dict:
    """
    Return the quantity that can actually be sold on Binance for *symbol*.

    Compares the DB quantity with the real free balance and returns a dict:
      - qty_to_sell: quantity aligned to LOT_SIZE, capped to real balance
      - balance_qty: raw free balance for the asset
      - is_ghost: True when balance / db_qty < 0.001 (DB record has no backing)
      - is_dust: True when 0 < balance * price < min_notional_usdt
      - top_up_qty: qty needed to reach min_notional_usdt (None if already >= min_notional)
      - reason: human-readable explanation
    """
    asset = symbol.removesuffix("USDT")
    balance = get_binance_asset_balance(client, asset)
    if not balance["ok"]:
        return {
            "qty_to_sell": 0.0,
            "balance_qty": None,
            "free_balance_qty": None,
            "locked_balance_qty": None,
            "db_qty": db_qty,
            "is_ghost": False,
            "is_dust": False,
            "top_up_qty": None,
            "ratio": None,
            "min_notional": min_notional_usdt,
            "balance_status": "api_error",
            "reason": "balance_unavailable",
            "error": balance.get("error"),
        }
    free_qty = balance["free"]
    locked_qty = balance["locked"]
    total_qty = balance["total"]

    filters = get_symbol_filters(client, symbol)
    step = filters.get("step", 0.001)
    min_notional = filters.get("min_notional", min_notional_usdt)

    ratio = total_qty / db_qty if db_qty > 0 else 0.0
    is_ghost = total_qty < 0.001 * db_qty if db_qty > 0 else False

    effective_qty = min(db_qty, free_qty)
    aligned_qty = align_quantity_to_step(effective_qty, step)

    balance_notional = total_qty * current_price
    is_dust = 0 < balance_notional < min_notional
    top_up_qty = None
    if is_dust:
        target_qty = math.ceil(min_notional / max(current_price, 1e-12) / step) * step
        target_qty = align_quantity_to_step(target_qty, step)
        if target_qty > total_qty:
            top_up_qty = align_quantity_to_step(target_qty - total_qty, step)

    return {
        "qty_to_sell": aligned_qty,
        "balance_qty": total_qty,
        "free_balance_qty": free_qty,
        "locked_balance_qty": locked_qty,
        "db_qty": db_qty,
        "is_ghost": is_ghost,
        "is_dust": is_dust,
        "top_up_qty": top_up_qty,
        "ratio": ratio,
        "min_notional": min_notional,
        "balance_status": "ok",
        "reason": (
            "ghost" if is_ghost else
            "dust" if is_dust else
            "balance_locked" if total_qty > 0 and aligned_qty <= 0 else
            "balance_capped" if aligned_qty < db_qty else
            "ok"
        ),
    }


def get_min_notional_delta(
    client: "BinanceClient",
    symbol: str,
    current_price: float,
    target_notional_usdt: float = 5.0,
) -> dict:
    """
    For a Binance orphan/dust balance, compute the BUY qty needed so the total
    reaches the exchange min_notional and can be sold in one MARKET order.
    """
    filters = get_symbol_filters(client, symbol)
    step = filters.get("step", 0.001)
    min_notional = filters.get("min_notional", target_notional_usdt)
    asset = symbol.replace("USDT", "")

    balance = get_binance_asset_balance(client, asset)
    if not balance["ok"]:
        return {
            "needed": False,
            "buy_qty": 0.0,
            "sell_qty": 0.0,
            "balance_status": "api_error",
            "reason": "balance_unavailable",
            "error": balance.get("error"),
        }
    balance_qty = balance["free"]
    if balance["locked"] > 0:
        return {
            "needed": False,
            "buy_qty": 0.0,
            "sell_qty": align_quantity_to_step(balance_qty, step),
            "balance_status": "locked",
            "locked_balance_qty": balance["locked"],
            "total_balance_qty": balance["total"],
            "min_notional": min_notional,
        }

    total_notional = balance_qty * current_price
    if total_notional >= min_notional:
        return {"needed": False, "buy_qty": 0.0, "sell_qty": balance_qty, "min_notional": min_notional}

    needed_notional = max(min_notional, target_notional_usdt) - total_notional
    needed_qty = align_quantity_to_step(needed_notional / max(current_price, 1e-12), step)
    # Make sure we cover rounding and commission; add one step of buffer.
    needed_qty = align_quantity_to_step(needed_qty + step, step)
    return {
        "needed": True,
        "buy_qty": needed_qty,
        "sell_qty": align_quantity_to_step(balance_qty + needed_qty, step),
        "balance_status": "ok",
        "min_notional": min_notional,
        "balance_qty": balance_qty,
    }


def format_indicators_for_predictor(indicators: dict, price: float) -> dict:
    """
    Normalise the raw indicator dict returned by AutonomousSignalGenerator
    into the structure expected by PnLPredictor.predict_pnl.

    Extended fields (when present):
      vol_spike    — volume ratio vs 20-candle average (e.g. 1.8 = 180% of avg)
      atr_pct      — ATR14 / price * 100 (% volatility expected)
      trend_4h     — +1 (EMA9>EMA21 on 4h), 0 (neutral), -1 (bearish)
      weekly_range — price position in 7-day range: 0.0 (support) .. 1.0 (resistance)
    """
    formatted = {
        "rsi": {"value": indicators.get("rsi", 50)},
        "macd": {
            "macd": indicators.get("macd", 0),
            "signal": indicators.get("signal", 0),
            "histogram": indicators.get("histogram", 0),
        },
        "sma": {"value": indicators.get("middle_band", price)},
        "ema": {"value": indicators.get("middle_band", price)},
        "bollinger_bands": {
            "upper": indicators.get("upper_band", price * 1.02),
            "middle": indicators.get("middle_band", price),
            "lower": indicators.get("lower_band", price * 0.98),
        },
    }
    # Extended features — included only when the caller provides them
    if "vol_spike" in indicators:
        formatted["vol_spike"] = {"value": float(indicators["vol_spike"])}
    if "atr_pct" in indicators:
        formatted["atr_pct"] = {"value": float(indicators["atr_pct"])}
    if "trend_4h" in indicators:
        _trend_raw = indicators["trend_4h"]
        if isinstance(_trend_raw, str):
            _trend_norm = _trend_raw.strip().upper()
            _trend_val = 1.0 if _trend_norm in ("UP", "BULL", "BULLISH") else -1.0 if _trend_norm in ("DOWN", "BEAR", "BEARISH") else 0.0
        else:
            _trend_val = float(_trend_raw)
        formatted["trend_4h"] = {"value": _trend_val}
    if "weekly_range" in indicators:
        formatted["weekly_range"] = {"value": float(indicators["weekly_range"])}
    return formatted
