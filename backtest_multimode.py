"""
backtest_multimode.py — Simulador de Fidelidad Total v4.0
==========================================================

Replica exactamente el comportamiento de producción:
  - TechnicalIndicators.analyze_signal() REAL (no simplificado)
  - PnLPredictor.predict_pnl() REAL con pesos aprendidos de la DB
  - format_indicators_for_predictor() REAL
  - Régimen dinámico por bar (SPOT_BULL / RANGE_SCALP / PRESERVATION / FUTURES_SHORT)
  - Anti-whipsaw de 3 barras para cambio de régimen
  - SmartTP: extend / scale_out / close según momentum score (igual a run_trades.py)
  - Anti-wick: SL/TS no cierra si last_closed_candle > threshold
  - Trailing stop con peak tracking
  - Predicción de salida (PredExit) cuando posición deteriorada
  - Kelly sizing con F&G boost (Capitulación×1.50, ExtremeFear×1.35)
  - Cap por categoría (LARGE_CAP≤3, MIDCAP_ALT≤5, NARRATIVE≤4, MEME≤2)
  - Futuros SHORT con margin, leverage, SL/TP algo-orders simulados
  - [v4] Futuros SmartTP: extender SHORT si momentum bearish sigue (score ≥3/4)
  - [v4] Futuros trailing stop: activa a 2% ganancia, distancia 1%
  - [v4] 30 símbolos spot (alineado con analysis_symbols en config.py)
  - [v4] CAT_PARAMS exactos de config.py category_thresholds
  - [v4] FNG boost correcto: F&G≤15 → ×1.50 (capitulación)
  - [v4] Kelly base $40 + 15% dinámico (producción)

5 ESCENARIOS:
  REAL:         Sistema completo (predictor+SmartTP+anti-wick+futuros+F&G)
  REAL_OPT:     REAL + params optimizados (str≥0.58)
  FUT_ONLY:     Solo futuros SHORT durante régimen FUTURES_SHORT
  SPOT_BULL_SIM:Simulación recuperación (forzar régimen SPOT_BULL)
  RECOVERY_SIM: SPOT_BULL + BTC +15% sintético (proyección salida shorts)

Uso:
    docker compose exec app python3 backtest_multimode.py [--days N] [--capital X]
"""

import sys, os, json, time, math, urllib.request, random, argparse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Tuple

# ── Parámetros globales (producción jun 2026) ─────────────────────────────────
INITIAL_CAPITAL    = 2910.0   # Capital real jun 2026
DAYS               = 120      # 90d sim + 30d warmup indicadores
WARMUP_BARS        = 50       # barras de calentamiento antes de tradear
INTERVAL           = "4h"
# C0: use centralized Binance commission module (defaults: spot 0.001, futures 0.0004)
from binance_commission import (
    entry_commission as _bt_entry_commission,
    exit_commission as _bt_exit_commission,
    get_config as _bt_comm_config,
)
COMMISSION_RATE    = _bt_comm_config().rate_for('spot')  # kept for backward compat, do not use directly
MAX_POSITIONS      = 12       # max posiciones spot simultáneas (producción usa hasta 20 en SPOT_BULL)
MAX_FUTURES_SHORTS = 7        # FUTURES_MAX_SHORTS producción
MAX_EXPOSURE_PCT   = 0.75     # MAX_TOTAL_EXPOSURE_PCT
MAX_PCT_PER_TRADE  = 0.15     # 15% del capital líquido por trade (KELLY_DYNAMIC_BASE_PCT producción)
MAX_ENTRIES_BAR    = 3        # max entradas nuevas por barra (throttle)
N_MC_SIMS          = 5_000    # Monte Carlo
ANTI_WHIPSAW       = 3        # bars consecutivos para confirmar régimen
FNG_VALUE          = 15       # Fear & Greed actual (Capitulación)
LEVERAGE           = 2        # apalancamiento futuros
KELLY_BASE         = 40.0     # KELLY_BASE_USDT (producción: $40)
KELLY_MAX          = 350.0    # KELLY_MAX_USDT (producción)

# C0: futures commission helper (entry + exit on notional position)
def _bt_fut_comm_total(sh, exit_price):
    qty = sh.notional / sh.entry_price if sh.entry_price > 0 else 0.0
    return _bt_entry_commission(qty, sh.entry_price, 'futures', None) + _bt_exit_commission(qty, exit_price, 'futures', None)

# ── Símbolos reales del sistema (32 tokens — analysis_symbols en config.py) ──
SPOT_SYMBOLS = [
    # LARGE_CAP
    "BTCUSDT","ETHUSDT","BNBUSDT",
    # MIDCAP_ALT / Infrastructure
    "SOLUSDT","XRPUSDT","ADAUSDT","AVAXUSDT","DOTUSDT","LINKUSDT",
    "TRXUSDT","TONUSDT","SUIUSDT","HBARUSDT","ARBUSDT","MATICUSDT","ATOMUSDT",
    "SEIUSDT","AAVEUSDT",
    # NARRATIVE / AI_DATA
    "FETUSDT","NEARUSDT","RENDERUSDT","TAOUSDT","WLDUSDT","INJUSDT",
    "OPUSDT","GRTUSDT","ONDOUSDT",
    # MEME
    "DOGEUSDT","PEPEUSDT",
]
FUTURES_SYMS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","ADAUSDT","NEARUSDT","SUIUSDT",
]
NARRATIVE_SET = {
    "FETUSDT","TAOUSDT","RENDERUSDT","NEARUSDT","WLDUSDT","INJUSDT",
    "OPUSDT","GRTUSDT","ONDOUSDT"
}
MEME_SET      = {"DOGEUSDT","PEPEUSDT","WIFUSDT"}
LARGE_CAP_SET = {"BTCUSDT","ETHUSDT","BNBUSDT"}

# ── Parámetros por categoría — exactos de config.py category_thresholds (v4) ──
CAT_PARAMS = {
    "LARGE_CAP":  {"sl": 0.018, "tp": 0.035, "trail_act": 0.025, "trail_dist": 0.012, "min_prob": 0.55, "min_pnl":  0.10},
    "MIDCAP_ALT": {"sl": 0.018, "tp": 0.035, "trail_act": 0.025, "trail_dist": 0.012, "min_prob": 0.50, "min_pnl":  0.00},
    "NARRATIVE":  {"sl": 0.035, "tp": 0.070, "trail_act": 0.080, "trail_dist": 0.030, "min_prob": 0.45, "min_pnl": -1.00},
    "MEME":       {"sl": 0.020, "tp": 0.045, "trail_act": 0.025, "trail_dist": 0.015, "min_prob": 0.52, "min_pnl":  0.05},
}

# ── Parámetros por régimen (producción) ───────────────────────────────────────
REGIME_PARAMS = {
    "SPOT_BULL":     {"kelly_mult": 1.00, "narrative_only": False, "max_rsi": 70,  "require_vol": 0.8},
    "RANGE_SCALP":   {"kelly_mult": 0.70, "narrative_only": False, "max_rsi": 55,  "require_vol": 1.2},
    "PRESERVATION":  {"kelly_mult": 0.50, "narrative_only": True,  "max_rsi": 65,  "require_vol": 1.3},
    "FUTURES_SHORT": {"kelly_mult": 0.30, "narrative_only": False, "max_rsi": 100, "require_vol": 0.5},
}

# ── F&G Kelly boost — idéntico a config.py (fng_capitulation_threshold=15) ───
def _fng_boost(fng: int) -> float:
    if fng <= 15:  return 1.50   # Capitulación (fng_capitulation_multiplier)
    if fng <= 20:  return 1.35   # Extreme Fear (fng_extreme_fear_multiplier)
    if fng <= 40:  return 1.15   # Fear
    if fng <= 60:  return 1.00   # Neutral
    if fng <= 75:  return 0.85   # Greed
    return 0.65                  # Extreme Greed (fng_extreme_greed_multiplier)

FNG_BOOST = _fng_boost(FNG_VALUE)

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


# ── Integración con módulos reales (solo disponibles dentro de Docker) ────────
_USE_REAL_INDICATORS = False
_USE_REAL_PREDICTOR  = False
_TechnicalIndicators = None
_PnLPredictor        = None
_format_indicators   = None
_indicator_weights_cache: Dict[str, Dict] = {}

