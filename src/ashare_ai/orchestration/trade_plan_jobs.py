from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ashare_ai.agents.model_settings import ModelConfigurationService
from ashare_ai.agents.openai_compatible import OpenAICompatibleStructuredLLMClient
from ashare_ai.backtest.trade_plan import (
    DeterministicTradePlan,
    OptimizedStrategy,
    TradePlanCandidateInput,
    TradePlanPositionInput,
    build_deterministic_trade_plan,
    optimize_trade_strategy,
)
from ashare_ai.core.config import get_settings
from ashare_ai.core.hashing import canonical_json, stable_hash
from ashare_ai.core.security import safe_error_message
from ashare_ai.observability.audit import AuditLogger
from ashare_ai.orchestration.builtin_backtest import read_backtest_bundle
from ashare_ai.orchestration.daily import flow
from ashare_ai.orchestration.redis_queue import RedisLeasedQueue
from ashare_ai.portfolio.user_assets import UserAssetService
from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.models import (
    CandidateRow,
    ScoreRow,
    SnapshotManifestRow,
    TradePlanRow,
)
from ashare_ai.storage.objects import LocalObjectStore

QUEUE_NAME = "ashare:trade-plan:pending"
PROCESSING_QUEUE_NAME = "ashare:trade-plan:processing"
PROMPT_VERSION = "trade-plan-explanation-v1"


class TradePlanExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_logic: str = Field(min_length=1, max_length=2000)
    exit_logic: str = Field(min_length=1, max_length=2000)
    key_evidence: list[str] = Field(default_factory=list, max_length=10)
    risks: list[str] = Field(default_factory=list, max_length=10)


def enqueue_trade_plan(plan_id: str, redis_url: str | None = None) -> None:
    import redis

    client = redis.Redis.from_url(redis_url or get_settings().redis_url, decode_responses=True)
    RedisLeasedQueue(
        client,
        pending=QUEUE_NAME,
        processing=PROCESSING_QUEUE_NAME,
        lease_seconds=get_settings().worker_lease_seconds,
    ).enqueue(plan_id)


