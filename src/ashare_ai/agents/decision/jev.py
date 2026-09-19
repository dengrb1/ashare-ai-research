from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ashare_ai.agents.decision.models import (
    ActionProbabilities,
    BinaryProbabilities,
    DecisionProbabilities,
    DirectionProbabilities,
    RiskProbabilities,
    UnifiedDecision,
)
from ashare_ai.agents.decision.protocols import DecisionProvider
from ashare_ai.core.hashing import stable_hash

if TYPE_CHECKING:
    from ashare_ai.agents.decision.models import MarketState

logger = logging.getLogger(__name__)


class JevDecisionProvider(DecisionProvider):
    """
    Jev-like 多任务 Transformer 监督学习决策 Provider。

    架构基于轻量 Transformer 编码器 + 多任务头：
    - 共享时序编码器（multi-head attention + FFN）
    - 6 个任务头：1日方向、5日方向、5日涨幅>3%、动作、风险、仓位
    - 纯监督学习，不使用强化学习
    - 支持 CPU 和 CUDA 推理
    """

    def __init__(
        self,
        model_dir: Path,
        model_version: str,
        checkpoint_path: Path | None = None,
        device: str = "auto",
    ):
        """
        初始化 Jev Provider。

        Args:
            model_dir: 模型根目录
            model_version: 模型版本标识
            checkpoint_path: checkpoint 文件路径，None 则使用默认
            device: 运行设备 auto/cpu/cuda
        """
        self._model_dir = model_dir
        self._model_version = model_version
        self._checkpoint_path = checkpoint_path
        self._device = device
        self._model: Any | None = None
        self._loaded = False

    @property
    def mode(self) -> str:
        return "jev"

    @property
    def model_version(self) -> str:
        return self._model_version

    def _load_model(self) -> None:
        """延迟加载模型，只在第一次推理时执行"""
        if self._loaded:
            return

        try:
            # 尝试导入 PyTorch
            import torch
        except ImportError as exc:
            raise RuntimeError(
                "PyTorch is not installed. Jev model requires torch as optional dependency. "
                "Install with: pip install torch"
            ) from exc

        # 确定设备
        if self._device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            device = self._device

        logger.info(
            "Loading Jev model",
            extra={
                "model_version": self._model_version,
                "device": device,
                "model_dir": str(self._model_dir),
            },
        )

        # 检查 checkpoint 是否存在
        if self._checkpoint_path is None:
            checkpoint_path = self._model_dir / self._model_version / "checkpoint.pt"
        else:
            checkpoint_path = self._checkpoint_path

        if not checkpoint_path.exists():
            raise FileNotFoundError(
                f"Jev checkpoint not found: {checkpoint_path}. "
                f"Train the model first using: ashare-ai train-jev"
            )

        # TODO: 加载实际的 Transformer 模型
        # 当前为骨架实现，返回 None 表示模型未实现
        logger.warning(
            "Jev model loading is skeleton implementation - actual model not yet trained"
        )
        self._model = None
        self._loaded = True

    async def predict(self, market_state: MarketState) -> UnifiedDecision:
        """
        使用 Jev 模型进行推理。

        Args:
            market_state: 市场状态输入

        Returns:
            UnifiedDecision: 统一决策输出

        Raises:
            RuntimeError: 模型加载失败或推理异常
            ValueError: 输入非法或概率非法
        """
        # 延迟加载模型
        self._load_model()

        if self._model is None:
            raise RuntimeError(
                "Jev model is not available. The model must be trained first. "
                "Current implementation is a skeleton placeholder."
            )

        # TODO: 实际的模型推理
        # 1. 特征编码：将 MarketState 转换为模型输入张量
        # 2. 前向传播：通过 Transformer 编码器和多任务头
        # 3. 概率解码：softmax 后归一化
        # 4. 校验：确保所有概率和为 1

        # 占位符：返回模拟输出
        logger.warning("Using skeleton Jev prediction - model inference not implemented")

        # 生成占位符概率分布（实际应从模型输出获取）
        direction_1d = DirectionProbabilities(UP=0.4, FLAT=0.35, DOWN=0.25)
        direction_5d = DirectionProbabilities(UP=0.45, FLAT=0.35, DOWN=0.2)
        up_over_3pct = BinaryProbabilities(YES=0.42, NO=0.58)
        action_probs = ActionProbabilities(BUY=0.45, HOLD=0.4, SELL=0.15)
        risk_probs = RiskProbabilities(LOW=0.3, MEDIUM=0.5, HIGH=0.2)

        # 从概率分布中选择最大概率的类别
        action = max(
            [("BUY", action_probs.BUY), ("HOLD", action_probs.HOLD), ("SELL", action_probs.SELL)],
            key=lambda x: x[1],
        )[0]
        risk = max(
            [("LOW", risk_probs.LOW), ("MEDIUM", risk_probs.MEDIUM), ("HIGH", risk_probs.HIGH)],
            key=lambda x: x[1],
        )[0]

        # 占位符仓位（实际应从模型第6个头输出）
        position = 30

        input_manifest = stable_hash(market_state.model_dump())

        return UnifiedDecision(
            symbol=market_state.symbol,
            trading_date=market_state.trading_date,
            available_at=market_state.available_at,
            decision_at=market_state.available_at,
            probabilities=DecisionProbabilities(
                direction_1d=direction_1d,
                direction_5d=direction_5d,
                up_over_3pct_5d=up_over_3pct,
                action=action_probs,
                risk=risk_probs,
            ),
            action=action,
            risk=risk,
            position=position,
            mode="jev",
            model_version=self._model_version,
            input_manifest_hash=input_manifest,
        )
