from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ashare_ai.agents.decision.protocols import DecisionProvider

if TYPE_CHECKING:
    from ashare_ai.agents.decision.models import DecisionMode, MarketState, UnifiedDecision

logger = logging.getLogger(__name__)


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
        mode: DecisionMode,
        fallback_enabled: bool = False,
        jev_model_dir: Path | None = None,
        jev_model_version: str = "jev-baseline-v1",
        jev_checkpoint: Path | None = None,
        jev_device: str = "auto",
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

        self._primary_provider: DecisionProvider | None = None
        self._fallback_provider: DecisionProvider | None = None

    def _get_provider(self, mode: DecisionMode) -> DecisionProvider:
        """根据模式创建 Provider"""
        if mode == "legacy":
            from ashare_ai.agents.decision.legacy import LegacyDecisionProvider

            return LegacyDecisionProvider()
        elif mode == "jev":
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


def create_decision_router(
    mode: DecisionMode,
    fallback_enabled: bool = False,
    jev_model_dir: Path | None = None,
    jev_model_version: str = "jev-baseline-v1",
    jev_checkpoint: Path | None = None,
    jev_device: str = "auto",
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
    )
