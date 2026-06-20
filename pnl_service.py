"""
PnL Service Module

Single responsibility: obtain a PnL prediction for a symbol.

Centralises the duplicated prediction logic that previously existed in both
run_trades.py (_buy_top_symbols, execute_once) and portfolio_manager.py so
both callers use the same code path.
"""

import logging
from typing import Optional, Dict, Tuple, TYPE_CHECKING

from pnl_predictor import PnLPredictor
from trade_utils import format_indicators_for_predictor

if TYPE_CHECKING:
    from binance_client import BinanceClient
    from autonomous_signals import AutonomousSignalGenerator

logger = logging.getLogger(__name__)

# Module-level singleton – shared across the process so the model is only
# loaded once.
_predictor: Optional[PnLPredictor] = None


def get_predictor() -> PnLPredictor:
    global _predictor
    if _predictor is None:
        _predictor = PnLPredictor()
    return _predictor


def predict_for_symbol(
    symbol: str,
    client: "BinanceClient",
    settings,
    position_size_usdt: float,
    interval: str = "1h",
    precomputed_signal: Optional[Dict] = None,
) -> Tuple[Optional[Dict], Optional[Dict]]:
    """
    Fetch indicators + run the PnL predictor for *symbol*.

    Args:
        precomputed_signal: If provided (from signal_meta in Step 1), skip
            analyze_symbol() and use these indicators directly. Avoids
            redundant API + analysis calls (BUG-E4 fix).

    Returns:
        (prediction, formatted_indicators) – both None on failure.
    """
    predictor = get_predictor()
    try:
        price = client.get_symbol_price(symbol)

        if precomputed_signal and precomputed_signal.get("indicators"):
            indicators_raw = precomputed_signal["indicators"]
        else:
            from autonomous_signals import AutonomousSignalGenerator
            signal_gen = AutonomousSignalGenerator(client)
            signal_data = signal_gen.analyze_symbol(symbol, interval=interval)
            indicators_raw = signal_data.get("indicators", {})
        formatted = format_indicators_for_predictor(indicators_raw, price)

        # Use realistic trade horizon (240 min = 4h) for PnL estimation.
        # The trade cycle is 5 min (SL/TP check frequency), but actual
        # position duration until SL/TP hit is typically 1-8 hours.
        trade_horizon_minutes = getattr(settings, "trade_horizon_minutes", 240)
        prediction = predictor.predict_pnl(
            symbol=symbol,
            current_price=price,
            indicators=formatted,
            cycle_duration_minutes=trade_horizon_minutes,
            position_size_usdt=position_size_usdt,
        )
        return prediction, formatted
    except Exception as exc:
        logger.warning(f"PnL prediction failed for {symbol}: {exc}")
        return None, None


def validate(prediction: Dict, settings, symbol: str = None,
             news_client=None) -> Tuple[bool, str]:
    """
    Validate a prediction dict against the thresholds defined in settings.
    Applies Fear & Greed modifier and per-category thresholds when available.

    Returns:
        (is_valid, reason_string)
    """
    if prediction is None:
        return True, "no prediction available – allowing trade"
    # Bootstrap mode: no learned data yet → skip PnL/probability gates so the
    # system can execute trades and accumulate the history needed for learning.
    if prediction.get("bootstrap_mode"):
        return True, "bootstrap mode – thresholds relaxed until trade history builds"
    predictor = get_predictor()
    min_pnl  = getattr(settings, "min_expected_pnl", 0.0)
    min_prob = getattr(settings, "min_success_probability", 0.5)
    max_risk = getattr(settings, "max_risk_percentage", 10.0)

    # ── Per-category thresholds ────────────────────────────────────────────────
    if symbol:
        categories      = getattr(settings, "token_categories", {})
        thresholds_map  = getattr(settings, "category_thresholds", {})
        cat = categories.get(symbol, {}).get("market_behavior", "MIDCAP_ALT")
        cat_th = thresholds_map.get(cat, {})
        if cat_th:
            min_prob = cat_th.get("min_prob", min_prob)
            min_pnl  = cat_th.get("min_pnl",  min_pnl)

    # ── Fear & Greed modifier ─────────────────────────────────────────────────
    fng_modifier = 0.0
    fng_label    = "Neutral"
    if news_client is not None:
        try:
            fng_modifier = news_client.get_fear_greed_modifier()
            fng_label    = news_client.get_fear_greed().get("label", "Neutral")
        except Exception:
            pass
    effective_min_prob = min_prob + fng_modifier
    if fng_modifier != 0.0:
        logger.debug(
            f"validate {symbol}: F&G={fng_label} → min_prob {min_prob:.2f}"
            f" → {effective_min_prob:.2f} (delta={fng_modifier:+.2f})"
        )

    return predictor.validate_prediction(
        prediction,
        min_expected_pnl=min_pnl,
        min_success_probability=effective_min_prob,
        max_risk_percentage=max_risk,
    )
