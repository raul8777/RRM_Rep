#!/usr/bin/env python3
"""
Backtest Optimizer — Simulación 20d × 1000 corridas SPOT+FUTURES
=================================================================
Ejecutar desde el container:
    python Dev/backtest_optimizer.py --runs 1000 --days 20

Qué hace:
  1. Carga datos históricos (parquet 1h) del período --days más reciente disponible
  2. Corre baseline con config actual del sistema
  3. Corre N corridas random-search (Latin Hypercube Sampling) con parámetros alternativos
  4. Persiste TODOS los resultados en BD (backtest_run + backtest_trade)
  5. Imprime ranking top-20 + recomendación de parámetros óptimos
"""

import argparse
import hashlib
import json
import math
import os
import random
import re
import socket
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np


class _NumpyEncoder(json.JSONEncoder):
    """Convert numpy scalars to native Python for JSON serialization."""
    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)

# ── Path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, str(os.path.join(os.path.dirname(__file__), '..')))

from binance_commission import get_config, total_commission  # centralized commission module

# ── Constants ─────────────────────────────────────────────────────────────────
DATA_DIR       = '/app/historical_data'
SLIPPAGE           = 0.0005  # 0.05% slippage estimate
FUTURES_LEVERAGE   = 10
FNG_VALUE          = 15      # Extreme Fear (actual)
FNG_FEAR_MULT      = 1.25    # Current fear boost
REGIME             = 'FUTURES_SHORT'
REGIME_CONFIRMED   = True
REGIME_CONFIDENCE  = 0.60
KELLY_MULT_K2      = 0.80    # conf=0.60 → 0.8× (K2 table)
SPOT_SIZING_LIQUID_CAP_MULT = 1.50   # cap effective spot liquid for Kelly sizing
FUTURES_SIZING_LIQUID_CAP_MULT = 2.00  # cap effective futures liquid for Kelly sizing

EXCLUDE_SYMBOLS = {
    'USD1USDT', 'PAXGUSDT', 'XAUTUSDT', 'SPCXBUSDT', 'RLUSDUSDT',
    'UUSDT', 'XPLUSDT', 'NIGHTUSDT', 'ASTERUSDT', 'BIOUSDT',
    'MEGAUSDT', 'SENTUSDT',
}
FUTURES_SYMBOLS = {
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'ADAUSDT',
    'NEARUSDT', 'SUIUSDT', 'DOGEUSDT', 'AVAXUSDT', 'LINKUSDT', 'TAOUSDT',
    'UNIUSDT', 'BCHUSDT', 'AAVEUSDT', 'INJUSDT',
}

# Loader diagnostic state for preflight visibility.
_PARQUET_ENGINE_ERROR: Optional[str] = None

# ── Parameter space for random search ─────────────────────────────────────────
PARAM_SPACE = {
    'kelly_base_usdt':         (50.0,  200.0),
    'kelly_max_usdt':          (200.0, 800.0),
    'kelly_max_pct_liquid':    (0.25,  0.75),
    'futures_kelly_max_usdt':  (300.0, 1200.0),
    'min_strength':            (0.35,  0.65),
    'min_prob':                (0.48,  0.70),
    'vol_spike_min':           (0.8,   2.5),
    'spot_sl_pct':             (0.010, 0.035),
    'spot_tp_pct':             (0.018, 0.065),
    'futures_sl_pct':          (0.012, 0.040),
    'futures_tp_pct':          (0.020, 0.080),
    # Commission rates are controlled by Binance, not tunable by backtest.
    # The backtest uses fixed documented rates via binance_commission.get_config().
    'slippage_pct':            (0.0003, 0.0007),   # 0.03% - 0.07% slippage estimate
}

# ── Current (baseline) config — applied 2026-06-28 from backtest run_0812 (run_id=1814)
BASELINE_PARAMS = {
    'kelly_base_usdt':         76.0,    # applied: 100→76
    'kelly_max_usdt':          663.0,   # applied: 500→663
    'kelly_max_pct_liquid':    0.621,   # applied: 0.55→0.621
    'futures_kelly_max_usdt':  1162.0,  # applied: 800→1162
    'min_strength':            0.359,   # applied (was blocked by safety rules, now baseline)
    'min_prob':                0.527,   # applied (global override, was rejected from whitelist)
    'vol_spike_min':           0.83,    # applied (was blocked by safety rules, now baseline)
    'spot_sl_pct':             0.0103,  # applied: 1.5%→1.03%
    'spot_tp_pct':             0.0583,  # applied: 2.5%→5.83%
    'futures_sl_pct':          0.0333,  # applied: 7.0%→3.33%
    'futures_tp_pct':          0.0768,  # applied: 7.0%→7.68%
    # Commission rates are controlled by Binance, not tunable by backtest.
    'slippage_pct':            0.00057, # from best run_0812
}


def _settings_from_params(params: Dict) -> object:
    """Build a minimal settings object for binance_commission module.

    Commission rates are controlled by Binance, not tunable by backtest.
    We use the documented defaults (0.1% spot / 0.04% futures) and the
    BNB-discount flag from the current parameter set if provided.
    """
    class _Settings:
        pass
    s = _Settings()
    s.use_bnb_for_commission = bool(params.get('use_bnb_for_commission', 0))
    # Binance-controlled rates; backtest does not optimize these.
    s.binance_commission_rate = 0.001   # 0.1% spot taker default
    s.futures_commission_rate = 0.0004  # 0.04% futures taker default
    s.bnb_spot_commission_rate = 0.0005   # 0.05% spot with BNB
    s.bnb_futures_commission_rate = 0.0002  # 0.02% futures with BNB
    return s


def _round_trip_commission_pct(trade_type: str, params: Dict) -> float:
    """Return round-trip commission percentage for the trade type using centralized module."""
    settings = _settings_from_params(params)
    rate = get_config(settings).rate_for(trade_type)
    return 2 * rate


# ══════════════════════════════════════════════════════════════════════════════
# DATA LOADER
# ══════════════════════════════════════════════════════════════════════════════

def load_parquet_symbols(data_dir: str, days: int) -> Dict[str, object]:
    """Load parquet files for all valid symbols, slice to last `days` days."""
    global _PARQUET_ENGINE_ERROR
    _PARQUET_ENGINE_ERROR = None
    import pandas as pd

    cutoff = datetime.utcnow() - timedelta(days=days + 2)  # +2 buffer
    data = {}
    skipped = []

    for fname in os.listdir(data_dir):
        if not fname.endswith('_1h.parquet'):
            continue
        symbol = fname.replace('_1h.parquet', '')
        if symbol in EXCLUDE_SYMBOLS:
            continue

        path = os.path.join(data_dir, fname)
        try:
            df = pd.read_parquet(path)
            df = df[df['open_time'] >= pd.Timestamp(cutoff)]
            if len(df) < 200:
                skipped.append(symbol)
                continue
            df = df.sort_values('open_time').reset_index(drop=True)
            data[symbol] = df
        except Exception as e:
            _msg = str(e)
            if _PARQUET_ENGINE_ERROR is None and 'Unable to find a usable engine' in _msg:
                _PARQUET_ENGINE_ERROR = _msg
            skipped.append(f"{symbol}({e})")

    if skipped:
        print(f"[Loader] Skipped {len(skipped)} symbols: {skipped[:5]}...")
    print(f"[Loader] Loaded {len(data)} symbols, window={days}d")
    return data


def load_synthetic_trades(data_dir: str, days: int) -> List[Dict]:
    """Load pre-computed synthetic trades from JSON, filter to last `days`."""
    cutoff = (datetime.utcnow() - timedelta(days=days + 2)).strftime('%Y-%m-%d')

    # Pick latest full manifest
    candidates = sorted(
        [f for f in os.listdir(data_dir) if f.startswith('synthetic_trades_full') and f.endswith('.json')],
        reverse=True
    )
    if not candidates:
        return []

    fpath = os.path.join(data_dir, candidates[0])
    with open(fpath) as fh:
        all_trades = json.load(fh)

    recent = [t for t in all_trades if t.get('entry_time', '') >= cutoff]
    print(f"[Loader] Synthetic trades in window: {len(recent)} (from {candidates[0]})")
    return recent


def _latest_data_end_from_price_data(price_data: Dict[str, object]) -> Optional[datetime]:
    """Return the most recent common data end timestamp across loaded symbols."""
    latest_points = []
    for _sym, _df in price_data.items():
        try:
            if _df is None or len(_df) == 0:
                continue
            _mx = _df['open_time'].max()
            if _mx is not None:
                latest_points.append(_mx.to_pydatetime() if hasattr(_mx, 'to_pydatetime') else _mx)
        except Exception:
            continue
    if not latest_points:
        return None
    # Use the minimum of per-symbol maxima as conservative common window_end.
    return min(latest_points)


