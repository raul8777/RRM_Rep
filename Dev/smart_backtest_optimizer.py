#!/usr/bin/env python3
"""
Super Smart Backtest Optimizer
==============================
Single-entry runner that orchestrates multi-regime optimization sessions
for SPOT+FUTURES coverage in one execution.

Primary goals:
- Period-based automatic regime coverage plan (3m / 1y / 2y / 3y or days)
- One command to launch all scenario runs
- Reuse canonical Dev/backtest_optimizer.py for persistence, lock, checkpoints
- Produce weighted recommendation and parity summary per scenario

Usage examples:
  python Dev/smart_backtest_optimizer.py --period 1y --simulations 12000
  python Dev/smart_backtest_optimizer.py --period 1y --simulations 12000 --capital 5283.95
  python Dev/smart_backtest_optimizer.py --period 3m --auto-recommend-sims
  python Dev/smart_backtest_optimizer.py --period 365 --simulations 20000 --strict-data

Note: If --capital is omitted, fetches current account balance from live API.
"""

import argparse
import json
import math
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Keep imports local to repository runtime.
sys.path.insert(0, str(os.path.join(os.path.dirname(__file__), '..')))

from config import get_settings
from db_client import DatabaseClient

DATA_DIR = '/app/historical_data'
CANONICAL_OPTIMIZER = '/app/Dev/backtest_optimizer.py'

# Thresholds mirrored from market_regime.py
_BEAR_7D_THRESHOLD = -7.0
_BEAR_30D_THRESHOLD = -15.0
_SIDEWAYS_DAY_RANGE = 1.2
_SIDEWAYS_VOL_MAX = 0.7
_BB_SIDEWAYS_MAX = 0.06
_BULL_DAY_MIN = 0.5


@dataclass
class ScenarioJob:
    name: str
    regime: str
    mode: str
    runs: int
    session_id: str


def _db_client() -> Optional[DatabaseClient]:
    try:
        s = get_settings()
        db_url = getattr(s, 'db_url', None) or getattr(s, 'database_url', None)
        if not db_url:
            db_url = (
                f"postgresql://{getattr(s, 'db_user', 'crypto_agent')}:"
                f"{getattr(s, 'db_password', '')}@"
                f"{getattr(s, 'db_host', 'postgres')}:{getattr(s, 'db_port', 5432)}/"
                f"{getattr(s, 'db_name', 'crypto_agent')}"
            )
        return DatabaseClient(db_url=db_url)
    except Exception as e:
        print(f"[Smart] DB unavailable: {e}")
        return None


def _audit_event(db: Optional[DatabaseClient], event: str, detail: Dict) -> None:
    if not db:
        return
    try:
        db.execute_update(
            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s,%s,%s,%s,%s)",
            ('__config__', 'system', event, json.dumps(detail, default=str), False),
        )
    except Exception as e:
        print(f"[Smart] audit failed ({event}): {e}")


def _period_to_days(period: str) -> int:
    p = str(period).strip().lower()
    aliases = {
        '3m': 90,
        '90d': 90,
        '1y': 365,
        '365d': 365,
        '2y': 730,
        '730d': 730,
        '3y': 1095,
        '1095d': 1095,
    }
    if p in aliases:
        return aliases[p]
    try:
        d = int(p)
        if d <= 0:
            raise ValueError("period days must be > 0")
        return d
    except Exception:
        raise ValueError(f"Unsupported period '{period}'. Use 3m|1y|2y|3y or integer days.")


def _recommended_simulations(days: int) -> int:
    if days <= 120:
        return 4000
    if days <= 450:
        return 12000
    if days <= 900:
        return 20000
    return 36000


