"""Periodic technical analysis cycle - runs every 15 minutes.

P1.2 (Master Plan 863057): Replaced legacy get_indicators() with
AutonomousSignalGenerator.analyze_symbol() so that vol_spike, trend_4h,
and BB bands (the two highest-weight predictors at 0.15 each) are included
in analysis_cache and reach the PnL predictor correctly.

FUTURES_SHORT fallback: when no BUY signals exist, analyze the currently
open short positions so SSO has fresh indicators even in bear market.
"""
import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import List, Dict

import error_collector as ec
from config import TRADE_CYCLE_MINUTES
from analysis_cache import analysis_cache
from signal_store import get_signals
from strategy import normalize_symbol
from binance_truth_guard import is_truth_critical
from universe_selector import select_analysis_universe

logger = logging.getLogger(__name__)


class _RateLimiter:
    """Simple sliding-window limiter for task admission (target req/s)."""

    def __init__(self, rate_per_second: float):
        self.rate = max(1.0, float(rate_per_second))
        self._lock = threading.Lock()
        self._events: List[float] = []

    def acquire(self) -> None:
        while True:
            now = time.monotonic()
            with self._lock:
                self._events = [t for t in self._events if now - t < 1.0]
                if len(self._events) < int(self.rate):
                    self._events.append(now)
                    return
                wait_s = max(0.01, 1.0 - (now - self._events[0]))
            time.sleep(wait_s)


def _get_client():
    """Return the shared BinanceClient from init_components (lazy import)."""
    from init_components import get_components
    return get_components().binance_client


def _get_settings():
    """Return settings singleton."""
    from init_components import get_components
    return get_components().settings


def get_top_symbols_from_signals(limit: int = 5) -> List[str]:
    """Extract top N unique buy symbols from recent signals."""
    signals = get_signals()
    symbols = []
    seen = set()

    for signal in reversed(signals):
        if signal.get('action') == 'BUY':
            sym = normalize_symbol(signal.get('symbol', ''))
            if sym and sym not in seen:
                symbols.append(sym)
                seen.add(sym)
                if len(symbols) >= limit:
                    break

    return symbols


def _get_futures_short_symbols() -> List[str]:
    """P1.2 FUTURES_SHORT fallback: return symbols of currently open short positions."""
    try:
        from run_trades import _futures_client as _fc
        if _fc and hasattr(_fc, '_open_positions') and _fc._open_positions:
            syms = []
            for key in list(_fc._open_positions.keys()):
                # key format: "SYMBOL_SIDE" or just symbol
                sym = key.split('_')[0] if '_' in key else key
                if sym and sym not in syms:
                    syms.append(sym)
            return syms
    except Exception:
        pass
    return []


