from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, time
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ashare_ai.agents.ai_cache import AICacheGeneration, AIResultCacheService
from ashare_ai.agents.model_settings import ModelConfigurationService
from ashare_ai.agents.openai_compatible import (
    OpenAICompatibleError,
    OpenAICompatibleStructuredLLMClient,
)
from ashare_ai.agents.protocols import TextGeneration
from ashare_ai.core.config import get_settings
from ashare_ai.core.hashing import canonical_json, sha256_bytes, stable_hash
from ashare_ai.core.time import SHANGHAI
from ashare_ai.market.service import get_market_data_service
from ashare_ai.notifications.service import NotificationService
from ashare_ai.orchestration.exit_advice_queue import (
    build_exit_advice_queue,
)
from ashare_ai.orchestration.exit_advice_queue import (
    enqueue_exit_advice as _enqueue_exit_advice,
)
from ashare_ai.orchestration.operation_runs import (
    create_operation_run,
    transition_operation_run,
)
from ashare_ai.orchestration.research_schedule import FreeExchangeCalendar
from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.models import (
    ExitAdviceRow,
    JobRun,
    ScoreRow,
    SecurityMaster,
    UserAccount,
    UserAssetState,
)
from ashare_ai.trading.rules import RuleContext, TradingRuleRepository
from ashare_ai.trading.sellability import position_sellability

PROMPT_VERSION = "exit-advice-v2"
_JSON_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", flags=re.DOTALL | re.IGNORECASE)


def enqueue_exit_advice(advice_id: str, redis_url: str | None = None) -> None:
    """Compatibility export for existing API and monitoring callers."""
    _enqueue_exit_advice(advice_id, redis_url)


class ExitLadderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_price: Decimal = Field(gt=0, max_digits=18, decimal_places=4)
    quantity: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=300)


class ExitAdviceAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["HOLD", "REDUCE", "SELL"]
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(min_length=1, max_length=1000)
    ladder: list[ExitLadderItem] = Field(default_factory=list, max_length=5)
    stop_loss_price: Decimal | None = Field(default=None, gt=0)
    risks: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def require_ladder_for_exit_actions(self) -> ExitAdviceAnalysis:
        """A HOLD conclusion is valid without an executable sell ladder."""
        if self.action in {"REDUCE", "SELL"} and not self.ladder:
            raise ValueError("REDUCE and SELL advice must include at least one ladder item")
        return self


class ExitAdviceRequestError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class ExitAdviceOutputError(OpenAICompatibleError):
    """A non-persisted parse or contract failure eligible for one repair attempt."""


def _parse_exit_advice_output(text: str) -> ExitAdviceAnalysis:
    """Normalize narrowly-defined JSON variants and enforce the domain contract."""

    cleaned = text.strip().lstrip("\ufeff")
    fenced = _JSON_FENCE.match(cleaned)
    if fenced:
        cleaned = fenced.group(1).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ExitAdviceOutputError(
            "Exit advice output is not a JSON object", code="MODEL_OUTPUT_INVALID_JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ExitAdviceOutputError(
            "Exit advice output is not a JSON object", code="MODEL_OUTPUT_INVALID_JSON"
        )
    normalized = dict(payload)
    _normalize_exit_advice_fields(normalized)
    try:
        return ExitAdviceAnalysis.model_validate(normalized)
    except ValueError as exc:
        raise ExitAdviceOutputError(
            "Exit advice output does not satisfy the domain contract",
            code="MODEL_OUTPUT_SCHEMA_INVALID",
        ) from exc


def _normalize_exit_advice_fields(payload: dict[str, Any]) -> None:
    """Accept only predictable presentation aliases before strict validation."""

    aliases = {
        "recommendation": "action",
        "建议": "action",
        "结论": "summary",
        "analysis": "summary",
        "sell_ladder": "ladder",
        "分批卖出": "ladder",
    }
    for source, target in aliases.items():
        if target not in payload and source in payload:
            payload[target] = payload.pop(source)
    action = payload.get("action")
    if isinstance(action, str):
        payload["action"] = {
            "持有": "HOLD",
            "继续持有": "HOLD",
            "减仓": "REDUCE",
            "分批减仓": "REDUCE",
            "卖出": "SELL",
        }.get(action.strip().upper(), action.strip().upper())
    for field in ("ladder", "risks"):
        if payload.get(field) is None:
            payload[field] = []
    if payload.get("stop_loss_price") in {"", "-", "无", "none", "null"}:
        payload["stop_loss_price"] = None
    ladder = payload.get("ladder")
    if isinstance(ladder, list):
        for item in ladder:
            if not isinstance(item, dict):
                continue
            if "target_price" not in item and "price" in item:
                item["target_price"] = item.pop("price")


