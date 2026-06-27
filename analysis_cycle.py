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
from datetime import datetime, timezone
from typing import List, Dict

import error_collector as ec
from config import TRADE_CYCLE_MINUTES
from analysis_cache import analysis_cache
from signal_store import get_signals
from strategy import normalize_symbol

logger = logging.getLogger(__name__)


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

    for symbol in symbols:
        try:
            sig = asg.analyze_symbol(symbol, interval=interval, use_multi_timeframe=True)
            if not sig:
                results[symbol] = {'status': 'no_data'}
                continue

            indicators = sig.get('indicators', {})
            # Enrich indicators with top-level signal fields (vol_spike, trend_4h, etc.)
            indicators['vol_spike']    = sig.get('vol_spike', indicators.get('vol_spike', 0.0))
            indicators['trend_4h']     = sig.get('higher_tf_trend', 'FLAT')
            indicators['signal']       = sig.get('signal', 'HOLD')
            indicators['strength']     = sig.get('strength', 0.0)
            indicators['change_24h_pct'] = sig.get('change_24h_pct', 0.0)
            indicators['bb_bandwidth'] = sig.get('bb_bandwidth', indicators.get('bb_bandwidth', 0.0))
            indicators['bb_lower_touch'] = sig.get('bb_lower_touch', False)
            indicators['current_price'] = sig.get('current_price', 0.0)

            # tech_score: use strength × 100 as proxy (0-100 scale)
            tech_score = float(sig.get('strength', 0.0)) * 100.0

            analysis_cache.set(symbol, indicators, tech_score)
            results[symbol] = {
                'indicators':    indicators,
                'technical_score': tech_score,
                'signal':        sig.get('signal', 'HOLD'),
                'strength':      sig.get('strength', 0.0),
                'status':        'success',
            }
            logger.info(
                f"[AnalysisCycle] {symbol}: signal={sig.get('signal')} "
                f"strength={sig.get('strength', 0):.2f} "
                f"vol_spike={indicators.get('vol_spike', 0):.2f} "
                f"trend_4h={indicators.get('trend_4h')} "
                f"rsi={indicators.get('rsi', 0):.1f}"
            )
        except Exception as e:
            logger.error(f"[AnalysisCycle] Failed to analyze {symbol}: {e}")
            ec.add_error(ec.SOURCE_ANALYSIS, ec.LEVEL_ERROR, f"Failed to analyze {symbol}: {e}")
            results[symbol] = {'status': 'error', 'error': str(e)}

    return results


def execute_once() -> None:
    """Run one technical analysis cycle (real indicators for tracked symbols).

    P1.2: Uses AutonomousSignalGenerator instead of legacy get_indicators().
    FUTURES_SHORT fallback: analyzes open shorts when no BUY signals exist.
    """
    try:
        logger.info("[AnalysisCycle] Starting technical analysis cycle")
        symbols = get_top_symbols_from_signals(limit=5)

        if not symbols:
            # P1.2 FUTURES_SHORT fallback — keep analysis_cache fresh for SSO
            symbols = _get_futures_short_symbols()
            if symbols:
                logger.info(
                    f"[AnalysisCycle] No BUY signals — FUTURES_SHORT fallback: "
                    f"analyzing {len(symbols)} active short(s): {symbols}"
                )
            else:
                logger.warning("[AnalysisCycle] No BUY signals and no open shorts — skipping cycle")
                return

        logger.info(f"[AnalysisCycle] Analyzing {len(symbols)} symbols: {', '.join(symbols)}")
        client = _get_client()
        settings = _get_settings()
        results = analyze_symbols(symbols, client=client, settings=settings)

        ok = sum(1 for r in results.values() if r.get('status') == 'success')
        logger.info(f"[AnalysisCycle] Cycle complete: {ok}/{len(results)} symbols analyzed successfully")

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
        logger.error(f"[AnalysisCycle] Technical analysis cycle failed: {e}")
        ec.add_error(ec.SOURCE_ANALYSIS, ec.LEVEL_ERROR, f"Technical analysis cycle failed: {e}")


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
