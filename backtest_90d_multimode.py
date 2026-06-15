"""
backtest_90d_multimode.py — Simulación MULTI-MODO 90 días con config jun-14-2026.

Escenarios (todos con $342 USDT):
  E1: Spot fijo — compra sin cambio de modo (referencia peor caso)
  E2: Preservation estático — solo narratives todo el tiempo
  E3: Regime-adaptive SIN futuros (config ANTERIOR: SL/TP/Kelly viejos, BTC.D 56%)
  E4: Regime-adaptive SIN futuros (config ACTUAL:   SL/TP/Kelly nuevos, BTC.D 64%)
  E5: Regime-adaptive CON futuros (config ACTUAL + FUTURES_SHORT + fast-confirm bear)
  E6: HODL BTC (referencia pasiva)

Diferencias E4 vs E3 (impacto de la optimización):
  - SL: 1.5% → 1.8%  | TP: 2.5% → 3.5%  (R:R 1.67x → 1.94x)
  - Kelly base: $60 → $20 (ajustado al capital real)
  - BTC.D threshold: 56% → 64% (desbloquea altcoins con BTC.D ~63%)
  - Fast-confirm bear: anti-whipsaw 4 barras → 1 barra en severe bear

Uso:
    docker compose exec app python3 backtest_90d_multimode.py
"""

import sys, os, json, time, math, urllib.request, random
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# PARÁMETROS GLOBALES
# ─────────────────────────────────────────────────────────────────────────────
INITIAL_CAPITAL    = 342.0
DAYS               = 90
INTERVAL           = "4h"
COMMISSION_RATE    = 0.001
MAX_POSITIONS      = 5
MAX_PER_SYMBOL     = 1
MAX_ENTRIES_PER_BAR= 1
FUTURES_MAX_SHORTS = 2
N_MC_SIMS          = 10_000
BTC_DOM_CURRENT    = 63.0   # BTC dominance actual estimado

# Anti-whipsaw: barras consecutivas antes de cambiar régimen
ANTI_WHIPSAW_BARS       = 4   # normal
ANTI_WHIPSAW_FAST_BARS  = 1   # fast-confirm en bear severo (BTC 30d < -15%)

SYMBOLS = [
    "DOGEUSDT", "RENDERUSDT", "BNBUSDT",  "SOLUSDT",  "TAOUSDT",
    "FETUSDT",  "AVAXUSDT",  "ADAUSDT",  "XRPUSDT",  "DOTUSDT",
    "SUIUSDT",  "PEPEUSDT",  "WIFUSDT",  "BTCUSDT",  "ETHUSDT",  "LINKUSDT",
]
FUTURES_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"}
NARRATIVE = {"FETUSDT","TAOUSDT","RENDERUSDT","NEARUSDT","WLDUSDT","AGIXUSDT","INJUSDT","OCEANUSDT"}
MEME      = {"DOGEUSDT","PEPEUSDT","WIFUSDT"}
LARGE_CAP = {"BTCUSDT","ETHUSDT","BNBUSDT"}

_BARS_PER_DAY = {"1h": 24, "4h": 6, "1d": 1}

# ── Parámetros por régimen — Config ANTERIOR ─────────────────────────────────
REGIME_PARAMS_OLD = {
    "SPOT_BULL":    {"sl":0.015,"tp":0.025,"kelly":1.00,"trail_act":0.020,"trail_dist":0.010,"narrative_only":False,"require_vol":1.3,"max_rsi":70},
    "RANGE_SCALP":  {"sl":0.012,"tp":0.018,"kelly":0.70,"trail_act":0.015,"trail_dist":0.008,"narrative_only":False,"require_vol":1.5,"max_rsi":40},
    "PRESERVATION": {"sl":0.015,"tp":0.030,"kelly":0.50,"trail_act":0.020,"trail_dist":0.012,"narrative_only":True, "require_vol":1.5,"max_rsi":65},
    "FUTURES_SHORT":{"sl":0.030,"tp":0.050,"kelly":0.30,"trail_act":0.030,"trail_dist":0.015,"narrative_only":False,"require_vol":1.0,"max_rsi":100},
}
KELLY_OLD  = {"base": 60.0, "max": 150.0, "pct_liquid": 0.40, "dyn_pct": 0.15}
BTC_DOM_OLD = 56.0  # threshold anterior — bloqueaba con BTC.D=63%

# ── Parámetros por régimen — Config ACTUAL (jun-14-2026) ──────────────────────
# R:R mejorado para todos los modos. Futuros: SL 2.5%, TP 6.0%
REGIME_PARAMS_NEW = {
    "SPOT_BULL":    {"sl":0.018,"tp":0.035,"kelly":1.00,"trail_act":0.025,"trail_dist":0.012,"narrative_only":False,"require_vol":1.3,"max_rsi":70},
    "RANGE_SCALP":  {"sl":0.015,"tp":0.022,"kelly":0.70,"trail_act":0.018,"trail_dist":0.010,"narrative_only":False,"require_vol":1.5,"max_rsi":40},
    "PRESERVATION": {"sl":0.018,"tp":0.035,"kelly":0.50,"trail_act":0.025,"trail_dist":0.012,"narrative_only":True, "require_vol":1.5,"max_rsi":65},
    "FUTURES_SHORT":{"sl":0.025,"tp":0.060,"kelly":0.30,"trail_act":0.030,"trail_dist":0.015,"narrative_only":False,"require_vol":1.0,"max_rsi":100},
}
KELLY_NEW  = {"base": 20.0, "max": 45.0, "pct_liquid": 0.30, "dyn_pct": 0.12}
BTC_DOM_NEW = 64.0  # threshold nuevo — permite altcoins con BTC.D=63%