def _format_repair_messages(raw_output: str) -> tuple[dict[str, str], dict[str, str]]:
    """Ask once for syntax/shape repair only; do not request new investment analysis."""

    contract = {
        "action": "HOLD | REDUCE | SELL",
        "confidence": "0 到 1 的数字",
        "summary": "非空文本",
        "ladder": [
            {"target_price": "正数", "quantity": "正整数", "reason": "非空文本"}
        ],
        "stop_loss_price": "正数或 null",
        "risks": ["文本"],
    }
    return (
        {
            "role": "system",
            "content": (
                "你只负责把已有退出建议转换为指定 JSON 对象，不能改变其投资结论、"
                "不能补充事实。只返回 JSON，不要 Markdown。HOLD 的 ladder 必须为空；"
                "REDUCE 或 SELL 的 ladder 必须有 1 到 5 项。"
            ),
        },
        {
            "role": "user",
            "content": canonical_json(
                {"contract": contract, "output_to_repair": raw_output}
            ).decode("utf-8"),
        },
    )


def _generate_exit_advice_with_repair(
    client: Any,
    *,
    messages: tuple[dict[str, str], ...],
    request_sha: str,
    on_repair: Callable[[str], None],
) -> tuple[ExitAdviceAnalysis, TextGeneration, bool]:
    """Generate once, then make one format-only repair attempt if required."""

    generation = asyncio.run(
        client.generate_json_object_from_stream(
            messages=messages,
            idempotency_key=request_sha,
        )
    )
    try:
        return _parse_exit_advice_output(generation.text), generation, False
    except ExitAdviceOutputError:
        on_repair(generation.text)
        repair_sha = stable_hash({"request_sha": request_sha, "attempt": "format-repair"})
        repair_generation = asyncio.run(
            client.generate_json_object_from_stream(
                messages=_format_repair_messages(generation.text),
                idempotency_key=repair_sha,
            )
        )
        try:
            analysis = _parse_exit_advice_output(repair_generation.text)
        except ExitAdviceOutputError as exc:
            raise OpenAICompatibleError(
                "Exit advice format repair did not produce a valid contract",
                code="MODEL_OUTPUT_REPAIR_FAILED",
            ) from exc
        return analysis, repair_generation, True


def request_manual_exit_advice(
    *,
    user_id: str,
    symbol: str,
    now: datetime | None = None,
    enqueue: Callable[[str], None] = enqueue_exit_advice,
) -> ExitAdviceRow:
    """Create a user-requested paper exit study using the normal exit worker.

    This function is deliberately the same resource and queue consumed by
    automatic profit/stop-loss triggers.  It does not submit, alter, or sell a
    simulated position; the worker only returns a reviewed recommendation.
    """

    with SessionLocal() as session:
        row = create_manual_exit_advice(
            session,
            user_id=user_id,
            symbol=symbol,
            now=now,
        )
        session.commit()
        advice_id = row.advice_id
    enqueue(advice_id)
    return row


def create_manual_exit_advice(
    session: Session,
    *,
    user_id: str,
    symbol: str,
    now: datetime | None = None,
) -> ExitAdviceRow:
    """Build a manual paper exit row without committing or enqueueing it.

    API callers use this function with their request transaction so the durable
    idempotency record and the queued resource are created atomically.
    """

    current = (now or datetime.now(UTC)).astimezone(UTC)
    normalized_symbol = symbol.strip().upper()
    state = session.get(UserAssetState, user_id)
    position = next(
        (
            dict(item)
            for item in (state.positions if state is not None else [])
            if str(item.get("symbol", "")).upper() == normalized_symbol
        ),
        None,
    )
    if position is None:
        raise ExitAdviceRequestError("position not found", code="POSITION_NOT_FOUND")
    try:
        quote = next(
            item
            for item in get_market_data_service().quotes([normalized_symbol])
            if item.get("symbol") == normalized_symbol
        )
    except Exception as exc:
        raise ExitAdviceRequestError("latest quote unavailable", code="QUOTE_UNAVAILABLE") from exc
    price = _fresh_quote_price(quote, current)
    decision_at = _quote_time(quote, current)
    cost = Decimal(str(position.get("cost", 0)))
    quantity = int(position.get("quantity", 0))
    if cost <= 0 or quantity <= 0:
        raise ExitAdviceRequestError("position is invalid", code="POSITION_INVALID")
    profit = ((price - cost) * quantity).quantize(Decimal("0.01"))
    _preflight_exit_rules(session, normalized_symbol, decision_at)
    runtime = ModelConfigurationService().resolve(session)
    context = _latest_research_context(
        user_id,
        normalized_symbol,
        decision_at,
        session=session,
    )
    context["model_configuration"] = runtime.manifest_reference() if runtime else None
    context["trigger_source"] = "MANUAL"
    context["policy_config_sha256"] = _policy_config_sha256()
    row = _create_exit_advice_row(
        session,
        user_id=user_id,
        symbol=normalized_symbol,
        decision_at=decision_at,
        current_price=price,
        unrealized_profit=profit,
        trigger_amount=Decimal("0"),
        trigger_type="MANUAL",
        trigger_price=None,
        position_snapshot=position,
        research_context=context,
    )
    return row


