"""Read compatibility shim for callers that still import the old provider.

The legacy name is retained only so old integrations can be upgraded without
an import-time failure.  It delegates to the versioned factor provider and
therefore never creates a ``mode=legacy`` or Jev decision.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ashare_ai.agents.decision.factor import FactorDecisionProvider
from ashare_ai.agents.decision.models import ActionProbabilities, MarketState, UnifiedDecision
from ashare_ai.scoring.factor import factor_action, factor_position

if TYPE_CHECKING:
    from ashare_ai.core.contracts import CompositeScore


class LegacyDecisionProvider(FactorDecisionProvider):
    """Deprecated alias for :class:`FactorDecisionProvider`."""

    def __init__(self, model_version: str | None = None) -> None:
        # The old argument is accepted for import compatibility but cannot
        # override the versioned factor formula.
        del model_version
        super().__init__()

    async def predict(
        self, market_state: MarketState, composite_score: CompositeScore | None = None
    ) -> UnifiedDecision:
        return await super().predict(market_state, composite_score=composite_score)

    def _score_to_action(self, score: float) -> tuple[str, ActionProbabilities]:
        action = factor_action(score, self.genome)
        tilt = max(-1.0, min(1.0, (score - 60.0) / 20.0))
        buy = 0.25 + 0.45 * max(tilt, 0.0)
        sell = 0.25 + 0.45 * max(-tilt, 0.0)
        hold = max(0.02, 1.0 - buy - sell)
        total = buy + hold + sell
        return action, ActionProbabilities(BUY=buy / total, HOLD=hold / total, SELL=sell / total)

    def _score_to_position(self, score: float) -> int:
        return factor_position(score, self.genome)


async def _predict_from_score(
    composite_score: CompositeScore, market_state: MarketState
) -> UnifiedDecision:
    return await LegacyDecisionProvider().predict(market_state, composite_score=composite_score)


def convert_composite_score_to_decision(
    *, composite_score: CompositeScore, market_state: MarketState, model_version: str | None = None
) -> UnifiedDecision:
    """Synchronous compatibility helper for historical integrations."""

    import asyncio

    return asyncio.run(_predict_from_score(composite_score, market_state))


__all__ = ["LegacyDecisionProvider", "convert_composite_score_to_decision"]
