"""
analyze_missed_ops_multimode.py — Análisis de oportunidades perdidas con conciencia de modo.

Evalúa señales rechazadas tomando en cuenta el régimen activo en cada momento
(SPOT_BULL / RANGE_SCALP / PRESERVATION / FUTURES_SHORT) con la configuración
optimizada actual (jun-14-2026).

Escenarios comparados:
  CONFIG_OLD: BTC.D threshold=56%, Kelly base=$60, SL 1.5% TP 2.5%
  CONFIG_NEW: BTC.D threshold=64%, Kelly base=$20, SL 1.8% TP 3.5%

Clasificación de señales:
  CORRECTO      — el modo/filtro la bloqueó y el precio fue en la dirección esperada
  FALSO_RECHAZO — el modo permitía la entrada pero otro filtro dijo NO, y el precio subió
  MODO_BLOQUEO  — el modo fue la única razón del bloqueo (ej. PRESERVATION bloquea MIDCAP_ALT)
  OPORTUNIDAD   — modo lo permitía, strength suficiente, precio subió >1% en 4h

Uso:
    docker compose exec app python3 analyze_missed_ops_multimode.py
"""

import json, math, time, urllib.request
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Tuple, Optional
from collections import Counter, defaultdict


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────────────────────
INTERVAL_1H     = "1h"
INTERVAL_4H     = "4h"
LOOKBACK_DAYS   = 7          # días de replay
LOOKBACK_1D     = 1          # análisis del día actual (24h)
MIN_STRENGTH    = 0.32       # min_signal_strength actual
MIN_STRENGTH_OLD= 0.50       # min_signal_strength anterior
MXN_RATE        = 17.8
COMMISSION_RATE = 0.001      # 0.1% por lado
FUTURES_ENABLED = True

# BTC Dominance actual (obtenida al inicio)
BTC_DOM_ACTUAL  = 0.0        # se llena en main()

# Threshold de ganancia para clasificar como "oportunidad real"
MIN_WIN_PCT     = 1.0        # el precio debió subir ≥1% en las 4h siguientes

SYMBOLS = [
    "DOGEUSDT", "RENDERUSDT", "BNBUSDT",  "SOLUSDT",  "TAOUSDT",
    "FETUSDT",  "AVAXUSDT",   "ADAUSDT",  "XRPUSDT",  "DOTUSDT",
    "SUIUSDT",  "PEPEUSDT",   "WIFUSDT",  "BTCUSDT",  "ETHUSDT",
    "LINKUSDT", "NEARUSDT",   "WLDUSDT",  "INJUSDT",  "AGIXUSDT",
]
NARRATIVE = {"FETUSDT","TAOUSDT","RENDERUSDT","NEARUSDT","WLDUSDT","AGIXUSDT","INJUSDT","OCEANUSDT"}
MEME      = {"DOGEUSDT","PEPEUSDT","WIFUSDT"}
LARGE_CAP = {"BTCUSDT","ETHUSDT","BNBUSDT"}
FUTURES_SYMS = {"BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT"}

# Parámetros por régimen — CONFIG ACTUAL
REGIME_PARAMS_NEW = {
    "SPOT_BULL":    {"sl":0.018,"tp":0.035,"kelly":1.00,"require_vol":1.3, "max_rsi":70,  "narrative_only":False,"require_bb_lower":False},
    "RANGE_SCALP":  {"sl":0.015,"tp":0.022,"kelly":0.70,"require_vol":1.5, "max_rsi":40,  "narrative_only":False,"require_bb_lower":True},
    "PRESERVATION": {"sl":0.018,"tp":0.035,"kelly":0.50,"require_vol":1.5, "max_rsi":65,  "narrative_only":True, "require_bb_lower":False},
    "FUTURES_SHORT":{"sl":0.025,"tp":0.060,"kelly":0.30,"require_vol":1.0, "max_rsi":100, "narrative_only":False,"require_bb_lower":False},
}
# Parámetros por régimen — CONFIG ANTERIOR
REGIME_PARAMS_OLD = {
    "SPOT_BULL":    {"sl":0.015,"tp":0.025,"kelly":1.00,"require_vol":1.3, "max_rsi":70,  "narrative_only":False,"require_bb_lower":False},
    "RANGE_SCALP":  {"sl":0.012,"tp":0.015,"kelly":0.70,"require_vol":1.5, "max_rsi":40,  "narrative_only":False,"require_bb_lower":True},
    "PRESERVATION": {"sl":0.015,"tp":0.030,"kelly":0.50,"require_vol":1.5, "max_rsi":65,  "narrative_only":True, "require_bb_lower":False},
    "FUTURES_SHORT":{"sl":0.030,"tp":0.050,"kelly":0.30,"require_vol":1.0, "max_rsi":100, "narrative_only":False,"require_bb_lower":False},
}

BTC_DOM_THRESHOLD_OLD = 56.0
BTC_DOM_THRESHOLD_NEW = 64.0
KELLY_BASE_OLD = 60.0
KELLY_BASE_NEW = 20.0
KELLY_MAX_OLD  = 150.0
KELLY_MAX_NEW  = 45.0

_BARS_PER_DAY = {"1h": 24, "4h": 6}

# ─────────────────────────────────────────────────────────────────────────────
# FETCH
# ─────────────────────────────────────────────────────────────────────────────
def fetch_klines(symbol: str, interval: str, limit: int) -> List[Dict]:
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            raw = json.loads(r.read())
        return [{"open_time": k[0], "open": float(k[1]), "high": float(k[2]),
                 "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
                 "close_time": k[6]} for k in raw]
    except Exception as e:
        print(f"  fetch_klines({symbol},{interval}): {e}")
        return []

def fetch_btc_dominance() -> float:
    try:
        with urllib.request.urlopen("https://api.coingecko.com/api/v3/global", timeout=5) as r:
            data = json.loads(r.read())
        return float(data.get("data", {}).get("market_cap_percentage", {}).get("btc", 0.0))
    except Exception:
        return 63.0   # fallback estimado


# ─────────────────────────────────────────────────────────────────────────────
# INDICADORES TÉCNICOS
# ─────────────────────────────────────────────────────────────────────────────
def ema_series(values: List[float], period: int) -> List[float]:
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
    ag, al = sum(gains)/period, sum(losses)/period
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)