def create_stop_loss_exit_advice(
    *,
    user_id: str,
    symbol: str,
    position: dict[str, Any],
    quote: dict[str, Any],
    stop_loss_price: Decimal,
    now: datetime,
    enqueue: Callable[[str], None] = enqueue_exit_advice,
    session_factory: Callable[[], Session] = SessionLocal,
) -> ExitAdviceRow:
    """Queue a fast paper exit study after a stop-loss notification has been created."""

    price = _fresh_quote_price(quote, now)
    decision_at = _quote_time(quote, now)
    cost = Decimal(str(position.get("cost", 0)))
    quantity = int(position.get("quantity", 0))
    profit = ((price - cost) * quantity).quantize(Decimal("0.01"))
    with session_factory() as session:
        _preflight_exit_rules(session, symbol, decision_at)
        runtime = ModelConfigurationService().resolve(session)
        context = _latest_research_context(user_id, symbol, decision_at, session=session)
        context["model_configuration"] = runtime.manifest_reference() if runtime else None
        context["trigger_source"] = "STOP_LOSS"
        context["policy_config_sha256"] = _policy_config_sha256()
        row = _create_exit_advice_row(
            session,
            user_id=user_id,
            symbol=symbol,
            decision_at=decision_at,
            current_price=price,
            unrealized_profit=profit,
            trigger_amount=Decimal("0"),
            trigger_type="STOP_LOSS",
            trigger_price=stop_loss_price,
            position_snapshot=dict(position),
            research_context=context,
        )
        session.commit()
        advice_id = row.advice_id
    enqueue(advice_id)
    return row