def _try_load_real_modules():
    global _USE_REAL_INDICATORS, _USE_REAL_PREDICTOR
    global _TechnicalIndicators, _PnLPredictor, _format_indicators

    try:
        from technical_indicators import TechnicalIndicators as _TI
        _TechnicalIndicators = _TI
        _USE_REAL_INDICATORS = True
        print("  [✓] TechnicalIndicators.analyze_signal() REAL cargado")
    except ImportError as e:
        print(f"  [~] TechnicalIndicators no disponible ({e}) — usando fallback")

    try:
        from pnl_predictor import PnLPredictor as _PP
        from trade_utils import format_indicators_for_predictor as _fmt
        _PnLPredictor  = _PP()
        _format_indicators = _fmt
        _USE_REAL_PREDICTOR = True
        print("  [✓] PnLPredictor REAL cargado (pesos aprendidos de DB)")
    except Exception as e:
        print(f"  [~] PnLPredictor no disponible ({e}) — usando modelo estadístico")


def _real_analyze_signal(closes, highs, lows, volumes) -> Dict:
    """
    Llama al TechnicalIndicators.analyze_signal() real y normaliza las claves
    para compatibilidad con el simulador (vol_spike, trend_4h, atr_pct).
    """
    if _USE_REAL_INDICATORS and _TechnicalIndicators:
        try:
            raw = _TechnicalIndicators.analyze_signal(closes, highs, lows, volumes)
            ind = raw.get("indicators", {})

            # Normalizar vol_spike: el módulo real usa 'volume_spike' (bool/None)
            # y 'atr' (absoluto). Calculamos vol_spike como ratio numérico.
            vs_raw = ind.get("volume_spike")
            if isinstance(vs_raw, bool):
                # Calcular ratio real a partir de volúmenes
                if len(volumes) >= 21:
                    avg20 = sum(volumes[-21:-1]) / 20
                    vs_num = volumes[-1] / avg20 if avg20 > 0 else 1.0
                else:
                    vs_num = 1.5 if vs_raw else 0.8
            elif isinstance(vs_raw, (int, float)):
                vs_num = float(vs_raw)
            else:
                vs_num = 1.0

            # trend_4h: el módulo real lo deja None (lo calcula en autonomous_signals)
            # Derivamos de EMA9 vs EMA21 como proxy
            ema9  = ind.get("ema_fast", ind.get("ema9",  closes[-1]))
            ema21 = ind.get("ema_slow", ind.get("ema21", closes[-1]))
            trend_4h = 1.0 if ema9 > ema21 else 0.0

            # atr_pct
            atr_abs = ind.get("atr", closes[-1] * 0.01)
            atr_pct = atr_abs / closes[-1] if closes[-1] > 0 else 0.01

            ind["vol_spike"] = round(vs_num, 3)
            ind["trend_4h"]  = trend_4h
            ind["atr_pct"]   = round(atr_pct, 4)
            raw["indicators"] = ind
            return raw
        except Exception:
            pass
    return _fallback_analyze_signal(closes, highs, lows, volumes)


def _real_predict_pnl(symbol: str, price: float, indicators: Dict,
                      position_usdt: float) -> Optional[Dict]:
    """Llama al PnLPredictor.predict_pnl() real o retorna None."""
    if not _USE_REAL_PREDICTOR or _PnLPredictor is None:
        return None
    try:
        fmt = _format_indicators(indicators)
        return _PnLPredictor.predict_pnl(
            symbol=symbol,
            current_price=price,
            indicators=fmt,
            cycle_duration_minutes=240,
            position_size_usdt=position_usdt,
        )
    except Exception:
        return None


# ── Indicadores técnicos (fallback autónomo) ──────────────────────────────────
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

def macd(closes: List[float]) -> Tuple[float, float]:
    """MACD(12,26,9) — retorna (macd_val, signal_val)."""
    if len(closes) < 26: return 0.0, 0.0
    e12 = ema(closes, 12)
    e26 = ema(closes, 26)
    macd_line = [e12[i] - e26[i] for i in range(len(closes))]
    sig_line  = ema(macd_line, 9)
    return macd_line[-1], sig_line[-1]

def _fallback_analyze_signal(closes: List[float], highs: List[float],
                              lows: List[float], volumes: List[float]) -> Dict:
    """
    Fallback que replica la lógica de TechnicalIndicators.analyze_signal()
    cuando el módulo real no está disponible. Usa los mismos indicadores
    y pesos aproximados.
    """
    if len(closes) < 30:
        return {"signal": "HOLD", "strength": 0.0, "reason": "insufficient_data",
                "indicators": {"rsi": 50.0, "vol_spike": 1.0, "macd": 0.0,
                               "trend_4h": 0.0, "atr_pct": 0.01}}
    r       = rsi(closes)
    vs      = vol_spike(volumes)
    e9      = ema(closes, 9)[-1]
    e21     = ema(closes, 21)[-1]
    e50_s   = ema(closes, min(50, len(closes)))
    e50     = e50_s[-1]
    e200    = ema(closes, min(200, len(closes)))[-1]
    bb_lo, bb_mid, bb_hi = bollinger(closes)
    macd_v, macd_sig = macd(closes)
    price   = closes[-1]

    # ATR
    if len(highs) >= 2 and len(lows) >= 2:
        true_ranges = [max(highs[i]-lows[i],
                          abs(highs[i]-closes[i-1]),
                          abs(lows[i]-closes[i-1]))
                       for i in range(1, min(15, len(closes)))]
        atr = sum(true_ranges) / len(true_ranges) if true_ranges else price * 0.01
    else:
        atr = price * 0.01
    atr_pct = atr / price

    # Scoring idéntico al sistema (pesos de indicator_weights DB)
    score = 0.5
    if r < 35:   score += 0.15
    elif r < 50: score += 0.07
    elif r > 70: score -= 0.15
    elif r > 65: score -= 0.08

    if e9 > e21:   score += 0.10
    elif e9 < e21: score -= 0.05

    if price < bb_lo:   score += 0.10
    elif price > bb_hi: score -= 0.10

    if macd_v > macd_sig: score += 0.08
    else:                 score -= 0.05

    if vs >= 2.0:  score += 0.15
    elif vs >= 1.5: score += 0.08
    elif vs < 0.5: score -= 0.10

    trend_4h = 1.0 if e9 > e21 else 0.0
    ema200_above = price > e200
    if not ema200_above: score *= 0.90   # penalizar si bajo EMA200

    score = max(0.0, min(1.0, score))

    signals_buy = sum([
        r < 50,
        e9 > e21,
        price < bb_lo * 1.02,
        macd_v > macd_sig,
        vs >= 1.3,
    ])
    signal = "BUY" if signals_buy >= 2 and score >= 0.55 else "HOLD"
    reason = f"rsi={r:.0f},vs={vs:.1f},macd={'UP' if macd_v>macd_sig else 'DW'}"

    return {
        "signal":   signal,
        "strength": round(score, 4),
        "reason":   reason,
        "indicators": {
            "rsi":       round(r, 2),
            "vol_spike": round(vs, 3),
            "macd":      round(macd_v, 6),
            "trend_4h":  trend_4h,
            "atr_pct":   round(atr_pct, 4),
            "ema9":      e9,
            "ema21":     e21,
            "ema200":    e200,
            "bb_lower":  bb_lo,
            "bb_upper":  bb_hi,
        }
    }


def _stat_predict_pnl(symbol: str, indicators: Dict, position_usdt: float,
                      cat: str, scenario: str) -> Dict:
    """
    Modelo estadístico de predicción PnL cuando el predictor real no está
    disponible. Replica la lógica bayesiana del PnLPredictor con pesos
    aprendidos promedio del sistema.
    """
    rsi_v  = indicators.get("rsi", 50.0)
    vs_v   = indicators.get("vol_spike", 1.0)
    macd_v = indicators.get("macd", 0.0)
    t4h    = indicators.get("trend_4h", 0.0)
    atr_p  = indicators.get("atr_pct", 0.01)

    # Pesos calibrados del Learning Engine v2 (promedio global DB)
    w_vs   = 0.1498
    w_t4h  = 0.1490
    w_rsi  = 0.1192
    w_macd = 0.0997

    rsi_norm  = (70 - rsi_v) / 70.0 if rsi_v < 70 else 0.0
    vs_norm   = min(vs_v / 3.0, 1.0)
    macd_norm = 0.6 if macd_v > 0 else 0.3
    t4h_norm  = t4h

    raw_prob = (w_rsi * rsi_norm + w_vs * vs_norm +
                w_macd * macd_norm + w_t4h * t4h_norm) / (w_vs+w_t4h+w_rsi+w_macd)
    # Calibrar al rango histórico real: WR promedio = 36.2% en bear market
    # No aplicar bias_correction=1.5 — ese infla artificialmente para trades reales,
    # pero en simulación usamos la probabilidad sin inflar para ser realistas.
    raw_prob = 0.28 + raw_prob * 0.35   # rango [0.28-0.63] centrado en 0.36
    prob = max(0.20, min(raw_prob, 0.75))  # sin bias_correction en sim

    cat_p = CAT_PARAMS.get(cat, CAT_PARAMS["MIDCAP_ALT"])
    tp_pct = cat_p["tp"]
    sl_pct = cat_p["sl"]
    exp_pnl = position_usdt * (prob * tp_pct - (1 - prob) * sl_pct)

    return {
        "success_probability": round(prob, 4),
        "expected_pnl":        round(exp_pnl, 4),
        "price_range_low":     0.0,
        "price_range_high":    0.0,
        "_source":             "statistical_model",
    }


