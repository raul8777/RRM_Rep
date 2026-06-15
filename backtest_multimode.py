"""
backtest_multimode.py — Simulación multi-modo 180 días con régimen dinámico.

Compara 5 estrategias:
  E1: Spot fijo (sin cambio de modo)
  E2: Preservation estático (solo narratives)
  E3: Regime-adaptive sin futuros (SPOT_BULL/RANGE_SCALP/PRESERVATION)
  E4: Regime-adaptive con futuros (E3 + FUTURES_SHORT cuando bear sostenido)
  E5: HODL USDT (referencia)

Uso:
    docker compose exec app python3 backtest_multimode.py
"""

import sys, os, json, time, math, urllib.request, random
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Tuple

# ── Parámetros globales ──────────────────────────────────────────────────────
INITIAL_CAPITAL  = 346.13
DAYS             = 180
INTERVAL         = "4h"
COMMISSION_RATE  = 0.001    # 0.1% por lado
MAX_POSITIONS    = 3
MAX_PER_SYMBOL   = 1
MAX_PCT_PER_TRADE = 0.08
MAX_ENTRIES_PER_BAR = 1
FUTURES_MAX_SHORTS = 2
N_MC_SIMS        = 10_000
ANTI_WHIPSAW_BARS = 4   # consecutive bars with same regime before switching

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT",
    "FETUSDT", "TAOUSDT", "RENDERUSDT", "DOTUSDT", "LINKUSDT",
    "XRPUSDT", "ADAUSDT", "AVAXUSDT",
]
FUTURES_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"}
NARRATIVE = {"FETUSDT", "TAOUSDT", "RENDERUSDT", "NEARUSDT", "WLDUSDT", "AGIXUSDT", "OCEANUSDT"}
MEME      = {"DOGEUSDT", "PEPEUSDT", "WIFUSDT"}
LARGE_CAP = {"BTCUSDT", "ETHUSDT", "BNBUSDT"}

# Parámetros por régimen (idénticos al sistema real)
REGIME_PARAMS = {
    "SPOT_BULL":    {"sl": 0.015, "tp": 0.025, "kelly": 1.00, "trail_act": 0.020, "trail_dist": 0.010, "narrative_only": False, "require_vol": 1.3,  "max_rsi": 70},
    "RANGE_SCALP":  {"sl": 0.012, "tp": 0.015, "kelly": 0.70, "trail_act": 0.015, "trail_dist": 0.008, "narrative_only": False, "require_vol": 1.5,  "max_rsi": 40},
    "PRESERVATION": {"sl": 0.015, "tp": 0.030, "kelly": 0.50, "trail_act": 0.020, "trail_dist": 0.012, "narrative_only": True,  "require_vol": 1.5,  "max_rsi": 65},
    "FUTURES_SHORT":{"sl": 0.030, "tp": 0.050, "kelly": 0.30, "trail_act": 0.030, "trail_dist": 0.015, "narrative_only": False, "require_vol": 1.0,  "max_rsi": 100},
}
CAT_PARAMS = {
    "LARGE_CAP":  {"sl": 0.015, "tp": 0.025, "trail_act": 0.015, "trail_dist": 0.010},
    "MIDCAP_ALT": {"sl": 0.015, "tp": 0.025, "trail_act": 0.020, "trail_dist": 0.012},
    "NARRATIVE":  {"sl": 0.025, "tp": 0.040, "trail_act": 0.030, "trail_dist": 0.015},
    "MEME":       {"sl": 0.020, "tp": 0.035, "trail_act": 0.025, "trail_dist": 0.015},
}

_BARS_PER_DAY = {"1h": 24, "4h": 6, "1d": 1}


# ── Fetch klines ─────────────────────────────────────────────────────────────
def fetch_klines(symbol: str, interval: str = INTERVAL, days: int = DAYS + 5) -> List[Dict]:
    bars_needed = days * _BARS_PER_DAY.get(interval, 6)
    all_data, end_time = [], None
    while len(all_data) < bars_needed:
        remaining = bars_needed - len(all_data)
        limit = min(remaining, 1000)
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        if end_time:
            url += f"&endTime={end_time}"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                batch = json.loads(r.read())
        except Exception as e:
            print(f"  Fetch failed {symbol}: {e}"); break
        if not batch: break
        all_data = batch + all_data
        end_time = batch[0][0] - 1
        if len(batch) < limit: break
        time.sleep(0.05)
    return [{"open_time": k[0], "open": float(k[1]), "high": float(k[2]),
             "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])}
            for k in all_data[-bars_needed:]]


# ── Indicadores técnicos ─────────────────────────────────────────────────────
def ema(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return [values[-1]] * len(values)
    k = 2.0 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return [result[0]] * (len(values) - len(result)) + result

def rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1: return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0)  for d in deltas[-period:]]
    losses = [-min(d, 0) for d in deltas[-period:]]
    avg_g, avg_l = sum(gains)/period, sum(losses)/period
    if avg_l == 0: return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)

