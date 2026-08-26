"""Tests for news feature extraction."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest

from ashare_ai.core.config import Settings
from ashare_ai.features.news_features import NewsFeatureExtractor


@pytest.fixture
def mock_settings() -> Settings:
    """Create test settings with News Bridge enabled."""
    settings = Settings()
    settings.news_bridge_enabled = True
    settings.news_bridge_url = "http://localhost:8082"
    return settings


@pytest.fixture
def mock_news_articles() -> list[dict]:
    """Create mock news articles."""
    now = datetime.now().astimezone()
    return [
        {
            "title": "公司发布年度报告",
            "summary": "业绩增长显著",
            "content": "详细内容...",
            "source": "Eastmoney",
            "published_at": now.isoformat(),
            "url": "https://example.com/1",
            "symbols": ["000001.SZ"],
        },
        {
            "title": "分析师上调评级",
            "summary": "目标价上调",
            "content": "详细分析...",
            "source": "Securities Daily",
            "published_at": (now - timedelta(days=2)).isoformat(),
            "url": "https://example.com/2",
            "symbols": ["000001.SZ"],
        },
        {
            "title": "行业政策出台",
            "summary": "利好相关企业",
            "content": "政策详情...",
            "source": "Eastmoney",
            "published_at": (now - timedelta(days=10)).isoformat(),
            "url": "https://example.com/3",
            "symbols": ["000001.SZ"],
        },
    ]


class TestNewsFeatureExtractor:
    def test_extract_recent_news(
        self, mock_settings: Settings, mock_news_articles: list[dict]
    ) -> None:
        with patch("ashare_ai.features.news_features.get_news_bridge_client") as mock_get:
            mock_client = Mock()
            mock_client.get_news.return_value = mock_news_articles
            mock_get.return_value = mock_client

            extractor = NewsFeatureExtractor(mock_settings)
            features = extractor.extract_news_features("000001.SZ", lookback_days=7)

            assert features["news_count"] == 2  # Only 2 within 7 days
            assert features["has_recent_news"] is True
            assert features["latest_article_date"] is not None
            assert len(features["sources"]) == 2
            assert "Eastmoney" in features["sources"]
            assert "Securities Daily" in features["sources"]
            assert len(features["titles"]) == 2
            assert "error" not in features

    def test_no_recent_news(self, mock_settings: Settings) -> None:
        old_article = {
            "title": "旧闻",
            "published_at": (datetime.now().astimezone() - timedelta(days=20)).isoformat(),
            "source": "Test Source",
            "symbols": ["000001.SZ"],
        }

        with patch("ashare_ai.features.news_features.get_news_bridge_client") as mock_get:
            mock_client = Mock()
            mock_client.get_news.return_value = [old_article]
            mock_get.return_value = mock_client

            extractor = NewsFeatureExtractor(mock_settings)
            features = extractor.extract_news_features("000001.SZ", lookback_days=7)

            assert features["news_count"] == 0
            assert features["has_recent_news"] is False
            assert features["latest_article_date"] is None

    def test_news_bridge_disabled(self) -> None:
        settings = Settings()
        settings.news_bridge_enabled = False

        extractor = NewsFeatureExtractor(settings)
        features = extractor.extract_news_features("000001.SZ")

        assert features["news_count"] == 0
        assert features["has_recent_news"] is False
        assert features["error"] == "News Bridge is disabled"

    def test_news_fetch_exception(self, mock_settings: Settings) -> None:
        with patch("ashare_ai.features.news_features.get_news_bridge_client") as mock_get:
            mock_client = Mock()
            mock_client.get_news.side_effect = ConnectionError("Service unavailable")
            mock_get.return_value = mock_client

            extractor = NewsFeatureExtractor(mock_settings)
            features = extractor.extract_news_features("000001.SZ")

            assert features["news_count"] == 0
            assert features["has_recent_news"] is False
            assert "error" in features
            assert "Service unavailable" in features["error"]

    def test_invalid_published_dates(
        self, mock_settings: Settings, mock_news_articles: list[dict]
    ) -> None:
        # Add articles with invalid dates
        bad_articles = mock_news_articles[:1] + [
            {"title": "Invalid date", "published_at": "not-a-date", "source": "Test"},
            {"title": "No date", "source": "Test"},
        ]

        with patch("ashare_ai.features.news_features.get_news_bridge_client") as mock_get:
            mock_client = Mock()
            mock_client.get_news.return_value = bad_articles
            mock_get.return_value = mock_client

            extractor = NewsFeatureExtractor(mock_settings)
            features = extractor.extract_news_features("000001.SZ")

            # Should only count the valid article
            assert features["news_count"] == 1
            assert features["has_recent_news"] is True
