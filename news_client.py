import feedparser
from datetime import datetime, timedelta, timezone
from typing import Dict, List

NEWS_FEEDS = {
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "reuters_crypto": "https://www.reutersagency.com/feed/?best-regions=us&best-topics=cryptocurrency&post_type=best",  # fallback to Reuters crypto feed if available
}


class NewsClient:
    def __init__(self) -> None:
        self.sources = NEWS_FEEDS

    def fetch_feed(self, url: str) -> List[Dict[str, str]]:
        feed = feedparser.parse(url)
        articles = []
        for entry in feed.entries:
            published = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                published = datetime.fromtimestamp(
                    int(datetime(*entry.published_parsed[:6]).timestamp()), tz=timezone.utc
                )
            articles.append(
                {
                    "title": entry.get("title", ""),
                    "link": entry.get("link", ""),
                    "summary": entry.get("summary", ""),
                    "published": published.isoformat() if published else None,
                    "source": feed.feed.get("title", url),
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