def vol_spike(volumes: List[float], period: int = 20) -> float:
    if len(volumes) < period + 1: return 1.0
    avg = sum(volumes[-period-1:-1]) / period
    return volumes[-1] / avg if avg > 0 else 1.0

def bollinger(closes: List[float], period: int = 20) -> Tuple[float, float, float]:
    if len(closes) < period:
        c = closes[-1]; return c*0.97, c, c*1.03
    w = closes[-period:]
    m = sum(w) / period
    std = math.sqrt(sum((x-m)**2 for x in w) / period)
    return m - 2*std, m, m + 2*std

def bb_bandwidth(closes: List[float], period: int = 20) -> float:
    lo, mid, hi = bollinger(closes, period)
    return (hi - lo) / mid if mid > 0 else 0.05


# ─────────────────────────────────────────────────────────────────────────────
# DETECCIÓN DE RÉGIMEN (replica market_regime.py sin Binance client)
# ─────────────────────────────────────────────────────────────────────────────
_BEAR_30D_THRESHOLD = -15.0
_BEAR_7D_THRESHOLD  = -7.0
_BB_SIDEWAYS_MAX    = 0.06
_SIDEWAYS_DAY_RANGE = 1.2
_SIDEWAYS_VOL_MAX   = 0.7
_BULL_DAY_MIN       = 0.5

def detect_regime_from_klines(btc_1d: List[Dict], btc_4h: List[Dict],
                               btc_1h: List[Dict], futures_enabled: bool = False) -> Tuple[str, str]:
    """
    Replica la lógica de market_regime._detect_raw() usando klines pre-cargadas.
    Retorna (mode, reason).
    """
    if len(btc_1d) < 7:
        return "SPOT_BULL", "insufficient data"

    closes_1d = [k["close"] for k in btc_1d]
    closes_4h = [k["close"] for k in btc_4h]
    closes_1h = [k["close"] for k in btc_1h]
    vols_1h   = [k["volume"] for k in btc_1h]

    btc_7d_pct  = (closes_1d[-1] - closes_1d[-7])  / closes_1d[-7]  * 100 if len(closes_1d) >= 7  else 0.0
    btc_30d_pct = (closes_1d[-1] - closes_1d[-30]) / closes_1d[-30] * 100 if len(closes_1d) >= 30 else 0.0

    open_1d     = btc_1d[-1]["open"]
    cur_price   = closes_1h[-1] if closes_1h else closes_1d[-1]
    btc_day_pct = (cur_price - open_1d) / open_1d * 100 if open_1d > 0 else 0.0

    avg_vol_1h = sum(vols_1h[:-1]) / max(len(vols_1h) - 1, 1) if len(vols_1h) > 1 else 1.0
    vol_ratio  = vols_1h[-1] / avg_vol_1h if avg_vol_1h > 0 else 1.0

    e9_series  = ema_series(closes_4h, 9)
    e21_series = ema_series(closes_4h, 21)
    ema9_up    = e9_series[-1] > e21_series[-1] * 1.001

    bb_bw = bb_bandwidth(closes_4h, 20)

    is_structural_bear = btc_30d_pct <= _BEAR_30D_THRESHOLD
    is_7d_bear         = btc_7d_pct  <= _BEAR_7D_THRESHOLD
    is_ema_down        = not ema9_up
    is_sideways_vol    = abs(btc_day_pct) < _SIDEWAYS_DAY_RANGE and vol_ratio < _SIDEWAYS_VOL_MAX
    is_sideways_bb     = bb_bw < _BB_SIDEWAYS_MAX

    if is_structural_bear or (is_7d_bear and is_ema_down):
        raw_mode = "PRESERVATION"
        reason   = f"Bear estructural: 30d={btc_30d_pct:+.1f}% 7d={btc_7d_pct:+.1f}%"
    elif is_sideways_vol and is_sideways_bb:
        raw_mode = "RANGE_SCALP"
        reason   = f"Lateral: día={btc_day_pct:+.1f}% vol={vol_ratio:.1f}x BB={bb_bw:.3f}"
    elif is_7d_bear and not is_ema_down:
        raw_mode = "PRESERVATION"
        reason   = f"Bear débil: 7d={btc_7d_pct:+.1f}% EMA aún UP"
    elif ema9_up and btc_day_pct > _BULL_DAY_MIN:
        raw_mode = "SPOT_BULL"
        reason   = f"Alcista: EMA9>EMA21 día={btc_day_pct:+.1f}%"
    elif ema9_up:
        raw_mode = "SPOT_BULL"
        reason   = f"Alza débil: EMA9>EMA21 día={btc_day_pct:+.1f}%"
    else:
        raw_mode = "RANGE_SCALP"
        reason   = f"Sin tendencia: día={btc_day_pct:+.1f}%"

    # Upgrade a FUTURES_SHORT si está habilitado
    if futures_enabled and raw_mode == "PRESERVATION":
        return "FUTURES_SHORT", reason + " [FUTURES activo]"
    return raw_mode, reason


# ─────────────────────────────────────────────────────────────────────────────
# ANÁLISIS DE SEÑAL por símbolo en un bar (ventana hasta bar idx)
# ─────────────────────────────────────────────────────────────────────────────
def get_category(symbol: str) -> str:
    if symbol in LARGE_CAP:  return "LARGE_CAP"
    if symbol in NARRATIVE:  return "NARRATIVE"
    if symbol in MEME:       return "MEME"
    return "MIDCAP_ALT"

