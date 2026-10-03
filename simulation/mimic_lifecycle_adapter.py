"""simulation/mimic_lifecycle_adapter.py — bridge lifecycle kernel to MIMIC simulation clients.

This adapter lets the lifecycle kernel run inside a MIMIC simulation by reading
prices from the simulation clock/feed, generating synthetic analyses, and executing
orders against SimulationBinanceClient / SimulationFuturesClient.
"""

from __future__ import annotations

import bisect
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from lifecycle.adapter import ExecutionAdapter
from lifecycle.base import (
    EntryDecision,
    ExitDecision,
    ExitPolicy,
    LifecycleConfig,
    MarketContext,
    MomentumExitRule,
    PartialTake,
    Position,
    Side,
    SizedTrade,
    StagnationRule,
)
from lifecycle.signal import _DEFAULT_CATEGORIES


# Per-symbol historical quality score loaded from prior peak analysis if available.
def _load_symbol_quality_map() -> Dict[str, float]:
    path = Path("Dev/benchmarks/super_mimic/predictive_sweep/signal_peak_outcomes.jsonl")
    if not path.exists():
        return {}
    sym_peaks: Dict[str, List[float]] = defaultdict(list)
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            peak = rec.get("peak_12h") or rec.get("peak_4h")
            if peak is not None:
                sym_peaks[rec["symbol"]].append(float(peak))
    except Exception:
        return {}
    return {s: sum(v) / len(v) for s, v in sym_peaks.items()}


_SYMBOL_QUALITY_MAP = _load_symbol_quality_map()

_BEST_EXIT_POLICY_PATH = Path("Dev/benchmarks/peak_lifecycle_tuning/best_exit_policy.json")


def _partial_take_from_mode(mode: Optional[str]) -> Any:
    if not mode or mode == "none":
        return ()
    if mode == "tp1_25_at_1.5":
        return (PartialTake(0.25, 1.5, "tp1"),)
    if mode == "tp1_25_at_2.0":
        return (PartialTake(0.25, 2.0, "tp1"),)
    if mode == "tp1_25_at_1.5_tp2_25_at_2.5":
        return (
            PartialTake(0.25, 1.5, "tp1"),
            PartialTake(0.25, 2.5, "tp2"),
        )
    return ()