def _load_btc_window(days: int) -> pd.DataFrame:
    fpath = os.path.join(DATA_DIR, 'BTCUSDT_1h.parquet')
    if not os.path.exists(fpath):
        raise FileNotFoundError(f"BTC parquet not found: {fpath}")

    df = pd.read_parquet(fpath)
    if 'open_time' not in df.columns:
        raise ValueError("BTC parquet missing 'open_time'")

    df = df.sort_values('open_time').reset_index(drop=True)
    end_ts = df['open_time'].max()
    start_ts = end_ts - pd.Timedelta(days=days)
    df = df[df['open_time'] >= start_ts].copy()
    if len(df) < 24 * 35:
        raise ValueError(f"Insufficient BTC data in selected window: {len(df)} rows")
    return df


def _regime_distribution(days: int) -> Dict[str, float]:
    df = _load_btc_window(days)
    close = df['close'].astype(float)
    vol = df['volume'].astype(float)

    df['ret_7d'] = close.pct_change(24 * 7) * 100
    df['ret_30d'] = close.pct_change(24 * 30) * 100
    df['ret_1d'] = close.pct_change(24) * 100

    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    df['ema_sig'] = np.where(ema9 > ema21 * 1.001, 'UP', np.where(ema9 < ema21 * 0.999, 'DOWN', 'FLAT'))

    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    df['bb_bw'] = (4 * std) / mid
    df['vol_ratio'] = vol / vol.rolling(24).mean()

    def classify(row) -> str:
        if any(pd.isna(row[x]) for x in ('ret_7d', 'ret_30d', 'ret_1d', 'bb_bw', 'vol_ratio')):
            return 'UNKNOWN'

        bear_struct = row['ret_30d'] <= _BEAR_30D_THRESHOLD
        bear_7d = row['ret_7d'] <= _BEAR_7D_THRESHOLD
        sideways_vol = abs(row['ret_1d']) < _SIDEWAYS_DAY_RANGE and row['vol_ratio'] < _SIDEWAYS_VOL_MAX
        sideways_bb = row['bb_bw'] < _BB_SIDEWAYS_MAX

        if bear_struct or (bear_7d and row['ema_sig'] == 'DOWN'):
            return 'PRESERVATION'
        if sideways_vol and sideways_bb:
            return 'RANGE_SCALP'
        if row['ema_sig'] == 'UP' and (row['ret_1d'] > _BULL_DAY_MIN or row['bb_bw'] > _BB_SIDEWAYS_MAX):
            return 'SPOT_BULL'
        if row['ema_sig'] == 'UP':
            return 'SPOT_BULL'
        return 'RANGE_SCALP'

    df['regime'] = df.apply(classify, axis=1)
    valid = df[df['regime'] != 'UNKNOWN']
    if valid.empty:
        return {'SPOT_BULL': 0.34, 'RANGE_SCALP': 0.33, 'PRESERVATION': 0.33}

    c = valid['regime'].value_counts().to_dict()
    total = float(len(valid))
    return {
        'SPOT_BULL': c.get('SPOT_BULL', 0) / total,
        'RANGE_SCALP': c.get('RANGE_SCALP', 0) / total,
        'PRESERVATION': c.get('PRESERVATION', 0) / total,
    }


def _allocate_runs(total_runs: int, reg_dist: Dict[str, float]) -> Dict[str, int]:
    # Include futures_short as a first-class scenario with at least 15% floor.
    w_spot = max(0.10, reg_dist.get('SPOT_BULL', 0.0))
    w_range = max(0.10, reg_dist.get('RANGE_SCALP', 0.0))
    w_pres = max(0.10, reg_dist.get('PRESERVATION', 0.0))
    w_fshort = max(0.15, w_pres)

    wsum = w_spot + w_range + w_pres + w_fshort
    raw = {
        'SPOT_BULL': total_runs * w_spot / wsum,
        'RANGE_SCALP': total_runs * w_range / wsum,
        'PRESERVATION': total_runs * w_pres / wsum,
        'FUTURES_SHORT': total_runs * w_fshort / wsum,
    }

    alloc = {k: max(1, int(math.floor(v))) for k, v in raw.items()}
    diff = total_runs - sum(alloc.values())

    # Distribute remainder by descending fractional part.
    remainders = sorted(((k, raw[k] - alloc[k]) for k in raw), key=lambda x: x[1], reverse=True)
    i = 0
    while diff > 0:
        alloc[remainders[i % len(remainders)][0]] += 1
        diff -= 1
        i += 1
    while diff < 0:
        k = remainders[::-1][i % len(remainders)][0]
        if alloc[k] > 1:
            alloc[k] -= 1
            diff += 1
        i += 1

    return alloc