def analyze_symbols(symbols: List[str], client=None, settings=None) -> Dict[str, Dict]:
    """P1.2: Analyze symbols using AutonomousSignalGenerator (real indicators).

    Uses analyze_symbol() which computes vol_spike, trend_4h, RSI, MACD, BB bands
    — the full indicator set used by the PnL predictor's learned weights.
    Falls back gracefully per-symbol without stopping the entire cycle.
    """
    if client is None:
        client = _get_client()
    if settings is None:
        settings = _get_settings()

    results = {}
    try:
        from autonomous_signals import AutonomousSignalGenerator
        asg = AutonomousSignalGenerator(client)
    except Exception as e:
        logger.error(f"[AnalysisCycle] Cannot instantiate AutonomousSignalGenerator: {e}")
        ec.add_error(ec.SOURCE_ANALYSIS, ec.LEVEL_ERROR, f"ASG init failed: {e}")
        return results

    interval = getattr(settings, 'interval', '1h') if settings else '1h'

    max_workers = max(1, int(getattr(settings, 'analysis_cycle_max_workers', 10) if settings else 10))
    req_rate = float(getattr(settings, 'analysis_cycle_request_rate_limit', 20.0) if settings else 20.0)
    req_slots = max(1, int(getattr(settings, 'analysis_cycle_request_semaphore', 4) if settings else 4))
    latency_warn_s = float(getattr(settings, 'analysis_cycle_latency_warn_seconds', 20.0) if settings else 20.0)
    limiter = _RateLimiter(req_rate)
    request_semaphore = threading.BoundedSemaphore(req_slots)
    latencies: List[float] = []

    def _analyze_one(symbol: str):
        started = time.monotonic()
        limiter.acquire()
        with request_semaphore:
            sig = asg.analyze_symbol(symbol, interval=interval, use_multi_timeframe=True)
        return symbol, sig, time.monotonic() - started

    def _store_result(symbol: str, sig: Dict):
        if not sig:
            results[symbol] = {'status': 'no_data'}
            return
        indicators = sig.get('indicators', {})
        if not isinstance(indicators, dict):
            indicators = {}
        # Enrich indicators with top-level signal fields (vol_spike, trend_4h, etc.)
        indicators['vol_spike'] = sig.get('vol_spike', indicators.get('vol_spike', 0.0))
        indicators['trend_4h'] = sig.get('higher_tf_trend', 'FLAT')
        indicators['signal'] = sig.get('signal', 'HOLD')
        indicators['strength'] = sig.get('strength', 0.0)
        indicators['change_24h_pct'] = sig.get('change_24h_pct', 0.0)
        indicators['bb_bandwidth'] = sig.get('bb_bandwidth', indicators.get('bb_bandwidth', 0.0))
        indicators['bb_lower_touch'] = sig.get('bb_lower_touch', False)
        indicators['current_price'] = sig.get('current_price', 0.0)
        tech_score = float(sig.get('strength', 0.0)) * 100.0
        analysis_cache.set(symbol, indicators, tech_score)
        results[symbol] = {
            'indicators': indicators,
            'technical_score': tech_score,
            'signal': sig.get('signal', 'HOLD'),
            'strength': sig.get('strength', 0.0),
            'status': 'success',
        }
        logger.info(
            f"[AnalysisCycle] {symbol}: signal={sig.get('signal')} "
            f"strength={sig.get('strength', 0):.2f} "
            f"vol_spike={indicators.get('vol_spike', 0):.2f} "
            f"trend_4h={indicators.get('trend_4h')} "
            f"rsi={indicators.get('rsi', 0):.1f}"
        )

    cycle_started = time.monotonic()
    if len(symbols) <= 1 or max_workers == 1:
        for symbol in symbols:
            try:
                _sym, _sig, _lat = _analyze_one(symbol)
                latencies.append(_lat)
                _store_result(_sym, _sig)
            except Exception as e:
                import traceback as _tb
                _tb_str = _tb.format_exc()
                logger.error(f"[AnalysisCycle] Failed to analyze {symbol}: {e}\n{_tb_str}")
                ec.add_error(ec.SOURCE_ANALYSIS, ec.LEVEL_ERROR, f"Failed to analyze {symbol}: {e}\n{_tb_str}")
                results[symbol] = {'status': 'error', 'error': str(e), 'traceback': _tb_str}
    else:
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='analysis') as pool:
            future_map = {pool.submit(_analyze_one, symbol): symbol for symbol in symbols}
            for future in as_completed(future_map):
                symbol = future_map[future]
                try:
                    _sym, _sig, _lat = future.result()
                    latencies.append(_lat)
                    _store_result(_sym, _sig)
                except Exception as e:
                    import traceback as _tb
                    _tb_str = _tb.format_exc()
                    logger.error(f"[AnalysisCycle] Failed to analyze {symbol}: {e}\n{_tb_str}")
                    ec.add_error(ec.SOURCE_ANALYSIS, ec.LEVEL_ERROR, f"Failed to analyze {symbol}: {e}\n{_tb_str}")
                    results[symbol] = {'status': 'error', 'error': str(e), 'traceback': _tb_str}

    cycle_latency = time.monotonic() - cycle_started
    if latencies:
        avg_latency = sum(latencies) / len(latencies)
        max_latency = max(latencies)
    else:
        avg_latency = 0.0
        max_latency = 0.0
    logger.info(
        f"[AnalysisCycle] Latency: total={cycle_latency:.2f}s avg_symbol={avg_latency:.2f}s "
        f"max_symbol={max_latency:.2f}s workers={max_workers} req_rate={req_rate:.1f}/s slots={req_slots}"
    )
    if cycle_latency > latency_warn_s:
        logger.warning(
            f"[AnalysisCycle] Latency alert: {cycle_latency:.2f}s > {latency_warn_s:.2f}s "
            f"(symbols={len(symbols)}, workers={max_workers})"
        )

    return results