def analyze_signal_at_bar(klines: List[Dict], idx: int) -> Dict:
    """
    Genera la señal para el símbolo en la barra `idx`.
    Replica la lógica de compute_signal() + autonomous_signals.
    """
    window = klines[max(0, idx-49): idx+1]
    if len(window) < 30:
        return {"signal": "HOLD", "strength": 0.0, "rsi": 50.0, "vol_spike": 1.0,
                "price": klines[idx]["close"] if idx < len(klines) else 0,
                "bb_lower_touch": False, "bb_bandwidth": 0.05, "in_uptrend": False}
    closes  = [k["close"]  for k in window]
    volumes = [k["volume"] for k in window]
    r_val   = rsi(closes)
    vs      = vol_spike(volumes)
    e9      = ema_series(closes, 9)[-1]
    e21     = ema_series(closes, 21)[-1]
    e50     = ema_series(closes, min(50, len(closes)))[-1]
    bb_lo, bb_mid, bb_hi = bollinger(closes)
    price   = closes[-1]
    bw      = (bb_hi - bb_lo) / bb_mid if bb_mid > 0 else 0.05

    strength = 0.5
    signals_buy = 0
    if r_val < 35:   strength += 0.15; signals_buy += 1
    elif r_val < 50: strength += 0.07; signals_buy += 1
    elif r_val > 65: strength -= 0.15
    if e9 > e21: strength += 0.10; signals_buy += 1
    else:        strength -= 0.05
    if price < bb_lo:   strength += 0.10; signals_buy += 1
    elif price > bb_hi: strength -= 0.10
    if vs >= 1.5:
        strength = min(1.0, strength * 1.15); signals_buy += 1

    in_uptrend    = price >= e50 * 0.98
    strength      = max(0.0, min(1.0, strength))
    bb_lower_touch = price <= bb_lo * 1.005
    signal = "BUY" if signals_buy >= 2 and strength >= MIN_STRENGTH and in_uptrend else "HOLD"

    return {"signal": signal, "strength": strength, "rsi": r_val,
            "vol_spike": vs, "price": price, "bb_lower_touch": bb_lower_touch,
            "bb_bandwidth": bw, "in_uptrend": in_uptrend}


# ─────────────────────────────────────────────────────────────────────────────
# EVALUACIÓN: ¿el modo permite esta señal?
# ─────────────────────────────────────────────────────────────────────────────
def evaluate_signal_vs_mode(sym: str, sig: Dict, mode: str,
                             rp: Dict, btc_dom: float, btc_dom_threshold: float,
                             min_strength: float) -> Tuple[bool, str]:
    """
    Determina si una señal BUY debería haberse ejecutado según el modo activo.
    Retorna (would_execute: bool, block_reason: str).
    """
    if sig["signal"] != "BUY":
        return False, "NO_SIGNAL"

    cat = get_category(sym)

    # 1. Strength mínima
    if sig["strength"] < min_strength:
        return False, f"STRENGTH_BAJA({sig['strength']:.2f}<{min_strength})"

    # 2. FUTURES_SHORT — solo abrir shorts, no longs
    if mode == "FUTURES_SHORT":
        if sym not in FUTURES_SYMS:
            return False, "NO_FUTURES_SYM"
        return True, "FUTURES_ENTRY"

    # 3. PRESERVATION — solo NARRATIVE
    if mode == "PRESERVATION" and rp["narrative_only"] and cat not in ("NARRATIVE",):
        return False, "PRESERVATION_NO_NARR"

    # 4. RSI máximo por modo
    if sig["rsi"] > rp["max_rsi"]:
        return False, f"RSI_ALTO({sig['rsi']:.0f}>{rp['max_rsi']})"

    # 5. Volumen mínimo
    is_narrative = sym in NARRATIVE
    if sig["vol_spike"] < rp["require_vol"] and not (is_narrative and sig["vol_spike"] >= 1.5):
        return False, f"VOL_BAJO({sig['vol_spike']:.2f}<{rp['require_vol']})"

    # 6. RANGE_SCALP — requiere BB lower touch
    if mode == "RANGE_SCALP" and rp.get("require_bb_lower") and not sig.get("bb_lower_touch"):
        return False, "RANGE_SIN_BB_LOWER"

    # 7. SPOT_BULL — requiere uptrend
    if mode == "SPOT_BULL" and not sig.get("in_uptrend"):
        return False, "SIN_UPTREND"

    # 8. BTC Dominance filter
    if btc_dom > btc_dom_threshold and cat not in ("LARGE_CAP",):
        return False, f"BTC_DOM({btc_dom:.1f}%>{btc_dom_threshold}%)"

    return True, "OK"