def _build_jobs(root_session_id: str, runs_by_regime: Dict[str, int]) -> List[ScenarioJob]:
    return [
        ScenarioJob('spot_bull', 'SPOT_BULL', 'both', runs_by_regime['SPOT_BULL'], f"{root_session_id}_spot_bull"),
        ScenarioJob('range_scalp', 'RANGE_SCALP', 'both', runs_by_regime['RANGE_SCALP'], f"{root_session_id}_range_scalp"),
        ScenarioJob('preservation', 'PRESERVATION', 'spot-only', runs_by_regime['PRESERVATION'], f"{root_session_id}_preservation"),
        ScenarioJob('futures_short', 'FUTURES_SHORT', 'both', runs_by_regime['FUTURES_SHORT'], f"{root_session_id}_futures_short"),
    ]


def _run_canonical_job(job: ScenarioJob, days: int, capital: float, strict_data: bool, lock_ttl: int) -> int:
    cmd = [
        sys.executable,
        CANONICAL_OPTIMIZER,
        '--runs', str(job.runs),
        '--days', str(days),
        '--capital', str(capital),
        '--mode', job.mode,
        '--regime', job.regime,
        '--session-id', job.session_id,
        '--lock-ttl-minutes', str(lock_ttl),
    ]
    if strict_data:
        cmd.append('--strict-data')

    print(f"\n[Smart] Running {job.name}: regime={job.regime} mode={job.mode} runs={job.runs}")
    print(f"[Smart] cmd: {' '.join(cmd)}")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line.rstrip())
    return proc.wait()


def _best_run_for_session(db: Optional[DatabaseClient], session_id: str) -> Optional[Dict]:
    if not db:
        return None
    try:
        rows = db.execute_query(
            """
            SELECT br.id, br.label, br.pnl_total, br.return_pct, br.win_rate, br.n_trades,
                   br.pnl_spot, br.pnl_futures, br.sharpe, br.max_drawdown
            FROM backtest_run br
            JOIN (
                SELECT (detail->>'run_id')::int AS run_id
                FROM trades_audit
                WHERE event = 'backtest_run_checkpoint'
                  AND detail->>'session_id' = %s
                  AND detail->>'status' = 'completed'
            ) ck ON ck.run_id = br.id
            ORDER BY br.pnl_total DESC
            LIMIT 1
            """,
            (session_id,),
        )
        return rows[0] if rows else None
    except Exception as e:
        print(f"[Smart] failed to fetch best run for {session_id}: {e}")
        return None


def _compare_vs_live(run_id: int, api_key: str) -> Optional[Dict]:
    if not api_key:
        return None
    try:
        qs = urllib.parse.urlencode({'run_id': run_id})
        req = urllib.request.Request(
            f"http://localhost:8000/backtest/compare-vs-live?{qs}",
            headers={'X-API-Key': api_key},
            method='GET',
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f"[Smart] parity compare failed for run_id={run_id}: {e}")
        return None


def _coverage_band(days: int, sims: int) -> str:
    # Coverage guidance table encoded as deterministic function.
    if days <= 120:
        if sims < 2000:
            return 'basic'
        if sims < 6000:
            return 'high-local'
        return 'very-high-recency'
    if days <= 450:
        if sims < 8000:
            return 'moderate-annual'
        if sims < 16000:
            return 'high-annual'
        return 'very-high-annual'
    if days <= 900:
        if sims < 14000:
            return 'moderate-multicycle'
        if sims < 28000:
            return 'high-multicycle'
        return 'very-high-multicycle'
    if sims < 20000:
        return 'moderate-global'
    if sims < 50000:
        return 'high-global'
    return 'maximum-global'