def _create_exit_advice_row(
    session: Session,
    *,
    user_id: str,
    symbol: str,
    decision_at: datetime,
    current_price: Decimal,
    unrealized_profit: Decimal,
    trigger_amount: Decimal,
    trigger_type: str,
    trigger_price: Decimal | None,
    position_snapshot: dict[str, Any],
    research_context: dict[str, Any],
) -> ExitAdviceRow:
    input_hash = stable_hash(
        {
            "purpose": "EXIT_ADVICE",
            "user_id": user_id,
            "symbol": symbol,
            "decision_at": decision_at,
            "price": str(current_price),
            "profit": str(unrealized_profit),
            "trigger_type": trigger_type,
            "trigger_price": str(trigger_price) if trigger_price is not None else None,
            "trigger_amount": str(trigger_amount),
            "position": position_snapshot,
            "research": research_context,
            "prompt_version": PROMPT_VERSION,
        }
    )
    existing = session.scalar(select(ExitAdviceRow).where(ExitAdviceRow.input_hash == input_hash))
    if existing is not None:
        return existing
    row = ExitAdviceRow(
        user_id=user_id,
        symbol=symbol,
        status="PENDING",
        decision_at=decision_at,
        available_at=decision_at,
        current_price=current_price,
        unrealized_profit=unrealized_profit,
        trigger_amount=trigger_amount,
        trigger_type=trigger_type,
        trigger_price=trigger_price,
        position_snapshot=position_snapshot,
        research_context=research_context,
        prompt_version=PROMPT_VERSION,
        input_hash=input_hash,
        cache_hit=False,
        created_at=datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    operation = create_operation_run(
        session,
        user_id=row.user_id,
        run_type="EXIT_ADVICE",
        resource_id=row.advice_id,
        trading_date=row.decision_at.astimezone(SHANGHAI).date(),
        decision_at=row.decision_at,
        input_hash=row.input_hash,
        manifest={"symbol": row.symbol, "trigger_type": row.trigger_type},
        created_at=row.created_at,
    )
    row.operation_run_id = operation.run_id
    return row


def _fresh_quote_price(quote: dict[str, Any], now: datetime) -> Decimal:
    raw_status = quote.get("status")
    status: dict[str, Any] = dict(raw_status) if isinstance(raw_status, dict) else {}
    if status.get("stale") or status.get("delayed"):
        raise ExitAdviceRequestError("latest quote is stale", code="QUOTE_STALE")
    try:
        price = Decimal(str(quote.get("price")))
    except Exception as exc:
        raise ExitAdviceRequestError("latest quote unavailable", code="QUOTE_UNAVAILABLE") from exc
    if price <= 0:
        raise ExitAdviceRequestError("latest quote unavailable", code="QUOTE_UNAVAILABLE")
    quote_at = _quote_time(quote, now).astimezone(UTC)
    if (now.astimezone(UTC) - quote_at).total_seconds() > get_settings().market_stale_seconds:
        raise ExitAdviceRequestError("latest quote is stale", code="QUOTE_STALE")
    return price


def _preflight_exit_rules(session: Session, symbol: str, decision_at: datetime) -> None:
    master = session.scalar(
        select(SecurityMaster)
        .where(
            SecurityMaster.symbol == symbol,
            SecurityMaster.available_at <= decision_at,
            SecurityMaster.effective_from <= decision_at.astimezone(SHANGHAI).date(),
            (SecurityMaster.effective_to.is_(None))
            | (SecurityMaster.effective_to >= decision_at.astimezone(SHANGHAI).date()),
            SecurityMaster.source_record_id.not_like("directory-%"),
        )
        .order_by(SecurityMaster.available_at.desc(), SecurityMaster.fetched_at.desc())
        .limit(1)
    )
    if master is None:
        raise ExitAdviceRequestError(
            "trading rule context unavailable", code="SECURITY_MASTER_UNAVAILABLE"
        )
    context = RuleContext(
        symbol=symbol,
        trading_date=decision_at.astimezone(SHANGHAI).date(),
        decision_at=decision_at,
        exchange=master.exchange,
        market="A",
        board=master.board,
        security_type="STOCK",
        risk_status="NORMAL",
        is_st=master.is_st,
        listing_days=max(0, (decision_at.date() - master.list_date).days),
        listing_session=max(0, (decision_at.date() - master.list_date).days),
    )
    try:
        TradingRuleRepository().resolve(session, context)
    except Exception as exc:
        raise ExitAdviceRequestError(
            "trading rules unavailable", code="TRADING_RULE_UNAVAILABLE"
        ) from exc


def _policy_config_sha256() -> str | None:
    try:
        return sha256_bytes(get_settings().policy_config_path.read_bytes())
    except OSError:
        return None


def _during_trading_session(current: datetime) -> bool:
    value = current.astimezone(SHANGHAI).time().replace(tzinfo=None)
    return time(9, 30) <= value <= time(11, 30) or time(13, 0) <= value <= time(15, 0)


def dispatch_exit_advice(
    *, now: datetime | None = None, enqueue: Callable[[str], None] = enqueue_exit_advice
) -> dict[str, Any]:
    current = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if not _during_trading_session(current):
        return {"state": "OUTSIDE_SESSION", "queued": []}
    try:
        sessions = FreeExchangeCalendar().sessions(current.date(), current.date())
    except Exception as exc:
        return {"state": "CALENDAR_UNAVAILABLE", "error_type": type(exc).__name__, "queued": []}
    if current.date() not in sessions:
        return {"state": "NON_TRADING_DAY", "queued": []}

    with SessionLocal() as session:
        states = list(
            session.scalars(
                select(UserAssetState)
                .join(UserAccount, UserAccount.user_id == UserAssetState.user_id)
                .where(
                    UserAssetState.exit_monitor_enabled.is_(True),
                    UserAccount.enabled.is_(True),
                )
            ).all()
        )
        runtime = ModelConfigurationService().resolve(session)
        model_reference = runtime.manifest_reference() if runtime is not None else None
    pairs: list[tuple[UserAssetState, dict[str, Any], str, Decimal]] = []
    for state in states:
        for position in state.positions:
            raw_price_trigger = position.get("exit_trigger_price")
            if raw_price_trigger:
                pairs.append((state, position, "PRICE", Decimal(str(raw_price_trigger))))
                continue
            raw_trigger = position.get("profit_trigger_amount") or state.default_profit_trigger
            if not raw_trigger:
                continue
            pairs.append((state, position, "PROFIT_AMOUNT", Decimal(str(raw_trigger))))
    symbols = sorted({str(position.get("symbol")) for _, position, _, _ in pairs})
    if not symbols:
        return {"state": "READY", "queued": []}
    quotes = {item["symbol"]: item for item in get_market_data_service().quotes(symbols)}
    queued: list[str] = []
    for state, position, trigger_type, trigger in pairs:
        symbol = str(position.get("symbol"))
        quote = quotes.get(symbol)
        if quote is None or not quote.get("price"):
            continue
        price = Decimal(str(quote["price"]))
        cost = Decimal(str(position.get("cost", 0)))
        quantity = int(position.get("quantity", 0))
        profit = ((price - cost) * quantity).quantize(Decimal("0.01"))
        trigger_price: Decimal | None = None
        if trigger_type == "PRICE":
            trigger_price = trigger
            if price <= trigger_price:
                continue
            equivalent_amount = ((trigger_price - cost) * quantity).quantize(Decimal("0.01"))
        else:
            if profit <= trigger:
                continue
            equivalent_amount = trigger
        decision_at = _quote_time(quote, current)
        context = _latest_research_context(state.user_id, symbol, decision_at)
        context["model_configuration"] = model_reference
        position_snapshot = dict(position)
        input_hash = stable_hash(
            {
                "purpose": "EXIT_ADVICE",
                "user_id": state.user_id,
                "symbol": symbol,
                "decision_at": decision_at,
                "price": str(price),
                "profit": str(profit),
                "trigger_type": trigger_type,
                "trigger_price": str(trigger_price) if trigger_price is not None else None,
                "trigger_amount": str(equivalent_amount),
                "position": position_snapshot,
                "research": context,
                "prompt_version": PROMPT_VERSION,
            }
        )
        with SessionLocal() as session:
            latest = session.scalar(
                select(ExitAdviceRow)
                .where(
                    ExitAdviceRow.user_id == state.user_id,
                    ExitAdviceRow.symbol == symbol,
                    ExitAdviceRow.created_at
                    >= datetime.combine(current.date(), time.min, SHANGHAI),
                )
                .order_by(ExitAdviceRow.created_at.desc())
                .limit(1)
            )
            if latest is not None:
                price_change = abs(price / latest.current_price - 1)
                same_position = stable_hash(position_snapshot) == stable_hash(
                    latest.position_snapshot
                )
                same_research = context.get("score_sha256") == latest.research_context.get(
                    "score_sha256"
                )
                if price_change < Decimal("0.03") and same_position and same_research:
                    continue
            row = ExitAdviceRow(
                user_id=state.user_id,
                symbol=symbol,
                status="PENDING",
                decision_at=decision_at,
                available_at=decision_at,
                current_price=price,
                unrealized_profit=profit,
                trigger_amount=equivalent_amount,
                trigger_type=trigger_type,
                trigger_price=trigger_price,
                position_snapshot=position_snapshot,
                research_context=context,
                prompt_version=PROMPT_VERSION,
                input_hash=input_hash,
                cache_hit=False,
                created_at=datetime.now(UTC),
            )
            session.add(row)
            session.flush()
            operation = create_operation_run(
                session,
                user_id=row.user_id,
                run_type="EXIT_ADVICE",
                resource_id=row.advice_id,
                trading_date=row.decision_at.astimezone(SHANGHAI).date(),
                decision_at=row.decision_at,
                input_hash=row.input_hash,
                manifest={"symbol": row.symbol, "trigger_type": row.trigger_type},
                created_at=row.created_at,
            )
            row.operation_run_id = operation.run_id
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                continue
            advice_id = row.advice_id
        enqueue(advice_id)
        queued.append(advice_id)
    return {"state": "READY", "queued": queued}


def _quote_time(quote: dict[str, Any], fallback: datetime) -> datetime:
    raw = (quote.get("status") or {}).get("collected_at") or quote.get("collected_at")
    if raw:
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return fallback
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=SHANGHAI)
    return fallback