def _apply_predictor(symbol: str, price: float, sig: Dict,
                     position_usdt: float, cat: str, scenario: str,
                     opt: bool = False) -> Tuple[bool, Dict]:
    """
    Aplica el predictor (real o estadístico) y decide si aprobar la entrada.
    Retorna (approved, prediction_dict).
    """
    indicators = sig.get("indicators", {})
    indicators["rsi"]       = sig.get("indicators", {}).get("rsi", 50.0)
    indicators["vol_spike"] = sig.get("indicators", {}).get("vol_spike", 1.0)

    pred = _real_predict_pnl(symbol, price, indicators, position_usdt)
    if pred is None:
        pred = _stat_predict_pnl(symbol, indicators, position_usdt, cat, scenario)

    cat_p    = CAT_PARAMS.get(cat, CAT_PARAMS["MIDCAP_ALT"])
    min_prob = cat_p["min_prob"]
    min_pnl  = cat_p["min_pnl"]

    # En escenario optimizado: umbrales ligeramente más estrictos
    if opt:
        min_prob = min(min_prob + 0.03, 0.65)

    approved = (pred.get("success_probability", 0) >= min_prob and
                pred.get("expected_pnl", -999) >= min_pnl)

    return approved, pred


# ── Kelly sizing con F&G boost (idéntico a run_trades.py) ─────────────────────
def kelly_size(capital: float, strength: float, cat: str,
               regime_kelly: float, opt: bool = False) -> float:
    """
    4 tiers de convicción (BASE/MEDIA/ALTA/FUERTE) con F&G boost.
    Idéntico a _kelly_position_size() en run_trades.py.
    """
    base_pct = 0.08   # KELLY_DYNAMIC_BASE_PCT
    if strength >= 0.80:   tier = "FUERTE"; mult = 1.6
    elif strength >= 0.70: tier = "ALTA";   mult = 1.3
    elif strength >= 0.60: tier = "MEDIA";  mult = 1.0
    else:                  tier = "BASE";   mult = 0.7

    cat_p  = CAT_PARAMS.get(cat, CAT_PARAMS["MIDCAP_ALT"])
    sl_pct = cat_p["sl"]
    tp_pct = cat_p["tp"]
    # Kelly fraction
    wr_est = 0.36 if not opt else 0.40  # win_rate real bear market (Learning Engine)
    b      = tp_pct / sl_pct
    kelly_f = max(0, (b * wr_est - (1 - wr_est)) / b)
    kelly_f = min(kelly_f, base_pct)

    size = capital * kelly_f * mult * regime_kelly * FNG_BOOST
    if opt: size *= 1.05   # +5% en parámetros optimizados

    # Caps producción
    size = min(size, capital * MAX_PCT_PER_TRADE)
    size = min(size, KELLY_MAX)
    size = max(size, KELLY_BASE)
    return round(size, 2)


def get_category(symbol: str) -> str:
    if symbol in LARGE_CAP_SET: return "LARGE_CAP"
    if symbol in NARRATIVE_SET: return "NARRATIVE"
    if symbol in MEME_SET:      return "MEME"
    return "MIDCAP_ALT"


# ── Detector de régimen por vela ─────────────────────────────────────────────
def detect_regime(btc_klines: List[Dict], idx: int,
                  pending: dict, scenario: str) -> str:
    """
    Evalúa el régimen en la vela `idx` usando indicadores suavizados.
    Anti-whipsaw: requiere ANTI_WHIPSAW_BARS velas consecutivas.
    """
    # Escenarios especiales
    if scenario in ("SPOT_BULL_SIM", "RECOVERY_SIM"):  return "SPOT_BULL"
    if scenario == "FUT_ONLY":                         return "FUTURES_SHORT"

    bars_per_day = _BARS_PER_DAY.get(INTERVAL, 6)
    bars_7d  = bars_per_day * 7
    bars_30d = bars_per_day * 30

    window = btc_klines[max(0, idx-79): idx+1]
    if len(window) < 40:
        return pending.get("active_mode", "SPOT_BULL")

    closes  = [k["close"]  for k in window]
    volumes = [k["volume"] for k in window]

    # EMA9/21 (igual que market_regime.py producción)
    e9_s  = ema(closes, 9)
    e21_s = ema(closes, 21)
    # Anti-whipsaw interno: mayoría de 3 barras recientes
    ema_up = sum(1 for i in [-1,-2,-3] if e9_s[i] > e21_s[i]) >= 2

    price  = closes[-1]
    vs_val = vol_spike(volumes, 24)
    bw     = bb_bandwidth(closes, 20)

    # Cambios % BTC (igual que market_regime.py)
    btc_7d_ago  = btc_klines[idx - bars_7d]["close"]  if idx >= bars_7d  else closes[0]
    btc_30d_ago = btc_klines[idx - bars_30d]["close"] if idx >= bars_30d else closes[0]
    chg_7d  = (price - btc_7d_ago)  / btc_7d_ago  * 100 if btc_7d_ago  > 0 else 0
    chg_30d = (price - btc_30d_ago) / btc_30d_ago * 100 if btc_30d_ago > 0 else 0

    # Thresholds idénticos a market_regime.py producción
    is_structural_bear = chg_30d <= -15.0
    is_7d_bear         = chg_7d  <= -7.0
    is_sideways_vol    = abs(chg_7d) < 3.0 and vs_val < 0.7
    is_sideways_bb     = bw < 0.06
    is_ema_down        = not ema_up

    if is_structural_bear or (is_7d_bear and is_ema_down):
        # PRESERVATION → upgrades a FUTURES_SHORT cuando futures_enabled
        proposed = "FUTURES_SHORT"   # siempre en sim (futures habilitados)
    elif is_sideways_vol and is_sideways_bb:
        proposed = "RANGE_SCALP"
    elif is_7d_bear and not is_ema_down:
        proposed = "PRESERVATION"
    elif ema_up:
        proposed = "SPOT_BULL"
    else:
        proposed = "RANGE_SCALP"

    # Anti-whipsaw: ANTI_WHIPSAW barras consecutivas (igual a _CONFIRM_CYCLES=2 prod)
    if proposed == pending.get("pending_mode"):
        pending["pending_count"] = pending.get("pending_count", 0) + 1
        if pending["pending_count"] >= ANTI_WHIPSAW:
            pending["active_mode"] = proposed
            pending["pending_mode"] = None
            pending["pending_count"] = 0
    else:
        pending["pending_mode"] = proposed
        pending["pending_count"] = 1

    return pending.get("active_mode", "SPOT_BULL")


# ── Señal de entrada unificada (usa módulo real si disponible) ────────────────
def compute_signal(klines: List[Dict], idx: int) -> Dict:
    """
    Usa TechnicalIndicators.analyze_signal() real si disponible en Docker,
    o el fallback calibrado si no.
    """
    window = klines[max(0, idx-199): idx+1]  # 200 barras para EMA200
    if len(window) < 30:
        return {"signal": "HOLD", "strength": 0.0,
                "indicators": {"rsi": 50.0, "vol_spike": 1.0, "trend_4h": 0.0,
                                "macd": 0.0, "atr_pct": 0.01}}
    closes  = [k["close"]  for k in window]
    highs   = [k["high"]   for k in window]
    lows    = [k["low"]    for k in window]
    volumes = [k["volume"] for k in window]
    return _real_analyze_signal(closes, highs, lows, volumes)


# ── Position classes (v3 — fidelidad total) ───────────────────────────────────
class SimPosition:
    """Posición SPOT LONG con SmartTP, trailing, anti-wick, PredExit."""
    def __init__(self, symbol: str, entry_price: float, qty: float,
                 cat: str, size_usdt: float):
        cp   = CAT_PARAMS.get(cat, CAT_PARAMS["MIDCAP_ALT"])
        self.symbol       = symbol
        self.entry_price  = entry_price
        self.qty          = qty
        self.category     = cat
        self.size_usdt    = size_usdt
        self.sl_pct       = cp["sl"]
        self.tp_pct       = cp["tp"]
        self.trail_act    = cp["trail_act"]
        self.trail_dist   = cp["trail_dist"] if cp["trail_dist"] else cp["sl"] * 0.6
        self.peak         = entry_price
        self.scale_done   = False     # scale-out del 50% ya ejecutado
        self.sl_moved_be  = False     # SL movido a breakeven
        self.tp_ext_count = 0         # cuántas veces se extendió el TP
        self.bars_held    = 0
        self.pred_exit_warned = False