def _load_best_exit_policies(
    regime: str, profile_label: str = "PREDICTIVE"
) -> Dict[str, ExitPolicy]:
    if not _BEST_EXIT_POLICY_PATH.exists():
        return {}
    try:
        data = json.loads(_BEST_EXIT_POLICY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    per = data.get("per_profile_exit_policy", {})
    policies: Dict[str, ExitPolicy] = {}
    for prof in ("A", "B", "C"):
        p = per.get(prof, {})
        tsh = p.get("time_stop_hours")
        policies[prof] = ExitPolicy(
            regime=regime,
            profile=prof,
            hard_sl_atr_mult=float(p.get("hard_sl_atr_mult", 2.5)),
            hard_tp_atr_mult=float(p.get("hard_tp_atr_mult", 3.0)),
            trailing_activation_pct=float(p.get("trailing_activation_pct", 0.02)),
            trailing_atr_mult=float(p.get("trailing_atr_mult", 1.5)),
            breakeven_activation_pct=float(p.get("breakeven_activation_pct", 0.015)),
            partial_takes=_partial_take_from_mode(p.get("partial_take_mode")),
            momentum_exit=MomentumExitRule(
                int(p.get("momentum_rsi_overbought", 70)),
                int(p.get("momentum_rsi_oversold", 30)),
                int(p.get("momentum_fng_extreme_greed", 80)),
                int(p.get("momentum_fng_extreme_fear", 20)),
            ),
            stagnation_rule=StagnationRule(
                int(p.get("stagnation_min_hours", 6)),
                float(p.get("stagnation_max_drawdown_from_peak_pct", 0.05)),
                0.5,
            ),
            use_expected_gain_for_tp=bool(p.get("use_expected_gain_for_tp", False)),
            expected_gain_mult=float(p.get("expected_gain_mult", 1.0)),
            time_stop_hours=float(tsh) if tsh is not None else None,
        )
    return policies


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


def _approx_24h_return(bars: List[Dict[str, Any]]) -> float:
    """Approximate 24h return percent from 1h bars."""
    if len(bars) < 25:
        return 0.0
    cur = float(bars[-1].get("close", 0))
    past = float(bars[-25].get("close", 0))
    if past <= 0 or cur <= 0:
        return 0.0
    return (cur - past) / past * 100.0


def _load_peak_quality_stats() -> Optional[Dict[str, Any]]:
    path = Path("Dev/benchmarks/super_mimic/predictive_sweep/peak_quality_stats.json")
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


_PEAK_QUALITY_STATS = _load_peak_quality_stats()


def _compute_peak_quality(
    symbol: str,
    bars: List[Dict[str, Any]],
    atr: float,
    expected_pnl: float,
    short_term_score: float,
    regime: str,
    features: Dict[str, float],
) -> float:
    """Predictive z-score for the magnitude of the next 12h peak.

    Uses precomputed per-regime means, stds and weights from historical peak
    analysis. Falls back to a simple heuristic if no stats are available.
    """
    stats = _PEAK_QUALITY_STATS
    if stats and regime in stats:
        s = stats[regime]
        means = s["means"]
        stds = s["stds"]
        weights = s["weights"]
        score = 0.0
        for f, w in weights.items():
            v = features.get(f, 0.0)
            score += w * (v - means.get(f, 0.0)) / stds.get(f, 1.0)
        return score

    # Fallback heuristic when calibration stats are not present.
    current = float(bars[-1].get("close", 0)) if bars else 0.0
    atr_pct = atr / current if current > 0 else 0.0
    category = _DEFAULT_CATEGORIES.get(symbol, "MIDCAP_ALT")
    cat_fact = {"NARRATIVE": 0.02, "MIDCAP_ALT": 0.0, "LARGE_CAP": -0.02}.get(category, 0.0)
    mom = _approx_24h_return(bars)
    regime_term = 0.0
    if regime == "SPOT_BULL":
        regime_term = -0.03 * mom
    elif regime == "RANGE_SCALP":
        regime_term = 0.03 * mom
    sq = (_SYMBOL_QUALITY_MAP.get(symbol, 0.02) - 0.02) * 2.0
    return (
        0.35 * atr_pct
        + 0.30 * expected_pnl
        + 0.10 * short_term_score
        + cat_fact
        + 0.10 * sq
        + regime_term
    )


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


def _signal_thresholds(
    params: Optional[Dict[str, Any]],
    regime: str,
) -> Dict[str, float]:
    """Return configurable signal-generation thresholds with regime-aware defaults."""
    defaults = {
        "SPOT_BULL": {
            "long_range_pos_threshold": 0.20,
            "long_rsi_threshold": 35.0,
            "short_range_pos_threshold": 0.55,
            "short_rsi_threshold": 50.0,
        },
        "RANGE_SCALP": {
            "long_range_pos_threshold": 0.15,
            "long_rsi_threshold": 25.0,
            "short_range_pos_threshold": 0.55,
            "short_rsi_threshold": 50.0,
        },
    }
    d = dict(defaults.get(regime, defaults["SPOT_BULL"]))
    if params:
        for k in d:
            d[k] = float(params.get(k, d[k]))
    return d


def _slice_feed_to_time(feed: Dict[str, List[Dict[str, Any]]], current_ts: float) -> Dict[str, List[Dict[str, Any]]]:
    """Slice feed to only include bars with open_time <= current_ts.

    Uses bisect for O(log n) per symbol. Returns a view (list slices) without copying bar dicts.
    """
    sliced = {}
    for symbol, bars in feed.items():
        if not bars:
            sliced[symbol] = []
            continue
        # bars are sorted by open_time; find insertion point for current_ts
        # We want all bars where open_time <= current_ts
        idx = bisect.bisect_right(bars, current_ts, key=lambda b: b["open_time"])
        sliced[symbol] = bars[:idx]
    return sliced


def _detect_regime(feed: Dict[str, List[Dict[str, Any]]]) -> str:
    """Detect regime from BTC price action (7-day % change).

    Returns:
      - "PRESERVATION" if BTC 7d% < -5%
      - "RANGE_SCALP" if BTC 7d% between -5% and -2%
      - "SPOT_BULL" otherwise
    """
    btc_bars = None
    for key in ("BTCUSDT", "BTC"):
        if key in feed and len(feed[key]) >= 168:
            btc_bars = feed[key]
            break
    if btc_bars is None:
        return "SPOT_BULL"  # default if no BTC data
    current = float(btc_bars[-1].get("close", 0))
    past_7d = float(btc_bars[-168].get("close", 0)) if len(btc_bars) >= 168 else current
    if past_7d <= 0 or current <= 0:
        return "SPOT_BULL"
    pct_7d = (current - past_7d) / past_7d * 100.0
    if pct_7d < -5.0:
        return "PRESERVATION"
    elif pct_7d < -2.0:
        return "RANGE_SCALP"
    return "SPOT_BULL"


def _build_analyses(
    symbols: List[str],
    feed: Dict[str, List[Dict[str, Any]]],
    regime: str = "SPOT_BULL",
    params: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Generate analyses for the lifecycle kernel using price-action indicators.

    Regime-aware:
      - SPOT_BULL: buy pullbacks to support (20-40% range) with volume + RSI 35-55.
      - RANGE_SCALP: buy near range support / oversold bounce with mean-reversion.
    """
    _params = params or {}
    thresholds = _signal_thresholds(params, regime)
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

        side = "HOLD"
        strength = 0.0
        success_probability = 0.0

        if regime == "RANGE_SCALP":
            lrt = thresholds["long_range_pos_threshold"]
            lrsi = thresholds["long_rsi_threshold"]
            srt = thresholds["short_range_pos_threshold"]
            srsi = thresholds["short_rsi_threshold"]

            # Mean-reversion / range-support score (BUY side)
            range_score = 0.0
            if range_pos is not None:
                if range_pos <= lrt:
                    range_score = 0.80  # deep oversold zone
                elif lrt < range_pos <= lrt + 0.20:
                    range_score = 1.0 - abs(range_pos - (lrt + 0.10)) / 0.10
                elif lrt + 0.20 < range_pos <= lrt + 0.35:
                    range_score = 0.35
                elif range_pos >= lrt + 0.55:
                    range_score = -1.0  # top of range, avoid buy

            vol_score = 0.0
            if vol_spike is not None:
                if vol_spike >= 1.3:
                    vol_score = 1.0
                elif vol_spike >= 0.9:
                    vol_score = 0.5
                elif vol_spike >= 0.5:
                    vol_score = 0.25

            rsi_score = 0.0
            if rsi is not None:
                if rsi < lrsi:
                    rsi_score = 0.80  # deeply oversold = bullish for mean-reversion
                elif lrsi <= rsi <= lrsi + 25.0:
                    rsi_score = 1.0 - abs(rsi - (lrsi + 12.0)) / 12.0
                elif lrsi + 25.0 < rsi <= lrsi + 35.0:
                    rsi_score = 0.20
                elif rsi > lrsi + 40.0:
                    rsi_score = -1.0  # overbought, avoid buy

            # Mean reversion is favourable when recent momentum is negative.
            pnl_score = 0.0
            if predicted_pnl > 0.3:
                pnl_score = 1.0
            elif predicted_pnl > -0.5:
                pnl_score = 0.35
            else:
                pnl_score = -0.5

            avoid_buy = range_score < 0 or rsi_score < 0
            confluence = range_score + vol_score + rsi_score + pnl_score
            if not avoid_buy and confluence >= 1.0:
                side = "BUY"
                normalized = min(confluence / 3.5, 1.0)
                strength = 0.50 + normalized * 0.40
                success_probability = 0.50 + normalized * 0.25
            else:
                # SHORT side: mean-reversion from range resistance / overbought
                short_range_score = 0.0
                if range_pos is not None:
                    if range_pos >= srt + 0.30:
                        short_range_score = 1.0  # deep overbought zone
                    elif range_pos >= srt + 0.20:
                        short_range_score = 0.75
                    elif range_pos >= srt + 0.10:
                        short_range_score = 0.50
                    elif range_pos >= srt:
                        short_range_score = 0.25
                short_rsi_score = 0.0
                if rsi is not None:
                    if rsi >= srsi + 30.0:
                        short_rsi_score = 1.0  # strongly overbought
                    elif rsi >= srsi + 20.0:
                        short_rsi_score = 0.75
                    elif rsi >= srsi + 10.0:
                        short_rsi_score = 0.50
                    elif rsi >= srsi:
                        short_rsi_score = 0.25
                    elif rsi < srsi - 15.0:
                        short_rsi_score = -1.0  # oversold, don't short
                short_pnl_score = 0.0
                if predicted_pnl < -0.3:
                    short_pnl_score = 1.0
                elif predicted_pnl < 0.5:
                    short_pnl_score = 0.35
                short_confluence = short_range_score + vol_score + short_rsi_score + short_pnl_score
                avoid_short = short_range_score <= 0 or short_rsi_score < 0
                if not avoid_short and short_confluence >= 1.0:
                    side = "SELL"
                    range_score = short_range_score
                    rsi_score = short_rsi_score
                    pnl_score = short_pnl_score
                    confluence = short_confluence
                    normalized = min(short_confluence / 3.5, 1.0)
                    strength = 0.50 + normalized * 0.40
                    success_probability = 0.50 + normalized * 0.25
        else:
            # SPOT_BULL default
            lrt = thresholds["long_range_pos_threshold"]
            lrsi = thresholds["long_rsi_threshold"]

            range_score = 0.0
            if range_pos is not None:
                if lrt <= range_pos <= lrt + 0.20:
                    range_score = 1.0 - abs(range_pos - (lrt + 0.10)) / 0.10
                elif lrt - 0.05 <= range_pos < lrt:
                    range_score = 0.25
                elif range_pos >= 0.75:
                    range_score = -1.0

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
                if lrsi <= rsi <= lrsi + 20.0:
                    rsi_score = 1.0 - abs(rsi - (lrsi + 10.0)) / 10.0
                elif lrsi - 5.0 <= rsi < lrsi or lrsi + 20.0 < rsi <= lrsi + 25.0:
                    rsi_score = 0.35
                elif rsi > lrsi + 30.0:
                    rsi_score = -1.0

            pnl_score = 0.0
            if predicted_pnl > 0.5:
                pnl_score = 1.0
            elif predicted_pnl > 0.0:
                pnl_score = 0.25

            avoid_buy = range_score < 0 or rsi_score < 0 or predicted_pnl < -1.0
            confluence = range_score + vol_score + rsi_score + pnl_score
            if not avoid_buy and confluence >= 1.5:
                side = "BUY"
                normalized = confluence / 4.0
                strength = 0.50 + min(normalized * 0.45, 0.45)
                success_probability = 0.50 + min(normalized * 0.30, 0.30)
            elif _params.get("shorts_enabled", True):
                # SHORT side in SPOT_BULL: defensive shorts when overbought / top of range
                srt = thresholds["short_range_pos_threshold"]
                srsi = thresholds["short_rsi_threshold"]
                short_range_score = 0.0
                if range_pos is not None:
                    if range_pos >= srt + 0.30:
                        short_range_score = 1.0
                    elif range_pos >= srt + 0.20:
                        short_range_score = 0.75
                    elif range_pos >= srt + 0.10:
                        short_range_score = 0.50
                    elif range_pos >= srt:
                        short_range_score = 0.25
                short_rsi_score = 0.0
                if rsi is not None:
                    if rsi >= srsi + 30.0:
                        short_rsi_score = 1.0
                    elif rsi >= srsi + 20.0:
                        short_rsi_score = 0.75
                    elif rsi >= srsi + 10.0:
                        short_rsi_score = 0.50
                    elif rsi >= srsi:
                        short_rsi_score = 0.25
                    elif rsi < srsi - 15.0:
                        short_rsi_score = -1.0
                short_pnl_score = 0.0
                if predicted_pnl < -0.3:
                    short_pnl_score = 1.0
                elif predicted_pnl < 0.5:
                    short_pnl_score = 0.35
                short_confluence = short_range_score + vol_score + short_rsi_score + short_pnl_score
                avoid_short = short_range_score <= 0 or short_rsi_score < 0
                if not avoid_short and short_confluence >= 1.5:
                    side = "SELL"
                    range_score = short_range_score
                    rsi_score = short_rsi_score
                    pnl_score = short_pnl_score
                    confluence = short_confluence
                    normalized = min(short_confluence / 3.5, 1.0)
                    strength = 0.50 + normalized * 0.40
                    success_probability = 0.50 + normalized * 0.25

        if side == "SELL":
            short_term_score = confluence * max(0.0, -predicted_pnl) * success_probability
        else:
            short_term_score = (
                confluence
                * max(0.0, predicted_pnl)
                * (success_probability if side == "BUY" else 0.5)
            )
        current_price = float(bars[-1].get("close", 0)) if bars else 0.0
        atr_pct = atr / current_price if current_price > 0 else 0.0
        peak_quality_features = {
            "atr_pct": atr_pct,
            "expected_pnl": predicted_pnl,
            "short_term_score": short_term_score,
            "pnl_score": pnl_score,
            "confluence": confluence,
            "vol_score": vol_score,
            "range_score": range_score,
            "range_position": range_pos or 0.5,
        }
        peak_quality = _compute_peak_quality(
            symbol, bars, atr, predicted_pnl, short_term_score, regime, peak_quality_features
        )
        symbol_quality_score = (_SYMBOL_QUALITY_MAP.get(symbol, 0.02) - 0.02) * 2.0

        analyses.append({
            "symbol": symbol,
            "signal": side,
            "strength": min(strength, 0.95),
            "expected_pnl": -predicted_pnl if side == "SELL" else predicted_pnl,
            "success_probability": min(success_probability, 0.80),
            "liquidity_usdt_24h": max(quote_volume, 1_000_000.0),
            "vol_spike": vol_spike or 1.0,
            "rsi_4h": rsi or 50.0,
            "atr": atr,
            "range_position": range_pos or 0.5,
            "range_score": range_score,
            "vol_score": vol_score,
            "rsi_score": rsi_score,
            "pnl_score": pnl_score,
            "confluence": confluence,
            "short_term_score": short_term_score,
            "symbol_quality_score": symbol_quality_score,
            "peak_quality": peak_quality,
            "regime": regime,
        })
    # Apply entry quality gate — default vol threshold 0.70 for SPOT_BULL.
    if regime == "SPOT_BULL":
        _params = params or {}
        _min_vol_base = float(_params.get("entry_quality_vol_spike_min_spot_bull", 0.70))
        _vol_floor = float(_params.get("entry_quality_vol_spike_floor_spot_bull", 0.70))
        _breakout_min_vol = float(_params.get("entry_quality_breakout_min_vol_spot_bull", 999.0))
        _breakout_rsi_max = float(_params.get("entry_quality_breakout_rsi_max_spot_bull", 78.0))
        _breakout_min_strength = float(_params.get("entry_quality_breakout_min_strength_spot_bull", 0.58))
        if _min_vol_base > 0 or _vol_floor > 0:
            _min_vol = max(_vol_floor, _min_vol_base)
            filtered = []
            for a in analyses:
                if a["signal"] != "BUY":
                    filtered.append(a)
                    continue
                _vs = a.get("vol_spike", 1.0)
                _rsi = a.get("rsi_4h", 50.0)
                _str = a.get("strength", 0.0)
                _breakout_ok = (
                    _rsi > 68.0
                    and _rsi <= _breakout_rsi_max
                    and _vs >= _breakout_min_vol
                    and _str >= _breakout_min_strength
                )
                if _vs < _min_vol and not _breakout_ok:
                    continue  # rejected by quality gate
                filtered.append(a)
            analyses = filtered

    # Paso 4: RSI gate — reject BUY if RSI > 68 (without breakout bypass)
    if regime == "SPOT_BULL" and _params.get("gates_enhanced", True):
        gate_filtered = []
        for a in analyses:
            if a["signal"] == "BUY":
                _rsi = a.get("rsi_4h", 50.0)
                _str = a.get("strength", 0.0)
                if _rsi > 68.0 and _str < 0.75:
                    continue  # RSI overbought gate — only strong breakouts pass
            gate_filtered.append(a)
        analyses = gate_filtered

    # Paso 4: BTC trend gate — reduce BUY strength if BTC momentum is negative
    btc_bars = feed.get("BTCUSDT") or feed.get("BTC")
    if _params.get("gates_enhanced", True) and btc_bars and len(btc_bars) >= 5:
        btc_current = float(btc_bars[-1].get("close", 0))
        btc_4h_ago = float(btc_bars[-4].get("close", 0)) if len(btc_bars) >= 4 else btc_current
        btc_momentum_pct = ((btc_current - btc_4h_ago) / btc_4h_ago * 100) if btc_4h_ago > 0 else 0.0
        if btc_momentum_pct < -2.0:
            for a in analyses:
                if a["signal"] == "BUY":
                    a["strength"] = max(0.0, a["strength"] - 0.15)

    # Paso 4: Quality proxy — heuristic score for entry quality discrimination
    for a in analyses:
        _conf = a.get("confluence", 0.0)
        _vs = a.get("vol_score", 0.0)
        _rsi_val = a.get("rsi_4h", 50.0)
        _rsi_factor = max(0.0, 1.0 - abs(_rsi_val - 45.0) / 45.0)
        a["quality_proxy"] = _conf * _vs * _rsi_factor

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
        self.entry_metadata: Dict[str, Dict[str, Any]] = {}
        # Synthetic short positions: {symbol: {qty, entry_price, entry_time, ...}}
        self._short_positions: Dict[str, Dict[str, Any]] = {}

    def _bar_timestamp(self, bar: Dict[str, Any]) -> float:
        ts = bar.get("timestamp") or bar.get("open_time") or bar.get("close_time") or 0
        if isinstance(ts, (int, float)):
            return float(ts)
        if isinstance(ts, str):
            try:
                return float(ts)
            except Exception:
                return 0.0
        return 0.0

    def _peak_since_entry(
        self, symbol: str, entry_price: float, entry_ts: float
    ) -> float:
        """Return the highest unrealized PnL % for a long position since entry."""
        if entry_price <= 0:
            return 0.0
        now_ts = self.clock.now().timestamp()
        bars = self.feed.get(symbol, [])
        max_price = entry_price
        for bar in bars:
            ts = self._bar_timestamp(bar)
            if ts < entry_ts or ts > now_ts:
                continue
            # Use high for long peak PnL; close as fallback.
            high = float(bar.get("high", bar.get("close", 0)) or 0)
            if high > max_price:
                max_price = high
        if max_price <= entry_price:
            # No new highs observed; current price may still be above entry.
            current = self._current_price(symbol) or entry_price
            max_price = max(entry_price, current)
        return (max_price - entry_price) / entry_price

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
        """Return open SPOT + synthetic SHORT positions as lifecycle Positions."""
        try:
            balances = self.spot_client.get_account_balance()
        except Exception:
            balances = {}
        positions: List[Position] = []
        for asset, qty in balances.items():
            if asset == "USDT" or float(qty) <= 0:
                continue
            symbol = f"{asset}USDT"
            price = self._current_price(symbol) or 0.0
            entry_price = self._avg_entry_price(symbol) or price
            meta = self.entry_metadata.get(symbol, {})
            entry_ts = meta.get("entry_time") or self.clock.now().timestamp()
            peak_pnl_pct = self._peak_since_entry(symbol, entry_price, entry_ts)
            positions.append(Position(
                symbol=symbol,
                side=Side.LONG,
                quantity=float(qty),
                entry_price=entry_price,
                entry_time=entry_ts,
                peak_pnl_pct=peak_pnl_pct,
                position_usdt=float(qty) * price,
                category=self.category_resolver(symbol),
                regime_at_entry=config.regime,
                profile_at_entry=config.profile,
                metadata={"trade_type": "spot", **meta},
            ))
        # Synthetic short positions
        for symbol, sp in list(self._short_positions.items()):
            price = self._current_price(symbol) or 0.0
            entry_price = sp["entry_price"]
            entry_ts = sp["entry_time"]
            # For shorts: peak PnL is when price dropped the most from entry
            peak_pnl_pct = sp.get("peak_pnl_pct", 0.0)
            if price > 0 and entry_price > 0:
                current_pnl_pct = (entry_price - price) / entry_price
                if current_pnl_pct > peak_pnl_pct:
                    sp["peak_pnl_pct"] = current_pnl_pct
                    peak_pnl_pct = current_pnl_pct
            positions.append(Position(
                symbol=symbol,
                side=Side.SHORT,
                quantity=sp["qty"],
                entry_price=entry_price,
                entry_time=entry_ts,
                peak_pnl_pct=peak_pnl_pct,
                position_usdt=sp["qty"] * price,
                category=self.category_resolver(symbol),
                regime_at_entry=config.regime,
                profile_at_entry=config.profile,
                metadata={"trade_type": "futures_short", **sp.get("meta", {})},
            ))
        return positions

    def execute_entry(self, trade: SizedTrade, config: LifecycleConfig) -> Dict:
        entry = trade.entry_decision
        symbol = entry.signal.symbol
        price = self._current_price(symbol) or entry.entry_price or 0.0
        qty = trade.quantity
        expected_peak_return = entry.signal.features.get("peak_quality", entry.signal.expected_pnl / 100.0 if entry.signal.expected_pnl else 0.0)
        if entry.signal.side == Side.SHORT:
            # Synthetic short: track internally, deduct margin from USDT
            margin_usdt = qty * price
            balances = self.spot_client.get_account_balance()
            usdt_avail = float(balances.get("USDT", 0.0))
            if margin_usdt > usdt_avail:
                return {"filled": False, "price": price, "raw": {"error": "insufficient margin"}}
            self.spot_client.balances["USDT"] = usdt_avail - margin_usdt
            self._short_positions[symbol] = {
                "qty": qty,
                "entry_price": price,
                "entry_time": self.clock.now().timestamp(),
                "margin_usdt": margin_usdt,
                "peak_pnl_pct": 0.0,
                "meta": {"expected_peak_return": float(expected_peak_return) if expected_peak_return else 0.0},
            }
            self.entry_metadata[symbol] = {
                "expected_peak_return": float(expected_peak_return) if expected_peak_return is not None else 0.0,
                "entry_time": self.clock.now().timestamp(),
                "side": "SHORT",
            }
            result = {"fills": [{"qty": qty, "price": price, "commission": margin_usdt * 0.0004, "commissionAsset": "USDT"}]}
            return {"filled": True, "price": price, "raw": result}
        else:
            # LONG: use spot client as before
            result = self.spot_client.create_market_order(symbol=symbol, side="BUY", quantity=qty)
            self.entry_metadata[symbol] = {
                "expected_peak_return": float(expected_peak_return) if expected_peak_return is not None else 0.0,
                "entry_time": self.clock.now().timestamp(),
                "side": "LONG",
            }
            return {"filled": True, "price": price, "raw": result}

    def execute_exit(self, decision: ExitDecision, position: Position, config: LifecycleConfig) -> Dict:
        symbol = decision.symbol
        price = self._current_price(symbol) or 0.0
        qty = position.quantity * decision.pct
        if decision.action.value == "CLOSE_ALL":
            qty = position.quantity

        if position.side == Side.SHORT:
            # Close synthetic short: return margin + PnL to USDT
            sp = self._short_positions.get(symbol)
            if sp is None:
                return {"filled": False, "price": price, "raw": {}}
            entry_price = sp["entry_price"]
            pnl_per_unit = entry_price - price  # short profit = entry - exit
            realized_pnl = pnl_per_unit * qty
            commission = qty * price * 0.0004
            realized_pnl -= commission
            # Return margin proportional to closed qty
            margin_return = (qty / sp["qty"]) * sp["margin_usdt"]
            balances = self.spot_client.get_account_balance()
            self.spot_client.balances["USDT"] = float(balances.get("USDT", 0.0)) + margin_return + realized_pnl
            # Update or remove short position
            remaining = sp["qty"] - qty
            if remaining <= 1e-10:
                del self._short_positions[symbol]
                self.entry_metadata.pop(symbol, None)
            else:
                sp["qty"] = remaining
                sp["margin_usdt"] = sp["margin_usdt"] * (remaining / (remaining + qty))
            result = {"fills": [{"qty": qty, "price": price, "commission": commission, "commissionAsset": "USDT"}]}
            return {
                "filled": True,
                "price": price,
                "avg_exit_price": price,
                "quantity": qty,
                "realized_pnl_usdt": realized_pnl,
                "commission_usdt": commission,
                "raw": result,
            }

        # LONG exit via spot client
        result = self.spot_client.create_market_order(symbol=symbol, side="SELL", quantity=qty)

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
                "INSERT INTO trades_audit (symbol, trade_type, event, detail, ts) VALUES (%s, %s, %s, %s, %s)",
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
    peak_entry_schedule: Optional[List[Dict[str, Any]]] = None,
    params: Optional[Dict[str, Any]] = None,
    dynamic_regime: bool = False,
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
    # Allow short signals for RANGE_SCALP and SPOT_BULL (defensive shorts)
    _short_eligible = set(f"{s}" for s in symbols) if config.regime in ("RANGE_SCALP", "SPOT_BULL") else set()
    lifecycle_runner = LifecycleRunner(
        adapter=adapter,
        signal_pipeline=SignalPipeline(short_eligible=_short_eligible),
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
    peak_schedule_idx = 0
    peak_entry_schedule = peak_entry_schedule or []
    while current <= runner.end:
        runner.clock._now = current
        runner._invalidate_caches()
        runner.news_client.update_for_date(current)
        try:
            # Slice feed to current simulation time so indicators use correct data
            current_ts = current.timestamp()
            sliced_feed = _slice_feed_to_time(runner.price_feed, current_ts)
            # Dynamic regime detection: re-evaluate regime each cycle based on BTC
            if dynamic_regime and lifecycle_config is None:
                detected = _detect_regime(sliced_feed)
                if detected != config.regime:
                    config = _config_for_regime(detected)
                    # Update short eligibility for new regime
                    _short_eligible = set(f"{s}" for s in symbols) if config.regime in ("RANGE_SCALP", "SPOT_BULL") else set()
                    lifecycle_runner.signal_pipeline = SignalPipeline(short_eligible=_short_eligible)
            analyses = _build_analyses(runner.symbols, sliced_feed, regime=config.regime, params=params)
            # Inyectar señales peak cuyo peak_start_ts cae en este ciclo o antes
            while (
                peak_schedule_idx < len(peak_entry_schedule)
                and peak_entry_schedule[peak_schedule_idx]["peak_start_dt"] <= current
            ):
                analyses.append(peak_entry_schedule[peak_schedule_idx])
                peak_schedule_idx += 1
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
    """Profile A: broad favourite — lower signal thresholds, many positions, moderate Kelly."""
    from lifecycle import LifecycleConfig, SignalPolicy, EntryPolicy, SizingPolicy, ExitPolicy, PartialTake, MomentumExitRule, StagnationRule
    return LifecycleConfig(
        regime="SPOT_BULL",
        profile="A",
        signal_policy=SignalPolicy(
            min_signal_strength=0.45,
            min_success_probability=0.50,
            min_expected_pnl=0.0,
            min_liquidity_usdt=0.0,
            categories_enabled=("LARGE_CAP", "MIDCAP_ALT", "NARRATIVE"),
            max_candidates=10,
            diversification_mode="category",
        ),
        entry_policy=EntryPolicy(
            min_composite_score=0.45,
            min_expected_pnl=0.0,
            min_success_probability=0.50,
            min_liquidity_usdt=0.0,
            max_total_positions=12,
            max_category_positions={"LARGE_CAP": 2, "MIDCAP_ALT": 4, "NARRATIVE": 2},
        ),
        sizing_policy=SizingPolicy(
            regime_allocation_pct=0.8,
            max_per_trade_usdt=1500.0,
            kelly_base_pct=0.18,
            kelly_max_pct=0.22,
            conviction_max_pct=0.22,
            category_caps={"LARGE_CAP": 0.25, "MIDCAP_ALT": 0.35, "NARRATIVE": 0.15},
        ),
        exit_policy=ExitPolicy(
            regime="SPOT_BULL",
            profile="A",
            hard_sl_atr_mult=5.5,
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
    """Profile B: B-rescue — higher signal thresholds, fewer positions, larger Kelly."""
    from lifecycle import LifecycleConfig, SignalPolicy, EntryPolicy, SizingPolicy, ExitPolicy, PartialTake, MomentumExitRule, StagnationRule
    return LifecycleConfig(
        regime="SPOT_BULL",
        profile="B",
        signal_policy=SignalPolicy(
            min_signal_strength=0.60,
            min_success_probability=0.62,
            min_expected_pnl=0.0,
            min_liquidity_usdt=0.0,
            categories_enabled=("LARGE_CAP", "MIDCAP_ALT", "NARRATIVE"),
            max_candidates=6,
            diversification_mode="category",
        ),
        entry_policy=EntryPolicy(
            min_composite_score=0.60,
            min_expected_pnl=0.0,
            min_success_probability=0.62,
            min_liquidity_usdt=0.0,
            max_total_positions=6,
            max_category_positions={"LARGE_CAP": 2, "MIDCAP_ALT": 2, "NARRATIVE": 1},
        ),
        sizing_policy=SizingPolicy(
            regime_allocation_pct=0.8,
            max_per_trade_usdt=2500.0,
            kelly_base_pct=0.22,
            kelly_max_pct=0.30,
            conviction_max_pct=0.30,
            category_caps={"LARGE_CAP": 0.25, "MIDCAP_ALT": 0.40, "NARRATIVE": 0.15},
        ),
        exit_policy=ExitPolicy(
            regime="SPOT_BULL",
            profile="B",
            hard_sl_atr_mult=5.5,
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
    """Profile C: C-rescue — ultra restrictive, only the highest-certainty setups, largest Kelly."""
    from lifecycle import LifecycleConfig, SignalPolicy, EntryPolicy, SizingPolicy, ExitPolicy, PartialTake, MomentumExitRule, StagnationRule
    return LifecycleConfig(
        regime="SPOT_BULL",
        profile="C",
        signal_policy=SignalPolicy(
            min_signal_strength=0.70,
            min_success_probability=0.70,
            min_expected_pnl=0.0,
            min_liquidity_usdt=0.0,
            categories_enabled=("LARGE_CAP", "MIDCAP_ALT", "NARRATIVE"),
            max_candidates=4,
            diversification_mode="category",
        ),
        entry_policy=EntryPolicy(
            min_composite_score=0.70,
            min_expected_pnl=0.0,
            min_success_probability=0.70,
            min_liquidity_usdt=0.0,
            max_total_positions=3,
            max_category_positions={"LARGE_CAP": 1, "MIDCAP_ALT": 1, "NARRATIVE": 1},
        ),
        sizing_policy=SizingPolicy(
            regime_allocation_pct=0.8,
            max_per_trade_usdt=3500.0,
            kelly_base_pct=0.28,
            kelly_max_pct=0.40,
            conviction_max_pct=0.40,
            category_caps={"LARGE_CAP": 0.35, "MIDCAP_ALT": 0.35, "NARRATIVE": 0.15},
        ),
        exit_policy=ExitPolicy(
            regime="SPOT_BULL",
            profile="C",
            hard_sl_atr_mult=5.5,
            hard_tp_atr_mult=3.5,
            trailing_activation_pct=0.02,
            trailing_atr_mult=1.5,
            breakeven_activation_pct=0.015,
            partial_takes=(PartialTake(0.25, 1.5, "tp1"),),
            momentum_exit=MomentumExitRule(70, 30, 80, 20),
            stagnation_rule=StagnationRule(6, 0.05, 0.5),
        ),
    )


def _preservation_config() -> LifecycleConfig:
    """PRESERVATION: no new entries, manage existing positions with wide SL."""
    from lifecycle import LifecycleConfig, SignalPolicy, EntryPolicy, SizingPolicy, ExitPolicy, PartialTake, MomentumExitRule, StagnationRule
    return LifecycleConfig(
        regime="PRESERVATION",
        profile="A",
        signal_policy=SignalPolicy(
            min_signal_strength=0.99,
            min_success_probability=0.99,
            min_expected_pnl=99.0,
            min_liquidity_usdt=0.0,
            categories_enabled=("LARGE_CAP", "MIDCAP_ALT", "NARRATIVE"),
            max_candidates=0,
            diversification_mode="category",
        ),
        entry_policy=EntryPolicy(
            min_composite_score=0.99,
            min_expected_pnl=99.0,
            min_success_probability=0.99,
            min_liquidity_usdt=0.0,
            max_total_positions=0,
            max_category_positions={"LARGE_CAP": 0, "MIDCAP_ALT": 0, "NARRATIVE": 0},
        ),
        sizing_policy=SizingPolicy(
            regime_allocation_pct=0.0,
            max_per_trade_usdt=0.0,
            kelly_base_pct=0.0,
            kelly_max_pct=0.0,
            conviction_max_pct=0.0,
            category_caps={"LARGE_CAP": 0.0, "MIDCAP_ALT": 0.0, "NARRATIVE": 0.0},
        ),
        exit_policy=ExitPolicy(
            regime="PRESERVATION",
            profile="A",
            hard_sl_atr_mult=8.0,
            hard_tp_atr_mult=2.0,
            trailing_activation_pct=0.01,
            trailing_atr_mult=1.0,
            breakeven_activation_pct=0.01,
            partial_takes=(PartialTake(0.50, 1.0, "tp1"),),
            momentum_exit=MomentumExitRule(65, 35, 75, 25),
            stagnation_rule=StagnationRule(4, 0.03, 0.4),
        ),
    )


def _range_scalp_config() -> LifecycleConfig:
    """RANGE_SCALP: mean-reversion with both LONG and SHORT signals."""
    from lifecycle import LifecycleConfig, SignalPolicy, EntryPolicy, SizingPolicy, ExitPolicy, PartialTake, MomentumExitRule, StagnationRule
    return LifecycleConfig(
        regime="RANGE_SCALP",
        profile="A",
        signal_policy=SignalPolicy(
            min_signal_strength=0.50,
            min_success_probability=0.50,
            min_expected_pnl=0.0,
            min_liquidity_usdt=0.0,
            categories_enabled=("LARGE_CAP", "MIDCAP_ALT", "NARRATIVE"),
            max_candidates=8,
            diversification_mode="category",
        ),
        entry_policy=EntryPolicy(
            min_composite_score=0.50,
            min_expected_pnl=0.0,
            min_success_probability=0.50,
            min_liquidity_usdt=0.0,
            max_total_positions=6,
            max_category_positions={"LARGE_CAP": 2, "MIDCAP_ALT": 3, "NARRATIVE": 1},
        ),
        sizing_policy=SizingPolicy(
            regime_allocation_pct=0.6,
            max_per_trade_usdt=1200.0,
            kelly_base_pct=0.15,
            kelly_max_pct=0.20,
            conviction_max_pct=0.20,
            category_caps={"LARGE_CAP": 0.25, "MIDCAP_ALT": 0.35, "NARRATIVE": 0.15},
        ),
        exit_policy=ExitPolicy(
            regime="RANGE_SCALP",
            profile="A",
            hard_sl_atr_mult=4.0,
            hard_tp_atr_mult=2.5,
            trailing_activation_pct=0.015,
            trailing_atr_mult=1.0,
            breakeven_activation_pct=0.01,
            partial_takes=(PartialTake(0.30, 1.2, "tp1"),),
            momentum_exit=MomentumExitRule(68, 32, 78, 22),
            stagnation_rule=StagnationRule(4, 0.04, 0.5),
        ),
    )


def _config_for_regime(regime: str) -> LifecycleConfig:
    """Return the appropriate LifecycleConfig for the detected regime."""
    if regime == "PRESERVATION":
        return _preservation_config()
    if regime == "RANGE_SCALP":
        return _range_scalp_config()
    return _spot_bull_config("A")


def spot_bull_config_from_params(params: Dict[str, Any], profile: str = "A") -> LifecycleConfig:
    """Build a SPOT_BULL lifecycle config from a flat parameter dict for sweeping.

    Recognised keys:
      - min_signal_strength, min_success_probability, max_candidates
      - min_composite_score, max_total_positions
      - max_per_trade_usdt, kelly_base_pct, kelly_max_pct, conviction_max_pct
      - hard_sl_atr_mult, hard_tp_atr_mult
      - trailing_activation_pct, trailing_atr_mult
      - breakeven_activation_pct
      - partial_take_N_pct, partial_take_N_trigger_atr (N = 1, 2, ...)
      - stagnation_min_hours, stagnation_max_drawdown_peak
      - momentum_rsi_overbought, momentum_rsi_oversold
      - time_stop_hours
    """
    from lifecycle import LifecycleConfig, SignalPolicy, EntryPolicy, SizingPolicy, ExitPolicy, PartialTake, MomentumExitRule, StagnationRule
    _cats = ("LARGE_CAP", "MIDCAP_ALT", "NARRATIVE")
    _max_total = int(params.get("max_total_positions", 5))
    _large = max(1, _max_total // 4)
    _mid = max(1, _max_total // 2)
    _narr = max(1, _max_total - _large - _mid)

    # Build partial takes from flat keys if present, otherwise keep one default level.
    partial_takes = []
    for i in range(1, 5):
        pct_key = f"partial_take_{i}_pct"
        trig_key = f"partial_take_{i}_trigger_atr"
        if pct_key in params and trig_key in params:
            pct = float(params[pct_key])
            if pct > 0.0:
                partial_takes.append(PartialTake(pct, float(params[trig_key]), f"tp{i}"))
    if not partial_takes:
        partial_takes = [PartialTake(0.25, 1.5, "tp1")]
    else:
        # Trailing stop uses the highest partial trigger for its reference.
        partial_takes.sort(key=lambda pt: pt.trigger_atr_mult)

    time_stop = params.get("time_stop_hours")
    time_stop_hours = float(time_stop) if time_stop is not None else None

    momentum = MomentumExitRule(
        rsi_overbought=float(params.get("momentum_rsi_overbought", 70)),
        rsi_oversold=float(params.get("momentum_rsi_oversold", 30)),
        fng_extreme_greed=float(params.get("momentum_fng_extreme_greed", 80)),
        fng_extreme_fear=float(params.get("momentum_fng_extreme_fear", 20)),
    )
    stagnation = StagnationRule(
        min_hours=float(params.get("stagnation_min_hours", 6)),
        max_drawdown_from_peak_pct=float(params.get("stagnation_max_drawdown_peak", 0.05)),
        decay_score_threshold=float(params.get("stagnation_decay_score_threshold", 0.5)),
    )

    return LifecycleConfig(
        regime="SPOT_BULL",
        profile=profile,
        signal_policy=SignalPolicy(
            min_signal_strength=float(params.get("min_signal_strength", 0.55)),
            min_success_probability=float(params.get("min_success_probability", 0.55)),
            min_expected_pnl=0.0,
            min_liquidity_usdt=0.0,
            categories_enabled=_cats,
            max_candidates=int(params.get("max_candidates", 5)),
            diversification_mode="category",
        ),
        entry_policy=EntryPolicy(
            min_composite_score=float(params.get("min_composite_score", params.get("min_signal_strength", 0.55))),
            min_expected_pnl=0.0,
            min_success_probability=float(params.get("min_success_probability", 0.55)),
            min_liquidity_usdt=0.0,
            max_total_positions=_max_total,
            max_category_positions={"LARGE_CAP": _large, "MIDCAP_ALT": _mid, "NARRATIVE": _narr},
        ),
        sizing_policy=SizingPolicy(
            regime_allocation_pct=0.8,
            max_per_trade_usdt=float(params.get("max_per_trade_usdt", 1000.0)),
            kelly_base_pct=float(params.get("kelly_base_pct", 0.18)),
            kelly_max_pct=float(params.get("kelly_max_pct", 0.20)),
            conviction_max_pct=float(params.get("conviction_max_pct", 0.20)),
            category_caps={"LARGE_CAP": 0.30, "MIDCAP_ALT": 0.35, "NARRATIVE": 0.15},
        ),
        exit_policy=ExitPolicy(
            regime="SPOT_BULL",
            profile=profile,
            hard_sl_atr_mult=float(params.get("hard_sl_atr_mult", 5.5)),
            hard_tp_atr_mult=float(params.get("hard_tp_atr_mult", 3.5)),
            trailing_activation_pct=float(params.get("trailing_activation_pct", 0.02)),
            trailing_atr_mult=float(params.get("trailing_atr_mult", 1.5)),
            breakeven_activation_pct=float(params.get("breakeven_activation_pct", 0.015)),
            partial_takes=tuple(partial_takes),
            momentum_exit=momentum,
            stagnation_rule=stagnation,
            time_stop_hours=time_stop_hours,
        ),
    )


def predictive_spot_bull_config_from_params(
    params: Dict[str, Any], profile: str = "PREDICTIVE"
) -> LifecycleConfig:
    """Build a SPOT_BULL config with per-position profile assignment enabled.

    Flat params can override any profile threshold prefixed by the profile key, e.g.
    ``A_min_composite_score`` or ``B_kelly_max_pct``.  Defaults come from
    `predictive_spot_bull_config`.
    """
    from lifecycle import (
        LifecycleConfig,
        SignalPolicy,
        EntryPolicy,
        SizingPolicy,
        ExitPolicy,
        PartialTake,
        MomentumExitRule,
        StagnationRule,
        ProfileThresholds,
    )

    base = predictive_spot_bull_config()

    def _p(prof: str, key: str, default):
        return type(default)(params.get(f"{prof}_{key}", default))

    thresholds = {}
    for prof in ("A", "B", "C"):
        t = base.profile_thresholds[prof]
        thresholds[prof] = ProfileThresholds(
            min_composite_score=_p(prof, "min_composite_score", t.min_composite_score),
            min_success_probability=_p(prof, "min_success_probability", t.min_success_probability),
            min_signal_strength=_p(prof, "min_signal_strength", t.min_signal_strength),
            min_expected_pnl=_p(prof, "min_expected_pnl", t.min_expected_pnl),
            max_rsi_4h=_p(prof, "max_rsi_4h", t.max_rsi_4h),
            min_vol_spike=_p(prof, "min_vol_spike", t.min_vol_spike),
            min_range_score=_p(prof, "min_range_score", t.min_range_score),
            min_confluence=_p(prof, "min_confluence", t.min_confluence),
            min_short_term_score=_p(prof, "min_short_term_score", t.min_short_term_score),
            min_peak_quality=_p(prof, "min_peak_quality", t.min_peak_quality),
        )

    sizing = {}
    for prof in ("A", "B", "C"):
        s = base.profile_sizing_policies[prof]
        sizing[prof] = SizingPolicy(
            regime_allocation_pct=_p(prof, "regime_allocation_pct", s.regime_allocation_pct),
            max_per_trade_usdt=_p(prof, "max_per_trade_usdt", s.max_per_trade_usdt),
            kelly_base_pct=_p(prof, "kelly_base_pct", s.kelly_base_pct),
            kelly_max_pct=_p(prof, "kelly_max_pct", s.kelly_max_pct),
            conviction_max_pct=_p(prof, "conviction_max_pct", s.conviction_max_pct),
            category_caps={
                "LARGE_CAP": _p(prof, "cap_large", s.category_caps.get("LARGE_CAP", 0.25)),
                "MIDCAP_ALT": _p(prof, "cap_mid", s.category_caps.get("MIDCAP_ALT", 0.35)),
                "NARRATIVE": _p(prof, "cap_narr", s.category_caps.get("NARRATIVE", 0.15)),
            },
            max_total_positions=int(params.get(f"{prof}_max_total_positions", s.max_total_positions)),
            fng_capital_multiplier=True,
        )

    signal = base.signal_policy
    entry = base.entry_policy
    exitp = base.exit_policy

    return LifecycleConfig(
        regime="SPOT_BULL",
        profile=profile,
        signal_policy=SignalPolicy(
            min_signal_strength=float(params.get("min_signal_strength", signal.min_signal_strength)),
            min_success_probability=float(params.get("min_success_probability", signal.min_success_probability)),
            min_expected_pnl=float(params.get("min_expected_pnl", signal.min_expected_pnl)),
            min_liquidity_usdt=0.0,
            categories_enabled=("LARGE_CAP", "MIDCAP_ALT", "NARRATIVE"),
            max_candidates=int(params.get("max_candidates", signal.max_candidates)),
            diversification_mode="category",
        ),
        entry_policy=EntryPolicy(
            min_composite_score=float(params.get("min_composite_score", entry.min_composite_score)),
            min_expected_pnl=float(params.get("min_expected_pnl", entry.min_expected_pnl)),
            min_success_probability=float(params.get("min_success_probability", entry.min_success_probability)),
            min_liquidity_usdt=0.0,
            max_total_positions=int(params.get("max_total_positions", entry.max_total_positions)),
            max_category_positions=dict(entry.max_category_positions),
        ),
        sizing_policy=base.sizing_policy,
        exit_policy=_build_exit_policy_from_params(params, exitp, profile),
        profile_order=("A", "B", "C"),
        profile_thresholds=thresholds,
        profile_sizing_policies=sizing,
        # Use sweep params for ALL profiles so the sweep actually tests them.
        profile_exit_policies={
            prof: _build_exit_policy_from_params(params, exitp, prof)
            for prof in ("A", "B", "C")
        },
    )


def predictive_spot_bull_config() -> LifecycleConfig:
    """Predictive A/B/C per-position profile assignment for SPOT_BULL.

    Profile assignment is driven primarily by the `peak_quality` score, which
    estimates the magnitude of the next 12h price peak.  A = ultra-Kelly and
    fastest exits; B = super-Kelly; C = aggressive-Kelly and wider exits.
    """
    from lifecycle import (
        LifecycleConfig,
        SignalPolicy,
        EntryPolicy,
        SizingPolicy,
        ExitPolicy,
        PartialTake,
        MomentumExitRule,
        StagnationRule,
        ProfileThresholds,
    )

    _cats = ("LARGE_CAP", "MIDCAP_ALT", "NARRATIVE")

    signal_policy = SignalPolicy(
        min_signal_strength=0.50,
        min_success_probability=0.50,
        min_expected_pnl=-0.5,
        min_liquidity_usdt=0.0,
        categories_enabled=_cats,
        max_candidates=20,
        diversification_mode="category",
    )

    entry_policy = EntryPolicy(
        min_composite_score=0.55,
        min_expected_pnl=-0.5,
        min_success_probability=0.50,
        min_liquidity_usdt=0.0,
        max_total_positions=14,
        max_category_positions={"LARGE_CAP": 4, "MIDCAP_ALT": 6, "NARRATIVE": 2},
    )

    # Default fallback exit policy (used when no per-profile policy matches).
    exit_policy = ExitPolicy(
        regime="SPOT_BULL",
        profile="PREDICTIVE",
        hard_sl_atr_mult=2.5,
        hard_tp_atr_mult=3.0,
        trailing_activation_pct=0.02,
        trailing_atr_mult=1.5,
        breakeven_activation_pct=0.015,
        partial_takes=(PartialTake(0.25, 1.5, "tp1"),),
        momentum_exit=MomentumExitRule(70, 30, 80, 20),
        stagnation_rule=StagnationRule(6, 0.05, 0.5),
    )

    # A = top ~10-15% peak-quality candidates, ultra-aggressive Kelly.
    thresholds_a = ProfileThresholds(
        min_composite_score=0.55,
        min_success_probability=0.50,
        min_signal_strength=0.50,
        min_expected_pnl=-0.5,
        max_rsi_4h=70.0,
        min_vol_spike=0.0,
        min_range_score=0.0,
        min_confluence=0.0,
        min_short_term_score=0.0,
        min_peak_quality=0.20,
    )
    sizing_a = SizingPolicy(
        regime_allocation_pct=0.8,
        max_per_trade_usdt=2500.0,
        kelly_base_pct=0.50,
        kelly_max_pct=0.65,
        conviction_max_pct=0.65,
        category_caps={"LARGE_CAP": 0.50, "MIDCAP_ALT": 0.50, "NARRATIVE": 0.20},
        max_total_positions=2,
        fng_capital_multiplier=True,
    )
    exit_a = ExitPolicy(
        regime="SPOT_BULL",
        profile="A",
        hard_sl_atr_mult=1.5,
        hard_tp_atr_mult=2.0,
        trailing_activation_pct=0.01,
        trailing_atr_mult=0.5,
        breakeven_activation_pct=0.005,
        partial_takes=(),
        momentum_exit=MomentumExitRule(70, 30, 80, 20),
        stagnation_rule=StagnationRule(4, 0.03, 0.5),
    )

    # B = moderate peak-quality candidates, super Kelly.
    thresholds_b = ProfileThresholds(
        min_composite_score=0.55,
        min_success_probability=0.50,
        min_signal_strength=0.50,
        min_expected_pnl=-0.5,
        max_rsi_4h=70.0,
        min_vol_spike=0.0,
        min_range_score=0.0,
        min_confluence=0.0,
        min_short_term_score=0.0,
        min_peak_quality=-0.30,
    )
    sizing_b = SizingPolicy(
        regime_allocation_pct=0.8,
        max_per_trade_usdt=1200.0,
        kelly_base_pct=0.30,
        kelly_max_pct=0.45,
        conviction_max_pct=0.45,
        category_caps={"LARGE_CAP": 0.40, "MIDCAP_ALT": 0.40, "NARRATIVE": 0.20},
        max_total_positions=4,
        fng_capital_multiplier=True,
    )
    exit_b = ExitPolicy(
        regime="SPOT_BULL",
        profile="B",
        hard_sl_atr_mult=2.0,
        hard_tp_atr_mult=2.5,
        trailing_activation_pct=0.015,
        trailing_atr_mult=1.0,
        breakeven_activation_pct=0.01,
        partial_takes=(PartialTake(0.25, 1.5, "tp1"),),
        momentum_exit=MomentumExitRule(70, 30, 80, 20),
        stagnation_rule=StagnationRule(6, 0.04, 0.5),
    )

    # C = small peak candidates, aggressive Kelly, wider exits.
    thresholds_c = ProfileThresholds(
        min_composite_score=0.55,
        min_success_probability=0.50,
        min_signal_strength=0.50,
        min_expected_pnl=-0.5,
        max_rsi_4h=70.0,
        min_vol_spike=0.0,
        min_range_score=0.0,
        min_confluence=0.0,
        min_short_term_score=0.0,
        min_peak_quality=-0.80,
    )
    sizing_c = SizingPolicy(
        regime_allocation_pct=0.8,
        max_per_trade_usdt=600.0,
        kelly_base_pct=0.18,
        kelly_max_pct=0.28,
        conviction_max_pct=0.28,
        category_caps={"LARGE_CAP": 0.35, "MIDCAP_ALT": 0.35, "NARRATIVE": 0.15},
        max_total_positions=8,
        fng_capital_multiplier=True,
    )
    exit_c = ExitPolicy(
        regime="SPOT_BULL",
        profile="C",
        hard_sl_atr_mult=2.5,
        hard_tp_atr_mult=3.0,
        trailing_activation_pct=0.02,
        trailing_atr_mult=1.5,
        breakeven_activation_pct=0.015,
        partial_takes=(PartialTake(0.25, 1.5, "tp1"),),
        momentum_exit=MomentumExitRule(70, 30, 80, 20),
        stagnation_rule=StagnationRule(8, 0.05, 0.5),
    )

    return LifecycleConfig(
        regime="SPOT_BULL",
        profile="PREDICTIVE",
        signal_policy=signal_policy,
        entry_policy=entry_policy,
        sizing_policy=SizingPolicy(
            regime_allocation_pct=0.8,
            max_per_trade_usdt=1200.0,
            kelly_base_pct=0.18,
            kelly_max_pct=0.28,
            conviction_max_pct=0.28,
            category_caps={"LARGE_CAP": 0.35, "MIDCAP_ALT": 0.35, "NARRATIVE": 0.15},
            max_total_positions=8,
        ),
        exit_policy=exit_policy,
        profile_order=("A", "B", "C"),
        profile_thresholds={"A": thresholds_a, "B": thresholds_b, "C": thresholds_c},
        profile_sizing_policies={"A": sizing_a, "B": sizing_b, "C": sizing_c},
        profile_exit_policies={"A": exit_a, "B": exit_b, "C": exit_c},
    )


def _build_exit_policy_from_params(params: Dict[str, Any], base_exit, profile: str):
    """Build an ExitPolicy reading ALL sweep params (trailing, breakeven, partials, etc.).

    Falls back to `base_exit` for any key not present in `params`.
    """
    from lifecycle import ExitPolicy, PartialTake, MomentumExitRule, StagnationRule

    # Partial takes from flat keys (partial_take_N_pct / partial_take_N_trigger_atr)
    partial_takes = []
    for i in range(1, 5):
        pct_key = f"partial_take_{i}_pct"
        trig_key = f"partial_take_{i}_trigger_atr"
        if pct_key in params and trig_key in params:
            pct = float(params[pct_key])
            if pct > 0.0:
                partial_takes.append(PartialTake(pct, float(params[trig_key]), f"tp{i}"))
    if not partial_takes:
        # Check if any partial_take key was in params but all had pct=0 (intentional disable)
        has_partial_keys = any(f"partial_take_{i}_pct" in params for i in range(1, 5))
        if not has_partial_keys:
            partial_takes = list(base_exit.partial_takes) if base_exit.partial_takes else []
        # else: intentionally empty (all partials disabled)
    else:
        partial_takes.sort(key=lambda pt: pt.trigger_atr_mult)

    # Momentum exit
    momentum = MomentumExitRule(
        rsi_overbought=float(params.get("momentum_rsi_overbought", base_exit.momentum_exit.rsi_overbought if base_exit.momentum_exit else 70)),
        rsi_oversold=float(params.get("momentum_rsi_oversold", base_exit.momentum_exit.rsi_oversold if base_exit.momentum_exit else 30)),
        fng_extreme_greed=float(params.get("momentum_fng_extreme_greed", base_exit.momentum_exit.fng_extreme_greed if base_exit.momentum_exit else 80)),
        fng_extreme_fear=float(params.get("momentum_fng_extreme_fear", base_exit.momentum_exit.fng_extreme_fear if base_exit.momentum_exit else 20)),
    )

    # Stagnation rule
    stagnation = StagnationRule(
        min_hours=float(params.get("stagnation_min_hours", base_exit.stagnation_rule.min_hours if base_exit.stagnation_rule else 6)),
        max_drawdown_from_peak_pct=float(params.get("stagnation_max_drawdown_peak", base_exit.stagnation_rule.max_drawdown_from_peak_pct if base_exit.stagnation_rule else 0.05)),
        decay_score_threshold=float(params.get("stagnation_decay_score_threshold", base_exit.stagnation_rule.decay_score_threshold if base_exit.stagnation_rule else 0.5)),
    )

    # Time stop
    time_stop = params.get("time_stop_hours")
    time_stop_hours = float(time_stop) if time_stop is not None else getattr(base_exit, "time_stop_hours", None)

    return ExitPolicy(
        regime=getattr(base_exit, "regime", "RANGE_SCALP"),
        profile=profile,
        hard_sl_atr_mult=float(params.get("hard_sl_atr_mult", base_exit.hard_sl_atr_mult)),
        hard_tp_atr_mult=float(params.get("hard_tp_atr_mult", base_exit.hard_tp_atr_mult)),
        trailing_activation_pct=float(params.get("trailing_activation_pct", base_exit.trailing_activation_pct)),
        trailing_atr_mult=float(params.get("trailing_atr_mult", base_exit.trailing_atr_mult)),
        breakeven_activation_pct=float(params.get("breakeven_activation_pct", base_exit.breakeven_activation_pct)),
        partial_takes=tuple(partial_takes),
        momentum_exit=momentum,
        stagnation_rule=stagnation,
        time_stop_hours=time_stop_hours,
    )


def predictive_range_scalp_config_from_params(
    params: Dict[str, Any], profile: str = "PREDICTIVE"
) -> LifecycleConfig:
    """Build a RANGE_SCALP predictive config from a flat parameter dict."""
    from lifecycle import (
        LifecycleConfig,
        SignalPolicy,
        EntryPolicy,
        SizingPolicy,
        ExitPolicy,
        PartialTake,
        MomentumExitRule,
        StagnationRule,
        ProfileThresholds,
    )

    base = predictive_range_scalp_config()

    def _p(prof: str, key: str, default):
        return type(default)(params.get(f"{prof}_{key}", default))

    thresholds = {}
    for prof in ("A", "B", "C"):
        t = base.profile_thresholds[prof]
        thresholds[prof] = ProfileThresholds(
            min_composite_score=_p(prof, "min_composite_score", t.min_composite_score),
            min_success_probability=_p(prof, "min_success_probability", t.min_success_probability),
            min_signal_strength=_p(prof, "min_signal_strength", t.min_signal_strength),
            min_expected_pnl=_p(prof, "min_expected_pnl", t.min_expected_pnl),
            max_rsi_4h=_p(prof, "max_rsi_4h", t.max_rsi_4h),
            min_vol_spike=_p(prof, "min_vol_spike", t.min_vol_spike),
            min_range_score=_p(prof, "min_range_score", t.min_range_score),
            min_confluence=_p(prof, "min_confluence", t.min_confluence),
            min_short_term_score=_p(prof, "min_short_term_score", t.min_short_term_score),
            min_peak_quality=_p(prof, "min_peak_quality", t.min_peak_quality),
        )

    sizing = {}
    for prof in ("A", "B", "C"):
        s = base.profile_sizing_policies[prof]
        sizing[prof] = SizingPolicy(
            regime_allocation_pct=_p(prof, "regime_allocation_pct", s.regime_allocation_pct),
            max_per_trade_usdt=_p(prof, "max_per_trade_usdt", s.max_per_trade_usdt),
            kelly_base_pct=_p(prof, "kelly_base_pct", s.kelly_base_pct),
            kelly_max_pct=_p(prof, "kelly_max_pct", s.kelly_max_pct),
            conviction_max_pct=_p(prof, "conviction_max_pct", s.conviction_max_pct),
            category_caps={
                "LARGE_CAP": _p(prof, "cap_large", s.category_caps.get("LARGE_CAP", 0.25)),
                "MIDCAP_ALT": _p(prof, "cap_mid", s.category_caps.get("MIDCAP_ALT", 0.35)),
                "NARRATIVE": _p(prof, "cap_narr", s.category_caps.get("NARRATIVE", 0.15)),
            },
            max_total_positions=int(params.get(f"{prof}_max_total_positions", s.max_total_positions)),
            fng_capital_multiplier=True,
        )

    signal = base.signal_policy
    entry = base.entry_policy
    exitp = base.exit_policy

    return LifecycleConfig(
        regime="RANGE_SCALP",
        profile=profile,
        signal_policy=SignalPolicy(
            min_signal_strength=float(params.get("min_signal_strength", signal.min_signal_strength)),
            min_success_probability=float(params.get("min_success_probability", signal.min_success_probability)),
            min_expected_pnl=float(params.get("min_expected_pnl", signal.min_expected_pnl)),
            min_liquidity_usdt=0.0,
            categories_enabled=("LARGE_CAP", "MIDCAP_ALT", "NARRATIVE"),
            max_candidates=int(params.get("max_candidates", signal.max_candidates)),
            diversification_mode="category",
        ),
        entry_policy=EntryPolicy(
            min_composite_score=float(params.get("min_composite_score", entry.min_composite_score)),
            min_expected_pnl=float(params.get("min_expected_pnl", entry.min_expected_pnl)),
            min_success_probability=float(params.get("min_success_probability", entry.min_success_probability)),
            min_liquidity_usdt=0.0,
            max_total_positions=int(params.get("max_total_positions", entry.max_total_positions)),
            max_category_positions=dict(entry.max_category_positions),
        ),
        sizing_policy=SizingPolicy(
            regime_allocation_pct=0.8,
            max_per_trade_usdt=1200.0,
            kelly_base_pct=0.18,
            kelly_max_pct=0.28,
            conviction_max_pct=0.28,
            category_caps={"LARGE_CAP": 0.35, "MIDCAP_ALT": 0.35, "NARRATIVE": 0.15},
            max_total_positions=8,
        ),
        exit_policy=_build_exit_policy_from_params(params, exitp, profile),
        profile_order=("A", "B", "C"),
        profile_thresholds=thresholds,
        profile_sizing_policies=sizing,
        # Use sweep params for ALL profiles so the sweep actually tests them.
        # _load_best_exit_policies returns static policies from disk — useless for sweeps.
        profile_exit_policies={
            prof: _build_exit_policy_from_params(params, exitp, prof)
            for prof in ("A", "B", "C")
        },
    )


def predictive_range_scalp_config() -> LifecycleConfig:
    """Predictive A/B/C per-position profile assignment for RANGE_SCALP.

    Profile assignment is driven primarily by the `peak_quality` score.  A gets
    the tightest exits, C the widest; sizing follows the same hierarchy.
    """
    from lifecycle import (
        LifecycleConfig,
        SignalPolicy,
        EntryPolicy,
        SizingPolicy,
        ExitPolicy,
        PartialTake,
        MomentumExitRule,
        StagnationRule,
        ProfileThresholds,
    )

    _cats = ("LARGE_CAP", "MIDCAP_ALT", "NARRATIVE")

    signal_policy = SignalPolicy(
        min_signal_strength=0.50,
        min_success_probability=0.50,
        min_expected_pnl=-0.5,
        min_liquidity_usdt=0.0,
        categories_enabled=_cats,
        max_candidates=20,
        diversification_mode="category",
    )

    entry_policy = EntryPolicy(
        min_composite_score=0.55,
        min_expected_pnl=-0.5,
        min_success_probability=0.50,
        min_liquidity_usdt=0.0,
        max_total_positions=14,
        max_category_positions={"LARGE_CAP": 4, "MIDCAP_ALT": 6, "NARRATIVE": 2},
    )

    exit_policy = ExitPolicy(
        regime="RANGE_SCALP",
        profile="PREDICTIVE",
        hard_sl_atr_mult=2.5,
        hard_tp_atr_mult=2.0,
        trailing_activation_pct=0.01,
        trailing_atr_mult=1.0,
        breakeven_activation_pct=0.01,
        partial_takes=(PartialTake(0.35, 1.0, "tp1"),),
        momentum_exit=MomentumExitRule(70, 30, 80, 20),
        stagnation_rule=StagnationRule(4, 0.04, 0.5),
    )

    # A = top ~10-15% peak-quality candidates, ultra-aggressive Kelly.
    thresholds_a = ProfileThresholds(
        min_composite_score=0.55,
        min_success_probability=0.50,
        min_signal_strength=0.50,
        min_expected_pnl=-0.5,
        max_rsi_4h=70.0,
        min_vol_spike=0.0,
        min_range_score=0.0,
        min_confluence=0.0,
        min_short_term_score=0.0,
        min_peak_quality=0.15,
    )
    sizing_a = SizingPolicy(
        regime_allocation_pct=0.8,
        max_per_trade_usdt=2500.0,
        kelly_base_pct=0.50,
        kelly_max_pct=0.65,
        conviction_max_pct=0.65,
        category_caps={"LARGE_CAP": 0.50, "MIDCAP_ALT": 0.50, "NARRATIVE": 0.20},
        max_total_positions=2,
        fng_capital_multiplier=True,
    )
    exit_a = ExitPolicy(
        regime="RANGE_SCALP",
        profile="A",
        hard_sl_atr_mult=1.5,
        hard_tp_atr_mult=1.5,
        trailing_activation_pct=0.005,
        trailing_atr_mult=0.5,
        breakeven_activation_pct=0.005,
        partial_takes=(),
        momentum_exit=MomentumExitRule(70, 30, 80, 20),
        stagnation_rule=StagnationRule(2, 0.02, 0.5),
    )

    # B = moderate peak-quality candidates, super Kelly.
    thresholds_b = ProfileThresholds(
        min_composite_score=0.55,
        min_success_probability=0.50,
        min_signal_strength=0.50,
        min_expected_pnl=-0.5,
        max_rsi_4h=70.0,
        min_vol_spike=0.0,
        min_range_score=0.0,
        min_confluence=0.0,
        min_short_term_score=0.0,
        min_peak_quality=-0.35,
    )
    sizing_b = SizingPolicy(
        regime_allocation_pct=0.8,
        max_per_trade_usdt=1200.0,
        kelly_base_pct=0.30,
        kelly_max_pct=0.45,
        conviction_max_pct=0.45,
        category_caps={"LARGE_CAP": 0.40, "MIDCAP_ALT": 0.40, "NARRATIVE": 0.20},
        max_total_positions=4,
        fng_capital_multiplier=True,
    )
    exit_b = ExitPolicy(
        regime="RANGE_SCALP",
        profile="B",
        hard_sl_atr_mult=2.0,
        hard_tp_atr_mult=2.0,
        trailing_activation_pct=0.01,
        trailing_atr_mult=1.0,
        breakeven_activation_pct=0.01,
        partial_takes=(PartialTake(0.35, 1.0, "tp1"),),
        momentum_exit=MomentumExitRule(70, 30, 80, 20),
        stagnation_rule=StagnationRule(4, 0.04, 0.5),
    )

    # C = small peak candidates, aggressive Kelly, wider exits.
    thresholds_c = ProfileThresholds(
        min_composite_score=0.55,
        min_success_probability=0.50,
        min_signal_strength=0.50,
        min_expected_pnl=-0.5,
        max_rsi_4h=70.0,
        min_vol_spike=0.0,
        min_range_score=0.0,
        min_confluence=0.0,
        min_short_term_score=0.0,
        min_peak_quality=-0.80,
    )
    sizing_c = SizingPolicy(
        regime_allocation_pct=0.8,
        max_per_trade_usdt=600.0,
        kelly_base_pct=0.18,
        kelly_max_pct=0.28,
        conviction_max_pct=0.28,
        category_caps={"LARGE_CAP": 0.35, "MIDCAP_ALT": 0.35, "NARRATIVE": 0.15},
        max_total_positions=8,
        fng_capital_multiplier=True,
    )
    exit_c = ExitPolicy(
        regime="RANGE_SCALP",
        profile="C",
        hard_sl_atr_mult=2.5,
        hard_tp_atr_mult=2.5,
        trailing_activation_pct=0.015,
        trailing_atr_mult=1.5,
        breakeven_activation_pct=0.015,
        partial_takes=(PartialTake(0.35, 1.0, "tp1"),),
        momentum_exit=MomentumExitRule(70, 30, 80, 20),
        stagnation_rule=StagnationRule(6, 0.05, 0.5),
    )

    return LifecycleConfig(
        regime="RANGE_SCALP",
        profile="PREDICTIVE",
        signal_policy=signal_policy,
        entry_policy=entry_policy,
        sizing_policy=SizingPolicy(
            regime_allocation_pct=0.8,
            max_per_trade_usdt=600.0,
            kelly_base_pct=0.18,
            kelly_max_pct=0.28,
            conviction_max_pct=0.28,
            category_caps={"LARGE_CAP": 0.35, "MIDCAP_ALT": 0.35, "NARRATIVE": 0.15},
            max_total_positions=8,
        ),
        exit_policy=exit_policy,
        profile_order=("A", "B", "C"),
        profile_thresholds={"A": thresholds_a, "B": thresholds_b, "C": thresholds_c},
        profile_sizing_policies={"A": sizing_a, "B": sizing_b, "C": sizing_c},
        profile_exit_policies={"A": exit_a, "B": exit_b, "C": exit_c},
    )