def _latest_research_context(
    user_id: str,
    symbol: str,
    decision_at: datetime,
    *,
    session: Session | None = None,
) -> dict[str, Any]:
    if session is None:
        with SessionLocal() as owned_session:
            return _latest_research_context(
                user_id,
                symbol,
                decision_at,
                session=owned_session,
            )
    row = session.scalar(
        select(ScoreRow)
        .join(JobRun, JobRun.run_id == ScoreRow.run_id)
        .where(
            ScoreRow.symbol == symbol,
            JobRun.user_id == user_id,
            JobRun.status.in_(("SUCCEEDED", "FUSED")),
            ScoreRow.decision_at <= decision_at,
        )
        .order_by(JobRun.trading_date.desc(), JobRun.completed_at.desc())
        .limit(1)
    )
    if row is None:
        return {"status": "NO_FORMAL_RESEARCH", "score_sha256": None}
    payload = {
        "status": "AVAILABLE",
        "run_id": row.run_id,
        "trading_date": row.trading_date.isoformat(),
        "decision_at": row.decision_at.isoformat(),
        "total_score": row.total_score,
        "fundamental_score": row.fundamental_score,
        "technical_score": row.technical_score,
        "sentiment_score": row.sentiment_score,
        "event_risk_multiplier": row.event_risk_multiplier,
        "formula_version": row.formula_version,
    }
    payload["score_sha256"] = stable_hash(payload)
    return payload