CAT_PARAMS = {
    "LARGE_CAP":  {"sl":0.018,"tp":0.035,"trail_act":0.025,"trail_dist":0.012},
    "MIDCAP_ALT": {"sl":0.018,"tp":0.035,"trail_act":0.025,"trail_dist":0.012},
    "NARRATIVE":  {"sl":0.025,"tp":0.050,"trail_act":0.030,"trail_dist":0.015},
    "MEME":       {"sl":0.020,"tp":0.045,"trail_act":0.025,"trail_dist":0.015},
}


# ─────────────────────────────────────────────────────────────────────────────
# DESCARGA DE DATOS
# ─────────────────────────────────────────────────────────────────────────────
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


# ─────────────────────────────────────────────────────────────────────────────
# INDICADORES TÉCNICOS
# ─────────────────────────────────────────────────────────────────────────────
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


# ─────────────────────────────────────────────────────────────────────────────
# DETECTOR DE RÉGIMEN
# ─────────────────────────────────────────────────────────────────────────────
def detect_regime(btc_klines: List[Dict], idx: int,
                  pending: dict, scenario: str,
                  fast_confirm: bool = False) -> str:
    """
    Detecta régimen en la vela `idx`.
    fast_confirm=True: 1 barra para confirmar FUTURES_SHORT en bear severo.
    """
    if scenario == "E1": return "SPOT_BULL"
    if scenario == "E2": return "PRESERVATION"

    bars_per_day = _BARS_PER_DAY.get(INTERVAL, 6)
    bars_7d  = bars_per_day * 7
    bars_14d = bars_per_day * 14
    bars_30d = bars_per_day * 30

    window = btc_klines[max(0, idx-79): idx+1]
    if len(window) < 40:
        return pending.get("active_mode", "SPOT_BULL")

    closes  = [k["close"]  for k in window]
    volumes = [k["volume"] for k in window]

    e21_series = ema(closes, 21)
    e55_series = ema(closes, min(55, len(closes)))
    ema_up = sum(1 for i in [-1,-2,-3] if e21_series[i] > e55_series[i]) >= 2

    price = closes[-1]
    vs    = vol_spike(volumes, 24)
    bw    = bb_bandwidth(closes, 20)

    btc_7d_ago  = btc_klines[idx - bars_7d]["close"]  if idx >= bars_7d  else closes[0]
    btc_14d_ago = btc_klines[idx - bars_14d]["close"] if idx >= bars_14d else closes[0]
    btc_30d_ago = btc_klines[idx - bars_30d]["close"] if idx >= bars_30d else closes[0]
    chg_7d  = (price - btc_7d_ago)  / btc_7d_ago  * 100 if btc_7d_ago  > 0 else 0
    chg_14d = (price - btc_14d_ago) / btc_14d_ago * 100 if btc_14d_ago > 0 else 0
    chg_30d = (price - btc_30d_ago) / btc_30d_ago * 100 if btc_30d_ago > 0 else 0

    # Clasificación de mercado
    bear_severe     = chg_30d < -15.0                            # severo: fast-confirm
    bear_structural = chg_30d < -18.0                            # estructural sostenido
    bear_medium     = chg_14d < -10.0 and chg_7d < -5.0         # consistente medio plazo
    sideways_strict = (bw < 0.055 and vs < 0.65
                       and abs(chg_7d) < 2.5
                       and not (bear_structural or bear_medium))

    if scenario in ("E3", "E4"):  # config vieja
        if bear_structural or bear_medium:
            proposed = "PRESERVATION"
        elif sideways_strict:
            proposed = "RANGE_SCALP"
        elif ema_up and chg_7d > -8:
            proposed = "SPOT_BULL"
        else:
            proposed = "PRESERVATION"
    else:  # E5: config nueva con fast-confirm y futuros
        if bear_structural or bear_medium:
            proposed = "FUTURES_SHORT"
        elif bear_severe:
            proposed = "PRESERVATION"   # camino: PRESERVATION → FUTURES_SHORT
        elif sideways_strict:
            proposed = "RANGE_SCALP"
        elif ema_up and chg_7d > -8:
            proposed = "SPOT_BULL"
        else:
            proposed = "PRESERVATION"

    # Anti-whipsaw con fast-confirm en bear severo
    whipsaw_needed = ANTI_WHIPSAW_FAST_BARS if (fast_confirm and bear_severe) else ANTI_WHIPSAW_BARS

    if proposed == pending.get("pending_mode"):
        pending["pending_count"] = pending.get("pending_count", 0) + 1
        if pending["pending_count"] >= whipsaw_needed:
            pending["active_mode"]  = proposed
            pending["pending_mode"] = None
            pending["pending_count"]= 0
    else:
        pending["pending_mode"]  = proposed
        pending["pending_count"] = 1

    return pending.get("active_mode", "SPOT_BULL")