def execute_trade_plan_job(
    plan_id: str,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
) -> DeterministicTradePlan:
    with session_factory() as session:
        row = session.get(TradePlanRow, plan_id)
        if row is None:
            raise KeyError(plan_id)
        manifests = list(
            session.scalars(
                select(SnapshotManifestRow).where(
                    SnapshotManifestRow.snapshot_id.in_(row.snapshot_ids)
                )
            ).all()
        )
        by_id = {item.snapshot_id: item for item in manifests}
        if set(by_id) != set(row.snapshot_ids) or any(
            item.status != "COMMITTED" for item in manifests
        ):
            raise ValueError("trade plan requires all input snapshots to be COMMITTED")
        row.status = "RUNNING"
        row.started_at = datetime.now(UTC)
        AuditLogger(session).record(
            row.run_id,
            "TRADE_PLAN_STARTED",
            "Trade Plan worker started deterministic optimization",
            details={"plan_id": plan_id, "snapshot_ids": row.snapshot_ids},
        )
        snapshot_uris = {item.snapshot_id: item.parquet_uri for item in manifests}
        expected_hashes = {
            item.snapshot_id: str(item.details.get("parquet_file_sha256", ""))
            for item in manifests
        }
        session.commit()

    try:
        bundle = read_backtest_bundle(snapshot_uris, expected_hashes)
        if any(item.price_basis != "RAW" for item in bundle.bars):
            raise ValueError("trade plan optimization requires unadjusted RAW prices")
        with session_factory() as session:
            row = session.get(TradePlanRow, plan_id)
            if row is None:
                raise KeyError(plan_id)
            candidates = list(
                session.scalars(
                    select(CandidateRow).where(
                        CandidateRow.run_id == row.run_id,
                        CandidateRow.symbol.in_(row.symbols),
                    )
                ).all()
            )
            scores = {
                item.symbol: item
                for item in session.scalars(
                    select(ScoreRow).where(
                        ScoreRow.run_id == row.run_id,
                        ScoreRow.symbol.in_(row.symbols),
                    )
                ).all()
            }
            assets = UserAssetService(session).get(row.user_id)
            manifest_details = next(
                (item.details for item in manifests if item.dataset == "backtest_bundle"), {}
            )

        calendar = bundle.trading_calendar
        bars_by_symbol = {
            symbol: tuple(item for item in bundle.bars if item.symbol == symbol)
            for symbol in row.symbols
        }
        rules_by_symbol = {
            symbol: {
                item.trading_date: item.rule for item in bundle.rules if item.symbol == symbol
            }
            for symbol in row.symbols
        }
        adv_by_symbol = {
            symbol: {
                item.trading_date: item.value
                for item in bundle.adv_amounts
                if item.symbol == symbol
            }
            for symbol in row.symbols
        }
        volatility_by_symbol = {
            symbol: {
                item.trading_date: float(item.value)
                for item in bundle.volatilities
                if item.symbol == symbol
            }
            for symbol in row.symbols
        }
        strategies: dict[str, OptimizedStrategy] = {}
        optimizer_policy = row.request_payload.get("optimizer_policy", {})
        optimizer_policy = optimizer_policy if isinstance(optimizer_policy, dict) else {}
        for symbol in row.symbols:
            strategies[symbol] = optimize_trade_strategy(
                symbol=symbol,
                trading_calendar=calendar,
                bars=bars_by_symbol[symbol],
                rules=rules_by_symbol[symbol],
                adv_amounts=adv_by_symbol[symbol],
                volatilities=volatility_by_symbol[symbol],
                execution_config=bundle.config.execution,
                maximum_drawdown=float(optimizer_policy.get("maximum_drawdown", 0.12)),
                minimum_completed_trades=int(
                    optimizer_policy.get("minimum_completed_trades", 5)
                ),
                maximum_history_sessions=int(
                    optimizer_policy.get("history_sessions", 240)
                ),
                training_sessions=int(optimizer_policy.get("training_sessions", 160)),
                validation_sessions=int(optimizer_policy.get("validation_sessions", 80)),
                entry_discounts=tuple(
                    Decimal(str(value))
                    for value in optimizer_policy.get("entry_discounts", [0, 0.01, 0.02])
                ),
                take_profits=tuple(
                    Decimal(str(value))
                    for value in optimizer_policy.get("take_profits", [0.08, 0.12, 0.16])
                ),
                stop_losses=tuple(
                    Decimal(str(value))
                    for value in optimizer_policy.get("stop_losses", [0.05, 0.08, 0.10])
                ),
                trailing_stops=tuple(
                    Decimal(str(value))
                    for value in optimizer_policy.get("trailing_stops", [0.05, 0.08])
                ),
                maximum_holding_options=tuple(
                    int(value)
                    for value in optimizer_policy.get(
                        "maximum_holding_sessions", [10, 20, 40, 60]
                    )
                ),
                entry_valid_sessions=int(optimizer_policy.get("entry_valid_sessions", 3)),
                entry_step_sessions=int(optimizer_policy.get("entry_step_sessions", 10)),
            )
        reference_prices = {
            symbol: max(
                (item for item in bars_by_symbol[symbol] if item.trading_date <= row.trading_date),
                key=lambda item: item.trading_date,
            ).close
            for symbol in row.symbols
        }
        effective_rules = {
            symbol: rules_by_symbol[symbol][
                max(value for value in rules_by_symbol[symbol] if value <= row.trading_date)
            ]
            for symbol in row.symbols
        }
        future_dates = tuple(
            date.fromisoformat(value)
            for value in manifest_details.get("future_trading_dates", [])
        )
        saved_positions = [
            TradePlanPositionInput(
                symbol=str(item.get("symbol", "")),
                quantity=int(item.get("quantity", 0)),
            )
            for item in assets.get("positions", [])
            if item.get("symbol") and int(item.get("quantity", 0)) >= 0
        ]
        frozen_position_value = sum(
            (
                reference_prices.get(item.symbol, _position_cost(assets, item.symbol))
                * item.quantity
                for item in saved_positions
            ),
            Decimal("0"),
        )
        configured_total = assets.get("total_assets")
        total_assets = (
            Decimal(str(configured_total))
            if configured_total is not None
            else max(Decimal("1000000"), frozen_position_value)
        )
        available_cash = max(Decimal("0"), total_assets - frozen_position_value)
        requested_budget = row.budget_override or available_cash
        candidate_by_symbol = {item.symbol: item for item in candidates}
        candidate_inputs = []
        for symbol in row.symbols:
            candidate = candidate_by_symbol[symbol]
            latest_volatility = max(
                volatility_by_symbol[symbol].items(), key=lambda item: item[0]
            )[1]
            candidate_inputs.append(
                TradePlanCandidateInput(
                    symbol=symbol,
                    industry_code=candidate.industry_code,
                    volatility=max(latest_volatility, 0.01),
                    event_risk_multiplier=candidate.event_risk_multiplier,
                )
            )
        result = build_deterministic_trade_plan(
            trading_date=row.trading_date,
            decision_at=row.decision_at.isoformat(),
            candidates=candidate_inputs,
            positions=saved_positions,
            strategies=strategies,
            reference_prices=reference_prices,
            effective_rules=effective_rules,
            future_trading_dates=future_dates,
            total_assets=total_assets,
            available_cash=available_cash,
            requested_budget=Decimal(requested_budget),
            score_exit_threshold=Decimal(
                str(optimizer_policy.get("score_exit_threshold", 60))
            ),
        )
        explanation = _generate_ai_explanation(
            plan=result,
            scores=scores,
            model_reference=row.model_configuration,
            session_factory=session_factory,
        )
        output_payload = {
            "deterministic_result": result.model_dump(mode="json"),
            "ai_explanation": explanation,
        }
        output_hash = stable_hash(output_payload)
        store = LocalObjectStore(Path(get_settings().lake_root).parent / "objects")
        object_uri, object_sha256 = store.put(
            canonical_json(output_payload), content_type="application/json"
        )
    except Exception as exc:
        mark_trade_plan_failed(plan_id, exc, session_factory=session_factory)
        raise

    with session_factory() as session:
        completed = session.get(TradePlanRow, plan_id)
        if completed is None:
            raise KeyError(plan_id)
        completed.status = "SUCCEEDED"
        completed.deterministic_result = result.model_dump(mode="json")
        completed.ai_explanation = explanation
        completed.output_hash = output_hash
        completed.object_uri = object_uri
        completed.object_sha256 = object_sha256
        completed.active_trade_plan_key = None
        completed.completed_at = datetime.now(UTC)
        AuditLogger(session).record(
            completed.run_id,
            "TRADE_PLAN_COMPLETED",
            "Deterministic Trade Plan completed",
            details={
                "plan_id": plan_id,
                "output_hash": output_hash,
                "outcome": result.outcome.value,
                "ai_status": explanation.get("status"),
            },
        )
        session.commit()
    return result