def execute_exit_advice(advice_id: str) -> dict[str, Any]:
    now = datetime.now(UTC)
    with SessionLocal() as session:
        row = session.get(ExitAdviceRow, advice_id)
        if row is None:
            raise KeyError(advice_id)
        if row.status == "SUCCEEDED":
            return row.result or {}
        row.status = "RUNNING"
        transition_operation_run(
            session,
            row.operation_run_id,
            status="RUNNING",
            event_type="EXIT_ADVICE_STARTED",
            message="Exit advice generation started",
            details={"advice_id": row.advice_id, "symbol": row.symbol},
        )
        session.commit()
        reference = row.research_context.get("model_configuration")
        runtime = (
            ModelConfigurationService().resolve_pinned(session, reference)
            if isinstance(reference, dict)
            else None
        )
        if runtime is None:
            row.status = "UNAVAILABLE"
            row.error_message = "MODEL_UNAVAILABLE"
            row.result = {"failure_code": "MODEL_UNAVAILABLE", "paper_trade_only": True}
            row.completed_at = now
            NotificationService(session).create(
                user_id=row.user_id,
                notification_type="EXIT_ADVICE_UNAVAILABLE",
                severity="WARNING",
                title=f"{row.symbol} 模拟退出研究暂不可用",
                body="未生成退出研究；不会改变持仓或自动卖出。",
                resource_type="EXIT_ADVICE",
                resource_id=row.advice_id,
                dedupe_key=f"exit-unavailable:{row.advice_id}",
            )
            session.commit()
            return {}
        evidence = {
            "symbol": row.symbol,
            "decision_at": row.decision_at.isoformat(),
            "current_price": str(row.current_price),
            "unrealized_profit": str(row.unrealized_profit),
            "trigger_amount": str(row.trigger_amount),
            "position": row.position_snapshot,
            "formal_research": row.research_context,
        }
        messages: tuple[dict[str, str], ...] = (
            {
                "role": "system",
                "content": (
                    "你是A股模拟持仓退出研究Agent。根据给定数据决定继续持有、分批减仓或卖出，"
                    "不得使用未提供事实，不得承诺收益。只返回一个 JSON 对象，"
                    "不要 Markdown、说明文字或代码块。"
                    "对象字段必须为 action、confidence、summary、ladder、stop_loss_price、risks。"
                    "若结论为继续持有，ladder 必须为空；若结论为分批减仓或卖出，"
                    "ladder 必须为1到5档。"
                    "数量建议会由服务端按T+1、持仓上限和生效交易规则复核。"
                ),
            },
            {"role": "user", "content": canonical_json(evidence).decode("utf-8")},
        )
        request_sha = stable_hash(
            {
                "prompt_version": PROMPT_VERSION,
                "model_config": runtime.config_sha256,
                "model": runtime.research_model,
                "effort": runtime.research_reasoning_effort,
                "messages": messages,
            }
        )
        client = OpenAICompatibleStructuredLLMClient(
            base_url=runtime.base_url,
            api_key=runtime.api_key,
            model=runtime.research_model,
            reasoning_effort=runtime.research_reasoning_effort,
            timeout_seconds=runtime.timeout_seconds,
            max_retries=1,
            cache_policy=runtime.profile_for(runtime.research_model).cache_policy,
        )
        format_repair_used = False

        def record_format_repair(raw_output: str) -> None:
            transition_operation_run(
                session,
                row.operation_run_id,
                status="RUNNING",
                event_type="EXIT_ADVICE_FORMAT_REPAIR_STARTED",
                message="Exit advice output format repair started",
                details={
                    "advice_id": row.advice_id,
                    "output_sha256": sha256_bytes(raw_output.encode("utf-8")),
                    "output_length": len(raw_output),
                },
            )

        def generate() -> AICacheGeneration:
            nonlocal format_repair_used
            analysis, generation, format_repair_used = _generate_exit_advice_with_repair(
                client,
                messages=messages,
                request_sha=request_sha,
                on_repair=record_format_repair,
            )
            metadata = generation.metadata
            return AICacheGeneration(
                response=analysis.model_dump(mode="json"),
                model_name=metadata.model_name,
                reasoning_effort=metadata.reasoning_effort,
                input_tokens=metadata.input_tokens,
                cached_input_tokens=metadata.cached_input_tokens,
                cache_write_tokens=metadata.cache_write_tokens,
                output_tokens=metadata.output_tokens,
                reasoning_tokens=metadata.reasoning_tokens,
                cache_policy=metadata.cache_policy,
            )

        expires_at = now.replace(hour=23, minute=59, second=59, microsecond=0)
        cache_result = AIResultCacheService(session_factory=SessionLocal).get_or_generate(
            user_id=row.user_id,
            purpose="EXIT_ADVICE",
            request_sha256=request_sha,
            prompt_version=PROMPT_VERSION,
            ttl_seconds=max(60, int((expires_at - now).total_seconds())),
            validate=lambda value: ExitAdviceAnalysis.model_validate(value),
            generate=generate,
        )
        analysis = ExitAdviceAnalysis.model_validate(cache_result.response)
        cached = cache_result.row
        cache_hit = cache_result.cache_hit
        input_tokens = cached.input_tokens
        cached_input_tokens = cached.input_tokens if cache_hit else cached.cached_input_tokens
        cache_write_tokens = 0 if cache_hit else cached.cache_write_tokens
        output_tokens = cached.output_tokens
        reasoning_tokens = 0 if cache_hit else cached.reasoning_tokens
        validated = _validate_sell_ladder(session, row, analysis)
        result = {
            **analysis.model_dump(mode="json", exclude={"ladder"}),
            **validated,
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "cache_write_tokens": cache_write_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "cache_hit": cache_hit,
            "cache_hits": cached.hit_count,
            "singleflight_wait_ms": cache_result.singleflight_wait_ms,
        }
        row.status = "SUCCEEDED"
        row.action = analysis.action
        row.result = result
        row.model_name = runtime.research_model
        row.reasoning_effort = runtime.research_reasoning_effort
        row.response_sha256 = stable_hash(result)
        row.cache_hit = cache_hit
        row.completed_at = datetime.now(UTC)
        transition_operation_run(
            session,
            row.operation_run_id,
            status="SUCCEEDED",
            event_type="EXIT_ADVICE_COMPLETED",
            message="Exit advice generation completed",
            details={
                "advice_id": row.advice_id,
                "symbol": row.symbol,
                "format_repair_used": not cache_hit and format_repair_used,
            },
            output_hash=row.response_sha256,
        )
        NotificationService(session).create(
            user_id=row.user_id,
            notification_type="EXIT_ADVICE_COMPLETED",
            severity="HIGH" if row.trigger_type == "STOP_LOSS" else "INFO",
            title=f"{row.symbol} 模拟退出研究已完成",
            body="退出建议已生成，仅供模拟交易方案参考，不会自动卖出。",
            resource_type="EXIT_ADVICE",
            resource_id=row.advice_id,
            payload={"symbol": row.symbol, "action": row.action, "trigger_type": row.trigger_type},
            dedupe_key=f"exit-completed:{row.advice_id}",
        )
        session.commit()
        return result