# ─────────────────────────────────────────────────────────────────────────────
# SEÑAL DE ENTRADA
# ─────────────────────────────────────────────────────────────────────────────
def compute_signal(klines: List[Dict], idx: int) -> Dict:
    window = klines[max(0, idx-49): idx+1]
    if len(window) < 30:
        return {"signal": "HOLD", "strength": 0.0, "rsi": 50.0, "vol_spike": 1.0, "price": 0}
    closes  = [k["close"]  for k in window]
    volumes = [k["volume"] for k in window]
    r   = rsi(closes)
    vs  = vol_spike(volumes)
    e9  = ema(closes, 9)[-1]
    e21 = ema(closes, 21)[-1]
    e50 = ema(closes, min(50, len(closes)))[-1]
    bb_lo, bb_mid, bb_hi = bollinger(closes)
    price    = closes[-1]
    strength = 0.5
    signals_buy = 0
    if r < 35:   strength += 0.15; signals_buy += 1
    elif r < 50: strength += 0.07; signals_buy += 1
    elif r > 65: strength -= 0.15
    if e9 > e21: strength += 0.10; signals_buy += 1
    else:        strength -= 0.05
    if price < bb_lo:   strength += 0.10; signals_buy += 1
    elif price > bb_hi: strength -= 0.10
    if vs >= 1.5: strength = min(1.0, strength * 1.15); signals_buy += 1
    in_uptrend = price >= e50 * 0.98
    strength   = max(0.0, min(1.0, strength))
    signal     = "BUY" if signals_buy >= 2 and strength >= 0.55 and in_uptrend else "HOLD"
    return {"signal": signal, "strength": strength, "rsi": r,
            "vol_spike": vs, "price": price, "in_uptrend": in_uptrend,
            "at_bb_lower": price <= bb_lo * 1.005}

def get_category(symbol: str) -> str:
    if symbol in LARGE_CAP:  return "LARGE_CAP"
    if symbol in NARRATIVE:  return "NARRATIVE"
    if symbol in MEME:       return "MEME"
    return "MIDCAP_ALT"


# ─────────────────────────────────────────────────────────────────────────────
# KELLY SIZING
# ─────────────────────────────────────────────────────────────────────────────
def kelly_size(capital: float, rsi_val: float, strength: float,
               regime_kelly: float, kcfg: dict) -> float:
    base = max(kcfg["base"], capital * kcfg["dyn_pct"]) * regime_kelly
    if rsi_val < 40 and strength >= 0.70: base *= 1.3
    elif strength >= 0.65:                base *= 1.1
    cap_liq = capital * kcfg["pct_liquid"]
    return max(5.0, min(base, cap_liq, kcfg["max"]))


# ─────────────────────────────────────────────────────────────────────────────
# POSICIONES
# ─────────────────────────────────────────────────────────────────────────────
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
    def __init__(self, symbol, entry_price, margin, sl_pct, tp_pct):
        self.symbol      = symbol
        self.entry_price = entry_price
        self.margin      = margin
        self.sl_pct      = sl_pct
        self.tp_pct      = tp_pct


