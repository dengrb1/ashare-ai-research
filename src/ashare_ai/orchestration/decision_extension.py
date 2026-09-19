"""
决策扩展模块 - 在 calculate_scores 之后生成统一决策。

此模块扩展 builtin pipeline，在评分完成后生成 UnifiedDecision。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ashare_ai.agents.decision.models import UnifiedDecision
from ashare_ai.agents.decision.market_state import build_market_state_from_bundle
from ashare_ai.orchestration.decision_integration import generate_decision_from_composite_score

if TYPE_CHECKING:
    from ashare_ai.core.contracts import CompositeScore
    from ashare_ai.orchestration.bundle import CanonicalDailyBundle

logger = logging.getLogger(__name__)


class DecisionArtifact:
    """决策结果集合（可选 Artifact，不修改现有 pipeline）"""

    def __init__(self, decisions: tuple[UnifiedDecision, ...]):
        self.decisions = decisions
        self.by_symbol = {d.symbol: d for d in decisions}

    def __repr__(self) -> str:
        return f"DecisionArtifact(count={len(self.decisions)})"


async def generate_decisions_for_scores(
    scores: tuple[CompositeScore, ...],
    bundle: CanonicalDailyBundle,
    *,
    mode: str = "legacy",
    model_version: str = "legacy-v1.0.0",
) -> DecisionArtifact:
    """
    从评分结果批量生成决策。

    在 calculate_scores() 之后调用此函数，为每个 CompositeScore 生成 UnifiedDecision。

    Args:
        scores: CompositeScore 元组
        bundle: CanonicalDailyBundle
        mode: 决策模式
        model_version: 模型版本

    Returns:
        DecisionArtifact: 决策结果集合
    """
    decisions: list[UnifiedDecision] = []

    for score in scores:
        try:
            decision = await generate_decision_from_composite_score(
                composite_score=score,
                bundle=bundle,
                mode=mode,
                model_version=model_version,
            )
            decisions.append(decision)
        except Exception as e:
            logger.error(f"Failed to generate decision for {score.symbol}: {e}")
            # Fail-closed: 不继续处理该股票
            continue

    return DecisionArtifact(decisions=tuple(decisions))