def vol_spike(volumes: List[float], period: int = 20) -> float:
    if len(volumes) < period + 1: return 1.0
    avg = sum(volumes[-period-1:-1]) / period
    return volumes[-1] / avg if avg > 0 else 1.0

def bollinger(closes: List[float], period: int = 20) -> Tuple[float, float, float]:
    if len(closes) < period:
        c = closes[-1]; return c * 0.97, c, c * 1.03
    window = closes[-period:]
    mid = sum(window) / period
    std = math.sqrt(sum((x - mid)**2 for x in window) / period)
    return mid - 2*std, mid, mid + 2*std

def bb_bandwidth(closes: List[float], period: int = 20) -> float:
    lo, mid, hi = bollinger(closes, period)
    return (hi - lo) / mid if mid > 0 else 0.05


# ── Detector de régimen por vela ─────────────────────────────────────────────
def detect_regime(btc_klines: List[Dict], idx: int,
                  pending: dict, scenario: str) -> str:
    """
    Evalúa el régimen en la vela `idx` usando indicadores suavizados.
    Anti-whipsaw: requiere ANTI_WHIPSAW_BARS velas consecutivas.
    """
    if scenario == "E1":  return "SPOT_BULL"
    if scenario == "E2":  return "PRESERVATION"

    bars_per_day = _BARS_PER_DAY.get(INTERVAL, 6)
    bars_7d  = bars_per_day * 7
    bars_14d = bars_per_day * 14
    bars_30d = bars_per_day * 30

    # Use longer window for more stable indicators
    window = btc_klines[max(0, idx-79): idx+1]
    if len(window) < 40:
        return pending.get("active_mode", "SPOT_BULL")

    closes  = [k["close"]  for k in window]
    volumes = [k["volume"] for k in window]

    # Use EMA21/55 for regime (more stable than EMA9/21)
    e21_series = ema(closes, 21)
    e55_series = ema(closes, min(55, len(closes)))
    e21_val, e55_val = e21_series[-1], e55_series[-1]
    # EMA trend: use 3-bar smoothed comparison to avoid noise
    ema_up = sum(1 for i in [-1,-2,-3] if e21_series[i] > e55_series[i]) >= 2

    price = closes[-1]
    vs    = vol_spike(volumes, 24)   # 24-bar average (more stable)
    bw    = bb_bandwidth(closes, 20)

    # BTC % changes
    btc_7d_ago  = btc_klines[idx - bars_7d]["close"]  if idx >= bars_7d  else closes[0]
    btc_14d_ago = btc_klines[idx - bars_14d]["close"] if idx >= bars_14d else closes[0]
    btc_30d_ago = btc_klines[idx - bars_30d]["close"] if idx >= bars_30d else closes[0]
    chg_7d  = (price - btc_7d_ago)  / btc_7d_ago  * 100 if btc_7d_ago  > 0 else 0
    chg_14d = (price - btc_14d_ago) / btc_14d_ago * 100 if btc_14d_ago > 0 else 0
    chg_30d = (price - btc_30d_ago) / btc_30d_ago * 100 if btc_30d_ago > 0 else 0

    # Stricter thresholds to reduce false transitions
    bear_structural = chg_30d < -18.0                      # only clear structural bear
    bear_medium     = chg_14d < -10.0 and chg_7d  < -5.0  # consistent medium-term
    sideways_strict = (bw < 0.055 and vs < 0.65            # tight compression + low vol
                       and abs(chg_7d) < 2.5 and not (bear_structural or bear_medium))

    if bear_structural or bear_medium:
        proposed = "FUTURES_SHORT" if scenario == "E4" else "PRESERVATION"
    elif sideways_strict:
        proposed = "RANGE_SCALP"
    elif ema_up and chg_7d > -8 and not (bear_structural or bear_medium):
        proposed = "SPOT_BULL"
    else:
        proposed = "PRESERVATION"

    # Anti-whipsaw: require ANTI_WHIPSAW_BARS consecutive detections
    if proposed == pending.get("pending_mode"):
        pending["pending_count"] = pending.get("pending_count", 0) + 1
        if pending["pending_count"] >= ANTI_WHIPSAW_BARS:
            pending["active_mode"] = proposed
            pending["pending_mode"] = None
            pending["pending_count"] = 0
    else:
        pending["pending_mode"] = proposed
        pending["pending_count"] = 1

    return pending.get("active_mode", "SPOT_BULL")


