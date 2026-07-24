from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from time import perf_counter
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
from ashare_ai.agents.chat_context import (
    ChatContextService,
    get_chat_context_service,
    resolve_security_mentions,
    symbol_from_code,
)
from ashare_ai.agents.chat_observability import record_chat_metric
from ashare_ai.agents.chat_threads import automatic_group
from ashare_ai.agents.model_settings import ModelConfigurationService
from ashare_ai.agents.openai_compatible import (
    OpenAICompatibleError,
    OpenAICompatibleStructuredLLMClient,
)
from ashare_ai.core.config import get_settings
from ashare_ai.core.hashing import canonical_json, sha256_bytes, stable_hash
from ashare_ai.notifications.service import NotificationService
from ashare_ai.search.web import get_web_search_service
from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.models import (
    AIChatAttachment,
    AIChatMessage,
    AIChatThread,
    AIResponseCacheRow,
)

CHAT_PROMPT_VERSION = "stock-chat-v4"


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
    return symbol_from_code(value)


def _resolve_mentions(
    content: str,
    assets: dict[str, Any],
    requested_refs: list[dict[str, str]],
    *,
    now: datetime,
) -> list[dict[str, str]]:
    del assets  # Mention identity is intentionally independent of saved assets.
    return resolve_security_mentions(
        content,
        requested_refs,
        decision_at=now,
        session_factory=SessionLocal,
    ).refs


