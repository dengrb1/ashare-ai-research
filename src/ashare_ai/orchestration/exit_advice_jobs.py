from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, Literal

import redis
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ashare_ai.agents.model_settings import ModelConfigurationService
from ashare_ai.agents.openai_compatible import OpenAICompatibleStructuredLLMClient
from ashare_ai.core.config import get_settings
from ashare_ai.core.hashing import canonical_json, stable_hash
from ashare_ai.core.time import SHANGHAI
from ashare_ai.market.service import get_market_data_service
from ashare_ai.orchestration.redis_queue import RedisLeasedQueue
from ashare_ai.orchestration.research_schedule import FreeExchangeCalendar
from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.models import (
    AIResponseCacheRow,
    ExitAdviceRow,
    JobRun,
    ScoreRow,
    SecurityMaster,
    UserAccount,
    UserAssetState,
)
from ashare_ai.trading.rules import RuleContext, TradingRuleRepository

QUEUE_NAME = "ashare:exit-advice:pending"
PROCESSING_QUEUE_NAME = "ashare:exit-advice:processing"
PROMPT_VERSION = "exit-advice-v1"


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
    ladder: list[ExitLadderItem] = Field(min_length=1, max_length=5)
    stop_loss_price: Decimal | None = Field(default=None, gt=0)
    risks: list[str] = Field(default_factory=list, max_length=8)


def enqueue_exit_advice(advice_id: str, redis_url: str | None = None) -> None:
    client = redis.Redis.from_url(redis_url or get_settings().redis_url, decode_responses=True)
    RedisLeasedQueue(
        client,
        pending=QUEUE_NAME,
        processing=PROCESSING_QUEUE_NAME,
        lease_seconds=get_settings().worker_lease_seconds,
    ).enqueue(advice_id)


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
    pairs: list[tuple[UserAssetState, dict[str, Any], Decimal]] = []
    for state in states:
        for position in state.positions:
            raw_trigger = position.get("profit_trigger_amount") or state.default_profit_trigger
            if not raw_trigger:
                continue
            pairs.append((state, position, Decimal(str(raw_trigger))))
    symbols = sorted({str(position.get("symbol")) for _, position, _ in pairs})
    if not symbols:
        return {"state": "READY", "queued": []}
    quotes = {item["symbol"]: item for item in get_market_data_service().quotes(symbols)}
    queued: list[str] = []
    for state, position, trigger in pairs:
        symbol = str(position.get("symbol"))
        quote = quotes.get(symbol)
        if quote is None or not quote.get("price"):
            continue
        price = Decimal(str(quote["price"]))
        cost = Decimal(str(position.get("cost", 0)))
        quantity = int(position.get("quantity", 0))
        profit = ((price - cost) * quantity).quantize(Decimal("0.01"))
        if profit <= trigger:
            continue
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
                "trigger": str(trigger),
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
                trigger_amount=trigger,
                position_snapshot=position_snapshot,
                research_context=context,
                prompt_version=PROMPT_VERSION,
                input_hash=input_hash,
                cache_hit=False,
                created_at=datetime.now(UTC),
            )
            session.add(row)
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
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=SHANGHAI)
    return fallback