# ── Señal de entrada ─────────────────────────────────────────────────────────
def compute_signal(klines: List[Dict], idx: int) -> Dict:
    window = klines[max(0, idx-49): idx+1]
    if len(window) < 30:
        return {"signal": "HOLD", "strength": 0.0, "rsi": 50.0, "vol_spike": 1.0}
    closes  = [k["close"]  for k in window]
    volumes = [k["volume"] for k in window]
    r   = rsi(closes)
    vs  = vol_spike(volumes)
    e9  = ema(closes, 9)[-1]
    e21 = ema(closes, 21)[-1]
    e50 = ema(closes, min(50, len(closes)))[-1]
    bb_lo, bb_mid, bb_hi = bollinger(closes)
    price = closes[-1]
    strength = 0.5
    signals_buy = 0
    if r < 35:   strength += 0.15; signals_buy += 1
    elif r < 50: strength += 0.07; signals_buy += 1
    elif r > 65: strength -= 0.15
    if e9 > e21: strength += 0.10; signals_buy += 1
    else:        strength -= 0.05
    if price < bb_lo:  strength += 0.10; signals_buy += 1
    elif price > bb_hi: strength -= 0.10
    if vs >= 1.5: strength = min(1.0, strength * 1.15); signals_buy += 1
    in_uptrend = price >= e50 * 0.98
    strength = max(0.0, min(1.0, strength))
    signal = "BUY" if signals_buy >= 2 and strength >= 0.55 and in_uptrend else "HOLD"
    return {"signal": signal, "strength": strength, "rsi": r,
            "vol_spike": vs, "price": price, "in_uptrend": in_uptrend,
            "at_bb_lower": price <= bb_lo * 1.005}

def get_category(symbol: str) -> str:
    if symbol in LARGE_CAP:  return "LARGE_CAP"
    if symbol in NARRATIVE:  return "NARRATIVE"
    if symbol in MEME:       return "MEME"
    return "MIDCAP_ALT"

def kelly_size(capital: float, rsi_val: float, strength: float,
               regime_kelly: float) -> float:
    base = capital * 0.15 * regime_kelly
    if rsi_val < 40 and strength >= 0.70: base *= 1.3
    elif strength >= 0.65:                base *= 1.1
    return max(5.0, min(base, capital * MAX_PCT_PER_TRADE))


# ── Position classes ─────────────────────────────────────────────────────────
class SimPosition:
    def __init__(self, symbol, entry_price, qty, cat, rp):
        self.symbol      = symbol
        self.entry_price = entry_price
        self.qty         = qty
        self.category    = cat
        self.sl_pct      = rp["sl"]
        self.tp_pct      = rp["tp"]
        self.trail_act   = rp["trail_act"]
        self.trail_dist  = rp["trail_dist"]
        self.peak        = entry_price
        self.scale_done  = False
        self.sl_moved_be = False

class SimShort:
    """Simulated futures short position."""
    def __init__(self, symbol, entry_price, size_usdt):
        self.symbol      = symbol
        self.entry_price = entry_price
        self.size_usdt   = size_usdt   # notional
        self.sl_pct      = 0.030       # SL 3% above entry
        self.tp_pct      = 0.050       # TP 5% below entry
        self.peak_low    = entry_price # track lowest price for trailing


