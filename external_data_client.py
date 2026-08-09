"""External market data adapters with cache and fail-open semantics.

C9 phase-1 integration:
- Optional third-party enrichments (CoinGlass / Usenami style feeds).
- 5s request timeout.
- 15m per-symbol cache TTL.
- Non-blocking fallback: failures never stop the trading cycle.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, Optional
from urllib import error as urlerror
from urllib import parse, request

logger = logging.getLogger(__name__)


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class ExternalDataClient:
    def __init__(self, settings=None, timeout_seconds: float = 5.0, cache_ttl_seconds: int = 900):
        self.settings = settings
        self.timeout_seconds = float(timeout_seconds)
        self.cache_ttl_seconds = int(cache_ttl_seconds)
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _cache_get(self, symbol: str) -> Optional[Dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            row = self._cache.get(symbol)
            if not row:
                return None
            if now >= row.get("expires_at", 0.0):
                self._cache.pop(symbol, None)
                return None
            return dict(row.get("data") or {})

    def _cache_set(self, symbol: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            self._cache[symbol] = {
                "expires_at": time.monotonic() + self.cache_ttl_seconds,
                "data": dict(payload),
            }

    def _fetch_json(self, url: str, headers: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
        req = request.Request(url, headers=headers or {}, method="GET")
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else None
        except (urlerror.URLError, TimeoutError, ValueError) as exc:
            logger.debug("External data request failed (%s): %s", url, exc)
            return None

    def _fetch_coinglass(self, symbol: str) -> Dict[str, Any]:
        base = str(getattr(self.settings, "coinglass_api_url", "") or "").strip()
        if not base:
            return {}
        token = str(getattr(self.settings, "coinglass_api_key", "") or "").strip()
        params = parse.urlencode({"symbol": symbol})
        url = f"{base.rstrip('/')}/market?{params}"
        headers = {"Accept": "application/json"}
        if token:
            headers["CG-API-KEY"] = token
        payload = self._fetch_json(url, headers=headers) or {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not isinstance(data, dict):
            return {}
        return {
            "funding_rate_annualized": _safe_float(data.get("funding_rate_annualized") or data.get("fundingAnnualized")),
            "oi_change_1h_pct": _safe_float(data.get("oi_change_1h_pct") or data.get("openInterestChange1h")),
            "liquidation_imbalance_1h": _safe_float(data.get("liquidation_imbalance_1h") or data.get("liqImbalance1h")),
        }

    def _fetch_usenami(self, symbol: str) -> Dict[str, Any]:
        base = str(getattr(self.settings, "usenami_api_url", "") or "").strip()
        if not base:
            return {}
        token = str(getattr(self.settings, "usenami_api_key", "") or "").strip()
        params = parse.urlencode({"symbol": symbol})
        url = f"{base.rstrip('/')}/signals?{params}"
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = self._fetch_json(url, headers=headers) or {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not isinstance(data, dict):
            return {}
        return {
            "coinbase_premium_pct": _safe_float(data.get("coinbase_premium_pct") or data.get("coinbasePremiumPct")),
            "sentiment_score_external": _safe_float(data.get("sentiment_score") or data.get("sentimentScore")),
        }

    def get_market_features(self, symbol: str) -> Dict[str, Any]:
        cached = self._cache_get(symbol)
        if cached is not None:
            return cached

        status: Dict[str, str] = {"coinglass": "unavailable", "usenami": "unavailable"}
        out: Dict[str, Any] = {}

        try:
            cg = self._fetch_coinglass(symbol)
            if cg:
                status["coinglass"] = "ok"
                out.update({k: v for k, v in cg.items() if v is not None})
        except Exception as exc:
            status["coinglass"] = "error"
            logger.debug("CoinGlass adapter error for %s: %s", symbol, exc)

        try:
            us = self._fetch_usenami(symbol)
            if us:
                status["usenami"] = "ok"
                out.update({k: v for k, v in us.items() if v is not None})
        except Exception as exc:
            status["usenami"] = "error"
            logger.debug("Usenami adapter error for %s: %s", symbol, exc)

        out["external_data_available"] = any(v == "ok" for v in status.values())
        out["external_data_sources"] = status
        self._cache_set(symbol, out)
        return out


_external_client_singleton: Optional[ExternalDataClient] = None
_external_client_lock = threading.Lock()


def get_external_data_client(settings=None) -> ExternalDataClient:
    global _external_client_singleton
    with _external_client_lock:
        if _external_client_singleton is None:
            _external_client_singleton = ExternalDataClient(settings=settings)
        elif settings is not None:
            _external_client_singleton.settings = settings
        return _external_client_singleton
