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
LOOKBACK_DAYS = 365
INTERVAL = "1h"
WINDOW = 100        # candles fed to analyze_signal
FORWARD_CANDLES = 24    # candles to simulate exit (24h horizon — matches real trade duration)
SL_ATR_MULT = 1.5
TP_ATR_MULT = 2.5
SL_PCT_FALLBACK = 0.015
TP_PCT_FALLBACK = 0.025

# ── Per-category lookback days (extended jun-16-2026) ──────────────────────────
# Binance public API provides up to ~1500 days of 1h klines per symbol
# Extended to maximize synthetic trade samples without diminishing returns
_NARRATIVE_TOKENS = {
    "FETUSDT", "TAOUSDT", "RENDERUSDT", "NEARUSDT", "WLDUSDT",
    "AGIXUSDT", "OCEANUSDT", "NMRUSDT", "RLCUSDT",
}
_MIDCAP_ALTS = {
    "SOLUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT", "SUIUSDT",
    "ADAUSDT", "XRPUSDT", "WIFUSDT", "PEPEUSDT", "DOGEUSDT",
}
_LARGE_CAPS = {"BTCUSDT", "ETHUSDT", "BNBUSDT"}
_MEME_TOKENS = {"DOGEUSDT", "PEPEUSDT", "WIFUSDT"}

_LOOKBACK_NARRATIVE = 1000   # 3 years — captures multiple AI narrative cycles
_LOOKBACK_MIDCAP    =  700   # ~2 years — 2 full market cycles
_LOOKBACK_LARGECAP  = 1000   # 3 years — BTC/ETH/BNB have clean long history
_LOOKBACK_MEME      =  500   # Meme coins: take what’s available


def _lookback_for_symbol(symbol: str, default: int = LOOKBACK_DAYS) -> int:
    """Return appropriate lookback days based on token category."""
    if symbol in _MEME_TOKENS:
        return _LOOKBACK_MEME
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
        self.indicators_at_entry = indicators   # includes vol_spike/atr_pct/trend_4h/weekly_range
        self.pnl_prediction = None              # no prior prediction to compare
        self.prediction_accuracy = None
        self.is_corrupted = False               # synthetic trades are never corrupted


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

    # Pre-compute weekly ranges for weekly_range feature (sliding 7d = 168 candles)
    _WEEKLY = 168

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
            atr = entry_price * SL_PCT_FALLBACK
            sl = entry_price * (1 - SL_PCT_FALLBACK)
            tp = entry_price * (1 + TP_PCT_FALLBACK)

        # Simulate exit over 24h window
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

        # ── Build extended indicator dict (4 new features) ─────────────────────────
        base_indicators = sig.get("indicators", {}) or {}

        # vol_spike: last volume vs avg of previous 20 candles
        _vol_avg20 = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else sum(volumes[:-1]) / max(len(volumes) - 1, 1)
        _vol_spike = volumes[-1] / _vol_avg20 if _vol_avg20 > 0 else 1.0

        # atr_pct: ATR14 as percentage of price
        _atr_pct = (atr / entry_price * 100) if entry_price > 0 else 2.0

        # trend_4h: every 4 hourly candles = 1 four-hour candle; use EMA9 vs EMA21 on last 21 of those
        _4h_closes = closes[::4][-22:]  # downsample 1h → 4h
        _trend_4h = 0
        if len(_4h_closes) >= 22:
            _k9 = 2 / 10; _k21 = 2 / 22
            _e9 = _4h_closes[0]; _e21 = _4h_closes[0]
            for _c in _4h_closes[1:]:
                _e9  = _c * _k9  + _e9  * (1 - _k9)
                _e21 = _c * _k21 + _e21 * (1 - _k21)
            _trend_4h = 1.0 if _e9 > _e21 else -1.0

        # weekly_range: where is entry_price in the last 168h (7d) high-low range
        _w_start = max(0, i - _WEEKLY)
        _w_highs = [k["high"] for k in klines[_w_start: i + WINDOW]]
        _w_lows  = [k["low"]  for k in klines[_w_start: i + WINDOW]]
        _wk_high = max(_w_highs) if _w_highs else entry_price * 1.05
        _wk_low  = min(_w_lows)  if _w_lows  else entry_price * 0.95
        _wk_range = _wk_high - _wk_low
        _weekly_range = (entry_price - _wk_low) / _wk_range if _wk_range > 0 else 0.5

        extended_indicators = {
            **base_indicators,
            "vol_spike":    _vol_spike,
            "atr_pct":      _atr_pct,
            "trend_4h":     _trend_4h,
            "weekly_range": _weekly_range,
        }

        results.append(_SyntheticPosition(
            symbol=symbol,
            pnl=round(pnl, 6),
            pnl_pct=round(pnl_pct, 4),
            indicators=extended_indicators,
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