def _validate_sell_ladder(
    session: Session, row: ExitAdviceRow, analysis: ExitAdviceAnalysis
) -> dict[str, Any]:
    position = row.position_snapshot
    eligibility = position_sellability(
        position, trading_date=row.decision_at.astimezone(SHANGHAI).date()
    )
    blockers: list[str] = list(eligibility.blockers)
    sellable = eligibility.sellable_quantity
    master = session.scalar(
        select(SecurityMaster)
        .where(
            SecurityMaster.symbol == row.symbol,
            SecurityMaster.available_at <= row.decision_at,
            SecurityMaster.effective_from <= row.decision_at.date(),
            (SecurityMaster.effective_to.is_(None))
            | (SecurityMaster.effective_to >= row.decision_at.date()),
            SecurityMaster.source_record_id.not_like("directory-%"),
        )
        .order_by(SecurityMaster.available_at.desc(), SecurityMaster.fetched_at.desc())
        .limit(1)
    )
    if master is None:
        blockers.append("TRADING_RULE_CONTEXT_MISSING")
        step = 100
    else:
        context = RuleContext(
            symbol=row.symbol,
            trading_date=row.decision_at.astimezone(SHANGHAI).date(),
            decision_at=row.decision_at,
            exchange=master.exchange,
            market="A",
            board=master.board,
            security_type="STOCK",
            risk_status="NORMAL",
            is_st=master.is_st,
            listing_days=max(0, (row.decision_at.date() - master.list_date).days),
            listing_session=max(0, (row.decision_at.date() - master.list_date).days),
        )
        try:
            rule = TradingRuleRepository().resolve(session, context)
            step = int(rule.details.get("sell_qty_step", rule.lot_size))
        except Exception:
            blockers.append("TRADING_RULE_UNAVAILABLE")
            step = 100
    remaining = sellable
    ladder: list[dict[str, Any]] = []
    for item in sorted(analysis.ladder, key=lambda value: value.target_price):
        requested = min(item.quantity, remaining)
        quantity = (requested // step) * step if step > 1 else requested
        if requested == remaining and remaining < step:
            quantity = remaining
        if quantity <= 0:
            continue
        price = item.target_price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        ladder.append(
            {
                "target_price": str(price),
                "quantity": quantity,
                "estimated_gross_proceeds": str(
                    (price * quantity).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                ),
                "reason": item.reason,
                "status": "BLOCKED" if blockers else "READY_FOR_CONFIRMATION",
            }
        )
        remaining -= quantity
        if remaining <= 0:
            break
    return {
        "sell_ladder": ladder,
        "sellable_quantity": sellable,
        "execution_blockers": blockers,
        "paper_trade_only": True,
    }


def run_exit_advice_job(advice_id: str) -> dict[str, Any]:
    try:
        return execute_exit_advice(advice_id)
    except OpenAICompatibleError as exc:
        with SessionLocal() as session:
            row = session.get(ExitAdviceRow, advice_id)
            if row is not None:
                _record_exit_failure(
                    session,
                    row,
                    status="UNAVAILABLE",
                    failure_code=_exit_model_failure_code(exc),
                    notification_type="EXIT_ADVICE_UNAVAILABLE",
                    title=f"{row.symbol} 模拟退出研究暂不可用",
                )
        return {}
    except Exception:
        with SessionLocal() as session:
            row = session.get(ExitAdviceRow, advice_id)
            if row is not None:
                _record_exit_failure(
                    session,
                    row,
                    status="FAILED",
                    failure_code="PROCESSING_FAILED",
                    notification_type="EXIT_ADVICE_FAILED",
                    title=f"{row.symbol} 模拟退出研究失败",
                )
        return {}


def _exit_model_failure_code(error: OpenAICompatibleError) -> str:
    """Expose only stable, actionable model failure categories."""

    supported = {
        "MODEL_TIMEOUT",
        "MODEL_RATE_LIMITED",
        "MODEL_GATEWAY_UNAVAILABLE",
        "MODEL_STREAM_INCOMPLETE",
        "MODEL_OUTPUT_INVALID_JSON",
        "MODEL_OUTPUT_SCHEMA_INVALID",
        "MODEL_OUTPUT_REPAIR_FAILED",
    }
    return error.code if error.code in supported else "MODEL_RESPONSE_ERROR"


def _record_exit_failure(
    session: Session,
    row: ExitAdviceRow,
    *,
    status: Literal["FAILED", "UNAVAILABLE"],
    failure_code: str,
    notification_type: str,
    title: str,
) -> None:
    """Persist only stable public failure codes; provider details never leave the worker."""

    row.status = status
    row.error_message = failure_code
    row.result = {"failure_code": failure_code, "paper_trade_only": True}
    row.completed_at = datetime.now(UTC)
    transition_operation_run(
        session,
        row.operation_run_id,
        status=status,
        event_type="EXIT_ADVICE_FAILED",
        message="Exit advice generation failed",
        details={"advice_id": row.advice_id, "failure_code": failure_code},
        error_message=failure_code,
    )
    NotificationService(session).create(
        user_id=row.user_id,
        notification_type=notification_type,
        severity="WARNING",
        title=title,
        body="未生成退出研究；持仓没有被修改或自动卖出。",
        resource_type="EXIT_ADVICE",
        resource_id=row.advice_id,
        payload={"failure_code": failure_code},
        dedupe_key=f"exit-failure:{row.advice_id}:{failure_code}",
    )
    session.commit()


def consume_exit_advice_queue() -> None:
    import redis

    client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    queue = build_exit_advice_queue(client)
    queue.consume_forever(run_exit_advice_job)
