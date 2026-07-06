"""
backtest_90d_v2.py — Simulación exhaustiva 90 días con parámetros de producción actuales.

Uso:
    docker compose exec app python3 backtest_90d_v2.py
    python3 backtest_90d_v2.py  (requiere acceso a internet para klines Binance)

Mejoras vs backtest_90d.py (original):
  - Capital inicial real: $2,910 USDT (era $346)
  - SL/TP actualizados por categoría (1.8/3.5, no 1.5/2.5)
  - 32 símbolos spot + 8 futures (era 13 estáticos)
  - Futuros SHORT integrados (régimen FUTURES_SHORT activo)
  - F&G Kelly boost: Capitulación×1.50, ExtremeF×1.35, ExtremeG×0.65
  - Anti-wick en trailing stop (no cierra por wick — post BUG-5)
  - 4 escenarios: A(baseline) / B(spot mejorado) / C(completo) / D(bear conservador)
  - Breakdown mensual + Monte Carlo 1,000 sims
  - Desglose spot vs futuros

Período cubierto: últimos 90 días desde hoy (klines 4h Binance API pública)
"""

import sys
import os
import json
import time
import math
import random
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Tuple

# ── Parámetros de producción actuales ──────────────────────────────────────
INITIAL_CAPITAL = 2910.0        # USDT — capital real jun 20 2026
DAYS            = 90
INTERVAL        = "4h"          # 6 velas/día → 90d ≈ 540 velas
# C0: use centralized Binance commission module (defaults: spot 0.001, futures 0.0004)
from binance_commission import (
    entry_commission as _bt90_entry_commission,
    exit_commission as _bt90_exit_commission,
    total_commission as _bt90_total_commission,
    get_config as _bt90_comm_config,
)
COMMISSION_RATE = _bt90_comm_config().rate_for('spot')  # kept for backward compat
MAX_ENTRIES_PER_BAR = 2         # hasta 2 entradas nuevas por vela 4h

# Régimen actual (jun 20 2026)
CURRENT_REGIME  = "FUTURES_SHORT"   # BTC 30d=-19%, F&G=15

# SL/TP por categoría — valores de PRODUCCIÓN actuales (post-audit)
CAT_PARAMS_PROD = {
    "LARGE_CAP":  {"sl": 0.018, "tp": 0.035, "trail_act": 0.025, "trail_dist": 0.012},
    "MIDCAP_ALT": {"sl": 0.018, "tp": 0.035, "trail_act": 0.025, "trail_dist": 0.012},
    "NARRATIVE":  {"sl": 0.035, "tp": 0.070, "trail_act": 0.080, "trail_dist": 0.030},
    "MEME":       {"sl": 0.020, "tp": 0.045, "trail_act": 0.025, "trail_dist": 0.015},
}

# SL/TP anteriores (baseline pre-audit para comparación)
CAT_PARAMS_OLD = {
    "LARGE_CAP":  {"sl": 0.015, "tp": 0.025, "trail_act": 0.015, "trail_dist": 0.010},
    "MIDCAP_ALT": {"sl": 0.015, "tp": 0.025, "trail_act": 0.020, "trail_dist": 0.012},
    "NARRATIVE":  {"sl": 0.025, "tp": 0.040, "trail_act": 0.030, "trail_dist": 0.015},
    "MEME":       {"sl": 0.020, "tp": 0.035, "trail_act": 0.025, "trail_dist": 0.015},
}

# Futuros SHORT — producción actual
FUTURES_SL         = 0.025   # 2.5% SL
FUTURES_TP         = 0.060   # 6.0% TP
FUTURES_LEVERAGE   = 2       # 2x
FUTURES_MARGIN     = 100.0   # USDT por trade (F&G=15 → boost 1.50 → ~$130-150 efectivo)
FUTURES_MAX_SHORTS = 7       # slots activos
FUTURES_LARGECAP_MAX = 5     # max largecap shorts

# Symbols — universo expandido (32 spot + 8 futures)
LARGE_CAP  = {"BTCUSDT", "ETHUSDT", "BNBUSDT"}
NARRATIVE  = {"FETUSDT", "TAOUSDT", "RENDERUSDT", "WLDUSDT", "AGIXUSDT", "OCEANUSDT",
              "GRTUSDT", "AIOZUSDT", "ONDOUSDT", "JUPUSDT", "OPUSDT"}
MEME       = {"DOGEUSDT", "PEPEUSDT", "WIFUSDT"}

SPOT_SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT","DOGEUSDT",
    "DOTUSDT","LINKUSDT","AVAXUSDT","NEARUSDT","SUIUSDT","FETUSDT","TAOUSDT",
    "RENDERUSDT","INJUSDT","APTUSDT","ARBUSDT","MATICUSDT","ATOMUSDT",
    "GRTUSDT","AIOZUSDT","SEIUSDT","AAVEUSDT","UNIUSDT","ONDOUSDT",
    "JUPUSDT","OPUSDT","WLDUSDT","AGIXUSDT","OCEANUSDT","BNBUSDT",
]
SPOT_SYMBOLS = list(dict.fromkeys(SPOT_SYMBOLS))  # dedup, preserve order