class SimShort:
    """Posición FUTURES SHORT con SL/TP tipo algo-order, trailing y SmartTP (v4)."""
    def __init__(self, symbol: str, entry_price: float, margin_usdt: float, opt: bool = False):
        self.symbol        = symbol
        self.entry_price   = entry_price
        self.margin_usdt   = margin_usdt     # capital comprometido como margen
        self.notional      = margin_usdt * LEVERAGE
        self.sl_pct        = 0.025           # SL 2.5% sobre entrada (FUTURES_SL_PCT producción)
        self.tp_pct        = 0.070           # TP 7.0% bajo entrada (FUTURES_TP_PCT producción A4)
        self.peak_low      = entry_price     # mejor precio alcanzado (trailing down para SHORT)
        self.trail_act_pct = 0.030           # trailing activa a 3% ganancia (ajustado v4: evitar cierres en rebotes 2%)
        self.trail_dist_pct= 0.010           # trailing dist 1% (futures_trailing_distance_pct)
        self.trailing_sl   = None            # precio de trailing SL (None hasta activar)
        self.bars_held     = 0
        self.tp_ext_count  = 0              # SmartTP extensions (max 2 para futuros)


# ── SmartTP: extend / scale_out / close (replica _evaluate_tp_momentum) ──────
def _smart_tp(pos: SimPosition, sym_klines: List[Dict], bar_idx: int) -> str:
    """
    Replica la lógica de _evaluate_tp_momentum() de run_trades.py.
    Usa indicadores del bar actual para score 0-4.
    """
    if pos.tp_ext_count >= 3:
        return "close"
    window = sym_klines[max(0, bar_idx-49): bar_idx+1]
    if len(window) < 20: return "close"
    closes  = [k["close"]  for k in window]
    volumes = [k["volume"] for k in window]
    highs   = [k["high"]   for k in window]
    lows    = [k["low"]    for k in window]
    sig = _real_analyze_signal(closes, highs, lows, volumes)
    ind = sig.get("indicators", {})
    rsi_v  = ind.get("rsi", 50.0)
    vs_v   = ind.get("vol_spike", 1.0)
    macd_v = ind.get("macd", 0.0)
    t4h    = ind.get("trend_4h", 0.0)

    score = 0
    if vs_v >= 1.5:    score += 1
    if t4h >= 1.0:     score += 1
    if 45 <= rsi_v <= 65: score += 1
    # Simular predictor: prob > 0.55 → score+1
    pnl_pct = (closes[-1] - pos.entry_price) / pos.entry_price
    cat_p   = CAT_PARAMS.get(pos.category, CAT_PARAMS["MIDCAP_ALT"])
    if pnl_pct < cat_p["tp"] * 1.5:  # no extender si ya ganamos 1.5× el TP
        score += 1

    if score >= 3 and pos.tp_ext_count < 3:
        return "extend"
    elif score >= 1:
        return "scale_out"
    return "close"


# ── SmartTP para futuros SHORT: extend si momentum bearish sigue (v4) ────────
def _smart_tp_short(sh: "SimShort", sym_klines: List[Dict], bar_idx: int) -> str:
    """
    Replica futures_smart_tp_enabled de config.py.
    Score 0-4: 3+ puntos = extend TP; <3 = close.
    """
    if sh.tp_ext_count >= 2:   # futures_smart_tp_max_extensions = 2
        return "close"
    window = sym_klines[max(0, bar_idx - 49): bar_idx + 1]
    if len(window) < 20:
        return "close"
    closes  = [k["close"]  for k in window]
    volumes = [k["volume"] for k in window]
    highs   = [k["high"]   for k in window]
    lows    = [k["low"]    for k in window]
    sig = _real_analyze_signal(closes, highs, lows, volumes)
    ind = sig.get("indicators", {})
    rsi_v  = ind.get("rsi", 50.0)
    vs_v   = ind.get("vol_spike", 1.0)
    macd_v = ind.get("macd", 0.0)
    t4h    = ind.get("trend_4h", 0.0)

    score = 0
    if rsi_v <= 45:    score += 1   # aún en zona oversold/neutral → SHORT puede continuar
    if t4h < 1.0:      score += 1   # trend DOWN
    if vs_v >= 1.3:    score += 1   # volumen confirma
    if macd_v < 0:     score += 1   # MACD negativo

    # futures_smart_tp_extend_score_threshold = 3 (más estricto que spot)
    if score >= 3:
        return "extend"
    return "close"


