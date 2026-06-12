"""
Trade Utilities Module

Shared low-level helpers used by run_trades.py, portfolio_manager.py and
any other module that needs quantity alignment or symbol-filter lookups.
Centralising these prevents duplicated code and ensures the 1-hour filter
cache in BinanceClient is always used.
"""

import math
from decimal import Decimal
from typing import Dict, TYPE_CHECKING

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


def compute_commission(quantity: float, price: float, settings) -> float:
    """Calculate entry/exit commission in USDT based on settings."""
    rate = getattr(settings, "binance_commission_rate", 0.001)
    if getattr(settings, "use_bnb_for_commission", False):
        rate = 0.0005
    return quantity * price * rate


def format_indicators_for_predictor(indicators: dict, price: float) -> dict:
    """
    Normalise the raw indicator dict returned by AutonomousSignalGenerator
    into the structure expected by PnLPredictor.predict_pnl.
    """
    return {
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