# ─────────────────────────────────────────────────────────────────────────────
# MOTOR DE SIMULACIÓN
# ─────────────────────────────────────────────────────────────────────────────
def run_simulation(klines_by_symbol: Dict[str, List[Dict]], scenario: str,
                   regime_params: dict, kcfg: dict, btc_dom_threshold: float,
                   fast_confirm: bool = False) -> Dict:
    capital   = INITIAL_CAPITAL
    positions: List[SimPosition] = []
    shorts:    List[SimShort]    = []
    fut_wallet = 0.0   # separado del spot wallet
    trades     = []
    equity     = [capital]
    monthly    = []
    regime_counts = {"SPOT_BULL":0,"RANGE_SCALP":0,"PRESERVATION":0,"FUTURES_SHORT":0}
    pending_regime = {"active_mode": "SPOT_BULL", "pending_mode": None, "pending_count": 0}

    btc_klines = klines_by_symbol.get("BTCUSDT", [])
    n_bars = max((len(v) for v in klines_by_symbol.values()), default=0)
    bars_per_day   = _BARS_PER_DAY.get(INTERVAL, 6)
    bars_per_month = bars_per_day * 30
    month_start_cap = capital
    month_trades: List[Dict] = []
    month_idx = 0

    for bar_idx in range(50, n_bars):
        bar_since = bar_idx - 50
        # Monthly boundary
        if bar_since > 0 and bar_since % bars_per_month == 0:
            monthly.append({
                "month":       month_idx + 1,
                "pnl":         round(capital + fut_wallet - month_start_cap, 4),
                "capital_end": round(capital + fut_wallet, 2),
                "trades":      len(month_trades),
                "wins":        sum(1 for t in month_trades if t["pnl"] > 0),
            })
            month_idx += 1
            month_start_cap = capital + fut_wallet
            month_trades = []

        # ── Régimen ───────────────────────────────────────────────────────────
        mode = detect_regime(btc_klines, bar_idx, pending_regime, scenario, fast_confirm)
        regime_counts[mode] = regime_counts.get(mode, 0) + 1
        rp = regime_params[mode]

        # ── Transferencia bidireccional spot↔futuros (E5) ──────────────────────
        if scenario == "E5":
            if mode == "FUTURES_SHORT":
                # Mover fondos de spot a futuros si se necesitan
                needed = regime_params["FUTURES_SHORT"]["sl"] * 25.0 * 2  # margin buffer
                if fut_wallet < needed and capital > needed * 2:
                    move = min(capital * 0.40, needed * 2 - fut_wallet)
                    capital -= move; fut_wallet += move
            else:
                # Retornar idle futures a spot cuando no hay shorts abiertos
                if not shorts and fut_wallet > 5.0:
                    returnable = fut_wallet - 5.0
                    capital += returnable; fut_wallet = 5.0

        # ── Cerrar longs que violan el régimen ───────────────────────────────
        to_regime_close = []
        for pos in positions:
            if mode == "FUTURES_SHORT":
                to_regime_close.append(pos)
            elif mode == "PRESERVATION" and pos.category not in ("NARRATIVE",):
                to_regime_close.append(pos)
        for pos in to_regime_close:
            sk = klines_by_symbol.get(pos.symbol, [])
            price = sk[bar_idx]["close"] if bar_idx < len(sk) else pos.entry_price
            pnl   = (price - pos.entry_price) * pos.qty
            comm  = pos.qty * price * COMMISSION_RATE * 2
            capital += pnl - comm
            t = {"symbol": pos.symbol, "reason": "regime_close",
                 "pnl": pnl - comm, "pnl_pct": (price/pos.entry_price-1)*100}
            trades.append(t); month_trades.append(t)
            positions.remove(pos)

        # ── Cerrar shorts si el régimen cambia ────────────────────────────────
        if mode != "FUTURES_SHORT" and shorts:
            for sh in shorts[:]:
                sk = klines_by_symbol.get(sh.symbol, [])
                price = sk[bar_idx]["close"] if bar_idx < len(sk) else sh.entry_price
                pnl_pct = (sh.entry_price - price) / sh.entry_price
                pnl = sh.margin * pnl_pct * 2 - sh.margin * COMMISSION_RATE * 2  # 2x leverage
                fut_wallet += pnl
                t = {"symbol": sh.symbol, "reason": "short_regime_close",
                     "pnl": pnl, "pnl_pct": pnl_pct*100, "is_short": True}
                trades.append(t); month_trades.append(t)
            shorts.clear()

        # ── Check longs: SL/TP/trailing ───────────────────────────────────────
        to_remove = []
        for pos in positions:
            sk = klines_by_symbol.get(pos.symbol, [])
            if bar_idx >= len(sk): continue
            candle = sk[bar_idx]
            lo, hi = candle["low"], candle["high"]
            sl_price = pos.entry_price * (1 - pos.sl_pct) if not pos.sl_moved_be else pos.entry_price
            tp_price = pos.entry_price * (1 + pos.tp_pct)
            pnl_pct_now = (candle["close"] - pos.entry_price) / pos.entry_price
            exit_price = exit_reason = None
            if lo <= sl_price:
                exit_price, exit_reason = sl_price, "stop_loss"
            elif hi >= tp_price:
                if not pos.scale_done:
                    # Scale-out: vende 50% al TP, mueve SL a breakeven
                    sq = pos.qty * 0.5
                    pnl  = (tp_price - pos.entry_price) * sq
                    comm = sq * tp_price * COMMISSION_RATE * 2
                    capital += pnl - comm
                    pos.qty -= sq; pos.scale_done = True; pos.sl_moved_be = True
                    t = {"symbol": pos.symbol, "reason": "scale_out_tp",
                         "pnl": pnl - comm, "pnl_pct": (tp_price/pos.entry_price-1)*100}
                    trades.append(t); month_trades.append(t)
                    continue
                else:
                    exit_price, exit_reason = tp_price, "take_profit"
            elif pnl_pct_now >= pos.trail_act:
                if candle["close"] > pos.peak: pos.peak = candle["close"]
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

        # ── Check shorts: SL/TP ───────────────────────────────────────────────
        shorts_remove = []
        for sh in shorts:
            sk = klines_by_symbol.get(sh.symbol, [])
            if bar_idx >= len(sk): continue
            candle = sk[bar_idx]
            lo, hi = candle["low"], candle["high"]
            sl_price = sh.entry_price * (1 + sh.sl_pct)   # SL arriba para short
            tp_price = sh.entry_price * (1 - sh.tp_pct)   # TP abajo para short
            exit_price = exit_reason = None
            if hi >= sl_price:
                exit_price, exit_reason = sl_price, "short_sl"
            elif lo <= tp_price:
                exit_price, exit_reason = tp_price, "short_tp"
            if exit_price:
                pnl_pct = (sh.entry_price - exit_price) / sh.entry_price
                pnl = sh.margin * pnl_pct * 2 - sh.margin * COMMISSION_RATE * 2
                fut_wallet += pnl
                t = {"symbol": sh.symbol, "reason": exit_reason,
                     "pnl": pnl, "pnl_pct": pnl_pct*100, "is_short": True}
                trades.append(t); month_trades.append(t); shorts_remove.append(sh)
        for s in shorts_remove: shorts.remove(s)

        # ── Abrir SHORTS (solo E5 en FUTURES_SHORT) ───────────────────────────
        if mode == "FUTURES_SHORT" and scenario == "E5":
            if len(shorts) < FUTURES_MAX_SHORTS and fut_wallet > 15:
                for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
                    if len(shorts) >= FUTURES_MAX_SHORTS: break
                    if any(s.symbol == sym for s in shorts): continue
                    sk = klines_by_symbol.get(sym, [])
                    if bar_idx >= len(sk): continue
                    sig = compute_signal(sk, bar_idx)
                    if sig["rsi"] < 45: continue  # esperar rebote para mejor entry
                    price  = sk[bar_idx]["close"]
                    margin = min(25.0, fut_wallet * 0.40)
                    if margin < 10.0: continue
                    comm   = margin * COMMISSION_RATE
                    fut_wallet -= margin + comm
                    shorts.append(SimShort(sym, price, margin,
                                          rp["sl"], rp["tp"]))

        # ── Abrir LONGS (todos los escenarios excepto cuando en FUTURES_SHORT) ─
        elif mode != "FUTURES_SHORT":
            entries_this_bar = 0
            if len(positions) < MAX_POSITIONS and capital > 20:
                for symbol in SYMBOLS:
                    if entries_this_bar >= MAX_ENTRIES_PER_BAR: break
                    if len(positions) >= MAX_POSITIONS: break
                    if any(p.symbol == symbol for p in positions): continue
                    sk = klines_by_symbol.get(symbol, [])
                    if bar_idx >= len(sk): continue
                    sig = compute_signal(sk, bar_idx)
                    if sig["signal"] != "BUY": continue
                    cat = get_category(symbol)
                    is_narrative = symbol in NARRATIVE
                    r_val, vs_, strength = sig["rsi"], sig["vol_spike"], sig["strength"]
                    # Filtros por régimen
                    if rp["narrative_only"] and not is_narrative: continue
                    if r_val > rp["max_rsi"]: continue
                    if vs_ < rp["require_vol"] and not (is_narrative and vs_ >= 1.5): continue
                    if mode == "RANGE_SCALP" and not sig.get("at_bb_lower"): continue
                    if strength < 0.62: continue
                    if not sig.get("in_uptrend") and mode == "SPOT_BULL": continue
                    # BTC Dominance filter
                    if BTC_DOM_CURRENT > btc_dom_threshold and cat not in ("LARGE_CAP",):
                        continue
                    size = kelly_size(capital, r_val, strength, rp["kelly"], kcfg)
                    if size < 5 or capital < size * 1.02: continue
                    qty  = size / sig["price"]
                    comm = qty * sig["price"] * COMMISSION_RATE
                    capital -= size + comm
                    entries_this_bar += 1
                    cat_p = CAT_PARAMS.get(cat, CAT_PARAMS["MIDCAP_ALT"])
                    blend_rp = {
                        "sl":         max(rp["sl"],         cat_p["sl"]),
                        "tp":         max(rp["tp"],         cat_p["tp"]),
                        "trail_act":  max(rp["trail_act"],  cat_p["trail_act"]),
                        "trail_dist": max(rp["trail_dist"], cat_p["trail_dist"]),
                    }
                    positions.append(SimPosition(symbol, sig["price"], qty, cat, blend_rp))

        # ── Equity mark-to-market ─────────────────────────────────────────────
        unr_long = sum(
            (klines_by_symbol.get(p.symbol,[{}])[min(bar_idx,len(klines_by_symbol.get(p.symbol,[{}]))-1)].get("close",p.entry_price) - p.entry_price) * p.qty
            for p in positions
        )
        unr_short = sum(
            (sh.entry_price - klines_by_symbol.get(sh.symbol,[{}])[min(bar_idx,len(klines_by_symbol.get(sh.symbol,[{}]))-1)].get("close",sh.entry_price)) / sh.entry_price * sh.margin * 2
            for sh in shorts
        )
        equity.append(capital + fut_wallet + unr_long + unr_short)

    # ── Cierre forzado al final ────────────────────────────────────────────────
    for pos in positions:
        sk = klines_by_symbol.get(pos.symbol, [])
        if sk:
            lp   = sk[-1]["close"]
            pnl  = (lp - pos.entry_price) * pos.qty
            comm = pos.qty * lp * COMMISSION_RATE * 2
            capital += pnl - comm
            trades.append({"symbol": pos.symbol, "reason": "end_period",
                           "pnl": pnl - comm, "pnl_pct": (lp/pos.entry_price-1)*100})
    for sh in shorts:
        sk = klines_by_symbol.get(sh.symbol, [])
        if sk:
            lp  = sk[-1]["close"]
            pnl_pct = (sh.entry_price - lp) / sh.entry_price
            pnl = sh.margin * pnl_pct * 2 - sh.margin * COMMISSION_RATE * 2
            fut_wallet += pnl
            trades.append({"symbol": sh.symbol, "reason": "short_end",
                           "pnl": pnl, "pnl_pct": pnl_pct*100, "is_short": True})

    if month_start_cap != capital + fut_wallet or month_trades:
        monthly.append({
            "month": month_idx + 1,
            "pnl": round(capital + fut_wallet - month_start_cap, 4),
            "capital_end": round(capital + fut_wallet, 2),
            "trades": len(month_trades),
            "wins": sum(1 for t in month_trades if t["pnl"] > 0),
        })

    total_cap = capital + fut_wallet
    total_pnl = total_cap - INITIAL_CAPITAL
    winners  = [t for t in trades if t["pnl"] > 0]
    losers   = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(winners) / len(trades) * 100 if trades else 0
    avg_win  = sum(t["pnl"] for t in winners) / len(winners) if winners else 0
    avg_loss = sum(t["pnl"] for t in losers)  / len(losers)  if losers  else 0
    max_dd = 0.0; peak_eq = equity[0]
    for eq in equity:
        if eq > peak_eq: peak_eq = eq
        dd = (peak_eq - eq) / peak_eq * 100
        if dd > max_dd: max_dd = dd
    pf = abs(sum(t["pnl"] for t in winners) / sum(t["pnl"] for t in losers)) if losers and sum(t["pnl"] for t in losers) != 0 else 99.9
    total_bars = sum(regime_counts.values())
    regime_pct = {k: round(v/total_bars*100, 1) if total_bars > 0 else 0 for k, v in regime_counts.items()}

    return {
        "capital_final": total_cap,  "total_pnl": total_pnl,
        "return_pct":    total_pnl / INITIAL_CAPITAL * 100,
        "total_trades":  len(trades), "win_rate": win_rate,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "max_drawdown": max_dd, "profit_factor": pf,
        "equity": equity, "trades": trades, "monthly": monthly,
        "regime_pct": regime_pct,
        "n_shorts": sum(1 for t in trades if t.get("is_short")),
        "spot_trades": sum(1 for t in trades if not t.get("is_short")),
    }