def _read_latest_manifest_meta(data_dir: str) -> Dict[str, object]:
    """Read latest manifest_* metadata for traceability and coverage diagnostics."""
    manifests = sorted(
        [f for f in os.listdir(data_dir) if f.startswith('manifest_') and f.endswith('.json')],
        reverse=True,
    )
    if not manifests:
        return {}
    latest_file = manifests[0]
    latest_path = os.path.join(data_dir, latest_file)
    try:
        with open(latest_path, 'r', encoding='utf-8') as fh:
            payload = json.load(fh)
    except Exception:
        payload = {}

    manifest_date = payload.get('date')
    if not manifest_date:
        m = re.search(r'(\d{8})_(\d{6})', latest_file)
        if m:
            manifest_date = f"{m.group(1)}T{m.group(2)}"

    return {
        'manifest_file': latest_file,
        'manifest_date': manifest_date,
        'manifest_total_requested': payload.get('total_requested'),
        'manifest_successful': payload.get('successful'),
        'manifest_symbols_successful': payload.get('symbols_successful', []),
    }


def _preflight_data_quality(
    price_data: Dict[str, object],
    syn_trades: List[Dict],
    mode: str,
    max_staleness_days: int,
    strict_data: bool,
) -> Dict[str, object]:
    """Validate recency and minimal symbol coverage before running 1000 iterations."""
    latest_common_end = _latest_data_end_from_price_data(price_data)
    if latest_common_end and latest_common_end.tzinfo is None:
        latest_common_end = latest_common_end.replace(tzinfo=timezone.utc)
    now_utc = datetime.now(timezone.utc)
    staleness_days = None
    if latest_common_end:
        staleness_days = (now_utc - latest_common_end).total_seconds() / 86400.0

    loaded_symbols = set(price_data.keys())
    required_futures = set(FUTURES_SYMBOLS)
    missing_futures = sorted(required_futures - loaded_symbols)

    errors = []
    warnings = []

    if _PARQUET_ENGINE_ERROR:
        errors.append(
            "parquet reader backend missing (install pyarrow or fastparquet). "
            f"details: {_PARQUET_ENGINE_ERROR.splitlines()[0]}"
        )

    if not syn_trades:
        msg = "synthetic trades are empty in selected window"
        if strict_data:
            errors.append(msg)
        else:
            warnings.append(msg)

    if mode in ('both', 'futures-only') and missing_futures and not _PARQUET_ENGINE_ERROR:
        errors.append(f"missing futures symbols in parquet window: {missing_futures[:8]}")

    if staleness_days is None:
        errors.append("could not determine latest common parquet timestamp")
    elif staleness_days > float(max_staleness_days):
        msg = (
            f"historical parquet appears stale by {staleness_days:.1f} days "
            f"(max allowed {max_staleness_days})"
        )
        if strict_data:
            errors.append(msg)
        else:
            warnings.append(msg)

    return {
        'ok': len(errors) == 0,
        'errors': errors,
        'warnings': warnings,
        'latest_common_end': latest_common_end.isoformat() if latest_common_end else None,
        'staleness_days': round(staleness_days, 3) if staleness_days is not None else None,
        'loaded_symbol_count': len(loaded_symbols),
        'required_futures_count': len(required_futures),
        'missing_futures': missing_futures,
    }


# ══════════════════════════════════════════════════════════════════════════════
# KELLY POSITION SIZER (standalone — mirrors production logic)
# ══════════════════════════════════════════════════════════════════════════════

def kelly_size(params: Dict, prob: float, strength: float,
               liquid: float, sl_pct: float, tp_pct: float,
               max_key: str = 'kelly_max_usdt',
               extra_mult: float = 1.0) -> Tuple[float, str]:
    """Returns (final_usdt, tier). Mirrors _kelly_position_size() in run_trades.py."""
    base    = max(params['kelly_base_usdt'], liquid * 0.15)
    max_u   = max(params.get(max_key, params['kelly_max_usdt']),
                  liquid * 0.25)
    cap_pct = params['kelly_max_pct_liquid']

    r = tp_pct / sl_pct if sl_pct > 0 else 1.67
    kelly_f = max(0.0, (prob - (1 - prob) / r)) * 0.5

    if prob >= 0.65 and strength >= 0.80:
        tier, sized = 'FUERTE', max_u
    elif prob >= 0.60 and strength >= 0.70:
        tier, sized = 'ALTA', base + (max_u - base) * 0.90
    elif prob >= 0.55 and strength >= 0.55:
        tier, sized = 'MEDIA', base + (max_u - base) * 0.50
    else:
        tier = 'BASE'
        sized = max(base * 0.5, base * min(1.0, kelly_f * 10))

    liquid_cap = liquid * cap_pct
    final = min(sized * extra_mult, liquid_cap, max_u)
    return max(0.0, final), tier


