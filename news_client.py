import feedparser
import urllib.request
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

NEWS_FEEDS = {
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "reuters_crypto": "https://www.reutersagency.com/feed/?best-regions=us&best-topics=cryptocurrency&post_type=best",  # fallback to Reuters crypto feed if available
}


# ── Module-level TTL caches (shared across all NewsClient instances) ──────────
_FNG_CACHE: Dict = {}          # {"value": int, "label": str, "fetched_at": float}
_FNG_TTL = 3600.0              # 1h — index updates once daily, no need to poll more
_TRENDING_CACHE: Dict = {}     # {"symbols": List[str], "fetched_at": float}
_TRENDING_TTL = 3600.0         # 1h — CoinGecko trending updates hourly
_SOCIAL_CACHE: Dict = {}       # {symbol: {"score": float, "fetched_at": float}}
_SOCIAL_TTL = 3600.0           # 1h — social stats don't change faster

# ── CryptoCompare Social — DISABLED until API key is configured ───────────────
# To enable: set CRYPTOCOMPARE_API_KEY in .env and flip this flag to True
CRYPTOCOMPARE_ENABLED: bool = False

# Baseline social activity per symbol (avg Reddit posts/24h)
# Used to detect abnormal spikes (score > baseline * 3 = significant)
_SOCIAL_BASELINES: Dict[str, int] = {
    "DOGEUSDT": 80, "PEPEUSDT": 30, "WIFUSDT": 15,
    "FETUSDT":  20, "TAOUSDT":  15, "RENDERUSDT": 12,
    "NEARUSDT": 25, "WLDUSDT":  18,
}

# CoinGecko coin ID map (needed for CryptoCompare social endpoint)
_COINGECKO_IDS: Dict[str, str] = {
    "DOGEUSDT":   "dogecoin",      "PEPEUSDT":   "pepe",
    "WIFUSDT":    "dogwifcoin",    "FETUSDT":    "fetch-ai",
    "TAOUSDT":    "bittensor",     "RENDERUSDT": "render-token",
    "NEARUSDT":   "near",          "WLDUSDT":    "worldcoin",
}

# ── Sentiment keyword sets (Fase 3) ──────────────────────────────────────────
_POSITIVE_WORDS = {
    "rally", "surge", "breakout", "adoption", "partnership", "launch",
    "milestone", "bullish", "all-time high", "ath", "listing", "upgrade",
    "approval", "soars", "jumps", "gains", "rises", "recovery", "outperform",
    "integration", "mainnet", "staking", "institutional", "accumulation",
}
_NEGATIVE_WORDS = {
    "crash", "hack", "ban", "lawsuit", "fraud", "scam", "bearish", "plunge",
    "dump", "rug", "exploit", "sanction", "investigation", "delist", "falls",
    "drops", "slumps", "loses", "sell-off", "selloff", "liquidation", "fine",
    "penalty", "breach", "vulnerability", "ponzi", "manipulation",
}


