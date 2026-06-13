"""
Bootstrap History Module

Downloads 90 days of 1h klines from Binance for each analysis symbol,
simulates trades using TechnicalIndicators, and feeds the results to
LearningEngine so the PnLPredictor has calibrated weights from day 1.

Simulation logic (per symbol):
  1. Slide a 100-candle window over the full history.
  2. Run TechnicalIndicators.analyze_signal() on each window.
  3. When signal == "BUY" and strength >= 0.35:
       - entry_price = window[-1]['close']
       - Simulate SL/TP exit over the next 5 candles (1h each):
           * SL = entry - ATR*1.5   (or 1.5% fallback)
           * TP = entry + ATR*2.5   (or 2.5% fallback)
       - Record outcome as a fake ClosedPosition.
  4. After all windows, call LearningEngine._compute_adjustments() +
     _persist_weights() with the synthetic positions.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np

from technical_indicators import TechnicalIndicators

logger = logging.getLogger(__name__)

MIN_STRENGTH = 0.35
LOOKBACK_DAYS = 90
INTERVAL = "1h"
WINDOW = 100       # candles fed to analyze_signal
FORWARD_CANDLES = 5   # candles to simulate exit
SL_ATR_MULT = 1.5
TP_ATR_MULT = 2.5
SL_PCT_FALLBACK = 0.015
TP_PCT_FALLBACK = 0.025

# ── Per-category lookback days ────────────────────────────────────────────────
# Narrative/AI tokens: sparse activity bursts → need more history to sample enough breakouts
_NARRATIVE_TOKENS = {
    "FETUSDT", "TAOUSDT", "RENDERUSDT", "NEARUSDT", "WLDUSDT",
    "AGIXUSDT", "OCEANUSDT", "NMRUSDT", "RLCUSDT",
}
# Mid-cap alts: moderate activity → 180d gives 2x more signal samples
_MIDCAP_ALTS = {
    "SOLUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT", "SUIUSDT",
    "ADAUSDT", "XRPUSDT", "WIFUSDT", "PEPEUSDT", "DOGEUSDT",
}
# Large caps: high daily activity → 90d is sufficient
_LARGE_CAPS = {"BTCUSDT", "ETHUSDT", "BNBUSDT"}

_LOOKBACK_NARRATIVE = 365
_LOOKBACK_MIDCAP    = 180
_LOOKBACK_LARGECAP  =  90


def _lookback_for_symbol(symbol: str, default: int = LOOKBACK_DAYS) -> int:
    """Return appropriate lookback days based on token category."""
    if symbol in _NARRATIVE_TOKENS:
        return _LOOKBACK_NARRATIVE
    if symbol in _MIDCAP_ALTS:
        return _LOOKBACK_MIDCAP
    if symbol in _LARGE_CAPS:
        return _LOOKBACK_LARGECAP
    return default


# ── Fake position object (duck-typed for LearningEngine) ─────────────────────

class _SyntheticPosition:
    """Minimal duck-type of ClosedPosition for LearningEngine."""

    def __init__(self, symbol: str, pnl: float, pnl_pct: float,
                 indicators: Dict, entry_price: float, exit_price: float,
                 entry_time: str, exit_time: str, close_reason: str):
        self.symbol = symbol
        self.pnl = pnl
        self.pnl_percentage = pnl_pct
        self.entry_price = entry_price
        self.exit_price = exit_price
        self.entry_time = entry_time
        self.exit_time = exit_time
        self.close_reason = close_reason
        self.indicators_at_entry = indicators
        self.pnl_prediction = None          # no prior prediction to compare
        self.prediction_accuracy = None


# ── Kline fetcher (pages through Binance in 1000-candle chunks) ───────────────

def _fetch_all_klines(client, symbol: str, interval: str = INTERVAL,
                      days: int = LOOKBACK_DAYS) -> List[Dict]:
    """Fetch up to `days` worth of klines, paging through Binance 1000-limit."""
    all_klines: List[Dict] = []
    end_ms = None
    target_candles = days * 24  # 1h candles per day

    while len(all_klines) < target_candles:
        params = dict(symbol=symbol, interval=interval, limit=1000)
        if end_ms:
            params["endTime"] = end_ms
        try:
            raw = client.client.get_klines(**params)
        except Exception as e:
            logger.warning(f"Bootstrap kline fetch failed for {symbol}: {e}")
            break
        if not raw:
            break
        parsed = [{
            "open_time": k[0],
            "open":   float(k[1]),
            "high":   float(k[2]),
            "low":    float(k[3]),
            "close":  float(k[4]),
            "volume": float(k[5]),
        } for k in raw]
        all_klines = parsed + all_klines          # prepend older data
        end_ms = raw[0][0] - 1                    # go further back
        if len(raw) < 1000:
            break                                 # hit the start of history

    # Sort ascending, trim to target
    all_klines.sort(key=lambda k: k["open_time"])
    return all_klines[-target_candles:]


# ── Single-symbol simulation ──────────────────────────────────────────────────

def _simulate_symbol(symbol: str, klines: List[Dict],
                     position_size_usdt: float = 50.0) -> List[_SyntheticPosition]:
    """Slide a window over klines and simulate BUY trades."""
    if len(klines) < WINDOW + FORWARD_CANDLES:
        logger.warning(f"Bootstrap {symbol}: not enough klines ({len(klines)})")
        return []

    results: List[_SyntheticPosition] = []
    step = 3   # analyse every 3rd window to avoid overfitting

    for i in range(0, len(klines) - WINDOW - FORWARD_CANDLES, step):
        window = klines[i: i + WINDOW]
        future = klines[i + WINDOW: i + WINDOW + FORWARD_CANDLES]

        closes  = [k["close"]  for k in window]
        highs   = [k["high"]   for k in window]
        lows    = [k["low"]    for k in window]
        volumes = [k["volume"] for k in window]

        sig = TechnicalIndicators.analyze_signal(closes, highs, lows, volumes)

        if sig["signal"] != "BUY" or sig["strength"] < MIN_STRENGTH:
            continue

        entry_price = closes[-1]
        entry_time  = str(datetime.utcfromtimestamp(window[-1]["open_time"] / 1000).isoformat())

        # ATR-based SL/TP
        atr_vals = TechnicalIndicators.atr(highs, lows, closes, 14)
        if atr_vals:
            atr = atr_vals[-1]
            sl = entry_price - atr * SL_ATR_MULT
            tp = entry_price + atr * TP_ATR_MULT
        else:
            sl = entry_price * (1 - SL_PCT_FALLBACK)
            tp = entry_price * (1 + TP_PCT_FALLBACK)

        # Simulate exit
        exit_price = None
        close_reason = "timeout"
        for fc in future:
            low_c  = fc["low"]
            high_c = fc["high"]
            if low_c <= sl:
                exit_price = sl
                close_reason = "stop_loss"
                exit_time = str(datetime.utcfromtimestamp(fc["open_time"] / 1000).isoformat())
                break
            if high_c >= tp:
                exit_price = tp
                close_reason = "take_profit"
                exit_time = str(datetime.utcfromtimestamp(fc["open_time"] / 1000).isoformat())
                break
        if exit_price is None:
            exit_price = future[-1]["close"]
            exit_time  = str(datetime.utcfromtimestamp(future[-1]["open_time"] / 1000).isoformat())

        qty = position_size_usdt / entry_price
        pnl = (exit_price - entry_price) * qty
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100

        results.append(_SyntheticPosition(
            symbol=symbol,
            pnl=round(pnl, 6),
            pnl_pct=round(pnl_pct, 4),
            indicators=sig.get("indicators", {}),
            entry_price=entry_price,
            exit_price=exit_price,
            entry_time=entry_time,
            exit_time=exit_time,
            close_reason=close_reason,
        ))

    return results


# ── Public API ────────────────────────────────────────────────────────────────

def run_bootstrap(binance_client, settings,
                  lookback_days: int = LOOKBACK_DAYS) -> Dict:
    """
    Run the full bootstrap for all analysis_symbols.

    Returns a summary dict with per-symbol trade counts and overall stats.
    """
    from learning_engine import get_learning_engine, WeightAdjustment, _WEIGHT_MIN, _WEIGHT_MAX

    symbols = getattr(settings, "analysis_symbols", [
        "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
        "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
        "SUIUSDT", "PEPEUSDT", "FETUSDT", "RENDERUSDT", "WIFUSDT",
    ])
    position_size = getattr(settings, "max_per_trade_usdt", 50.0)

    all_synthetic: List[_SyntheticPosition] = []
    per_symbol: Dict[str, int] = {}

    logger.info(f"Bootstrap: starting for {len(symbols)} symbols, variable lookback per category")

    for symbol in symbols:
        try:
            sym_lookback = _lookback_for_symbol(symbol, lookback_days)
            klines = _fetch_all_klines(binance_client, symbol,
                                       interval=INTERVAL, days=sym_lookback)
            if len(klines) < WINDOW + FORWARD_CANDLES:
                logger.warning(f"Bootstrap {symbol}: skipped (only {len(klines)} klines)")
                per_symbol[symbol] = 0
                continue

            trades = _simulate_symbol(symbol, klines, position_size)
            all_synthetic.extend(trades)
            per_symbol[symbol] = len(trades)
            wins = sum(1 for t in trades if t.pnl > 0)
            logger.info(
                f"Bootstrap {symbol} ({sym_lookback}d): {len(trades)} simulated trades"
                + (f", win_rate={wins/len(trades):.1%}" if trades else "")
            )
        except Exception as e:
            logger.error(f"Bootstrap {symbol} failed: {e}")
            per_symbol[symbol] = 0

    if not all_synthetic:
        logger.warning("Bootstrap: no synthetic trades generated — skipping weight update")
        return {"status": "skipped", "reason": "no trades generated", "per_symbol": per_symbol}

    # Feed all synthetic trades into LearningEngine
    engine = get_learning_engine()
    try:
        # Override _load_closed_positions temporarily to return our synthetic data
        orig_load = engine._load_closed_positions

        def _fake_load(lookback_days_arg):
            return all_synthetic

        engine._load_closed_positions = _fake_load
        result = engine.run(min_trades=5, lookback_days=lookback_days)
        engine._load_closed_positions = orig_load  # restore
    except Exception as e:
        logger.error(f"Bootstrap: LearningEngine.run() failed: {e}")
        return {"status": "error", "error": str(e), "per_symbol": per_symbol}

    # Invalidate PnLPredictor weight cache so next trade uses new weights
    try:
        from pnl_service import get_predictor
        get_predictor().invalidate_cache()
    except Exception:
        pass

    total = len(all_synthetic)
    wins_total = sum(1 for t in all_synthetic if t.pnl > 0)
    summary = {
        "status": "completed",
        "total_simulated_trades": total,
        "global_win_rate": round(wins_total / total, 4) if total else 0,
        "symbols": len(symbols),
        "per_symbol": per_symbol,
        "learning_result": result,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    logger.info(f"Bootstrap complete: {total} trades, win_rate={summary['global_win_rate']:.1%}")
    return summary


def needs_bootstrap(min_trades: int = 50) -> bool:
    """Return True if the LearningEngine has fewer than min_trades of real history."""
    try:
        from init_components import get_components
        pm = get_components().position_manager
        closed = pm.get_closed_positions(limit=min_trades + 1)
        return len(closed) < min_trades
    except Exception:
        return True