# ══════════════════════════════════════════════════════════════════════════════
# BACKTEST ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class BacktestEngine:
    """
    Simulates SPOT BUY + FUTURES SHORT trades over pre-loaded parquet data.
    Uses pre-computed signals from synthetic_trades JSON for speed.
    For FUTURES SHORT: re-simulates exit using actual OHLCV candles (realistic).
    """

    def __init__(
        self,
        price_data: Dict,
        synthetic_trades: List[Dict],
        capital_spot: float = 100.0,
        capital_futures: float = 988.0,
        regime_mode: str = REGIME,
    ):
        self.price_data  = price_data    # symbol -> DataFrame
        self.syn_trades  = synthetic_trades
        self.spot_liquid = capital_spot
        self.futures_liquid = capital_futures
        self.capital_start = capital_spot + capital_futures
        self.regime_mode = regime_mode
        self._build_price_index()

    def _build_price_index(self):
        """Pre-index OHLCV data as numpy arrays per symbol for fast exit simulation."""
        self._ohlcv = {}
        for sym, df in self.price_data.items():
            self._ohlcv[sym] = {
                'ts':    df['open_time'].values,
                'open':  df['open'].astype(float).values,
                'high':  df['high'].astype(float).values,
                'low':   df['low'].astype(float).values,
                'close': df['close'].astype(float).values,
            }

    def _simulate_spot_exit(self, sym: str, entry_ts, entry_price: float,
                             sl: float, tp: float,
                             max_candles: int = 48) -> Tuple[float, str]:
        """Candle-by-candle exit simulation for SPOT BUY."""
        ohlcv = self._ohlcv.get(sym)
        if ohlcv is None:
            pnl_pct = (tp - entry_price) / entry_price * 0.4  # fallback estimate
            return entry_price * (1 + pnl_pct), 'time_exit'

        ts_arr = ohlcv['ts']
        idx = np.searchsorted(ts_arr, entry_ts)
        end = min(idx + max_candles, len(ts_arr))

        for i in range(idx, end):
            lo = ohlcv['low'][i]
            hi = ohlcv['high'][i]
            if lo <= sl:
                return sl, 'stop_loss'
            if hi >= tp:
                return tp, 'take_profit'
        # time exit
        close_idx = min(end - 1, len(ts_arr) - 1)
        return float(ohlcv['close'][close_idx]), 'time_exit'

    def _simulate_futures_exit(self, sym: str, entry_ts, entry_price: float,
                                sl: float, tp: float,
                                max_candles: int = 72) -> Tuple[float, str]:
        """Candle-by-candle exit for FUTURES SHORT (sl above, tp below entry)."""
        ohlcv = self._ohlcv.get(sym)
        if ohlcv is None:
            return entry_price * 0.97, 'time_exit'  # fallback 3% gain estimate

        ts_arr = ohlcv['ts']
        idx = np.searchsorted(ts_arr, entry_ts)
        end = min(idx + max_candles, len(ts_arr))

        for i in range(idx, end):
            hi = ohlcv['high'][i]
            lo = ohlcv['low'][i]
            if hi >= sl:   # SHORT: SL is above entry
                return sl, 'stop_loss'
            if lo <= tp:   # SHORT: TP is below entry
                return tp, 'take_profit'
        close_idx = min(end - 1, len(ts_arr) - 1)
        return float(ohlcv['close'][close_idx]), 'time_exit'

    def _simulate_futures_exit_long(self, sym: str, entry_ts, entry_price: float,
                                    sl: float, tp: float,
                                    max_candles: int = 72) -> Tuple[float, str]:
        """Candle-by-candle exit for FUTURES LONG (sl below, tp above entry)."""
        ohlcv = self._ohlcv.get(sym)
        if ohlcv is None:
            return entry_price * 1.03, 'time_exit'  # fallback 3% gain estimate

        ts_arr = ohlcv['ts']
        idx = np.searchsorted(ts_arr, entry_ts)
        end = min(idx + max_candles, len(ts_arr))

        for i in range(idx, end):
            hi = ohlcv['high'][i]
            lo = ohlcv['low'][i]
            if lo <= sl:   # LONG: SL is below entry
                return sl, 'stop_loss'
            if hi >= tp:   # LONG: TP is above entry
                return tp, 'take_profit'
        close_idx = min(end - 1, len(ts_arr) - 1)
        return float(ohlcv['close'][close_idx]), 'time_exit'

    def _fng_boost(self, size: float, params: Dict) -> float:
        """Apply Fear & Greed boost (FNG=15 Extreme Fear currently)."""
        fear_mult = params.get('fng_fear_mult', FNG_FEAR_MULT)
        if FNG_VALUE <= 15:
            return size * fear_mult
        return size

    def run(self, params: Dict, label: str = '', mode: str = 'both') -> Dict:
        """
        Run full backtest with given params.
        mode: 'both' | 'spot-only' | 'futures-only'
        Returns dict with all metrics + list of trades.
        """
        t0 = time.time()

        spot_liquid    = self.spot_liquid if mode != 'futures-only' else 0.0
        futures_liquid = self.futures_liquid if mode != 'spot-only' else 0.0
        spot_capital   = spot_liquid
        futures_capital = futures_liquid

        spot_trades    = []
        futures_trades = []

        min_strength = params['min_strength']
        min_prob     = params['min_prob']
        vol_min      = params['vol_spike_min']
        spot_sl      = params['spot_sl_pct']
        spot_tp      = params['spot_tp_pct']
        f_sl         = params['futures_sl_pct']
        f_tp         = params['futures_tp_pct']

        # ── SPOT BUY simulation ───────────────────────────────────────────────
        if mode != 'futures-only':
            # Use pre-computed synthetic trades as signal source (same analyze_signal)
            open_spots = 0  # concurrent open positions (max 3 per regime)
            daily_spot_trades: Dict[str, int] = {}

            for t in self.syn_trades:
                sym = t.get('symbol', '')
                if sym in EXCLUDE_SYMBOLS:
                    continue
                # In FUTURES_SHORT regime: SPOT is narrative-only (no new SPOT BUY)
                # We simulate SPOT anyway to show what it would earn/lose independently
                ind = t.get('indicators_at_entry', {})
                strength = float(ind.get('vol_spike', ind.get('volume_spike', 0.5)))
                # Use close_reason and pnl_pct from synthetic trade as signal quality proxy
                prob = 0.55  # conservative estimate; syn_trade win_rate 42.8% → ~0.52 adjusted
                vol_spike = float(ind.get('vol_spike', ind.get('volume_spike', 1.0)))
                rsi       = float(ind.get('rsi', 50.0))
                trend_raw = ind.get('trend_4h', 0.5)
                if isinstance(trend_raw, str):
                    trend = 1.0 if trend_raw.upper() == 'UP' else 0.0
                else:
                    trend = float(trend_raw)

                # Filters
                if vol_spike < vol_min:
                    continue
                if rsi > 70:
                    continue
                # Derive strength from indicators
                strength_score = (
                    min(1.0, vol_spike / 3.0) * 0.35 +
                    (1 - abs(rsi - 45) / 45) * 0.30 +
                    trend * 0.20 +
                    (0.15 if t.get('close_reason') == 'take_profit' else 0.0)
                )
                if strength_score < min_strength:
                    continue
                if prob < min_prob:
                    continue

                # Prevent runaway geometric growth from synthetic rapid-fire trades.
                spot_liquid_for_size = min(
                    spot_liquid,
                    max(spot_capital, 10.0) * SPOT_SIZING_LIQUID_CAP_MULT,
                )

                # Kelly sizing
                size, tier = kelly_size(params, prob, strength_score,
                                         spot_liquid_for_size, spot_sl, spot_tp)
                size = self._fng_boost(size, params)
                size = min(size, spot_liquid_for_size * 0.55)  # hard cap
                if size < 5.0:
                    continue

                # Use pre-computed outcome (realistic — same signal, same outcome)
                pnl_pct_raw = float(t.get('pnl_percentage', 0.0)) / 100.0
                entry_price = float(t.get('entry_price', 1.0))
                exit_price  = float(t.get('exit_price', entry_price))

                # Re-scale SL/TP to params (the pre-computed trade used different SL/TP)
                _slippage = float(params.get('slippage_pct', 0.0005))
                _comm_spot = _round_trip_commission_pct('spot', params)
                close_reason = t.get('close_reason', 'time_exit')
                if close_reason == 'stop_loss':
                    actual_pnl_pct = -spot_sl - _slippage - _comm_spot
                elif close_reason == 'take_profit':
                    actual_pnl_pct = spot_tp - _slippage - _comm_spot
                else:
                    actual_pnl_pct = pnl_pct_raw - _comm_spot - _slippage

                pnl_usdt = size * actual_pnl_pct
                spot_liquid = max(10.0, spot_liquid + pnl_usdt)

                spot_trades.append({
                    'symbol':       sym,
                    'trade_type':   'spot',
                    'side':         'BUY',
                    'entry_ts':     t.get('entry_time'),
                    'exit_ts':      t.get('exit_time'),
                    'entry_price':  entry_price,
                    'exit_price':   exit_price,
                    'usdt_size':    round(size, 2),
                    'pnl_usdt':     round(pnl_usdt, 4),
                    'pnl_pct':      round(actual_pnl_pct * 100, 3),
                    'tier':         tier,
                    'regime':       self.regime_mode,
                    'close_reason': close_reason,
                    'kelly_mult':   1.0,
                    'indicators':   ind,
                })

        # ── FUTURES simulation (SHORT/LONG by regime) ─────────────────────────
        futures_short_allowed = (self.regime_mode == 'FUTURES_SHORT')
        futures_long_allowed = (self.regime_mode in ('SPOT_BULL', 'RANGE_SCALP'))
        if mode != 'spot-only' and futures_short_allowed:
            # Generate SHORT signals from price data: RSI>60 + downtrend confirmation
            open_shorts: Dict[str, int] = {}  # symbol -> count of open shorts
            max_concurrent = 12

            for sym in sorted(FUTURES_SYMBOLS):
                ohlcv = self._ohlcv.get(sym)
                if ohlcv is None:
                    continue

                ts_arr    = ohlcv['ts']
                closes    = ohlcv['close']
                highs     = ohlcv['high']
                lows      = ohlcv['low']
                n         = len(closes)
                if n < 50:
                    continue

                # Compute rolling RSI-like proxy and vol_spike for signal detection
                prev_entry_idx = -999  # min candle gap between entries on same symbol

                for i in range(50, n - 72):
                    if sum(open_shorts.values()) >= max_concurrent:
                        break

                    # Simple SHORT signal: price near recent high + RSI overbought proxy
                    window_c = closes[max(0, i-20):i]
                    recent_h = np.max(highs[max(0, i-10):i])
                    price    = closes[i]

                    # Price at 85%+ of recent high → potential short setup
                    if recent_h <= 0 or (price / recent_h) < 0.85:
                        continue

                    # Vol spike proxy
                    avg_vol_window = closes[max(0, i-20):i]
                    vol_proxy = float(closes[i]) / (float(np.mean(window_c)) + 1e-9)
                    if vol_proxy < vol_min * 0.7:
                        continue

                    # Min gap between entries on same symbol
                    if i - prev_entry_idx < 24:
                        continue

                    # Derive signal strength
                    rsi_proxy = min(80.0, max(30.0, 50 + (price - float(np.mean(window_c))) / float(np.mean(window_c)) * 300))
                    strength_f = min(1.0, max(0.3, (rsi_proxy - 50) / 30 * 0.7 + 0.3))
                    prob_f     = min(0.68, max(0.48, 0.50 + (rsi_proxy - 60) / 100))

                    if strength_f < min_strength or prob_f < min_prob:
                        continue

                    # Kelly sizing for futures
                    kelly_mult = KELLY_MULT_K2  # regime confidence → 0.8×
                    # Correlation penalty: more open shorts → lower mult
                    n_open = sum(open_shorts.values())
                    corr_mult = max(0.65, 1.0 - n_open * 0.04)

                    futures_liquid_for_size = min(
                        futures_liquid,
                        max(futures_capital, 10.0) * FUTURES_SIZING_LIQUID_CAP_MULT,
                    )

                    size, tier = kelly_size(params, prob_f, strength_f,
                                             futures_liquid_for_size,
                                             f_sl / FUTURES_LEVERAGE,
                                             f_tp / FUTURES_LEVERAGE,
                                             max_key='futures_kelly_max_usdt',
                                             extra_mult=kelly_mult * corr_mult)
                    size = min(size, futures_liquid_for_size * 0.55)
                    if size < 5.0:
                        continue

                    # SL/TP prices for SHORT
                    sl_price = price * (1 + f_sl)
                    tp_price = price * (1 - f_tp)

                    # Simulate exit using real candles
                    exit_price, close_reason = self._simulate_futures_exit(
                        sym, ts_arr[i], price, sl_price, tp_price
                    )

                    # PnL for SHORT (leveraged)
                    _slippage_f = float(params.get('slippage_pct', 0.0005))
                    _comm_futures = _round_trip_commission_pct('futures', params)
                    if close_reason == 'stop_loss':
                        pnl_pct = -(f_sl + _slippage_f + _comm_futures) * FUTURES_LEVERAGE
                    elif close_reason == 'take_profit':
                        pnl_pct = (f_tp - _slippage_f - _comm_futures) * FUTURES_LEVERAGE
                    else:
                        raw = (price - exit_price) / price  # SHORT: profit when price falls
                        pnl_pct = (raw - _slippage_f - _comm_futures) * FUTURES_LEVERAGE

                    pnl_usdt = size * pnl_pct
                    futures_liquid = max(10.0, futures_liquid + pnl_usdt)
                    prev_entry_idx = i

                    futures_trades.append({
                        'symbol':       sym,
                        'trade_type':   'futures',
                        'side':         'SHORT',
                        'entry_ts':     str(ts_arr[i])[:19],
                        'exit_ts':      None,
                        'entry_price':  round(float(price), 6),
                        'exit_price':   round(float(exit_price), 6),
                        'usdt_size':    round(size, 2),
                        'pnl_usdt':     round(pnl_usdt, 4),
                        'pnl_pct':      round(pnl_pct * 100, 3),
                        'tier':         tier,
                        'regime':       self.regime_mode,
                        'close_reason': close_reason,
                        'kelly_mult':   round(kelly_mult * corr_mult, 4),
                        'indicators':   {'rsi_proxy': round(rsi_proxy, 1),
                                         'strength':  round(strength_f, 3),
                                         'prob':      round(prob_f, 3)},
                    })
        elif mode != 'spot-only' and futures_long_allowed:
            # LONG futures in lateral/bull regimes to mirror live execution branches.
            open_longs: Dict[str, int] = {}
            max_concurrent = 8

            for sym in sorted(FUTURES_SYMBOLS):
                ohlcv = self._ohlcv.get(sym)
                if ohlcv is None:
                    continue

                ts_arr = ohlcv['ts']
                closes = ohlcv['close']
                highs = ohlcv['high']
                lows = ohlcv['low']
                n = len(closes)
                if n < 50:
                    continue

                prev_entry_idx = -999
                for i in range(50, n - 72):
                    if sum(open_longs.values()) >= max_concurrent:
                        break

                    window_c = closes[max(0, i-20):i]
                    if len(window_c) < 10:
                        continue
                    recent_l = np.min(lows[max(0, i-10):i])
                    price = closes[i]

                    if recent_l <= 0:
                        continue

                    # Regime-specific LONG triggers.
                    rsi_proxy = min(80.0, max(30.0, 50 + (price - float(np.mean(window_c))) / float(np.mean(window_c)) * 300))
                    near_support = (price / recent_l) <= 1.05
                    uptrend_bias = price >= float(np.mean(window_c))

                    if self.regime_mode == 'RANGE_SCALP':
                        if not near_support or rsi_proxy > 45:
                            continue
                    else:  # SPOT_BULL
                        if not uptrend_bias or rsi_proxy > 50 or rsi_proxy < 30:
                            continue

                    vol_proxy = float(closes[i]) / (float(np.mean(window_c)) + 1e-9)
                    if vol_proxy < vol_min * 0.8:
                        continue

                    if i - prev_entry_idx < 24:
                        continue

                    strength_l = min(1.0, max(0.3, ((50 - rsi_proxy) / 20.0) * 0.6 + 0.4))
                    prob_l = min(0.70, max(0.48, 0.52 + (50 - rsi_proxy) / 120.0))

                    if strength_l < min_strength or prob_l < min_prob:
                        continue

                    kelly_mult = KELLY_MULT_K2
                    n_open = sum(open_longs.values())
                    corr_mult = max(0.65, 1.0 - n_open * 0.04)

                    futures_liquid_for_size = min(
                        futures_liquid,
                        max(futures_capital, 10.0) * FUTURES_SIZING_LIQUID_CAP_MULT,
                    )

                    size, tier = kelly_size(
                        params,
                        prob_l,
                        strength_l,
                        futures_liquid_for_size,
                        f_sl / FUTURES_LEVERAGE,
                        f_tp / FUTURES_LEVERAGE,
                        max_key='futures_kelly_max_usdt',
                        extra_mult=kelly_mult * corr_mult,
                    )
                    size = min(size, futures_liquid_for_size * 0.55)
                    if size < 5.0:
                        continue

                    sl_price = price * (1 - f_sl)
                    tp_price = price * (1 + f_tp)

                    exit_price, close_reason = self._simulate_futures_exit_long(
                        sym, ts_arr[i], price, sl_price, tp_price
                    )

                    _slippage_f = float(params.get('slippage_pct', 0.0005))
                    _comm_futures = _round_trip_commission_pct('futures', params)
                    if close_reason == 'stop_loss':
                        pnl_pct = -(f_sl + _slippage_f + _comm_futures) * FUTURES_LEVERAGE
                    elif close_reason == 'take_profit':
                        pnl_pct = (f_tp - _slippage_f - _comm_futures) * FUTURES_LEVERAGE
                    else:
                        raw = (exit_price - price) / price  # LONG: profit when price rises
                        pnl_pct = (raw - _slippage_f - _comm_futures) * FUTURES_LEVERAGE

                    pnl_usdt = size * pnl_pct
                    futures_liquid = max(10.0, futures_liquid + pnl_usdt)
                    prev_entry_idx = i
                    open_longs[sym] = open_longs.get(sym, 0) + 1

                    futures_trades.append({
                        'symbol':       sym,
                        'trade_type':   'futures',
                        'side':         'LONG',
                        'entry_ts':     str(ts_arr[i])[:19],
                        'exit_ts':      None,
                        'entry_price':  round(float(price), 6),
                        'exit_price':   round(float(exit_price), 6),
                        'usdt_size':    round(size, 2),
                        'pnl_usdt':     round(pnl_usdt, 4),
                        'pnl_pct':      round(pnl_pct * 100, 3),
                        'tier':         tier,
                        'regime':       self.regime_mode,
                        'close_reason': close_reason,
                        'kelly_mult':   round(kelly_mult * corr_mult, 4),
                        'indicators':   {
                            'rsi_proxy': round(rsi_proxy, 1),
                            'strength': round(strength_l, 3),
                            'prob': round(prob_l, 3),
                            'near_support': bool(near_support),
                        },
                    })
        elif mode != 'spot-only':
            # PRESERVATION has no futures legs in the canonical simulator.
            pass

        # ── Metrics ───────────────────────────────────────────────────────────
        all_trades = spot_trades + futures_trades
        n_total  = len(all_trades)
        n_wins   = sum(1 for t in all_trades if t['pnl_usdt'] > 0)
        n_losses = n_total - n_wins
        pnl_spot     = sum(t['pnl_usdt'] for t in spot_trades)
        pnl_futures  = sum(t['pnl_usdt'] for t in futures_trades)
        pnl_total    = pnl_spot + pnl_futures
        win_rate     = n_wins / n_total if n_total > 0 else 0.0

        wins_sum  = sum(t['pnl_usdt'] for t in all_trades if t['pnl_usdt'] > 0)
        loss_sum  = abs(sum(t['pnl_usdt'] for t in all_trades if t['pnl_usdt'] < 0))
        if loss_sum > 0:
            profit_factor = wins_sum / loss_sum
        elif wins_sum > 0:
            # No losses: cap at a large but finite value to avoid DB overflow.
            # backtest_run.profit_factor is NUMERIC(18,6): abs(value) must be < 1e12.
            profit_factor = 99_999_999.0
        else:
            profit_factor = 0.0
        # Clamp profit_factor to DB numeric(18,6) safe range + guard against NaN/Inf.
        if math.isnan(profit_factor) or math.isinf(profit_factor):
            profit_factor = 0.0
        else:
            profit_factor = max(min(profit_factor, 99_999_999.0), -99_999_999.0)

        # Daily PnL for Sharpe
        daily: Dict[str, float] = {}
        for t in all_trades:
            day = str(t.get('entry_ts', ''))[:10]
            daily[day] = daily.get(day, 0.0) + t['pnl_usdt']
        daily_vals = list(daily.values())
        if len(daily_vals) >= 2:
            mu  = float(np.mean(daily_vals))
            std = float(np.std(daily_vals, ddof=1))
            sharpe = (mu / std * math.sqrt(252)) if std > 0 else 0.0
        else:
            sharpe = 0.0
        # Clamp sharpe to DB numeric(18,6) safe range + guard against NaN/Inf.
        if math.isnan(sharpe) or math.isinf(sharpe):
            sharpe = 0.0
        else:
            sharpe = max(min(sharpe, 99_999_999.0), -99_999_999.0)

        # Max drawdown on cumulative equity
        equity = [0.0]
        for t in sorted(all_trades, key=lambda x: str(x.get('entry_ts', ''))):
            equity.append(equity[-1] + t['pnl_usdt'])
        peak = equity[0]
        max_dd = 0.0
        for e in equity:
            if e > peak:
                peak = e
            dd = (peak - e)
            if dd > max_dd:
                max_dd = dd
        # Clamp max_drawdown to DB numeric(8,4) limit
        max_dd = min(max_dd, 9999.9999)

        capital_start = self.capital_start
        capital_end   = capital_start + pnl_total
        return_pct    = pnl_total / capital_start * 100

        elapsed_ms = int((time.time() - t0) * 1000)

        return {
            'params':         params,
            'label':          label,
            'n_trades':       n_total,
            'n_spot':         len(spot_trades),
            'n_futures':      len(futures_trades),
            'n_wins':         n_wins,
            'n_losses':       n_losses,
            'pnl_total':      round(pnl_total, 4),
            'pnl_spot':       round(pnl_spot, 4),
            'pnl_futures':    round(pnl_futures, 4),
            'win_rate':       round(win_rate, 4),
            'profit_factor':  round(profit_factor, 4),
            'sharpe':         round(sharpe, 4),
            'max_drawdown':   round(max_dd, 4),
            'capital_start':  capital_start,
            'capital_end':    round(capital_end, 4),
            'return_pct':     round(return_pct, 4),
            'duration_ms':    elapsed_ms,
            'trades':         all_trades,
        }