def mark_trade_plan_failed(
    plan_id: str,
    error: Exception,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
) -> None:
    with session_factory() as session:
        row = session.get(TradePlanRow, plan_id)
        if row is None:
            return
        row.status = "FAILED"
        row.error_message = safe_error_message(error)
        row.active_trade_plan_key = None
        row.completed_at = datetime.now(UTC)
        AuditLogger(session).record(
            row.run_id,
            "TRADE_PLAN_FAILED",
            "Trade Plan generation failed",
            severity="ERROR",
            details={"plan_id": plan_id, "error_type": type(error).__name__},
        )
        session.commit()


def _generate_ai_explanation(
    *,
    plan: DeterministicTradePlan,
    scores: dict[str, ScoreRow],
    model_reference: dict[str, Any] | None,
    session_factory: Callable[[], Session],
) -> dict[str, Any]:
    if not model_reference or not model_reference.get("enabled", False):
        return {"status": "UNAVAILABLE", "message": "AI解释未生成"}
    try:
        with session_factory() as session:
            runtime = ModelConfigurationService().resolve_pinned(session, model_reference)
        if runtime is None:
            return {"status": "UNAVAILABLE", "message": "AI解释未生成"}
        client = OpenAICompatibleStructuredLLMClient(
            base_url=runtime.base_url,
            api_key=runtime.api_key,
            model=runtime.research_model,
            reasoning_effort=runtime.research_reasoning_effort,
            timeout_seconds=runtime.timeout_seconds,
            max_retries=1,
        )
        items: dict[str, Any] = {}
        for symbol_plan in plan.symbol_plans:
            score = scores.get(symbol_plan.symbol)
            evidence = {
                "symbol": symbol_plan.symbol,
                "final_score": score.total_score if score is not None else None,
                "base_total_score": score.base_total_score if score is not None else None,
                "dividend_bonus": score.dividend_bonus if score is not None else None,
                "event_risk_multiplier": (
                    score.event_risk_multiplier if score is not None else None
                ),
                "deterministic_plan": symbol_plan.model_dump(mode="json"),
            }
            messages = (
                {
                    "role": "system",
                    "content": (
                        "你是模拟交易方案解释 Agent。只能解释给定确定性数值，不得修改、"
                        "重算或新增数量、价格、仓位、止盈止损和历史指标；禁止承诺收益。"
                        "只返回要求的文本字段。"
                    ),
                },
                {
                    "role": "user",
                    "content": canonical_json(evidence).decode("utf-8"),
                },
            )
            request_hash = stable_hash(
                {"prompt_version": PROMPT_VERSION, "messages": messages}
            )
            generation = asyncio.run(
                client.generate_structured(
                    schema=TradePlanExplanation,
                    messages=messages,
                    idempotency_key=request_hash,
                )
            )
            explanation = TradePlanExplanation.model_validate(generation.output)
            items[symbol_plan.symbol] = {
                **explanation.model_dump(mode="json"),
                "request_sha256": request_hash,
                "response_sha256": stable_hash(generation.output),
                "model": generation.metadata.model_name,
                "reasoning_effort": generation.metadata.reasoning_effort,
            }
        return {
            "status": "SUCCEEDED",
            "prompt_version": PROMPT_VERSION,
            "items": items,
            "model_configuration": model_reference,
        }
    except Exception as exc:
        return {
            "status": "UNAVAILABLE",
            "message": "AI解释未生成",
            "error_type": type(exc).__name__,
        }


def _position_cost(assets: dict[str, Any], symbol: str) -> Decimal:
    for item in assets.get("positions", []):
        if item.get("symbol") == symbol:
            return Decimal(str(item.get("cost", 0)))
    return Decimal("0")


@flow(name="ashare-trade-plan", log_prints=True)
def run_trade_plan_job(plan_id: str) -> dict[str, Any]:
    try:
        return execute_trade_plan_job(plan_id).model_dump(mode="json")
    except Exception as exc:
        mark_trade_plan_failed(plan_id, exc)
        raise


def consume_trade_plan_queue() -> None:
    import redis

    client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    queue = RedisLeasedQueue(
        client,
        pending=QUEUE_NAME,
        processing=PROCESSING_QUEUE_NAME,
        lease_seconds=get_settings().worker_lease_seconds,
    )
    queue.consume_forever(run_trade_plan_job)