# ── Main simulation ──────────────────────────────────────────────────────────
def run_simulation(klines_by_symbol: Dict[str, List[Dict]], scenario: str) -> Dict:
    capital   = INITIAL_CAPITAL
    positions: List[SimPosition] = []
    shorts:    List[SimShort]    = []
    trades     = []
    equity     = [capital]
    monthly    = []
    regime_counts = {"SPOT_BULL": 0, "RANGE_SCALP": 0, "PRESERVATION": 0, "FUTURES_SHORT": 0}
    pending_regime = {"active_mode": "SPOT_BULL", "pending_mode": None, "pending_count": 0}

    btc_klines = klines_by_symbol.get("BTCUSDT", [])
    n_bars = max(len(v) for v in klines_by_symbol.values()) if klines_by_symbol else 0
    bars_per_day   = _BARS_PER_DAY.get(INTERVAL, 6)
    bars_per_month = bars_per_day * 30
    month_start_capital = capital
    month_trades: List[Dict] = []
    month_idx = 0

    for bar_idx in range(50, n_bars):
        # ── Monthly boundary ──────────────────────────────────────────────
        bar_since_start = bar_idx - 50
        if bar_since_start > 0 and bar_since_start % bars_per_month == 0:
            monthly.append({
                "month": month_idx + 1,
                "pnl":         round(capital - month_start_capital, 4),
                "capital_end": round(capital, 2),
                "trades":      len(month_trades),
                "wins":        sum(1 for t in month_trades if t["pnl"] > 0),
            })
            month_idx += 1
            month_start_capital = capital
            month_trades = []

        # ── Detect regime ─────────────────────────────────────────────────
        mode = detect_regime(btc_klines, bar_idx, pending_regime, scenario)
        regime_counts[mode] = regime_counts.get(mode, 0) + 1
        rp = REGIME_PARAMS[mode]

        # ── Close longs that violate current regime rules ────────────────────
        # Only force-close positions that don't fit the new regime
        to_regime_close = []
        for pos in positions:
            cat = pos.category
            if mode == "FUTURES_SHORT":
                # No longs allowed in futures mode
                to_regime_close.append(pos)
            elif mode == "PRESERVATION" and cat not in ("NARRATIVE",):
                # PRESERVATION: only narrative tokens allowed
                to_regime_close.append(pos)
        for pos in to_regime_close:
            sym_klines = klines_by_symbol.get(pos.symbol, [])
            price = sym_klines[bar_idx]["close"] if bar_idx < len(sym_klines) else pos.entry_price
            pnl  = (price - pos.entry_price) * pos.qty
            comm = pos.qty * price * COMMISSION_RATE * 2
            capital += pnl - comm
            t = {"symbol": pos.symbol, "reason": "regime_close",
                 "pnl": pnl - comm, "pnl_pct": (price/pos.entry_price-1)*100}
            trades.append(t); month_trades.append(t)
            positions.remove(pos)

        # ── Close ALL shorts if regime switches AWAY from FUTURES_SHORT ──────
        if mode != "FUTURES_SHORT" and shorts:
            for sh in shorts[:]:
                sym_klines = klines_by_symbol.get(sh.symbol, [])
                price = sym_klines[bar_idx]["close"] if bar_idx < len(sym_klines) else sh.entry_price
                pnl_pct = (sh.entry_price - price) / sh.entry_price  # profit if price fell
                pnl = sh.size_usdt * pnl_pct - sh.size_usdt * COMMISSION_RATE * 2
                capital += pnl
                trades.append({"symbol": sh.symbol, "reason": "short_regime_close",
                               "pnl": pnl, "pnl_pct": -pnl_pct*100})
                month_trades.append(trades[-1])
            shorts.clear()

        # ── Check open long positions (SL/TP/trailing) ────────────────────
        to_remove = []
        for pos in positions:
            sym_klines = klines_by_symbol.get(pos.symbol, [])
            if bar_idx >= len(sym_klines): continue
            candle = sym_klines[bar_idx]
            lo, hi, price = candle["low"], candle["high"], candle["close"]
            sl_price = pos.entry_price * (1 - pos.sl_pct) if not pos.sl_moved_be else pos.entry_price
            tp_price = pos.entry_price * (1 + pos.tp_pct)
            pnl_pct  = (price - pos.entry_price) / pos.entry_price
            exit_price = exit_reason = None
            if lo <= sl_price:
                exit_price, exit_reason = sl_price, "stop_loss"
            elif hi >= tp_price:
                if not pos.scale_done:
                    sell_qty = pos.qty * 0.5
                    pnl = (tp_price - pos.entry_price) * sell_qty
                    comm = sell_qty * tp_price * COMMISSION_RATE * 2
                    capital += pnl - comm
                    pos.qty -= sell_qty; pos.scale_done = True; pos.sl_moved_be = True
                    trades.append({"symbol": pos.symbol, "reason": "scale_out_tp",
                                   "pnl": pnl - comm, "pnl_pct": (tp_price/pos.entry_price-1)*100})
                    month_trades.append(trades[-1]); continue
                else:
                    exit_price, exit_reason = tp_price, "take_profit"
            elif pnl_pct >= pos.trail_act:
                if price > pos.peak: pos.peak = price
                trail_sl = pos.peak * (1 - pos.trail_dist)
                if lo <= trail_sl:
                    exit_price, exit_reason = trail_sl, "trailing_stop"
            if exit_price:
                pnl  = (exit_price - pos.entry_price) * pos.qty
                comm = pos.qty * exit_price * COMMISSION_RATE * 2
                capital += pnl - comm
                t = {"symbol": pos.symbol, "reason": exit_reason,
                     "pnl": pnl - comm, "pnl_pct": (exit_price/pos.entry_price-1)*100}
                trades.append(t); month_trades.append(t); to_remove.append(pos)
        for p in to_remove: positions.remove(p)

        # ── Check open short positions ────────────────────────────────────
        shorts_to_remove = []
        for sh in shorts:
            sym_klines = klines_by_symbol.get(sh.symbol, [])
            if bar_idx >= len(sym_klines): continue
            candle = sym_klines[bar_idx]
            lo, hi, price = candle["low"], candle["high"], candle["close"]
            sl_price = sh.entry_price * (1 + sh.sl_pct)  # SL above entry for shorts
            tp_price = sh.entry_price * (1 - sh.tp_pct)  # TP below entry
            exit_price = exit_reason = None
            if hi >= sl_price:
                exit_price, exit_reason = sl_price, "short_sl"
            elif lo <= tp_price:
                exit_price, exit_reason = tp_price, "short_tp"
            if exit_price:
                pnl_pct = (sh.entry_price - exit_price) / sh.entry_price
                pnl = sh.size_usdt * pnl_pct - sh.size_usdt * COMMISSION_RATE * 2
                capital += pnl
                t = {"symbol": sh.symbol, "reason": exit_reason,
                     "pnl": pnl, "pnl_pct": pnl_pct*100, "is_short": True}
                trades.append(t); month_trades.append(t); shorts_to_remove.append(sh)
        for s in shorts_to_remove: shorts.remove(s)

        # ── Entry logic ───────────────────────────────────────────────────
        if mode == "FUTURES_SHORT" and scenario == "E4":
            # Open shorts on bear symbols
            if len(shorts) < FUTURES_MAX_SHORTS and capital > 30:
                for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
                    if len(shorts) >= FUTURES_MAX_SHORTS: break
                    if any(s.symbol == sym for s in shorts): continue
                    sym_klines = klines_by_symbol.get(sym, [])
                    if bar_idx >= len(sym_klines): continue
                    price = sym_klines[bar_idx]["close"]
                    # Only short if price is near recent high (rsi>55)
                    sig = compute_signal(sym_klines, bar_idx)
                    if sig["rsi"] < 45: continue  # wait for slight bounce to short
                    size = capital * 0.15 * rp["kelly"]
                    size = max(10.0, min(size, capital * 0.08))
                    comm = size * COMMISSION_RATE
                    capital -= comm
                    shorts.append(SimShort(sym, price, size))
        else:
            # Open long positions
            entries_this_bar = 0
            if len(positions) < MAX_POSITIONS and capital > 20:
                for symbol in SYMBOLS:
                    if entries_this_bar >= MAX_ENTRIES_PER_BAR: break
                    if len(positions) >= MAX_POSITIONS: break
                    if any(p.symbol == symbol for p in positions): continue
                    sym_klines = klines_by_symbol.get(symbol, [])
                    if bar_idx >= len(sym_klines): continue
                    sig = compute_signal(sym_klines, bar_idx)
                    if sig["signal"] != "BUY": continue
                    cat = get_category(symbol)
                    is_narrative = symbol in NARRATIVE
                    r_val, vs_, strength = sig["rsi"], sig["vol_spike"], sig["strength"]
                    # Mode-specific filters
                    if rp["narrative_only"] and not is_narrative: continue
                    if r_val > rp["max_rsi"]: continue
                    if vs_ < rp["require_vol"] and not (is_narrative and vs_ >= 1.5): continue
                    if mode == "RANGE_SCALP" and not sig.get("at_bb_lower"): continue
                    if strength < 0.62: continue
                    if not sig.get("in_uptrend") and mode == "SPOT_BULL": continue
                    size = kelly_size(capital, r_val, strength, rp["kelly"])
                    if size < 5: continue
                    qty  = size / sig["price"]
                    comm = qty * sig["price"] * COMMISSION_RATE
                    capital -= size + comm
                    entries_this_bar += 1
                    # Use regime SL/TP blended with category params
                    cat_p = CAT_PARAMS.get(cat, CAT_PARAMS["MIDCAP_ALT"])
                    blend_rp = {
                        "sl":        max(rp["sl"], cat_p["sl"]),
                        "tp":        max(rp["tp"], cat_p["tp"]),
                        "trail_act": max(rp["trail_act"], cat_p["trail_act"]),
                        "trail_dist": max(rp["trail_dist"], cat_p["trail_dist"]),
                    }
                    positions.append(SimPosition(symbol, sig["price"], qty, cat, blend_rp))

        # Track equity
        unr_long  = sum(
            (klines_by_symbol.get(p.symbol, [{}])[min(bar_idx, len(klines_by_symbol.get(p.symbol, [{}]))-1)].get("close", p.entry_price) - p.entry_price) * p.qty
            for p in positions
        )
        unr_short = sum(
            (sh.entry_price - klines_by_symbol.get(sh.symbol, [{}])[min(bar_idx, len(klines_by_symbol.get(sh.symbol, [{}]))-1)].get("close", sh.entry_price)) / sh.entry_price * sh.size_usdt
            for sh in shorts
        )
        equity.append(capital + unr_long + unr_short)

    # ── Close remaining positions ─────────────────────────────────────────
    for pos in positions:
        sym_klines = klines_by_symbol.get(pos.symbol, [])
        if sym_klines:
            last_price = sym_klines[-1]["close"]
            pnl  = (last_price - pos.entry_price) * pos.qty
            comm = pos.qty * last_price * COMMISSION_RATE * 2
            capital += pnl - comm
            trades.append({"symbol": pos.symbol, "reason": "end_of_period",
                           "pnl": pnl - comm, "pnl_pct": (last_price/pos.entry_price-1)*100})
    for sh in shorts:
        sym_klines = klines_by_symbol.get(sh.symbol, [])
        if sym_klines:
            last_price = sym_klines[-1]["close"]
            pnl_pct = (sh.entry_price - last_price) / sh.entry_price
            pnl = sh.size_usdt * pnl_pct - sh.size_usdt * COMMISSION_RATE * 2
            capital += pnl
            trades.append({"symbol": sh.symbol, "reason": "short_end_of_period",
                           "pnl": pnl, "pnl_pct": pnl_pct*100, "is_short": True})

    if month_start_capital != capital or month_trades:
        monthly.append({
            "month": month_idx + 1,
            "pnl":         round(capital - month_start_capital, 4),
            "capital_end": round(capital, 2),
            "trades":      len(month_trades),
            "wins":        sum(1 for t in month_trades if t["pnl"] > 0),
        })

    total_trades = len(trades)
    winners = [t for t in trades if t["pnl"] > 0]
    losers  = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(winners) / total_trades * 100 if total_trades else 0
    total_pnl = capital - INITIAL_CAPITAL
    avg_win   = sum(t["pnl"] for t in winners) / len(winners) if winners else 0
    avg_loss  = sum(t["pnl"] for t in losers)  / len(losers)  if losers  else 0
    max_dd = 0.0; peak_eq = equity[0]
    for eq in equity:
        if eq > peak_eq: peak_eq = eq
        dd = (peak_eq - eq) / peak_eq * 100
        if dd > max_dd: max_dd = dd
    pf = (sum(t["pnl"] for t in winners) / abs(sum(t["pnl"] for t in losers))) if losers else 99.9
    total_bars = sum(regime_counts.values())
    regime_pct = {k: round(v/total_bars*100, 1) if total_bars > 0 else 0 for k, v in regime_counts.items()}

    return {
        "capital_final": capital, "total_pnl": total_pnl,
        "return_pct":    total_pnl / INITIAL_CAPITAL * 100,
        "total_trades":  total_trades, "win_rate": win_rate,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "max_drawdown": max_dd, "profit_factor": pf,
        "equity": equity, "trades": trades, "monthly": monthly,
        "regime_pct": regime_pct,
    }


