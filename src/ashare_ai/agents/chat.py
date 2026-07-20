from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

import redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ashare_ai.agents.attachments import (
    MAX_IMAGES_PER_MESSAGE,
    MAX_MESSAGE_IMAGE_BYTES,
    AttachmentService,
)
from ashare_ai.agents.chat_threads import automatic_group
from ashare_ai.agents.model_settings import ModelConfigurationService
from ashare_ai.agents.openai_compatible import (
    OpenAICompatibleError,
    OpenAICompatibleStructuredLLMClient,
)
from ashare_ai.core.config import get_settings
from ashare_ai.core.hashing import canonical_json, sha256_bytes, stable_hash
from ashare_ai.market.service import get_market_data_service, normalize_symbol
from ashare_ai.portfolio.user_assets import UserAssetService
from ashare_ai.search.searxng import SearXNGSearchClient
from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.models import (
    AIChatAttachment,
    AIChatMessage,
    AIChatThread,
    AIResponseCacheRow,
    JobRun,
    ScoreRow,
    SecurityMaster,
)

CHAT_PROMPT_VERSION = "stock-chat-v2"
_MENTION = re.compile(r"@([0-9]{6}(?:\.(?:SH|SZ|BJ))?|[\u4e00-\u9fffA-Za-z]{2,20})", re.I)


class ChatStreamError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        request_id: str,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.retryable = retryable
        self.status_code = status_code


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


def _resolve_mentions(
    content: str,
    assets: dict[str, Any],
    requested_refs: list[dict[str, str]],
    *,
    now: datetime,
) -> list[dict[str, str]]:
    aliases: dict[str, str] = {}
    verified_names: dict[str, str] = {}
    asset_symbols: set[str] = set()
    for position in assets.get("positions", []):
        symbol = str(position.get("symbol") or "").upper()
        if symbol:
            aliases[symbol.casefold()] = symbol
            asset_symbols.add(symbol)
    for symbol in assets.get("watchlist", []):
        normalized = str(symbol).upper()
        aliases[normalized.casefold()] = normalized
        asset_symbols.add(normalized)

    requested: list[tuple[str, str]] = []
    for item in requested_refs:
        symbol = normalize_symbol(str(item.get("symbol") or ""))
        name = str(item.get("name") or "").strip()
        requested.append((symbol, name))

    verified_symbols = set(asset_symbols)
    candidate_symbols = sorted(asset_symbols | {symbol for symbol, _ in requested})
    if candidate_symbols:
        with SessionLocal() as session:
            for symbol in candidate_symbols:
                master = session.scalar(
                    select(SecurityMaster)
                    .where(
                        SecurityMaster.symbol == symbol,
                        SecurityMaster.available_at <= now,
                    )
                    .order_by(SecurityMaster.available_at.desc())
                    .limit(1)
                )
                if master is not None:
                    verified_names[symbol] = master.short_name
                    verified_symbols.add(symbol)
                    aliases[master.short_name.casefold()] = symbol

    requested_by_alias: dict[str, str] = {}
    requested_symbols: list[str] = []
    requested_names_by_symbol: dict[str, set[str]] = {}
    for symbol, name in requested:
        if symbol not in verified_symbols:
            continue
        canonical_name = verified_names.get(symbol)
        if canonical_name is not None and name.casefold() == canonical_name.casefold():
            requested_by_alias[name.casefold()] = symbol
            requested_names_by_symbol.setdefault(symbol, set()).add(name)
        requested_by_alias[symbol.casefold()] = symbol
        requested_symbols.append(symbol)

    symbols: list[str] = []
    for match in _MENTION.finditer(content):
        raw = match.group(1).strip()
        resolved = _symbol_from_code(raw) or aliases.get(raw.casefold())
        resolved = resolved or requested_by_alias.get(raw.casefold())
        if resolved and resolved not in symbols:
            symbols.append(normalize_symbol(resolved))
    for symbol in requested_symbols:
        if symbol not in symbols and (
            f"@{symbol}".casefold() in content.casefold()
            or any(
                f"@{name}" in content
                for name in requested_names_by_symbol.get(symbol, set())
            )
        ):
            symbols.append(symbol)
    symbols = symbols[:5]

    symbols = [symbol for symbol in symbols if symbol in verified_symbols]
    refs: list[dict[str, str]] = []
    for symbol in symbols:
        display_name = verified_names.get(symbol) or symbol
        refs.append({"symbol": symbol, "name": display_name})
    return refs


