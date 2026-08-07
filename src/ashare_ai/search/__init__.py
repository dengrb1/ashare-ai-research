"""Interactive financial search isolated from frozen research snapshots."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "FinancialSearchResponse",
    "FinancialSearchService",
    "FinancialSearchStatus",
    "get_financial_search_service",
]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(name)
    return getattr(import_module("ashare_ai.search.service"), name)
