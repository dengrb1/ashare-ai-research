"""Bounded interactive Web search, isolated from frozen research snapshots."""

from ashare_ai.search.searxng import SearXNGSearchClient
from ashare_ai.search.web import WebSearchResult, WebSearchService, get_web_search_service

__all__ = ["SearXNGSearchClient", "WebSearchResult", "WebSearchService", "get_web_search_service"]
