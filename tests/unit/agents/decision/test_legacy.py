from __future__ import annotations

import pytest
from datetime import date, datetime, timezone

from ashare_ai.agents.decision.legacy import LegacyDecisionProvider
from ashare_ai.agents.decision.models import MarketState


@pytest.mark.asyncio
class TestLegacyDecisionProvider:
    """测试 Legacy Provider 输出统一协议"""

    async def test_legacy_provider_returns_unified_decision(self):
        provider = LegacyDecisionProvider()

        market_state = MarketState(
            symbol="600000.SH",
            trading_date=date(2026, 7, 17),
            available_at=datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc),
            open=10.0,
            high=10.5,
            low=9.8,
            close=10.2,
            volume=1000000.0,
            amount=10000000.0,
        )

        decision = await provider.predict(market_state)

        assert decision.symbol == "600000.SH"
        assert decision.trading_date == date(2026, 7, 17)
        assert decision.mode == "legacy"
        assert decision.model_version == "legacy-v1.0.0"
        assert decision.action in ["BUY", "HOLD", "SELL"]
        assert decision.risk in ["LOW", "MEDIUM", "HIGH"]
        assert decision.position in [0, 10, 20, 30, 50, 70, 100]

        # 校验概率和
        probs = decision.probabilities
        assert abs(probs.direction_1d.UP + probs.direction_1d.FLAT + probs.direction_1d.DOWN - 1.0) < 1e-6
        assert abs(probs.direction_5d.UP + probs.direction_5d.FLAT + probs.direction_5d.DOWN - 1.0) < 1e-6
        assert abs(probs.up_over_3pct_5d.YES + probs.up_over_3pct_5d.NO - 1.0) < 1e-6
        assert abs(probs.action.BUY + probs.action.HOLD + probs.action.SELL - 1.0) < 1e-6
        assert abs(probs.risk.LOW + probs.risk.MEDIUM + probs.risk.HIGH - 1.0) < 1e-6

    async def test_legacy_provider_mode_property(self):
        provider = LegacyDecisionProvider()
        assert provider.mode == "legacy"

    async def test_legacy_provider_model_version(self):
        provider = LegacyDecisionProvider(model_version="legacy-v2.0.0")
        assert provider.model_version == "legacy-v2.0.0"