def _system_context(
    user_id: str, symbols: list[str], decision_at: datetime
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
                    JobRun.decision_at <= decision_at,
                    ScoreRow.decision_at <= decision_at,
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
            collected_at = (item.get("status") or {}).get("collected_at")
            try:
                collected = datetime.fromisoformat(str(collected_at))
            except (TypeError, ValueError):
                continue
            if collected.tzinfo is not None and collected <= decision_at:
                quotes[item["symbol"]] = item
        for symbol in symbols:
            try:
                payload = market.klines(symbol, "day", limit=30, end=decision_at)
                bars[symbol] = payload.get("bars", [])[-30:]
            except RuntimeError:
                bars[symbol] = []
    sources = [
        {
            "source": "system",
            "symbol": symbol,
            "uri": f"/api/v1/market/quotes?symbols={symbol}",
            "available_at": (quotes.get(symbol, {}).get("status", {}) or {}).get(
                "collected_at"
            ),
        }
        for symbol in symbols
    ]
    return {
        "symbols": symbols,
        "positions": positions,
        "quotes": quotes,
        "daily_bars": bars,
        "latest_formal_scores": scores,
        "decision_at": decision_at.isoformat(),
    }, sources


def _validate_attachments(
    *, user_id: str, thread_id: str, attachment_ids: list[str], now: datetime
) -> list[AIChatAttachment]:
    unique_ids = list(dict.fromkeys(attachment_ids))
    if len(unique_ids) != len(attachment_ids) or len(unique_ids) > MAX_IMAGES_PER_MESSAGE:
        raise ValueError("attachment_ids must contain at most four unique values")
    if not unique_ids:
        return []
    with SessionLocal() as session:
        rows = list(
            session.scalars(
                select(AIChatAttachment).where(
                    AIChatAttachment.user_id == user_id,
                    AIChatAttachment.attachment_id.in_(unique_ids),
                )
            ).all()
        )
        by_id = {row.attachment_id: row for row in rows}
        if set(by_id) != set(unique_ids):
            raise ValueError("one or more attachments are unavailable")
        ordered = [by_id[item] for item in unique_ids]
        if sum(row.byte_size for row in ordered) > MAX_MESSAGE_IMAGE_BYTES:
            raise ValueError("attachment total exceeds 25 MB")
        for row in ordered:
            if row.deleted_at is not None or row.expires_at <= now:
                raise ValueError("one or more attachments have expired")
            if row.thread_id not in {None, thread_id}:
                raise ValueError("attachment belongs to another thread")
            if row.thread_id is None:
                row.thread_id = thread_id
        session.commit()
        return ordered


async def stream_chat_response(
    *,
    user_id: str,
    thread_id: str,
    content: str,
    model: str,
    reasoning_effort: str,
    web_search: bool,
    attachment_ids: list[str],
    mention_refs: list[dict[str, str]],
    decision_at: datetime,
    idempotency_key: str,
    request_id: str,
) -> AsyncIterator[dict[str, Any]]:
    now = datetime.now(UTC)
    if decision_at.tzinfo is None:
        raise ValueError("decision_at must include a timezone")
    client_decision_at = decision_at.astimezone(UTC)
    if client_decision_at > now + timedelta(seconds=30):
        raise ValueError("decision_at must not be in the future")
    decision_at = now
    trading_date = decision_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    key_sha = sha256_bytes(idempotency_key.encode())
    with SessionLocal() as session:
        thread = session.scalar(
            select(AIChatThread).where(
                AIChatThread.thread_id == thread_id,
                AIChatThread.user_id == user_id,
            )
        )
        if thread is None:
            raise KeyError(thread_id)
        assets = UserAssetService(session).get(user_id)
        refs = _resolve_mentions(content, assets, mention_refs, now=now)
        symbols = [item["symbol"] for item in refs]
        runtime = ModelConfigurationService().resolve(session)
        if runtime is not None and model not in {runtime.search_model, runtime.research_model}:
            raise ValueError("selected model is not allowed")
        attachments = _validate_attachments(
            user_id=user_id,
            thread_id=thread_id,
            attachment_ids=attachment_ids,
            now=now,
        )
        request_payload_sha = stable_hash(
            {
                "content": content,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "web_search": web_search,
                "mention_refs": refs,
                "attachments": [
                    {"id": row.attachment_id, "sha256": row.content_sha256}
                    for row in attachments
                ],
                "decision_at": client_decision_at.isoformat(),
            }
        )
        existing_user = session.scalar(
            select(AIChatMessage).where(
                AIChatMessage.thread_id == thread_id,
                AIChatMessage.idempotency_key_sha256 == key_sha,
            )
        )
        replayed = False
        if existing_user is not None:
            if existing_user.request_sha256 != request_payload_sha:
                raise ChatStreamError(
                    "Idempotency-Key 已用于其他请求",
                    code="IDEMPOTENCY_CONFLICT",
                    request_id=request_id,
                    status_code=409,
                )
            assistant = session.scalar(
                select(AIChatMessage)
                .where(
                    AIChatMessage.parent_message_id == existing_user.message_id,
                    AIChatMessage.role == "assistant",
                )
                .with_for_update()
            )
            yield {
                "type": "meta",
                "replayed": True,
                "user_message_id": existing_user.message_id,
                "assistant_message_id": assistant.message_id if assistant else None,
                "status": assistant.status if assistant else "PENDING",
            }
            if assistant is None:
                raise ChatStreamError(
                    "幂等请求状态无效",
                    code="IDEMPOTENCY_STATE_INVALID",
                    request_id=request_id,
                    retryable=False,
                )
            if assistant.status == "COMPLETED":
                for index in range(0, len(assistant.content), 80):
                    yield {"type": "delta", "delta": assistant.content[index : index + 80]}
                yield {
                    "type": "done",
                    "replayed": True,
                    "cache_hit": assistant.cache_hit,
                    "status": "COMPLETED",
                }
                return
            if assistant.status in {"PENDING", "STREAMING"}:
                raise ChatStreamError(
                    "相同请求仍在处理中",
                    code="CHAT_REQUEST_IN_PROGRESS",
                    request_id=request_id,
                    retryable=True,
                    status_code=409,
                )
            if assistant.content:
                for index in range(0, len(assistant.content), 80):
                    yield {"type": "delta", "delta": assistant.content[index : index + 80]}
                raise ChatStreamError(
                    "AI 回复未完成",
                    code=assistant.error_code or "MODEL_RESPONSE_ERROR",
                    request_id=assistant.request_id or request_id,
                    retryable=False,
                )
            assistant.status = "PENDING"
            assistant.error_code = None
            assistant.request_id = request_id
            assistant.content = ""
            assistant.cache_hit = False
            session.commit()
            user_message_id = existing_user.message_id
            assistant_message_id = assistant.message_id
            excluded_ids = [user_message_id, assistant_message_id]
            replayed = True
        else:
            excluded_ids = []

        history = list(
            session.scalars(
                select(AIChatMessage)
                .where(
                    AIChatMessage.thread_id == thread_id,
                    AIChatMessage.status.in_(("COMPLETED", "FAILED", "CANCELLED")),
                    AIChatMessage.available_at <= decision_at,
                    AIChatMessage.message_id.not_in(excluded_ids),
                )
                .order_by(AIChatMessage.created_at.desc())
                .limit(20)
            ).all()
        )[::-1]
        if existing_user is None:
            user_message = AIChatMessage(
                message_id=str(uuid4()),
                thread_id=thread_id,
                role="user",
                content=content,
                status="COMPLETED",
                trading_date=trading_date,
                decision_at=decision_at,
                available_at=decision_at,
                idempotency_key_sha256=key_sha,
                request_sha256=request_payload_sha,
                mentioned_symbols=symbols,
                mention_refs=refs,
                attachment_ids=attachment_ids,
                sources=[],
                cache_hit=False,
                request_id=request_id,
                created_at=now,
            )
            session.add(user_message)
            assistant_message = AIChatMessage(
                message_id=str(uuid4()),
                thread_id=thread_id,
                role="assistant",
                content="",
                status="PENDING",
                trading_date=trading_date,
                decision_at=decision_at,
                available_at=decision_at,
                parent_message_id=user_message.message_id,
                mentioned_symbols=symbols,
                mention_refs=refs,
                attachment_ids=[],
                sources=[],
                cache_hit=False,
                request_id=request_id,
                created_at=now,
            )
            session.add(assistant_message)
            thread.updated_at = now
            if thread.title == "新对话":
                thread.title = content.strip()[:48]
            cumulative = {item["symbol"]: item for item in thread.cumulative_mentions}
            cumulative.update({item["symbol"]: item for item in refs})
            thread.cumulative_mentions = list(cumulative.values())
            if thread.group_mode == "AUTO":
                thread.group_type, thread.group_label = automatic_group(
                    thread.cumulative_mentions
                )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                winner_user = session.scalar(
                    select(AIChatMessage).where(
                        AIChatMessage.thread_id == thread_id,
                        AIChatMessage.idempotency_key_sha256 == key_sha,
                    )
                )
                if winner_user is None:
                    raise
                if winner_user.request_sha256 != request_payload_sha:
                    raise ChatStreamError(
                        "Idempotency-Key 已用于其他请求",
                        code="IDEMPOTENCY_CONFLICT",
                        request_id=request_id,
                        status_code=409,
                    ) from None
                winner_assistant = session.scalar(
                    select(AIChatMessage).where(
                        AIChatMessage.parent_message_id == winner_user.message_id,
                        AIChatMessage.role == "assistant",
                    )
                )
                yield {
                    "type": "meta",
                    "replayed": True,
                    "user_message_id": winner_user.message_id,
                    "assistant_message_id": (
                        winner_assistant.message_id if winner_assistant else None
                    ),
                    "status": winner_assistant.status if winner_assistant else "PENDING",
                }
                if winner_assistant is not None and winner_assistant.status == "COMPLETED":
                    for index in range(0, len(winner_assistant.content), 80):
                        yield {
                            "type": "delta",
                            "delta": winner_assistant.content[index : index + 80],
                        }
                    yield {
                        "type": "done",
                        "replayed": True,
                        "cache_hit": winner_assistant.cache_hit,
                        "status": "COMPLETED",
                    }
                    return
                if winner_assistant is not None and winner_assistant.content:
                    for index in range(0, len(winner_assistant.content), 80):
                        yield {
                            "type": "delta",
                            "delta": winner_assistant.content[index : index + 80],
                        }
                    raise ChatStreamError(
                        "AI 回复未完成",
                        code=winner_assistant.error_code or "MODEL_RESPONSE_ERROR",
                        request_id=winner_assistant.request_id or request_id,
                        retryable=False,
                    ) from None
                raise ChatStreamError(
                    "相同请求仍在处理中",
                    code="CHAT_REQUEST_IN_PROGRESS",
                    request_id=request_id,
                    retryable=True,
                    status_code=409,
                ) from None
            user_message_id = user_message.message_id
            assistant_message_id = assistant_message.message_id

    yield {
        "type": "meta",
        "replayed": replayed,
        "cache_hit": False,
        "status": "PENDING",
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "symbols": symbols,
        "mention_refs": refs,
    }

    chunks: list[str] = []
    try:
        if runtime is None:
            raise ChatStreamError(
                "AI 模型尚未配置",
                code="MODEL_NOT_CONFIGURED",
                request_id=request_id,
            )
        context, sources = await asyncio.to_thread(
            _system_context, user_id, symbols, decision_at
        )
        web_sources: list[dict[str, Any]] = []
        if web_search:
            query = " ".join(symbols) + " A股 最新消息 " + content.replace("@", " ")[:120]
            try:
                web_sources = await asyncio.to_thread(SearXNGSearchClient().search, query)
            except Exception:
                web_sources = []
        sources.extend(
            {
                "source": "searxng",
                "title": item["title"],
                "uri": item["url"],
                "engine": item["engine"],
            }
            for item in web_sources
        )
        context["web_search"] = web_sources
        context_sha = stable_hash(context)
        stable_prompt = (
            "你是A股研究对话助手。只能使用系统上下文、已保存对话和联网摘要回答；"
            "明确区分实时行情、历史研究和外部网页。不得声称执行真实交易，不得泄漏"
            "内部路径、凭据或审计载荷。涉及买卖时给出条件、风险和数据时点，不承诺收益。"
            "引用网页时用[来源标题](URL)。"
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": stable_prompt}]
        history_attachment_ids = list(
            dict.fromkeys(
                attachment_id
                for item in history
                for attachment_id in (item.attachment_ids or [])
            )
        )
        history_image_parts: dict[str, list[dict[str, Any]]] = {}
        remaining_history_images = MAX_IMAGES_PER_MESSAGE
        if history_attachment_ids:
            with SessionLocal() as session:
                rows = list(
                    session.scalars(
                        select(AIChatAttachment).where(
                            AIChatAttachment.attachment_id.in_(history_attachment_ids),
                            AIChatAttachment.user_id == user_id,
                        )
                    ).all()
                )
                by_id = {row.attachment_id: row for row in rows}
                attachment_service = AttachmentService(session)
                for item in reversed(history):
                    if item.role != "user" or not item.attachment_ids:
                        continue
                    parts: list[dict[str, Any]] = []
                    for attachment_id in item.attachment_ids:
                        row = by_id.get(attachment_id)
                        data_url = (
                            attachment_service.model_data_url(row)
                            if row is not None
                            else None
                        )
                        if data_url and remaining_history_images > 0:
                            parts.append({"type": "input_image", "image_url": data_url})
                            remaining_history_images -= 1
                        elif not data_url:
                            parts.append(
                                {
                                    "type": "input_text",
                                    "text": "[图片已按七天保留策略销毁]",
                                }
                            )
                    if parts:
                        history_image_parts[item.message_id] = parts
        for item in history:
            image_parts = history_image_parts.get(item.message_id, [])
            if item.role == "user" and image_parts:
                messages.append(
                    {
                        "role": item.role,
                        "content": [
                            {"type": "input_text", "text": item.content},
                            *image_parts,
                        ],
                    }
                )
            else:
                messages.append({"role": item.role, "content": item.content})
        dynamic_context = canonical_json(context).decode("utf-8")
        messages.append({"role": "system", "content": "当前动态上下文：\n" + dynamic_context})
        current_parts: list[dict[str, Any]] = [{"type": "input_text", "text": content}]
        with SessionLocal() as session:
            current_rows = list(
                session.scalars(
                    select(AIChatAttachment).where(
                        AIChatAttachment.attachment_id.in_(attachment_ids),
                        AIChatAttachment.user_id == user_id,
                    )
                ).all()
            )
            attachment_service = AttachmentService(session)
            current_by_id = {row.attachment_id: row for row in current_rows}
            for attachment_id in attachment_ids:
                row = current_by_id.get(attachment_id)
                data_url = attachment_service.model_data_url(row) if row is not None else None
                if data_url:
                    current_parts.append({"type": "input_image", "image_url": data_url})
                else:
                    current_parts.append(
                        {"type": "input_text", "text": "[图片已按七天保留策略销毁]"}
                    )
        messages.append({"role": "user", "content": current_parts})
        request_sha = stable_hash(
            {
                "parent_turn": history[-1].message_id if history else None,
                "prompt_version": CHAT_PROMPT_VERSION,
                "model_config": runtime.config_sha256,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "question": content,
                "attachment_hashes": [row.content_sha256 for row in attachments],
                "context_sha256": context_sha,
                "messages": messages,
            }
        )
        with SessionLocal() as session:
            cached = session.scalar(
                select(AIResponseCacheRow).where(
                    AIResponseCacheRow.user_id == user_id,
                    AIResponseCacheRow.purpose == "CHAT",
                    AIResponseCacheRow.request_sha256 == request_sha,
                    AIResponseCacheRow.expires_at > datetime.now(UTC),
                )
            )
            if cached is not None:
                cached.hit_count += 1
                cached.last_hit_at = datetime.now(UTC)
                text = str(cached.response.get("content") or "")
                _complete_assistant(
                    session,
                    assistant_message_id,
                    content=text,
                    status="COMPLETED",
                    symbols=symbols,
                    refs=refs,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    sources=sources,
                    context_sha=context_sha,
                    response_sha=cached.response_sha256,
                    cache_hit=True,
                    input_tokens=0,
                    output_tokens=0,
                )
                session.commit()
                yield {"type": "cache", "cache_hit": True, "sources": sources}
                for index in range(0, len(text), 80):
                    yield {"type": "delta", "delta": text[index : index + 80]}
                yield {"type": "done", "cache_hit": True, "status": "COMPLETED"}
                return

        yield {"type": "context", "cache_hit": False, "sources": sources}
        client = OpenAICompatibleStructuredLLMClient(
            base_url=runtime.base_url,
            api_key=runtime.api_key,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=runtime.timeout_seconds,
            max_retries=2,
        )
        input_tokens = output_tokens = 0
        actual_model = model
        streaming_marked = False
        async for event in client.stream_text(
            messages=tuple(messages), idempotency_key=request_sha
        ):
            if event["type"] == "delta":
                chunks.append(str(event["delta"]))
                if not streaming_marked:
                    _set_message_status(assistant_message_id, "STREAMING")
                    streaming_marked = True
                yield event
            elif event["type"] == "completed":
                input_tokens = int(event.get("input_tokens", 0))
                output_tokens = int(event.get("output_tokens", 0))
                actual_model = str(event.get("model") or model)
        text = "".join(chunks).strip()
        if not text:
            raise ChatStreamError(
                "模型未返回文本",
                code="MODEL_EMPTY_RESPONSE",
                request_id=request_id,
            )
        response_sha = stable_hash({"content": text})
        with SessionLocal() as session:
            _complete_assistant(
                session,
                assistant_message_id,
                content=text,
                status="COMPLETED",
                symbols=symbols,
                refs=refs,
                model=actual_model,
                reasoning_effort=reasoning_effort,
                sources=sources,
                context_sha=context_sha,
                response_sha=response_sha,
                cache_hit=False,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
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
                    created_at=datetime.now(UTC),
                    expires_at=datetime.now(UTC) + timedelta(hours=24),
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                _set_message_status(assistant_message_id, "COMPLETED", content=text)
        yield {
            "type": "done",
            "cache_hit": False,
            "status": "COMPLETED",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    except asyncio.CancelledError:
        _fail_assistant(
            assistant_message_id,
            status="CANCELLED",
            content="".join(chunks),
            error_code="CLIENT_CANCELLED",
            request_id=request_id,
        )
        raise
    except OpenAICompatibleError as exc:
        _fail_assistant(
            assistant_message_id,
            status="FAILED",
            content="".join(chunks),
            error_code=exc.code,
            request_id=request_id,
        )
        raise ChatStreamError(
            "AI 对话生成失败",
            code=exc.code,
            request_id=request_id,
            retryable=exc.retryable and not chunks,
            status_code=exc.status_code,
        ) from exc
    except ChatStreamError as exc:
        _fail_assistant(
            assistant_message_id,
            status="FAILED",
            content="".join(chunks),
            error_code=exc.code,
            request_id=request_id,
        )
        raise
    except Exception as exc:
        _fail_assistant(
            assistant_message_id,
            status="FAILED",
            content="".join(chunks),
            error_code="CHAT_INTERNAL_ERROR",
            request_id=request_id,
        )
        raise ChatStreamError(
            "AI 对话生成失败",
            code="CHAT_INTERNAL_ERROR",
            request_id=request_id,
        ) from exc


def _complete_assistant(
    session: Any,
    message_id: str,
    *,
    content: str,
    status: str,
    symbols: list[str],
    refs: list[dict[str, str]],
    model: str,
    reasoning_effort: str,
    sources: list[dict[str, Any]],
    context_sha: str,
    response_sha: str,
    cache_hit: bool,
    input_tokens: int,
    output_tokens: int,
) -> None:
    row = session.get(AIChatMessage, message_id)
    if row is None:
        return
    row.content = content
    row.status = status
    row.mentioned_symbols = symbols
    row.mention_refs = refs
    row.model_name = model
    row.reasoning_effort = reasoning_effort
    row.sources = sources
    row.context_sha256 = context_sha
    row.response_sha256 = response_sha
    row.cache_hit = cache_hit
    row.input_tokens = input_tokens
    row.output_tokens = output_tokens
    row.error_code = None
    completed_at = datetime.now(UTC)
    row.available_at = completed_at
    row.decision_at = completed_at
    row.trading_date = completed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()


def _set_message_status(message_id: str, status: str, content: str | None = None) -> None:
    with SessionLocal() as session:
        row = session.get(AIChatMessage, message_id)
        if row is not None:
            row.status = status
            if content is not None:
                row.content = content
            if status == "COMPLETED":
                completed_at = datetime.now(UTC)
                row.available_at = completed_at
                row.decision_at = completed_at
                row.trading_date = completed_at.astimezone(
                    ZoneInfo("Asia/Shanghai")
                ).date()
            session.commit()


def _fail_assistant(
    message_id: str,
    *,
    status: str,
    content: str,
    error_code: str,
    request_id: str,
) -> None:
    with SessionLocal() as session:
        row = session.get(AIChatMessage, message_id)
        if row is not None and row.status != "COMPLETED":
            row.status = status
            row.content = content
            row.error_code = error_code
            row.request_id = request_id
            failed_at = datetime.now(UTC)
            row.available_at = failed_at
            row.decision_at = failed_at
            row.trading_date = failed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
            session.commit()
