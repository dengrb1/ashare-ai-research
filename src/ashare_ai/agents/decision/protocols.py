from __future__ import annotations

from abc import ABC, abstractmethod

from ashare_ai.agents.decision.models import MarketState, UnifiedDecision


class DecisionProvider(ABC):
    """统一决策接口，所有决策模式必须实现此协议"""

    @abstractmethod
    async def predict(self, market_state: MarketState) -> UnifiedDecision:
        """
        根据市场状态生成决策。

        Args:
            market_state: 点内时间市场状态输入

        Returns:
            UnifiedDecision: 统一决策输出，包含动作、风险、仓位和概率

        Raises:
            ValueError: 输入非法或违反 PIT 约束
            RuntimeError: 模型加载失败或推理异常
        """
        ...

    @property
    @abstractmethod
    def mode(self) -> str:
        """返回当前 Provider 的模式标识：legacy 或 jev"""
        ...

    @property
    @abstractmethod
    def model_version(self) -> str:
        """返回当前使用的模型版本"""
        ...