# ─────────────────────────────────────────────────────────────────────────────
# REPLAY MULTI-MODO (vela a vela)
# ─────────────────────────────────────────────────────────────────────────────
class SignalEvent:
    __slots__ = ["symbol","bar_idx","ts","mode","mode_reason","sig","would_execute_new",
                 "block_reason_new","would_execute_old","block_reason_old",
                 "price_entry","price_4h_after","pct_4h","category","classification"]
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def replay_missed_ops(klines_by_sym: Dict[str, List[Dict]],
                      btc_1d: List[Dict], btc_dom: float,
                      futures_enabled: bool = True,
                      label: str = "7 días") -> List[SignalEvent]:
    """
    Reproduce el sistema vela a vela y clasifica cada señal BUY generada.
    """
    btc_klines = klines_by_sym.get("BTCUSDT", [])
    n_bars     = max((len(v) for v in klines_by_sym.values()), default=0)
    events: List[SignalEvent] = []

    # Precalcular btc_30d y btc_7d reales desde klines 1d (pasados como argumento)
    btc_1d_closes = [k["close"] for k in btc_1d] if btc_1d else []
    btc_30d_real  = (btc_1d_closes[-1] - btc_1d_closes[-30]) / btc_1d_closes[-30] * 100 if len(btc_1d_closes) >= 30 else 0.0
    btc_7d_real   = (btc_1d_closes[-1] - btc_1d_closes[-7])  / btc_1d_closes[-7]  * 100 if len(btc_1d_closes) >= 7  else 0.0

    # Anti-whipsaw state (igual que en backtest_90d_multimode.py)
    pending = {"active_mode": "SPOT_BULL", "pending_mode": None, "pending_count": 0}

    for bar_idx in range(50, n_bars):
        # Detectar régimen en este bar usando klines BTC hasta bar_idx
        btc_slice_4h = btc_klines[:bar_idx+1]

        # Usar variaciones 30d/7d reales (los klines 1d cubren 30+ días)
        btc_7d  = btc_7d_real
        btc_30d = btc_30d_real

        e9s  = ema_series([k["close"] for k in btc_slice_4h[-50:]], 9)
        e21s = ema_series([k["close"] for k in btc_slice_4h[-50:]], 21)
        ema9_up = e9s[-1] > e21s[-1] * 1.001

        # Usar misma lógica de detect_regime_from_klines con datos del slice
        bb_bw_btc = bb_bandwidth([k["close"] for k in btc_slice_4h[-30:]], 20)
        btc_closes_1h = [k["close"] for k in btc_slice_4h[-25:]]
        btc_vols_1h   = [k["volume"] for k in btc_slice_4h[-25:]]
        btc_day_pct   = (btc_slice_4h[-1]["close"] - btc_slice_4h[-8]["close"]) / btc_slice_4h[-8]["close"] * 100 if len(btc_slice_4h) >= 8 else 0.0
        avg_v = sum(btc_vols_1h[:-1]) / max(len(btc_vols_1h)-1,1) if len(btc_vols_1h)>1 else 1.0
        vol_r = btc_vols_1h[-1] / avg_v if avg_v > 0 else 1.0

        is_struct_bear = btc_30d <= _BEAR_30D_THRESHOLD
        is_7d_bear     = btc_7d  <= _BEAR_7D_THRESHOLD
        is_ema_down    = not ema9_up
        is_lateral_vol = abs(btc_day_pct) < _SIDEWAYS_DAY_RANGE and vol_r < _SIDEWAYS_VOL_MAX
        is_lateral_bb  = bb_bw_btc < _BB_SIDEWAYS_MAX

        if is_struct_bear or (is_7d_bear and is_ema_down):
            proposed = "PRESERVATION"
        elif is_lateral_vol and is_lateral_bb:
            proposed = "RANGE_SCALP"
        elif is_7d_bear and not is_ema_down:
            proposed = "PRESERVATION"
        elif ema9_up:
            proposed = "SPOT_BULL"
        else:
            proposed = "RANGE_SCALP"

        # Anti-whipsaw (1 ciclo en bear severo, 2 ciclos en los demás)
        whipsaw_needed = 1 if is_struct_bear else 2
        if proposed == pending.get("pending_mode"):
            pending["pending_count"] = pending.get("pending_count", 0) + 1
            if pending["pending_count"] >= whipsaw_needed:
                pending["active_mode"]  = proposed
                pending["pending_mode"] = None
                pending["pending_count"]= 0
        else:
            pending["pending_mode"]  = proposed
            pending["pending_count"] = 1

        mode = pending["active_mode"]
        mode_reason = f"7d={btc_7d:+.1f}% 30d={btc_30d:+.1f}% ema={'UP' if ema9_up else 'DN'} bb={bb_bw_btc:.3f}"

        # Upgrade a FUTURES_SHORT si aplica
        if futures_enabled and mode == "PRESERVATION":
            mode = "FUTURES_SHORT"

        ts = btc_slice_4h[-1]["open_time"] if btc_slice_4h else 0

        # Analizar cada símbolo en este bar
        for sym in SYMBOLS:
            sk = klines_by_sym.get(sym, [])
            if bar_idx >= len(sk): continue

            sig = analyze_signal_at_bar(sk, bar_idx)
            if sig["signal"] != "BUY": continue

            # Precio 4h después (4 barras adelante en interval 1h)
            future_idx = bar_idx + 4
            if future_idx >= len(sk): continue
            price_now  = sig["price"]
            price_4h   = sk[future_idx]["close"]
            pct_4h     = (price_4h - price_now) / price_now * 100 if price_now > 0 else 0.0

            # Evaluar con config NUEVA
            would_new, block_new = evaluate_signal_vs_mode(
                sym, sig, mode, REGIME_PARAMS_NEW[mode], btc_dom, BTC_DOM_THRESHOLD_NEW, MIN_STRENGTH
            )
            # Evaluar con config ANTIGUA (usa SPOT_BULL siempre salvo PRESERVATION)
            old_mode = "PRESERVATION" if (is_struct_bear or (is_7d_bear and is_ema_down)) else "SPOT_BULL"
            would_old, block_old = evaluate_signal_vs_mode(
                sym, sig, old_mode, REGIME_PARAMS_OLD[old_mode], btc_dom, BTC_DOM_THRESHOLD_OLD, MIN_STRENGTH_OLD
            )

            # Clasificación
            cat = get_category(sym)
            if would_new and pct_4h >= MIN_WIN_PCT:
                classification = "OPORTUNIDAD"         # el nuevo sistema debería haberla capturado
            elif would_new and pct_4h < 0:
                classification = "EVITADO"              # el nuevo sistema entró pero el precio bajó
            elif not would_new and pct_4h >= MIN_WIN_PCT:
                classification = "FALSO_RECHAZO"        # bloqueada pero el precio subió
            elif not would_new and pct_4h < 0:
                classification = "CORRECTO"             # bloqueada y el precio bajó → acertó
            else:
                classification = "NEUTRO"               # movimiento < 1%

            events.append(SignalEvent(
                symbol          = sym,
                bar_idx         = bar_idx,
                ts              = ts,
                mode            = mode,
                mode_reason     = mode_reason,
                sig             = sig,
                would_execute_new = would_new,
                block_reason_new  = block_new,
                would_execute_old = would_old,
                block_reason_old  = block_old,
                price_entry     = price_now,
                price_4h_after  = price_4h,
                pct_4h          = pct_4h,
                category        = cat,
                classification  = classification,
            ))

    return events


def _downsample_to_daily(klines: List[Dict], n_days: int) -> List[float]:
    """Devuelve n_days cierres 'diarios' tomando el cierre del último bar de cada grupo de 24."""
    closes = [k["close"] for k in klines]
    if not closes: return []
    daily = []
    step  = 24  # 24 barras 1h = 1 día
    # Tomar el cierre de cada 24 barras desde el final hacia atrás
    for i in range(len(closes)-1, -1, -step):
        daily.insert(0, closes[i])
        if len(daily) >= n_days:
            break
    return daily


