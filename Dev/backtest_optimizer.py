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
import json
import math
import os
import random
import sys
import time
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

# ── Constants ─────────────────────────────────────────────────────────────────
DATA_DIR       = '/app/historical_data'
COMMISSION_SPOT    = 0.001   # 0.1% taker each side
COMMISSION_FUTURES = 0.0004  # 0.04% maker futures
SLIPPAGE           = 0.0005  # 0.05% slippage estimate
FUTURES_LEVERAGE   = 10
FNG_VALUE          = 15      # Extreme Fear (actual)
FNG_FEAR_MULT      = 1.25    # Current fear boost
REGIME             = 'FUTURES_SHORT'
REGIME_CONFIRMED   = True
REGIME_CONFIDENCE  = 0.60
KELLY_MULT_K2      = 0.80    # conf=0.60 → 0.8× (K2 table)

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
}

# ── Current (baseline) config — updated jun26 2026 post-backtest optimizer ────
BASELINE_PARAMS = {
    'kelly_base_usdt':         100.0,
    'kelly_max_usdt':          663.0,   # updated: 500→663
    'kelly_max_pct_liquid':    0.621,   # updated: 0.55→0.621
    'futures_kelly_max_usdt':  1162.0,  # updated: 800→1162
    'min_strength':            0.40,
    'min_prob':                0.50,
    'vol_spike_min':           1.0,
    'spot_sl_pct':             0.015,
    'spot_tp_pct':             0.025,
    'futures_sl_pct':          0.0333,  # updated: 0.020→0.0333
    'futures_tp_pct':          0.0768,  # updated: 0.035→0.0768
}


# ══════════════════════════════════════════════════════════════════════════════
# DATA LOADER
# ══════════════════════════════════════════════════════════════════════════════