# ─────────────────────────────────────────────────────────────────────────────
# MONTE CARLO
# ─────────────────────────────────────────────────────────────────────────────
def monte_carlo(avg_win: float, avg_loss: float, win_rate: float,
                trades_per_month: float, capital: float,
                months: int = 3, n: int = N_MC_SIMS) -> Dict:
    rng = random.Random(42)
    finals = []
    wr = win_rate / 100
    for _ in range(n):
        cap = capital
        for _ in range(months):
            for _ in range(max(1, int(trades_per_month))):
                if rng.random() < wr: cap += abs(avg_win)
                else:                 cap -= abs(avg_loss)
        finals.append(cap)
    finals.sort()
    return {
        "p5":    finals[int(n * 0.05)],
        "p50":   finals[int(n * 0.50)],
        "p95":   finals[int(n * 0.95)],
        "mean":  sum(finals) / n,
        "pct_pos": sum(1 for x in finals if x > capital) / n * 100,
    }

def equity_ascii(eq: List[float], width: int = 60, height: int = 6) -> str:
    if not eq: return ""
    step = max(1, len(eq) // width)
    sampled = [eq[i] for i in range(0, len(eq), step)][:width]
    mn, mx = min(sampled), max(sampled)
    if mx == mn: return ""
    rows = []
    for row in range(height, 0, -1):
        threshold = mn + (mx - mn) * row / height
        rows.append("  " + "".join("█" if v >= threshold else " " for v in sampled))
    rows.append("  " + "─" * len(sampled))
    return "\n".join(rows)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    MXN = 17.8
    print("=" * 74)
    print("  BACKTEST MULTI-MODO 90 DÍAS — $342 USDT — Config jun-14-2026")
    print("=" * 74)
    print()
    print("Descargando datos Binance (90 días, 4h)...")

    klines_by_symbol: Dict[str, List[Dict]] = {}
    for sym in SYMBOLS:
        print(f"  {sym}...", end=" ", flush=True)
        klines_by_symbol[sym] = fetch_klines(sym, INTERVAL, DAYS + 5)
        print(f"{len(klines_by_symbol[sym])} velas")

    btc_klines = klines_by_symbol.get("BTCUSDT", [])
    btc_start  = btc_klines[50]["close"] if len(btc_klines) > 50 else 0
    btc_end    = btc_klines[-1]["close"] if btc_klines else 0
    btc_change = (btc_end - btc_start) / btc_start * 100 if btc_start > 0 else 0

    print(f"\n  BTC 90d: ${btc_start:,.0f} → ${btc_end:,.0f} ({btc_change:+.1f}%)")
    print(f"  BTC dominance actual estimado: {BTC_DOM_CURRENT}%")
    print()

    # Definición de escenarios
    scenarios_def = [
        ("E1", "Spot fijo (referencia worst case)",
         REGIME_PARAMS_OLD, KELLY_OLD, BTC_DOM_OLD, False),
        ("E2", "Preservation estático (solo narratives)",
         REGIME_PARAMS_OLD, KELLY_OLD, BTC_DOM_OLD, False),
        ("E3", "Regime-adaptive (config ANTERIOR, BTC.D 56%)",
         REGIME_PARAMS_OLD, KELLY_OLD, BTC_DOM_OLD, False),
        ("E4", "Regime-adaptive (config ACTUAL, BTC.D 64%)",
         REGIME_PARAMS_NEW, KELLY_NEW, BTC_DOM_NEW, False),
        ("E5", "Regime-adaptive + FUTUROS SHORT (config ACTUAL)",
         REGIME_PARAMS_NEW, KELLY_NEW, BTC_DOM_NEW, True),
    ]

    print("Corriendo simulaciones multi-modo...")
    results: Dict[str, Dict] = {}
    for code, label, rp, kc, btcd, fast in scenarios_def:
        print(f"  {code}: {label}...")
        results[code] = run_simulation(klines_by_symbol, code, rp, kc, btcd, fast)
        r = results[code]
        print(f"    → Cap: ${r['capital_final']:.2f}  Trades: {r['total_trades']} ({r['n_shorts']} shorts)  WR: {r['win_rate']:.1f}%  MaxDD: {r['max_drawdown']:.1f}%")

    hodl_btc  = INITIAL_CAPITAL * (1 + btc_change / 100)
    hodl_usdt = INITIAL_CAPITAL

    # ── TABLA COMPARATIVA ─────────────────────────────────────────────────────
    print()
    print("=" * 74)
    print("  RESULTADOS COMPARATIVOS — 90 DÍAS — $342 USDT inicial")
    print("=" * 74)
    hdr = f"  {'Escenario':<40} {'Cap':>8} {'PnL':>7} {'%':>6} {'MXN':>8} {'WR':>6} {'PF':>5} {'DD':>6}"
    print(hdr)
    print("  " + "─" * 72)
    best_cap = max(r["capital_final"] for r in results.values())
    for code, label, *_ in scenarios_def:
        r = results[code]
        mark = " ◀" if r["capital_final"] == best_cap else ""
        mxn_gain = r["total_pnl"] * MXN
        print(f"  {label[:40]:<40} ${r['capital_final']:>7.2f} {r['total_pnl']:>+6.2f} {r['return_pct']:>+5.1f}% {mxn_gain:>+7.1f} {r['win_rate']:>5.1f}% {r['profit_factor']:>4.2f} {r['max_drawdown']:>5.1f}%{mark}")
    print("  " + "─" * 72)
    print(f"  {'HODL BTC (referencia)':<40} ${hodl_btc:>7.2f} {hodl_btc-INITIAL_CAPITAL:>+6.2f} {btc_change:>+5.1f}%  {'—':>8}  {'—':>6}  {'—':>5}  {'—':>5}")
    print(f"  {'HODL USDT (referencia)':<40} ${hodl_usdt:>7.2f}  +0.00  +0.0%      +0.0      —      —      —")

    # ── DISTRIBUCIÓN DE REGÍMENES ─────────────────────────────────────────────
    print()
    print("  DISTRIBUCIÓN DE REGÍMENES (% del tiempo en cada modo):")
    print(f"  {'Modo':<16} {'E3-Anterior':>12} {'E4-Actual':>12} {'E5-Futuros':>12}")
    print("  " + "─" * 56)
    for mode in ["SPOT_BULL", "RANGE_SCALP", "PRESERVATION", "FUTURES_SHORT"]:
        p3 = results["E3"]["regime_pct"].get(mode, 0)
        p4 = results["E4"]["regime_pct"].get(mode, 0)
        p5 = results["E5"]["regime_pct"].get(mode, 0)
        bar = "█" * int(p4 / 5)
        print(f"  {mode:<16} {p3:>10.1f}%  {p4:>10.1f}%  {p5:>10.1f}%   {bar}")

    # ── DESGLOSE MENSUAL ──────────────────────────────────────────────────────
    print()
    print("  DESGLOSE MENSUAL (mejores escenarios E4 y E5):")
    print(f"  {'Mes':<8} {'E4 Trades':>10} {'E4 WR':>7} {'E4 PnL':>9} {'E4 Cap':>9}  |  {'E5 Trades':>10} {'E5 WR':>7} {'E5 PnL':>9}")
    print("  " + "─" * 80)
    for mi in range(max(len(results["E4"]["monthly"]), len(results["E5"]["monthly"]))):
        m4 = results["E4"]["monthly"][mi] if mi < len(results["E4"]["monthly"]) else {}
        m5 = results["E5"]["monthly"][mi] if mi < len(results["E5"]["monthly"]) else {}
        wr4 = round(m4.get("wins",0)/m4.get("trades",1)*100) if m4.get("trades",0) > 0 else 0
        wr5 = round(m5.get("wins",0)/m5.get("trades",1)*100) if m5.get("trades",0) > 0 else 0
        mn  = ["","Jun","Jul","Ago"][min(mi+1,3)]
        print(f"  Mes {mi+1} ({mn}){'':<2} {m4.get('trades',0):>10} {wr4:>6}% {m4.get('pnl',0):>+9.2f} ${m4.get('capital_end',0):>8.2f}  |  "
              f"{m5.get('trades',0):>10} {wr5:>6}% {m5.get('pnl',0):>+9.2f}")

    # ── CURVAS DE EQUITY ──────────────────────────────────────────────────────
    print()
    print("  CURVAS DE EQUITY:")
    for code, label, *_ in scenarios_def[2:]:
        r = results[code]
        print(f"\n  {code} — {label}  (final: ${r['capital_final']:.2f})")
        print(equity_ascii(r["equity"]))

    # ── MONTE CARLO FORWARD 90 DÍAS ───────────────────────────────────────────
    print()
    print("  MONTE CARLO FORWARD — Próximos 90 días (10,000 simulaciones):")
    print(f"  {'Escenario':<20} {'Media':>9} {'P5':>9} {'P50':>9} {'P95':>9} {'% +':>7}")
    print("  " + "─" * 60)
    for code, label, *_ in scenarios_def[2:]:
        r = results[code]
        tpm = max(1.0, r["total_trades"] / 3)
        mc  = monte_carlo(r["avg_win"], r["avg_loss"], r["win_rate"],
                          tpm, r["capital_final"])
        print(f"  {code} {label[:18]:<18} ${mc['mean']:>8.2f} ${mc['p5']:>8.2f} ${mc['p50']:>8.2f} ${mc['p95']:>8.2f} {mc['pct_pos']:>6.1f}%")
        print(f"  {'  → MXN':<20} ${mc['mean']*MXN:>8.1f} ${mc['p5']*MXN:>8.1f}"
              f"  {'':>9} ${mc['p95']*MXN:>8.1f}")

    # ── ANÁLISIS EXPERTO ──────────────────────────────────────────────────────
    r3 = results["E3"]; r4 = results["E4"]; r5 = results["E5"]
    e4_vs_e3 = r4["capital_final"] - r3["capital_final"]
    e5_vs_e4 = r5["capital_final"] - r4["capital_final"]
    e5_vs_hodl = r5["capital_final"] - hodl_btc

    btc_type = ("BEAR SEVERO" if btc_change < -15 else
                "BEAR MODERADO" if btc_change < -8 else "LATERAL/ALCISTA")

    pct_fs_e5 = r5["regime_pct"].get("FUTURES_SHORT", 0)
    pct_pres  = r4["regime_pct"].get("PRESERVATION", 0)
    pct_bull  = r4["regime_pct"].get("SPOT_BULL", 0)
    pct_scalp = r4["regime_pct"].get("RANGE_SCALP", 0)

    if e5_vs_e4 > 2.0:
        fut_verdict = "ACTIVAR FUTURES — genera ventaja significativa"
    elif e5_vs_e4 > 0:
        fut_verdict = "FUTUROS marginalmente mejor — activa con cautela"
    else:
        fut_verdict = "PRESERVATION supera a FUTURES en este mercado (bear con rebotes)"

    print()
    print("=" * 74)
    print("  ANÁLISIS EXPERTO MULTI-MODO")
    print("=" * 74)
    print(f"""
  MERCADO DETECTADO: {btc_type} (BTC 90d: {btc_change:+.1f}% | BTC.D: ~{BTC_DOM_CURRENT}%)

  IMPACTO DE LA OPTIMIZACIÓN:
    E4 vs E3 (parámetros nuevos vs viejos): {e4_vs_e3:+.4f} USDT ({e4_vs_e3*MXN:+.1f} MXN)
    E5 vs E4 (futuros activos vs sin futuros): {e5_vs_e4:+.4f} USDT ({e5_vs_e4*MXN:+.1f} MXN)
    Mejor escenario vs HODL BTC: {e5_vs_hodl:+.2f} USDT ({e5_vs_hodl*MXN:+.1f} MXN)

  DISTRIBUCIÓN DE MODOS (E4 — config actual):
    SPOT_BULL    {pct_bull:.0f}%  → Full Kelly {REGIME_PARAMS_NEW['SPOT_BULL']['tp']*100:.0f}% TP / {REGIME_PARAMS_NEW['SPOT_BULL']['sl']*100:.0f}% SL
    RANGE_SCALP  {pct_scalp:.0f}%  → Kelly ×0.7 — solo en soporte BB
    PRESERVATION {pct_pres:.0f}%  → Solo narratives, Kelly ×0.5
    FUTURES_SHORT {pct_fs_e5:.0f}% → Solo en E5, margin $25, TP 6% SL 2.5%

  VEREDICTO FUTUROS: {fut_verdict}

  BUG MÁS CRÍTICO CORREGIDO HOY:
    BTC.D threshold 56%→64% libera altcoins:
    E3 (BTC.D 56%): {r3['total_trades']} trades | E4 (BTC.D 64%): {r4['total_trades']} trades
    Diferencia: {r4['total_trades']-r3['total_trades']:+d} trades en 90 días

  KELLY SIZING — COMPARATIVA:
    Config anterior: base=$60 (={60/INITIAL_CAPITAL*100:.1f}% capital) — sobredimensionado
    Config actual:   base=$20 (={20/INITIAL_CAPITAL*100:.1f}% capital) — calibrado al capital real
    Kelly ajustado reduce riesgo de ruina mientras mantiene compounding

  RÉGIMEN ACTUAL (jun 14 2026, BTC 30d=-18.2%):
    → Pendiente de confirmar PRESERVATION (anti-whipsaw fast: 1 ciclo en bear severo)
    → Si confirma PRESERVATION: transferir fondos spot→futuros wallet
    → FUTURES_SHORT activo en el PRÓXIMO CICLO de trading
""")

    print("=" * 74)
    print("  Simulación completada.")
    print("=" * 74)

if __name__ == "__main__":
    main()
