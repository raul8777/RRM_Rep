"""
pair_screener.py — Regime-aware dynamic pair screener using Binance 24h ticker.

Replaces static analysis_symbols / futures_symbols lists with a live-ranked
selection based on volume × momentum. The ranking is adjusted by the active
market regime so the scanner does not suggest LONGs while the system is in
FUTURES_SHORT / PRESERVATION. Called once per cache TTL (default 15 min).

Tiers:
  TIER_A  vol_24h >= futures_min_vol ($100M)  → eligible for futures
  TIER_B  vol_24h >= spot_min_vol    ($10M)   → eligible for spot
  TIER_C  vol_24h <  spot_min_vol              → excluded

Score is regime-dependent:
  SPOT_BULL                    → spot: vol × positive chg (long momentum)
  RANGE_SCALP                  → spot/futures: vol × abs(chg) (range volatility)
  PRESERVATION / FUTURES_SHORT → spot: narrative-only or empty (no new buys)
                                   futures: futures_short_score (bear bounces)

Every screening result is persisted to `screener_decisions` for full auditability.
"""

import json
import time
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any


def _cfg_param(settings, name: str, default: Any = None) -> Any:
    """Read a parameter from settings using canonical defaults if missing."""
    if settings is None:
        return default
    from config_helpers import get_param
    return get_param(settings, name, default)


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


def _futures_short_score(c: dict, cached_indicators: dict = None) -> float:
    """
    Score optimized for SHORT candidates in FUTURES_SHORT regime.

    Goal: short bounces inside a bearish trend, NOT already-overextended dumps.
    A positive 24h change in a bearish context is often a bounce to short.
    """
    vol = c.get("vol_usdt", 0.0)
    chg = c.get("chg_pct", 0.0)
    base = vol * abs(chg)
    if base == 0:
        return 0.0

    # Enrich with cached technical indicators if available
    rsi = None
    trend = None
    if cached_indicators:
        rsi = cached_indicators.get("rsi")
        trend = cached_indicators.get("trend_4h")

    score = base
    # Reward moderate bounces (likely continuation shorts) on a DOWN day
    if 0.5 <= chg <= 8.0:
        score *= 1.40
    # Heavy penalty for already-overextended drops (risk of short squeeze)
    if chg < -8.0:
        score *= 0.50
    # Heavy penalty for strong bullish momentum (possible breakout)
    if chg > 12.0:
        score *= 0.30

    # Cached indicator overrides (best-effort, no extra API calls)
    if trend == "DOWN" and rsi is not None and 45 <= rsi <= 65:
        score *= 1.30
    elif trend == "UP" and chg > 5.0:
        score *= 0.50
    elif rsi is not None and rsi < 30:
        score *= 0.50  # oversold extreme

    return round(score, 2)


def _score_for_regime(c: dict, regime_mode: str, cached_indicators: dict = None) -> Dict[str, float]:
    """
    Return a dict of score keys for the candidate based on the active regime.

    - SPOT_BULL: spot score rewards positive momentum.
    - RANGE_SCALP: spot/futures score uses absolute volatility.
    - PRESERVATION / FUTURES_SHORT: spot score is zeroed (no new buys);
      futures uses the bear-bounce score.
    """
    vol = c.get("vol_usdt", 0.0)
    chg = c.get("chg_pct", 0.0)
    futures_short = _futures_short_score(c, cached_indicators=cached_indicators)

    if regime_mode in ("PRESERVATION", "FUTURES_SHORT"):
        spot_score = 0.0
        range_score = 0.0
    elif regime_mode == "RANGE_SCALP":
        spot_score = vol * abs(chg)
        range_score = spot_score
    else:  # SPOT_BULL or unknown
        spot_score = vol * max(0.0, chg)
        range_score = vol * abs(chg)

    return {
        "spot_score": round(spot_score, 2),
        "futures_score": round(vol * abs(chg), 2),
        "futures_short_score": futures_short,
        "range_score": round(range_score, 2),
    }