def _latest_research_context(user_id: str, symbol: str, decision_at: datetime) -> dict[str, Any]:
    with SessionLocal() as session:
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
        session.commit()
        reference = row.research_context.get("model_configuration")
        runtime = (
            ModelConfigurationService().resolve_pinned(session, reference)
            if isinstance(reference, dict)
            else None
        )
        if runtime is None:
            row.status = "UNAVAILABLE"
            row.error_message = "AI模型未配置"
            row.completed_at = now
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
        messages = (
            {
                "role": "system",
                "content": (
                    "你是A股模拟持仓退出研究Agent。根据给定数据决定继续持有、分批减仓或卖出，"
                    "并给出1到5档目标价格和每档卖出股数。不得使用未提供事实，不得承诺收益。"
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
        cached = session.scalar(
            select(AIResponseCacheRow).where(
                AIResponseCacheRow.user_id == row.user_id,
                AIResponseCacheRow.purpose == "EXIT_ADVICE",
                AIResponseCacheRow.request_sha256 == request_sha,
                AIResponseCacheRow.expires_at > now,
            )
        )
        if cached is not None:
            analysis = ExitAdviceAnalysis.model_validate(cached.response)
            cached.hit_count += 1
            cached.last_hit_at = now
            cache_hit = True
            input_tokens = output_tokens = 0
        else:
            client = OpenAICompatibleStructuredLLMClient(
                base_url=runtime.base_url,
                api_key=runtime.api_key,
                model=runtime.research_model,
                reasoning_effort=runtime.research_reasoning_effort,
                timeout_seconds=runtime.timeout_seconds,
                max_retries=1,
            )
            generation = asyncio.run(
                client.generate_structured(
                    schema=ExitAdviceAnalysis,
                    messages=messages,
                    idempotency_key=request_sha,
                )
            )
            analysis = ExitAdviceAnalysis.model_validate(generation.output)
            input_tokens = generation.metadata.input_tokens
            output_tokens = generation.metadata.output_tokens
            cache_hit = False
            session.add(
                AIResponseCacheRow(
                    user_id=row.user_id,
                    purpose="EXIT_ADVICE",
                    request_sha256=request_sha,
                    response_sha256=stable_hash(generation.output),
                    model_name=generation.metadata.model_name,
                    reasoning_effort=generation.metadata.reasoning_effort,
                    prompt_version=PROMPT_VERSION,
                    response=generation.output,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    created_at=now,
                    expires_at=now.replace(hour=23, minute=59, second=59, microsecond=0),
                )
            )
        validated = _validate_sell_ladder(session, row, analysis)
        result = {
            **analysis.model_dump(mode="json", exclude={"ladder"}),
            **validated,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        row.status = "SUCCEEDED"
        row.action = analysis.action
        row.result = result
        row.model_name = runtime.research_model
        row.reasoning_effort = runtime.research_reasoning_effort
        row.response_sha256 = stable_hash(result)
        row.cache_hit = cache_hit
        row.completed_at = datetime.now(UTC)
        session.commit()
        return result


def _validate_sell_ladder(
    session: Session, row: ExitAdviceRow, analysis: ExitAdviceAnalysis
) -> dict[str, Any]:
    position = row.position_snapshot
    total = int(position.get("quantity", 0))
    acquired_raw = position.get("acquired_on")
    blockers: list[str] = []
    if not acquired_raw:
        blockers.append("MISSING_ACQUIRED_ON")
        sellable = 0
    else:
        acquired_on = date.fromisoformat(str(acquired_raw))
        sellable = total if acquired_on < row.decision_at.astimezone(SHANGHAI).date() else 0
        if sellable == 0:
            blockers.append("T1_NOT_SELLABLE")
    master = session.scalar(
        select(SecurityMaster)
        .where(
            SecurityMaster.symbol == row.symbol,
            SecurityMaster.available_at <= row.decision_at,
            SecurityMaster.effective_from <= row.decision_at.date(),
        )
        .order_by(SecurityMaster.available_at.desc())
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
    except Exception as exc:
        with SessionLocal() as session:
            row = session.get(ExitAdviceRow, advice_id)
            if row is not None:
                row.status = "FAILED"
                row.error_message = type(exc).__name__
                row.completed_at = datetime.now(UTC)
                session.commit()
        raise


def consume_exit_advice_queue() -> None:
    client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    queue = RedisLeasedQueue(
        client,
        pending=QUEUE_NAME,
        processing=PROCESSING_QUEUE_NAME,
        lease_seconds=get_settings().worker_lease_seconds,
    )
    queue.consume_forever(run_exit_advice_job)
