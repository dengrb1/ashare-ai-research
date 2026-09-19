from __future__ import annotations

import logging
from typing import TYPE_CHECKING

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
    from ashare_ai.core.contracts import CompositeScore

logger = logging.getLogger(__name__)


class LegacyDecisionProvider(DecisionProvider):
    """
    Legacy 决策 Provider，将现有 builtin/LLM 组件结果和 CompositeScore 转换为统一决策协议。

    保留旧 Provider、旧请求协议和旧输出接口，通过适配器接入新的统一决策协议。
    """

    def __init__(self, model_version: str = "legacy-v1.0.0"):
        self._model_version = model_version

    @property
    def mode(self) -> str:
        return "legacy"

    @property
    def model_version(self) -> str:
        return self._model_version

    async def predict(
        self,
        market_state: MarketState,
        composite_score: CompositeScore | None = None,
    ) -> UnifiedDecision:
        """
        将 Legacy 决策逻辑转换为统一协议。

        Args:
            market_state: 市场状态输入
            composite_score: 可选的预计算 CompositeScore（若无则使用简化映射）

        Returns:
            UnifiedDecision: 统一决策结果
        """
        if composite_score is not None:
            return self._convert_from_composite_score(composite_score, market_state)

        # 未提供 CompositeScore 时使用简化规则（骨架实现）
        logger.warning(
            "LegacyDecisionProvider: no composite_score provided, using fallback rules"
        )
        total_score = 50.0

        action, action_probs = self._score_to_action(total_score)
        risk, risk_probs = self._score_to_risk(total_score)
        position = self._score_to_position(total_score)
        direction_1d, direction_5d, up_over_3pct = self._score_to_direction(total_score)

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
            mode="legacy",
            model_version=self._model_version,
            input_manifest_hash=input_manifest,
        )

    def _convert_from_composite_score(
        self,
        composite_score: CompositeScore,
        market_state: MarketState,
    ) -> UnifiedDecision:
        """从 CompositeScore 转换为 UnifiedDecision"""
        total_score = composite_score.total_score

        action, action_probs = self._score_to_action(total_score)
        risk, risk_probs = self._score_to_risk(total_score)
        position = self._score_to_position(total_score)
        direction_1d, direction_5d, up_over_3pct = self._score_to_direction(total_score)

        return UnifiedDecision(
            symbol=composite_score.symbol,
            trading_date=composite_score.trading_date,
            available_at=market_state.available_at,
            decision_at=composite_score.decision_at,
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
            mode="legacy",
            model_version=self._model_version,
            input_manifest_hash=composite_score.agent_bundle_sha256,
        )

    def _score_to_action(self, score: float) -> tuple[str, ActionProbabilities]:
        """将评分转换为动作和动作概率"""
        if score >= 70:
            return "BUY", ActionProbabilities(BUY=0.7, HOLD=0.25, SELL=0.05)
        elif score >= 50:
            return "HOLD", ActionProbabilities(BUY=0.3, HOLD=0.6, SELL=0.1)
        else:
            return "SELL", ActionProbabilities(BUY=0.1, HOLD=0.3, SELL=0.6)

    def _score_to_risk(self, score: float) -> tuple[str, RiskProbabilities]:
        """将评分转换为风险等级和风险概率"""
        if score >= 70:
            return "LOW", RiskProbabilities(LOW=0.7, MEDIUM=0.25, HIGH=0.05)
        elif score >= 50:
            return "MEDIUM", RiskProbabilities(LOW=0.2, MEDIUM=0.6, HIGH=0.2)
        else:
            return "HIGH", RiskProbabilities(LOW=0.05, MEDIUM=0.35, HIGH=0.6)

    def _score_to_position(self, score: float) -> int:
        """将评分转换为仓位枚举"""
        if score >= 80:
            return 70
        elif score >= 70:
            return 50
        elif score >= 60:
            return 30
        elif score >= 50:
            return 20
        elif score >= 40:
            return 10
        else:
            return 0

    def _score_to_direction(
        self, score: float
    ) -> tuple[DirectionProbabilities, DirectionProbabilities, BinaryProbabilities]:
        """将评分转换为方向概率"""
        if score >= 70:
            dir_1d = DirectionProbabilities(UP=0.6, FLAT=0.3, DOWN=0.1)
            dir_5d = DirectionProbabilities(UP=0.65, FLAT=0.25, DOWN=0.1)
            up_3pct = BinaryProbabilities(YES=0.55, NO=0.45)
        elif score >= 50:
            dir_1d = DirectionProbabilities(UP=0.4, FLAT=0.4, DOWN=0.2)
            dir_5d = DirectionProbabilities(UP=0.45, FLAT=0.35, DOWN=0.2)
            up_3pct = BinaryProbabilities(YES=0.4, NO=0.6)
        else:
            dir_1d = DirectionProbabilities(UP=0.2, FLAT=0.3, DOWN=0.5)
            dir_5d = DirectionProbabilities(UP=0.25, FLAT=0.25, DOWN=0.5)
            up_3pct = BinaryProbabilities(YES=0.25, NO=0.75)

        return dir_1d, dir_5d, up_3pct


def convert_composite_score_to_decision(
    composite_score: CompositeScore,
    market_state: MarketState,
    model_version: str = "legacy-v1.0.0",
) -> UnifiedDecision:
    """
    将现有 CompositeScore 转换为 UnifiedDecision。

    这是一个工具函数，用于在集成阶段将旧的评分结果适配到新协议。
    """
    total_score = composite_score.total_score

    # 使用 LegacyDecisionProvider 的内部逻辑
    provider = LegacyDecisionProvider(model_version=model_version)

    action, action_probs = provider._score_to_action(total_score)
    risk, risk_probs = provider._score_to_risk(total_score)
    position = provider._score_to_position(total_score)
    direction_1d, direction_5d, up_over_3pct = provider._score_to_direction(total_score)

    input_manifest = composite_score.agent_bundle_sha256

    return UnifiedDecision(
        symbol=composite_score.symbol,
        trading_date=composite_score.trading_date,
        available_at=market_state.available_at,
        decision_at=composite_score.decision_at,
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
        mode="legacy",
        model_version=model_version,
        input_manifest_hash=input_manifest,
    )
