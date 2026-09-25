from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from ashare_ai.agents.decision.protocols import DecisionProvider
from ashare_ai.core.config import Settings
from ashare_ai.core.hashing import stable_hash

if TYPE_CHECKING:
    from ashare_ai.agents.decision.models import DecisionMode, MarketState, UnifiedDecision

logger = logging.getLogger(__name__)


class System2Diagnostic(BaseModel):
    """Bounded, structured output used only for low-confidence diagnostics."""

    summary: str = Field(min_length=1, max_length=2000)
    risk_flags: list[str] = Field(default_factory=list, max_length=12)
    confidence: float = Field(ge=0.0, le=1.0)


async def _llm_system2_dispatch(market_state: MarketState, reason: str) -> System2Diagnostic:
    """Run one optional diagnostic through the configured research model.

    The model receives only the frozen market state and never decides an order.
    Its validated response is logged as a diagnostic event; the deterministic
    Jev/legacy decision returned by the router remains authoritative.
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


class DecisionRouter:
    """
    决策模式路由器，根据配置选择 Legacy 或 Jev Provider。

    处理：
    - mode 校验
    - 模型加载失败处理
    - fallback 逻辑
    - 统一的审计记录
    """

    def __init__(
        self,
        mode: DecisionMode = "jev",
        fallback_enabled: bool = True,
        jev_model_dir: Path | None = None,
        jev_model_version: str = "jev-baseline-v1",
        jev_checkpoint: Path | None = None,
        jev_device: str = "auto",
        jev_backend: str = "local",
        jev_live_base_url: str | None = None,
        jev_live_api_key: str | None = None,
        jev_live_model: str = "jev-live",
        jev_live_endpoint: str = "/v1/decisions",
        jev_live_timeout_seconds: float = 30.0,
        confidence_threshold: float = 0.60,
        system2_enabled: bool = True,
        system2_provider: DecisionProvider | None = None,
        system2_dispatch: Callable[[MarketState, str], Awaitable[None]] | None = None,
        system2_enqueue: Callable[[MarketState, str], str] | None = None,
    ):
        """
        初始化决策路由器。

        Args:
            mode: 决策模式 legacy 或 jev
            fallback_enabled: Jev 异常时是否回退到 Legacy
            jev_model_dir: Jev 模型目录
            jev_model_version: Jev 模型版本
            jev_checkpoint: Jev checkpoint 路径
            jev_device: Jev 运行设备
        """
        self._mode = mode
        self._fallback_enabled = fallback_enabled
        self._jev_model_dir = jev_model_dir or Path("data/models/jev")
        self._jev_model_version = jev_model_version
        self._jev_checkpoint = jev_checkpoint
        self._jev_device = jev_device
        self._jev_backend = jev_backend
        self._jev_live_base_url = jev_live_base_url
        self._jev_live_api_key = jev_live_api_key
        self._jev_live_model = jev_live_model
        self._jev_live_endpoint = jev_live_endpoint
        self._jev_live_timeout_seconds = jev_live_timeout_seconds
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
    def system2_state(self) -> str:
        """Current best-effort System-2 diagnostic state for the UI."""
        return self._system2_state

    @property
    def system2_job_id(self) -> str | None:
        return self._system2_job_id

    def _get_provider(self, mode: DecisionMode) -> DecisionProvider:
        """根据模式创建 Provider"""
        if mode == "legacy":
            from ashare_ai.agents.decision.legacy import LegacyDecisionProvider

            return LegacyDecisionProvider()
        elif mode == "jev":
            if self._jev_backend == "live":
                from ashare_ai.agents.decision.jev_live import JevLiveDecisionProvider

                return JevLiveDecisionProvider(
                    base_url=self._jev_live_base_url or "",
                    api_key=self._jev_live_api_key or "",
                    model=self._jev_live_model,
                    endpoint=self._jev_live_endpoint,
                    timeout_seconds=self._jev_live_timeout_seconds,
                )
            from ashare_ai.agents.decision.jev import JevDecisionProvider

            return JevDecisionProvider(
                model_dir=self._jev_model_dir,
                model_version=self._jev_model_version,
                checkpoint_path=self._jev_checkpoint,
                device=self._jev_device,
            )
        else:
            raise ValueError(f"Unsupported decision mode: {mode}")

    async def predict(self, market_state: MarketState) -> UnifiedDecision:
        """
        执行决策推理，处理 fallback 逻辑。

        Args:
            market_state: 市场状态输入

        Returns:
            UnifiedDecision: 统一决策输出

        Raises:
            RuntimeError: 主 Provider 失败且 fallback 未开启
            ValueError: 输入非法
        """
        # 延迟初始化主 Provider
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
            # 记录主 Provider 失败
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

            self._schedule_system2(market_state, "JEV_UNAVAILABLE")
            # 如果 fallback 未开启，直接抛出异常
            if not self._fallback_enabled:
                raise RuntimeError(
                    f"Decision provider ({self._mode}) failed and fallback is disabled. "
                    f"Enable fallback in config or fix the primary provider. Error: {exc}"
                ) from exc

            # fallback 到 Legacy
            logger.warning(
                "Falling back to legacy decision provider",
                extra={
                    "primary_mode": self._mode,
                    "fallback_mode": "legacy",
                    "symbol": market_state.symbol,
                },
            )

            if self._fallback_provider is None:
                self._fallback_provider = self._get_provider("legacy")

            try:
                decision = await self._fallback_provider.predict(market_state)
                logger.info(
                    "Fallback decision generated",
                    extra={
                        "primary_mode": self._mode,
                        "fallback_mode": "legacy",
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
                        "fallback_mode": "legacy",
                        "symbol": market_state.symbol,
                        "error": str(fallback_exc),
                    },
                    exc_info=True,
                )
                raise RuntimeError(
                    f"Both primary ({self._mode}) and fallback (legacy) providers failed. "
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
    mode: DecisionMode = "jev",
    fallback_enabled: bool = True,
    jev_model_dir: Path | None = None,
    jev_model_version: str = "jev-baseline-v1",
    jev_checkpoint: Path | None = None,
    jev_device: str = "auto",
    jev_backend: str = "local",
    jev_live_base_url: str | None = None,
    jev_live_api_key: str | None = None,
    jev_live_model: str = "jev-live",
    jev_live_endpoint: str = "/v1/decisions",
    jev_live_timeout_seconds: float = 30.0,
    confidence_threshold: float = 0.60,
    system2_enabled: bool = True,
        system2_provider: DecisionProvider | None = None,
        system2_dispatch: Callable[[MarketState, str], Awaitable[None]] | None = None,
        system2_enqueue: Callable[[MarketState, str], str] | None = None,
) -> DecisionRouter:
    """
    工厂函数：创建决策路由器。

    Args:
        mode: 决策模式
        fallback_enabled: 是否启用 fallback
        jev_model_dir: Jev 模型目录
        jev_model_version: Jev 模型版本
        jev_checkpoint: Jev checkpoint 路径
        jev_device: Jev 设备

    Returns:
        DecisionRouter 实例
    """
    return DecisionRouter(
        mode=mode,
        fallback_enabled=fallback_enabled,
        jev_model_dir=jev_model_dir,
        jev_model_version=jev_model_version,
        jev_checkpoint=jev_checkpoint,
        jev_device=jev_device,
        jev_backend=jev_backend,
        jev_live_base_url=jev_live_base_url,
        jev_live_api_key=jev_live_api_key,
        jev_live_model=jev_live_model,
        jev_live_endpoint=jev_live_endpoint,
        jev_live_timeout_seconds=jev_live_timeout_seconds,
        confidence_threshold=confidence_threshold,
        system2_enabled=system2_enabled,
        system2_provider=system2_provider,
        system2_dispatch=system2_dispatch,
        system2_enqueue=system2_enqueue,
    )