def _get_narrative_tokens(settings) -> List[str]:
    """Return narrative tokens from settings if available, otherwise empty list."""
    tokens = _cfg_param(settings, 'narrative_tokens')
    if not tokens:
        return []
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except Exception:
            return []
    return [str(t) for t in tokens if t]


def _persist_screener_decision(
    db_client,
    regime_mode: str,
    result: Dict[str, Any],
    parameters: Dict[str, Any],
    rejected_reason: Optional[str] = None,
) -> None:
    """Persist a screening decision to screener_decisions for full auditability."""
    if db_client is None:
        return
    try:
        scores = {}
        for key in ("top_bearish", "top_bullish"):
            scores[key] = result.get("_meta", {}).get(key, [])
        db_client.execute_update(
            """
            INSERT INTO screener_decisions
                (regime_mode, spot_symbols, futures_symbols, all_symbols,
                 scores_json, parameters_json, rejected_reason, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pair_screener')
            """,
            (
                regime_mode,
                json.dumps(result.get("spot", [])),
                json.dumps(result.get("futures", [])),
                json.dumps(result.get("all", [])),
                json.dumps(scores),
                json.dumps(parameters),
                rejected_reason,
            ),
        )
    except Exception as e:
        print(f"[Screener] DB persist failed: {e}")


def _persist_screener_snapshot(db_client, result: Dict[str, Any]) -> None:
    """Persist the latest successful screener result for restart-safe fallback."""
    if db_client is None:
        return
    try:
        db_client.execute_update(
            """
            INSERT INTO trading_runtime_state (key, value, updated_at)
            VALUES (%s, %s::jsonb, NOW())
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value,
                updated_at = NOW()
            """,
            ('screener_snapshot', json.dumps(result)),
        )
    except Exception as e:
        print(f"[Screener] snapshot persist failed: {e}")


def _load_screener_snapshot(db_client) -> Optional[Dict[str, Any]]:
    """Load the last successful screener snapshot from DB fallback storage."""
    if db_client is None:
        return None
    try:
        rows = db_client.execute_query(
            "SELECT value FROM trading_runtime_state WHERE key = %s",
            ('screener_snapshot',),
        )
        if not rows:
            return None
        value = rows[0].get('value')
        if isinstance(value, str):
            value = json.loads(value)
        if isinstance(value, dict):
            return value
    except Exception as e:
        print(f"[Screener] snapshot load failed: {e}")
    return None


def _load_profile_screener_overrides(
    db_client,
    regime_mode: str,
    profile_label: Optional[str],
) -> Dict[str, Any]:
    """Load optional screener overrides from active regime profile params in runtime state."""
    if db_client is None or not profile_label:
        return {}
    try:
        key = f"regime_profile_{regime_mode}_{str(profile_label).strip().upper()}"
        rows = db_client.execute_query(
            "SELECT value FROM trading_runtime_state WHERE key = %s",
            (key,),
        )
        if not rows:
            return {}
        raw = rows[0].get("value")
        if isinstance(raw, str):
            raw = json.loads(raw)
        if isinstance(raw, dict) and isinstance(raw.get("params"), dict):
            raw = raw.get("params")
        if not isinstance(raw, dict):
            return {}
        out: Dict[str, Any] = {}
        for src in (
            "screener_min_vol_usdt",
            "screener_futures_min_vol",
            "screener_max_pairs",
            "screener_max_futures_pairs",
            "screener_cache_minutes",
            "screener_snapshot_max_age_minutes",
        ):
            if src in raw:
                out[src] = raw[src]
        return out
    except Exception as e:
        print(f"[Screener] profile override load failed: {e}")
        return {}


