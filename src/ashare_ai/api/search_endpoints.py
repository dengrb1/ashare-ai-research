"""Authenticated SearXNG endpoints for interactive research context."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ashare_ai.api.dependencies import get_auth_context
from ashare_ai.api.schemas import WebSearchRequest, WebSearchResponse
from ashare_ai.agents.chat import allow_chat_request
from ashare_ai.api.auth import AuthContext
from ashare_ai.search import get_web_search_service

router = APIRouter(prefix="/api/v1/search", tags=["search"])


@router.post("/web", response_model=WebSearchResponse)
def web_search(payload: WebSearchRequest, context: AuthContext = Depends(get_auth_context)) -> WebSearchResponse:
    if not allow_chat_request(context.user.user_id):
        raise HTTPException(status_code=429, detail="search rate limit exceeded", headers={"Retry-After": "60"})
    result = get_web_search_service().search(payload.query)
    if payload.max_results < len(result.items):
        result.items[:] = result.items[: payload.max_results]
    return WebSearchResponse(items=result.items, status=result.status, cache_hit=result.cache_hit)


@router.get("/status")
def search_status(_: AuthContext = Depends(get_auth_context)) -> dict[str, object]:
    service = get_web_search_service()
    return {"state": "AVAILABLE" if service.client.available() else "UNAVAILABLE", "source": "searxng"}