def execute_once() -> None:
    """Run one technical analysis cycle (real indicators for tracked symbols).

    P1.2: Uses AutonomousSignalGenerator instead of legacy get_indicators().
    FUTURES_SHORT fallback: analyzes open shorts when no BUY signals exist.
    """
    try:
        logger.info("[AnalysisCycle] Starting technical analysis cycle")
        try:
            from init_components import get_components as _gc
            _db = _gc().db_client
            if is_truth_critical(_db, max_age_seconds=900):
                logger.warning("[AnalysisCycle] skipped: binance truth status is critical")
                return
        except Exception:
            pass
        symbols = get_top_symbols_from_signals(limit=5)

        if not symbols:
            try:
                from init_components import get_components as _gc
                _comp = _gc()
            except Exception:
                _comp = None

            settings = _get_settings()
            active_regime = str(getattr(settings, 'current_regime_mode', '') or '').strip().upper() or None
            short_symbols = _get_futures_short_symbols()
            universe = select_analysis_universe(
                settings=settings,
                regime_mode=active_regime,
                profile_label=None,  # s95: per-operation profiles, no global active profile
                db_client=getattr(_comp, 'db_client', None),
            )
            symbols = list(dict.fromkeys((short_symbols or []) + (universe.get('symbols') or [])))
            if short_symbols:
                logger.info(
                    f"[AnalysisCycle] No BUY signals — FUTURES_SHORT fallback: "
                    f"analyzing {len(short_symbols)} active short(s): {short_symbols}"
                )
            logger.info(
                f"[AnalysisCycle] Shared universe selector: regime={universe.get('regime_mode')} "
                f"profile={universe.get('profile_label') or '-'} source={universe.get('source')} "
                f"reason={universe.get('reason')} cap={universe.get('cap')} selected={len(symbols)}"
            )
            if not symbols:
                logger.warning("[AnalysisCycle] No BUY signals, shorts, or fallback universe — skipping cycle")
                return

        logger.info(f"[AnalysisCycle] Analyzing {len(symbols)} symbols: {', '.join(symbols)}")
        client = _get_client()
        settings = _get_settings()
        cycle_started = time.monotonic()
        results = analyze_symbols(symbols, client=client, settings=settings)
        cycle_elapsed = time.monotonic() - cycle_started

        ok = sum(1 for r in results.values() if r.get('status') == 'success')
        logger.info(
            f"[AnalysisCycle] Cycle complete: {ok}/{len(results)} symbols analyzed successfully "
            f"in {cycle_elapsed:.2f}s"
        )

        # P2.3: Persist analysis_cache_freshness to trading_runtime_state
        try:
            import json as _json
            from init_components import get_components as _gc
            _db = _gc().db_client
            if _db:
                _payload = _json.dumps({
                    'ts': datetime.now(timezone.utc).isoformat(),
                    'symbols': [s for s, r in results.items() if r.get('status') == 'success'],
                    'cycle': ok,
                })
                _db.execute_update(
                    "INSERT INTO trading_runtime_state (key, value, updated_at) "
                    "VALUES ('analysis_cache_freshness', %s, NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                    (_payload, _payload)
                )
        except Exception as _rs_err:
            logger.debug(f"[AnalysisCycle] runtime_state persist failed (non-fatal): {_rs_err}")

    except Exception as e:
        import traceback as _tb
        _tb_str = _tb.format_exc()
        logger.error(f"[AnalysisCycle] Technical analysis cycle failed: {e}\n{_tb_str}")
        ec.add_error(ec.SOURCE_ANALYSIS, ec.LEVEL_ERROR, f"Technical analysis cycle failed: {e}\n{_tb_str}")


def run_loop() -> None:
    """Infinite loop for periodic technical analysis."""
    cycle_seconds = TRADE_CYCLE_MINUTES * 60 * 3  # Run every 3x trade cycle (15 min if trade is 5 min)
    logger.info(f"Starting analysis loop - runs every {cycle_seconds} seconds ({cycle_seconds / 60:.0f} minutes)")

    while True:
        try:
            execute_once()
        except Exception as e:
            logger.error(f"Unhandled exception in analysis loop: {e}")
        
        time.sleep(cycle_seconds)