def _snapshot_is_usable(
    snapshot: Dict[str, Any],
    regime_mode: str,
    profile_label: Optional[str],
    max_age_seconds: int,
) -> bool:
    """Validate fallback snapshot alignment with current regime/profile and freshness."""
    meta = (snapshot or {}).get("_meta", {}) if isinstance(snapshot, dict) else {}
    snap_regime = str(meta.get("regime_mode") or "").strip().upper()
    if snap_regime and snap_regime != str(regime_mode or "").strip().upper():
        return False

    req_profile = str(profile_label or "").strip().upper()
    snap_profile = str(meta.get("profile_label") or "").strip().upper()
    if req_profile and snap_profile and req_profile != snap_profile:
        return False

    snap_ts = meta.get("fetched_at_ts")
    try:
        snap_ts = float(snap_ts)
    except Exception:
        snap_ts = 0.0
    if snap_ts <= 0:
        return False
    return (time.time() - snap_ts) <= max(0, int(max_age_seconds))


def get_screened_pairs(
    spot_min_vol: float = 10_000_000,       # $10M min for spot eligibility
    futures_min_vol: float = 100_000_000,   # $100M min for futures eligibility
    max_spot: int = 50,                     # max spot candidates
    max_futures: int = 15,                  # max futures candidates
    cache_seconds: int = 900,               # 15 min default TTL
    settings=None,                          # optional: override defaults from settings
    regime_mode: str = "SPOT_BULL",         # active market regime
    profile_label: Optional[str] = None,    # active profile label (A/B/C)
    db_client=None,                         # optional DB client for audit persistence
) -> Dict[str, List[str]]:
    """
    Return {"spot": [...], "futures": [...], "all": [...]} of best USDT pairs
    ranked by vol_24h × momentum, adjusted for the active market regime.

    Results are cached for `cache_seconds` to avoid hitting Binance on every
    5-min trading cycle. The cache is invalidated when the regime changes.
    """
    # ── Read settings overrides ───────────────────────────────────────────────
    if settings is not None:
        from config_helpers import get_param
        spot_min_vol    = get_param(settings, 'screener_min_vol_usdt')
        futures_min_vol = get_param(settings, 'screener_futures_min_vol')
        max_spot        = get_param(settings, 'screener_max_pairs')
        cache_seconds   = int(get_param(settings, 'screener_cache_minutes') * 60)
        regime_mode     = get_param(settings, 'current_regime_mode', regime_mode)

    _snapshot_max_age_seconds = max(60, int(cache_seconds * 3))
    if settings is not None:
        try:
            from config_helpers import get_param
            _snapshot_max_age_seconds = int(get_param(settings, 'screener_snapshot_max_age_minutes', _snapshot_max_age_seconds / 60) * 60)
        except Exception:
            pass

    # Profile-aware overrides (if present in runtime-state profile params)
    _profile_overrides = _load_profile_screener_overrides(db_client, regime_mode, profile_label)
    if _profile_overrides:
        try:
            if 'screener_min_vol_usdt' in _profile_overrides:
                spot_min_vol = float(_profile_overrides['screener_min_vol_usdt'])
            if 'screener_futures_min_vol' in _profile_overrides:
                futures_min_vol = float(_profile_overrides['screener_futures_min_vol'])
            if 'screener_max_pairs' in _profile_overrides:
                max_spot = int(_profile_overrides['screener_max_pairs'])
            if 'screener_max_futures_pairs' in _profile_overrides:
                max_futures = int(_profile_overrides['screener_max_futures_pairs'])
            if 'screener_cache_minutes' in _profile_overrides:
                cache_seconds = max(60, int(float(_profile_overrides['screener_cache_minutes']) * 60))
            if 'screener_snapshot_max_age_minutes' in _profile_overrides:
                _snapshot_max_age_seconds = max(60, int(float(_profile_overrides['screener_snapshot_max_age_minutes']) * 60))
        except Exception as e:
            print(f"[Screener] profile override parse failed: {e}")

    # ── Cache hit ─────────────────────────────────────────────────────────────
    now = time.time()
    if _screener_cache["data"] is not None and (now - _screener_cache["ts"]) < cache_seconds:
        cached = _screener_cache["data"]
        _cm = cached.get("_meta", {})
        _cached_profile = str(_cm.get("profile_label") or "").strip().upper()
        _req_profile = str(profile_label or "").strip().upper()
        if _cm.get("regime_mode") == regime_mode and (not _req_profile or _cached_profile == _req_profile):
            return cached
        # Regime changed — invalidate cache
        invalidate_cache()

    # ── Fetch Binance 24h ticker (public endpoint, no auth) ───────────────────
    try:
        url = "https://api.binance.com/api/v3/ticker/24hr"
        req = urllib.request.Request(url, headers={"User-Agent": "crypto-agent/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            tickers = json.loads(resp.read())
    except Exception as e:
        print(f"[Screener] ticker/24hr fetch failed: {e} — trying DB snapshot fallback")
        snapshot = _load_screener_snapshot(db_client)
        _fb_params = {
            "spot_min_vol": spot_min_vol,
            "futures_min_vol": futures_min_vol,
            "max_spot": max_spot,
            "max_futures": max_futures,
            "cache_seconds": cache_seconds,
            "profile_label": (str(profile_label).strip().upper() if profile_label else None),
            "snapshot_max_age_seconds": _snapshot_max_age_seconds,
        }
        if snapshot and _snapshot_is_usable(snapshot, regime_mode, profile_label, _snapshot_max_age_seconds):
            print(
                f"[Screener] using snapshot fallback "
                f"(regime={regime_mode}, profile={profile_label or '-'}, max_age_s={_snapshot_max_age_seconds})"
            )
            _persist_screener_decision(
                db_client,
                regime_mode,
                snapshot,
                _fb_params,
                rejected_reason="ticker_fetch_failed_snapshot_used",
            )
            return snapshot
        if snapshot:
            print(
                f"[Screener] snapshot rejected (regime/profile/age mismatch) "
                f"for regime={regime_mode}, profile={profile_label or '-'}"
            )
            _persist_screener_decision(
                db_client,
                regime_mode,
                {"spot": [], "futures": [], "all": [], "_meta": {"regime_mode": regime_mode, "profile_label": profile_label}},
                _fb_params,
                rejected_reason="ticker_fetch_failed_snapshot_rejected",
            )
        else:
            _persist_screener_decision(
                db_client,
                regime_mode,
                {"spot": [], "futures": [], "all": [], "_meta": {"regime_mode": regime_mode, "profile_label": profile_label}},
                _fb_params,
                rejected_reason="ticker_fetch_failed_no_snapshot",
            )
        return {"spot": [], "futures": [], "all": []}

    # ── Best-effort indicator cache enrichment (no extra API calls) ───────────
    try:
        from analysis_cache import analysis_cache as _ac
        _cached_all = _ac.get_all() or {}
    except Exception:
        _cached_all = {}

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

        cached_ind = _cached_all.get(symbol, {}).get("indicators", {}) if _cached_all else {}
        scores = _score_for_regime(
            {"vol_usdt": vol, "chg_pct": chg_pct}, regime_mode, cached_indicators=cached_ind
        )
        candidates.append({
            "symbol": symbol,
            "vol_usdt": round(vol),
            "chg_pct": round(chg_pct, 2),
            "score": scores["spot_score"],
            "futures_score": scores["futures_score"],
            "futures_short_score": scores["futures_short_score"],
            "range_score": scores["range_score"],
        })

    # Spot ranking depends on regime: bear modes avoid new longs
    if regime_mode in ("PRESERVATION", "FUTURES_SHORT"):
        # In bear market SPOT does not open new longs (no short spot available).
        # Allow only narrative tokens if the user has explicitly configured them.
        narrative_tokens = _get_narrative_tokens(settings) if settings else []
        # Keep only narrative tokens that passed the volume filter and sort by bullish score
        narrative_candidates = [c for c in candidates if c["symbol"] in narrative_tokens]
        narrative_candidates.sort(key=lambda x: x["score"], reverse=True)
        spot_pairs = [c["symbol"] for c in narrative_candidates[:max_spot]]
    elif regime_mode == "RANGE_SCALP":
        candidates.sort(key=lambda x: x["range_score"], reverse=True)
        spot_pairs = [c["symbol"] for c in candidates[:max_spot]]
    else:  # SPOT_BULL or unknown
        candidates.sort(key=lambda x: x["score"], reverse=True)
        spot_pairs = [c["symbol"] for c in candidates[:max_spot]]

    # Futures ranking depends on regime: FUTURES_SHORT prioritizes short-bounce candidates
    if regime_mode == "FUTURES_SHORT":
        futures_candidates = sorted(
            [c for c in candidates if c["vol_usdt"] >= futures_min_vol],
            key=lambda x: x["futures_short_score"], reverse=True
        )
    elif regime_mode == "RANGE_SCALP":
        futures_candidates = sorted(
            [c for c in candidates if c["vol_usdt"] >= futures_min_vol],
            key=lambda x: x["range_score"], reverse=True
        )
    else:  # SPOT_BULL or unknown — futures long candidates by momentum
        futures_candidates = sorted(
            [c for c in candidates if c["vol_usdt"] >= futures_min_vol],
            key=lambda x: x["futures_score"], reverse=True
        )
    futures_pairs = [c["symbol"] for c in futures_candidates[:max_futures]]

    top_bearish = sorted(
        [c for c in candidates if c["vol_usdt"] >= futures_min_vol],
        key=lambda x: x["futures_short_score"], reverse=True
    )[:5]
    top_bullish = sorted(candidates, key=lambda x: x["score"], reverse=True)[:5]

    fetched_at = datetime.now(timezone.utc).isoformat()
    result = {
        "spot":    spot_pairs,
        "futures": futures_pairs,
        "all":     [c["symbol"] for c in candidates],
        "_meta": {
            "regime_mode": regime_mode,
            "profile_label": (str(profile_label).strip().upper() if profile_label else None),
            "fetched_at": fetched_at,
            "fetched_at_ts": now,
            "total_candidates": len(candidates),
            "spot_count": len(spot_pairs),
            "futures_count": len(futures_pairs),
            "top_bearish": [
                {"symbol": c["symbol"], "chg_pct": c["chg_pct"], "score": c["futures_short_score"]}
                for c in top_bearish
            ],
            "top_bullish": [
                {"symbol": c["symbol"], "chg_pct": c["chg_pct"], "score": c["score"]}
                for c in top_bullish
            ],
        }
    }

    # ── Persist decision for auditability ─────────────────────────────────────
    parameters = {
        "spot_min_vol": spot_min_vol,
        "futures_min_vol": futures_min_vol,
        "max_spot": max_spot,
        "max_futures": max_futures,
        "cache_seconds": cache_seconds,
        "profile_label": (str(profile_label).strip().upper() if profile_label else None),
        "snapshot_max_age_seconds": _snapshot_max_age_seconds,
    }
    rejected_reason = None
    if regime_mode in ("PRESERVATION", "FUTURES_SHORT") and not spot_pairs:
        rejected_reason = "Bear regime: SPOT longs disabled (narrative-only)"
    _persist_screener_decision(db_client, regime_mode, result, parameters, rejected_reason)
    _persist_screener_snapshot(db_client, result)

    # ── Update cache ──────────────────────────────────────────────────────────
    _screener_cache["data"] = result
    _screener_cache["ts"]   = now

    print(
        f"[Screener] {len(candidates)} pairs scanned → "
        f"spot={len(spot_pairs)}, futures={len(futures_pairs)} "
        f"(regime={regime_mode}, top_bearish={top_bearish[0]['symbol'] if top_bearish else '-'})"
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