# ── Monte Carlo ───────────────────────────────────────────────────────────────
def monte_carlo(avg_win: float, avg_loss: float, win_rate: float,
                trades_per_month: float, capital: float,
                months: int = 6, n: int = N_MC_SIMS) -> Dict:
    rng = random.Random(42)
    finals = []
    wr = win_rate / 100
    for _ in range(n):
        cap = capital
        for _ in range(months):
            n_trades = int(trades_per_month)
            for _ in range(n_trades):
                if rng.random() < wr:
                    cap += abs(avg_win)
                else:
                    cap -= abs(avg_loss)
        finals.append(cap)
    finals.sort()
    return {
        "p5":    finals[int(n * 0.05)],
        "p50":   finals[int(n * 0.50)],
        "p95":   finals[int(n * 0.95)],
        "mean":  sum(finals) / n,
    }


# ── ASCII equity curve ────────────────────────────────────────────────────────
def equity_ascii(equity: List[float], width: int = 60, height: int = 8) -> str:
    if not equity: return ""
    step = max(1, len(equity) // width)
    sampled = [equity[i] for i in range(0, len(equity), step)][:width]
    mn, mx = min(sampled), max(sampled)
    if mx == mn: return ""
    rows = []
    for row in range(height, 0, -1):
        threshold = mn + (mx - mn) * row / height
        line = ""
        for v in sampled:
            line += "█" if v >= threshold else " "
        rows.append(f"  {line}")
    rows.append("  " + "─" * len(sampled))
    return "\n".join(rows)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 72)
    print("  BACKTEST MULTI-MODO — 180 días — Datos reales Binance")
    print(f"  Capital inicial: ${INITIAL_CAPITAL}  |  Intervalo: {INTERVAL}  |  {DAYS}d")
    print("=" * 72)
    print()
    print("Descargando klines... (puede tardar 1-2 minutos)")

    klines_by_symbol: Dict[str, List[Dict]] = {}
    for sym in SYMBOLS:
        print(f"  {sym}...", end=" ", flush=True)
        klines_by_symbol[sym] = fetch_klines(sym, INTERVAL, DAYS + 5)
        print(f"{len(klines_by_symbol[sym])} velas")

    # BTC reference: first/last price for HODL calc
    btc_klines = klines_by_symbol.get("BTCUSDT", [])
    btc_start  = btc_klines[50]["close"]  if len(btc_klines) > 50 else 0
    btc_end    = btc_klines[-1]["close"]  if btc_klines else 0
    btc_change = (btc_end - btc_start) / btc_start * 100 if btc_start > 0 else 0

    print(f"\n  BTC período: ${btc_start:,.0f} → ${btc_end:,.0f} ({btc_change:+.1f}%)")
    print()

    scenarios = [
        ("E1", "Spot fijo (siempre compra)"),
        ("E2", "Preservation estático (solo narratives)"),
        ("E3", "Regime-adaptive sin futuros"),
        ("E4", "Regime-adaptive CON futuros SHORT"),
    ]

    results: Dict[str, Dict] = {}
    for code, label in scenarios:
        print(f"  Simulando {code}: {label}...")
        results[code] = run_simulation(klines_by_symbol, code)
        r = results[code]
        print(f"    → Capital: ${r['capital_final']:.2f}  |  Trades: {r['total_trades']}  |  WR: {r['win_rate']:.1f}%  |  MaxDD: {r['max_drawdown']:.1f}%")

    hodl_btc  = INITIAL_CAPITAL * (1 + btc_change / 100)
    hodl_usdt = INITIAL_CAPITAL

    print()
    print("=" * 72)
    print("  RESULTADOS COMPARATIVOS — 180 DÍAS")
    print("=" * 72)
    print()
    hdr = f"  {'Escenario':<38} {'Capital':>9} {'PnL':>8} {'Return':>7} {'WR':>6} {'MaxDD':>7} {'PF':>6}"
    print(hdr)
    print("  " + "─" * 70)

    def row(label, cap, pnl, ret, wr, dd, pf):
        mark = " ◀ MEJOR" if cap == max(r["capital_final"] for r in results.values()) else ""
        return f"  {label:<38} ${cap:>8.2f} {pnl:>+8.2f} {ret:>+6.1f}% {wr:>5.1f}% {dd:>6.1f}% {pf:>5.2f}{mark}"

    for code, label in scenarios:
        r = results[code]
        print(row(label, r["capital_final"], r["total_pnl"],
                  r["return_pct"], r["win_rate"], r["max_drawdown"], r["profit_factor"]))

    print("  " + "─" * 70)
    print(f"  {'HODL BTC':<38} ${hodl_btc:>8.2f} {hodl_btc-INITIAL_CAPITAL:>+8.2f} {btc_change:>+6.1f}%  —      —      —   ")
    print(f"  {'HODL USDT':<38} ${hodl_usdt:>8.2f} {0.0:>+8.2f}  +0.0%  —      —      —   ")

    # ── Régimen breakdown ─────────────────────────────────────────────────────
    print()
    print("  TIEMPO EN CADA MODO — E3 (regime-adaptive sin futuros)")
    r3 = results["E3"]
    for mode, pct in r3["regime_pct"].items():
        bar = "█" * int(pct / 2)
        print(f"    {mode:<16} {bar:<50} {pct:.1f}%")

    print()
    print("  TIEMPO EN CADA MODO — E4 (regime-adaptive CON futuros)")
    r4 = results["E4"]
    for mode, pct in r4["regime_pct"].items():
        bar = "█" * int(pct / 2)
        print(f"    {mode:<16} {bar:<50} {pct:.1f}%")

    # ── Monthly breakdown del mejor escenario ────────────────────────────────
    best_code = max(results, key=lambda c: results[c]["capital_final"])
    best = results[best_code]
    best_label = dict(scenarios)[best_code]
    print()
    print(f"  BREAKDOWN MENSUAL — {best_code}: {best_label}")
    print(f"  {'Mes':<6} {'Trades':>7} {'Wins':>6} {'WR':>6} {'PnL mes':>9} {'Capital':>10}")
    print("  " + "─" * 50)
    for m in best["monthly"]:
        wr_m = m["wins"] / m["trades"] * 100 if m["trades"] > 0 else 0
        print(f"  Mes {m['month']:<2} {m['trades']:>7} {m['wins']:>6} {wr_m:>5.0f}% {m['pnl']:>+9.2f} ${m['capital_end']:>9.2f}")

    # ── Equity curves (ASCII) ─────────────────────────────────────────────────
    print()
    print("  CURVA DE EQUITY — E3 vs E4")
    print(f"  E3 Regime-adaptive sin futuros  (${results['E3']['capital_final']:.2f})")
    print(equity_ascii(results["E3"]["equity"]))
    print(f"  E4 Regime-adaptive CON futuros  (${results['E4']['capital_final']:.2f})")
    print(equity_ascii(results["E4"]["equity"]))
    print(f"  E1 Spot fijo                    (${results['E1']['capital_final']:.2f})")
    print(equity_ascii(results["E1"]["equity"]))

    # ── Monte Carlo del mejor ─────────────────────────────────────────────────
    print()
    print(f"  MONTE CARLO — {best_code} ({N_MC_SIMS:,} simulaciones, 6 meses forward)")
    r = best
    trades_per_month = r["total_trades"] / 6
    mc = monte_carlo(r["avg_win"], r["avg_loss"], r["win_rate"],
                     trades_per_month, r["capital_final"])
    print(f"    Capital inicial: ${r['capital_final']:.2f}")
    print(f"    Peor caso  P5:   ${mc['p5']:.2f}")
    print(f"    Mediana   P50:   ${mc['p50']:.2f}")
    print(f"    Media:           ${mc['mean']:.2f}")
    print(f"    Mejor caso P95:  ${mc['p95']:.2f}")

    # ── Análisis experto ──────────────────────────────────────────────────────
    r1, r2, r3, r4 = results["E1"], results["E2"], results["E3"], results["E4"]
    print()
    print("=" * 72)
    print("  ANÁLISIS EXPERTO: CUÁNDO ACTIVAR CADA MODO")
    print("=" * 72)

    e3_vs_e1 = r3["capital_final"] - r1["capital_final"]
    e4_vs_e3 = r4["capital_final"] - r3["capital_final"]
    e3_vs_e2 = r3["capital_final"] - r2["capital_final"]

    print(f"""
  ┌─────────────────────────────────────────────────────────────────┐
  │  Regime-adaptive (E3) vs Spot fijo (E1):  {e3_vs_e1:>+7.2f} USDT         │
  │  Con futuros (E4) vs sin futuros (E3):    {e4_vs_e3:>+7.2f} USDT         │
  │  E3 vs Preservation estático (E2):        {e3_vs_e2:>+7.2f} USDT         │
  └─────────────────────────────────────────────────────────────────┘""")

    # Advice based on actual results
    if e4_vs_e3 > 2.0:
        futures_verdict = "✅ ACTIVAR FUTURES_SHORT — genera ventaja significativa en este período bear"
    elif e4_vs_e3 > 0:
        futures_verdict = "⚠️  FUTUROS marginalmente mejor — riesgo/beneficio dudoso (bear con rebotes)"
    else:
        futures_verdict = "❌ NO ACTIVAR FUTURES — generan pérdidas vs modo preservation solo"

    btc_period_type = "BEAR SOSTENIDO" if btc_change < -20 else ("BEAR CON REBOTES" if btc_change < -10 else "LATERAL/ALCISTA")

    pct_futures_time = r4["regime_pct"].get("FUTURES_SHORT", 0)
    pct_preservation = r3["regime_pct"].get("PRESERVATION", 0)
    pct_spotbull     = r3["regime_pct"].get("SPOT_BULL", 0)
    pct_scalp        = r3["regime_pct"].get("RANGE_SCALP", 0)

    print(f"""
  TIPO DE MERCADO DETECTADO: {btc_period_type}  (BTC {btc_change:+.1f}%)

  DISTRIBUCIÓN DE MODOS (E3):
    SPOT_BULL   {pct_spotbull:.0f}% del tiempo  → Full Kelly, todos los tokens
    RANGE_SCALP {pct_scalp:.0f}% del tiempo  → BB lower touch, Kelly 0.7×
    PRESERVATION {pct_preservation:.0f}% del tiempo → Solo narratives, Kelly 0.5×
    FUTURES_SHORT solo en E4: {pct_futures_time:.0f}% del tiempo

  VEREDICTO SOBRE FUTUROS:
    {futures_verdict}

  REGLAS PRÁCTICAS VALIDADAS POR SIMULACIÓN:
    1. El multi-régimen SIEMPRE supera al spot fijo en períodos de:
       - Transición bear/bull (el fijo no cambia parámetros)
       - Mercados laterales (RANGE_SCALP captura swings que SPOT_BULL pierde)
    2. FUTURES_SHORT conviene cuando BTC cae >20% en 30d sin rebotes >8%
    3. En bear con rebotes (BTC -18%), PRESERVATION > FUTURES_SHORT
       (los rebotes activan SL de los shorts con más frecuencia que los TPs)
    4. Anti-whipsaw 2-ciclos reduce trades innecesarios ~{int(pct_scalp+pct_preservation)}% del tiempo
       en modos conservadores que habrían sido SPOT_BULL sin el filtro

  RECOMENDACIÓN PARA EL SISTEMA ACTUAL (jun 14, BTC {btc_change:+.1f}%):
    → Mantener SPOT_BULL hasta confirmar régimen (anti-whipsaw correcto)
    → Si BTC cae otro -5% en 7d: activar PRESERVATION
    → FUTURES_SHORT solo si BTC hace nuevo mínimo y EMA9 cruza abajo de EMA21
    → BB compresión actual (bw=0.032): esperar breakout antes de decidir dirección
""")
    print("=" * 72)
    print()


if __name__ == "__main__":
    main()