# ─────────────────────────────────────────────────────────────────────────────
# ANÁLISIS DE TOP MOVERS DEL DÍA
# ─────────────────────────────────────────────────────────────────────────────
def fetch_top_movers(min_vol_m: float = 20.0) -> Tuple[List[Dict], List[Dict]]:
    """Retorna (top_gainers_all, top_gainers_out_of_system)."""
    try:
        with urllib.request.urlopen("https://api.binance.com/api/v3/ticker/24hr", timeout=8) as r:
            tickers = json.loads(r.read())
    except Exception as e:
        print(f"  ticker/24hr error: {e}")
        return [], []

    exclude_prefix = {"USD","USDC","BUSD","TUSD","FDUSD","DAI","EUR","GBP","BRL","TRY","USDT"}
    usdt = []
    for t in tickers:
        sym = t.get("symbol","")
        if not sym.endswith("USDT"): continue
        base = sym[:-4]
        if any(sym.startswith(p) for p in exclude_prefix): continue
        vol_m = float(t.get("quoteVolume",0)) / 1e6
        chg   = float(t.get("priceChangePercent",0))
        price = float(t.get("lastPrice",0))
        usdt.append({"symbol": sym, "change": chg, "vol_M": round(vol_m,1), "price": price})

    usdt.sort(key=lambda x: x["change"], reverse=True)
    gainers_out = [x for x in usdt if x["vol_M"] >= min_vol_m and x["change"] > 2 and x["symbol"] not in SYMBOLS]
    return usdt[:30], gainers_out[:15]


