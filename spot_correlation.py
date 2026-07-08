"""
spot_correlation.py — Dynamic SPOT LONG Correlation Matrix (P3.2-S)

When multiple SPOT LONG positions move together (high correlation), the
diversification benefit is reduced. This module computes a portfolio-level
correlation score and returns a Kelly reduction factor so that concentrated
bets don't over-lever the account.

Design:
  - Uses 1h OHLCV closes (48 bars = 2 days) to compute pairwise Pearson r
  - Cache TTL = 15 min (3–4 spot cycles) — avoids excessive API calls
  - Thread-safe: single Lock guards the cache
  - Returns kelly_mult ∈ [0.65, 1.0] — never blocks entries, only sizes down
    - Fail-safe: any error returns the conservative minimum multiplier (0.65)
"""

import threading
import time
import logging
from typing import Dict, List, Tuple, Optional

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
_CACHE_TTL = 900    # 15 min
_N_BARS = 48        # 1h bars (2 days of data)
_INTERVAL = "1h"
_CORR_FLOOR = 0.60  # avg |r| below this → no reduction
_KELLY_MIN = 0.65   # minimum multiplier

# ── Module-level cache ────────────────────────────────────────────────────────
_lock = threading.Lock()
_cache: Dict = {
    "matrix": {},    # {(sym_a, sym_b): r}
    "ts": 0.0,
    "symbols": [],
}


def _fetch_closes(binance_client, symbol: str, n_bars: int = _N_BARS) -> List[float]:
    """Fetch last n_bars 1h close prices for symbol. Returns [] on error."""
    try:
        bc = binance_client.client
        klines = bc.get_klines(symbol=symbol, interval=_INTERVAL, limit=n_bars + 1)
        return [float(k[4]) for k in klines[:-1]]  # exclude last incomplete bar
    except Exception as e:
        logger.debug(f"[SpotCorrelation] fetch_closes {symbol}: {e}")
        return []


def _pearson(a: List[float], b: List[float]) -> Optional[float]:
    """Pearson correlation coefficient between two equal-length lists."""
    n = min(len(a), len(b))
    if n < 10:
        return None
    a, b = a[-n:], b[-n:]
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    std_a = (sum((x - mean_a) ** 2 for x in a) / n) ** 0.5
    std_b = (sum((x - mean_b) ** 2 for x in b) / n) ** 0.5
    if std_a == 0 or std_b == 0:
        return None
    return round(cov / (n * std_a * std_b), 4)


def _refresh_cache(binance_client, symbols: List[str]) -> None:
    """Fetch closes for all symbols and recompute pairwise correlation matrix."""
    closes: Dict[str, List[float]] = {}
    for sym in symbols:
        c = _fetch_closes(binance_client, sym)
        if c:
            closes[sym] = c

    matrix: Dict[Tuple[str, str], float] = {}
    syms = list(closes.keys())
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            r = _pearson(closes[syms[i]], closes[syms[j]])
            if r is not None:
                matrix[(syms[i], syms[j])] = abs(r)
                matrix[(syms[j], syms[i])] = abs(r)

    _cache["matrix"] = matrix
    _cache["ts"] = time.monotonic()
    _cache["symbols"] = syms
    logger.info(
        f"[SpotCorrelation] Matrix refreshed for {len(syms)} symbols: "
        + ", ".join(f"{k[0][:3]}/{k[1][:3]}={v:.2f}" for k, v in list(matrix.items())[:6])
    )


def get_portfolio_correlation(
    binance_client,
    open_long_symbols: List[str],
    force_refresh: bool = False,
) -> Tuple[float, float]:
    """
    Compute the average pairwise absolute correlation for open SPOT LONG positions.

    Returns:
        (avg_r, kelly_mult)
        avg_r      — average |Pearson r| across all pairs (0.0–1.0)
        kelly_mult — Kelly size multiplier to apply (0.65–1.0)

    Cached for _CACHE_TTL seconds. Fail-safe: returns (0.0, _KELLY_MIN) on any error.
    """
    if len(open_long_symbols) < 2:
        return 0.0, 1.0  # single or no position — no correlation to compute

    with _lock:
        now = time.monotonic()
        stale = (now - _cache["ts"]) > _CACHE_TTL
        syms_changed = set(open_long_symbols) != set(_cache["symbols"])

        if force_refresh or stale or syms_changed:
            try:
                _refresh_cache(binance_client, open_long_symbols)
            except Exception as e:
                logger.warning(f"[SpotCorrelation] Refresh failed: {e}")
                return 0.0, _KELLY_MIN

        matrix = _cache["matrix"]

    pairs = []
    syms = open_long_symbols
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            r = matrix.get((syms[i], syms[j]))
            if r is not None:
                pairs.append(r)

    if not pairs:
        return 0.0, 1.0

    avg_r = sum(pairs) / len(pairs)

    if avg_r <= _CORR_FLOOR:
        kelly_mult = 1.0
    else:
        reduction = (avg_r - _CORR_FLOOR) / (1.0 - _CORR_FLOOR)
        kelly_mult = max(_KELLY_MIN, 1.0 - reduction * (1.0 - _KELLY_MIN))

    kelly_mult = round(kelly_mult, 3)
    logger.info(
        f"[SpotCorrelation] {len(syms)} longs | avg_|r|={avg_r:.3f} | kelly_mult={kelly_mult:.3f} "
        f"({'no reduction' if kelly_mult >= 1.0 else f'-{(1-kelly_mult)*100:.0f}% size'})"
    )
    return round(avg_r, 4), kelly_mult


def _kelly_reduction(avg_r: float, corr_floor: float = _CORR_FLOOR, kelly_min: float = _KELLY_MIN) -> float:
    if avg_r <= corr_floor:
        return 1.0
    reduction = (avg_r - corr_floor) / (1.0 - corr_floor)
    return round(max(kelly_min, 1.0 - reduction * (1.0 - kelly_min)), 3)


def get_correlation_matrix(
    binance_client,
    symbols: List[str],
    force_refresh: bool = False,
) -> Dict[Tuple[str, str], float]:
    """Return the full |r| correlation matrix for the given symbols."""
    if len(symbols) < 2:
        return {}

    with _lock:
        now = time.monotonic()
        stale = (now - _cache["ts"]) > _CACHE_TTL
        syms_changed = set(symbols) != set(_cache["symbols"])

        if force_refresh or stale or syms_changed:
            try:
                _refresh_cache(binance_client, symbols)
            except Exception as e:
                logger.warning(f"[SpotCorrelation] Matrix refresh failed: {e}")
                return {}

        return dict(_cache["matrix"])


def invalidate_cache() -> None:
    """Force cache refresh on next call (e.g. after opening/closing a position)."""
    with _lock:
        _cache["ts"] = 0.0
    logger.debug("[SpotCorrelation] cache invalidated")