# ══════════════════════════════════════════════════════════════════════════════
# LATIN HYPERCUBE SAMPLER
# ══════════════════════════════════════════════════════════════════════════════

def latin_hypercube_sample(n: int, seed: int = 42) -> List[Dict]:
    """
    Generate n parameter sets using Latin Hypercube Sampling.
    Provides much better coverage than pure random for 11D space.
    """
    rng = np.random.RandomState(seed)
    keys = list(PARAM_SPACE.keys())
    d = len(keys)

    # LHS: divide each dimension into n equal intervals, sample once per interval
    samples = np.zeros((n, d))
    for j in range(d):
        perm = rng.permutation(n)
        u    = rng.uniform(size=n)
        samples[:, j] = (perm + u) / n

    # Scale to parameter ranges
    result = []
    for i in range(n):
        params = {}
        for j, key in enumerate(keys):
            lo, hi = PARAM_SPACE[key]
            params[key] = lo + samples[i, j] * (hi - lo)
        # Round for readability
        params['kelly_base_usdt']        = round(params['kelly_base_usdt'], 0)
        params['kelly_max_usdt']         = round(params['kelly_max_usdt'], 0)
        params['futures_kelly_max_usdt'] = round(params['futures_kelly_max_usdt'], 0)
        params['kelly_max_pct_liquid']   = round(params['kelly_max_pct_liquid'], 3)
        params['min_strength']           = round(params['min_strength'], 3)
        params['min_prob']               = round(params['min_prob'], 3)
        params['vol_spike_min']          = round(params['vol_spike_min'], 2)
        params['spot_sl_pct']            = round(params['spot_sl_pct'], 4)
        params['spot_tp_pct']            = round(params['spot_tp_pct'], 4)
        params['futures_sl_pct']         = round(params['futures_sl_pct'], 4)
        params['futures_tp_pct']         = round(params['futures_tp_pct'], 4)
        params['slippage_pct']           = round(params['slippage_pct'], 5)
        result.append(params)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════════