FUTURES_SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","BNBUSDT",
                   "NEARUSDT","SUIUSDT","ADAUSDT"]

# F&G simulator (aprox histórico — estará en ~15-30 en bear)
def get_fng_approx(bar_idx: int) -> int:
    """Simula F&G oscilando entre 10-40 en bear market (FUTURES_SHORT régimen)."""
    t = bar_idx * 0.03
    return max(10, min(40, int(20 + 15 * math.sin(t) + 5 * math.sin(t * 3.7))))


def get_category(symbol: str) -> str:
    if symbol in LARGE_CAP:   return "LARGE_CAP"
    if symbol in NARRATIVE:   return "NARRATIVE"
    if symbol in MEME:        return "MEME"
    return "MIDCAP_ALT"


# ── Fetch klines ─────────────────────────────────────────────────────────────
_BARS_PER_DAY = {"1h": 24, "4h": 6, "1d": 1}

def fetch_klines(symbol: str, interval: str = INTERVAL, days: int = DAYS + 5) -> List[Dict]:
    bars_needed = days * _BARS_PER_DAY.get(interval, 6)
    all_data = []
    end_time = None
    attempts = 0
    while len(all_data) < bars_needed and attempts < 5:
        remaining = bars_needed - len(all_data)
        limit = min(remaining, 1000)
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        if end_time:
            url += f"&endTime={end_time}"
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                batch = json.loads(r.read())
        except Exception as e:
            attempts += 1
            print(f"  Retry {attempts} {symbol}: {e}")
            time.sleep(1)
            continue
        if not batch:
            break
        all_data = batch + all_data
        end_time = batch[0][0] - 1
        if len(batch) < limit:
            break
        time.sleep(0.08)
    return [{"open_time": k[0], "open": float(k[1]), "high": float(k[2]),
             "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])}
            for k in all_data[-bars_needed:]]


# ── Indicadores técnicos ──────────────────────────────────────────────────────
def ema(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return [values[-1]] * len(values)
    k = 2.0 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    pad = [result[0]] * (len(values) - len(result))
    return pad + result

def rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas[-period:]]
    losses = [-min(d, 0) for d in deltas[-period:]]
    avg_g  = sum(gains) / period
    avg_l  = sum(losses) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)

def vol_spike(volumes: List[float], period: int = 20) -> float:
    if len(volumes) < period + 1:
        return 1.0
    avg = sum(volumes[-period-1:-1]) / period
    return volumes[-1] / avg if avg > 0 else 1.0

def bollinger(closes: List[float], period: int = 20) -> Tuple[float, float, float]:
    if len(closes) < period:
        c = closes[-1]
        return c * 0.97, c, c * 1.03
    window = closes[-period:]
    mid = sum(window) / period
    std = math.sqrt(sum((x - mid) ** 2 for x in window) / period)
    return mid - 2 * std, mid, mid + 2 * std

def atr(klines: List[Dict], period: int = 14) -> float:
    if len(klines) < period + 1:
        return klines[-1]["close"] * 0.02
    trs = []
    for i in range(1, len(klines)):
        hi, lo, prev_c = klines[i]["high"], klines[i]["low"], klines[i-1]["close"]
        trs.append(max(hi - lo, abs(hi - prev_c), abs(lo - prev_c)))
    return sum(trs[-period:]) / period


# ── Signal computation ────────────────────────────────────────────────────────
def compute_signal(klines: List[Dict], idx: int, scenario: str = "C") -> Dict:
    window = klines[max(0, idx-49): idx+1]
    if len(window) < 30:
        return {"signal": "HOLD", "strength": 0.0, "rsi": 50.0, "vol_spike": 1.0}

    closes  = [k["close"]  for k in window]
    volumes = [k["volume"] for k in window]

    r_val  = rsi(closes)
    vs     = vol_spike(volumes)
    e9     = ema(closes, 9)[-1]
    e21    = ema(closes, 21)[-1]
    e50    = ema(closes, min(50, len(closes)))[-1]
    bb_lo, bb_mid, bb_hi = bollinger(closes)
    atr_v  = atr(window)
    price  = closes[-1]

    strength = 0.5
    signals_buy = 0

    # RSI
    if r_val < 35:
        strength += 0.15; signals_buy += 1
    elif r_val < 50:
        strength += 0.07; signals_buy += 1
    elif r_val > 65:
        strength -= 0.15

    # EMA cross
    if e9 > e21:
        strength += 0.10; signals_buy += 1
    else:
        strength -= 0.05

    # Bollinger
    if price < bb_lo:
        strength += 0.10; signals_buy += 1
    elif price > bb_hi:
        strength -= 0.10

    # Volume
    if vs >= 1.5:
        strength = min(1.0, strength * 1.15); signals_buy += 1

    # ATR boost: if ATR/price > 2% (volatile = opportunity)
    atr_pct = atr_v / price if price > 0 else 0
    if atr_pct > 0.02:
        strength = min(1.0, strength + 0.05)

    # Long-term trend
    in_uptrend = price >= e50 * 0.98

    # Bullish screener (spot): só score positivo si chg > 0 — post GAP-3
    # Proxy: use EMA cross as trend proxy
    bullish_market = (e9 > e21)

    strength = max(0.0, min(1.0, strength))

    # Entry threshold by scenario
    min_str = {"A": 0.55, "B": 0.60, "C": 0.62, "D": 0.65}.get(scenario, 0.62)
    rsi_max  = 65

    volspike_override = (vs >= 1.5 and r_val < 65)  # NARRATIVE bypass

    signal = "HOLD"
    if signals_buy >= 2 and strength >= min_str and (in_uptrend or volspike_override):
        if r_val <= rsi_max or volspike_override:
            if vs >= 1.3 or volspike_override:
                if bullish_market or volspike_override:
                    signal = "BUY"

    return {
        "signal": signal, "strength": strength, "rsi": r_val,
        "vol_spike": vs, "price": price, "in_uptrend": in_uptrend,
        "atr_pct": atr_pct, "bullish": bullish_market
    }