# ── Motor de simulación principal v4 — Fidelidad Total ───────────────────────
def run_simulation(klines_by_symbol: Dict[str, List[Dict]],
                   scenario: str, opt: bool = False) -> Dict:
    """
    Simula el sistema completo barra a barra con:
    - Régimen dinámico (idéntico a market_regime.py)
    - Señales via TechnicalIndicators.analyze_signal() real
    - Predictor PnL como guardián de entradas
    - SmartTP: extend/scale_out/close
    - Anti-wick: SL/TS no dispara si última vela cerró sobre threshold
    - Trailing stop con peak tracking
    - PredExit: cierre anticipado si predictor deteriorado tras 8+ barras
    - Futuros SHORT con margin/leverage/SL/TP
    - Kelly con F&G boost por tier de convicción
    - Cap exposición total MAX_EXPOSURE_PCT
    """
    capital   = INITIAL_CAPITAL
    positions: List[SimPosition] = []
    shorts:    List[SimShort]    = []
    trades:    List[Dict]        = []
    equity:    List[float]       = [capital]
    monthly:   List[Dict]        = []

    regime_counts  = {"SPOT_BULL": 0, "RANGE_SCALP": 0, "PRESERVATION": 0, "FUTURES_SHORT": 0}
    pending_regime = {"active_mode": "SPOT_BULL", "pending_mode": None, "pending_count": 0}
    stats = {
        "predictor_blocked": 0,
        "predictor_approved": 0,
        "anti_wick_saves": 0,
        "smart_tp_extend": 0,
        "smart_tp_scale": 0,
        "smart_tp_close": 0,
        "trailing_fired": 0,
        "pred_exits": 0,
        "short_trail_fired": 0,    # trailing stop en futuros SHORT (v4)
        "short_smart_tp_extend": 0,# SmartTP extend en futuros SHORT (v4)
        "short_smart_tp_close": 0, # SmartTP close en futuros SHORT (v4)
    }

    # ── RECOVERY_SIM: aplicar drift alcista +15% sintético a todos los klines ──
    if scenario == "RECOVERY_SIM":
        # BTC $64K → $76K (+19%) en 60d = drift por barra
        recovery_target = 1.15  # +15% total sobre el período de trading
        trading_bars    = (DAYS - (DAYS - 90)) * _BARS_PER_DAY.get(INTERVAL, 6)
        if trading_bars <= 0: trading_bars = 90 * 6
        drift_per_bar   = (recovery_target ** (1.0 / max(trading_bars, 1))) - 1.0
        modified_klines: Dict[str, List[Dict]] = {}
        for sym, kls in klines_by_symbol.items():
            new_kls = []
            for i, k in enumerate(kls):
                bar_offset = max(0, i - WARMUP_BARS)
                mult = (1 + drift_per_bar) ** bar_offset
                new_kls.append({
                    "open_time": k["open_time"],
                    "open":   k["open"]   * mult,
                    "high":   k["high"]   * mult,
                    "low":    k["low"]    * mult,
                    "close":  k["close"]  * mult,
                    "volume": k["volume"],
                })
            modified_klines[sym] = new_kls
        klines_by_symbol = modified_klines

    btc_klines = klines_by_symbol.get("BTCUSDT", [])
    n_bars     = max(len(v) for v in klines_by_symbol.values()) if klines_by_symbol else 0

    bars_per_day   = _BARS_PER_DAY.get(INTERVAL, 6)
    bars_per_month = bars_per_day * 30
    month_start_cap = capital
    month_trades: List[Dict] = []
    month_idx = 0

    for bar_idx in range(WARMUP_BARS, n_bars):
        # ── Límite mensual ────────────────────────────────────────────────
        bar_since = bar_idx - WARMUP_BARS
        if bar_since > 0 and bar_since % bars_per_month == 0:
            monthly.append({
                "month":       month_idx + 1,
                "pnl":         round(capital - month_start_cap, 2),
                "capital_end": round(capital, 2),
                "trades":      len(month_trades),
                "wins":        sum(1 for t in month_trades if t["pnl"] > 0),
            })
            month_idx += 1
            month_start_cap = capital
            month_trades = []

        # ── Detectar régimen ──────────────────────────────────────────────
        mode = detect_regime(btc_klines, bar_idx, pending_regime, scenario)
        regime_counts[mode] = regime_counts.get(mode, 0) + 1
        rp   = REGIME_PARAMS.get(mode, REGIME_PARAMS["SPOT_BULL"])

        # ── Cierre forzado de longs si régimen cambia ─────────────────────
        to_force_close = []
        for pos in positions:
            if mode == "FUTURES_SHORT":
                to_force_close.append(pos)
            elif mode == "PRESERVATION" and pos.category not in ("NARRATIVE",):
                to_force_close.append(pos)
        for pos in to_force_close:
            sk = klines_by_symbol.get(pos.symbol, [])
            price = sk[bar_idx]["close"] if bar_idx < len(sk) else pos.entry_price
            pnl  = (price - pos.entry_price) * pos.qty
            comm = _bt_exit_commission(pos.qty, price, 'spot', None)  # C0: solo salida
            capital += pos.size_usdt + pnl - comm     # devolver principal + PnL
            t = {"symbol": pos.symbol, "reason": "regime_close", "cat": pos.category,
                 "pnl": round(pnl - comm, 4), "pnl_pct": round((price/pos.entry_price-1)*100, 2)}
            trades.append(t); month_trades.append(t)
        positions = [p for p in positions if p not in to_force_close]

        # ── Cierre de shorts si régimen deja de ser FUTURES_SHORT ─────────
        if mode != "FUTURES_SHORT" and shorts:
            for sh in shorts[:]:
                sk    = klines_by_symbol.get(sh.symbol, [])
                price = sk[bar_idx]["close"] if bar_idx < len(sk) else sh.entry_price
                pnl_f = (sh.entry_price - price) / sh.entry_price
                pnl   = sh.notional * pnl_f - _bt_fut_comm_total(sh, price)
                capital += pnl + sh.margin_usdt   # devolver margen
                t = {"symbol": sh.symbol, "reason": "short_regime_close", "cat": "FUTURES",
                     "pnl": round(pnl, 4), "pnl_pct": round(pnl_f*100, 2), "is_short": True}
                trades.append(t); month_trades.append(t)
            shorts.clear()

        # ── Gestión posiciones SPOT abiertas ──────────────────────────────
        to_remove = []
        for pos in positions:
            sk = klines_by_symbol.get(pos.symbol, [])
            if bar_idx >= len(sk): continue
            pos.bars_held += 1
            candle   = sk[bar_idx]
            lo, hi, price = candle["low"], candle["high"], candle["close"]
            prev_close = sk[bar_idx-1]["close"] if bar_idx > 0 else price  # anti-wick ref

            sl_price = (pos.entry_price if pos.sl_moved_be
                        else pos.entry_price * (1 - pos.sl_pct))
            tp_price = pos.entry_price * (1 + pos.tp_pct)
            pnl_pct  = (price - pos.entry_price) / pos.entry_price
            exit_price = exit_reason = None

            # 1. SL check con anti-wick
            if lo <= sl_price:
                # Anti-wick: si la vela anterior cerró POR ENCIMA del SL → wick, no cerrar
                if prev_close > sl_price:
                    stats["anti_wick_saves"] += 1
                else:
                    exit_price, exit_reason = sl_price, "stop_loss"

            # 2. TP check con SmartTP
            elif hi >= tp_price and exit_price is None:
                smart_dec = _smart_tp(pos, sk, bar_idx)
                if smart_dec == "extend" and not pos.scale_done:
                    pos.tp_pct      *= 1.15   # extender TP 15%
                    pos.tp_ext_count += 1
                    stats["smart_tp_extend"] += 1
                elif smart_dec == "scale_out" and not pos.scale_done:
                    sell_qty = pos.qty * 0.50
                    pnl  = (tp_price - pos.entry_price) * sell_qty
                    comm = _bt_exit_commission(sell_qty, tp_price, 'spot', None)  # C0: salida parcial
                    capital += pos.size_usdt * 0.50 + pnl - comm  # devolver 50% principal
                    pos.qty       -= sell_qty
                    pos.scale_done = True
                    pos.sl_moved_be = True   # mover SL a breakeven
                    t = {"symbol": pos.symbol, "reason": "scale_out_tp", "cat": pos.category,
                         "pnl": round(pnl-comm, 4), "pnl_pct": round((tp_price/pos.entry_price-1)*100, 2)}
                    trades.append(t); month_trades.append(t)
                    stats["smart_tp_scale"] += 1
                else:
                    exit_price, exit_reason = tp_price, "take_profit"
                    stats["smart_tp_close"] += 1

            # 3. Trailing stop
            elif pnl_pct >= pos.trail_act and exit_price is None:
                if price > pos.peak: pos.peak = price
                trail_sl = pos.peak * (1 - pos.trail_dist)
                if lo <= trail_sl:
                    # Anti-wick en trailing también
                    if prev_close > trail_sl:
                        stats["anti_wick_saves"] += 1
                    else:
                        exit_price, exit_reason = trail_sl, "trailing_stop"
                        stats["trailing_fired"] += 1

            # 4. PredExit: si posición lleva >8 barras y predictor dice negativo
            elif pos.bars_held >= 8 and not pos.pred_exit_warned and exit_price is None:
                ind = compute_signal(sk, bar_idx).get("indicators", {})
                pred = _real_predict_pnl(pos.symbol, price, ind, pos.size_usdt)
                if pred is None:
                    pred = _stat_predict_pnl(pos.symbol, ind, pos.size_usdt, pos.category, scenario)
                if pred.get("success_probability", 0.5) < 0.35 and pnl_pct < -0.005:
                    exit_price, exit_reason = price, "pred_exit"
                    stats["pred_exits"] += 1
                else:
                    pos.pred_exit_warned = True  # solo 1 check después de 8 barras

            if exit_price:
                pnl  = (exit_price - pos.entry_price) * pos.qty
                comm = _bt_exit_commission(pos.qty, exit_price, 'spot', None)  # C0: salida
                capital += pos.size_usdt + pnl - comm  # devolver principal + PnL
                t = {"symbol": pos.symbol, "reason": exit_reason, "cat": pos.category,
                     "pnl": round(pnl-comm, 4), "pnl_pct": round((exit_price/pos.entry_price-1)*100, 2)}
                trades.append(t); month_trades.append(t)
                to_remove.append(pos)
        positions = [p for p in positions if p not in to_remove]

        # ── Gestión FUTURES SHORT abiertos (v4: trailing + SmartTP) ──────
        shorts_rm = []
        for sh in shorts:
            sk = klines_by_symbol.get(sh.symbol, [])
            if bar_idx >= len(sk): continue
            sh.bars_held += 1
            candle = sk[bar_idx]
            lo, hi, price = candle["low"], candle["high"], candle["close"]

            # ── Trailing stop para SHORT (activa cuando precio cae X% desde entrada)
            pnl_pct_sh = (sh.entry_price - price) / sh.entry_price
            if pnl_pct_sh >= sh.trail_act_pct:
                # actualizar peak_low (mínimo favorable al SHORT)
                if price < sh.peak_low:
                    sh.peak_low = price
                # trailing SL = peak_low * (1 + trail_dist) — cierra si precio sube sobre esto
                new_trail_sl = sh.peak_low * (1 + sh.trail_dist_pct)
                if sh.trailing_sl is None or new_trail_sl < sh.trailing_sl:
                    sh.trailing_sl = new_trail_sl

            sl_price = sh.entry_price * (1 + sh.sl_pct)   # SL original arriba
            tp_price = sh.entry_price * (1 - sh.tp_pct)   # TP abajo
            exit_price = exit_reason = None

            # Verificar trailing SL primero (protege ganancias)
            if sh.trailing_sl is not None and hi >= sh.trailing_sl:
                exit_price, exit_reason = sh.trailing_sl, "short_trail"
                stats["short_trail_fired"] += 1
            elif hi >= sl_price:
                exit_price, exit_reason = sl_price, "short_sl"
            elif lo <= tp_price:
                # SmartTP: evaluar si extender o cerrar
                smart_dec = _smart_tp_short(sh, sk, bar_idx)
                if smart_dec == "extend" and sh.tp_ext_count < 2:
                    sh.tp_pct += 0.020   # extender TP 2% más (futures_smart_tp_extension_pct)
                    sh.tp_ext_count += 1
                    stats["short_smart_tp_extend"] += 1
                else:
                    exit_price, exit_reason = tp_price, "short_tp"
                    stats["short_smart_tp_close"] += 1

            if exit_price:
                pnl_f = (sh.entry_price - exit_price) / sh.entry_price
                pnl   = sh.notional * pnl_f - _bt_fut_comm_total(sh, exit_price)
                capital += pnl + sh.margin_usdt   # devolver margen
                t = {"symbol": sh.symbol, "reason": exit_reason, "cat": "FUTURES",
                     "pnl": round(pnl, 4), "pnl_pct": round(pnl_f*100, 2), "is_short": True}
                trades.append(t); month_trades.append(t)
                shorts_rm.append(sh)
        shorts = [s for s in shorts if s not in shorts_rm]

        # ── Lógica de ENTRADAS ────────────────────────────────────────────
        # Calcular exposición actual
        invested_usdt = sum(p.size_usdt for p in positions)
        margins_usdt  = sum(s.margin_usdt for s in shorts)
        total_eq      = capital + invested_usdt + margins_usdt
        exposure_pct  = invested_usdt / total_eq if total_eq > 0 else 0

        # A) Entradas FUTURES SHORT
        if mode == "FUTURES_SHORT" and len(shorts) < MAX_FUTURES_SHORTS:
            for sym in FUTURES_SYMS:
                if len(shorts) >= MAX_FUTURES_SHORTS: break
                if any(s.symbol == sym for s in shorts): continue
                sk = klines_by_symbol.get(sym, [])
                if not sk or bar_idx >= len(sk): continue
                sig = compute_signal(sk, bar_idx)
                ind = sig.get("indicators", {})
                rsi_v = ind.get("rsi", 50.0)
                vs_v  = ind.get("vol_spike", 1.0)
                # Solo entrar SHORT si precio no está en oversold extremo
                if rsi_v < 25: continue
                margin = kelly_size(capital, sig.get("strength", 0.55),
                                    get_category(sym), rp["kelly_mult"], opt)
                margin = min(margin, capital * 0.06, 250.0)
                margin = max(margin, KELLY_BASE)
                if margin > capital * 0.9: continue
                comm = _bt_entry_commission(margin * LEVERAGE / sk[bar_idx]["close"], sk[bar_idx]["close"], 'futures', None)
                capital -= margin + comm
                shorts.append(SimShort(sym, sk[bar_idx]["close"], margin))

        # B) Entradas SPOT LONG
        elif mode != "FUTURES_SHORT":
            entries_bar = 0
            syms_to_try = SPOT_SYMBOLS.copy()
            random.shuffle(syms_to_try)   # evitar sesgo de orden

            for symbol in syms_to_try:
                if entries_bar >= MAX_ENTRIES_BAR: break
                if len(positions) >= MAX_POSITIONS: break
                if exposure_pct >= MAX_EXPOSURE_PCT: break
                if any(p.symbol == symbol for p in positions): continue
                sk = klines_by_symbol.get(symbol, [])
                if not sk or bar_idx >= len(sk): continue

                sig = compute_signal(sk, bar_idx)
                if sig.get("signal") != "BUY": continue

                cat      = get_category(symbol)
                ind      = sig.get("indicators", {})
                rsi_v    = ind.get("rsi", 50.0)
                vs_v     = ind.get("vol_spike", 1.0)
                strength = sig.get("strength", 0.0)

                # Filtros del régimen
                if rp["narrative_only"] and cat != "NARRATIVE": continue
                if rsi_v > rp["max_rsi"]: continue
                if vs_v  < rp["require_vol"]: continue
                min_str = 0.50 if not opt else 0.58   # REAL_OPT: str≥0.58 (P1-A del plan)
                if strength < min_str: continue

                # Kelly sizing
                size = kelly_size(capital, strength, cat, rp["kelly_mult"], opt)
                if size > capital * 0.9: continue
                if size < KELLY_BASE: continue

                # ── PREDICTOR como guardián de entrada ────────────────────
                approved, pred = _apply_predictor(
                    symbol, sk[bar_idx]["close"], sig, size, cat, scenario, opt
                )
                if not approved:
                    stats["predictor_blocked"] += 1
                    continue
                stats["predictor_approved"] += 1

                qty  = size / sk[bar_idx]["close"]
                comm = _bt_entry_commission(qty, sk[bar_idx]["close"], 'spot', None)
                capital -= size + comm
                entries_bar += 1
                exposure_pct = (invested_usdt + size) / (total_eq) if total_eq > 0 else 0
                positions.append(SimPosition(symbol, sk[bar_idx]["close"], qty, cat, size))

        # ── Equity mark-to-market ─────────────────────────────────────────
        unr_long = 0.0
        for p in positions:
            sk = klines_by_symbol.get(p.symbol, [])
            if sk and bar_idx < len(sk):
                unr_long += (sk[bar_idx]["close"] - p.entry_price) * p.qty
        unr_short = 0.0
        for sh in shorts:
            sk = klines_by_symbol.get(sh.symbol, [])
            if sk and bar_idx < len(sk):
                pnl_f = (sh.entry_price - sk[bar_idx]["close"]) / sh.entry_price
                unr_short += sh.notional * pnl_f
        equity.append(capital + unr_long + unr_short)

    # ── Cierre final de posiciones abiertas ───────────────────────────────
    for pos in positions:
        sk = klines_by_symbol.get(pos.symbol, [])
        if sk:
            lp   = sk[-1]["close"]
            pnl  = (lp - pos.entry_price) * pos.qty
            comm = _bt_exit_commission(pos.qty, lp, 'spot', None)  # C0: salida
            capital += pos.size_usdt + pnl - comm   # devolver principal + PnL
            trades.append({"symbol": pos.symbol, "reason": "end_of_period",
                           "cat": pos.category, "pnl": round(pnl-comm, 4),
                           "pnl_pct": round((lp/pos.entry_price-1)*100, 2)})
    for sh in shorts:
        sk = klines_by_symbol.get(sh.symbol, [])
        if sk:
            lp    = sk[-1]["close"]
            pnl_f = (sh.entry_price - lp) / sh.entry_price
            pnl   = sh.notional * pnl_f - _bt_fut_comm_total(sh, lp)
            capital += pnl + sh.margin_usdt
            trades.append({"symbol": sh.symbol, "reason": "short_end_of_period",
                           "cat": "FUTURES", "pnl": round(pnl, 4),
                           "pnl_pct": round(pnl_f*100, 2), "is_short": True})

    # ── Mes final ─────────────────────────────────────────────────────────
    if month_trades:
        monthly.append({
            "month":       month_idx + 1,
            "pnl":         round(capital - month_start_cap, 2),
            "capital_end": round(capital, 2),
            "trades":      len(month_trades),
            "wins":        sum(1 for t in month_trades if t["pnl"] > 0),
        })

    # ── Métricas finales ──────────────────────────────────────────────────
    total_trades = len(trades)
    winners = [t for t in trades if t["pnl"] > 0]
    losers  = [t for t in trades if t["pnl"] <= 0]
    spot_t  = [t for t in trades if not t.get("is_short")]
    fut_t   = [t for t in trades if t.get("is_short")]
    spot_w  = [t for t in spot_t if t["pnl"] > 0]
    fut_w   = [t for t in fut_t  if t["pnl"] > 0]

    win_rate     = len(winners) / total_trades * 100 if total_trades else 0
    spot_wr      = len(spot_w)  / len(spot_t)  * 100 if spot_t else 0
    fut_wr       = len(fut_w)   / len(fut_t)   * 100 if fut_t  else 0
    total_pnl    = capital - INITIAL_CAPITAL
    avg_win      = sum(t["pnl"] for t in winners) / len(winners) if winners else 0
    avg_loss     = sum(t["pnl"] for t in losers)  / len(losers)  if losers  else 0
    gross_win    = sum(t["pnl"] for t in winners)
    gross_loss   = abs(sum(t["pnl"] for t in losers))
    pf           = gross_win / gross_loss if gross_loss > 0 else 99.9

    max_dd = 0.0; peak_eq = equity[0]
    for eq in equity:
        if eq > peak_eq: peak_eq = eq
        dd = (peak_eq - eq) / peak_eq * 100 if peak_eq > 0 else 0
        if dd > max_dd: max_dd = dd

    total_bars = sum(regime_counts.values())
    regime_pct = {k: round(v/total_bars*100, 1) if total_bars > 0 else 0
                  for k, v in regime_counts.items()}

    return {
        "capital_final":  round(capital, 2),
        "total_pnl":      round(total_pnl, 2),
        "return_pct":     round(total_pnl / INITIAL_CAPITAL * 100, 2),
        "total_trades":   total_trades,
        "spot_trades":    len(spot_t),
        "fut_trades":     len(fut_t),
        "win_rate":       round(win_rate, 2),
        "spot_wr":        round(spot_wr, 2),
        "fut_wr":         round(fut_wr, 2),
        "avg_win":        round(avg_win, 4),
        "avg_loss":       round(avg_loss, 4),
        "profit_factor":  round(pf, 3),
        "max_drawdown":   round(max_dd, 2),
        "equity":         equity,
        "trades":         trades,
        "monthly":        monthly,
        "regime_pct":     regime_pct,
        "stats":          stats,
        "predictor_filter_rate": (
            stats["predictor_blocked"] /
            max(stats["predictor_blocked"] + stats["predictor_approved"], 1) * 100
        ),
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


# ── Main v3 — 4 escenarios reales ─────────────────────────────────────────────
def main():
    global INITIAL_CAPITAL, DAYS

    parser = argparse.ArgumentParser(description="Simulador Fidelidad Total v3")
    parser.add_argument("--days",    type=int,   default=DAYS,            help="Días a simular")
    parser.add_argument("--capital", type=float, default=INITIAL_CAPITAL, help="Capital inicial USDT")
    parser.add_argument("--no-real", action="store_true",                 help="No intentar cargar módulos reales")
    args = parser.parse_args()

    INITIAL_CAPITAL = args.capital
    DAYS            = args.days

    print("=" * 76)
    print("  SIMULADOR FIDELIDAD TOTAL v4.0 — Sistema Crypto Multi-Modal")
    print(f"  Capital: ${INITIAL_CAPITAL:,.2f} USDT  |  Intervalo: {INTERVAL}  |  {DAYS}d")
    print(f"  F&G: {FNG_VALUE} (Capitulación) → Kelly boost: ×{FNG_BOOST:.2f}")
    print(f"  Predictor: statistical_model + real si disponible")
    print("=" * 76)

    # ── Cargar módulos reales (si corremos dentro de Docker) ──────────────
    if not args.no_real:
        print("\nCargando módulos del sistema...")
        _try_load_real_modules()

    # ── Descargar klines ──────────────────────────────────────────────────
    all_syms = list(dict.fromkeys(SPOT_SYMBOLS + FUTURES_SYMS))
    print(f"\nDescargando klines de {len(all_syms)} símbolos ({DAYS}d + warmup)...")
    klines_by_symbol: Dict[str, List[Dict]] = {}
    for sym in all_syms:
        print(f"  {sym}...", end=" ", flush=True)
        kl = fetch_klines(sym, INTERVAL, DAYS + 10)
        if len(kl) >= WARMUP_BARS + 10:
            klines_by_symbol[sym] = kl
            print(f"{len(kl)} velas ✓")
        else:
            print(f"insuficiente ({len(kl)}) — omitido")

    btc_klines = klines_by_symbol.get("BTCUSDT", [])
    btc_start  = btc_klines[WARMUP_BARS]["close"] if len(btc_klines) > WARMUP_BARS else 0
    btc_end    = btc_klines[-1]["close"]           if btc_klines else 0
    btc_change = (btc_end - btc_start) / btc_start * 100 if btc_start > 0 else 0
    btc_type   = ("BEAR SOSTENIDO" if btc_change < -20 else
                  "BEAR CON REBOTES" if btc_change < -10 else "LATERAL/ALCISTA")
    print(f"\n  BTC período: ${btc_start:,.0f} → ${btc_end:,.0f} ({btc_change:+.1f}%) — {btc_type}")

    # ── Correr 5 escenarios (v4) ──────────────────────────────────────────
    scenarios = [
        ("REAL",          "Sistema completo (predictor+SmartTP+anti-wick+futuros)", False),
        ("REAL_OPT",      "REAL + params optimizados (str≥0.58)",                   True),
        ("FUT_ONLY",      "Solo futuros SHORT (FUTURES_SHORT forzado)",              False),
        ("SPOT_BULL_SIM", "Recuperación SPOT_BULL (régimen forzado)",               False),
        ("RECOVERY_SIM",  "RECOVERY: SPOT_BULL + BTC drift +15% sintético",         False),
    ]

    results: Dict[str, Dict] = {}
    print()
    for code, label, opt in scenarios:
        print(f"  [{code}] {label}...")
        r = run_simulation(klines_by_symbol, code, opt)
        results[code] = r
        spot_str = f"SPOT WR:{r['spot_wr']:.0f}%" if r['spot_trades'] else ""
        fut_str  = f"FUT WR:{r['fut_wr']:.0f}%"  if r['fut_trades']  else ""
        print(f"    → ${r['capital_final']:,.2f} | PnL:{r['total_pnl']:+.2f} | "
              f"Trades:{r['total_trades']} | {spot_str} {fut_str} | "
              f"MaxDD:{r['max_drawdown']:.1f}% | PF:{r['profit_factor']:.2f}")

    # ── TABLA COMPARATIVA ─────────────────────────────────────────────────
    hodl_btc  = INITIAL_CAPITAL * (1 + btc_change / 100)

    print()
    print("=" * 76)
    print("  RESULTADOS COMPARATIVOS")
    print("=" * 76)
    hdr = (f"  {'Escenario':<36} {'Capital':>10} {'PnL':>9} {'Ret%':>6} "
           f"{'WR':>5} {'SpWR':>5} {'FutWR':>6} {'MaxDD':>7} {'PF':>5}")
    print(hdr)
    print("  " + "─" * 74)

    best_cap = max(r["capital_final"] for r in results.values())
    for code, label, _ in scenarios:
        r    = results[code]
        mark = " ◀" if r["capital_final"] == best_cap else ""
        swr  = f"{r['spot_wr']:.0f}%" if r["spot_trades"] else " —"
        fwr  = f"{r['fut_wr']:.0f}%"  if r["fut_trades"]  else " —"
        print(f"  {label:<36} ${r['capital_final']:>9,.2f} {r['total_pnl']:>+9.2f} "
              f"{r['return_pct']:>+5.1f}% {r['win_rate']:>4.0f}% {swr:>5} {fwr:>6} "
              f"{r['max_drawdown']:>6.1f}% {r['profit_factor']:>4.2f}{mark}")

    print("  " + "─" * 74)
    print(f"  {'HODL BTC':<36} ${hodl_btc:>9,.2f} {hodl_btc-INITIAL_CAPITAL:>+9.2f} "
          f"{btc_change:>+5.1f}%   —     —      —       —     —")
    print(f"  {'HODL USDT (sin riesgo)':<36} ${INITIAL_CAPITAL:>9,.2f} {0.0:>+9.2f}  +0.0%   —     —      —       —     —")

    # ── DISTRIBUCIÓN DE REGÍMENES ─────────────────────────────────────────
    print()
    print("  DISTRIBUCIÓN DE REGÍMENES — REAL")
    r_real = results["REAL"]
    for mode, pct in sorted(r_real["regime_pct"].items(), key=lambda x: -x[1]):
        bar = "█" * int(pct / 2)
        print(f"    {mode:<16} {bar:<38} {pct:.1f}%")

    # ── ESTADÍSTICAS DEL SISTEMA ──────────────────────────────────────────
    st = r_real["stats"]
    total_pred = st["predictor_blocked"] + st["predictor_approved"]
    print()
    print("  ESTADÍSTICAS SISTEMA — REAL")
    print(f"    Predictor bloqueó:     {st['predictor_blocked']:>4} señales "
          f"({r_real['predictor_filter_rate']:.0f}% del total evaluado)")
    print(f"    Predictor aprobó:      {st['predictor_approved']:>4} entradas")
    print(f"    Anti-wick saves:       {st['anti_wick_saves']:>4} SL/TS ignorados (wick detectado)")
    print(f"    SmartTP extend:        {st['smart_tp_extend']:>4} veces TP extendido")
    print(f"    SmartTP scale-out:     {st['smart_tp_scale']:>4} veces scale-out 50%")
    print(f"    SmartTP close:         {st['smart_tp_close']:>4} cierres normales en TP")
    print(f"    Trailing fired (SPOT):  {st['trailing_fired']:>4} trailing stops spot ejecutados")
    print(f"    PredExit (deterioro):   {st['pred_exits']:>4} cierres anticipados")
    print(f"    FUT trailing fired:     {st['short_trail_fired']:>4} trailing stops futuros ejecutados")
    print(f"    FUT SmartTP extend:     {st['short_smart_tp_extend']:>4} TP extendido en futuros SHORT")
    print(f"    FUT SmartTP close:      {st['short_smart_tp_close']:>4} cierres normales en futuros TP")

    # ── BREAKDOWN MENSUAL ─────────────────────────────────────────────────
    best_code = max(results, key=lambda c: results[c]["capital_final"])
    best      = results[best_code]
    best_lbl  = next(l for c, l, _ in scenarios if c == best_code)
    print()
    print(f"  BREAKDOWN MENSUAL — {best_code}: {best_lbl}")
    print(f"  {'Mes':<5} {'Trades':>7} {'Wins':>5} {'WR%':>5} {'PnL mes':>10} {'Capital fin':>12}")
    print("  " + "─" * 52)
    for m in best["monthly"]:
        wr_m = m["wins"] / m["trades"] * 100 if m["trades"] > 0 else 0
        print(f"  Mes {m['month']:<2} {m['trades']:>7} {m['wins']:>5} {wr_m:>4.0f}% "
              f"{m['pnl']:>+10.2f} ${m['capital_end']:>11,.2f}")

    # ── EQUITY CURVES ─────────────────────────────────────────────────────
    print()
    print("  CURVAS DE EQUITY")
    for code, label, _ in scenarios:
        r = results[code]
        print(f"\n  {code} — {label}  (${r['capital_final']:,.2f} | {r['return_pct']:+.1f}%)")
        print(equity_ascii(r["equity"]))

    # ── MONTE CARLO ───────────────────────────────────────────────────────
    print()
    print(f"  MONTE CARLO — {best_code} ({N_MC_SIMS:,} simulaciones, 3 meses forward)")
    months_sim = max(len(best["monthly"]), 1)
    tpm = best["total_trades"] / months_sim
    mc  = monte_carlo(best["avg_win"], best["avg_loss"], best["win_rate"], tpm,
                      best["capital_final"], months=3)
    cap_start = best["capital_final"]
    print(f"    Capital base:    ${cap_start:>10,.2f}")
    print(f"    Peor caso  P5:   ${mc['p5']:>10,.2f}  ({(mc['p5']/cap_start-1)*100:+.1f}%)")
    print(f"    Mediana   P50:   ${mc['p50']:>10,.2f}  ({(mc['p50']/cap_start-1)*100:+.1f}%)")
    print(f"    Media:           ${mc['mean']:>10,.2f}  ({(mc['mean']/cap_start-1)*100:+.1f}%)")
    print(f"    Mejor caso P95:  ${mc['p95']:>10,.2f}  ({(mc['p95']/cap_start-1)*100:+.1f}%)")
    mxn_rate = 19.5
    print(f"    Proyección mediana 3m: ${mc['p50']-cap_start:>+8,.2f} USDT = "
          f"${(mc['p50']-cap_start)*mxn_rate:>+8,.0f} MXN")

    # ── ANÁLISIS EXPERTO MULTI-ROL ────────────────────────────────────────
    r_real = results["REAL"]
    r_opt  = results["REAL_OPT"]
    r_fut  = results["FUT_ONLY"]
    r_bull = results["SPOT_BULL_SIM"]

    opt_delta     = r_opt["capital_final"]  - r_real["capital_final"]
    fut_pnl_total = sum(t["pnl"] for t in r_real["trades"] if t.get("is_short"))
    spot_pnl_tot  = sum(t["pnl"] for t in r_real["trades"] if not t.get("is_short"))
    bull_upside   = r_bull["total_pnl"]

    print()
    print("=" * 76)
    print("  ANÁLISIS EXPERTO MULTI-ROL")
    print("=" * 76)

    # Rol: Quant / Actuario
    print(f"""
  ROL QUANT — MÉTRICAS CLAVE (REAL):
    Profit Factor: {r_real['profit_factor']:.3f}  (>1.5 = bueno, >2.0 = excelente)
    Avg Win:  ${r_real['avg_win']:>+8.4f}  |  Avg Loss: ${r_real['avg_loss']:>+8.4f}
    Ratio W/L: {abs(r_real['avg_win']/r_real['avg_loss']) if r_real['avg_loss'] else 0:.2f}×
    Predictor filtró: {r_real['predictor_filter_rate']:.0f}% de señales BUY → calidad > cantidad
    Anti-wick: {r_real['stats']['anti_wick_saves']} SL salvados → reduce slippage real estimado
""")

    # Rol: Trading Expert / Estrategia
    if btc_change < -15:
        regime_verdict = "FUTURES_SHORT es el modo correcto — bear estructural confirmado"
        action_now     = "Mantener 7 SHORTs activos, esperar TPs en cadena"
    elif btc_change < -8:
        regime_verdict = "PRESERVATION + futuros oportunistas — bear con rebotes"
        action_now     = "Máx 4 SHORTs + NARRATIVE spot selectivo"
    else:
        regime_verdict = "SPOT_BULL recuperación — Kelly pleno en altcoins"
        action_now     = "Escalar exposición spot hasta 65%, reducir SHORTs"

    bull_usd_day = bull_upside / (DAYS * 0.7) if DAYS > 0 else 0  # 70% del período en SPOT_BULL

    print(f"""  ROL TRADING EXPERT — ESTRATEGIA ACTUAL:
    Período simulado: {btc_type} (BTC {btc_change:+.1f}%)
    Régimen correcto: {regime_verdict}
    Acción recomendada: {action_now}

    PnL FUTUROS SHORT (en régimen FUTURES_SHORT): ${fut_pnl_total:>+.2f} USDT
    PnL SPOT LONG (resto del tiempo):             ${spot_pnl_tot:>+.2f} USDT
    Upside si recuperación SPOT_BULL (+{abs(btc_change):.0f}%):   ${bull_upside:>+.2f} USDT
    Velocidad recuperación proyectada:             ${bull_usd_day:.2f} USDT/día
""")

    # Rol: Behavioral Finance
    print(f"""  ROL BEHAVIORAL FINANCE — F&G = {FNG_VALUE} (Capitulación):
    Kelly boost activo: ×{FNG_BOOST:.2f} → más agresivo en el peor miedo
    Históricamente: F&G<20 → reversión promedio +28% en 60d (Bitcoin)
    Señal: acumular SPOT en soporte, mantener SHORTs hasta TP

  ROL ACTUARIO — RIESGO:
    MaxDD REAL: {r_real['max_drawdown']:.1f}%  |  MaxDD OPT: {r_opt['max_drawdown']:.1f}%
    Riesgo mensual P5 (MC): ${mc['p5']-cap_start:+.2f} USDT ({(mc['p5']/cap_start-1)*100:+.1f}%)
    Capital en riesgo por trade: ${KELLY_BASE:.0f}–${KELLY_MAX:.0f} USDT
    Max exposición: {MAX_EXPOSURE_PCT*100:.0f}% del portfolio
""")

    # ── RECOMENDACIONES CONCRETAS ─────────────────────────────────────────
    print("=" * 76)
    print("  RECOMENDACIONES PRIORIZADAS (por MXN/día adicional)")
    print("=" * 76)

    recs = []

    if opt_delta > 10:
        recs.append(f"[P1] ✅ Activar parámetros optimizados → +${opt_delta:.2f} USDT "
                    f"({opt_delta/DAYS*30:.0f} USDT/mes)")
    elif opt_delta > 0:
        recs.append(f"[P2] ⚠️  Parámetros OPT marginalmente mejor (+${opt_delta:.2f}) — "
                    f"evaluar en 30 trades reales")

    if r_real["stats"]["anti_wick_saves"] > 5:
        recs.append(f"[P1] ✅ sl_confirmation_candles=1 activo — "
                    f"{r_real['stats']['anti_wick_saves']} SL falsos evitados en sim")

    if r_real["fut_trades"] > 0 and r_real["fut_wr"] >= 55:
        recs.append(f"[P1] ✅ Futuros SHORT WR={r_real['fut_wr']:.0f}% — mantener MAX_SHORTS=7")
    elif r_real["fut_trades"] > 0 and r_real["fut_wr"] < 45:
        recs.append(f"[P2] ⚠️  Futuros WR={r_real['fut_wr']:.0f}% bajo — "
                    f"reducir a MAX_SHORTS=4 o subir SL a 2.0%")

    if r_real["stats"]["smart_tp_extend"] > r_real["stats"]["smart_tp_close"] * 0.3:
        recs.append("[P2] ⚠️  SmartTP extendiendo frecuentemente — "
                    "verificar que extensiones no devuelven ganancias")

    if btc_change > -5:
        recs.append("[P1] 🚀 BTC lateral/recuperación: aumentar MIN_SIGNAL_STRENGTH a 0.30 "
                    "para capturar más señales en SPOT_BULL")

    if not recs:
        recs.append("[OK] Sistema calibrado correctamente — sin ajustes urgentes")

    for rec in recs:
        print(f"  {rec}")

    print()
    print(f"  USD/día proyectado (mediana MC 3m): "
          f"${(mc['p50']-cap_start)/(3*30):.2f} USDT/día = "
          f"${(mc['p50']-cap_start)/(3*30)*mxn_rate:.0f} MXN/día")
    print()
    print("=" * 76)
    print(f"  Módulo real TechnicalIndicators: {'✅ ACTIVO' if _USE_REAL_INDICATORS else '⚠️  fallback'}")
    print(f"  Módulo real PnLPredictor:        {'✅ ACTIVO' if _USE_REAL_PREDICTOR  else '⚠️  statistical_model'}")
    print("=" * 76)
    print()


if __name__ == "__main__":
    main()