def _get_live_balance() -> Optional[float]:
    """Fetch current account balance from /account/balance endpoint.
    Returns total_usdt or None if unavailable."""
    try:
        req = urllib.request.Request(
            'http://localhost:8000/account/balance',
            headers={'Accept': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            total_usdt = data.get('total_usdt')
            if total_usdt is not None:
                return float(total_usdt)
    except Exception as e:
        print(f"[Smart] Failed to fetch live balance: {e}")
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description='Super Smart Backtest Optimizer (single-entry multimode runner)')
    parser.add_argument('--period', type=str, default='1y', help='3m|1y|2y|3y or integer days')
    parser.add_argument('--simulations', type=int, default=0, help='Total simulations budget for all scenarios')
    parser.add_argument('--auto-recommend-sims', action='store_true', help='Auto-fill simulations if omitted/zero')
    parser.add_argument('--capital', type=float, required=False, default=None, help='Total capital in USDT (spot+futures). If omitted, fetches from live account balance')
    parser.add_argument('--strict-data', action='store_true', help='Pass strict data gate to canonical optimizer')
    parser.add_argument('--session-id', type=str, default='', help='Optional explicit root session id')
    parser.add_argument('--lock-ttl-minutes', type=int, default=180, help='Per-run lock TTL passed to canonical optimizer')
    parser.add_argument('--dry-plan', action='store_true', help='Print execution plan and exit')
    args = parser.parse_args()

    # Resolve initial capital: explicit arg or live balance
    if args.capital is not None:
        capital = args.capital
        capital_source = "explicit --capital argument"
    else:
        capital = _get_live_balance()
        if capital is None:
            print("\n[ERROR] Unable to determine initial capital.")
            print("  Either provide --capital explicitly:")
            print("    python Dev/smart_backtest_optimizer.py --period 1y --simulations 12000 --capital 5283.95")
            print("  Or ensure /account/balance API is accessible (http://localhost:8000/account/balance)")
            sys.exit(1)
        capital_source = "live account balance"

    print(f"[Smart] Using capital: {capital:.2f} USDT (from {capital_source})")

    days = _period_to_days(args.period)
    sims = int(args.simulations)
    if sims <= 0 or args.auto_recommend_sims:
        sims = _recommended_simulations(days)

    now_tag = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    root_session_id = args.session_id.strip() or f"smart_{args.period}_{sims}_{now_tag}"

    reg_dist = _regime_distribution(days)
    runs_by_regime = _allocate_runs(sims, reg_dist)
    jobs = _build_jobs(root_session_id, runs_by_regime)

    coverage = _coverage_band(days, sims)

    print("\n" + "=" * 90)
    print("SUPER SMART BACKTEST OPTIMIZER")
    print("=" * 90)
    print(f"Period days:        {days}")
    print(f"Total simulations:  {sims}")
    print(f"Capital:            {args.capital:.2f} USDT")
    print(f"Root session:       {root_session_id}")
    print(f"Coverage band:      {coverage}")
    print("Regime distribution from BTC window:")
    for k in ('SPOT_BULL', 'RANGE_SCALP', 'PRESERVATION'):
        print(f"  - {k:<13}: {reg_dist.get(k, 0.0) * 100:6.2f}%")
    print("Allocated runs:")
    for j in jobs:
        print(f"  - {j.name:<14} regime={j.regime:<13} mode={j.mode:<10} runs={j.runs}")
    print("=" * 90 + "\n")

    db = _db_client()
    _audit_event(db, 'smart_backtest_session_start', {
        'root_session_id': root_session_id,
        'period_days': days,
        'simulations': sims,
        'capital': args.capital,
        'coverage': coverage,
        'regime_distribution': reg_dist,
        'runs_by_regime': runs_by_regime,
    })

    if args.dry_plan:
        _audit_event(db, 'smart_backtest_session_plan_only', {'root_session_id': root_session_id})
        print('[Smart] dry plan complete.')
        return

    started = time.time()
    failures: List[Tuple[str, int]] = []
    for job in jobs:
        _audit_event(db, 'smart_backtest_scenario_start', {
            'root_session_id': root_session_id,
            'scenario': job.name,
            'session_id': job.session_id,
            'regime': job.regime,
            'mode': job.mode,
            'runs': job.runs,
        })
        rc = _run_canonical_job(job, days, capital, args.strict_data, args.lock_ttl_minutes)
        _audit_event(db, 'smart_backtest_scenario_end', {
            'root_session_id': root_session_id,
            'scenario': job.name,
            'session_id': job.session_id,
            'exit_code': rc,
        })
        if rc != 0:
            failures.append((job.name, rc))

    api_key = os.environ.get('DASHBOARD_API_KEY', '')
    scenario_results = []
    for job in jobs:
        best = _best_run_for_session(db, job.session_id)
        parity = _compare_vs_live(best['id'], api_key) if best and best.get('id') else None
        scenario_results.append({
            'scenario': job.name,
            'session_id': job.session_id,
            'regime': job.regime,
            'mode': job.mode,
            'runs': job.runs,
            'best': best,
            'parity': parity,
        })

    elapsed = round(time.time() - started, 2)
    # Weighted score by detected regime prevalence and pnl/day with drawdown penalty.
    weights = {
        'spot_bull': reg_dist.get('SPOT_BULL', 0.0),
        'range_scalp': reg_dist.get('RANGE_SCALP', 0.0),
        'preservation': reg_dist.get('PRESERVATION', 0.0),
        'futures_short': max(0.15, reg_dist.get('PRESERVATION', 0.0)),
    }

    def score(item: Dict) -> float:
        b = item.get('best') or {}
        pnl = float(b.get('pnl_total') or 0.0)
        dd = float(b.get('max_drawdown') or 0.0)
        pnl_day = pnl / max(days, 1)
        # Conservative penalty for high drawdown magnitude.
        adj = pnl_day - (dd * 0.005)
        return adj * weights.get(item['scenario'], 0.0)

    ranked = sorted(scenario_results, key=score, reverse=True)

    print("\n" + "=" * 90)
    print("SMART BACKTEST SUMMARY")
    print("=" * 90)
    print(f"Elapsed: {elapsed}s")
    print(f"Failures: {failures if failures else 'none'}")

    for r in ranked:
        b = r.get('best')
        if not b:
            print(f"- {r['scenario']}: no completed runs found")
            continue
        print(
            f"- {r['scenario']}: run_id={b['id']} pnl={float(b['pnl_total']):+.2f} "
            f"ret={float(b['return_pct']):+.2f}% trades={int(b['n_trades'])} "
            f"spot={float(b['pnl_spot']):+.2f} fut={float(b['pnl_futures']):+.2f} "
            f"score={score(r):+.4f}"
        )
        p = r.get('parity')
        if p and isinstance(p, dict):
            gap = ((p.get('parity') or {}).get('pnl_total_gap_pct'))
            if gap is not None:
                print(f"    parity pnl_gap_pct={gap}")

    recommendation = ranked[0]['scenario'] if ranked else 'none'
    print(f"\nRecommended primary scenario: {recommendation}")

    _audit_event(db, 'smart_backtest_session_end', {
        'root_session_id': root_session_id,
        'period_days': days,
        'simulations': sims,
        'coverage': coverage,
        'elapsed_sec': elapsed,
        'failures': failures,
        'recommended_scenario': recommendation,
        'scenario_results': [
            {
                'scenario': x['scenario'],
                'session_id': x['session_id'],
                'runs': x['runs'],
                'best_run_id': (x.get('best') or {}).get('id'),
                'best_pnl': (x.get('best') or {}).get('pnl_total'),
                'best_return_pct': (x.get('best') or {}).get('return_pct'),
                'best_n_trades': (x.get('best') or {}).get('n_trades'),
            }
            for x in ranked
        ],
    })


if __name__ == '__main__':
    main()