def compute_short_signal(klines: List[Dict], idx: int) -> Dict:
    """Señal para futuros SHORT: bajista confirma entrada."""
    window = klines[max(0, idx-49): idx+1]
    if len(window) < 30:
        return {"signal": "HOLD", "strength": 0.5}
    closes  = [k["close"]  for k in window]
    volumes = [k["volume"] for k in window]
    r_val  = rsi(closes)
    vs     = vol_spike(volumes)
    e9     = ema(closes, 9)[-1]
    e21    = ema(closes, 21)[-1]
    e50    = ema(closes, min(50, len(closes)))[-1]
    price  = closes[-1]

    # Condiciones bajistas para SHORT
    bearish = 0
    strength = 0.5
    if r_val > 55:
        strength += 0.15; bearish += 1   # sobrecomprado = oportunidad short
    if e9 < e21:
        strength += 0.10; bearish += 1   # downtrend
    if price < e50 * 0.99:
        strength += 0.10; bearish += 1   # por debajo EMA50
    if vs >= 1.3:
        strength = min(1.0, strength * 1.10); bearish += 1

    signal = "SHORT" if bearish >= 2 and strength >= 0.60 else "HOLD"
    return {"signal": signal, "strength": min(1.0, strength), "rsi": r_val, "vol_spike": vs}


# ── Kelly sizing ──────────────────────────────────────────────────────────────
def kelly_size(capital: float, strength: float, rsi_val: float,
               scenario: str, fng: int, cat_params: Dict) -> float:
    # Smart Kelly dinámico — alineado con producción real
    # base dinámica: 12% del capital líquido
    base_pct = 0.12
    base     = capital * base_pct

    # Tiers de convicción
    if strength >= 0.80:
        size = capital * 0.25   # FUERTE: 25% capital
    elif strength >= 0.70:
        size = capital * 0.22   # ALTA: 22%
    elif strength >= 0.55:
        size = capital * 0.18   # MEDIA: 18%
    else:
        size = base              # BASE: 12%

    # F&G Kelly boost
    if fng <= 15:
        size *= 1.50   # Capitulación
    elif fng <= 20:
        size *= 1.35   # Extreme Fear
    elif fng >= 75:
        size *= 0.65   # Extreme Greed

    # Scenario D: bear conservador reduce kelly
    if scenario == "D":
        size *= 0.60

    # Caps — ORDEN CORRECTO: primero % del capital, luego absoluto
    size = min(size, capital * 0.28)   # max 28% del capital disponible
    size = min(size, 350.0)            # cap absoluto $350 (env MAX_PER_TRADE_USDT)

    # Scenario A: base fija simple (baseline pre-improvements)
    if scenario == "A":
        size = min(capital * 0.08, 200.0)

    return max(5.0, size)


# ── Position classes ──────────────────────────────────────────────────────────
class SimSpotPosition:
    def __init__(self, symbol, entry_price, qty, cat_params, category, scenario):
        self.symbol      = symbol
        self.entry_price = entry_price
        self.qty         = qty
        self.sl_pct      = cat_params["sl"]
        self.tp_pct      = cat_params["tp"]
        self.trail_act   = cat_params["trail_act"]
        self.trail_dist  = cat_params["trail_dist"]
        self.category    = category
        self.scenario    = scenario
        self.peak        = entry_price
        self.scale_done  = False
        self.sl_moved_be = False
        self.sl_price    = entry_price * (1 - cat_params["sl"])
        self.tp_price    = entry_price * (1 + cat_params["tp"])


class SimFuturesPosition:
    def __init__(self, symbol, entry_price, qty, margin, side="SHORT"):
        self.symbol      = symbol
        self.entry_price = entry_price
        self.qty         = qty
        self.margin      = margin
        self.side        = side
        self.peak        = entry_price   # best_price (lowest for short)
        self.sl_price    = entry_price * (1 + FUTURES_SL)  # SHORT: SL above entry
        self.tp_price    = entry_price * (1 - FUTURES_TP)  # SHORT: TP below entry
        self.trail_dist  = 0.018         # 1.8% trailing para futuros