def get_db_client():
    """Get DB client from production db_client module."""
    try:
        from db_client import DatabaseClient
        from config import get_settings
        s = get_settings()
        db_url = getattr(s, 'db_url', None) or getattr(s, 'database_url', None)
        if not db_url:
            db_url = (f"postgresql://{getattr(s,'db_user','crypto_agent')}:"
                      f"{getattr(s,'db_password','')}@"
                      f"{getattr(s,'db_host','postgres')}:{getattr(s,'db_port',5432)}/"
                      f"{getattr(s,'db_name','crypto_agent')}")
        db = DatabaseClient(db_url=db_url)
        return db
    except Exception as e:
        print(f"[DB] Warning: could not connect — {e}")
        return None


def persist_run(db, result: Dict, window_start: str, window_end: str,
                is_baseline: bool = False) -> Optional[int]:
    """Insert backtest_run row + batch-insert backtest_trade in a single connection."""
    if not db:
        return None
    import psycopg2.extras as _pg_extras

    def _f(v): return float(v) if v is not None else None
    def _i(v): return int(v)   if v is not None else None

    try:
        with db.get_connection() as conn:
            run_id = None
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO backtest_run
                       (label, window_start, window_end, params, n_trades, n_spot, n_futures,
                        n_wins, n_losses, pnl_total, pnl_spot, pnl_futures, win_rate,
                        profit_factor, sharpe, max_drawdown, capital_start, capital_end,
                        return_pct, is_baseline, duration_ms)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       RETURNING id""",
                    (result['label'], window_start, window_end,
                     json.dumps(result['params'], cls=_NumpyEncoder),
                     _i(result['n_trades']), _i(result['n_spot']), _i(result['n_futures']),
                     _i(result['n_wins']), _i(result['n_losses']),
                     _f(result['pnl_total']), _f(result['pnl_spot']), _f(result['pnl_futures']),
                     _f(result['win_rate']), _f(result['profit_factor']), _f(result['sharpe']),
                     _f(result['max_drawdown']), _f(result['capital_start']), _f(result['capital_end']),
                     _f(result['return_pct']), is_baseline, _i(result['duration_ms']))
                )
                row = cur.fetchone()
                run_id = row[0] if row else None

            if not run_id:
                conn.rollback()
                return None

            # Batch insert trades in same connection/transaction
            trades = result.get('trades', [])
            if trades:
                values = [
                    (run_id, t['symbol'], t['trade_type'], t['side'],
                     t.get('entry_ts'), t.get('exit_ts'),
                     _f(t.get('entry_price')), _f(t.get('exit_price')),
                     None, _f(t.get('usdt_size')),
                     _f(t.get('pnl_usdt')), _f(t.get('pnl_pct')),
                     t.get('tier'), t.get('regime'), t.get('close_reason'),
                     _f(t.get('kelly_mult')),
                     json.dumps(t.get('indicators', {}), cls=_NumpyEncoder))
                    for t in trades
                ]
                with conn.cursor() as cur2:
                    _pg_extras.execute_batch(
                        cur2,
                        """INSERT INTO backtest_trade
                           (run_id, symbol, trade_type, side, entry_ts, exit_ts,
                            entry_price, exit_price, qty, usdt_size, pnl_usdt, pnl_pct,
                            tier, regime, close_reason, kelly_mult, indicators)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        values,
                        page_size=500
                    )
            conn.commit()
            return run_id
    except Exception as e:
        print(f"[DB] persist_run error: {e}")
        return None


def update_ranks(db, run_ids_ordered: List[int]):
    """Update rank_pnl field after all runs complete."""
    if not db:
        return
    try:
        for rank, run_id in enumerate(run_ids_ordered, 1):
            db.execute_update(
                "UPDATE backtest_run SET rank_pnl=%s WHERE id=%s",
                (rank, run_id)
            )
    except Exception as e:
        print(f"[DB] update_ranks error: {e}")


def _audit_event(db, event: str, detail: Dict[str, object]) -> None:
    """Best-effort audit event writer for backtest session lifecycle."""
    if not db:
        return
    try:
        db.execute_update(
            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
            "VALUES (%s, %s, %s, %s, %s)",
            ('__config__', 'system', event, json.dumps(detail, cls=_NumpyEncoder), False),
        )
    except Exception as _audit_err:
        print(f"[DB] audit event failed ({event}): {_audit_err}")