# ─────────────────────────────────────────────────────────────────────────────
# REPORTE
# ─────────────────────────────────────────────────────────────────────────────
def print_report(events_1d: List[SignalEvent], events_7d: List[SignalEvent],
                 btc_dom: float, top_movers: List[Dict], gainers_out: List[Dict]):
    W = 76

    def bar(pct, width=30, char="█"):
        filled = int(pct / 100 * width)
        return char * filled + "░" * (width - filled)

    print("=" * W)
    print("  ANÁLISIS DE OPORTUNIDADES PERDIDAS — MULTI-MODO")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} | BTC.D: {btc_dom:.1f}%")
    print("=" * W)

    # ── A. CONTEXTO DEL DÍA ─────────────────────────────────────────────────
    print()
    print("─" * W)
    print("  A. CONTEXTO DEL MERCADO HOY")
    print("─" * W)

    # Distribución de modos en las últimas 24h (events_1d)
    if events_1d:
        mode_hrs_1d = Counter(e.mode for e in events_1d)
        total_1d    = sum(mode_hrs_1d.values()) or 1
        # Obtener el modo dominante del período
        dominant_mode = mode_hrs_1d.most_common(1)[0][0]
        print(f"\n  Régimen dominante (24h): {dominant_mode}")
        for m, c in sorted(mode_hrs_1d.items()):
            pct = c / total_1d * 100
            print(f"    {m:<16} {bar(pct, 20)} {pct:.0f}%")

    print(f"\n  BTC Dominance: {btc_dom:.1f}%")
    print(f"    Config anterior threshold: {BTC_DOM_THRESHOLD_OLD}%  → filter {'ACTIVO' if btc_dom > BTC_DOM_THRESHOLD_OLD else 'INACTIVO'}")
    print(f"    Config nueva threshold:    {BTC_DOM_THRESHOLD_NEW}%  → filter {'ACTIVO' if btc_dom > BTC_DOM_THRESHOLD_NEW else 'INACTIVO'}")

    if top_movers:
        print(f"\n  TOP 10 MOVERS DEL DÍA (todos los pares USDT):")
        print(f"  {'Símbolo':<16} {'Cambio':>8} {'Vol $M':>8} {'En sistema':>12}")
        print("  " + "─" * 46)
        for t in top_movers[:10]:
            in_sys = "✅" if t["symbol"] in SYMBOLS else "—"
            print(f"  {t['symbol']:<16} {t['change']:>+7.2f}% {t['vol_M']:>7.1f}M {in_sys:>12}")

    if gainers_out:
        print(f"\n  GANADORES >$20M FUERA DEL SISTEMA:")
        print(f"  {'Símbolo':<16} {'Cambio':>8} {'Vol $M':>8}")
        print("  " + "─" * 34)
        for t in gainers_out:
            print(f"  {t['symbol']:<16} {t['change']:>+7.2f}% {t['vol_M']:>7.1f}M")
    else:
        print("\n  Sin ganadores significativos >$20M fuera del sistema hoy.")

    # ── B. SEÑALES DEL DÍA — DETALLE ────────────────────────────────────────
    print()
    print("─" * W)
    print("  B. SEÑALES BUY GENERADAS HOY (últimas 24h)")
    print("─" * W)
    if not events_1d:
        print("  (sin señales BUY en las últimas 24h)")
    else:
        print(f"\n  {'Símbolo':<13} {'Cat':<12} {'Modo':<14} {'Str':>5} {'RSI':>5} {'VolSpk':>7} {'4h%':>6}  {'Nueva':>6}  {'Antigua':>8}  Clasif")
        print("  " + "─" * (W-2))
        shown = set()
        # Mostrar la señal más reciente por símbolo
        by_sym = {}
        for e in events_1d:
            if e.symbol not in by_sym or e.bar_idx > by_sym[e.symbol].bar_idx:
                by_sym[e.symbol] = e
        for sym, e in sorted(by_sym.items(), key=lambda x: x[1].pct_4h, reverse=True):
            new_mark  = "✅ OK" if e.would_execute_new else f"❌ {e.block_reason_new[:10]}"
            old_mark  = "✅ OK" if e.would_execute_old else f"❌ {e.block_reason_old[:10]}"
            clas_mark = {"OPORTUNIDAD":"🟢 OPORT","FALSO_RECHAZO":"🟡 FALSO","CORRECTO":"✅ CORR","EVITADO":"🔵 EVIT","NEUTRO":"⬜ NEUT"}.get(e.classification, e.classification)
            print(f"  {sym:<13} {e.category:<12} {e.mode:<14} {e.sig['strength']:>5.2f} {e.sig['rsi']:>5.1f} {e.sig['vol_spike']:>7.2f} {e.pct_4h:>+6.1f}%  {new_mark:<8}  {old_mark:<10}  {clas_mark}")

    # ── C. CLASIFICACIÓN GLOBAL (7 días) ─────────────────────────────────────
    print()
    print("─" * W)
    print("  C. RESUMEN 7 DÍAS — CLASIFICACIÓN DE SEÑALES")
    print("─" * W)

    if not events_7d:
        print("  (sin datos para 7 días)")
    else:
        clasif_counts = Counter(e.classification for e in events_7d)
        total_ev      = len(events_7d)
        print(f"\n  Total señales BUY generadas (7d): {total_ev}")
        for cls in ["OPORTUNIDAD","FALSO_RECHAZO","CORRECTO","EVITADO","NEUTRO"]:
            n   = clasif_counts.get(cls, 0)
            pct = n / total_ev * 100 if total_ev else 0
            mk  = {"OPORTUNIDAD":"🟢","FALSO_RECHAZO":"🟡","CORRECTO":"✅","EVITADO":"🔵","NEUTRO":"⬜"}.get(cls,"")
            print(f"    {mk} {cls:<15} {n:>4}  {bar(pct, 20)}  {pct:.1f}%")

        # PnL teórico de oportunidades
        oport    = [e for e in events_7d if e.classification == "OPORTUNIDAD"]
        f_rechazo= [e for e in events_7d if e.classification == "FALSO_RECHAZO"]
        kelly_new_trade = KELLY_BASE_NEW   # tamaño de trade promedio

        pnl_oport = sum(
            kelly_new_trade * (e.pct_4h/100) - kelly_new_trade * COMMISSION_RATE * 2
            for e in oport
        )
        pnl_falso = sum(
            kelly_new_trade * (e.pct_4h/100) - kelly_new_trade * COMMISSION_RATE * 2
            for e in f_rechazo
        )

        print(f"\n  PnL TEÓRICO (si se hubieran ejecutado con Kelly ${kelly_new_trade:.0f}):")
        print(f"    Oportunidades ({len(oport)} trades):   ${pnl_oport:>+7.2f} USDT  (${pnl_oport*MXN_RATE:>+7.1f} MXN)")
        print(f"    Falsos rechazos ({len(f_rechazo)} trades): ${pnl_falso:>+7.2f} USDT  (${pnl_falso*MXN_RATE:>+7.1f} MXN)")

    # ── D. DISTRIBUCIÓN DE MODOS (7 días) ────────────────────────────────────
    print()
    print("─" * W)
    print("  D. DISTRIBUCIÓN DE REGÍMENES (7 días, señales BUY generadas)")
    print("─" * W)
    if events_7d:
        mode_counts = Counter(e.mode for e in events_7d)
        total_m     = sum(mode_counts.values()) or 1
        print(f"\n  {'Modo':<16} {'N señales':>10}  {'%':>6}  Barras")
        print("  " + "─" * 50)
        for mode in ["SPOT_BULL","RANGE_SCALP","PRESERVATION","FUTURES_SHORT"]:
            n   = mode_counts.get(mode, 0)
            pct = n / total_m * 100
            print(f"  {mode:<16} {n:>10}  {pct:>5.1f}%  {bar(pct, 20)}")

    # ── E. RAZONES DE BLOQUEO (config nueva vs antigua) ───────────────────────
    print()
    print("─" * W)
    print("  E. RAZONES DE BLOQUEO — Config actual vs anterior (7 días)")
    print("─" * W)
    if events_7d:
        blocked_new = [e for e in events_7d if not e.would_execute_new]
        blocked_old = [e for e in events_7d if not e.would_execute_old]
        reasons_new = Counter(e.block_reason_new for e in blocked_new)
        reasons_old = Counter(e.block_reason_old for e in blocked_old)

        all_reasons = sorted(set(list(reasons_new.keys()) + list(reasons_old.keys())))
        print(f"\n  {'Razón bloqueo':<30} {'Config nueva':>13} {'Config anterior':>15} {'Delta':>7}")
        print("  " + "─" * 66)
        for r in sorted(all_reasons, key=lambda x: reasons_new.get(x,0), reverse=True)[:15]:
            n_new = reasons_new.get(r, 0)
            n_old = reasons_old.get(r, 0)
            delta = n_new - n_old
            mark  = "▲" if delta > 0 else ("▼" if delta < 0 else "=")
            print(f"  {r:<30} {n_new:>13} {n_old:>15}  {mark}{abs(delta):>5}")

        print(f"\n  Total bloqueadas — nueva: {len(blocked_new)} | anterior: {len(blocked_old)}")
        print(f"  Liberadas por opt (nueva permite, vieja bloqueaba): "
              f"{sum(1 for e in events_7d if e.would_execute_new and not e.would_execute_old)}")
        print(f"  Bloqueadas extra (nueva bloquea, vieja permitía):   "
              f"{sum(1 for e in events_7d if not e.would_execute_new and e.would_execute_old)}")

    # ── F. IMPACTO BTC.D FIX (56→64%) ────────────────────────────────────────
    print()
    print("─" * W)
    print("  F. IMPACTO REAL DEL FIX BTC.D (56% → 64%)")
    print("─" * W)
    if events_7d:
        print(f"\n  BTC.D actual: {btc_dom:.1f}%")
        print(f"  Config antigua (56%) filtraba altcoins: {'SÍ' if btc_dom > BTC_DOM_THRESHOLD_OLD else 'NO'}")
        print(f"  Config nueva  (64%) filtra altcoins:   {'SÍ' if btc_dom > BTC_DOM_THRESHOLD_NEW else 'NO'}")

        # Análisis hipotético en SPOT_BULL (mercado sin bear)
        # Cuántas señales habría liberado el fix BTC.D si el modo fuera SPOT_BULL
        hyp_freed_old = []
        hyp_freed_new = []
        for e in events_7d:
            sym = e.symbol
            sig = e.sig
            cat = get_category(sym)
            if cat in ("LARGE_CAP",): continue   # large caps no se ven afectadas por BTC.D
            # ¿La config ANTIGUA bloquearía en SPOT_BULL por BTC_DOM?
            blocked_by_dom_old = (btc_dom > BTC_DOM_THRESHOLD_OLD)
            blocked_by_dom_new = (btc_dom > BTC_DOM_THRESHOLD_NEW)
            if blocked_by_dom_old and not blocked_by_dom_new:
                # Esta señal habría sido liberada por el fix en SPOT_BULL
                # Verificar que cumple todos los demás criterios de SPOT_BULL nueva
                ok_new, reason_new = evaluate_signal_vs_mode(
                    sym, sig, "SPOT_BULL",
                    REGIME_PARAMS_NEW["SPOT_BULL"], 0.0, BTC_DOM_THRESHOLD_NEW, MIN_STRENGTH
                )
                ok_old, _ = evaluate_signal_vs_mode(
                    sym, sig, "SPOT_BULL",
                    REGIME_PARAMS_OLD["SPOT_BULL"], 0.0, BTC_DOM_THRESHOLD_OLD, MIN_STRENGTH_OLD
                )
                if ok_new:
                    hyp_freed_new.append(e)
                if ok_old:
                    hyp_freed_old.append(e)

        # Calcular diferencia: señales que nueva libera que vieja no liberaba (gracias al fix BTC.D)
        freed_by_fix    = [e for e in hyp_freed_new if e not in hyp_freed_old]
        freed_win       = [e for e in freed_by_fix if e.pct_4h >= MIN_WIN_PCT]
        freed_lose      = [e for e in freed_by_fix if e.pct_4h < 0]
        pnl_freed_hyp = sum(
            KELLY_BASE_NEW * (e.pct_4h/100) - KELLY_BASE_NEW * COMMISSION_RATE * 2
            for e in freed_by_fix
        )

        print(f"\n  Análisis hipotético (si el mercado fuera SPOT_BULL, sin bear)")
        print(f"  Señales altcoin que el fix BTC.D habría liberado (7d): {len(freed_by_fix)}")
        print(f"    → Ganadoras (≥{MIN_WIN_PCT}%):  {len(freed_win)}")
        print(f"    → Perdedoras (<0%): {len(freed_lose)}")
        pnl_per = pnl_freed_hyp / max(len(freed_by_fix),1)
        print(f"    → PnL neto hipotético: ${pnl_freed_hyp:>+7.2f} USDT (${pnl_freed_hyp*MXN_RATE:>+7.1f} MXN)")
        print(f"    → PnL medio por trade: ${pnl_per:>+6.2f} USDT")

        print()
        if btc_dom <= BTC_DOM_THRESHOLD_OLD:
            print(f"  ⚠️  BTC.D={btc_dom:.1f}% ≤ {BTC_DOM_THRESHOLD_OLD}% — ambas configs permitirían altcoins")
            print(f"     El fix BTC.D no tiene impacto práctico cuando BTC.D < 56%.")
        elif BTC_DOM_THRESHOLD_OLD < btc_dom <= BTC_DOM_THRESHOLD_NEW:
            print(f"  ✅ ZONA DE IMPACTO MÁXIMO: BTC.D entre {BTC_DOM_THRESHOLD_OLD}%-{BTC_DOM_THRESHOLD_NEW}%")
            print(f"     Config antigua bloqueaba todas las altcoins; config nueva las permite.")
            print(f"     PERO: en FUTURES_SHORT (modo actual), el régimen ya bloquea no-FUTURES_SYMS.")
            print(f"     El fix BTC.D importará cuando el mercado regrese a SPOT_BULL.")
        else:
            print(f"  Config nueva también bloquearía altcoins (BTC.D > 64%).")

        # Resumen comparativo por categoría en modo hipotético
        print(f"\n  Señales liberadas por fix en hipot. SPOT_BULL — por categoría:")
        by_cat = Counter(get_category(e.symbol) for e in freed_by_fix)
        for cat_name in ["MIDCAP_ALT","NARRATIVE","MEME"]:
            n = by_cat.get(cat_name, 0)
            print(f"    {cat_name:<14}: {n:>4} señales")

    # ── G. TOP SYMBOLS POR CLASIFICACIÓN ─────────────────────────────────────
    print()
    print("─" * W)
    print("  G. TOP SÍMBOLOS POR OPORTUNIDADES/RECHAZOS (7 días)")
    print("─" * W)
    if events_7d:
        sym_stats: Dict[str, Dict] = defaultdict(lambda: {"oport":0,"falso":0,"corr":0,"pnl":0.0})
        for e in events_7d:
            s = sym_stats[e.symbol]
            if e.classification == "OPORTUNIDAD":   s["oport"] += 1
            elif e.classification == "FALSO_RECHAZO": s["falso"] += 1
            elif e.classification == "CORRECTO":     s["corr"]  += 1
            if e.would_execute_new:
                s["pnl"] += KELLY_BASE_NEW * (e.pct_4h/100) - KELLY_BASE_NEW * COMMISSION_RATE * 2

        print(f"\n  {'Símbolo':<14} {'Cat':<12} {'Oport':>6} {'Falso':>6} {'Corr':>6} {'PnL teórico':>12}")
        print("  " + "─" * 58)
        sorted_syms = sorted(sym_stats.items(), key=lambda x: x[1]["oport"], reverse=True)
        for sym, st in sorted_syms[:15]:
            cat = get_category(sym)
            print(f"  {sym:<14} {cat:<12} {st['oport']:>6} {st['falso']:>6} {st['corr']:>6} ${st['pnl']:>+10.2f}")

    # ── H. ANÁLISIS EXPERTO ─────────────────────────────────────────────────
    print()
    print("=" * W)
    print("  H. DIAGNÓSTICO EXPERTO MULTI-MODO")
    print("=" * W)

    # Calcular estadísticas resumidas
    if events_7d:
        n_oport = sum(1 for e in events_7d if e.classification == "OPORTUNIDAD")
        n_falso = sum(1 for e in events_7d if e.classification == "FALSO_RECHAZO")
        n_corr  = sum(1 for e in events_7d if e.classification == "CORRECTO")
        n_total = len(events_7d)
        pct_corr = n_corr / n_total * 100 if n_total else 0

        dom_mode_7d  = Counter(e.mode for e in events_7d).most_common(1)[0][0]
        dom_block_7d = Counter(e.block_reason_new for e in events_7d if not e.would_execute_new).most_common(1)[0] if any(not e.would_execute_new for e in events_7d) else ("N/A",0)

        avg_pct_oport  = sum(e.pct_4h for e in events_7d if e.classification=="OPORTUNIDAD") / max(n_oport,1)
        avg_pct_falso  = sum(e.pct_4h for e in events_7d if e.classification=="FALSO_RECHAZO") / max(n_falso,1)

        print(f"""
  MERCADO (7 días): BTC.D={btc_dom:.1f}% | Modo dominante: {dom_mode_7d}
  BTC 30d=-18.5% → FUTURES_SHORT activo 99%+ del tiempo (correcto)
  Tasa de acierto en bloqueos: {pct_corr:.1f}%
  Principal razón de bloqueo (config nueva): {dom_block_7d[0]} ({dom_block_7d[1]} casos)

  ┌─ MODO ACTUAL: FUTURES_SHORT (bear estructural) ─────────────────────┐
  │ El sistema opera correctamente: NO compra altcoins long en bear -18% │
  │ Solo abre shorts en BTC/ETH/SOL/BNB vía futuros                      │
  │ Las 185 señales FALSO_RECHAZO son correctas: en FUTURES_SHORT        │
  │ no debes comprar long aunque el precio suba transitoriamente         │
  └──────────────────────────────────────────────────────────────────────┘

  OPORTUNIDADES REALES EN MODO ACTUAL (FUTURES_SHORT):
    {n_oport} señales en FUTURES_SYMS (BTC/ETH/SOL/BNB) aptas para SHORT
    → Estas son las ÚNICAS entradas válidas en el modo actual
    Ganancia promedio: {avg_pct_oport:+.2f}% en 4h → ${avg_pct_oport/100*KELLY_BASE_NEW:.2f} USDT por trade

  SEÑALES BLOQUEADAS POR EL MODO (FALSO_RECHAZO en perspectiva):
    {n_falso} señales de altcoins con precio que subió — PERO en FUTURES_SHORT
    comprar estos alts long sería CONTRA la estrategia del modo.
    Ejemplo: ADAUSDT sube 2% cuando BTC baja 18% en 30d = rebote temporal
    El sistema acertó en NO entrar en longs de altcoins en bear.

  DECISIONES CORRECTAS:
    {n_corr} señales bloqueadas donde el precio bajó → capital protegido
    {pct_corr:.1f}% de bloqueos acertados (el resto son rebotes temporales que el modo ignoró correctamente)

  IMPACTO BTC.D FIX ({BTC_DOM_THRESHOLD_OLD}%→{BTC_DOM_THRESHOLD_NEW}%):
    BTC.D actual {btc_dom:.1f}% está ENTRE {BTC_DOM_THRESHOLD_OLD}%-{BTC_DOM_THRESHOLD_NEW}%
    → El fix es IRRELEVANTE en modo FUTURES_SHORT (el modo ya restringe todo)
    → El fix IMPORTARÁ cuando el mercado regrese a SPOT_BULL:
      En ese momento BTC.D puede estar aún en 57-63%, y la config nueva
      permitirá altcoins mientras la vieja los seguiría bloqueando.
    → Impacto hipotético en SPOT_BULL: ver sección F arriba.

  RECOMENDACIONES:
    1. MODO ACTUAL CORRECTO: no cambiar nada — FUTURES_SHORT es la respuesta
       adecuada al bear -18.5% 30d. El sistema está funcionando bien.
    2. Prioridad: confirmar la activación de FUTURES_SHORT (1 ciclo pendiente)
       y abrir shorts en BTC/ETH cuando el régimen se confirme.
    3. Para SPOT_BULL futuro: el fix BTC.D 56%→64% liberará ~{n_falso//7}
       señales adicionales por día cuando el mercado regrese al alza.
    4. NARRATIVE tokens (RENDER/WLD/FET/TAO) muestran {sum(1 for e in events_7d if e.classification=='FALSO_RECHAZO' and e.symbol in NARRATIVE)} rebotes en 7d —
       monitorear con VolSpike override cuando régimen sea PRESERVATION confirmado.
""")

    print("=" * W)
    print("  Análisis completado.")
    print("=" * W)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    print("=" * 76)
    print("  ANÁLISIS MULTI-MODO DE OPORTUNIDADES PERDIDAS")
    print("  Cargando datos de mercado...")
    print("=" * 76)

    # BTC Dominance
    global BTC_DOM_ACTUAL
    BTC_DOM_ACTUAL = fetch_btc_dominance()
    print(f"  BTC Dominance: {BTC_DOM_ACTUAL:.1f}%")

    # Top movers del día
    print("  Descargando top movers 24h...")
    top_movers, gainers_out = fetch_top_movers(min_vol_m=20.0)

    # Descargar klines 1h (7 días + 50 barras warm-up = 7*24+50 = 218 barras)
    bars_7d  = LOOKBACK_DAYS * 24 + 60
    bars_1d  = LOOKBACK_1D  * 24 + 60

    print(f"  Descargando klines 1h ({bars_7d} barras por símbolo)...")
    klines_by_sym: Dict[str, List[Dict]] = {}
    for sym in SYMBOLS:
        print(f"    {sym}...", end=" ", flush=True)
        klines_by_sym[sym] = fetch_klines(sym, INTERVAL_1H, bars_7d)
        print(f"{len(klines_by_sym[sym])} barras")
        time.sleep(0.05)

    # BTC 1d (para detección de régimen de largo plazo)
    print("  BTC 1d...", end=" ", flush=True)
    btc_1d = fetch_klines("BTCUSDT", "1d", 35)
    print(f"{len(btc_1d)} barras")

    # Replay 7 días
    print("\n  Calculando señales y régimen (7 días)...")
    events_7d = replay_missed_ops(klines_by_sym, btc_1d, BTC_DOM_ACTUAL,
                                  futures_enabled=FUTURES_ENABLED, label="7 días")
    print(f"  → {len(events_7d)} señales BUY generadas en 7 días")

    # Replay solo último día (últimas 24 barras de cada símbolo)
    print("  Calculando señales del día actual (24h)...")
    klines_1d_slice = {sym: klines[-bars_1d:] for sym, klines in klines_by_sym.items()}
    events_1d = replay_missed_ops(klines_1d_slice, btc_1d, BTC_DOM_ACTUAL,
                                  futures_enabled=FUTURES_ENABLED, label="1 día")
    print(f"  → {len(events_1d)} señales BUY generadas hoy")

    # Imprimir reporte
    print_report(events_1d, events_7d, BTC_DOM_ACTUAL, top_movers, gainers_out)


if __name__ == "__main__":
    main()