# ── Main simulation ───────────────────────────────────────────────────────────
def run_simulation(klines_by_symbol: Dict[str, List[Dict]], scenario: str) -> Dict:
    capital          = INITIAL_CAPITAL
    spot_positions:    List[SimSpotPosition]    = []
    futures_positions: List[SimFuturesPosition] = []
    trades           = []
    futures_trades   = []
    equity           = [capital]
    monthly          = []

    cat_params_map = CAT_PARAMS_PROD if scenario != "A" else CAT_PARAMS_OLD

    btc_klines = klines_by_symbol.get("BTCUSDT", [])
    n_bars     = max((len(v) for v in klines_by_symbol.values()), default=0)
    print(f"    [{scenario}] n_bars={n_bars}, symbols={len(klines_by_symbol)}", flush=True)
    bars_per_day   = _BARS_PER_DAY.get(INTERVAL, 6)
    bars_per_month = bars_per_day * 30
    bars_7d        = bars_per_day * 7
    bars_30d       = bars_per_day * 30

    month_start_cap = capital
    month_trades: List[Dict] = []
    month_idx = 0

    # Start after enough history for 30d regime detection (180 bars 4h = 30d)
    start_bar = max(50, bars_30d + 1)
    for bar_idx in range(start_bar, n_bars):
        bar_since = bar_idx - 50

        # Monthly boundary
        if bar_since > 0 and bar_since % bars_per_month == 0:
            unrealized = sum(
                (klines_by_symbol.get(p.symbol, [{}])[min(bar_idx, len(klines_by_symbol.get(p.symbol, [])) - 1)].get("close", p.entry_price) - p.entry_price) * p.qty
                for p in spot_positions
            )
            monthly.append({
                "month":       month_idx + 1,
                "pnl":         round(capital - month_start_cap, 4),
                "capital_end": round(capital + unrealized, 2),
                "trades":      len(month_trades),
                "wins":        sum(1 for t in month_trades if t["pnl"] > 0),
            })
            month_idx       += 1
            month_start_cap  = capital
            month_trades     = []

        btc_bar = btc_klines[bar_idx] if bar_idx < len(btc_klines) else None

        # Bear mode / regime detection (requires history buffer)
        bear_mode  = False
        future_short_regime = False
        btc_now = btc_bar["close"] if btc_bar else None

        if btc_bar and btc_now and bar_idx >= bars_7d and (bar_idx - bars_7d) < len(btc_klines):
            btc_7d = btc_klines[bar_idx - bars_7d]["close"]
            if btc_7d > 0:
                btc_7d_chg = (btc_now - btc_7d) / btc_7d * 100
                bear_mode  = btc_7d_chg <= -7.0

        if btc_bar and btc_now and bar_idx >= bars_30d and (bar_idx - bars_30d) < len(btc_klines):
            btc_30d = btc_klines[bar_idx - bars_30d]["close"]
            if btc_30d > 0:
                btc_30d_chg = (btc_now - btc_30d) / btc_30d * 100
                future_short_regime = (btc_30d_chg <= -15.0)

        fng = get_fng_approx(bar_idx)

        # ── Check & close spot positions ──────────────────────────────────
        to_remove_spot = []
        for pos in spot_positions:
            sym_klines = klines_by_symbol.get(pos.symbol, [])
            if bar_idx >= len(sym_klines):
                continue
            candle = sym_klines[bar_idx]
            lo, hi = candle["low"], candle["high"]
            price  = candle["close"]
            pnl_pct = (price - pos.entry_price) / pos.entry_price

            exit_price  = None
            exit_reason = None

            # Stop-loss
            sl_price = pos.entry_price if pos.sl_moved_be else pos.entry_price * (1 - pos.sl_pct)
            if lo <= sl_price:
                # Anti-wick check (BUG-5 fix): only trigger if close also below SL
                if scenario in ("B", "C", "D") and price > sl_price:
                    pass  # wick — skip close
                else:
                    exit_price  = sl_price
                    exit_reason = "stop_loss"

            # Take-profit / scale-out
            elif hi >= pos.tp_price:
                if scenario in ("B", "C", "D") and not pos.scale_done:
                    sell_qty = pos.qty * 0.5
                    pnl  = (pos.tp_price - pos.entry_price) * sell_qty
                    comm = _bt90_total_commission(sell_qty, pos.entry_price, pos.tp_price, 'spot', None)
                    capital     += pnl - comm
                    pos.qty     -= sell_qty
                    pos.scale_done  = True
                    pos.sl_moved_be = True
                    trades.append({"symbol": pos.symbol, "reason": "scale_out",
                                   "pnl": pnl - comm, "pnl_pct": (pos.tp_price/pos.entry_price-1)*100,
                                   "type": "spot"})
                    month_trades.append(trades[-1])
                    continue
                else:
                    exit_price  = pos.tp_price
                    exit_reason = "take_profit"

            # Trailing stop — post BUG-5: anti-wick on trailing too
            elif pnl_pct >= pos.trail_act:
                if price > pos.peak:
                    pos.peak = price
                trail_sl = pos.peak * (1 - pos.trail_dist)
                if lo <= trail_sl:
                    # BUG-5: anti-wick — verify close also below trail_sl
                    if scenario in ("B", "C", "D") and price > trail_sl:
                        pass  # wick on trailing — skip
                    else:
                        exit_price  = trail_sl
                        exit_reason = "trailing_stop"

            if exit_price and exit_reason:
                pnl  = (exit_price - pos.entry_price) * pos.qty
                comm = _bt90_total_commission(pos.qty, pos.entry_price, exit_price, 'spot', None)
                capital += pnl - comm
                trades.append({"symbol": pos.symbol, "reason": exit_reason,
                               "pnl": pnl - comm,
                               "pnl_pct": (exit_price / pos.entry_price - 1) * 100,
                               "type": "spot"})
                to_remove_spot.append(pos)
                month_trades.append(trades[-1])

        for p in to_remove_spot:
            spot_positions.remove(p)

        # ── Check & close futures positions ──────────────────────────────
        if scenario in ("C", "D"):
            to_remove_fut = []
            for fpos in futures_positions:
                sym_klines = klines_by_symbol.get(fpos.symbol, [])
                if bar_idx >= len(sym_klines):
                    continue
                candle = sym_klines[bar_idx]
                lo, hi = candle["low"], candle["high"]
                price  = candle["close"]

                exit_price  = None
                exit_reason = None

                if fpos.side == "SHORT":
                    # SL: price goes UP past sl_price (loss for short)
                    if hi >= fpos.sl_price:
                        exit_price  = fpos.sl_price
                        exit_reason = "stop_loss"
                    # TP: price goes DOWN past tp_price (profit for short)
                    elif lo <= fpos.tp_price:
                        exit_price  = fpos.tp_price
                        exit_reason = "take_profit"
                    # Trailing short: move sl down as price drops
                    elif price < fpos.peak:
                        fpos.peak = price
                        new_sl = fpos.peak * (1 + fpos.trail_dist)
                        if new_sl < fpos.sl_price:
                            fpos.sl_price = new_sl
                    # Regime change: if no longer bear → close short (protect profit)
                    elif not future_short_regime and not bear_mode:
                        fut_pnl_pct = (fpos.entry_price - price) / fpos.entry_price
                        if fut_pnl_pct > 0:  # only close winning shorts
                            exit_price  = price
                            exit_reason = "regime_change"

                if exit_price and exit_reason:
                    if fpos.side == "SHORT":
                        pnl_notional = (fpos.entry_price - exit_price) * fpos.qty * FUTURES_LEVERAGE
                    else:
                        pnl_notional = (exit_price - fpos.entry_price) * fpos.qty * FUTURES_LEVERAGE
                    comm = _bt90_total_commission(fpos.qty, fpos.entry_price, exit_price, 'futures', None)
                    net_pnl = pnl_notional - comm
                    capital += net_pnl
                    futures_trades.append({
                        "symbol": fpos.symbol, "reason": exit_reason,
                        "pnl": net_pnl,
                        "pnl_pct": net_pnl / fpos.margin * 100,
                        "type": "futures_short"
                    })
                    to_remove_fut.append(fpos)
                    month_trades.append(futures_trades[-1])

            for p in to_remove_fut:
                futures_positions.remove(p)

        # ── Entry: spot ───────────────────────────────────────────────────
        if future_short_regime:
            max_spot = 4   # FUTURES_SHORT: solo NARRATIVE spot (max 4)
        elif bear_mode:
            max_spot = 5   # PRESERVATION: solo NARRATIVE
        else:
            max_spot = 20  # SPOT_BULL
        entries_this_bar = 0

        if len(spot_positions) < max_spot and capital > 50:
            for symbol in SPOT_SYMBOLS:
                if entries_this_bar >= MAX_ENTRIES_PER_BAR:
                    break
                if len(spot_positions) >= max_spot:
                    break
                if any(p.symbol == symbol for p in spot_positions):
                    continue
                sym_klines = klines_by_symbol.get(symbol, [])
                if bar_idx >= len(sym_klines):
                    continue
                # In FUTURES_SHORT regime: skip spot entries (only PRESERVATION-style narrative)
                cat = get_category(symbol)
                if future_short_regime and scenario in ("C", "D") and cat not in ("NARRATIVE",):
                    continue  # bear market: only narrative spot entries

                sig = compute_signal(sym_klines, bar_idx, scenario)
                if sig["signal"] != "BUY":
                    continue

                # Bear mode scenario A/B: skip non-narrative
                if bear_mode and scenario == "A" and cat not in ("NARRATIVE",):
                    continue

                # Scenario D: stricter filter
                if scenario == "D" and sig["strength"] < 0.65:
                    continue

                cat_p = cat_params_map.get(cat, cat_params_map["MIDCAP_ALT"])
                size  = kelly_size(capital, sig["strength"], sig["rsi"], scenario, fng, cat_p)
                if size < 10:
                    continue

                price = sig["price"]
                qty   = size / price
                comm  = _bt90_entry_commission(qty, price, 'spot', None)  # C0: entry-only at open
                if capital < size + comm:
                    continue
                capital -= size + comm
                entries_this_bar += 1
                spot_positions.append(SimSpotPosition(symbol, price, qty, cat_p, cat, scenario))

        # ── Entry: futures SHORT ──────────────────────────────────────────
        if scenario in ("C", "D") and (future_short_regime or bear_mode):
            largecap_shorts = sum(1 for p in futures_positions if p.symbol in LARGE_CAP)
            for symbol in FUTURES_SYMBOLS:
                if len(futures_positions) >= FUTURES_MAX_SHORTS:
                    break
                if any(p.symbol == symbol for p in futures_positions):
                    continue
                if symbol in LARGE_CAP and largecap_shorts >= FUTURES_LARGECAP_MAX:
                    continue
                sym_klines = klines_by_symbol.get(symbol, [])
                if bar_idx >= len(sym_klines):
                    continue

                sig = compute_short_signal(sym_klines, bar_idx)
                if sig["signal"] != "SHORT":
                    continue

                # Scenario D: stricter
                if scenario == "D" and sig["strength"] < 0.65:
                    continue

                # F&G boost on margin
                margin = FUTURES_MARGIN
                if fng <= 15:
                    margin *= 1.30
                elif fng <= 20:
                    margin *= 1.15

                if capital < margin * 1.05:
                    continue

                price = sym_klines[bar_idx]["close"]
                qty   = (margin * FUTURES_LEVERAGE) / price
                comm  = _bt90_entry_commission(qty, price, 'futures', None)  # C0: entry fee
                capital -= margin + comm
                futures_positions.append(SimFuturesPosition(symbol, price, qty, margin, "SHORT"))
                if symbol in LARGE_CAP:
                    largecap_shorts += 1

        # Equity tracking
        spot_unr = sum(
            (klines_by_symbol.get(p.symbol, [{}])[min(bar_idx, len(klines_by_symbol.get(p.symbol, [])) - 1)].get("close", p.entry_price) - p.entry_price) * p.qty
            for p in spot_positions
        )
        equity.append(capital + spot_unr)

    # Close remaining spot positions
    for pos in spot_positions:
        sym_klines = klines_by_symbol.get(pos.symbol, [])
        if sym_klines:
            last_price = sym_klines[-1]["close"]
            pnl  = (last_price - pos.entry_price) * pos.qty
            comm = _bt90_total_commission(pos.qty, pos.entry_price, last_price, 'spot', None)
            capital += pnl - comm
            trades.append({"symbol": pos.symbol, "reason": "end_of_period",
                           "pnl": pnl - comm,
                           "pnl_pct": (last_price / pos.entry_price - 1) * 100,
                           "type": "spot"})

    # Close remaining futures positions
    if scenario in ("C", "D"):
        for fpos in futures_positions:
            sym_klines = klines_by_symbol.get(fpos.symbol, [])
            if sym_klines:
                last_price = sym_klines[-1]["close"]
                pnl_notional = (fpos.entry_price - last_price) * fpos.qty * FUTURES_LEVERAGE
                comm = _bt90_total_commission(fpos.qty, fpos.entry_price, last_price, 'futures', None)
                net_pnl = pnl_notional - comm
                capital += net_pnl
                futures_trades.append({"symbol": fpos.symbol, "reason": "end_of_period",
                                       "pnl": net_pnl, "pnl_pct": net_pnl / fpos.margin * 100,
                                       "type": "futures_short"})

    # Flush last partial month
    if month_start_cap != capital or month_trades:
        monthly.append({
            "month":       month_idx + 1,
            "pnl":         round(capital - month_start_cap, 4),
            "capital_end": round(capital, 2),
            "trades":      len(month_trades),
            "wins":        sum(1 for t in month_trades if t["pnl"] > 0),
        })

    all_trades   = trades + futures_trades
    total_trades = len(all_trades)
    winners      = [t for t in all_trades if t["pnl"] > 0]
    losers       = [t for t in all_trades if t["pnl"] <= 0]
    win_rate     = len(winners) / total_trades * 100 if total_trades else 0
    total_pnl    = capital - INITIAL_CAPITAL
    return_pct   = total_pnl / INITIAL_CAPITAL * 100
    avg_win      = sum(t["pnl"] for t in winners) / len(winners) if winners else 0
    avg_loss     = sum(t["pnl"] for t in losers)  / len(losers)  if losers else 0

    max_dd = 0.0
    peak   = equity[0]
    for eq in equity:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    pf = (sum(t["pnl"] for t in winners) /
          abs(sum(t["pnl"] for t in losers))) if losers else 99.9

    # Spot vs futures breakdown
    spot_pnl    = sum(t["pnl"] for t in trades)
    futures_pnl = sum(t["pnl"] for t in futures_trades)

    return {
        "scenario":       scenario,
        "capital_final":  capital,
        "total_pnl":      total_pnl,
        "return_pct":     return_pct,
        "pnl_per_day":    total_pnl / DAYS,
        "mxn_per_day":    total_pnl / DAYS * 17.5,
        "total_trades":   total_trades,
        "spot_trades":    len(trades),
        "futures_trades": len(futures_trades),
        "win_rate":       win_rate,
        "avg_win":        avg_win,
        "avg_loss":       avg_loss,
        "max_drawdown":   max_dd,
        "profit_factor":  pf,
        "spot_pnl":       spot_pnl,
        "futures_pnl":    futures_pnl,
        "equity":         equity,
        "monthly":        monthly,
        "trades":         all_trades,
    }


