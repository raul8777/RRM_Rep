"""Periodic technical analysis cycle - runs every 15 minutes."""
import time
import logging
from datetime import datetime
from typing import List, Dict

import error_collector as ec
from config import TRADE_CYCLE_MINUTES
from technical_analysis import get_indicators, score_technical
from analysis_cache import analysis_cache
from signal_store import get_signals
from strategy import normalize_symbol

logger = logging.getLogger(__name__)


def _get_client():
    """Return the shared BinanceClient from init_components (lazy import)."""
    from init_components import get_components
    return get_components().binance_client


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


def analyze_symbols(symbols: List[str], client=None) -> Dict[str, Dict]:
    """Analyze technical indicators for each symbol."""
    if client is None:
        client = _get_client()
    results = {}

    for symbol in symbols:
        try:
            indicators = get_indicators(client, symbol)
            tech_score = score_technical(indicators)
            analysis_cache.set(symbol, indicators, tech_score)
            results[symbol] = {
                'indicators': indicators,
                'technical_score': tech_score,
                'status': 'success',
            }
            logger.info(f"Analyzed {symbol}: score={tech_score:.1f}")
        except Exception as e:
            logger.error(f"Failed to analyze {symbol}: {e}")
            ec.add_error(ec.SOURCE_ANALYSIS, ec.LEVEL_ERROR, f"Failed to analyze {symbol}: {e}")
            results[symbol] = {'status': 'error', 'error': str(e)}

    return results


def execute_once() -> None:
    """Run one technical analysis cycle (indicators + scores for tracked symbols)."""
    try:
        logger.info("Starting technical analysis cycle")
        symbols = get_top_symbols_from_signals(limit=5)

        if not symbols:
            logger.warning("No BUY signals found for analysis")
            return

        logger.info(f"Analyzing {len(symbols)} symbols: {', '.join(symbols)}")
        results = analyze_symbols(symbols, client=_get_client())
        logger.info(f"Analysis cycle complete. Results: {len(results)} symbols analyzed")

    except Exception as e:
        logger.error(f"Technical analysis cycle failed: {e}")
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
