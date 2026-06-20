"""
pair_screener.py — Dynamic pair screener using Binance 24h ticker.

Replaces static analysis_symbols / futures_symbols lists with a live-ranked
selection based on volume × momentum. Called once per cache TTL (default 15 min)
so it adds minimal latency to each trading cycle.

Tiers:
  TIER_A  vol_24h >= futures_min_vol ($100M)  → eligible for futures LONG/SHORT
  TIER_B  vol_24h >= spot_min_vol    ($10M)   → eligible for spot
  TIER_C  vol_24h <  spot_min_vol              → excluded

Score = vol_24h × abs(priceChangePercent24h)
       Higher score = more liquid AND more momentum → best candidates
"""

import json
import time
import urllib.request
from typing import Dict, List, Optional

# ── Module-level cache ────────────────────────────────────────────────────────
_screener_cache: Dict = {"data": None, "ts": 0.0}

# Stablecoins and non-crypto base tokens to exclude
_EXCLUDE_BASES = {
    "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "UST", "USDD", "USDP",
    "EUR", "GBP", "BRL", "TRY", "AUD", "RUB",
}

def _is_valid_pair(symbol: str, last_price: float) -> bool:
    """Return True if this USDT pair is a real crypto (not a stable or trash token)."""
    if not symbol.endswith("USDT"):
        return False
    base = symbol[:-4]
    if base in _EXCLUDE_BASES:
        return False
    if last_price < 1e-7:
        return False
    return True


def get_screened_pairs(
    spot_min_vol: float = 10_000_000,       # $10M min for spot eligibility
    futures_min_vol: float = 100_000_000,   # $100M min for futures eligibility
    max_spot: int = 50,                     # max spot candidates
    max_futures: int = 15,                  # max futures candidates
    cache_seconds: int = 900,               # 15 min default TTL
    settings=None,                          # optional: override defaults from settings
) -> Dict[str, List[str]]:
    """
    Return {"spot": [...], "futures": [...], "all": [...]} of best USDT pairs
    ranked by vol_24h × abs(priceChange24h%).

    Results are cached for `cache_seconds` to avoid hitting Binance on every
    5-min trading cycle.
    """
    # ── Read settings overrides ───────────────────────────────────────────────
    if settings is not None:
        spot_min_vol    = getattr(settings, 'screener_min_vol_usdt',     spot_min_vol)
        futures_min_vol = getattr(settings, 'screener_futures_min_vol',  futures_min_vol)
        max_spot        = getattr(settings, 'screener_max_pairs',        max_spot)
        cache_seconds   = int(getattr(settings, 'screener_cache_minutes', cache_seconds / 60) * 60)

    # ── Cache hit ─────────────────────────────────────────────────────────────
    now = time.time()
    if _screener_cache["data"] is not None and (now - _screener_cache["ts"]) < cache_seconds:
        return _screener_cache["data"]

    # ── Fetch Binance 24h ticker (public endpoint, no auth) ───────────────────
    try:
        url = "https://api.binance.com/api/v3/ticker/24hr"
        req = urllib.request.Request(url, headers={"User-Agent": "crypto-agent/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            tickers = json.loads(resp.read())
    except Exception as e:
        print(f"[Screener] ticker/24hr fetch failed: {e} — returning empty screener result")
        return {"spot": [], "futures": [], "all": []}

    # ── Parse and rank ────────────────────────────────────────────────────────
    candidates = []
    for t in tickers:
        symbol = t.get("symbol", "")
        try:
            price   = float(t.get("lastPrice", 0))
            vol     = float(t.get("quoteVolume", 0))   # volume in USDT
            chg_pct = float(t.get("priceChangePercent", 0))
        except (ValueError, TypeError):
            continue

        if not _is_valid_pair(symbol, price):
            continue
        if vol < spot_min_vol:
            continue

        # Bullish score for spot ranking (penalizes tokens in downtrend)
        # Bearish score kept separately — futures SHORT selection uses abs()
        spot_score    = vol * max(0.0, chg_pct)   # 0 if falling — avoids long entries in downtrend
        futures_score = vol * abs(chg_pct)         # full momentum for SHORT candidates
        candidates.append({
            "symbol": symbol,
            "vol_usdt": round(vol),
            "chg_pct": round(chg_pct, 2),
            "score": spot_score,
            "futures_score": futures_score,
        })

    # Sort spot by bullish score (uptrend priority), futures by abs momentum score
    candidates.sort(key=lambda x: x["score"], reverse=True)
    spot_pairs    = [c["symbol"] for c in candidates[:max_spot]]

    futures_candidates = sorted(
        [c for c in candidates if c["vol_usdt"] >= futures_min_vol],
        key=lambda x: x["futures_score"], reverse=True
    )
    futures_pairs = [c["symbol"] for c in futures_candidates[:max_futures]]

    result = {
        "spot":    spot_pairs,
        "futures": futures_pairs,
        "all":     [c["symbol"] for c in candidates],
        "_meta": {
            "fetched_at": now,
            "total_candidates": len(candidates),
            "spot_count": len(spot_pairs),
            "futures_count": len(futures_pairs),
        }
    }

    # ── Update cache ──────────────────────────────────────────────────────────
    _screener_cache["data"] = result
    _screener_cache["ts"]   = now

    print(
        f"[Screener] {len(candidates)} pairs scanned → "
        f"spot={len(spot_pairs)}, futures={len(futures_pairs)}"
    )
    return result


def merge_with_static(
    screened: List[str],
    static: List[str],
    max_total: int = 60,
) -> List[str]:
    """
    Merge dynamic screener result with static configured list.
    Static symbols always appear first (guaranteed analysis of known tokens),
    followed by dynamic additions, deduped, capped at max_total.
    """
    merged = list(dict.fromkeys(static + screened))
    return merged[:max_total]


def invalidate_cache() -> None:
    """Force next call to re-fetch from Binance (use after config change)."""
    _screener_cache["ts"] = 0.0
    _screener_cache["data"] = None