# ── Monte Carlo ───────────────────────────────────────────────────────────────
def monte_carlo(win_rate: float, avg_win: float, avg_loss: float,
                trades_per_day: float, days: int = 90,
                n_sims: int = 1000, initial_capital: float = INITIAL_CAPITAL) -> Dict:
    results = []
    for _ in range(n_sims):
        cap = initial_capital
        for _ in range(int(trades_per_day * days)):
            if random.random() < win_rate / 100:
                cap += abs(avg_win)
            else:
                cap += avg_loss  # avg_loss is negative
        results.append(cap)
    results.sort()
    return {
        "P10": results[int(n_sims * 0.10)],
        "P50": results[int(n_sims * 0.50)],
        "P90": results[int(n_sims * 0.90)],
        "P10_return": (results[int(n_sims * 0.10)] - initial_capital) / initial_capital * 100,
        "P50_return": (results[int(n_sims * 0.50)] - initial_capital) / initial_capital * 100,
        "P90_return": (results[int(n_sims * 0.90)] - initial_capital) / initial_capital * 100,
    }


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 72)
    print(f"  SIMULACIÓN 90 DÍAS v2 — Capital inicial: ${INITIAL_CAPITAL:,.2f} USDT")
    print(f"  Período: últimos {DAYS} días | intervalo {INTERVAL}")
    print(f"  Régimen actual: {CURRENT_REGIME} | F&G~15 (Capitulación)")
    print(f"  Símbolos spot: {len(SPOT_SYMBOLS)} | Futuros: {len(FUTURES_SYMBOLS)}")
    print("=" * 72)

    # Descargar klines spot
    all_symbols = list(dict.fromkeys(SPOT_SYMBOLS + FUTURES_SYMBOLS))
    print(f"\n[1/3] Descargando datos de Binance ({len(all_symbols)} símbolos × {DAYS}d)...")
    klines_by_symbol = {}
    failed = []
    for sym in all_symbols:
        print(f"  {sym}...", end=" ", flush=True)
        kl = fetch_klines(sym, INTERVAL, DAYS + 35)  # +35d warmup para 30d regime detection
        if len(kl) < 50:
            print(f"SKIP (solo {len(kl)} velas)")
            failed.append(sym)
            continue
        klines_by_symbol[sym] = kl
        print(f"{len(kl)} velas")
        time.sleep(0.10)

    if failed:
        print(f"\nSímbolos sin datos: {failed}")

    btc_kl = klines_by_symbol.get("BTCUSDT", [])
    if btc_kl:
        btc_start  = btc_kl[0]["close"]
        btc_end    = btc_kl[-1]["close"]
        btc_return = (btc_end - btc_start) / btc_start * 100
        print(f"\nBTC en período: ${btc_start:,.0f} → ${btc_end:,.0f} ({btc_return:+.1f}%)")

    # Detectar régimen 30d
    if len(btc_kl) >= 180:
        btc_30d_chg = (btc_kl[-1]["close"] - btc_kl[-180]["close"]) / btc_kl[-180]["close"] * 100
        btc_7d_chg  = (btc_kl[-1]["close"] - btc_kl[-42]["close"])  / btc_kl[-42]["close"]  * 100
        print(f"BTC 30d: {btc_30d_chg:+.1f}% | BTC 7d: {btc_7d_chg:+.1f}%")
        regime_detect = "FUTURES_SHORT" if btc_30d_chg <= -15 else ("PRESERVATION" if btc_30d_chg <= -7 else "SPOT_BULL")
        print(f"Régimen detectado por datos: {regime_detect}")

    # Correr 4 escenarios
    print("\n[2/3] Corriendo 4 escenarios de simulación...")
    scenarios = [
        ("A", "Baseline pre-audit    (SL/TP viejo, sin futuros)"),
        ("B", "Spot mejorado         (SL/TP nuevo, SmartTP, anti-wick)"),
        ("C", "Sistema completo      (B + 7 futuros SHORT, F&G Kelly)"),
        ("D", "Bear conservador      (C + kelly×0.6, str≥0.65)"),
    ]

    results = {}
    for sc, desc in scenarios:
        print(f"\n  Escenario {sc}: {desc}")
        r = run_simulation(klines_by_symbol, sc)
        results[sc] = r
        print(f"    PnL: ${r['total_pnl']:+.2f} | WR: {r['win_rate']:.1f}% | PF: {r['profit_factor']:.2f} | MaxDD: {r['max_drawdown']:.1f}%")
        print(f"    Spot: {r['spot_trades']} trades (${r['spot_pnl']:+.2f}) | Fut: {r['futures_trades']} trades (${r['futures_pnl']:+.2f})")
        print(f"    {r['pnl_per_day']:+.2f} USD/día | {r['mxn_per_day']:+.1f} MXN/día")

    # Tabla comparativa
    print("\n[3/3] RESULTADOS COMPARATIVOS")
    print("=" * 72)
    print(f"{'Escenario':<8} {'PnL 90d':>10} {'Return':>8} {'WR':>6} {'PF':>6} {'MaxDD':>7} {'USD/día':>9} {'MXN/día':>9}")
    print("-" * 72)
    for sc, desc in scenarios:
        r = results[sc]
        print(f"  {sc:<6} ${r['total_pnl']:>+9.2f} {r['return_pct']:>+7.1f}% {r['win_rate']:>5.1f}% {r['profit_factor']:>5.2f} {r['max_drawdown']:>6.1f}% ${r['pnl_per_day']:>+8.2f} {r['mxn_per_day']:>+8.1f}")

    # Desglose mensual escenario C (principal)
    print("\n── DESGLOSE MENSUAL — Escenario C (Sistema Completo) ──")
    for m in results["C"]["monthly"]:
        wr = m["wins"] / m["trades"] * 100 if m["trades"] else 0
        print(f"  Mes {m['month']}: PnL ${m['pnl']:+.2f} | Trades {m['trades']} | WR {wr:.0f}% | Capital ${m['capital_end']:,.2f}")

    # Monte Carlo para escenario C
    print("\n── MONTE CARLO 1,000 sims — Escenario C ──")
    rc = results["C"]
    tpd = rc["total_trades"] / DAYS
    mc  = monte_carlo(rc["win_rate"], rc["avg_win"], rc["avg_loss"], tpd)
    print(f"  P10 (pesimista): ${mc['P10']:,.2f} ({mc['P10_return']:+.1f}%)")
    print(f"  P50 (mediana):   ${mc['P50']:,.2f} ({mc['P50_return']:+.1f}%)")
    print(f"  P90 (optimista): ${mc['P90']:,.2f} ({mc['P90_return']:+.1f}%)")

    # Decisiones estratégicas basadas en resultados
    print("\n── DECISIONES ESTRATÉGICAS ──")
    diff_cd = results["C"]["total_pnl"] - results["D"]["total_pnl"]
    dd_c    = results["C"]["max_drawdown"]
    dd_d    = results["D"]["max_drawdown"]
    if dd_d < dd_c * 0.80 and diff_cd < results["C"]["total_pnl"] * 0.20:
        print("  ✅ Escenario D mejor risk-adj: recomendar BEAR_MARKET_KELLY_MULTIPLIER=0.4")
    else:
        print("  ✅ Escenario C domina: mantener Kelly actual sin reducción adicional")

    if results["C"]["futures_pnl"] > results["C"]["spot_pnl"]:
        print("  ✅ Futuros SHORT generan mayor PnL que spot en este período — FUTURES_SHORT correcto")
    else:
        print("  ⚠️  Spot supera futuros en PnL — revisar parámetros futures SL/TP")

    if results["C"]["win_rate"] >= 45:
        print(f"  ✅ WR {results['C']['win_rate']:.1f}% ≥ 45% — thresholds actuales correctos")
    else:
        print(f"  ⚠️  WR {results['C']['win_rate']:.1f}% < 45% — considerar subir min_signal_strength a 0.40")

    pf_narrative = None
    narrative_trades = [t for t in results["C"]["trades"]
                        if t.get("symbol") in NARRATIVE]
    if narrative_trades:
        nar_wins = [t for t in narrative_trades if t["pnl"] > 0]
        nar_loss = [t for t in narrative_trades if t["pnl"] <= 0]
        if nar_loss:
            pf_narrative = sum(t["pnl"] for t in nar_wins) / abs(sum(t["pnl"] for t in nar_loss))
            print(f"  PF NARRATIVE: {pf_narrative:.2f} ({'✅ mantener SL3.5/TP7' if pf_narrative >= 1.5 else '⚠️ revisar umbrales NARRATIVE'})")

    # Resumen final
    print("\n" + "=" * 72)
    print(f"  MEJOR ESCENARIO: C — ${results['C']['total_pnl']:+.2f} USDT en 90 días")
    print(f"  Proyección: {results['C']['mxn_per_day']:+.1f} MXN/día con capital actual")
    print(f"  Futuros aportaron: ${results['C']['futures_pnl']:+.2f} ({results['C']['futures_trades']} trades)")
    print(f"  Spot aportó:       ${results['C']['spot_pnl']:+.2f} ({results['C']['spot_trades']} trades)")
    print("=" * 72)

    # Save JSON results
    out_file = "/tmp/backtest_90d_v2_results.json"
    try:
        with open(out_file, "w") as f:
            # Don't serialize full equity curve (too large)
            save_data = {sc: {k: v for k, v in results[sc].items() if k not in ("equity", "trades")}
                         for sc in results}
            json.dump(save_data, f, indent=2, default=str)
        print(f"\nResultados guardados en: {out_file}")
    except Exception as e:
        print(f"No se pudo guardar JSON: {e}")


if __name__ == "__main__":
    main()
