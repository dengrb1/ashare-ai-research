"""News feature extraction for research workflows."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from ashare_ai.core.config import Settings, get_settings
from ashare_ai.market.news_bridge_client import get_news_bridge_client

logger = logging.getLogger(__name__)


class NewsFeatureExtractor:
    """Extract news-based features for stock research.

    Fetches recent news articles from News Bridge and computes
    aggregated features such as article count, sentiment indicators,
    and keyword presence.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = (
            get_news_bridge_client(self.settings)
            if self.settings.news_bridge_enabled
            else None
        )

    def close(self) -> None:
        """Close the News Bridge client."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def extract_news_features(
        self,
        symbol: str,
        *,
        lookback_days: int = 7,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Extract news features for a symbol.

        Args:
            symbol: Stock symbol (e.g., "000001.SZ")
            lookback_days: Number of days to look back for news
            limit: Maximum number of articles to fetch

        Returns:
            Dictionary with news features:
            - news_count: Total number of articles
            - has_recent_news: Whether news exists in lookback window
            - latest_article_date: ISO timestamp of most recent article
            - sources: List of news sources mentioned
            - titles: List of article titles (for downstream NLP)
            - error: Error message if news fetch failed (optional)
        """
        if self._client is None:
            return {
                "news_count": 0,
                "has_recent_news": False,
                "latest_article_date": None,
                "sources": [],
                "titles": [],
                "error": "News Bridge is disabled",
            }

        try:
            articles = self._client.get_news(symbol=symbol, limit=limit)

            # Filter articles within the lookback window
            cutoff = datetime.now().astimezone() - timedelta(days=lookback_days)
            recent_articles = []
            for article in articles:
                if article.get("published_at"):
                    try:
                        published = datetime.fromisoformat(article["published_at"])
                        if published >= cutoff:
                            recent_articles.append(article)
                    except (ValueError, TypeError):
                        continue

            # Extract features
            sources = list({a.get("source", "") for a in recent_articles if a.get("source")})
            titles = [a.get("title", "") for a in recent_articles if a.get("title")]
            latest_date = None
            if recent_articles:
                try:
                    latest_date = max(
                        datetime.fromisoformat(a["published_at"])
                        for a in recent_articles
                        if a.get("published_at")
                    ).isoformat()
                except (ValueError, TypeError):
                    pass

            return {
                "news_count": len(recent_articles),
                "has_recent_news": len(recent_articles) > 0,
                "latest_article_date": latest_date,
                "sources": sources,
                "titles": titles,
            }
        except Exception as exc:
            logger.warning("News feature extraction failed for %s: %s", symbol, exc)
            return {
                "news_count": 0,
                "has_recent_news": False,
                "latest_article_date": None,
                "sources": [],
                "titles": [],
                "error": str(exc),
            }


def get_news_feature_extractor(settings: Settings | None = None) -> NewsFeatureExtractor:
    """Factory function for NewsFeatureExtractor."""
    return NewsFeatureExtractor(settings=settings)