def _system_context(
    user_id: str, symbols: list[str], decision_at: datetime
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    refs = [{"symbol": symbol, "name": symbol} for symbol in symbols]
    result = ChatContextService().build(
        user_id=user_id,
        refs=refs,
        requested_decision_at=decision_at,
        web_search=False,
        model_configuration_sha256=None,
    )
    return result.context, result.sources


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
    decision_at: datetime | None,
    idempotency_key: str,
    request_id: str,
) -> AsyncIterator[dict[str, Any]]:
    now = datetime.now(UTC)
    if decision_at is not None and decision_at.tzinfo is None:
        raise ValueError("decision_at must include a timezone")
    client_decision_at = decision_at.astimezone(UTC) if decision_at is not None else None
    if client_decision_at is not None and client_decision_at > now + timedelta(seconds=30):
        raise ValueError("decision_at must not be in the future")
    # An omitted decision_at deliberately means live mode. It is replaced by the
    # authoritative server timestamp only after parallel live-data retrieval.
    decision_at = client_decision_at or now
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
        mention_resolution = resolve_security_mentions(
            content,
            mention_refs,
            decision_at=decision_at,
            session_factory=SessionLocal,
        )
        refs = mention_resolution.refs
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
                    {"id": row.attachment_id, "sha256": row.content_sha256} for row in attachments
                ],
                "decision_at": client_decision_at.isoformat() if client_decision_at else None,
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
                thread.group_type, thread.group_label = automatic_group(thread.cumulative_mentions)
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

    chunks: list[str] = []
    try:
        if runtime is None:
            raise ChatStreamError(
                "AI 模型尚未配置",
                code="MODEL_NOT_CONFIGURED",
                request_id=request_id,
            )
        yield {"type": "stage", "stage": "retrieval", "status": "STARTED"}
        started_context = perf_counter()
        context_result = await asyncio.to_thread(
            get_chat_context_service().build,
            user_id=user_id,
            refs=refs,
            requested_decision_at=client_decision_at,
            web_search=web_search,
            model_configuration_sha256=runtime.config_sha256,
        )
        context_elapsed_ms = round((perf_counter() - started_context) * 1000)
        decision_at = context_result.decision_at
        trading_date = decision_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        context = context_result.context
        sources = context_result.sources
        if web_search and client_decision_at is None:
            public_search = await asyncio.to_thread(get_web_search_service().search, content)
            context["web_search"] = {
                "query": content,
                "results": public_search.items,
                "status": public_search.status,
            }
            sources.extend(
                {
                    "source": "searxng",
                    "title": str(item.get("title") or "网页来源"),
                    "uri": str(item.get("url") or ""),
                    "available_at": public_search.status.get("searched_at"),
                }
                for item in public_search.items
                if item.get("url")
            )
        elif web_search:
            context["web_search"] = {
                "query": content,
                "results": [],
                "status": {
                    "state": "EXCLUDED",
                    "reason_code": "HISTORICAL_WEB_SEARCH_EXCLUDED",
                },
            }
        data_status = {
            **context_result.data_status,
            "mentions": mention_resolution.statuses,
            "web_search": context.get("web_search", {}).get("status"),
        }
        context["data_status"] = data_status
        _set_message_pit(
            user_message_id,
            assistant_message_id,
            decision_at=decision_at,
            trading_date=trading_date,
        )
        record_chat_metric(
            user_id=user_id,
            metric="context",
            hit=context_result.context_cache_hit,
            latency_ms=context_elapsed_ms,
            singleflight_wait_ms=context_result.singleflight_wait_ms,
        )
        record_chat_metric(
            user_id=user_id,
            metric="market",
            hit=context_result.market_cache_hit,
            latency_ms=context_elapsed_ms,
            singleflight_wait_ms=context_result.singleflight_wait_ms,
        )
        record_chat_metric(
            user_id=user_id,
            metric="news",
            hit=context_result.news_cache_hit,
            latency_ms=context_elapsed_ms,
            singleflight_wait_ms=context_result.singleflight_wait_ms,
        )
        yield {
            "type": "meta",
            "replayed": replayed,
            "cache_hit": False,
            "status": "PENDING",
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "symbols": symbols,
            "mention_refs": refs,
            "mention_status": mention_resolution.statuses,
            "decision_at": decision_at.isoformat(),
            "historical": client_decision_at is not None,
            "data_status": data_status,
        }
        yield {
            "type": "stage",
            "stage": "market",
            "status": "COMPLETED",
            "cache_hit": context_result.market_cache_hit,
        }
        yield {
            "type": "stage",
            "stage": "news",
            "status": "COMPLETED",
            "cache_hit": context_result.news_cache_hit,
        }
        context_sha = stable_hash(context)
        # Re-read history against the final frozen live timestamp.  A browser
        # request never gets to make a newer message visible in an older turn.
        with SessionLocal() as session:
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
        profile = runtime.profile_for(model)
        stable_prompt = (
            "你是A股研究对话助手。只能使用系统上下文、已保存对话和联网摘要回答；"
            "明确区分实时行情、历史研究和外部网页。不得声称执行真实交易，不得泄漏"
            "内部路径、凭据或审计载荷。涉及买卖时给出条件、风险和数据时点，不承诺收益。"
            "引用网页时用[来源标题](URL)。"
        )
        history_messages: list[dict[str, Any]] = []
        snapshots_by_user = {
            item.parent_message_id: item.private_context_snapshot
            for item in history
            if item.role == "assistant" and item.parent_message_id and item.private_context_snapshot
        }
        history_attachment_ids = list(
            dict.fromkeys(
                attachment_id for item in history for attachment_id in (item.attachment_ids or [])
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
                            attachment_service.model_data_url(row) if row is not None else None
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
            if profile.cache_policy == "GROK":
                snapshot = snapshots_by_user.get(item.message_id)
                if isinstance(snapshot, str) and snapshot:
                    history_messages.append(
                        {"role": "system", "content": "当前动态上下文：\n" + snapshot}
                    )
            image_parts = history_image_parts.get(item.message_id, [])
            if item.role == "user" and image_parts:
                history_messages.append(
                    {
                        "role": item.role,
                        "content": [
                            {"type": "input_text", "text": item.content},
                            *image_parts,
                        ],
                    }
                )
            else:
                history_messages.append({"role": item.role, "content": item.content})
        dynamic_context = canonical_json(context).decode("utf-8")
        dynamic_message = {"role": "system", "content": "当前动态上下文：\n" + dynamic_context}
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
        current_message = {"role": "user", "content": current_parts}
        selected_history, context_budget_status = _select_history_within_budget(
            stable_prompt=stable_prompt,
            history_messages=history_messages,
            dynamic_message=dynamic_message,
            current_message=current_message,
            input_budget_tokens=profile.input_budget_tokens,
        )
        if selected_history is None:
            raise ChatStreamError(
                "当前上下文超过所选模型的安全输入预算，请减少引用、图片或问题范围后重试",
                code="CHAT_CONTEXT_TOO_LARGE",
                request_id=request_id,
            )
        messages: list[dict[str, Any]] = [{"role": "system", "content": stable_prompt}]
        messages.extend(selected_history)
        messages.append(dynamic_message)
        messages.append(current_message)
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
        attachment_context_sha = stable_hash([row.content_sha256 for row in attachments])
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
                    input_tokens=cached.input_tokens,
                    cached_input_tokens=cached.input_tokens,
                    cache_write_tokens=0,
                    output_tokens=cached.output_tokens,
                    reasoning_tokens=0,
                    cache_policy=profile.cache_policy,
                    context_budget_status=context_budget_status,
                    private_context_snapshot=dynamic_context,
                    streaming_mode="CACHED",
                    data_status=data_status,
                    model_configuration_sha256=runtime.config_sha256,
                    attachment_context_sha256=attachment_context_sha,
                )
                session.commit()
                record_chat_metric(
                    user_id=user_id,
                    metric="answer",
                    hit=True,
                    latency_ms=0,
                )
                record_chat_metric(
                    user_id=user_id,
                    metric="model",
                    hit=True,
                    latency_ms=0,
                )
                yield {"type": "cache", "cache_hit": True, "sources": sources}
                yield {"type": "stage", "stage": "generation", "status": "CACHED"}
                for index in range(0, len(text), 80):
                    yield {"type": "delta", "delta": text[index : index + 80]}
                yield {
                    "type": "done",
                    "cache_hit": True,
                    "status": "COMPLETED",
                    "streaming_mode": "CACHED",
                }
                return

        yield {"type": "context", "cache_hit": False, "sources": sources}
        yield {"type": "stage", "stage": "generation", "status": "STARTED"}
        yield {"type": "heartbeat", "stage": "generation"}
        client = OpenAICompatibleStructuredLLMClient(
            base_url=runtime.base_url,
            api_key=runtime.api_key,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=runtime.timeout_seconds,
            max_retries=2,
            cache_policy=profile.cache_policy,
        )
        input_tokens = cached_input_tokens = cache_write_tokens = output_tokens = (
            reasoning_tokens
        ) = 0
        actual_model = model
        response_id: str | None = None
        streaming_mode = "STREAMING"
        model_started = perf_counter()
        streaming_marked = False
        previous_response_id = _reusable_response_id(
            history,
            model=model,
            configuration_sha256=runtime.config_sha256,
            attachment_context_sha=attachment_context_sha,
            allow_reuse=(
                profile.cache_policy == "OPENAI" and context_budget_status == "WITHIN_BUDGET"
            ),
        )
        request_messages = (
            (dynamic_message, current_message) if previous_response_id else tuple(messages)
        )
        async for event in client.stream_text(
            messages=request_messages,
            idempotency_key=request_sha,
            previous_response_id=previous_response_id,
            prompt_cache_key=stable_hash(
                {
                    "prompt_version": CHAT_PROMPT_VERSION,
                    "stable_prompt": stable_prompt,
                    "model": model,
                }
            ),
        ):
            if event["type"] == "delta":
                chunks.append(str(event["delta"]))
                if not streaming_marked:
                    _set_message_status(assistant_message_id, "STREAMING")
                    streaming_marked = True
                yield event
            elif event["type"] == "degraded":
                streaming_mode = "DEGRADED"
                yield {"type": "stage", "stage": "generation", "status": "DEGRADED"}
            elif event["type"] == "completed":
                input_tokens = int(event.get("input_tokens", 0))
                cached_input_tokens = int(event.get("cached_input_tokens", 0))
                cache_write_tokens = int(event.get("cache_write_tokens", 0))
                output_tokens = int(event.get("output_tokens", 0))
                reasoning_tokens = int(event.get("reasoning_tokens", 0))
                actual_model = str(event.get("model") or model)
                raw_response_id = event.get("response_id")
                response_id = raw_response_id if isinstance(raw_response_id, str) else None
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
                cached_input_tokens=cached_input_tokens,
                cache_write_tokens=cache_write_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
                cache_policy=profile.cache_policy,
                context_budget_status=context_budget_status,
                private_context_snapshot=dynamic_context,
                streaming_mode=streaming_mode,
                data_status=data_status,
                response_id=response_id,
                model_configuration_sha256=runtime.config_sha256,
                attachment_context_sha256=attachment_context_sha,
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
                    cached_input_tokens=cached_input_tokens,
                    cache_write_tokens=cache_write_tokens,
                    output_tokens=output_tokens,
                    reasoning_tokens=reasoning_tokens,
                    cache_policy=profile.cache_policy,
                    created_at=datetime.now(UTC),
                    expires_at=datetime.now(UTC) + timedelta(hours=24),
                )
            )
            try:
                if streaming_mode == "DEGRADED":
                    NotificationService(session).create_for_administrators(
                        notification_type="CHAT_STREAM_DEGRADED",
                        severity="WARNING",
                        title="AI 对话已降级为一次性回复",
                        body="当前模型网关不支持稳定的 Responses SSE 流式输出。",
                        resource_type="AI_CHAT_MESSAGE",
                        resource_id=assistant_message_id,
                        dedupe_key=f"chat-stream-degraded:{runtime.config_sha256}",
                    )
                session.commit()
            except IntegrityError:
                session.rollback()
                _set_message_status(assistant_message_id, "COMPLETED", content=text)
        model_elapsed_ms = round((perf_counter() - model_started) * 1000)
        record_chat_metric(
            user_id=user_id,
            metric="answer",
            hit=False,
            latency_ms=model_elapsed_ms,
            degraded=streaming_mode == "DEGRADED",
        )
        record_chat_metric(
            user_id=user_id,
            metric="model",
            hit=False,
            latency_ms=model_elapsed_ms,
            degraded=streaming_mode == "DEGRADED",
        )
        yield {
            "type": "done",
            "cache_hit": False,
            "status": "COMPLETED",
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "cache_write_tokens": cache_write_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "cache_policy": profile.cache_policy,
            "context_budget_status": context_budget_status,
            "streaming_mode": streaming_mode,
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
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
    cache_policy: str = "COMPATIBLE",
    context_budget_status: str = "WITHIN_BUDGET",
    private_context_snapshot: str | None = None,
    streaming_mode: str = "STREAMING",
    data_status: dict[str, Any] | None = None,
    response_id: str | None = None,
    model_configuration_sha256: str | None = None,
    attachment_context_sha256: str | None = None,
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
    row.cached_input_tokens = cached_input_tokens
    row.cache_write_tokens = cache_write_tokens
    row.output_tokens = output_tokens
    row.reasoning_tokens = reasoning_tokens
    row.cache_policy = cache_policy
    row.context_budget_status = context_budget_status
    row.private_context_snapshot = private_context_snapshot
    row.error_code = None
    row.streaming_mode = streaming_mode
    row.data_status = data_status or {}
    row.response_id = response_id
    row.model_configuration_sha256 = model_configuration_sha256
    row.attachment_context_sha256 = attachment_context_sha256
    completed_at = datetime.now(UTC)
    row.available_at = completed_at


def _set_message_status(message_id: str, status: str, content: str | None = None) -> None:
    with SessionLocal() as session:
        row = session.get(AIChatMessage, message_id)
        if row is not None:
            row.status = status
            if content is not None:
                row.content = content
            if status == "COMPLETED":
                row.available_at = datetime.now(UTC)
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
            row.available_at = datetime.now(UTC)
            session.commit()


def _set_message_pit(
    user_message_id: str,
    assistant_message_id: str,
    *,
    decision_at: datetime,
    trading_date: Any,
) -> None:
    with SessionLocal() as session:
        for message_id in (user_message_id, assistant_message_id):
            row = session.get(AIChatMessage, message_id)
            if row is not None:
                row.decision_at = decision_at
                row.trading_date = trading_date
        session.commit()


def _reusable_response_id(
    history: list[AIChatMessage],
    *,
    model: str,
    configuration_sha256: str,
    attachment_context_sha: str,
    allow_reuse: bool,
) -> str | None:
    """Reuse only an intact OpenAI conversation; never trust a cropped remote chain."""

    if not allow_reuse:
        return None
    for row in reversed(history):
        if row.role != "assistant" or row.status != "COMPLETED":
            continue
        if (
            row.response_id
            and row.model_name == model
            and row.model_configuration_sha256 == configuration_sha256
            and row.attachment_context_sha256 == attachment_context_sha
        ):
            return row.response_id
        return None
    return None


def _estimate_message_tokens(message: Mapping[str, Any]) -> int:
    """Conservative local estimator used only to protect a provider context window."""

    content = message.get("content")
    if isinstance(content, str):
        return max(1, (len(content.encode("utf-8")) + 3) // 4)
    if not isinstance(content, list):
        return 1
    total = 0
    for part in content:
        if not isinstance(part, Mapping):
            continue
        if part.get("type") == "input_image":
            total += 1024
        else:
            text = part.get("text")
            if isinstance(text, str):
                total += max(1, (len(text.encode("utf-8")) + 3) // 4)
    return max(1, total)


def _conversation_turns(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    turns: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    pending_prefix: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "system" and any(item.get("role") in {"user", "assistant"} for item in current):
            pending_prefix.append(message)
            continue
        if role == "user" and any(item.get("role") in {"user", "assistant"} for item in current):
            turns.append(current)
            current = pending_prefix
            pending_prefix = []
        elif pending_prefix:
            current.extend(pending_prefix)
            pending_prefix = []
        current.append(message)
    current.extend(pending_prefix)
    if current:
        turns.append(current)
    return turns


def _select_history_within_budget(
    *,
    stable_prompt: str,
    history_messages: list[dict[str, Any]],
    dynamic_message: dict[str, Any],
    current_message: dict[str, Any],
    input_budget_tokens: int,
) -> tuple[list[dict[str, Any]] | None, str]:
    fixed = sum(
        _estimate_message_tokens(message)
        for message in (
            {"role": "system", "content": stable_prompt},
            dynamic_message,
            current_message,
        )
    )
    if fixed > input_budget_tokens:
        return None, "CONTEXT_TOO_LARGE"
    selected: list[list[dict[str, Any]]] = []
    used = fixed
    trimmed = False
    for turn in reversed(_conversation_turns(history_messages)):
        turn_tokens = sum(_estimate_message_tokens(message) for message in turn)
        if used + turn_tokens > input_budget_tokens:
            trimmed = True
            break
        selected.append(turn)
        used += turn_tokens
    selected.reverse()
    flattened = [message for turn in selected for message in turn]
    if len(flattened) != len(history_messages):
        trimmed = True
    return flattened, "HISTORY_TRIMMED" if trimmed else "WITHIN_BUDGET"
