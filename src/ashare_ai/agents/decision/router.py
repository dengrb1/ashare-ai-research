"""Decision router: the single entry point for every decision request.

The router always produces a deterministic factor decision.  ``legacy`` is
accepted as a compatibility alias for ``factor``; ``jev`` is rejected with an
explicit deprecation error so no new Jev decision can be created.

The optional System-2 path is unchanged: it only runs a bounded LLM diagnostic
when confidence is low or the primary provider fails, and it never alters the
factor score or the position.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from ashare_ai.agents.decision.protocols import DecisionProvider
from ashare_ai.core.config import Settings
from ashare_ai.core.hashing import stable_hash

if TYPE_CHECKING:
    from ashare_ai.agents.decision.models import DecisionMode, MarketState, UnifiedDecision
    from ashare_ai.scoring.factor import FactorGenome

logger = logging.getLogger(__name__)

# ``legacy`` is a compatibility alias: a request that asks for it still gets a
# deterministic factor decision.  ``jev`` is only accepted when *reading* a
# historical record, never when creating one.
COMPATIBLE_REQUEST_MODES = frozenset({"factor", "legacy"})
RETIRED_REQUEST_MODES = frozenset({"jev"})


class System2Diagnostic(BaseModel):
    """Bounded, structured output used only for low-confidence diagnostics."""

    summary: str = Field(min_length=1, max_length=2000)
    risk_flags: list[str] = Field(default_factory=list, max_length=12)
    confidence: float = Field(ge=0.0, le=1.0)


async def _llm_system2_dispatch(market_state: MarketState, reason: str) -> System2Diagnostic:
    """Run one optional diagnostic through the configured research model.

    The model receives only the frozen market state and never decides an order.
    Its validated response is logged as a diagnostic event; the deterministic
    factor decision returned by the router remains authoritative.
    """

    from ashare_ai.agents.model_settings import ModelConfigurationService
    from ashare_ai.agents.openai_compatible import OpenAICompatibleStructuredLLMClient
    from ashare_ai.core.system_settings import get_effective_settings
    from ashare_ai.storage.database import SessionLocal

    settings: Settings = get_effective_settings()
    with SessionLocal() as session:
        runtime = ModelConfigurationService(settings).resolve(session)
    if runtime is None or not runtime.enabled:
        raise RuntimeError("System-2 model is not configured")

    client = OpenAICompatibleStructuredLLMClient(
        base_url=runtime.base_url,
        api_key=runtime.api_key,
        model=runtime.research_model,
        reasoning_effort=runtime.research_reasoning_effort,
        timeout_seconds=min(runtime.timeout_seconds, settings.llm_timeout_seconds),
        max_retries=1,
        cache_policy=runtime.profile_for(runtime.research_model).cache_policy,
    )
    try:
        generation = await client.generate_structured(
            schema=System2Diagnostic,
            messages=(
                {
                    "role": "system",
                    "content": (
                        "你是A股研究诊断器。只根据给定的冻结市场状态指出风险和不确定性，"
                        "不得给出买卖指令、目标价或仓位。输出简短中文摘要。"
                    ),
                },
                {
                    "role": "user",
                    "content": market_state.model_dump_json()
                    + f"\n触发原因: {reason}",
                },
            ),
            idempotency_key=stable_hash(
                {"system": "system2-diagnostic-v1", "state": market_state, "reason": reason}
            ),
        )
        output = System2Diagnostic.model_validate(generation.output)
        logger.info(
            "System-2 diagnostic completed",
            extra={
                "symbol": market_state.symbol,
                "trading_date": str(market_state.trading_date),
                "decision_at": market_state.available_at.isoformat(),
                "reason": reason,
                "confidence": output.confidence,
                "risk_flag_count": len(output.risk_flags),
            },
        )
        return output
    finally:
        await client.aclose()


def normalize_request_mode(mode: str) -> str:
    """Map a requested mode onto the only mode that creates decisions.

    ``legacy`` is a compatibility alias for ``factor``.  ``jev`` is retired and
    raises so a caller cannot silently receive a decision it did not ask for.
    """

    normalized = (mode or "factor").strip().casefold()
    if normalized in RETIRED_REQUEST_MODES:
        raise ValueError(
            "decision mode 'jev' has been removed; use 'factor' "
            "(the deterministic factor scoring formula)"
        )
    if normalized not in COMPATIBLE_REQUEST_MODES:
        raise ValueError(f"Unsupported decision mode: {mode}")
    return "factor"


class DecisionRouter:
    """Deterministic factor router with a frozen-genome fallback.

    The primary provider applies the active :class:`FactorGenome`.  If it
    cannot produce a decision, the router falls back to the frozen default
    genome so a research run degrades to the baseline formula instead of
    failing outright.
    """

    def __init__(
        self,
        mode: DecisionMode = "factor",
        fallback_enabled: bool = True,
        genome: FactorGenome | None = None,
        confidence_threshold: float = 0.60,
        system2_enabled: bool = True,
        system2_provider: DecisionProvider | None = None,
        system2_dispatch: Callable[[MarketState, str], Awaitable[None]] | None = None,
        system2_enqueue: Callable[[MarketState, str], str] | None = None,
    ):
        self._mode = normalize_request_mode(mode)
        self._fallback_enabled = fallback_enabled
        self._genome = genome
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be between 0 and 1")
        self._confidence_threshold = confidence_threshold
        self._system2_provider = system2_provider
        self._system2_dispatch = system2_dispatch if system2_enabled else None
        self._system2_enqueue = system2_enqueue
        if (
            system2_enabled
            and self._system2_provider is None
            and self._system2_dispatch is None
            and self._system2_enqueue is None
        ):
            from ashare_ai.orchestration.system2_jobs import enqueue_system2_diagnostic

            self._system2_enqueue = enqueue_system2_diagnostic
        self._system2_enabled = system2_enabled
        self._system2_tasks: set[asyncio.Task[None]] = set()
        self._system2_state = "IDLE"
        self._system2_job_id: str | None = None

        self._primary_provider: DecisionProvider | None = None
        self._fallback_provider: DecisionProvider | None = None

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def system2_state(self) -> str:
        """Current best-effort System-2 diagnostic state for the UI."""
        return self._system2_state

    @property
    def system2_job_id(self) -> str | None:
        return self._system2_job_id

    def _get_provider(self, mode: str) -> DecisionProvider:
        """Build the provider for a normalized mode."""

        from ashare_ai.agents.decision.factor import FactorDecisionProvider

        if mode == "factor":
            return FactorDecisionProvider(self._genome)
        raise ValueError(f"Unsupported decision mode: {mode}")

    async def predict(self, market_state: MarketState) -> UnifiedDecision:
        """Execute one decision, handling the frozen-genome fallback.

        Raises:
            RuntimeError: the primary provider failed and fallback is disabled.
            ValueError: the requested mode is retired or unknown.
        """

        if self._primary_provider is None:
            self._primary_provider = self._get_provider(self._mode)

        try:
            decision = await self._primary_provider.predict(market_state)
            logger.info(
                "Decision generated",
                extra={
                    "mode": self._mode,
                    "symbol": market_state.symbol,
                    "trading_date": str(market_state.trading_date),
                    "action": decision.action,
                    "risk": decision.risk,
                    "position": decision.position,
                },
            )
            if self._decision_confidence(decision) < self._confidence_threshold:
                self._schedule_system2(market_state, "LOW_CONFIDENCE")
            return decision

        except Exception as exc:
            logger.error(
                "Primary decision provider failed",
                extra={
                    "mode": self._mode,
                    "symbol": market_state.symbol,
                    "trading_date": str(market_state.trading_date),
                    "error": str(exc),
                    "fallback_enabled": self._fallback_enabled,
                },
                exc_info=True,
            )

            self._schedule_system2(market_state, "FACTOR_UNAVAILABLE")
            if not self._fallback_enabled:
                raise RuntimeError(
                    f"Decision provider ({self._mode}) failed and fallback is disabled. "
                    f"Enable fallback in config or fix the primary provider. Error: {exc}"
                ) from exc

            logger.warning(
                "Falling back to the frozen default factor genome",
                extra={"primary_mode": self._mode, "symbol": market_state.symbol},
            )

            if self._fallback_provider is None:
                self._fallback_provider = self._get_provider("factor")

            try:
                decision = await self._fallback_provider.predict(market_state)
                logger.info(
                    "Fallback decision generated",
                    extra={
                        "primary_mode": self._mode,
                        "symbol": market_state.symbol,
                        "trading_date": str(market_state.trading_date),
                        "action": decision.action,
                    },
                )
                return decision
            except Exception as fallback_exc:
                logger.critical(
                    "Fallback decision provider also failed",
                    extra={
                        "primary_mode": self._mode,
                        "symbol": market_state.symbol,
                        "error": str(fallback_exc),
                    },
                    exc_info=True,
                )
                raise RuntimeError(
                    f"Both primary ({self._mode}) and fallback (factor) providers failed. "
                    f"Primary error: {exc}. Fallback error: {fallback_exc}"
                ) from fallback_exc

    @staticmethod
    def _decision_confidence(decision: UnifiedDecision) -> float:
        action = decision.probabilities.action
        risk = decision.probabilities.risk
        action_score = getattr(action, decision.action)
        risk_score = getattr(risk, decision.risk)
        direction_score = max(decision.probabilities.direction_1d.model_dump().values())
        return float((action_score + risk_score + direction_score) / 3.0)

    def _schedule_system2(self, market_state: MarketState, reason: str) -> None:
        if not self._system2_enabled:
            self._system2_state = "DISABLED"
            return
        if (
            self._system2_provider is None
            and self._system2_dispatch is None
            and self._system2_enqueue is None
        ):
            self._system2_state = "UNAVAILABLE"
            return
        self._system2_state = "QUEUED"
        if (
            self._system2_enqueue is not None
            and self._system2_provider is None
            and self._system2_dispatch is None
        ):
            try:
                self._system2_job_id = self._system2_enqueue(market_state, reason)
            except Exception:
                self._system2_state = "FAILED"
                logger.exception("System-2 diagnostic could not be queued")
            return
        task = asyncio.create_task(self._run_system2(market_state, reason))
        self._system2_tasks.add(task)
        task.add_done_callback(self._system2_tasks.discard)

    async def _run_system2(self, market_state: MarketState, reason: str) -> None:
        self._system2_state = "RUNNING"
        try:
            if self._system2_dispatch is not None:
                await self._system2_dispatch(market_state, reason)
            elif self._system2_provider is not None:
                await self._system2_provider.predict(market_state)
            self._system2_state = "SUCCEEDED"
        except Exception:
            self._system2_state = "FAILED"
            logger.exception(
                "System-2 diagnostic failed",
                extra={"symbol": market_state.symbol, "reason": reason},
            )


def create_decision_router(
    mode: DecisionMode = "factor",
    fallback_enabled: bool = True,
    genome: FactorGenome | None = None,
    confidence_threshold: float = 0.60,
    system2_enabled: bool = True,
    system2_provider: DecisionProvider | None = None,
    system2_dispatch: Callable[[MarketState, str], Awaitable[None]] | None = None,
    system2_enqueue: Callable[[MarketState, str], str] | None = None,
) -> DecisionRouter:
    """Factory function: create the deterministic factor decision router."""

    return DecisionRouter(
        mode=mode,
        fallback_enabled=fallback_enabled,
        genome=genome,
        confidence_threshold=confidence_threshold,
        system2_enabled=system2_enabled,
        system2_provider=system2_provider,
        system2_dispatch=system2_dispatch,
        system2_enqueue=system2_enqueue,
    )