class NewsClient:
    def __init__(self) -> None:
        self.sources = NEWS_FEEDS

    # ── Fear & Greed Index ────────────────────────────────────────────────────

    def get_fear_greed(self) -> Dict:
        """
        Fetch the Crypto Fear & Greed Index from Alternative.me.
        Returns dict with 'value' (0-100), 'label', and 'fetched_at'.
        Cached for 1h — index only updates once per day.
        Value interpretation:
          0-24  = Extreme Fear  → buying opportunity, relax thresholds
          25-44 = Fear          → cautious buying
          45-55 = Neutral
          56-74 = Greed         → normal operation
          75-100 = Extreme Greed → tighten thresholds, avoid chasing
        """
        now = time.monotonic()
        if _FNG_CACHE and (now - _FNG_CACHE.get("fetched_at", 0)) < _FNG_TTL:
            return _FNG_CACHE.copy()
        try:
            req = urllib.request.Request(
                "https://api.alternative.me/fng/?limit=1",
                headers={"User-Agent": "crypto-agent/1.0"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            entry = data["data"][0]
            result = {
                "value": int(entry["value"]),
                "label": entry["value_classification"],
                "fetched_at": now,
            }
            _FNG_CACHE.update(result)
            return result
        except Exception as e:
            return {"value": 50, "label": "Neutral", "fetched_at": now, "error": str(e)}

    def get_fear_greed_modifier(self) -> float:
        """
        Return a probability threshold modifier based on the Fear & Greed Index.
        Negative = relax thresholds (easier to enter), Positive = tighten (harder to enter).
          Extreme Fear  (<25) → -0.07  (relax: great buying opportunity)
          Fear         (<45)  → -0.03  (slightly relax)
          Neutral      (<56)  →  0.00  (no change)
          Greed        (<75)  → +0.02  (slightly tighten)
          Extreme Greed (>=75)→ +0.07  (tighten hard: don't chase the top)
        """
        fng = self.get_fear_greed()
        v = fng.get("value", 50)
        if v < 25:
            return -0.07
        if v < 45:
            return -0.03
        if v < 56:
            return 0.00
        if v < 75:
            return +0.02
        return +0.07

    # ── CoinGecko Trending ────────────────────────────────────────────────────

    def get_trending_symbols(self) -> List[str]:
        """
        Fetch the top-7 trending tokens from CoinGecko (no API key required).
        Returns a list of uppercase ticker symbols like ['BTC', 'FET', 'TAO'].
        Cached for 1h.
        """
        now = time.monotonic()
        if _TRENDING_CACHE and (now - _TRENDING_CACHE.get("fetched_at", 0)) < _TRENDING_TTL:
            return _TRENDING_CACHE.get("symbols", [])
        try:
            req = urllib.request.Request(
                "https://api.coingecko.com/api/v3/search/trending",
                headers={"User-Agent": "crypto-agent/1.0"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            symbols = [
                coin["item"]["symbol"].upper()
                for coin in data.get("coins", [])
            ]
            _TRENDING_CACHE["symbols"] = symbols
            _TRENDING_CACHE["fetched_at"] = now
            return symbols
        except Exception:
            return _TRENDING_CACHE.get("symbols", [])

    # ── CryptoCompare Social Score (Fase 2 — DESACTIVADO) ────────────────────

    def get_social_score(self, symbol: str) -> Dict:
        """
        Fetch social stats from CryptoCompare for the given symbol.
        Returns dict with 'reddit_posts_24h', 'twitter_posts_24h', 'score'.
        DISABLED (CRYPTOCOMPARE_ENABLED=False) — returns {"score": 0, "disabled": True}.
        Enable by: set CRYPTOCOMPARE_ENABLED=True and CRYPTOCOMPARE_API_KEY in env.
        """
        if not CRYPTOCOMPARE_ENABLED:
            return {"score": 0, "disabled": True}
        coin_id = _COINGECKO_IDS.get(symbol.upper())
        if not coin_id:
            return {"score": 0, "unknown": True}
        now = time.monotonic()
        cached = _SOCIAL_CACHE.get(symbol, {})
        if cached and (now - cached.get("fetched_at", 0)) < _SOCIAL_TTL:
            return cached.copy()
        api_key = os.environ.get("CRYPTOCOMPARE_API_KEY", "")
        if not api_key:
            return {"score": 0, "no_key": True}
        try:
            url = f"https://min-api.cryptocompare.com/data/social/coin/latest?coinId={coin_id}&api_key={api_key}"
            req = urllib.request.Request(url, headers={"User-Agent": "crypto-agent/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            reddit = data.get("Data", {}).get("Reddit", {})
            twitter = data.get("Data", {}).get("Twitter", {})
            reddit_posts = int(reddit.get("posts_per_day", 0))
            twitter_posts = int(twitter.get("statuses_count", 0))
            score = float(reddit_posts + twitter_posts * 0.5)
            result = {
                "reddit_posts_24h": reddit_posts,
                "twitter_posts_24h": twitter_posts,
                "score": score,
                "fetched_at": now,
            }
            _SOCIAL_CACHE[symbol] = result
            return result
        except Exception as e:
            return {"score": 0, "error": str(e)}

    def is_social_trending(self, symbol: str) -> bool:
        """
        Return True if social activity for this symbol is 3x above its baseline.
        Always returns False if CRYPTOCOMPARE_ENABLED=False.
        """
        if not CRYPTOCOMPARE_ENABLED:
            return False
        data = self.get_social_score(symbol)
        score = data.get("score", 0)
        baseline = _SOCIAL_BASELINES.get(symbol.upper(), 50)
        return score > baseline * 3

    # ── News Sentiment Analysis (Fase 3) ──────────────────────────────────

    def analyze_sentiment(self, text: str) -> str:
        """
        Simple keyword-based sentiment: 'positive', 'negative', or 'neutral'.
        Counts positive vs negative word matches in lowercased text.
        """
        lower = text.lower()
        pos = sum(1 for w in _POSITIVE_WORDS if w in lower)
        neg = sum(1 for w in _NEGATIVE_WORDS if w in lower)
        if pos > neg:
            return "positive"
        if neg > pos:
            return "negative"
        return "neutral"

    def is_trending(self, symbol: str) -> bool:
        """
        Return True if the base asset of *symbol* is currently trending on CoinGecko.
        E.g. 'FETUSDT' → checks if 'FET' is in the trending list.
        """
        base = symbol.upper().replace("USDT", "").replace("BTC", "").replace("ETH", "")
        return base in self.get_trending_symbols()

    def fetch_feed(self, url: str) -> List[Dict[str, str]]:
        feed = feedparser.parse(url)
        articles = []
        for entry in feed.entries:
            published = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                published = datetime.fromtimestamp(
                    int(datetime(*entry.published_parsed[:6]).timestamp()), tz=timezone.utc
                )
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            articles.append(
                {
                    "title": title,
                    "link": entry.get("link", ""),
                    "summary": summary,
                    "published": published.isoformat() if published else None,
                    "source": feed.feed.get("title", url),
                    "sentiment": self.analyze_sentiment(f"{title} {summary}"),
                }
            )
        return articles

    def get_recent_news(self, symbols: List[str], since_hours: int = 24) -> List[Dict[str, str]]:
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=since_hours)
        keywords = [symbol.upper() for symbol in symbols]
        articles = []

        for source_name, url in self.sources.items():
            try:
                feed_articles = self.fetch_feed(url)
            except Exception:
                continue
            for article in feed_articles:
                published = article.get("published")
                if not published:
                    continue
                published_dt = datetime.fromisoformat(published)
                if published_dt < since:
                    continue
                text = f"{article['title']} {article['summary']}".upper()
                if any(keyword in text for keyword in keywords):
                    articles.append(article)

        return articles
