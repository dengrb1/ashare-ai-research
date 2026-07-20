from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ashare_ai.agents.model_settings import ModelConfigurationService
from ashare_ai.agents.openai_compatible import OpenAICompatibleStructuredLLMClient
from ashare_ai.core.config import get_settings
from ashare_ai.core.hashing import canonical_json, stable_hash
from ashare_ai.market.service import get_market_data_service, normalize_symbol
from ashare_ai.portfolio.user_assets import UserAssetService
from ashare_ai.search.searxng import SearXNGSearchClient
from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.models import (
    AIChatMessage,
    AIChatThread,
    AIResponseCacheRow,
    JobRun,
    ScoreRow,
)

CHAT_PROMPT_VERSION = "stock-chat-v1"
_MENTION = re.compile(r"@([0-9]{6}(?:\.(?:SH|SZ|BJ))?|[\u4e00-\u9fffA-Za-z]{2,20})", re.I)


def allow_chat_request(user_id: str) -> bool:
    settings = get_settings()
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    minute = int(datetime.now(UTC).timestamp() // 60)
    key = f"ashare:rate:ai-chat:{user_id}:{minute}"
    try:
        count = int(cast(Any, client.incr(key)))
        if count == 1:
            client.expire(key, 90)
        return count <= settings.ai_chat_rate_limit_per_minute
    except redis.RedisError:
        return False


def _symbol_from_code(value: str) -> str | None:
    raw = value.upper()
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", raw):
        return raw
    if not re.fullmatch(r"\d{6}", raw):
        return None
    suffix = "SH" if raw.startswith("6") else "BJ" if raw.startswith(("4", "8")) else "SZ"
    return f"{raw}.{suffix}"


def resolve_mentions(content: str, assets: dict[str, Any]) -> list[str]:
    aliases: dict[str, str] = {}
    for position in assets.get("positions", []):
        symbol = str(position.get("symbol") or "").upper()
        name = str(position.get("name") or "").strip().casefold()
        if symbol:
            aliases[symbol.casefold()] = symbol
        if symbol and name:
            aliases[name] = symbol
    for symbol in assets.get("watchlist", []):
        aliases[str(symbol).casefold()] = str(symbol).upper()
    symbols: list[str] = []
    for match in _MENTION.finditer(content):
        raw = match.group(1).strip()
        resolved_symbol = _symbol_from_code(raw) or aliases.get(raw.casefold())
        if resolved_symbol and resolved_symbol not in symbols:
            symbols.append(normalize_symbol(resolved_symbol))
    return symbols[:5]


def _system_context(
    user_id: str, symbols: list[str]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with SessionLocal() as session:
        assets = UserAssetService(session).get(user_id)
        positions = {
            str(item.get("symbol")): item
            for item in assets.get("positions", [])
            if item.get("symbol") in symbols
        }
        scores: dict[str, Any] = {}
        for symbol in symbols:
            row = session.scalar(
                select(ScoreRow)
                .join(JobRun, JobRun.run_id == ScoreRow.run_id)
                .where(
                    ScoreRow.symbol == symbol,
                    JobRun.user_id == user_id,
                    JobRun.status.in_(("SUCCEEDED", "FUSED")),
                )
                .order_by(JobRun.trading_date.desc(), JobRun.completed_at.desc())
                .limit(1)
            )
            if row is not None:
                scores[symbol] = {
                    "trading_date": row.trading_date.isoformat(),
                    "decision_at": row.decision_at.isoformat(),
                    "total_score": row.total_score,
                    "fundamental_score": row.fundamental_score,
                    "technical_score": row.technical_score,
                    "sentiment_score": row.sentiment_score,
                    "event_risk_multiplier": row.event_risk_multiplier,
                    "formula_version": row.formula_version,
                }
    quotes: dict[str, Any] = {}
    bars: dict[str, Any] = {}
    if symbols:
        market = get_market_data_service()
        for item in market.quotes(symbols):
            quotes[item["symbol"]] = item
        for symbol in symbols:
            try:
                payload = market.klines(symbol, "day", limit=30)
                bars[symbol] = payload.get("bars", [])[-30:]
            except RuntimeError:
                bars[symbol] = []
    sources = [
        {
            "source": "system",
            "symbol": symbol,
            "uri": f"/api/v1/market/quotes?symbols={symbol}",
            "available_at": (quotes.get(symbol, {}).get("status", {}) or {}).get("collected_at"),
        }
        for symbol in symbols
    ]
    return {
        "symbols": symbols,
        "positions": positions,
        "quotes": quotes,
        "daily_bars": bars,
        "latest_formal_scores": scores,
    }, sources


async def stream_chat_response(
    *,
    user_id: str,
    thread_id: str,
    content: str,
    model: str,
    reasoning_effort: str,
    web_search: bool,
) -> AsyncIterator[dict[str, Any]]:
    now = datetime.now(UTC)
    with SessionLocal() as session:
        thread = session.scalar(
            select(AIChatThread).where(
                AIChatThread.thread_id == thread_id, AIChatThread.user_id == user_id
            )
        )
        if thread is None:
            raise KeyError(thread_id)
        assets = UserAssetService(session).get(user_id)
        symbols = resolve_mentions(content, assets)
        runtime = ModelConfigurationService().resolve(session)
        if runtime is None:
            raise RuntimeError("AI model is not configured")
        allowed_models = {runtime.search_model, runtime.research_model}
        if model not in allowed_models:
            raise ValueError("selected model is not allowed")
        user_message = AIChatMessage(
            thread_id=thread_id,
            role="user",
            content=content,
            mentioned_symbols=symbols,
            sources=[],
            cache_hit=False,
            created_at=now,
        )
        session.add(user_message)
        thread.updated_at = now
        if thread.title == "新对话":
            thread.title = content.strip()[:48]
        session.commit()
        history = list(
            session.scalars(
                select(AIChatMessage)
                .where(AIChatMessage.thread_id == thread_id)
                .order_by(AIChatMessage.created_at.desc())
                .limit(20)
            ).all()
        )[::-1]

    context, sources = await asyncio.to_thread(_system_context, user_id, symbols)
    web_sources: list[dict[str, Any]] = []
    if web_search:
        query = " ".join(symbols) + " A股 最新消息 " + content.replace("@", " ")[:120]
        try:
            web_sources = await asyncio.to_thread(SearXNGSearchClient().search, query)
        except Exception:
            web_sources = []
    sources.extend(
        {"source": "searxng", "title": item["title"], "uri": item["url"], "engine": item["engine"]}
        for item in web_sources
    )
    context["web_search"] = web_sources
    context_sha = stable_hash(context)
    system_prompt = (
        "你是A股研究对话助手。只能使用下方系统上下文和联网检索摘要回答；明确区分实时行情、"
        "历史研究和外部网页。不得声称执行真实交易，不得泄漏内部路径、凭据或审计载荷。"
        "涉及买卖时给出条件、风险和数据时点，不承诺收益。引用网页时用[来源标题](URL)。\n"
        + canonical_json(context).decode("utf-8")
    )
    messages = (
        {"role": "system", "content": system_prompt},
        *tuple({"role": item.role, "content": item.content} for item in history),
    )
    request_sha = stable_hash(
        {
            "prompt_version": CHAT_PROMPT_VERSION,
            "model_config": runtime.config_sha256,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "messages": messages,
        }
    )
    with SessionLocal() as session:
        cached = session.scalar(
            select(AIResponseCacheRow).where(
                AIResponseCacheRow.user_id == user_id,
                AIResponseCacheRow.purpose == "CHAT",
                AIResponseCacheRow.request_sha256 == request_sha,
                AIResponseCacheRow.expires_at > now,
            )
        )
        if cached is not None:
            cached.hit_count += 1
            cached.last_hit_at = now
            session.commit()
            text = str(cached.response.get("content") or "")
            yield {"type": "meta", "cache_hit": True, "sources": sources, "symbols": symbols}
            for index in range(0, len(text), 80):
                yield {"type": "delta", "delta": text[index : index + 80]}
            _persist_assistant(
                thread_id=thread_id,
                content=text,
                symbols=symbols,
                model=model,
                reasoning_effort=reasoning_effort,
                sources=sources,
                context_sha=context_sha,
                response_sha=cached.response_sha256,
                cache_hit=True,
                input_tokens=0,
                output_tokens=0,
            )
            yield {"type": "done", "cache_hit": True}
            return

    yield {"type": "meta", "cache_hit": False, "sources": sources, "symbols": symbols}
    client = OpenAICompatibleStructuredLLMClient(
        base_url=runtime.base_url,
        api_key=runtime.api_key,
        model=model,
        reasoning_effort=reasoning_effort,
        timeout_seconds=runtime.timeout_seconds,
        max_retries=1,
    )
    chunks: list[str] = []
    input_tokens = output_tokens = 0
    actual_model = model
    async for event in client.stream_text(messages=messages, idempotency_key=request_sha):
        if event["type"] == "delta":
            chunks.append(event["delta"])
            yield event
        elif event["type"] == "completed":
            input_tokens = int(event.get("input_tokens", 0))
            output_tokens = int(event.get("output_tokens", 0))
            actual_model = str(event.get("model") or model)
    text = "".join(chunks).strip()
    response_sha = stable_hash({"content": text})
    _persist_assistant(
        thread_id=thread_id,
        content=text,
        symbols=symbols,
        model=actual_model,
        reasoning_effort=reasoning_effort,
        sources=sources,
        context_sha=context_sha,
        response_sha=response_sha,
        cache_hit=False,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    with SessionLocal() as session:
        session.add(
            AIResponseCacheRow(
                user_id=user_id,
                purpose="CHAT",
                request_sha256=request_sha,
                response_sha256=response_sha,
                model_name=actual_model,
                reasoning_effort=reasoning_effort,
                prompt_version=CHAT_PROMPT_VERSION,
                response={"content": text},
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                created_at=now,
                expires_at=now + timedelta(hours=24),
            )
        )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
    yield {
        "type": "done",
        "cache_hit": False,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _persist_assistant(**values: Any) -> None:
    with SessionLocal() as session:
        session.add(
            AIChatMessage(
                thread_id=values["thread_id"],
                role="assistant",
                content=values["content"],
                mentioned_symbols=values["symbols"],
                model_name=values["model"],
                reasoning_effort=values["reasoning_effort"],
                sources=values["sources"],
                context_sha256=values["context_sha"],
                response_sha256=values["response_sha"],
                cache_hit=values["cache_hit"],
                input_tokens=values["input_tokens"],
                output_tokens=values["output_tokens"],
                created_at=datetime.now(UTC),
            )
        )
        thread = session.get(AIChatThread, values["thread_id"])
        if thread is not None:
            thread.updated_at = datetime.now(UTC)
        session.commit()