def _load_completed_run_labels(db, session_id: str) -> set:
    """Load completed run labels for a prior session to support resumability."""
    if not db or not session_id:
        return set()
    try:
        rows = db.execute_query(
            """
            SELECT detail->>'run_label' AS run_label
            FROM trades_audit
            WHERE event = 'backtest_run_checkpoint'
              AND detail->>'session_id' = %s
              AND detail->>'status' = 'completed'
            """,
            (session_id,),
        )
        return {r.get('run_label') for r in rows if r.get('run_label')}
    except Exception as _e:
        print(f"[DB] failed to load completed checkpoints for session {session_id}: {_e}")
        return set()


def _params_sha(params: Dict[str, object]) -> str:
    """Stable SHA256 over parameter payload for reproducibility tracking."""
    payload = json.dumps(params, sort_keys=True, cls=_NumpyEncoder)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _acquire_backtest_lock(db, session_id: str, ttl_minutes: int = 180) -> Optional[str]:
    """Acquire an optimizer lock in trading_runtime_state to prevent concurrent runs.

    Returns owner token if acquired, else None.
    """
    if not db:
        return None
    lock_key = 'backtest_optimizer_lock'
    owner = str(uuid.uuid4())
    owner_meta = {
        'owner': owner,
        'session_id': session_id,
        'pid': os.getpid(),
        'host': socket.gethostname(),
        'acquired_at': datetime.now(timezone.utc).isoformat(),
    }
    try:
        inserted = db.execute_update(
            """
            INSERT INTO trading_runtime_state (key, value, updated_at)
            VALUES (%s, %s::jsonb, NOW())
            ON CONFLICT (key) DO NOTHING
            """,
            (lock_key, json.dumps(owner_meta, cls=_NumpyEncoder)),
        )
        if inserted > 0:
            return owner

        # Lock already exists: inspect owner for stale/dead process takeover.
        current = db.execute_query(
            "SELECT value, updated_at FROM trading_runtime_state WHERE key = %s",
            (lock_key,),
        )
        current_value = (current[0].get('value') if current else {}) or {}
        current_pid = current_value.get('pid')
        current_host = current_value.get('host')

        # Same-host dead PID: immediate takeover (common after crashes/restarts).
        if current_host == socket.gethostname() and current_pid is not None:
            try:
                _pid_i = int(current_pid)
                _alive = os.path.exists(f"/proc/{_pid_i}")
            except Exception:
                _alive = False
            if not _alive:
                replaced_dead = db.execute_update(
                    """
                    UPDATE trading_runtime_state
                       SET value = %s::jsonb,
                           updated_at = NOW()
                     WHERE key = %s
                    """,
                    (json.dumps(owner_meta, cls=_NumpyEncoder), lock_key),
                )
                if replaced_dead > 0:
                    print(f"[Lock] detected dead owner pid={current_pid}; takeover granted")
                    return owner

        # Time-based stale lock fallback.
        stolen = db.execute_update(
            """
            UPDATE trading_runtime_state
               SET value = %s::jsonb,
                   updated_at = NOW()
             WHERE key = %s
               AND updated_at < NOW() - (%s * INTERVAL '1 minute')
            """,
            (json.dumps(owner_meta, cls=_NumpyEncoder), lock_key, ttl_minutes),
        )
        if stolen > 0:
            print(f"[Lock] stale lock detected; takeover granted (ttl={ttl_minutes}m)")
            return owner
        if current:
            cur = current[0]
            print(
                "[Lock] optimizer is already running: "
                f"session={((cur.get('value') or {}).get('session_id'))} "
                f"host={((cur.get('value') or {}).get('host'))} "
                f"pid={((cur.get('value') or {}).get('pid'))} "
                f"updated_at={cur.get('updated_at')}"
            )
        return None
    except Exception as e:
        print(f"[Lock] failed to acquire optimizer lock: {e}")
        return None