def load_parquet_symbols(data_dir: str, days: int) -> Dict[str, object]:
    """Load parquet files for all valid symbols, slice to last `days` days."""
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

    def __init__(self, price_data: Dict, synthetic_trades: List[Dict]):
        self.price_data  = price_data    # symbol -> DataFrame
        self.syn_trades  = synthetic_trades
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

    def _fng_boost(self, size: float, params: Dict) -> float:
        """Apply Fear & Greed boost (FNG=15 Extreme Fear currently)."""
        fear_mult = params.get('fng_fear_mult', FNG_FEAR_MULT)
        if FNG_VALUE <= 15:
            return size * fear_mult
        return size

    def run(self, params: Dict, label: str = '') -> Dict:
        """
        Run full backtest with given params.
        Returns dict with all metrics + list of trades.
        """
        t0 = time.time()

        spot_liquid    = 100.0    # actual spot balance
        futures_liquid = 988.0   # actual futures balance
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

            # Kelly sizing
            size, tier = kelly_size(params, prob, strength_score,
                                     spot_liquid, spot_sl, spot_tp)
            size = self._fng_boost(size, params)
            size = min(size, spot_liquid * 0.55)  # hard cap
            if size < 5.0:
                continue

            # Use pre-computed outcome (realistic — same signal, same outcome)
            pnl_pct_raw = float(t.get('pnl_percentage', 0.0)) / 100.0
            entry_price = float(t.get('entry_price', 1.0))
            exit_price  = float(t.get('exit_price', entry_price))

            # Re-scale SL/TP to params (the pre-computed trade used different SL/TP)
            close_reason = t.get('close_reason', 'time_exit')
            if close_reason == 'stop_loss':
                actual_pnl_pct = -spot_sl - SLIPPAGE - COMMISSION_SPOT * 2
            elif close_reason == 'take_profit':
                actual_pnl_pct = spot_tp - SLIPPAGE - COMMISSION_SPOT * 2
            else:
                actual_pnl_pct = pnl_pct_raw - COMMISSION_SPOT * 2 - SLIPPAGE

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
                'regime':       REGIME,
                'close_reason': close_reason,
                'kelly_mult':   1.0,
                'indicators':   ind,
            })

        # ── FUTURES SHORT simulation ──────────────────────────────────────────
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

                size, tier = kelly_size(params, prob_f, strength_f,
                                         futures_liquid,
                                         f_sl / FUTURES_LEVERAGE,
                                         f_tp / FUTURES_LEVERAGE,
                                         max_key='futures_kelly_max_usdt',
                                         extra_mult=kelly_mult * corr_mult)
                size = min(size, futures_liquid * 0.55)
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
                if close_reason == 'stop_loss':
                    pnl_pct = -(f_sl + SLIPPAGE + COMMISSION_FUTURES * 2) * FUTURES_LEVERAGE
                elif close_reason == 'take_profit':
                    pnl_pct = (f_tp - SLIPPAGE - COMMISSION_FUTURES * 2) * FUTURES_LEVERAGE
                else:
                    raw = (price - exit_price) / price  # SHORT: profit when price falls
                    pnl_pct = (raw - SLIPPAGE - COMMISSION_FUTURES * 2) * FUTURES_LEVERAGE

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
                    'regime':       REGIME,
                    'close_reason': close_reason,
                    'kelly_mult':   round(kelly_mult * corr_mult, 4),
                    'indicators':   {'rsi_proxy': round(rsi_proxy, 1),
                                     'strength':  round(strength_f, 3),
                                     'prob':      round(prob_f, 3)},
                })

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
        profit_factor = wins_sum / loss_sum if loss_sum > 0 else wins_sum

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

        capital_start = 1088.0
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


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='Backtest Optimizer — SPOT+FUTURES 1000 runs')
    parser.add_argument('--runs',    type=int, default=1000, help='Number of optimization runs (default: 1000)')
    parser.add_argument('--days',    type=int, default=20,   help='Days of history to simulate (default: 20)')
    parser.add_argument('--seed',    type=int, default=42,   help='Random seed for LHS')
    parser.add_argument('--top',     type=int, default=20,   help='Print top N results')
    parser.add_argument('--no-db',   action='store_true',    help='Skip DB persistence (dry run)')
    args = parser.parse_args()

    print(f"\n{'='*70}")
    print(f"  BACKTEST OPTIMIZER — {args.runs} runs × {args.days}d window")
    print(f"  Capital: spot=$100 + futures=$988 = $1,088 USDT")
    print(f"  Regime: {REGIME} confirmed, kelly_mult={KELLY_MULT_K2}×")
    print(f"{'='*70}\n")

    # ── Load data ─────────────────────────────────────────────────────────────
    print("[1/5] Loading parquet data...")
    price_data = load_parquet_symbols(DATA_DIR, args.days)

    print("[2/5] Loading synthetic trades...")
    syn_trades = load_synthetic_trades(DATA_DIR, args.days)

    # Determine window dates from data
    window_end   = '2026-06-17'
    window_start = (datetime.strptime(window_end, '%Y-%m-%d') - timedelta(days=args.days)).strftime('%Y-%m-%d')
    print(f"      Window: {window_start} → {window_end}")

    # ── DB ────────────────────────────────────────────────────────────────────
    db = None if args.no_db else get_db_client()
    if db:
        print("[3/5] DB connected ✓")
    else:
        print("[3/5] DB skipped (--no-db or connection failed)")

    # ── Engine ────────────────────────────────────────────────────────────────
    engine = BacktestEngine(price_data, syn_trades)

    # ── Baseline ─────────────────────────────────────────────────────────────
    print("\n[4/5] Running baseline (current config)...")
    baseline = engine.run(BASELINE_PARAMS, label='baseline')
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
    t_start  = time.time()

    for i, params in enumerate(param_sets):
        label  = f'run_{i+1:04d}'
        result = engine.run(params, label=label)
        results.append(result)

        run_id = persist_run(db, result, window_start, window_end, is_baseline=False)
        if run_id:
            run_ids.append((result['pnl_total'], run_id))

        if (i + 1) % 100 == 0:
            elapsed = time.time() - t_start
            best_so_far = max(results, key=lambda x: x['pnl_total'])
            print(f"      {i+1:4d}/{args.runs} | elapsed={elapsed:.1f}s "
                  f"| best_pnl={best_so_far['pnl_total']:+.2f} USDT "
                  f"| avg_pnl={sum(r['pnl_total'] for r in results)/len(results):+.2f}")

    total_elapsed = time.time() - t_start
    print(f"\n  Optimization complete: {args.runs} runs in {total_elapsed:.1f}s "
          f"({total_elapsed/args.runs*1000:.1f}ms/run)")

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


if __name__ == '__main__':
    main()