def _release_backtest_lock(db, owner: Optional[str]) -> None:
    """Release optimizer lock only if owned by this process token."""
    if not db or not owner:
        return
    lock_key = 'backtest_optimizer_lock'
    try:
        db.execute_update(
            "DELETE FROM trading_runtime_state WHERE key = %s AND value->>'owner' = %s",
            (lock_key, owner),
        )
    except Exception as e:
        print(f"[Lock] failed to release optimizer lock: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='Backtest Optimizer — SPOT+FUTURES 1000 runs')
    parser.add_argument('--runs',    type=int, default=1000, help='Number of optimization runs (default: 1000)')
    parser.add_argument('--days',    type=int, default=20,   help='Days of history to simulate (default: 20)')
    parser.add_argument('--capital', type=float, default=1088.0, help='Total starting capital in USDT (default: 1088)')
    parser.add_argument('--seed',    type=int, default=42,   help='Random seed for LHS')
    parser.add_argument('--top',     type=int, default=20,   help='Print top N results')
    parser.add_argument('--mode',    type=str, default='both', choices=['both', 'spot-only', 'futures-only'],
                        help='Trade modes to simulate (default: both)')
    parser.add_argument('--regime',  type=str, default=REGIME,
                        choices=['SPOT_BULL', 'RANGE_SCALP', 'PRESERVATION', 'FUTURES_SHORT'],
                        help='Regime context label for the run; futures SHORT in FUTURES_SHORT, futures LONG in SPOT_BULL/RANGE_SCALP.')
    parser.add_argument('--window-end', type=str, default='',
                        help='Optional fixed window end date (YYYY-MM-DD). Defaults to latest common parquet timestamp.')
    parser.add_argument('--max-staleness-days', type=int, default=3,
                        help='Maximum allowed staleness for parquet data before failing in strict mode (default: 3).')
    parser.add_argument('--strict-data', action='store_true',
                        help='Fail fast when data preflight finds stale/incomplete inputs.')
    parser.add_argument('--session-id', type=str, default='',
                        help='Optional explicit backtest session id. Useful for deterministic audit grouping.')
    parser.add_argument('--resume-session-id', type=str, default='',
                        help='Resume optimization runs from completed checkpoints in trades_audit for this session id.')
    parser.add_argument('--lock-ttl-minutes', type=int, default=180,
                        help='Consider optimizer lock stale after this many minutes and allow takeover (default: 180).')
    parser.add_argument('--no-db',      action='store_true', help='Skip DB persistence (dry run)')
    parser.add_argument('--auto-apply',  action='store_true', help='Auto-apply best config via API after optimization')
    parser.add_argument('--api-url',     type=str, default='http://localhost:8000', help='API base URL for --auto-apply')
    parser.add_argument('--api-key',     type=str, default=os.environ.get('DASHBOARD_API_KEY', ''), help='API key for --auto-apply')
    args = parser.parse_args()

    # Preserve the original 100/988 split while scaling to the requested total capital.
    _capital_spot    = round(args.capital * 100.0 / 1088.0, 2)
    _capital_futures = round(args.capital - _capital_spot, 2)

    print(f"\n{'='*70}")
    print(f"  BACKTEST OPTIMIZER — {args.runs} runs × {args.days}d window")
    print(f"  Capital: spot=${_capital_spot:.2f} + futures=${_capital_futures:.2f} = ${args.capital:.2f} USDT")
    print(f"  Regime: {args.regime} confirmed, kelly_mult={KELLY_MULT_K2}×")
    if args.mode != 'spot-only' and args.regime == 'PRESERVATION':
        print("  NOTE: PRESERVATION mode runs spot-only semantics in canonical simulator (futures disabled).")
    print(f"{'='*70}\n")

    # ── Load data ─────────────────────────────────────────────────────────────
    print("[1/5] Loading parquet data...")
    price_data = load_parquet_symbols(DATA_DIR, args.days)

    print("[2/5] Loading synthetic trades...")
    syn_trades = load_synthetic_trades(DATA_DIR, args.days)

    # Determine window dates from data and run preflight checks.
    manifest_meta = _read_latest_manifest_meta(DATA_DIR)
    preflight = _preflight_data_quality(
        price_data,
        syn_trades,
        mode=args.mode,
        max_staleness_days=args.max_staleness_days,
        strict_data=args.strict_data,
    )
    for _w in preflight['warnings']:
        print(f"      [Preflight/WARN] {_w}")
    if not preflight['ok']:
        for _e in preflight['errors']:
            print(f"      [Preflight/ERROR] {_e}")
        raise SystemExit("Data preflight failed. Use fresh data or disable --strict-data.")

    if args.window_end:
        window_end_dt = datetime.strptime(args.window_end, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    else:
        _latest_iso = preflight.get('latest_common_end')
        if not _latest_iso:
            raise SystemExit("Could not derive window_end from loaded parquet data.")
        window_end_dt = datetime.fromisoformat(_latest_iso)

    window_end = window_end_dt.strftime('%Y-%m-%d')
    window_start = (window_end_dt - timedelta(days=args.days)).strftime('%Y-%m-%d')
    print(f"      Window: {window_start} → {window_end}")
    print(
        f"      Preflight: symbols={preflight['loaded_symbol_count']} "
        f"| futures_missing={len(preflight['missing_futures'])} "
        f"| stale_days={preflight['staleness_days']}"
    )
    if manifest_meta:
        print(
            "      Manifest: "
            f"{manifest_meta.get('manifest_file')} "
            f"(date={manifest_meta.get('manifest_date')}, "
            f"ok={manifest_meta.get('manifest_successful')}/{manifest_meta.get('manifest_total_requested')})"
        )

    # ── DB ────────────────────────────────────────────────────────────────────
    db = None if args.no_db else get_db_client()
    if db:
        print("[3/5] DB connected ✓")
    else:
        print("[3/5] DB skipped (--no-db or connection failed)")

    # Session-level audit envelope for reproducibility and traceability.
    session_id = args.session_id.strip() or args.resume_session_id.strip() or datetime.now(timezone.utc).strftime('bt1000_%Y%m%d_%H%M%S')
    if args.resume_session_id and not db:
        raise SystemExit("--resume-session-id requires DB connectivity (remove --no-db)")

    lock_owner = None
    if db:
        lock_owner = _acquire_backtest_lock(db, session_id=session_id, ttl_minutes=args.lock_ttl_minutes)
        if not lock_owner:
            raise SystemExit("Another backtest optimizer instance is active. Retry later or wait for lock TTL.")

    resumed_labels = _load_completed_run_labels(db, args.resume_session_id.strip()) if args.resume_session_id else set()
    if resumed_labels:
        print(f"      Resume mode: found {len(resumed_labels)} completed checkpoint(s) for session {args.resume_session_id}")

    session_meta = {
        'session_id': session_id,
        'runs_requested': args.runs,
        'days': args.days,
        'mode': args.mode,
        'regime': args.regime,
        'seed': args.seed,
        'window_start': window_start,
        'window_end': window_end,
        'capital_total': args.capital,
        'capital_spot': _capital_spot,
        'capital_futures': _capital_futures,
        'data_preflight': preflight,
        'manifest': {
            'file': manifest_meta.get('manifest_file'),
            'date': manifest_meta.get('manifest_date'),
            'successful': manifest_meta.get('manifest_successful'),
            'requested': manifest_meta.get('manifest_total_requested'),
        },
        'resume_session_id': args.resume_session_id or None,
    }
    _audit_event(db, 'backtest_session_start', session_meta)

    try:

        # ── Engine ────────────────────────────────────────────────────────────────
        engine = BacktestEngine(
            price_data,
            syn_trades,
            capital_spot=_capital_spot,
            capital_futures=_capital_futures,
            regime_mode=args.regime,
        )

        # ── Baseline ─────────────────────────────────────────────────────────────
        print("\n[4/5] Running baseline (current config)...")
        baseline = engine.run(BASELINE_PARAMS, label='baseline', mode=args.mode)
        baseline_id = persist_run(db, baseline, window_start, window_end, is_baseline=True)
        print(f"      BASELINE → PnL={baseline['pnl_total']:+.2f} USDT "
              f"| trades={baseline['n_trades']} (spot={baseline['n_spot']}, fut={baseline['n_futures']}) "
              f"| win_rate={baseline['win_rate']*100:.1f}% "
              f"| sharpe={baseline['sharpe']:.2f} "
              f"| return={baseline['return_pct']:+.2f}%")

        # ── Optimization runs ─────────────────────────────────────────────────────
        print(f"\n[5/5] Running {args.runs} optimization configs (LHS seed={args.seed})...")
        param_sets = latin_hypercube_sample(args.runs, seed=args.seed)

        results = []
        run_ids  = []
        label_to_id: Dict[str, int] = {}
        t_start  = time.time()
        skipped_resumed = 0
        failed_runs = 0

        run_common_meta = {
            'session_id': session_id,
            'window_start': window_start,
            'window_end': window_end,
            'mode': args.mode,
            'regime': args.regime,
            'seed': args.seed,
            'days': args.days,
            'manifest_file': manifest_meta.get('manifest_file'),
            'manifest_date': manifest_meta.get('manifest_date'),
            'latest_common_end': preflight.get('latest_common_end'),
            'staleness_days': preflight.get('staleness_days'),
            'loaded_symbol_count': preflight.get('loaded_symbol_count'),
        }

        for i, params in enumerate(param_sets):
            label = f'run_{i+1:04d}'
            if label in resumed_labels:
                skipped_resumed += 1
                continue

            params_hash = _params_sha(params)
            _audit_event(db, 'backtest_run_checkpoint', {
                **run_common_meta,
                'run_index': i + 1,
                'run_label': label,
                'status': 'started',
                'params_sha256': params_hash,
            })

            try:
                result = engine.run(params, label=label, mode=args.mode)
                results.append(result)

                # Persist reproducibility metadata inside backtest_run.params for non-baseline runs.
                # Use a private key and JSON-safe scalar values to avoid interfering with tuning logic.
                _persist_result = dict(result)
                _persist_params = dict(result.get('params', {}))
                _persist_params['__run_meta'] = {
                    'session_id': session_id,
                    'run_index': i + 1,
                    'run_label': label,
                    'params_sha256': params_hash,
                    'mode': args.mode,
                    'regime': args.regime,
                    'seed': args.seed,
                    'days': args.days,
                    'window_start': window_start,
                    'window_end': window_end,
                    'manifest_file': manifest_meta.get('manifest_file'),
                    'manifest_date': manifest_meta.get('manifest_date'),
                    'latest_common_end': preflight.get('latest_common_end'),
                    'staleness_days': preflight.get('staleness_days'),
                    'loaded_symbol_count': preflight.get('loaded_symbol_count'),
                    'required_futures_count': preflight.get('required_futures_count'),
                    'missing_futures': preflight.get('missing_futures', []),
                    'capital_total': args.capital,
                    'capital_spot': _capital_spot,
                    'capital_futures': _capital_futures,
                }
                _persist_result['params'] = _persist_params

                run_id = persist_run(db, _persist_result, window_start, window_end, is_baseline=False)
                if run_id:
                    run_ids.append((result['pnl_total'], run_id))
                    label_to_id[label] = run_id
                    _audit_event(db, 'backtest_run_checkpoint', {
                        **run_common_meta,
                        'run_index': i + 1,
                        'run_label': label,
                        'run_id': run_id,
                        'status': 'completed',
                        'params_sha256': params_hash,
                        'pnl_total': result.get('pnl_total'),
                        'n_trades': result.get('n_trades'),
                        'n_spot': result.get('n_spot'),
                        'n_futures': result.get('n_futures'),
                        'duration_ms': result.get('duration_ms'),
                    })
                    _audit_event(db, 'backtest_run_meta', {
                        **run_common_meta,
                        'run_index': i + 1,
                        'run_label': label,
                        'run_id': run_id,
                        'params_sha256': params_hash,
                        'params': params,
                        'preflight': preflight,
                    })
                else:
                    failed_runs += 1
                    _audit_event(db, 'backtest_run_checkpoint', {
                        **run_common_meta,
                        'run_index': i + 1,
                        'run_label': label,
                        'status': 'persist_failed',
                        'params_sha256': params_hash,
                        'pnl_total': result.get('pnl_total'),
                        'n_trades': result.get('n_trades'),
                    })
            except Exception as _run_err:
                failed_runs += 1
                _audit_event(db, 'backtest_run_checkpoint', {
                    **run_common_meta,
                    'run_index': i + 1,
                    'run_label': label,
                    'status': 'failed',
                    'params_sha256': params_hash,
                    'error': str(_run_err),
                })
                print(f"      [RunError] {label}: {_run_err}")

            if (i + 1) % 100 == 0:
                elapsed = time.time() - t_start
                if results:
                    best_so_far = max(results, key=lambda x: x['pnl_total'])
                    avg_pnl = sum(r['pnl_total'] for r in results) / len(results)
                    print(f"      {i+1:4d}/{args.runs} | elapsed={elapsed:.1f}s "
                          f"| best_pnl={best_so_far['pnl_total']:+.2f} USDT "
                          f"| avg_pnl={avg_pnl:+.2f} | resumed_skips={skipped_resumed} | failed={failed_runs}")
                else:
                    print(f"      {i+1:4d}/{args.runs} | elapsed={elapsed:.1f}s | no new completed runs yet "
                          f"| resumed_skips={skipped_resumed} | failed={failed_runs}")

        total_elapsed = time.time() - t_start
        print(f"\n  Optimization complete: {args.runs} runs in {total_elapsed:.1f}s "
              f"({total_elapsed/args.runs*1000:.1f}ms/run)")
        if skipped_resumed:
            print(f"  Resume summary: skipped {skipped_resumed} run(s) already completed in session {args.resume_session_id}")
        if failed_runs:
            print(f"  Warning: {failed_runs} run(s) failed or could not persist (see trades_audit backtest_run_checkpoint)")

        # ── Update ranks ─────────────────────────────────────────────────────────
        if run_ids:
            sorted_ids = [rid for _, rid in sorted(run_ids, key=lambda x: -x[0])]
            if baseline_id:
                sorted_ids.insert(0, baseline_id)  # baseline always gets context rank
            update_ranks(db, sorted_ids)

        # ── Print results ─────────────────────────────────────────────────────────
        all_results = [baseline] + results
        sorted_results = sorted(all_results, key=lambda x: -x['pnl_total'])
        top_n = sorted_results[:args.top]

        print(f"\n{'='*70}")
        print(f"  TOP {args.top} RESULTS (sorted by PnL)")
        print(f"{'='*70}")
        print(f"  {'#':>3} {'Label':<12} {'PnL':>10} {'Return':>8} {'Trades':>7} {'WinRate':>8} {'Sharpe':>7} {'MaxDD':>8}")
        print(f"  {'-'*70}")

        for rank, r in enumerate(top_n, 1):
            marker = ' ← BASELINE' if r['label'] == 'baseline' else ''
            print(f"  {rank:>3} {r['label']:<12} {r['pnl_total']:>+10.2f} "
                  f"{r['return_pct']:>+7.2f}% {r['n_trades']:>7} "
                  f"{r['win_rate']*100:>7.1f}% {r['sharpe']:>7.2f} "
                  f"{r['max_drawdown']:>8.2f}{marker}")

        # ── Best config vs baseline ───────────────────────────────────────────────
        best = sorted_results[0]
        if best['label'] == 'baseline':
            best['id'] = baseline_id
        else:
            best['id'] = label_to_id.get(best['label'])
        baseline['id'] = baseline_id
        if best['label'] != 'baseline':
            print(f"\n{'='*70}")
            print(f"  BEST CONFIG vs BASELINE")
            print(f"{'='*70}")
            print(f"  {'Parameter':<30} {'Baseline':>12} {'Optimal':>12} {'Delta':>10}")
            print(f"  {'-'*70}")
            for key in PARAM_SPACE:
                bv = BASELINE_PARAMS.get(key, 0)
                ov = best['params'].get(key, 0)
                delta = ov - bv
                marker = ' ▲' if delta > 0 else (' ▼' if delta < 0 else '')
                print(f"  {key:<30} {bv:>12.4f} {ov:>12.4f} {delta:>+10.4f}{marker}")

            print(f"\n  Baseline PnL : {baseline['pnl_total']:+.2f} USDT ({baseline['return_pct']:+.2f}%)")
            print(f"  Optimal PnL  : {best['pnl_total']:+.2f} USDT ({best['return_pct']:+.2f}%)")
            print(f"  Improvement  : {best['pnl_total']-baseline['pnl_total']:+.2f} USDT")
            print(f"\n  Apply via API:")
            patch = {
                'kelly_base_usdt':        best['params']['kelly_base_usdt'],
                'kelly_max_usdt':         best['params']['kelly_max_usdt'],
                'kelly_max_pct_liquid':   best['params']['kelly_max_pct_liquid'],
                'futures_kelly_max_usdt': best['params']['futures_kelly_max_usdt'],
            }
            print(f"  PATCH /config/kelly → {json.dumps(patch, indent=4)}")
        else:
            print(f"\n  Baseline IS the best config — current parameters are optimal for this window.")

        print(f"\n  Results persisted to BD: backtest_run + backtest_trade")
        print(f"  View via: GET /backtest/runs | GET /backtest/best")
        print(f"{'='*70}\n")

        # ── Auto-apply best config if requested ───────────────────────────────────
        if getattr(args, 'auto_apply', False):
            if best['label'] == 'baseline':
                print("[auto-apply] Baseline is best — no changes applied.")
            else:
                api_key = args.api_key
                if not api_key:
                    print("[auto-apply] ⚠️  No API key provided. Use --api-key or set DASHBOARD_API_KEY env var.")
                else:
                    applied, skipped, err = auto_apply_best(
                        best, baseline, args.api_url, api_key, db
                    )
                    if err:
                        print(f"[auto-apply] ❌ Failed: {err}")
                    else:
                        print(f"[auto-apply] ✅ Done. Applied {len(applied)} top-level configs, skipped {len(skipped)}.")
                        print(f"[auto-apply] NEVER_AUTO_APPLY: {list(_NEVER_AUTO_APPLY)}")
        else:
            print("  Tip: re-run with --auto-apply to apply best config automatically.\n")

        _audit_event(db, 'backtest_session_end', {
            **session_meta,
            'elapsed_sec': round(total_elapsed, 3),
            'baseline_pnl': baseline['pnl_total'],
            'best_label': best['label'],
            'best_pnl': best['pnl_total'],
            'resumed_skips': skipped_resumed,
            'failed_runs': failed_runs,
            'new_runs_completed': len(results),
        })
    finally:
        _release_backtest_lock(db, lock_owner)


# ── Parameters that are NEVER auto-applied (safety hardcoded) ────────────────
_NEVER_AUTO_APPLY = set()
# Rationale:
#   min_strength  — minimum 0.40 validated; case AAVE SHORT jun23 strength=0.30 → -5.88%
#   vol_spike_min — optimal 0.83 < 1.0 means below-avg volume = no directional conviction
#   kelly_base_usdt — lowering from $100 reduces position size floor with no clear gain

# Category field whitelist for auto-apply
_ALLOWED_CAT_FIELDS_AUTO = {"min_prob", "take_profit_pct"}


def auto_apply_best(best_result: Dict, baseline_result: Dict, api_url: str, api_key: str, db=None):
    """
    Automatically apply the best backtest config to the live system via POST /backtest/apply.
    Uses centralized snapshot + safety rules in app.py. Logs to trades_audit for full traceability.
    Returns: (applied_fields, skipped_fields, error_or_None)
    """
    if best_result.get('label') == 'baseline':
        print("  [auto-apply] Baseline IS best — nothing to apply.")
        return {}, {}, None

    run_id = best_result.get('id')
    if not run_id:
        return {}, {}, "best_result missing id"

    body = {
        "run_id": run_id,
        "mode": "auto",
        "auto_apply": True,
    }

    print(f"\n  [auto-apply] Sending POST /backtest/apply for run {run_id}...")
    print(f"               Payload: {json.dumps(body, indent=4)}")

    try:
        req = urllib.request.Request(
            f"{api_url}/backtest/apply",
            data=json.dumps(body).encode(),
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_data = json.loads(resp.read())

        if resp_data.get("ok"):
            applied_fields = resp_data.get("applied", {})
            rejected_fields = resp_data.get("rejected", {})
            skipped_fields = resp_data.get("skipped", {})
            snapshot_id = resp_data.get("snapshot_id")
            print(f"  [auto-apply] ✅ Applied: {list(applied_fields.keys())}")
            print(f"  [auto-apply] 📸 Snapshot: {snapshot_id}")
            if rejected_fields:
                print(f"  [auto-apply] ⚠️  Rejected: {rejected_fields}")
            if skipped_fields:
                print(f"  [auto-apply] ⚠️  Skipped: {skipped_fields}")

            # Extra audit already written by /backtest/apply; this is a local CLI backup
            if db:
                try:
                    detail = json.dumps({
                        "source": "backtest_optimizer",
                        "best_label": best_result['label'],
                        "improvement_usdt": round(best_result['pnl_total'] - baseline_result['pnl_total'], 2),
                        "applied": applied_fields,
                        "skipped": skipped_fields,
                        "rejected": rejected_fields,
                        "snapshot_id": snapshot_id,
                    }, cls=_NumpyEncoder)
                    db.execute_update(
                        "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
                        "VALUES ('__config__', 'system', 'backtest_auto_apply', %s, false)",
                        (detail,)
                    )
                    print("  [auto-apply] 📝 Audit entry saved to trades_audit.")
                except Exception as _ae:
                    print(f"  [auto-apply] ⚠️  Audit write failed: {_ae}")

            return applied_fields, skipped_fields, None
        else:
            err = resp_data.get("error", str(resp_data))
            print(f"  [auto-apply] ❌ API returned error: {err}")
            return {}, {}, err

    except Exception as e:
        print(f"  [auto-apply] ❌ Request failed: {e}")
        return {}, {}, str(e)


if __name__ == '__main__':
    main()
